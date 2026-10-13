"""Security / robustness tests for the contest editor JSON API.

Three attack surfaces are locked in here:

* **Concurrent editing** — two teachers race on the same contest. Contest
  times are last-write-wins (the "only extend, never shorten" guards that
  used to reject a stale form were removed so a wrong deadline stays
  fixable), so what these pin down is that each save lands *atomically*:
  whichever payload wins is applied whole, never half.
* **Malicious payloads** — hostile JSON (floats that overflow to infinity,
  astronomically large ints, nested objects in text fields, SQLi strings,
  unknown fields) must produce a clean 4xx or a benign no-op, never a 500
  and never a corrupted row.
* **Student-side simulation** — a student's access matrix for contest
  problems: only an active live session in the *containing* contest grants
  access. Expired students and outsiders stay locked out; spectators may
  still view; nobody but editors may edit.
"""

import json
from datetime import datetime, timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import ContestParticipation, ContestProblem
from judge.models.tests.util import CommonDataMixin, create_contest, create_problem, create_user


class ContestConcurrentEditTestCase(CommonDataMixin, TestCase):
    """Two teachers racing on the same contest (read-modify-write hazard).

    Each save carries a full-form payload, so the loser's stale form clobbers
    the winner's changes. The extend-only guards used to reject that as a side
    effect; they were removed so a teacher can fix a mis-typed deadline, which
    makes contest times plain last-write-wins. These tests pin that down — it
    is the accepted trade-off, not an oversight — and check that whatever does
    land is applied atomically.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.teacher_a = create_user(
            username='race_teacher_a',
            user_permissions=('edit_own_contest', 'edit_all_contest'),
        )
        cls.teacher_b = create_user(
            username='race_teacher_b',
            user_permissions=('edit_own_contest', 'edit_all_contest'),
        )
        _now = timezone.now()
        cls.contest = create_contest(
            key='racecontest', name='Race',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=5),
            authors=('superuser',),
            time_limit=timedelta(hours=2),
        )
        # Students have joined — no longer a gate on shortening, but it keeps
        # the participation-shifting code path in play for these saves.
        ContestParticipation.objects.create(
            contest=cls.contest,
            user=create_user(username='race_student').profile,
            real_start=_now,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.teacher_a)

    def _payload(self, **overrides):
        now = timezone.now()
        payload = {
            'name': 'Race', 'key': 'racecontest',
            'start_time': (now - timedelta(days=1)).isoformat(),
            'end_time': (now + timedelta(days=5)).isoformat(),
            'duration_hours': 2, 'duration_minutes': 0,
            'is_visible': False,
        }
        payload.update(overrides)
        return payload

    def test_stale_form_overwrites_an_earlier_extension(self):
        # Teacher A extends 2h → 3h...
        a = self.client.post(
            reverse('contest_edit', args=['racecontest']),
            data=json.dumps(self._payload(duration_hours=3)), content_type='application/json',
        )
        self.assertEqual(a.status_code, 200, a.content)
        # ...then teacher B, who loaded the page BEFORE A's save, submits their
        # stale form (original 2h + a rename). Nothing rejects it any more:
        # last write wins, and B's whole payload lands together.
        self.client.force_login(self.teacher_b)
        b = self.client.post(
            reverse('contest_edit', args=['racecontest']),
            data=json.dumps(self._payload(duration_hours=2, name='Stale Rename')),
            content_type='application/json',
        )
        self.assertEqual(b.status_code, 200, b.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.time_limit, timedelta(hours=2))  # A's extension lost
        self.assertEqual(self.contest.name, 'Stale Rename')

    def test_stale_form_pulls_the_end_time_back_in(self):
        # Teacher A pushes the deadline out to +10 days...
        now = timezone.now()
        later = now + timedelta(days=10)
        a = self.client.post(
            reverse('contest_edit', args=['racecontest']),
            data=json.dumps(self._payload(end_time=later.isoformat())), content_type='application/json',
        )
        self.assertEqual(a.status_code, 200, a.content)
        # ...teacher B's stale form still carries the original +5d deadline and
        # now wins. Same trade-off as above: the check that used to catch this
        # also blocked legitimately correcting a wrong deadline.
        self.client.force_login(self.teacher_b)
        stale = self._payload()
        b = self.client.post(
            reverse('contest_edit', args=['racecontest']),
            data=json.dumps(stale), content_type='application/json',
        )
        self.assertEqual(b.status_code, 200, b.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.end_time, datetime.fromisoformat(stale['end_time']))

    def test_same_base_extension_by_both_teachers_is_allowed(self):
        # Both loaded the 2h form. A saves 3h; B's form — with the editor's
        # recalculated end (max(saved end, now) + increase, i.e. the stored
        # base plus B's own 1h bump) — saves 4h. Nothing conflicts here: B's
        # values are strictly larger, so the later write wins with no data
        # loss and no lock needed.
        now = timezone.now()
        a = self.client.post(
            reverse('contest_edit', args=['racecontest']),
            data=json.dumps(self._payload(
                duration_hours=3,
                end_time=(now + timedelta(days=5, hours=1)).isoformat(),
            )), content_type='application/json',
        )
        self.assertEqual(a.status_code, 200, a.content)
        self.client.force_login(self.teacher_b)
        b = self.client.post(
            reverse('contest_edit', args=['racecontest']),
            data=json.dumps(self._payload(
                duration_hours=4,
                end_time=(now + timedelta(days=5, hours=2)).isoformat(),
            )), content_type='application/json',
        )
        self.assertEqual(b.status_code, 200, b.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.time_limit, timedelta(hours=4))


class ContestMaliciousPayloadTestCase(CommonDataMixin, TestCase):
    """Hostile / malformed JSON bodies must never 500 or corrupt the row.

    The editor is a JSON API reachable by everyone with contest edit rights,
    so a crafted body is cheap to try. Every case here asserts either a clean
    4xx or a benign no-op on the stored row.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.editor = create_user(
            username='secure_editor',
            user_permissions=('edit_own_contest', 'edit_all_contest'),
        )
        _now = timezone.now()
        cls.contest = create_contest(
            key='securecontest', name='Secure',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=5),
            authors=('superuser',),
            time_limit=timedelta(hours=2),
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.editor)

    def _payload(self, **overrides):
        now = timezone.now()
        payload = {
            'name': 'Secure', 'key': 'securecontest',
            'start_time': (now - timedelta(days=1)).isoformat(),
            'end_time': (now + timedelta(days=5)).isoformat(),
            'duration_hours': 2, 'duration_minutes': 0,
            'is_visible': False,
        }
        payload.update(overrides)
        return payload

    def test_malformed_json_returns_400(self):
        response = self.client.post(
            reverse('contest_edit', args=['securecontest']),
            data='{this is definitely not json', content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid request', json.loads(response.content)['error'])

    def test_infinity_duration_returns_400_not_500(self):
        # json.loads('1e309') → float('inf'); int(inf) raises OverflowError.
        # Regression: this used to fall through to the generic 500 handler.
        for field in ('duration_hours', 'duration_minutes'):
            with self.subTest(field=field):
                response = self.client.post(
                    reverse('contest_edit', args=['securecontest']),
                    data=json.dumps(self._payload(**{field: 1e309})), content_type='application/json',
                )
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn('Invalid duration', json.loads(response.content)['error'])
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.time_limit, timedelta(hours=2))  # untouched

    def test_astronomically_large_duration_string_returns_400(self):
        response = self.client.post(
            reverse('contest_edit', args=['securecontest']),
            data=json.dumps(self._payload(duration_hours='9' * 200)), content_type='application/json',
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.time_limit, timedelta(hours=2))

    def test_negative_duration_returns_400(self):
        response = self.client.post(
            reverse('contest_edit', args=['securecontest']),
            data=json.dumps(self._payload(duration_hours=-3)), content_type='application/json',
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_infinity_in_int_field_falls_back_to_default(self):
        # Overflowing int fields degrade to the model default instead of 500.
        response = self.client.post(
            reverse('contest_edit', args=['securecontest']),
            data=json.dumps(self._payload(points_precision=1e309)), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.points_precision, 3)  # model default

    def test_unknown_fields_are_ignored(self):
        # Mass-assignment probe: fields that are not registered (or that the
        # user may not touch) must be silently skipped, not persisted.
        response = self.client.post(
            reverse('contest_edit', args=['securecontest']),
            data=json.dumps({**self._payload(), 'totally_unknown_field': 'x', 'is_rated': True}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertFalse(self.contest.is_rated)  # restricted without contest_rating

    def test_text_field_with_nested_object_is_stringified_not_crashed(self):
        # A nested object in a text field is coerced to its string form by the
        # model field — the request succeeds and the row is never corrupted,
        # and a 500 is impossible.
        response = self.client.post(
            reverse('contest_edit', args=['securecontest']),
            data=json.dumps(self._payload(description={'nested': ['evil', 1]})),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertIn('nested', self.contest.description)

    def test_sql_injection_in_name_is_stored_as_literal_text(self):
        evil = "x'; DROP TABLE judge_contest; --"
        response = self.client.post(
            reverse('contest_edit', args=['securecontest']),
            data=json.dumps(self._payload(name=evil)), content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.name, evil)  # stored verbatim (ORM-parameterized)

    def test_key_with_special_characters_returns_400(self):
        response = self.client.post(
            reverse('contest_edit', args=['securecontest']),
            data=json.dumps(self._payload(key="x'; DROP")), content_type='application/json',
        )
        self.assertEqual(response.status_code, 400, response.content)

    def test_curator_of_another_contest_cannot_edit(self):
        outsider = create_user(username='secure_outsider', user_permissions=('edit_own_contest',))
        self.client.force_login(outsider)
        response = self.client.post(
            reverse('contest_edit', args=['securecontest']),
            data=json.dumps(self._payload()), content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_anonymous_post_is_redirected_to_login(self):
        # dispatch() sends anonymous users to login before post() can run.
        self.client.logout()
        response = self.client.post(
            reverse('contest_edit', args=['securecontest']),
            data=json.dumps(self._payload()), content_type='application/json',
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])


@override_settings(DMOJ_ENABLE_SOCIAL=False, DMOJ_ENABLE_COMMENTS=False)
class ContestStudentSimulationTestCase(CommonDataMixin, TestCase):
    """Student-side access matrix for contest problems.

    A student may open a contest problem ONLY while holding a session (live
    participation) in the containing contest. Expired sessions, outsiders,
    and no-session students are all rejected with 404.

    Access is asserted via ``Problem.is_accessible_by`` — the exact gate
    ``ProblemMixin.get_object`` consults before raising the "No such problem"
    Http404. Asserting at the model level keeps these tests independent of
    the workspace page template, whose Django-form widget render hits a
    pre-existing Django 4.2 + Python 3.14 ``BaseContext.__copy__`` bug
    (same env issue as ``test_contest_detail_timezone``).
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        _now = timezone.now()
        cls.contest_a = create_contest(
            key='simcontest_a', name='Sim A',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=5),
            authors=('superuser',),
            is_visible=True,
            time_limit=timedelta(hours=2),
        )
        cls.contest_b = create_contest(
            key='simcontest_b', name='Sim B',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=5),
            authors=('superuser',),
            is_visible=True,
            time_limit=timedelta(hours=2),
        )
        # Private problems (is_public defaults False), each in exactly one contest.
        cls.problem_a = create_problem('sim-problem-a')
        cls.problem_b = create_problem('sim-problem-b')
        ContestProblem.objects.create(contest=cls.contest_a, problem=cls.problem_a, points=100, order=1)
        ContestProblem.objects.create(contest=cls.contest_b, problem=cls.problem_b, points=100, order=1)
        cls.student = create_user(username='sim_student').profile

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.student.user)

    def _join(self, contest, virtual=ContestParticipation.LIVE, real_start=None):
        participation = ContestParticipation.objects.create(
            contest=contest, user=self.student, virtual=virtual,
            real_start=real_start or timezone.now(),
        )
        self.student.current_contest = participation
        self.student.save()
        return participation

    def test_active_student_can_open_own_contest_problem(self):
        self._join(self.contest_a)
        self.assertTrue(self.problem_a.is_accessible_by(self.student.user))

    def test_student_cannot_open_problem_of_contest_they_are_not_in(self):
        # In contest A, trying to open a problem that only belongs to B.
        self._join(self.contest_a)
        self.assertFalse(self.problem_b.is_accessible_by(self.student.user))

    def test_student_without_session_cannot_open_contest_problem(self):
        # Never joined: the private problem must stay invisible.
        self.assertFalse(self.problem_a.is_accessible_by(self.student.user))

    def test_expired_student_is_locked_out(self):
        # Window ran out (real_start 3h ago, 2h limit) and the middleware
        # cleared current_contest — no teacher extension → still locked out.
        participation = self._join(self.contest_a, real_start=timezone.now() - timedelta(hours=3))
        self.assertTrue(participation.ended)
        self.student.update_contest()  # what the middleware does each request
        self.assertIsNone(self.student.current_contest)
        self.assertFalse(self.problem_a.is_accessible_by(self.student.user))

    def test_spectator_can_view_contest_problem(self):
        # A spectator keeps a current_contest, so the problem stays viewable.
        self._join(self.contest_a, virtual=ContestParticipation.SPECTATE)
        self.assertTrue(self.problem_a.is_accessible_by(self.student.user))

    def test_student_cannot_edit_contest(self):
        # A plain student (no edit permission) must never reach the editor.
        response = self.client.post(
            reverse('contest_edit', args=['simcontest_a']),
            data=json.dumps({
                'name': 'Sim A', 'key': 'simcontest_a',
                'start_time': (timezone.now() - timedelta(days=1)).isoformat(),
                'end_time': (timezone.now() + timedelta(days=5)).isoformat(),
            }), content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
