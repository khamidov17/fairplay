"""Tests for the frontend contest editor (add + edit share ``ContestCreate``).

Covers the payload→model helpers, the edit mode added at
``/contests/<key>/edit/``, the newly registered model fields, and the
regressions fixed while refactoring (invalid dates, naive datetimes, bad M2M
ids). Uses ``force_login`` so the tests exercise view-level permission logic
rather than password authentication.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Class, Contest, ContestParticipation, ContestTag, Organization,
    SEBConfigPreset, StoredFileRecord,
)
from judge.models.seb import DEFAULT_PRESET_CACHE_KEY
from judge.models.tests.util import CommonDataMixin, create_contest, create_problem, create_user
from judge.views.contests import ContestCreate

PRESET_BEK = 'e' * 64
PRESET_CFG = 'f' * 64


def _create_preset(**kwargs):
    """The site-default SEB configuration an instructor can adopt in one click."""
    kwargs.setdefault('is_default', True)
    preset = SEBConfigPreset(name='Editor default', browser_exam_keys=PRESET_BEK,
                             config_keys=PRESET_CFG, **kwargs)
    preset.config_file.save('editor_default.seb', ContentFile(b'SEB config'), save=False)
    preset.save()
    return preset


class ContestEditorHelperTestCase(TestCase):
    """Unit tests for the static payload→model helpers on ``ContestCreate``."""

    def test_parse_datetime_aware_with_z(self):
        dt = ContestCreate._parse_datetime({'start_time': '2026-08-01T10:00:00Z'}, 'start_time')
        self.assertEqual(dt, datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))

    def test_parse_datetime_naive_treated_as_utc(self):
        # A timestamp without an offset must not produce a naive datetime (that
        # would crash when compared against aware values) — interpret as UTC.
        dt = ContestCreate._parse_datetime({'start_time': '2026-08-01T10:00:00'}, 'start_time')
        self.assertTrue(timezone.is_aware(dt))
        self.assertEqual(dt, datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))

    def test_parse_datetime_blank_returns_none(self):
        self.assertIsNone(ContestCreate._parse_datetime({'start_time': ''}, 'start_time'))
        self.assertIsNone(ContestCreate._parse_datetime({}, 'start_time'))

    def test_parse_datetime_invalid_raises_validation_error(self):
        # Regression: this used to raise TypeError inside gettext, 500ing the view.
        with self.assertRaises(ValidationError):
            ContestCreate._parse_datetime({'start_time': 'not-a-date'}, 'start_time')

    def test_parse_duration(self):
        self.assertEqual(ContestCreate._parse_duration({'duration_hours': '2', 'duration_minutes': '30'}),
                         timedelta(hours=2, minutes=30))
        self.assertIsNone(ContestCreate._parse_duration({'duration_hours': '0', 'duration_minutes': '0'}))
        self.assertIsNone(ContestCreate._parse_duration({}))

    def test_error_response_maps_validation_error_to_400(self):
        response = ContestCreate._error_response(ValidationError('boom'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('boom', json.loads(response.content)['error'])

    def test_error_response_maps_integrity_error_without_id_wording(self):
        with self.assertLogs('judge.views.contests', level='ERROR'):
            response = ContestCreate._error_response(IntegrityError('contestproblem unique'))
        self.assertEqual(response.status_code, 400)
        error = json.loads(response.content)['error']
        self.assertIn('not an ID conflict', error)
        self.assertNotIn('already exists', error)
        self.assertNotIn('contestproblem', error)

    def test_error_response_masks_internal_errors(self):
        with self.assertLogs('judge.views.contests', level='ERROR'):
            response = ContestCreate._error_response(RuntimeError('internal secret'))
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('secret', json.loads(response.content)['error'])
        self.assertIn('assignment', json.loads(response.content)['error'].lower())
        self.assertNotIn('contest', json.loads(response.content)['error'].lower())

    def test_queue_rescore_uses_contest_task(self):
        with patch('judge.tasks.rescore_contest') as mock_task:
            ContestCreate._queue_rescore('somekey')
        mock_task.s.assert_called_once_with('somekey')

    def test_contest_problem_points_defaults_to_library_score(self):
        problem = create_problem('helper-pts', is_public=True, points=25)
        self.assertEqual(ContestCreate._contest_problem_points(None, problem), 25)
        self.assertEqual(ContestCreate._contest_problem_points('', problem), 25)
        self.assertEqual(ContestCreate._contest_problem_points('bogus', problem), 25)
        self.assertEqual(ContestCreate._contest_problem_points(40, problem), 40)
        self.assertEqual(ContestCreate._contest_problem_points('40', problem), 40)


class ContestEditViewTestCase(CommonDataMixin, TestCase):
    """Edit mode: access control, prefill, in-place update, key rename, M2M."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # A full contest editor: edit_all_contest plus every granular permission
        # the Django admin gates behind (contest_rating, lock_contest, access
        # codes, SEB, visibility, private contests, label script, perf ceiling).
        # Without them, ContestCreate._restricted_fields correctly refuses to
        # write those fields — see the permission-gating tests below.
        cls.editor = create_user(username='contest_editor', user_permissions=(
            'add_contest', 'edit_own_contest', 'edit_all_contest', 'contest_rating', 'lock_contest',
            'contest_access_code',
            'contest_seb_requirement', 'create_private_contest', 'change_contest_visibility',
            'contest_problem_label', 'override_performance_ceiling',
        ))
        cls.curator = create_user(username='contest_curator', user_permissions=('edit_own_contest',))
        cls.normal = create_user(username='contest_normal')
        cls.organization = Organization.objects.create(name='Edit Org', slug='edit-org')

        _now = timezone.now()
        cls.contest = create_contest(
            key='editablecontest', name='Editable',
            start_time=_now + timedelta(days=1), end_time=_now + timedelta(days=2),
            authors=('superuser',),
            curators=('contest_curator',),
            description='original description',
        )
        # A contest this user does NOT curate — an edit_own_contest holder must
        # not be able to touch it.
        cls.other_contest = create_contest(
            key='othercontest', name='Other',
            start_time=_now + timedelta(days=1), end_time=_now + timedelta(days=2),
            authors=('superuser',),
        )

    def setUp(self):
        self.client = Client()

    def _full_payload(self, **overrides):
        """A complete payload — every field the editor can save."""
        now = timezone.now()
        payload = {
            'name': 'Renamed', 'key': 'editablecontest',
            'start_time': (now + timedelta(days=3)).isoformat(),
            'end_time': (now + timedelta(days=4)).isoformat(),
            'duration_hours': 1, 'duration_minutes': 0,
            'description': 'updated description',
            'is_visible': True, 'is_rated': False, 'use_clarifications': True,
            'hide_problem_tags': False, 'hide_problem_authors': False,
            'allow_virtual_participation': True, 'allow_spectate': False, 'run_pretests_only': True,
            'rate_all': False, 'tester_see_submissions': False, 'tester_see_scoreboard': True,
            'show_short_display': False, 'is_private': False, 'is_organization_private': False,
            'limit_join_organizations': False, 'seb_required': False,
            'format_name': 'default', 'scoreboard_visibility': 'V', 'points_precision': 3,
            'format_config': '', 'access_code': '', 'problem_label_script': '',
            'og_image': '', 'logo_override_image': '', 'summary': '',
            'rating_floor': '', 'rating_ceiling': '', 'performance_ceiling_override': '',
            'locked_after': '', 'seb_browser_exam_keys': '', 'seb_config_keys': '',
            'seb_quit_url': '', 'description_extra': None,
            'authors': [], 'curators': [], 'testers': [], 'spectators': [],
            'organizations': [], 'classes': [], 'join_organizations': [], 'tags': [],
            'private_contestants': [], 'rate_exclude': [], 'view_contest_scoreboard': [],
            'view_contest_submissions': [], 'banned_users': [],
        }
        payload.update(overrides)
        return payload

    def test_get_unknown_contest_returns_404(self):
        self.client.force_login(self.editor)
        response = self.client.get(reverse('contest_edit', args=['doesnotexist']))
        self.assertEqual(response.status_code, 404)

    def test_get_denied_for_user_without_permission(self):
        self.client.force_login(self.normal)
        response = self.client.get(reverse('contest_edit', args=['editablecontest']))
        self.assertEqual(response.status_code, 403)

    def test_get_allowed_for_edit_all_holder(self):
        self.client.force_login(self.editor)
        response = self.client.get(reverse('contest_edit', args=['editablecontest']))
        self.assertEqual(response.status_code, 200)

    def test_get_allowed_for_curator_of_that_contest(self):
        self.client.force_login(self.curator)
        response = self.client.get(reverse('contest_edit', args=['editablecontest']))
        self.assertEqual(response.status_code, 200)

    def test_get_denied_for_curator_of_another_contest(self):
        # edit_own_contest is object-scoped: curating contest A does not grant
        # access to contest B.
        self.client.force_login(self.curator)
        response = self.client.get(reverse('contest_edit', args=['othercontest']))
        self.assertEqual(response.status_code, 403)

    def test_get_prefills_contest_in_context(self):
        self.client.force_login(self.editor)
        response = self.client.get(reverse('contest_edit', args=['editablecontest']))
        self.assertEqual(response.context['contest'], self.contest)
        self.assertTrue(response.context['is_edit'])
        self.assertEqual(response.context['prefill']['name'], 'Editable')
        self.assertEqual(response.context['prefill']['description'], 'original description')
        self.assertFalse(response.context['prefill']['allow_ai_hints'])
        self.assertFalse(response.context['prefill']['hide_sandbox_output'])

    def test_edit_profile_selects_use_name_username_labels(self):
        """Authors/curators chips match profile_select2: Name (username)."""
        self.curator.first_name = '张三'
        self.curator.save()
        self.client.force_login(self.editor)
        response = self.client.get(reverse('contest_edit', args=['editablecontest']))
        labels = [label for _, label in response.context['prefill']['curators']]
        self.assertIn('张三 (contest_curator)', labels)
        self.assertContains(response, '张三 (contest_curator)')
        html = response.content.decode()
        self.assertNotIn('class="crumb"', html)
        self.assertNotIn('sp-layout-switch', html)

    def test_post_roundtrips_allow_ai_hints(self):
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(self._full_payload(allow_ai_hints=True)),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertTrue(self.contest.allow_ai_hints)
        response = self.client.get(reverse('contest_edit', args=['editablecontest']))
        self.assertTrue(response.context['prefill']['allow_ai_hints'])
        self.assertContains(response, 'id="id_allow_ai_hints"')

    def test_post_roundtrips_hide_sandbox_output(self):
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(self._full_payload(hide_sandbox_output=True)),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertTrue(self.contest.hide_sandbox_output)
        response = self.client.get(reverse('contest_edit', args=['editablecontest']))
        self.assertTrue(response.context['prefill']['hide_sandbox_output'])
        self.assertContains(response, 'id="id_hide_sandbox_output"')
        html = response.content.decode()
        self.assertRegex(html, r'id="id_hide_sandbox_output"[^>]*checked')
        self.assertNotIn('lock_non_code_part', html)

    def test_get_echoes_using_profile_timezone(self):
        """The editor's Validity fields echo stored UTC in the profile timezone."""
        self.client.force_login(self.editor)
        profile = self.editor.profile
        profile.timezone = 'America/Toronto'
        profile.save()
        from zoneinfo import ZoneInfo
        start_utc = self.contest.start_time
        response = self.client.get(reverse('contest_edit', args=['editablecontest']))
        self.assertEqual(response.context['user_timezone'], 'America/Toronto')
        self.assertEqual(response.context['prefill']['start_time'],
                         start_utc.astimezone(ZoneInfo('America/Toronto')).strftime('%Y-%m-%dT%H:%M'))

    def test_post_wall_clock_in_profile_timezone_roundtrips(self):
        """Regression: a teacher editing in a non-browser timezone must be able
        to set a wall-clock time that is stored as the matching UTC instant.

        Frontend contract: the datetime-local input ``2026-08-04T22:56`` (in the
        profile timezone, here Asia/Shanghai) is sent as ``2026-08-04T14:56:00.000Z``
        (22:56 CST = 14:56 UTC). The backend must store that instant, and echoing
        back in the profile timezone must reproduce 22:56.
        """
        self.client.force_login(self.editor)
        profile = self.editor.profile
        profile.timezone = 'Asia/Shanghai'
        profile.save()
        from zoneinfo import ZoneInfo
        payload = self._full_payload(
            start_time='2026-08-04T14:56:00.000Z',
            end_time='2026-08-05T14:56:00.000Z',
        )
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.start_time,
                         datetime(2026, 8, 4, 14, 56, tzinfo=timezone.utc))
        # Echo back in the same wall-clock zone → the teacher sees 22:56 again.
        response = self.client.get(reverse('contest_edit', args=['editablecontest']))
        self.assertEqual(response.context['user_timezone'], 'Asia/Shanghai')
        self.assertEqual(response.context['prefill']['start_time'],
                         self.contest.start_time.astimezone(ZoneInfo('Asia/Shanghai'))
                         .strftime('%Y-%m-%dT%H:%M'))
        self.assertEqual(response.context['prefill']['start_time'], '2026-08-04T22:56')
        # A student in US Eastern must see the *same instant* (10:56 EDT), not
        # the 12h-shifted value the pre-fix conversion produced.
        self.assertEqual(self.contest.start_time.astimezone(ZoneInfo('America/New_York'))
                         .strftime('%Y-%m-%dT%H:%M'), '2026-08-04T10:56')

    def test_post_updates_scalar_fields(self):
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(self._full_payload()),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.name, 'Renamed')
        self.assertEqual(self.contest.description, 'updated description')
        self.assertTrue(self.contest.is_visible)
        self.assertTrue(self.contest.run_pretests_only)
        self.assertTrue(self.contest.tester_see_scoreboard)
        self.assertEqual(self.contest.time_limit, timedelta(hours=1))

    def test_update_writes_assignment_log_entry(self):
        self.client.force_login(self.editor)
        payload = self._full_payload(name='Logged Edit')
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        ct = ContentType.objects.get_for_model(Contest)
        entry = LogEntry.objects.filter(content_type=ct, object_id=str(self.contest.pk)).latest('action_time')
        self.assertEqual(entry.action_flag, CHANGE)
        self.assertEqual(entry.user_id, self.editor.id)
        for blob in (entry.object_repr, entry.change_message):
            self.assertIn('assignment', blob.lower())
            self.assertNotIn('contest', blob.lower())

    def test_post_renames_key(self):
        self.client.force_login(self.editor)
        payload = self._full_payload(key='renamedkey')
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.key, 'renamedkey')

    def test_post_rename_key_conflict_returns_400(self):
        self.client.force_login(self.editor)
        payload = self._full_payload(key='othercontest')  # taken by another contest
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('already exists', json.loads(response.content)['error'])

    def test_post_replaces_m2m_fields(self):
        self.client.force_login(self.editor)
        payload = self._full_payload(curators=[self.curator.id], testers=[self.normal.id])
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertEqual(set(self.contest.curators.values_list('user_id', flat=True)), {self.curator.id})
        self.assertEqual(set(self.contest.testers.values_list('user_id', flat=True)), {self.normal.id})

    def test_post_clears_m2m_with_empty_list(self):
        self.client.force_login(self.editor)
        # First set a curator, then clear it via an empty list.
        payload = self._full_payload(curators=[self.curator.id])
        self.client.post(reverse('contest_edit', args=['editablecontest']),
                         data=json.dumps(payload), content_type='application/json')
        payload = self._full_payload(curators=[])
        response = self.client.post(reverse('contest_edit', args=['editablecontest']),
                                    data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.curators.count(), 0)

    def test_post_clears_private_contestants_with_empty_list(self):
        # The editor must POST [] after the teacher removes every Private
        # assignee. An omitted key keeps the stored rows.
        self.client.force_login(self.editor)
        assignee_id = self.normal.profile.id
        payload = self._full_payload(vis_mode='private', private_contestants=[assignee_id])
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertTrue(self.contest.is_private)
        self.assertEqual(list(self.contest.private_contestants.values_list('id', flat=True)), [assignee_id])

        payload = self._full_payload(vis_mode='private', private_contestants=[])
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.private_contestants.count(), 0)

    def test_post_omitted_private_contestants_keeps_existing(self):
        self.client.force_login(self.editor)
        assignee_id = self.normal.profile.id
        payload = self._full_payload(vis_mode='private', private_contestants=[assignee_id])
        self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        payload = self._full_payload(vis_mode='private')
        del payload['private_contestants']
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(list(self.contest.private_contestants.values_list('id', flat=True)), [assignee_id])

    def test_post_denied_for_user_without_permission(self):
        self.client.force_login(self.normal)
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(self._full_payload()), content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_post_invalid_date_returns_400(self):
        self.client.force_login(self.editor)
        payload = self._full_payload(start_time='not-a-date')
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    # ── security: granular permission gating (mirrors the Django admin) ─────

    def test_curator_cannot_escalate_visibility(self):
        # A curator holding only edit_own_contest must not be able to flip
        # is_visible (needs change_contest_visibility) — the admin makes the
        # field read-only for them, and so does the frontend editor.
        self.client.force_login(self.curator)
        payload = self._full_payload(is_visible=True)
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertFalse(self.contest.is_visible)

    def test_curator_cannot_arm_seb_or_set_access_code(self):
        # contest_seb_requirement / contest_access_code are separate permissions.
        self.client.force_login(self.curator)
        payload = self._full_payload(
            seb_required=True, seb_browser_exam_keys='a' * 64,
            access_code='sneaky',
        )
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertFalse(self.contest.seb_required)
        self.assertEqual(self.contest.access_code, '')

    def test_curator_cannot_adopt_default_seb_preset(self):
        # seb_preset is gated by the same permission as the rest of the SEB
        # fields, so the one-click checkbox is not a way around it.
        _create_preset()
        self.client.force_login(self.curator)
        payload = self._full_payload(seb_required=True, seb_use_default=True)
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertIsNone(self.contest.seb_preset)

    def test_curator_cannot_mark_rated_or_lock(self):
        # contest_rating / lock_contest are separate permissions.
        self.client.force_login(self.curator)
        payload = self._full_payload(is_rated=True, rate_all=True, locked_after='2030-01-01T00:00:00Z')
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertFalse(self.contest.is_rated)
        self.assertIsNone(self.contest.locked_after)

    def test_curator_cannot_change_organizations(self):
        # create_private_contest is required to scope a contest to an org.
        self.client.force_login(self.curator)
        payload = self._full_payload(organizations=[self.organization.id])
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.organizations.count(), 0)

    # ── vis_mode seg control is the single source of truth for visibility ──

    def test_vis_mode_public_sets_is_visible(self):
        self.client.force_login(self.editor)
        payload = self._full_payload(vis_mode='public')
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertTrue(self.contest.is_visible)
        self.assertFalse(self.contest.is_private)
        self.assertFalse(self.contest.is_organization_private)

    def test_vis_mode_org_sets_org_private(self):
        self.client.force_login(self.editor)
        payload = self._full_payload(vis_mode='org')
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertTrue(self.contest.is_visible)
        self.assertTrue(self.contest.is_organization_private)

    def test_vis_mode_hidden_hides(self):
        # First make the contest visible, then hide it again via the seg.
        self.contest.is_visible = True
        self.contest.save()
        self.client.force_login(self.editor)
        payload = self._full_payload(vis_mode='hidden')
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertFalse(self.contest.is_visible)

    def test_duration_out_of_range_returns_400(self):
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(self._full_payload(duration_hours=5000)), content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_curator_cannot_attach_hidden_problem(self):
        # Security: a curator holding only edit_own_contest must not attach
        # another teacher's hidden problem to a contest students can see —
        # the contest detail page would otherwise leak it to them.
        hidden = create_problem('hidden-attach')  # is_public defaults False
        self.client.force_login(self.curator)
        payload = self._full_payload(problems=[{'id': hidden.id}])
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('not allowed', json.loads(response.content)['error'])

    def test_curator_keeps_existing_hidden_problem_when_saving(self):
        # Regression guard: an admin-attached hidden problem must not block a
        # curator from saving the contest (existing problems stay allowed).
        from judge.models import ContestProblem
        hidden = create_problem('hidden-existing')
        ContestProblem.objects.create(contest=self.contest, problem=hidden, points=100, order=99)
        self.client.force_login(self.curator)
        payload = self._full_payload(problems=[{'id': hidden.id, 'points': 100}])
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertTrue(self.contest.contest_problems.filter(problem=hidden).exists())

    def test_switch_icpc_to_exam_clears_leftover_config(self):
        """Exam rejects non-empty config; leftover ICPC JSON must not 400 Save."""
        self.contest.format_name = 'icpc'
        self.contest.format_config = {'penalty': 20}
        self.contest.save()
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(self._full_payload(format_name='exam', format_config='{"penalty": 20}')),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.format_name, 'exam')
        self.assertIsNone(self.contest.format_config)

    def test_switch_icpc_to_exam_omitted_config_still_clears(self):
        """Frontend used to drop a blank format_config key; leftover DB JSON
        must still be cleared when switching to exam."""
        self.contest.format_name = 'icpc'
        self.contest.format_config = {'penalty': 20}
        self.contest.save()
        self.client.force_login(self.editor)
        payload = self._full_payload(format_name='exam')
        del payload['format_config']
        response = self.client.post(
            reverse('contest_edit', args=['editablecontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.format_name, 'exam')
        self.assertIsNone(self.contest.format_config)

    def test_format_change_queues_rescore(self):
        self.client.force_login(self.editor)
        with patch.object(ContestCreate, '_queue_rescore') as mock_rescore:
            response = self.client.post(
                reverse('contest_edit', args=['editablecontest']),
                data=json.dumps(self._full_payload(format_name='exam')),
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200, response.content)
        mock_rescore.assert_called_once_with('editablecontest')

    def test_problem_points_change_queues_rescore(self):
        from judge.models import ContestProblem
        problem = create_problem('rescore-pts', is_public=True)
        ContestProblem.objects.create(contest=self.contest, problem=problem, points=100, order=1)
        self.client.force_login(self.editor)
        with patch.object(ContestCreate, '_queue_rescore') as mock_rescore:
            response = self.client.post(
                reverse('contest_edit', args=['editablecontest']),
                data=json.dumps(self._full_payload(
                    problems=[{'id': problem.id, 'points': 50}],
                )),
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200, response.content)
        mock_rescore.assert_called_once_with('editablecontest')

    def test_name_only_change_does_not_queue_rescore(self):
        self.client.force_login(self.editor)
        with patch.object(ContestCreate, '_queue_rescore') as mock_rescore:
            response = self.client.post(
                reverse('contest_edit', args=['editablecontest']),
                data=json.dumps(self._full_payload(name='Still no rescore')),
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200, response.content)
        mock_rescore.assert_not_called()


class ContestCreateNewFieldsTestCase(CommonDataMixin, TestCase):
    """Create mode: newly registered model fields + regression fixes."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Same full editor as ContestEditViewTestCase (see comment there).
        cls.editor = create_user(username='fields_editor', user_permissions=(
            'add_contest', 'edit_own_contest', 'edit_all_contest', 'contest_rating', 'lock_contest',
            'contest_access_code',
            'contest_seb_requirement', 'create_private_contest', 'change_contest_visibility',
            'contest_problem_label', 'override_performance_ceiling',
        ))
        cls.user_a = create_user(username='fields_user_a')
        cls.user_b = create_user(username='fields_user_b')
        cls.organization = Organization.objects.create(name='Fields Org', slug='fields-org')
        cls.tag = ContestTag.objects.create(name='fields-tag', color='#00ff00')

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.editor)
        # The default-preset pointer is cached outside the test transaction, so
        # it would otherwise leak between tests.
        cache.delete(DEFAULT_PRESET_CACHE_KEY)

    def test_create_defaults_vis_mode_course(self):
        response = self.client.get(reverse('contest_create'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['prefill']['vis_mode'], 'org')
        self.assertRegex(response.content.decode(), r'name="vis_mode" value="org"\s*checked')

    def test_create_defaults_format_exam(self):
        response = self.client.get(reverse('contest_create'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['prefill']['format_name'], 'exam')
        html = response.content.decode()
        self.assertRegex(html, r'value="exam"\s+selected')

    def test_create_defaults_scoreboard_hidden(self):
        response = self.client.get(reverse('contest_create'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['prefill']['scoreboard_visibility'], Contest.SCOREBOARD_HIDDEN)
        html = response.content.decode()
        self.assertRegex(html, r'value="H"[^>]*selected')
        self.assertNotRegex(html, r'value="V"[^>]*selected')
        self.assertLess(html.find('value="H"'), html.find('value="V"'))
        self.assertIn('forceScoreboardHiddenDefault', html)
        self.assertIn("sb.value = 'H'", html)

    def test_create_defaults_hide_sandbox_output_off(self):
        response = self.client.get(reverse('contest_create'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['prefill']['hide_sandbox_output'])
        html = response.content.decode()
        self.assertIn('id="id_hide_sandbox_output"', html)
        self.assertNotRegex(html, r'id="id_hide_sandbox_output"[^>]*checked')
        self.assertNotIn('lock_non_code_part', html)
        self.assertNotIn('Lock non-programming part', html)

    def test_create_page_omits_crumb_and_layout_switch(self):
        """Tabs stay; the crumb row and Horizontal/Vertical switch under them do not."""
        html = self.client.get(reverse('contest_create')).content.decode()
        self.assertIn('Add Assignment', html)
        self.assertIn('fa-list', html)
        self.assertIn('fa-calendar', html)
        self.assertNotIn('class="crumb"', html)
        self.assertNotIn('sp-layout-switch', html)
        self.assertNotIn('data-layout="layout-lr"', html)
        self.assertNotIn('data-layout="layout-tb"', html)
        self.assertNotIn('>Horizontal<', html)
        self.assertNotIn('>Vertical<', html)
        self.assertIn('class="v2-split', html)
        self.assertIn('.pcv2 { background: transparent', html)
        # User pickers still talk to the shared profile_select2 (name + username).
        self.assertIn(reverse('profile_select2'), html)
        self.assertIn('class="profile-select2"', html)

    def test_virtual_join_label_not_participation(self):
        html = self.client.get(reverse('contest_create')).content.decode()
        self.assertIn('Allow virtual join', html)
        self.assertNotIn('Allow virtual participation', html)
        # Checkbox rows use an inline [box] label ? layout (form-row-check).
        self.assertIn('form-row-check', html)
        self.assertIn('id_allow_virtual_participation', html)

    def test_library_has_select_all_and_row_checkboxes(self):
        html = self.client.get(reverse('contest_create')).content.decode()
        self.assertIn('id="lib-select-all"', html)
        self.assertIn('lib-row-check', html)
        self.assertIn('colspan="7"', html)
        create_problem('lib-check-prob', is_public=True)
        html_with_rows = self.client.get(reverse('contest_create')).content.decode()
        self.assertIn('class="lib-row-check"', html_with_rows)

    def test_lib_row_html_out_prefix_defaults_to_10000(self):
        html = self.client.get(reverse('contest_create')).content.decode()
        self.assertIn('class="cp-out-prefix"', html)
        self.assertIn('value="10000"', html)

    def test_create_writes_assignment_log_entry(self):
        response = self._post(name='Logged Assignment', key='loggedasgn')
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(key='loggedasgn')
        ct = ContentType.objects.get_for_model(Contest)
        entry = LogEntry.objects.filter(content_type=ct, object_id=str(contest.pk)).latest('action_time')
        self.assertEqual(entry.action_flag, ADDITION)
        self.assertEqual(entry.user_id, self.editor.id)
        for blob in (entry.object_repr, entry.change_message):
            self.assertIn('assignment', blob.lower())
            self.assertNotIn('contest', blob.lower())

    def test_assignment_id_lives_in_advanced(self):
        html = self.client.get(reverse('contest_create')).content.decode()
        before, after = html.split('class="advanced-hidden"', 1)
        self.assertNotIn('id="id_key"', before)
        self.assertIn('id="id_key"', after)

    def test_create_key_is_blank_auto_generated(self):
        # Create mode must leave the id blank: it is auto-generated and reserved
        # atomically at save time, so pre-filling a "suggestion" would mislead
        # editors (two simultaneous creators would see the same suggestion but
        # land on different ids).
        response = self.client.get(reverse('contest_create'))
        self.assertEqual(response.context['prefill']['key'], '')
        html = response.content.decode()
        self.assertNotIn('generateUniqueContestKey', html)
        self.assertNotIn('?suggest=1', html)
        self.assertNotIn("replace(/[^a-z0-9]+/g, '')", html)
        self.assertIn('placeholder="Auto-generated"', html)

    def test_preview_title_comes_before_time(self):
        html = self.client.get(reverse('contest_create')).content.decode()
        preview = html.split('id="pv-body"', 1)[1].split('</aside>', 1)[0]
        self.assertLess(preview.find('id="pv-title"'), preview.find('id="pv-time"'))

    def test_preview_problems_link_and_read_display_name(self):
        """syncPreviewProblems must read the display_name input and link the name."""
        html = self.client.get(reverse('contest_create')).content.decode()
        self.assertIn('function syncPreviewProblems()', html)
        self.assertIn('function cpRowDisplayName(row)', html)
        self.assertIn('function cpRowOverrideName(row)', html)
        self.assertIn('input.cp-display-name', html)
        self.assertIn('maxlength="100" value="" placeholder="', html)
        self.assertNotIn("escapeHtml(row.dataset.name || 'Problem ' + row.dataset.pid)", html)
        self.assertIn('function assignmentProblemHref(code)', html)
        self.assertIn('assignment_preview=1', html)
        self.assertIn('preview_codes=', html)

    def test_create_empty_key_is_auto_incremented_not_name(self):
        response = self._post(name='Homework Name Copy', key='')
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(name='Homework Name Copy')
        self.assertNotEqual(contest.key, 'homeworknamecopy')
        self.assertRegex(contest.key, r'^c\d{6}$')

    def test_check_key_suggests_next_unused(self):
        occupied = Contest.generate_unique_key()
        create_contest(
            key=occupied, name='Occupied suggest key',
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
            authors=('superuser',),
        )
        response = self.client.get(reverse('contest_check_key'), {'suggest': '1'})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body['exists'])
        self.assertRegex(body['key'], r'^c\d{6}$')
        self.assertNotEqual(body['key'], occupied)

    def test_create_own_key_already_saved_returns_400(self):
        # Saving a second assignment under a key this user already owns must not
        # auto-redirect or silently renumber — it fails cleanly so the user can
        # pick a different ID without losing the in-progress edits.
        first = self._post(name='Mine', key='c000000')
        self.assertEqual(first.status_code, 200, first.content)
        retry = self._post(name='Mine again', key='c000000')
        self.assertEqual(retry.status_code, 400, retry.content)
        error = retry.json()['error']
        self.assertIn('already exists', error)
        self.assertIn('c000000', error)
        self.assertEqual(Contest.objects.filter(key='c000000').count(), 1)

    def test_create_own_key_already_saved_does_not_overwrite_existing(self):
        # The retry is a no-op on the stored row: the existing assignment keeps
        # its original name, and no duplicate row is created.
        self._post(name='Mine', key='c000000')
        retry = self._post(name='Mine again', key='c000000')
        self.assertEqual(retry.status_code, 400, retry.content)
        self.assertEqual(Contest.objects.filter(key='c000000').count(), 1)
        existing = Contest.objects.get(key='c000000')
        self.assertEqual(existing.name, 'Mine')

    def test_create_auto_key_ignores_submitted_key(self):
        # With key_auto the submitted key is only a suggestion: the server
        # reserves a fresh key atomically, so a stale suggestion never collides.
        self._post(name='Mine', key='c000000')
        retry = self._post(name='Mine again', key='c000000', key_auto=True)
        self.assertEqual(retry.status_code, 200, retry.content)
        body = retry.json()
        self.assertNotIn('id_changed', body)
        self.assertNotEqual(body['key'], 'c000000')
        self.assertEqual(Contest.objects.filter(key='c000000').count(), 1)
        self.assertTrue(Contest.objects.filter(name='Mine again').exists())

    def test_create_full_payload_produces_consistent_assignment(self):
        """A complete create round-trips every field category into one coherent
        assignment: auto-generated key, duration, problems (points/partial/order),
        M2M (orgs/tags/curators/testers), the creator as author, and a redirect."""
        problem = create_problem('completeness-prob', is_public=True)
        now = timezone.now()
        response = self._post(
            name='Complete Assignment',
            key='',
            start_time=(now + timedelta(hours=1)).isoformat(),
            end_time=(now + timedelta(hours=5)).isoformat(),
            duration_hours=1,
            duration_minutes=30,
            description='full description',
            format_name='default',
            scoreboard_visibility='V',
            problems=[{'id': problem.id, 'points': 150, 'partial': False}],
            organizations=[self.organization.id],
            tags=[self.tag.id],
            curators=[self.user_a.id],
            testers=[self.user_b.id],
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['action'], 'save')

        contest = Contest.objects.get(name='Complete Assignment')
        self.assertRegex(contest.key, r'^c\d{6}$')
        self.assertEqual(contest.time_limit, timedelta(hours=1, minutes=30))
        self.assertEqual(contest.description, 'full description')

        problems = list(contest.contest_problems.order_by('order'))
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0].problem_id, problem.id)
        self.assertEqual(problems[0].points, 150)
        self.assertFalse(problems[0].partial)
        self.assertEqual(problems[0].order, 1)

        self.assertCountEqual(list(contest.organizations.values_list('id', flat=True)), [self.organization.id])
        self.assertCountEqual(list(contest.tags.values_list('id', flat=True)), [self.tag.id])
        self.assertCountEqual(list(contest.curators.values_list('id', flat=True)), [self.user_a.id])
        self.assertCountEqual(list(contest.testers.values_list('id', flat=True)), [self.user_b.id])
        self.assertIn(self.editor.profile, contest.authors.all())
        self.assertEqual(body['redirect'], reverse('contest_view', args=[contest.key]))

    def test_create_series_key_taken_by_other_returns_400(self):
        occupied = Contest.generate_unique_key()
        create_contest(
            key=occupied, name='Other assignment',
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
            authors=('fields_user_a',),
        )
        response = self._post(name='Collision', key=occupied)
        self.assertEqual(response.status_code, 400, response.content)
        error = response.json()['error']
        self.assertIn('already exists', error)
        self.assertIn(occupied, error)
        self.assertFalse(Contest.objects.filter(name='Collision').exists())

    def test_create_custom_key_taken_returns_400_with_id(self):
        create_contest(
            key='homework1', name='Taken custom',
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
            authors=('fields_user_a',),
        )
        response = self._post(name='Dup custom', key='homework1')
        self.assertEqual(response.status_code, 400)
        error = response.json()['error']
        self.assertIn('already exists', error)
        self.assertIn('homework1', error)

    def test_create_series_key_without_auto_returns_400(self):
        occupied = Contest.generate_unique_key()
        create_contest(
            key=occupied, name='Forced collision',
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
            authors=('fields_user_a',),
        )
        response = self._post(name='Forced', key=occupied, key_auto=False)
        self.assertEqual(response.status_code, 400)
        error = response.json()['error']
        self.assertIn('already exists', error)
        self.assertIn(occupied, error)

    def test_create_series_key_race_returns_400(self):
        # A manual key that loses a race (another INSERT lands between our check
        # and our own INSERT) fails cleanly, not with a 500 or a silent renumber.
        original_save = Contest.save
        calls = {'n': 0}

        def racing_save(self, *args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise IntegrityError('duplicate key')
            return original_save(self, *args, **kwargs)

        with patch.object(Contest, 'save', racing_save):
            response = self._post(name='Race manual', key='c000000', key_auto=False)
        self.assertEqual(response.status_code, 400, response.content)
        error = response.json()['error']
        self.assertIn('already exists', error)
        self.assertFalse(Contest.objects.filter(name='Race manual').exists())

    def test_create_auto_key_race_reserves_next_free(self):
        # An auto key retries atomically when its INSERT loses a race, landing on
        # the next free id instead of colliding.
        original_save = Contest.save
        calls = {'n': 0}

        def racing_save(self, *args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise IntegrityError('duplicate key')
            return original_save(self, *args, **kwargs)

        with patch.object(Contest, 'save', racing_save):
            with patch.object(Contest, 'generate_unique_key', side_effect=['c000099', 'c000100']):
                response = self._post(name='Race auto', key='c000000', key_auto=True)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertNotIn('id_changed', body)
        self.assertEqual(body['key'], 'c000100')
        self.assertTrue(Contest.objects.filter(key='c000100', name='Race auto').exists())

    def test_create_related_integrity_error_is_not_id_conflict(self):
        with patch.object(ContestCreate, '_sync_problems', side_effect=IntegrityError('contestproblem unique')):
            with self.assertLogs('judge.views.contests', level='ERROR'):
                response = self._post(name='Related fail', key='quiz1')
        self.assertEqual(response.status_code, 400, response.content)
        error = response.json()['error']
        self.assertNotIn('already exists', error)
        self.assertIn('not an ID conflict', error)
        self.assertFalse(Contest.objects.filter(key='quiz1').exists())

    def test_create_series_related_integrity_error_does_not_retarget_id(self):
        with patch.object(ContestCreate, '_sync_problems', side_effect=IntegrityError('contestproblem unique')):
            with self.assertLogs('judge.views.contests', level='ERROR'):
                response = self._post(name='Related fail series', key='c000000')
        self.assertEqual(response.status_code, 400, response.content)
        error = response.json()['error']
        self.assertNotIn('already exists', error)
        self.assertIn('not an ID conflict', error)
        self.assertFalse(response.json().get('id_changed'))
        self.assertFalse(Contest.objects.filter(name='Related fail series').exists())

    def test_per_problem_code_options_hidden_for_other_types(self):
        from judge.models.question import ProblemMCQConfig
        code = create_problem('lock-code', is_public=True, submission_type='C')
        single = create_problem('lock-single', is_public=True, submission_type='M')
        ProblemMCQConfig.objects.create(problem=single, multiple_select=False, is_true_false=False)
        truefalse = create_problem('lock-tf', is_public=True, submission_type='M')
        ProblemMCQConfig.objects.create(problem=truefalse, multiple_select=False, is_true_false=True)
        multi = create_problem('lock-multi', is_public=True, submission_type='M')
        ProblemMCQConfig.objects.create(problem=multi, multiple_select=True)
        blank = create_problem('lock-blank', is_public=True, submission_type='B')
        short = create_problem('lock-short', is_public=True, submission_type='S')
        filep = create_problem('lock-file', is_public=True, submission_type='F')

        lib = self.client.get(reverse('contest_create')).content.decode()
        self.assertNotIn('applyCpOptionLocks', lib)
        self.assertIn('data-format="single"', lib)
        self.assertIn('data-format="truefalse"', lib)
        self.assertIn('data-format="multiple"', lib)
        self.assertIn('data-points="%s"' % int(code.points), lib)
        self.assertIn('function defaultCpPoints', lib)
        self.assertIn('cp-opt-partial', lib)
        self.assertIn('.cp-row:not([data-format="C"]) .cp-opt-pretested', lib)
        self.assertIn('.cp-row:not([data-format="C"]) .cp-opt-out-prefix', lib)

        response = self._post(
            name='Option locks', key='optlocks',
            problems=[
                {'id': code.id, 'points': 100},
                {'id': single.id, 'points': 100},
                {'id': truefalse.id, 'points': 100},
                {'id': multi.id, 'points': 100},
                {'id': blank.id, 'points': 100},
                {'id': short.id, 'points': 100},
                {'id': filep.id, 'points': 100},
            ],
        )
        self.assertEqual(response.status_code, 200, response.content)
        html = self.client.get(reverse('contest_edit', args=['optlocks'])).content.decode()

        def row_for(problem):
            marker = 'data-pid="%s"' % problem.id
            start = html.find(marker)
            self.assertNotEqual(start, -1, marker)
            open_div = html.rfind('<div class="cp-row"', 0, start)
            nxt = html.find('<div class="cp-row"', start + 1)
            if nxt == -1:
                nxt = html.find('id="cp-empty"', start)
            return html[open_div:nxt]

        def assert_code_opts(row, is_code):
            self.assertIn('cp-opt-partial', row)
            self.assertNotIn(' disabled', row.split('cp-opt-partial', 1)[1].split('</label>', 1)[0])
            if is_code:
                self.assertIn('cp-opt-pretested', row)
                self.assertIn('cp-opt-out-prefix', row)
            else:
                self.assertNotIn('cp-opt-pretested', row)
                self.assertNotIn('cp-opt-out-prefix', row)

        assert_code_opts(row_for(code), True)
        assert_code_opts(row_for(single), False)
        assert_code_opts(row_for(truefalse), False)
        assert_code_opts(row_for(multi), False)
        assert_code_opts(row_for(blank), False)
        assert_code_opts(row_for(short), False)
        assert_code_opts(row_for(filep), False)

    def test_format_and_virtual_live_in_basic(self):
        html = self.client.get(reverse('contest_create')).content.decode()
        before, after = html.split('class="advanced-hidden"', 1)
        self.assertIn('id="id_format_name"', before)
        self.assertIn('id="id_allow_virtual_participation"', before)
        self.assertNotIn('id="id_format_config"', before)
        self.assertNotIn('id="id_format_name"', after)
        self.assertNotIn('id="id_allow_virtual_participation"', after)
        self.assertIn('id="id_format_config"', after)
        self.assertEqual(html.count('id="id_format_name"'), 1)
        self.assertEqual(html.count('id="id_format_config"'), 1)
        self.assertEqual(html.count('id="id_allow_virtual_participation"'), 1)
        # Basic: after Target score, before Visibility; virtual follows format name.
        basic = before[before.find('Basic information'):]
        self.assertLess(basic.find('id="f-score"'), basic.find('id="id_format_name"'))
        self.assertLess(basic.find('id="id_format_name"'), basic.find('id="id_allow_virtual_participation"'))
        self.assertLess(basic.find('id="id_allow_virtual_participation"'), basic.find('name="vis_mode"'))
        # Advanced Format: config sits with the existing label script.
        self.assertLess(after.find('id="id_format_config"'), after.find('id="id_problem_label_script"'))

    def test_anti_cheat_has_no_level_presets(self):
        html = self.client.get(reverse('contest_create')).content.decode()
        ac = html[html.find('Anti-cheating'):html.find('id="advanced-btn"')]
        self.assertNotIn('name="cheat_level"', html)
        self.assertNotIn('High ≈ SEB exam', html)
        self.assertNotIn('value="high"', ac)
        self.assertNotIn('value="medium"', ac)
        self.assertNotIn('value="low"', ac)
        self.assertNotIn('value="custom"', ac)
        self.assertNotIn('>High<', ac)
        self.assertNotIn('>Medium<', ac)
        self.assertNotIn('>Low<', ac)
        self.assertNotIn('>Custom<', ac)
        self.assertIn('id="id_access_code"', ac)
        self.assertIn('id="id_seb_required"', ac)

    def test_advanced_help_tooltips_present(self):
        html = self.client.get(reverse('contest_create')).content.decode()
        before, advanced = html.split('class="advanced-hidden"', 1)
        self.assertIn('class="oj-tip"', advanced)
        self.assertIn('oj-tip-pop', advanced)
        self.assertIn('These users will be able to edit the assignment.', advanced)
        self.assertGreater(advanced.count('class="oj-tip"'), 10)
        basic = before[before.find('Basic information'):]
        self.assertIn('The assignment format module to use.', basic)
        self.assertIn('Allow students to start a virtual participation after the assignment ends.', basic)
        self.assertIn('Spectator submissions do not count toward statistics or rankings.', basic)
        self.assertIn('A JSON object to serve as the configuration for the chosen assignment format module.', advanced)

    def test_advanced_fields_round_trip(self):
        response = self._post(
            name='Adv RT', key='advrt',
            format_name='icpc',
            format_config='{"penalty": 20}',
            allow_virtual_participation=False,
            allow_spectate=True,
            use_clarifications=False,
            hide_problem_tags=True,
            show_short_display=True,
            summary='hello summary',
            og_image='https://example.com/og.png',
            logo_override_image='https://example.com/logo.png',
            points_precision=2,
            scoreboard_visibility='C',
            access_code='secret42',
            seb_required=False,
        )
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(key='advrt')
        self.assertEqual(contest.format_name, 'icpc')
        self.assertEqual(contest.format_config, {'penalty': 20})
        self.assertFalse(contest.allow_virtual_participation)
        self.assertTrue(contest.allow_spectate)
        self.assertFalse(contest.use_clarifications)
        self.assertTrue(contest.hide_problem_tags)
        self.assertTrue(contest.show_short_display)
        self.assertEqual(contest.summary, 'hello summary')
        self.assertEqual(contest.og_image, 'https://example.com/og.png')
        self.assertEqual(contest.logo_override_image, 'https://example.com/logo.png')
        self.assertEqual(contest.points_precision, 2)
        self.assertEqual(contest.scoreboard_visibility, 'C')
        self.assertEqual(contest.access_code, 'secret42')
        self.assertFalse(contest.seb_required)

        edit = self.client.get(reverse('contest_edit', args=['advrt']))
        self.assertEqual(edit.status_code, 200)
        prefill = edit.context['prefill']
        self.assertEqual(prefill['format_name'], 'icpc')
        self.assertFalse(prefill['allow_virtual_participation'])
        self.assertTrue(prefill['allow_spectate'])
        self.assertFalse(prefill['use_clarifications'])
        self.assertTrue(prefill['hide_problem_tags'])
        self.assertTrue(prefill['show_short_display'])
        self.assertEqual(prefill['summary'], 'hello summary')
        self.assertEqual(prefill['og_image'], 'https://example.com/og.png')
        self.assertEqual(prefill['access_code'], 'secret42')
        self.assertEqual(prefill['points_precision'], 2)
        self.assertEqual(prefill['scoreboard_visibility'], 'C')
        html = edit.content.decode()
        before, advanced = html.split('class="advanced-hidden"', 1)
        self.assertIn('id="id_format_name"', before)
        self.assertIn('id="id_allow_virtual_participation"', before)
        self.assertIn('id="id_allow_spectate"', before)
        self.assertNotIn('id="id_format_config"', before)
        self.assertNotIn('id="id_format_name"', advanced)
        self.assertNotIn('id="id_allow_virtual_participation"', advanced)
        self.assertNotIn('id="id_allow_spectate"', advanced)
        self.assertIn('id="id_format_config"', advanced)
        self.assertNotIn('name="cheat_level"', html)

    def test_join_organizations_multi_select_round_trip(self):
        org_a = Organization.objects.create(name='Join Org A', slug='join-org-a', is_open=True)
        org_b = Organization.objects.create(name='Join Org B', slug='join-org-b', is_open=True)
        response = self._post(
            name='Join M2M', key='joinm2m',
            limit_join_organizations=True,
            join_organizations=[org_a.id, org_b.id],
        )
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(key='joinm2m')
        self.assertTrue(contest.limit_join_organizations)
        self.assertCountEqual(
            list(contest.join_organizations.values_list('id', flat=True)),
            [org_a.id, org_b.id],
        )
        edit = self.client.get(reverse('contest_edit', args=['joinm2m']))
        self.assertEqual(edit.status_code, 200)
        html = edit.content.decode()
        self.assertIn('id="id_join_organizations"', html)
        self.assertRegex(html, r'id="id_join_organizations"[^>]*multiple')
        prefill = edit.context['prefill']
        self.assertCountEqual([o[0] for o in prefill['join_organizations']], [org_a.id, org_b.id])

    def test_create_omitted_scoreboard_visibility_saves_hidden(self):
        response = self._post(name='SB Default', key='sbdefault')
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(key='sbdefault')
        self.assertEqual(contest.scoreboard_visibility, Contest.SCOREBOARD_HIDDEN)

    def test_invalid_scoreboard_visibility_returns_400(self):
        response = self._post(name='SB Bad', key='sbbad', scoreboard_visibility='X')
        self.assertEqual(response.status_code, 400)
        self.assertIn('scoreboard', json.loads(response.content)['error'].lower())

    def test_create_new_problem_links_to_v2(self):
        adder = create_user(username='fields_problem_adder', user_permissions=(
            'add_contest', 'create_private_contest', 'create_private_problem',
        ))
        self.client.force_login(adder)
        html = self.client.get(reverse('contest_create')).content.decode()
        self.assertIn('/problems/new/', html)
        self.assertIn('from=assignment', html)
        self.assertIn('id="cp-create-new"', html)
        self.assertNotIn('/admin/judge/problem/add/', html)
        self.assertRegex(html, r'<a class="btn-soft" id="cp-create-new"')
        self.assertNotRegex(html, r'<a class="btn-soft alt" id="cp-create-new"')

    def test_visibility_sits_above_course(self):
        html = self.client.get(reverse('contest_create')).content.decode()
        vis = html.find('id="vis-seg"')
        course = html.find('id="id_organizations"')
        self.assertGreater(vis, 0)
        self.assertGreater(course, vis)
        self.assertEqual(html.count('id="vis-seg"'), 1)

    def test_library_panel_matches_workspace_filters(self):
        html = self.client.get(reverse('contest_create')).content.decode()
        _, library = html.split('id="problem-library"', 1)
        self.assertIn('id="lib-filter-panel"', library)
        self.assertIn('ws-filter-panel', library)
        self.assertIn('id="lib-filter-format"', library)
        self.assertIn('id="lib-filter-difficulty"', library)
        self.assertIn('id="lib-filter-course"', library)
        self.assertIn('id="lib-filter-status"', library)
        self.assertIn('id="lib-filter-knowledge"', library)
        self.assertIn('id="lib-search"', library)
        self.assertNotIn('id="lib-knowledge-picker"', library)
        self.assertNotIn('>ID</th>', library)
        self.assertNotIn('data-sort="group"', library)
        self.assertIn('id="cp-from-library"', html)
        self.assertIn('id="id_allow_ai_hints"', html)

    def test_library_author_filter_hidden_without_problem_perm(self):
        html = self.client.get(reverse('contest_create')).content.decode()
        _, library = html.split('id="problem-library"', 1)
        self.assertNotIn('id="lib-filter-author"', library)
        self.assertNotIn('data-author-ids', library)

    def test_library_author_filter_visible_for_problem_editor(self):
        teacher = create_user(username='lib_author_teacher', user_permissions=(
            'add_contest', 'create_private_contest', 'edit_own_problem',
        ))
        create_problem('libauthp', name='Lib Auth P', is_public=True, authors=('lib_author_teacher',))
        self.client.force_login(teacher)
        html = self.client.get(reverse('contest_create')).content.decode()
        _, library = html.split('id="problem-library"', 1)
        self.assertIn('id="lib-filter-author"', library)
        self.assertIn('data-author-ids', library)
        self.assertIn(str(teacher.profile.id), library)

    def test_course_selects_omit_public_sentinel(self):
        public_problem = create_problem(
            'libpubshown', name='Library Public Shown', is_public=True,
        )
        html = self.client.get(reverse('contest_create')).content.decode()
        _, library = html.split('id="problem-library"', 1)
        lib_course = library.split('id="lib-filter-course"', 1)[1].split('</select>', 1)[0]
        self.assertNotIn('value="public"', lib_course)
        self.assertNotRegex(lib_course, r'>\s*Public\s*<')
        # Unselected course still lists public problems in the library.
        self.assertIn(public_problem.code, library)
        self.assertIn(public_problem.name, library)

        org_select = html.split('id="id_organizations"', 1)[1].split('</select>', 1)[0]
        self.assertNotIn('value="public"', org_select)
        self.assertNotIn('— Public —', org_select)
        self.assertNotRegex(org_select, r'>\s*Public\s*<')
        self.assertNotRegex(org_select, r'<option[^>]*value=""')
        self.assertRegex(html, r'id="id_organizations"[^>]*multiple')
        self.assertIn('data-placeholder', org_select)

        class_select = html.split('id="id_classes"', 1)[1].split('</select>', 1)[0]
        self.assertNotRegex(class_select, r'<option[^>]*value=""')
        self.assertNotRegex(class_select, r'<option[^>]*>\s*Select course first')
        self.assertRegex(html, r'id="id_classes"[^>]*multiple')
        self.assertIn('data-placeholder', class_select)

    def test_classes_multi_select_round_trip(self):
        org_a = Organization.objects.create(name='Class Org A', slug='class-org-a', is_open=True)
        org_b = Organization.objects.create(name='Class Org B', slug='class-org-b', is_open=True)
        class_a = Class.objects.create(organization=org_a, name='Class A1', slug='class-a1')
        class_b = Class.objects.create(organization=org_b, name='Class B1', slug='class-b1')
        response = self._post(
            name='Class M2M', key='classm2m',
            vis_mode='org',
            organizations=[org_a.id, org_b.id],
            classes=[class_a.id, class_b.id],
        )
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(key='classm2m')
        self.assertTrue(contest.is_organization_private)
        self.assertCountEqual(
            list(contest.organizations.values_list('id', flat=True)),
            [org_a.id, org_b.id],
        )
        self.assertCountEqual(
            list(contest.classes.values_list('id', flat=True)),
            [class_a.id, class_b.id],
        )
        edit = self.client.get(reverse('contest_edit', args=['classm2m']))
        self.assertEqual(edit.status_code, 200)
        prefill = edit.context['prefill']
        self.assertEqual(prefill['vis_mode'], 'org')
        self.assertCountEqual([o[0] for o in prefill['organizations']], [org_a.id, org_b.id])
        self.assertCountEqual([c[0] for c in prefill['classes']], [class_a.id, class_b.id])
        html = edit.content.decode()
        class_html = html.split('id="id_classes"', 1)[1].split('</select>', 1)[0]
        self.assertIn('selected', class_html)
        self.assertIn(str(class_a.id), class_html)
        self.assertIn(str(class_b.id), class_html)

    def test_classes_require_organizations(self):
        org = Organization.objects.create(name='Orphan Class Org', slug='orphan-class-org', is_open=True)
        cls = Class.objects.create(organization=org, name='Orphan Class', slug='orphan-class')
        response = self._post(
            name='Class No Org', key='classnoorg',
            vis_mode='org',
            classes=[cls.id],
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('organizations', json.loads(response.content)['error'].lower())

    def test_classes_must_belong_to_selected_organizations(self):
        org_a = Organization.objects.create(name='Match Org A', slug='match-org-a', is_open=True)
        org_b = Organization.objects.create(name='Match Org B', slug='match-org-b', is_open=True)
        class_b = Class.objects.create(organization=org_b, name='Match Class B', slug='match-class-b')
        response = self._post(
            name='Class Mismatch', key='classmismatch',
            vis_mode='org',
            organizations=[org_a.id],
            classes=[class_b.id],
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('classes', json.loads(response.content)['error'].lower())

    def test_public_vis_mode_stores_but_does_not_apply_course_scope(self):
        org = Organization.objects.create(name='Public Scope Org', slug='public-scope-org', is_open=True)
        cls = Class.objects.create(organization=org, name='Public Scope Class', slug='public-scope-class')
        response = self._post(
            name='Public Scoped', key='pubscoped',
            vis_mode='public',
            organizations=[org.id],
            classes=[cls.id],
        )
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(key='pubscoped')
        self.assertTrue(contest.is_visible)
        self.assertFalse(contest.is_organization_private)
        self.assertCountEqual(list(contest.organizations.values_list('id', flat=True)), [org.id])
        self.assertCountEqual(list(contest.classes.values_list('id', flat=True)), [cls.id])

    def test_validity_toggle_shares_label_row(self):
        html = self.client.get(reverse('contest_create')).content.decode()
        self.assertIn('validity-head', html)
        head = html.split('validity-head', 1)[1].split('</div>', 1)[0]
        self.assertIn('Validity', head)
        self.assertIn('Range', head)
        self.assertIn('Permanent', head)

    def test_preview_omits_join_assignment(self):
        html = self.client.get(reverse('contest_create')).content.decode()
        self.assertNotIn('Join assignment', html)
        self.assertNotIn('pv-join', html)

    def _post(self, **overrides):
        now = timezone.now()
        data = {
            'name': 'New Contest', 'key': 'newcontest',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=3)).isoformat(),
        }
        data.update(overrides)
        return self.client.post(
            reverse('contest_create'),
            data=json.dumps(data), content_type='application/json',
        )

    def test_bool_flags_saved(self):
        response = self._post(
            name='Flags', key='flagtest',
            run_pretests_only=True, tester_see_submissions=True, tester_see_scoreboard=True,
            is_private=True, is_organization_private=True, limit_join_organizations=True,
            allow_ai_hints=True,
        )
        self.assertEqual(response.status_code, 200)
        contest = Contest.objects.get(key='flagtest')
        self.assertTrue(contest.run_pretests_only)
        self.assertTrue(contest.tester_see_submissions)
        self.assertTrue(contest.tester_see_scoreboard)
        self.assertTrue(contest.is_private)
        self.assertTrue(contest.is_organization_private)
        self.assertTrue(contest.limit_join_organizations)
        self.assertTrue(contest.allow_ai_hints)
        self.assertFalse(contest.hide_sandbox_output)

    def test_hide_sandbox_output_saved(self):
        response = self._post(name='Hide Out', key='hideout', hide_sandbox_output=True)
        self.assertEqual(response.status_code, 200)
        contest = Contest.objects.get(key='hideout')
        self.assertTrue(contest.hide_sandbox_output)

    def test_seb_fields_saved(self):
        # Keys must be valid 64-char hex hashes: model validation (full_clean)
        # is now enforced on save, rejecting anything shorter.
        bek = 'a' * 64
        cek = 'b' * 64
        response = self._post(
            name='SEB', key='sebtest',
            seb_required=True, seb_browser_exam_keys=bek + '\n' + cek, seb_config_keys=cek,
            seb_quit_url='https://example.com/quit',
        )
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(key='sebtest')
        self.assertTrue(contest.seb_required)
        self.assertEqual(contest.seb_browser_exam_keys, bek + '\n' + cek)
        self.assertEqual(contest.seb_config_keys, cek)
        self.assertEqual(contest.seb_quit_url, 'https://example.com/quit')

    def test_seb_required_without_keys_returns_400(self):
        # Regression: seb_required=True used to save with no keys (bricking the
        # contest for every student); model validation now rejects it.
        response = self._post(name='SEB Bad', key='sebbad', seb_required=True)
        self.assertEqual(response.status_code, 400)

    def test_seb_use_default_arms_contest_in_one_save(self):
        # The whole point of the preset: no keys typed, no .seb uploaded, and
        # the contest still passes Contest.clean() on the very first save.
        preset = _create_preset()
        response = self._post(name='SEB Default', key='sebdefault',
                              seb_required=True, seb_use_default=True)
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(key='sebdefault')
        self.assertEqual(contest.seb_preset, preset)
        self.assertEqual(contest.seb_browser_exam_keys, '')
        self.assertEqual(contest.seb_allowed_bek(), [PRESET_BEK])

    def test_seb_use_default_without_a_default_returns_400(self):
        response = self._post(name='SEB No Default', key='sebnodefault',
                              seb_required=True, seb_use_default=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn('No default Safe Exam Browser configuration',
                      json.loads(response.content)['error'])

    def test_seb_use_default_ignores_inactive_preset(self):
        _create_preset(is_active=False)
        response = self._post(name='SEB Inactive', key='sebinactive',
                              seb_required=True, seb_use_default=True)
        self.assertEqual(response.status_code, 400)

    def _edit_seb(self, contest, **overrides):
        data = {
            'name': contest.name, 'key': contest.key,
            'start_time': contest.start_time.isoformat(),
            'end_time': contest.end_time.isoformat(),
            'seb_required': True,
        }
        data.update(overrides)
        return self.client.post(
            reverse('contest_edit', args=[contest.key]),
            data=json.dumps(data), content_type='application/json',
        )

    def test_adopting_the_default_clears_the_contests_own_config(self):
        """Ticking "use the default" must actually hand control to the preset.

        Every SEB accessor prefers the contest's own value, and the editor hides
        those fields once the box is ticked — so leaving them kept serving the
        old upload with no visible reason why.
        """
        _create_preset()
        self._post(name='SEB Own', key='sebown', seb_required=True,
                   seb_browser_exam_keys='a' * 64, seb_config_keys='b' * 64,
                   seb_quit_url='https://example.com/quit')
        contest = Contest.objects.get(key='sebown')
        contest.seb_config_file.save('own.seb', ContentFile(b'OLD UPLOAD'), save=True)
        self.assertEqual(contest.seb_effective_config_file.read(), b'OLD UPLOAD')

        response = self._edit_seb(contest, seb_use_default=True)
        self.assertEqual(response.status_code, 200, response.content)
        contest.refresh_from_db()

        self.assertFalse(contest.seb_config_file)
        self.assertEqual(contest.seb_browser_exam_keys, '')
        self.assertEqual(contest.seb_config_keys, '')
        self.assertEqual(contest.seb_quit_url, '')
        # The preset now supplies the file AND the hashes that authenticate it.
        self.assertEqual(contest.seb_effective_config_file.read(), b'SEB config')
        self.assertEqual(contest.seb_allowed_bek(), contest.seb_preset.allowed_bek())
        self.assertEqual(contest.seb_allowed_config_keys(), contest.seb_preset.allowed_config_keys())

    def test_adopting_the_default_refuses_during_a_live_exam(self):
        # Clearing the overrides re-keys the contest; doing that mid-exam would
        # 403 every student already inside SEB.
        _create_preset()
        self._post(name='SEB Live', key='seblive', seb_required=True,
                   seb_browser_exam_keys='a' * 64, seb_config_keys='b' * 64)
        contest = Contest.objects.get(key='seblive')
        now = timezone.now()
        Contest.objects.filter(pk=contest.pk).update(
            start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=1))
        contest.refresh_from_db()

        response = self._edit_seb(contest, seb_use_default=True)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('in progress', response.content.decode())
        contest.refresh_from_db()
        self.assertEqual(contest.seb_browser_exam_keys, 'a' * 64)

    def test_resave_keeps_existing_preset_when_default_changes(self):
        # Publishing a newer default must not silently re-point an existing
        # assignment: that changes its Config Key and 403s every student
        # already inside SEB.
        original = _create_preset()
        self._post(name='SEB Keep', key='sebkeep', seb_required=True, seb_use_default=True)
        contest = Contest.objects.get(key='sebkeep')
        self.assertEqual(contest.seb_preset, original)

        newer = SEBConfigPreset(name='Newer default', browser_exam_keys='1' * 64,
                                config_keys='2' * 64, is_default=True)
        newer.config_file.save('newer.seb', ContentFile(b'SEB config'), save=False)
        newer.save()
        self.assertEqual(SEBConfigPreset.get_default(), newer)

        response = self._edit_seb(contest, seb_use_default=True)
        self.assertEqual(response.status_code, 200, response.content)
        contest.refresh_from_db()
        self.assertEqual(contest.seb_preset, original)

    def test_resave_keeps_preset_after_default_retired(self):
        # A disabled checkbox is not submitted, so an unrelated save used to
        # arrive as seb_use_default=False and silently drop the link. The
        # control now stays enabled whenever a link exists.
        preset = _create_preset()
        self._post(name='SEB Retired', key='sebretired', seb_required=True, seb_use_default=True)
        contest = Contest.objects.get(key='sebretired')

        SEBConfigPreset.objects.filter(pk=preset.pk).update(is_active=False)
        cache.delete(DEFAULT_PRESET_CACHE_KEY)
        self.assertIsNone(SEBConfigPreset.get_default())

        response = self._edit_seb(contest, seb_use_default=True)
        self.assertEqual(response.status_code, 200, response.content)
        contest.refresh_from_db()
        self.assertEqual(contest.seb_preset, preset)

    def test_seb_use_default_unset_clears_preset(self):
        preset = _create_preset()
        self._post(name='SEB Clear', key='sebclear', seb_required=True, seb_use_default=True)
        contest = Contest.objects.get(key='sebclear')
        self.assertEqual(contest.seb_preset, preset)
        response = self.client.post(
            reverse('contest_edit', args=['sebclear']),
            data=json.dumps({
                'name': 'SEB Clear', 'key': 'sebclear',
                'start_time': contest.start_time.isoformat(),
                'end_time': contest.end_time.isoformat(),
                'seb_required': True, 'seb_use_default': False,
                'seb_browser_exam_keys': 'c' * 64, 'seb_config_keys': 'd' * 64,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        contest.refresh_from_db()
        self.assertIsNone(contest.seb_preset)

    def test_format_config_json_saved(self):
        # ICPC accepts a config dict with a validated 'penalty' key — use it to
        # exercise the JSON round-trip (the default format rejects any config).
        response = self._post(name='FC', key='fctest', format_name='icpc',
                              format_config='{"penalty": 20}')
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(key='fctest')
        self.assertEqual(contest.format_name, 'icpc')
        self.assertEqual(contest.format_config, {'penalty': 20})

    def test_create_exam_with_icpc_config_clears_config(self):
        """Create-with-exam must not 400 on leftover ICPC-shaped JSON."""
        response = self._post(name='Exam leftover', key='examleftover',
                              format_name='exam', format_config='{"penalty": 20}')
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(key='examleftover')
        self.assertEqual(contest.format_name, 'exam')
        self.assertIsNone(contest.format_config)

    def test_invalid_icpc_config_still_returns_400(self):
        """Auto-clear is exam/default only — ICPC still validates its keys."""
        response = self._post(name='ICPC Bad', key='icpcbad', format_name='icpc',
                              format_config='{"unknown": 1}')
        self.assertEqual(response.status_code, 400)

    def test_format_switch_js_clears_config_and_sends_empty(self):
        """Switching to exam/default clears the textarea; blank config is sent."""
        html = self.client.get(reverse('contest_create')).content.decode()
        self.assertIn('formatNeedsNoConfig', html)
        self.assertIn('syncFormatConfigForFormat', html)
        self.assertIn("data.format_config = ''", html)
        self.assertIn('recalculate scores for existing submissions', html)
        self.assertNotIn('will rescore existing submissions', html)

    def test_format_config_invalid_json_returns_400(self):
        response = self._post(name='FC Bad', key='fcbad', format_config='{not json')
        self.assertEqual(response.status_code, 400)

    def test_invalid_format_name_returns_400(self):
        # Regression: an unregistered format name used to store fine and then
        # KeyError-crash every page rendering the contest.
        response = self._post(name='Fmt Bad', key='fmtbad', format_name='not-a-format')
        self.assertEqual(response.status_code, 400)
        self.assertIn('format', json.loads(response.content)['error'])

    def test_profile_m2m_fields_saved(self):
        response = self._post(
            name='M2M', key='m2mtest',
            curators=[self.user_a.id], testers=[self.user_b.id], spectators=[self.user_a.id],
            rate_exclude=[self.user_b.id], view_contest_scoreboard=[self.user_a.id],
            view_contest_submissions=[self.user_b.id], banned_users=[self.user_a.id],
        )
        self.assertEqual(response.status_code, 200)
        contest = Contest.objects.get(key='m2mtest')
        self.assertEqual(set(contest.curators.values_list('user_id', flat=True)), {self.user_a.id})
        self.assertEqual(set(contest.testers.values_list('user_id', flat=True)), {self.user_b.id})
        self.assertEqual(set(contest.spectators.values_list('user_id', flat=True)), {self.user_a.id})
        self.assertEqual(set(contest.rate_exclude.values_list('user_id', flat=True)), {self.user_b.id})
        self.assertEqual(set(contest.view_contest_scoreboard.values_list('user_id', flat=True)), {self.user_a.id})
        self.assertEqual(set(contest.view_contest_submissions.values_list('user_id', flat=True)), {self.user_b.id})
        self.assertEqual(set(contest.banned_users.values_list('user_id', flat=True)), {self.user_a.id})

    def test_invalid_date_string_returns_400(self):
        # Regression: a malformed date used to raise TypeError (500) because
        # gettext() rejects extra arguments.
        response = self._post(name='Bad', key='baddate', start_time='not-a-date')
        self.assertEqual(response.status_code, 400)
        self.assertIn('date format', json.loads(response.content)['error'])

    def test_naive_start_time_saved_as_utc(self):
        # A naive local timestamp (no Z) must be stored as UTC, not crash.
        response = self._post(
            name='Naive', key='naivetime',
            start_time='2026-08-01T10:00:00', end_time='2026-08-01T12:00:00',
        )
        self.assertEqual(response.status_code, 200)
        contest = Contest.objects.get(key='naivetime')
        self.assertEqual(contest.start_time, datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))

    def test_invalid_m2m_id_returns_400(self):
        response = self._post(name='Bad M2M', key='badm2m', curators=['not-an-int'])
        self.assertEqual(response.status_code, 400)

    def test_create_with_problems_saves_points(self):
        # The problem must be one the editor may attach (public, or owned by
        # them) — the visibility rule rejects other teachers' hidden problems.
        problem = create_problem('problems-save-test', is_public=True)
        response = self._post(name='With Prob', key='withprob', problems=[{'id': problem.id, 'points': 150}])
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(key='withprob')
        cp = contest.contest_problems.get()
        self.assertEqual(cp.problem, problem)
        self.assertEqual(cp.points, 150)
        self.assertEqual(cp.order, 1)

    def test_create_with_problem_ids_backward_compatible(self):
        # The legacy plain-id list keeps the library problem's own score.
        problem = create_problem('problems-legacy-test', is_public=True, points=25)
        response = self._post(name='Legacy Prob', key='legacyprob', problem_ids=[problem.id])
        self.assertEqual(response.status_code, 200)
        contest = Contest.objects.get(key='legacyprob')
        cp = contest.contest_problems.get()
        self.assertEqual(cp.points, 25)

    def test_create_omitted_points_uses_problem_points(self):
        problem = create_problem('omit-pts', is_public=True, points=7)
        response = self._post(name='Omit Pts', key='omitpts', problems=[{'id': problem.id}])
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(key='omitpts')
        self.assertEqual(contest.contest_problems.get().points, 7)

    def test_library_rows_carry_problem_points(self):
        problem = create_problem('lib-pts-25', is_public=True, points=25)
        html = self.client.get(reverse('contest_create')).content.decode()
        marker = 'data-pid="%s"' % problem.id
        start = html.find(marker)
        self.assertNotEqual(start, -1, marker)
        row = html[start:html.find('</tr>', start)]
        self.assertIn('data-points="25"', row)

    # NOTE: attaching a *hidden* problem is only possible for trusted editors
    # (edit_all_contest / see_private_problem). Curators are blocked from it —
    # see ContestEditViewTestCase.test_curator_cannot_attach_hidden_problem.

    def test_create_saves_per_problem_options(self):
        problem = create_problem('problem-options', is_public=True)
        response = self._post(
            name='Opts', key='optstest',
            problems=[{'id': problem.id, 'points': 200, 'partial': False,
                       'is_pretested': True, 'max_submissions': 3}],
        )
        self.assertEqual(response.status_code, 200, response.content)
        contest = Contest.objects.get(key='optstest')
        cp = contest.contest_problems.get()
        self.assertEqual(cp.points, 200)
        self.assertFalse(cp.partial)
        self.assertTrue(cp.is_pretested)
        self.assertEqual(cp.max_submissions, 3)

    def test_create_saves_display_name(self):
        problem = create_problem('display-name-create', is_public=True, name='Library Title')
        response = self._post(
            name='Named', key='dispname',
            problems=[{'id': problem.id, 'points': 100, 'display_name': 'Quiz Q1'}],
        )
        self.assertEqual(response.status_code, 200, response.content)
        cp = Contest.objects.get(key='dispname').contest_problems.get()
        self.assertEqual(cp.display_name, 'Quiz Q1')
        self.assertEqual(cp.problem.name, 'Library Title')

    def test_create_then_detail_shows_display_name(self):
        problem = create_problem('display-name-detail', is_public=True, name='Library Original')
        response = self._post(
            name='Named Detail', key='dispdetail',
            problems=[{'id': problem.id, 'points': 100, 'display_name': 'Quiz Title'}],
        )
        self.assertEqual(response.status_code, 200, response.content)
        detail = self.client.get(reverse('contest_view', args=['dispdetail']))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.context['contest_problems'][0].effective_name, 'Quiz Title')
        self.assertContains(detail, 'Quiz Title')

    def test_create_rejects_display_name_too_long(self):
        problem = create_problem('display-name-long', is_public=True)
        response = self._post(
            name='Too Long', key='displong',
            problems=[{'id': problem.id, 'display_name': 'x' * 101}],
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('display name', json.loads(response.content)['error'].lower())

    def test_create_empty_display_name_is_blank(self):
        problem = create_problem('display-name-empty', is_public=True, name='Original')
        response = self._post(
            name='Empty Name', key='dispempty',
            problems=[{'id': problem.id, 'display_name': '   '}],
        )
        self.assertEqual(response.status_code, 200, response.content)
        cp = Contest.objects.get(key='dispempty').contest_problems.get()
        self.assertEqual(cp.display_name, '')
        self.assertEqual(cp.problem.name, 'Original')

    def test_create_library_title_as_display_name_is_not_an_override(self):
        """Saving the library name as display_name must not freeze the title."""
        problem = create_problem('display-name-same', is_public=True, name='Library Title')
        response = self._post(
            name='Same As Library', key='dispsame',
            problems=[{'id': problem.id, 'points': 100, 'display_name': 'Library Title'}],
        )
        self.assertEqual(response.status_code, 200, response.content)
        cp = Contest.objects.get(key='dispsame').contest_problems.get()
        self.assertEqual(cp.display_name, '')


class ContestEditProblemsTestCase(CommonDataMixin, TestCase):
    """Edit mode: problems are pre-filled and synced on save."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.editor = create_user(username='prob_editor', user_permissions=(
            'add_contest', 'edit_own_contest', 'edit_all_contest',
        ))
        cls.problem = create_problem('prob-edit-test')
        cls.contest = create_contest(
            key='probeditcontest', name='Prob Edit',
            start_time=timezone.now() + timedelta(days=1),
            end_time=timezone.now() + timedelta(days=2),
            authors=('superuser',),
        )
        from judge.models import ContestProblem
        ContestProblem.objects.create(contest=cls.contest, problem=cls.problem, points=100, order=1)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.editor)

    def _full_payload(self, **overrides):
        now = timezone.now()
        payload = {
            'name': 'Prob Edit', 'key': 'probeditcontest',
            'start_time': (now + timedelta(days=1)).isoformat(),
            'end_time': (now + timedelta(days=2)).isoformat(),
            'duration_hours': 0, 'duration_minutes': 0,
            'is_visible': False, 'is_rated': False, 'use_clarifications': True,
            'hide_problem_tags': False, 'hide_problem_authors': False,
            'allow_virtual_participation': True, 'run_pretests_only': False,
            'rate_all': False, 'tester_see_submissions': False, 'tester_see_scoreboard': False,
            'show_short_display': False, 'is_private': False, 'is_organization_private': False,
            'limit_join_organizations': False, 'seb_required': False,
            'format_name': 'default', 'scoreboard_visibility': 'V', 'points_precision': 3,
            'authors': [], 'curators': [], 'testers': [], 'spectators': [],
            'organizations': [], 'classes': [], 'join_organizations': [], 'tags': [],
            'private_contestants': [], 'rate_exclude': [], 'view_contest_scoreboard': [],
            'view_contest_submissions': [], 'banned_users': [],
        }
        payload.update(overrides)
        return payload

    def test_get_prefills_problems_in_context(self):
        response = self.client.get(reverse('contest_edit', args=['probeditcontest']))
        self.assertEqual(response.status_code, 200)
        problems = response.context['prefill']['problems']
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]['id'], self.problem.id)
        self.assertEqual(problems[0]['points'], 100)
        self.assertEqual(problems[0]['letter'], '1')
        self.assertEqual(problems[0]['display_name'], '')
        self.assertEqual(problems[0]['name'], self.problem.name)
        html = response.content.decode()
        marker = 'data-pid="%s"' % self.problem.id
        start = html.find(marker)
        self.assertNotEqual(start, -1)
        open_div = html.rfind('<div class="cp-row"', 0, start)
        nxt = html.find('<div class="cp-row"', start + 1)
        row = html[open_div:nxt]
        self.assertIn('placeholder="%s"' % self.problem.name, row)
        self.assertRegex(row, r'class="pname cp-display-name"[^>]*value=""')
        self.assertNotIn('value="%s"' % self.problem.name, row)

    def test_get_prefills_display_name_override(self):
        from judge.models import ContestProblem
        ContestProblem.objects.filter(contest=self.contest, problem=self.problem).update(
            display_name='Assignment Title',
        )
        response = self.client.get(reverse('contest_edit', args=['probeditcontest']))
        self.assertEqual(response.status_code, 200)
        problems = response.context['prefill']['problems']
        self.assertEqual(problems[0]['display_name'], 'Assignment Title')
        self.assertEqual(problems[0]['name'], self.problem.name)
        html = response.content.decode()
        self.assertIn('cp-display-name', html)
        self.assertIn('Assignment Title', html)

    def test_post_saves_and_clears_display_name(self):
        response = self.client.post(
            reverse('contest_edit', args=['probeditcontest']),
            data=json.dumps(self._full_payload(problems=[{
                'id': self.problem.id, 'points': 100, 'display_name': 'Renamed in assignment',
            }])),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        cp = self.contest.contest_problems.get()
        self.assertEqual(cp.display_name, 'Renamed in assignment')

        response = self.client.post(
            reverse('contest_edit', args=['probeditcontest']),
            data=json.dumps(self._full_payload(problems=[{
                'id': self.problem.id, 'points': 100, 'display_name': '',
            }])),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        cp = self.contest.contest_problems.get()
        self.assertEqual(cp.display_name, '')

    def test_post_updates_problem_points(self):
        response = self.client.post(
            reverse('contest_edit', args=['probeditcontest']),
            data=json.dumps(self._full_payload(problems=[{'id': self.problem.id, 'points': 250}])),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        cp = self.contest.contest_problems.get()
        self.assertEqual(cp.points, 250)
        self.assertEqual(cp.order, 1)

    def test_post_removes_problems_when_list_empty(self):
        response = self.client.post(
            reverse('contest_edit', args=['probeditcontest']),
            data=json.dumps(self._full_payload(problems=[])),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.contest_problems.count(), 0)

    def test_get_renders_max_submissions_input_in_row(self):
        """The Max submissions input lives in the problem row's Max column,
        not as a duplicated label in the per-problem options strip below."""
        response = self.client.get(reverse('contest_edit', args=['probeditcontest']))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('class="cp-max-subs"', html)
        # The old duplicated "Max submissions" label was removed from the
        # options strip; only the header tooltip text (different wording)
        # may mention max submissions.
        self.assertNotIn('Max submissions', html)
        # The options strip still renders the other per-problem settings.
        for marker in ('cp-partial', 'cp-pretested', 'cp-results', 'cp-out-prefix'):
            self.assertIn(marker, html)

    def test_post_roundtrips_max_submissions_from_row_input(self):
        """A max_submissions value sent by the in-row input round-trips to the
        stored ContestProblem (blank → unlimited)."""
        response = self.client.post(
            reverse('contest_edit', args=['probeditcontest']),
            data=json.dumps(self._full_payload(problems=[
                {'id': self.problem.id, 'points': 100, 'max_submissions': 5},
            ])),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.contest_problems.get().max_submissions, 5)
        # Blank means unlimited.
        response = self.client.post(
            reverse('contest_edit', args=['probeditcontest']),
            data=json.dumps(self._full_payload(problems=[
                {'id': self.problem.id, 'points': 100, 'max_submissions': ''},
            ])),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.contest.refresh_from_db()
        self.assertIsNone(self.contest.contest_problems.get().max_submissions)


class ContestEditorNewEndpointsTestCase(CommonDataMixin, TestCase):
    """New editor endpoints: tag create, .seb upload, finalize/unfinalize."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.editor = create_user(username='endpoints_editor', user_permissions=(
            'add_contest', 'edit_own_contest', 'edit_all_contest', 'contest_rating',
            'lock_contest', 'contest_access_code',
            'contest_seb_requirement', 'create_private_contest', 'change_contest_visibility',
            'contest_problem_label', 'override_performance_ceiling',
        ))
        cls.curator = create_user(username='endpoints_curator', user_permissions=('edit_own_contest',))
        cls.normal = create_user(username='endpoints_normal')
        _now = timezone.now()
        cls.contest = create_contest(
            key='endpointcontest', name='Endpoint',
            start_time=_now + timedelta(days=1), end_time=_now + timedelta(days=2),
            authors=('superuser',),
            curators=('endpoints_curator',),
        )

    def setUp(self):
        self.client = Client()

    # ── tag create ──────────────────────────────────────────────────────────

    def test_tag_create_success(self):
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse('contest_tag_create'),
            data=json.dumps({'name': 'midterm'}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = json.loads(response.content)
        self.assertTrue(data['ok'])
        self.assertEqual(data['tag']['name'], 'midterm')
        self.assertTrue(data['tag']['id'])
        self.assertTrue(data['tag']['color'].startswith('#'))
        self.assertTrue(ContestTag.objects.filter(name='midterm').exists())

    def test_tag_create_rejects_bad_name(self):
        self.client.force_login(self.editor)
        for bad in ('Midterm', 'mid term', 'midterm!', '中term'):
            response = self.client.post(
                reverse('contest_tag_create'),
                data=json.dumps({'name': bad}), content_type='application/json',
            )
            self.assertEqual(response.status_code, 400, bad)

    def test_tag_create_rejects_too_long(self):
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse('contest_tag_create'),
            data=json.dumps({'name': 'a' * 21}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_tag_create_returns_existing_on_duplicate(self):
        existing = ContestTag.objects.create(name='dup-tag', color='#ff0000')
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse('contest_tag_create'),
            data=json.dumps({'name': 'dup-tag'}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = json.loads(response.content)
        self.assertTrue(data['ok'])
        self.assertEqual(data['tag']['id'], existing.id)
        self.assertEqual(ContestTag.objects.filter(name='dup-tag').count(), 1)

    def test_tag_create_allowed_for_contest_creator(self):
        # create_private_contest holders may still create tags (preset pills).
        creator = create_user(username='tag_contest_creator',
                              user_permissions=('create_private_contest',))
        self.client.force_login(creator)
        response = self.client.post(
            reverse('contest_tag_create'),
            data=json.dumps({'name': 'homework'}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_tag_create_denied_for_normal_user(self):
        self.client.force_login(self.normal)
        response = self.client.post(
            reverse('contest_tag_create'),
            data=json.dumps({'name': 'sneaky'}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_tag_create_allowed_for_curator(self):
        # edit_own_contest holders may open the editor, so they may add tags.
        self.client.force_login(self.curator)
        response = self.client.post(
            reverse('contest_tag_create'),
            data=json.dumps({'name': 'curator-tag'}), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)

    # ── .seb upload ─────────────────────────────────────────────────────────

    def test_seb_upload_success(self):
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse('contest_seb_upload', args=['endpointcontest']),
            data={'seb_config_file': SimpleUploadedFile('exam.seb', b'SEB config')},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(json.loads(response.content)['ok'])
        self.contest.refresh_from_db()
        self.assertTrue(self.contest.seb_config_file)
        # The stored name is a generated object key, never the uploaded filename:
        # seb_configs/YYYY/MM/contest-<id>/<uuid>.seb
        name = self.contest.seb_config_file.name
        self.assertRegex(name, r'^seb_configs/\d{4}/\d{2}/contest-%d/[0-9a-f]{32}\.seb$' % self.contest.id)
        self.assertNotIn('exam', name)
        # Every object written through ResilientStorage is tracked for mirroring.
        self.assertTrue(StoredFileRecord.objects.filter(path=name).exists())

    def test_seb_download_returns_stored_file(self):
        self.client.force_login(self.editor)
        self.client.post(
            reverse('contest_seb_upload', args=['endpointcontest']),
            data={'seb_config_file': SimpleUploadedFile('exam.seb', b'SEB config bytes')},
        )
        response = self.client.get(reverse('contest_seb_upload', args=['endpointcontest']))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'SEB config bytes')
        self.assertIn('endpointcontest.seb', response['Content-Disposition'])

    def test_seb_download_denied_without_permission(self):
        self.client.force_login(self.curator)
        response = self.client.get(reverse('contest_seb_upload', args=['endpointcontest']))
        self.assertEqual(response.status_code, 403)

    def test_seb_upload_replaces_previous_object(self):
        self.client.force_login(self.editor)
        self.client.post(
            reverse('contest_seb_upload', args=['endpointcontest']),
            data={'seb_config_file': SimpleUploadedFile('first.seb', b'first')},
        )
        self.contest.refresh_from_db()
        first = self.contest.seb_config_file.name
        self.client.post(
            reverse('contest_seb_upload', args=['endpointcontest']),
            data={'seb_config_file': SimpleUploadedFile('second.seb', b'second')},
        )
        self.contest.refresh_from_db()
        self.assertNotEqual(self.contest.seb_config_file.name, first)
        # The replaced object is removed rather than orphaned in the bucket.
        self.assertEqual(
            StoredFileRecord.objects.get(path=first).status, StoredFileRecord.STATUS_DELETED)

    def test_seb_upload_keeps_object_shared_with_a_clone(self):
        # ContestClone copies the field value, so two contests can name the same
        # object. Replacing one must not delete the other's config.
        self.client.force_login(self.editor)
        self.client.post(
            reverse('contest_seb_upload', args=['endpointcontest']),
            data={'seb_config_file': SimpleUploadedFile('shared.seb', b'shared')},
        )
        self.contest.refresh_from_db()
        shared = self.contest.seb_config_file.name
        _now = timezone.now()
        clone = create_contest(
            key='clonedseb', name='Cloned',
            start_time=_now + timedelta(days=1), end_time=_now + timedelta(days=2),
            seb_config_file=shared,
        )

        self.client.post(
            reverse('contest_seb_upload', args=['endpointcontest']),
            data={'seb_config_file': SimpleUploadedFile('new.seb', b'new')},
        )
        clone.refresh_from_db()
        self.assertEqual(clone.seb_config_file.name, shared)
        self.assertNotEqual(
            StoredFileRecord.objects.get(path=shared).status, StoredFileRecord.STATUS_DELETED)
        self.assertEqual(clone.seb_config_file.open('rb').read(), b'shared')

    def test_seb_upload_rejects_bad_extension(self):
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse('contest_seb_upload', args=['endpointcontest']),
            data={'seb_config_file': SimpleUploadedFile('exam.txt', b'x')},
        )
        self.assertEqual(response.status_code, 400)

    def test_seb_upload_rejects_missing_file(self):
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse('contest_seb_upload', args=['endpointcontest']),
            data={},
        )
        self.assertEqual(response.status_code, 400)

    def test_seb_upload_denied_for_curator_of_other_contest(self):
        # The curator curates endpointcontest — deny when the contest is not theirs.
        self.client.force_login(self.curator)
        _now = timezone.now()
        create_contest(
            key='othersebcontest', name='Other SEB',
            start_time=_now + timedelta(days=1), end_time=_now + timedelta(days=2),
            authors=('superuser',),
        )
        response = self.client.post(
            reverse('contest_seb_upload', args=['othersebcontest']),
            data={'seb_config_file': SimpleUploadedFile('exam.seb', b'x')},
        )
        self.assertEqual(response.status_code, 403)

    # ── finalize / unfinalize ───────────────────────────────────────────────

    def test_finalize_denied_for_curator(self):
        self.client.force_login(self.curator)
        response = self.client.post(reverse('contest_finalize', args=['endpointcontest']))
        self.assertEqual(response.status_code, 403)

    def test_finalize_denied_for_normal_user(self):
        self.client.force_login(self.normal)
        response = self.client.post(reverse('contest_finalize', args=['endpointcontest']))
        self.assertEqual(response.status_code, 403)

    def test_finalize_allowed_for_editor(self):
        self.client.force_login(self.editor)
        response = self.client.post(reverse('contest_finalize', args=['endpointcontest']))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(json.loads(response.content)['ok'])

    def test_unfinalize_allowed_for_editor(self):
        self.client.force_login(self.editor)
        response = self.client.post(reverse('contest_unfinalize', args=['endpointcontest']))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(json.loads(response.content)['ok'])


class ContestEditorActionFieldTestCase(CommonDataMixin, TestCase):
    """Save actions: save → contest page, continue → editor, another → no redirect."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.editor = create_user(username='action_editor', user_permissions=(
            'add_contest', 'edit_own_contest', 'edit_all_contest',
        ))

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.editor)

    def _create_payload(self, key='actiontest'):
        now = timezone.now()
        return {
            'name': 'Action Test', 'key': key,
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=3)).isoformat(),
        }

    def test_action_save_redirects_to_contest_view(self):
        response = self.client.post(
            reverse('contest_create'),
            data=json.dumps(self._create_payload()), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['action'], 'save')
        self.assertEqual(data['redirect'], reverse('contest_view', args=['actiontest']))

    def test_action_continue_redirects_to_editor(self):
        response = self.client.post(
            reverse('contest_create'),
            data=json.dumps({**self._create_payload(), 'action': 'continue'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['action'], 'continue')
        self.assertEqual(data['redirect'], reverse('contest_edit', args=['actiontest']))

    def test_action_another_has_no_redirect(self):
        response = self.client.post(
            reverse('contest_create'),
            data=json.dumps({**self._create_payload(), 'action': 'another'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['action'], 'another')
        self.assertNotIn('redirect', data)
        # The contest still exists.
        self.assertTrue(Contest.objects.filter(key='actiontest').exists())


class ContestEditorProblemOptionsTestCase(CommonDataMixin, TestCase):
    """Per-problem output_prefix / hide_results round-trip."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.editor = create_user(username='probopts_editor', user_permissions=(
            'add_contest', 'edit_own_contest', 'edit_all_contest',
        ))
        cls.problem = create_problem('probopts-test', is_public=True)
        cls.contest = create_contest(
            key='proboptscontest', name='Prob Opts',
            start_time=timezone.now() + timedelta(days=1),
            end_time=timezone.now() + timedelta(days=2),
            authors=('superuser',),
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.editor)

    def _payload(self, **overrides):
        now = timezone.now()
        payload = {
            'name': 'Prob Opts', 'key': 'proboptscontest',
            'start_time': (now + timedelta(days=1)).isoformat(),
            'end_time': (now + timedelta(days=2)).isoformat(),
            'is_visible': False,
        }
        payload.update(overrides)
        return payload

    def test_create_saves_output_prefix_and_hide_results(self):
        response = self.client.post(
            reverse('contest_edit', args=['proboptscontest']),
            data=json.dumps(self._payload(problems=[
                {'id': self.problem.id, 'points': 100, 'output_prefix': 5, 'hide_results': True},
            ])), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        cp = self.contest.contest_problems.get()
        self.assertEqual(cp.output_prefix_override, 5)
        self.assertTrue(cp.hide_results)

    def test_hide_results_three_state(self):
        for raw, expected in ((None, None), ('default', None), ('hide', True), ('show', False), (False, False)):
            # Each POST fully replaces the problem list, so re-fetch after save.
            response = self.client.post(
                reverse('contest_edit', args=['proboptscontest']),
                data=json.dumps(self._payload(problems=[
                    {'id': self.problem.id, 'points': 100, 'hide_results': raw},
                ])), content_type='application/json',
            )
            self.assertEqual(response.status_code, 200, response.content)
            cp = self.contest.contest_problems.get()
            self.assertEqual(cp.hide_results, expected, raw)

    def test_prefill_includes_output_prefix_and_hide_results(self):
        from judge.models import ContestProblem
        ContestProblem.objects.create(
            contest=self.contest, problem=self.problem, points=100, order=1,
            output_prefix_override=7, hide_results=False,
        )
        response = self.client.get(reverse('contest_edit', args=['proboptscontest']))
        self.assertEqual(response.status_code, 200)
        problems = response.context['prefill']['problems']
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]['output_prefix'], 7)
        self.assertEqual(problems[0]['hide_results'], False)

    def test_prefill_output_prefix_defaults_to_10000_when_empty(self):
        from judge.models import ContestProblem
        ContestProblem.objects.create(
            contest=self.contest, problem=self.problem, points=100, order=1,
            output_prefix_override=None,
        )
        response = self.client.get(reverse('contest_edit', args=['proboptscontest']))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['prefill']['problems'][0]['output_prefix'], 10000)
        html = response.content.decode()
        self.assertIn('value="10000"', html)

    def test_prefill_output_prefix_zero_is_kept(self):
        from judge.models import ContestProblem
        ContestProblem.objects.create(
            contest=self.contest, problem=self.problem, points=100, order=1,
            output_prefix_override=0,
        )
        response = self.client.get(reverse('contest_edit', args=['proboptscontest']))
        self.assertEqual(response.context['prefill']['problems'][0]['output_prefix'], 0)

    def test_results_markup_is_exam_only(self):
        """Results lives in ``.cp-results`` (exam-only). Create defaults to exam,
        so ``.cp-list`` is marked ``is-exam``. ``syncExamResultsVisibility``
        toggles that class when ``#id_format_name`` changes."""
        html = self.client.get(reverse('contest_create')).content.decode()
        self.assertIn('class="cp-opt cp-results"', html)
        self.assertIn('data-exam-only="hide_results"', html)
        self.assertIn('select class="cp-results"', html)
        self.assertIn('.cp-list.is-exam .cp-options .cp-opt.cp-results', html)
        self.assertIn("fmt.value === 'exam'", html)
        self.assertIn('syncExamResultsVisibility', html)
        # Create defaults to Exam format — list is exam-active.
        self.assertIn('id="cp-list"', html)
        self.assertIn('class="cp-list is-exam"', html)

    def test_exam_edit_marks_cp_list_is_exam(self):
        from judge.models import ContestProblem
        exam = create_contest(
            key='examopts', name='Exam Opts',
            start_time=timezone.now() + timedelta(days=1),
            end_time=timezone.now() + timedelta(days=2),
            authors=('superuser',),
            format_name='exam',
            problems=(),
        )
        ContestProblem.objects.create(
            contest=exam, problem=self.problem, points=100, order=1,
            hide_results=True,
        )
        response = self.client.get(reverse('contest_edit', args=['examopts']))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('class="cp-list is-exam"', html)
        self.assertIn('data-exam-only="hide_results"', html)
        self.assertIn('class="cp-opt cp-results"', html)

    def test_hide_results_round_trip_on_exam_format(self):
        """Exam format still persists hide_results (the UI only *shows* Results then)."""
        response = self.client.post(
            reverse('contest_edit', args=['proboptscontest']),
            data=json.dumps(self._payload(
                format_name='exam',
                problems=[{'id': self.problem.id, 'points': 100, 'hide_results': 'hide'}],
            )), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.format_name, 'exam')
        self.assertTrue(self.contest.contest_problems.get().hide_results)
        self.assertTrue(self.contest.format_class.has_hidden_results)

    def test_non_exam_save_keeps_hide_results(self):
        """Non-exam UI hides Results but still submits the stored three-state
        so switching back to exam does not lose Hide/Show. Runtime masking is a
        no-op here (``has_hidden_results`` is False); see
        ``judge.models.tests.test_exam_contest``."""
        response = self.client.post(
            reverse('contest_edit', args=['proboptscontest']),
            data=json.dumps(self._payload(
                format_name='default',
                problems=[{'id': self.problem.id, 'points': 100, 'hide_results': 'hide'}],
            )), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.format_name, 'default')
        self.assertTrue(self.contest.contest_problems.get().hide_results)
        self.assertFalse(self.contest.format_class.has_hidden_results)

    def test_max_submissions_zero_means_unlimited(self):
        """Blank and 0 both mean unlimited (model min is 1 or None)."""
        for raw in (0, '0', '', None):
            response = self.client.post(
                reverse('contest_edit', args=['proboptscontest']),
                data=json.dumps(self._payload(problems=[
                    {'id': self.problem.id, 'points': 100, 'max_submissions': raw},
                ])), content_type='application/json',
            )
            self.assertEqual(response.status_code, 200, response.content)
            self.assertIsNone(self.contest.contest_problems.get().max_submissions, raw)


class ContestDurationGuardTestCase(CommonDataMixin, TestCase):
    """Editing duration / end_time on a contest students have already joined.

    Shrinking the personal window or pulling the deadline earlier does
    retroactively end those students' participation (the middleware clears
    their current_contest on the next request), but that is the teacher's
    call to make — a mis-typed deadline has to be fixable. The editor only
    warns; it must not refuse. These tests lock in that every direction is
    accepted, and that an extension still reaches the joined students.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.editor = create_user(username='guard_editor', user_permissions=(
            'add_contest', 'edit_own_contest', 'edit_all_contest',
        ))
        _now = timezone.now()
        cls.contest = create_contest(
            key='guardcontest', name='Guard',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=5),
            authors=('superuser',),
        )
        # The contest has a 2h personal time window.
        cls.contest.time_limit = timedelta(hours=2)
        cls.contest.save()

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.editor)

    def _payload(self, **overrides):
        now = timezone.now()
        payload = {
            'name': 'Guard', 'key': 'guardcontest',
            'start_time': (now - timedelta(days=1)).isoformat(),
            'end_time': (now + timedelta(days=5)).isoformat(),
            'duration_hours': 2, 'duration_minutes': 0,
            'is_visible': False,
        }
        payload.update(overrides)
        return payload

    def _add_participant(self):
        from judge.models import ContestParticipation
        return ContestParticipation.objects.create(
            contest=self.contest,
            user=create_user(username='guard_student_%s' % ContestParticipation.objects.count()).profile,
            real_start=timezone.now(),
        )

    # ── no participants yet → any edit allowed ────────────────────────────

    def test_no_participants_allows_shrinking(self):
        response = self.client.post(
            reverse('contest_edit', args=['guardcontest']),
            data=json.dumps(self._payload(duration_hours=1)), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.time_limit, timedelta(hours=1))

    # ── duration: any direction ───────────────────────────────────────────

    def test_can_shrink_duration_with_participants(self):
        # Shortening a live window is destructive but permitted: the teacher
        # is warned in the editor, not blocked.
        self._add_participant()
        response = self.client.post(
            reverse('contest_edit', args=['guardcontest']),
            data=json.dumps(self._payload(duration_hours=1)), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.time_limit, timedelta(hours=1))

    def test_can_extend_duration_with_participants(self):
        self._add_participant()
        response = self.client.post(
            reverse('contest_edit', args=['guardcontest']),
            data=json.dumps(self._payload(duration_hours=3)), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.time_limit, timedelta(hours=3))

    def test_can_switch_unlimited_to_finite_with_participants(self):
        # Imposing a finite duration on a previously unlimited contest is a
        # shrink for everyone already in it, and is allowed all the same.
        # Use a fresh contest so the shared setUpTestData object is untouched.
        _now = timezone.now()
        unlimited = create_contest(
            key='guardunlimited', name='Guard Unlimited',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=5),
            authors=('superuser',),
            time_limit=None,
        )
        from judge.models import ContestParticipation
        ContestParticipation.objects.create(
            contest=unlimited,
            user=create_user(username='guard_student_unlim').profile,
            real_start=timezone.now(),
        )
        response = self.client.post(
            reverse('contest_edit', args=['guardunlimited']),
            data=json.dumps({
                'name': 'Guard Unlimited', 'key': 'guardunlimited',
                'start_time': (_now - timedelta(days=1)).isoformat(),
                'end_time': (_now + timedelta(days=5)).isoformat(),
                'duration_hours': 2, 'duration_minutes': 0,
                'is_visible': False,
            }), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        unlimited.refresh_from_db()
        self.assertEqual(unlimited.time_limit, timedelta(hours=2))

    def test_can_remove_finite_duration_with_participants(self):
        # Removing a personal limit extends every window to "until the contest
        # ends" — and must restore the sessions the middleware had cleared.
        self._add_participant()
        response = self.client.post(
            reverse('contest_edit', args=['guardcontest']),
            data=json.dumps(self._payload(duration_hours=0, duration_minutes=0)),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertIsNone(self.contest.time_limit)

    # ── end_time: any direction ───────────────────────────────────────────

    def test_can_pull_end_time_earlier_with_participants(self):
        # The case this whole change is about: fixing a mis-typed deadline on
        # an assignment students have already opened.
        self._add_participant()
        now = timezone.now()
        earlier_end = now + timedelta(days=1)  # was now + 5 days
        response = self.client.post(
            reverse('contest_edit', args=['guardcontest']),
            data=json.dumps(self._payload(end_time=earlier_end.isoformat())),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.end_time, earlier_end)

    def test_can_push_end_time_later_with_participants(self):
        self._add_participant()
        now = timezone.now()
        later_end = now + timedelta(days=10)  # was now + 5 days
        response = self.client.post(
            reverse('contest_edit', args=['guardcontest']),
            data=json.dumps(self._payload(end_time=later_end.isoformat())),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.end_time, later_end)

    # ── duration increase reaches joined students ──────────────────────────
    # The submitted end_time is stored verbatim — the server never rewrites it.
    # A duration increase still shifts each joined student's ``real_start`` so
    # the extra time reaches people whose window had already run out, but it
    # remains capped by whatever deadline the teacher set.

    def test_duration_increase_keeps_submitted_deadline(self):
        """Deadline still ahead: the student's window grows by the increase and
        end_time is exactly what was posted."""
        part = self._add_participant()
        # The student's deadline is min(real_start + 2h, now + 5d) = real_start + 2h.
        old_student_deadline = ContestParticipation.objects.get(pk=part.pk).end_time
        payload = self._payload(duration_hours=3)
        response = self.client.post(
            reverse('contest_edit', args=['guardcontest']),
            data=json.dumps(payload), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.time_limit, timedelta(hours=3))
        # No server-side deadline push: the posted value is stored as-is.
        self.assertEqual(self.contest.end_time, datetime.fromisoformat(payload['end_time']))
        # The joined student's own window still grows by the same amount — the
        # view shifts their real_start, which their deadline is anchored to.
        fresh = ContestParticipation.objects.get(pk=part.pk)
        self.assertEqual(fresh.end_time, old_student_deadline + timedelta(hours=1))

    def test_duration_increase_alone_is_capped_by_a_passed_deadline(self):
        """Deadline already passed: raising the duration on its own gives the
        student nothing (end_time still caps their window) — that is deliberate,
        the deadline is the teacher's to set. Pushing end_time out as well, which
        the editor pre-fills into the visible field, revives them."""
        _now = timezone.now()
        past_end = _now - timedelta(days=2) + timedelta(hours=2)
        expired = create_contest(
            key='guardexpired', name='Guard Expired',
            start_time=_now - timedelta(days=2),
            end_time=past_end,
            authors=('superuser',),
            time_limit=timedelta(hours=2),
        )
        part = ContestParticipation.objects.create(
            contest=expired,
            user=create_user(username='guard_student_expired').profile,
            real_start=_now - timedelta(days=2),
        )

        def _post(**overrides):
            body = {
                'name': 'Guard Expired', 'key': 'guardexpired',
                'start_time': (_now - timedelta(days=2)).isoformat(),
                'end_time': past_end.isoformat(),
                'duration_hours': 3, 'duration_minutes': 0,
                'is_visible': False,
            }
            body.update(overrides)
            return body, self.client.post(
                reverse('contest_edit', args=['guardexpired']),
                data=json.dumps(body), content_type='application/json',
            )

        # 1. Duration 2h → 3h, deadline left in the past.
        body, response = _post()
        self.assertEqual(response.status_code, 200, response.content)
        expired.refresh_from_db()
        self.assertEqual(expired.time_limit, timedelta(hours=3))
        self.assertEqual(expired.end_time, past_end)  # stored verbatim
        # Still expired: min(real_start + 3h, past end) is in the past.
        self.assertLess(ContestParticipation.objects.get(pk=part.pk).end_time, _now)

        # 2. Now push the deadline out too — the student is back in.
        new_end = _now + timedelta(days=1)
        body, response = _post(end_time=new_end.isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        expired.refresh_from_db()
        self.assertEqual(expired.end_time, datetime.fromisoformat(body['end_time']))
        fresh = ContestParticipation.objects.get(pk=part.pk)
        self.assertGreater(fresh.end_time, _now)


class ContestTimeoutLogicTestCase(CommonDataMixin, TestCase):
    """Participation deadline computation and the middleware's kick-out rule.

    ``ContestParticipation.end_time`` decides when a student's window ends:
    min(real_start + time_limit, contest.end_time). Once ``ended`` is true the
    middleware clears ``profile.current_contest``, which removes the submit
    entry point (submissions no longer count toward the contest). These tests
    verify that computation — the gate that "expired students cannot submit".
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.editor = create_user(username='timeout_editor', user_permissions=(
            'add_contest', 'edit_own_contest', 'edit_all_contest',
        ))

    def _mk_contest(self, time_limit=None, end_delta_days=5):
        _now = timezone.now()
        contest = create_contest(
            key='timeout%s' % self._seq(),
            name='Timeout',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=end_delta_days),
            authors=('superuser',),
        )
        if time_limit is not None:
            contest.time_limit = time_limit
            contest.save()
        return contest

    _seq_counter = 0

    def _seq(self):
        ContestTimeoutLogicTestCase._seq_counter += 1
        return ContestTimeoutLogicTestCase._seq_counter

    def _mk_participation(self, contest, real_start=None):
        from judge.models import ContestParticipation
        return ContestParticipation.objects.create(
            contest=contest,
            user=create_user(username='timeout_student_%s' % self._seq()).profile,
            real_start=real_start or timezone.now(),
        )

    def test_no_limit_follows_contest_end(self):
        contest = self._mk_contest(time_limit=None)
        p = self._mk_participation(contest)
        self.assertEqual(p.end_time, contest.end_time)

    def test_duration_deadline_is_real_start_plus_limit(self):
        contest = self._mk_contest(time_limit=timedelta(hours=2))
        now = timezone.now()
        p = self._mk_participation(contest, real_start=now)
        self.assertEqual(p.end_time, min(now + timedelta(hours=2), contest.end_time))

    def test_duration_capped_by_contest_end(self):
        # A 10-day duration on a contest ending in 2 days must cap at contest end.
        contest = self._mk_contest(time_limit=timedelta(days=10), end_delta_days=2)
        now = timezone.now()
        p = self._mk_participation(contest, real_start=now)
        self.assertEqual(p.end_time, contest.end_time)

    def test_ended_true_after_deadline(self):
        contest = self._mk_contest(time_limit=timedelta(hours=1))
        started = timezone.now() - timedelta(hours=2)  # joined 2h ago, limit 1h
        p = self._mk_participation(contest, real_start=started)
        self.assertTrue(p.ended)

    def test_not_ended_before_deadline(self):
        contest = self._mk_contest(time_limit=timedelta(hours=3))
        started = timezone.now() - timedelta(hours=1)
        p = self._mk_participation(contest, real_start=started)
        self.assertFalse(p.ended)

    def test_extending_duration_rescues_participant(self):
        # Extending the duration after a student joined moves their deadline
        # and un-expires them — this is what the editor's real_start shift and
        # session restore are ultimately for.
        contest = self._mk_contest(time_limit=timedelta(hours=1))
        started = timezone.now() - timedelta(hours=2)
        p = self._mk_participation(contest, real_start=started)
        self.assertTrue(p.ended)
        contest.time_limit = timedelta(hours=3)
        contest.save()
        # end_time/_now are cached_properties on the participation instance;
        # drop the cache so the extension is observed (mirrors a fresh request).
        try:
            del p.end_time
            del p._now
        except AttributeError:
            pass
        self.assertFalse(p.ended)

    def test_shrinking_duration_expires_participant(self):
        # And shrinking it retroactively expires someone mid-run. The editor
        # allows that (a wrong deadline has to be fixable) but warns first —
        # this is the damage the warning is about.
        contest = self._mk_contest(time_limit=timedelta(hours=3))
        started = timezone.now() - timedelta(hours=2)
        p = self._mk_participation(contest, real_start=started)
        self.assertFalse(p.ended)
        contest.time_limit = timedelta(hours=1)
        contest.save()
        # See above: cached_property on the participation must be dropped.
        try:
            del p.end_time
            del p._now
        except AttributeError:
            pass
        self.assertTrue(p.ended)


@override_settings(DMOJ_ENABLE_SOCIAL=False, DMOJ_ENABLE_COMMENTS=False)
class ContestExtensionReadmissionTestCase(CommonDataMixin, TestCase):
    """Teacher extends duration → expired students are re-admitted.

    Regression for the production bug where a student could SEE the extended
    remaining time but clicking any problem returned "No such problem" (404).
    Root cause: when a student's window ended, the middleware cleared
    ``profile.current_contest``; the extension un-expired the participation
    (``real_start`` shifted) but nothing restored ``current_contest``, and
    ``Problem.is_accessible_by`` only admits contest problems while it is set.

    Access is asserted via ``Problem.is_accessible_by`` (the exact gate behind
    the 404) rather than a full page render, which hits a pre-existing Django
    4.2 + Python 3.14 template bug unrelated to this feature.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.editor = create_user(username='readmit_editor', user_permissions=(
            'add_contest', 'edit_own_contest', 'edit_all_contest',
        ))
        _now = timezone.now()
        cls.contest = create_contest(
            key='readmitcontest', name='Readmit',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=5),
            authors=('superuser',),
            is_visible=True,
            time_limit=timedelta(hours=2),
        )
        cls.problem = create_problem('readmit-problem')  # is_public defaults False
        from judge.models import ContestProblem
        ContestProblem.objects.create(contest=cls.contest, problem=cls.problem, points=100, order=1)

    def setUp(self):
        self.client = Client()

    def _payload(self, **overrides):
        now = timezone.now()
        payload = {
            'name': 'Readmit', 'key': 'readmitcontest',
            'start_time': (now - timedelta(days=1)).isoformat(),
            'end_time': (now + timedelta(days=5)).isoformat(),
            'duration_hours': 2, 'duration_minutes': 0,
            'is_visible': False,
        }
        payload.update(overrides)
        return payload

    def _extend_duration(self):
        self.client.force_login(self.editor)
        return self.client.post(
            reverse('contest_edit', args=['readmitcontest']),
            data=json.dumps(self._payload(duration_hours=3)), content_type='application/json',
        )

    def _expired_student(self, username='readmit_student'):
        """A student whose 2h window ran out 1h ago; middleware cleared the session."""
        profile = create_user(username=username).profile
        participation = ContestParticipation.objects.create(
            contest=self.contest, user=profile,
            real_start=timezone.now() - timedelta(hours=3),  # deadline = now - 1h
        )
        profile.current_contest = participation
        profile.save()
        profile.update_contest()  # simulates the middleware kick-out
        profile.refresh_from_db()
        self.assertIsNone(profile.current_contest)
        return profile, participation

    def test_extended_student_is_readmitted_and_can_open_problem(self):
        """The exact production bug: expired student, teacher extends → the
        student must be back in the contest and able to open its problems."""
        profile, participation = self._expired_student()
        response = self._extend_duration()
        self.assertEqual(response.status_code, 200, response.content)
        profile.refresh_from_db()
        self.assertEqual(profile.current_contest_id, participation.id)  # re-admitted
        # The access gate behind the "No such problem" 404 now admits them.
        self.assertTrue(self.problem.is_accessible_by(profile.user))

    def test_valid_window_with_cleared_session_is_readmitted(self):
        """The actual production scenario: the student's window is STILL valid
        (never expired) but current_contest was cleared (e.g. the middleware
        dropped it while the contest was briefly inaccessible). A duration
        increase must restore the session — this is the case the original
        rescue-only fix missed."""
        profile = create_user(username='readmit_valid').profile
        participation = ContestParticipation.objects.create(
            contest=self.contest, user=profile,
            real_start=timezone.now() - timedelta(minutes=30),  # deadline still ahead
        )
        profile.current_contest = None  # session missing for an unrelated reason
        profile.save()
        response = self._extend_duration()
        self.assertEqual(response.status_code, 200, response.content)
        profile.refresh_from_db()
        self.assertEqual(profile.current_contest_id, participation.id)  # session restored
        self.assertTrue(self.problem.is_accessible_by(profile.user))

    def test_removing_limit_restores_missing_session(self):
        """Removing the personal duration (window → until contest end) is also
        an extension, so it must restore missing sessions the same way."""
        profile = create_user(username='readmit_unlimited').profile
        participation = ContestParticipation.objects.create(
            contest=self.contest, user=profile,
            real_start=timezone.now() - timedelta(minutes=30),
        )
        profile.current_contest = None
        profile.save()
        self.client.force_login(self.editor)
        response = self.client.post(
            reverse('contest_edit', args=['readmitcontest']),
            data=json.dumps(self._payload(duration_hours=0, duration_minutes=0)),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        profile.refresh_from_db()
        self.assertEqual(profile.current_contest_id, participation.id)

    def test_student_in_another_contest_is_not_hijacked(self):
        """An expired student who moved on to a different contest keeps their
        current session — the extension of the old contest must not override it."""
        profile, _ = self._expired_student(username='readmit_busy')
        other = create_contest(
            key='readmitother', name='Other',
            start_time=timezone.now() - timedelta(days=1),
            end_time=timezone.now() + timedelta(days=5),
            authors=('superuser',),
            time_limit=timedelta(hours=2),
        )
        other_participation = ContestParticipation.objects.create(
            contest=other, user=profile, real_start=timezone.now(),
        )
        profile.current_contest = other_participation
        profile.save()
        response = self._extend_duration()
        self.assertEqual(response.status_code, 200, response.content)
        profile.refresh_from_db()
        self.assertEqual(profile.current_contest_id, other_participation.id)  # untouched

    def test_active_student_keeps_current_contest(self):
        """A student mid-window is untouched: extension keeps their session."""
        profile = create_user(username='readmit_active').profile
        participation = ContestParticipation.objects.create(
            contest=self.contest, user=profile, real_start=timezone.now(),
        )
        profile.current_contest = participation
        profile.save()
        response = self._extend_duration()
        self.assertEqual(response.status_code, 200, response.content)
        profile.refresh_from_db()
        self.assertEqual(profile.current_contest_id, participation.id)  # unchanged

    def test_expired_student_without_extension_stays_locked_out(self):
        """Positive control: without a teacher extension the expired student
        stays locked out (404) — the fix must not over-grant access."""
        profile, _ = self._expired_student(username='readmit_locked')
        self.assertFalse(self.problem.is_accessible_by(profile.user))
