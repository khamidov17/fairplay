from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from judge.models import Contest, ContestParticipation, ContestTag
from judge.models.contest import MinValueOrNoneValidator, contest_problem_display_name
from judge.models.tests.util import (
    CommonDataMixin, create_contest, create_contest_participation,
    create_contest_problem, create_problem, create_user,
)


class ContestTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.users.update({
            'staff_contest_edit_own': create_user(
                username='staff_contest_edit_own',
                is_staff=True,
                user_permissions=('edit_own_contest',),
            ),
            'staff_contest_see_all': create_user(
                username='staff_contest_see_all',
                user_permissions=('see_private_contest',),
            ),
            'staff_contest_edit_all': create_user(
                username='staff_contest_edit_all',
                is_staff=True,
                user_permissions=('edit_own_contest', 'edit_all_contest'),
            ),
            'normal_during_window': create_user(
                username='normal_during_window',
            ),
            'normal_after_window': create_user(
                username='normal_after_window',
            ),
            'normal_before_window': create_user(
                username='normal_before_window',
            ),
            'non_staff_author': create_user(
                username='non_staff_author',
                is_staff=False,
            ),
            'non_staff_tester': create_user(
                username='non_staff_tester',
                is_staff=False,
            ),
            'normal_open_org': create_user(
                username='normal_open_org',
                is_staff=False,
            ),
            'non_staff_spectator': create_user(
                username='non_staff_spectator',
                is_staff=False,
            ),
        })

        self.users['normal_open_org'].profile.organizations.add(self.organizations['open'])

        _now = timezone.now()

        self.basic_contest = create_contest(
            key='basic',
            start_time=_now - timezone.timedelta(days=1),
            end_time=_now + timezone.timedelta(days=100),
            authors=('superuser', 'staff_contest_edit_own'),
            testers=('non_staff_tester',),
        )

        self.hidden_scoreboard_contest = create_contest(
            key='hidden_scoreboard',
            start_time=_now - timezone.timedelta(days=1),
            end_time=_now + timezone.timedelta(days=100),
            is_visible=True,
            scoreboard_visibility=Contest.SCOREBOARD_AFTER_CONTEST,
            problem_label_script="""
                function(n)
                    return tostring(math.floor(n))
                end
            """,
            spectators=('non_staff_spectator',),
        )

        self.hidden_scoreboard_non_staff_author = create_contest(
            key='non_staff_author',
            start_time=_now - timezone.timedelta(days=1),
            end_time=_now + timezone.timedelta(days=100),
            is_visible=True,
            scoreboard_visibility=Contest.SCOREBOARD_AFTER_CONTEST,
            authors=('non_staff_author',),
            curators=('staff_contest_edit_own',),
        )

        self.contest_hidden_scoreboard_contest = create_contest(
            key='contest_scoreboard',
            start_time=_now - timezone.timedelta(days=10),
            end_time=_now + timezone.timedelta(days=100),
            time_limit=timezone.timedelta(days=1),
            is_visible=True,
            scoreboard_visibility=Contest.SCOREBOARD_AFTER_CONTEST,
            testers=('non_staff_tester',),
        )

        self.particip_hidden_scoreboard_contest = create_contest(
            key='particip_scoreboard',
            start_time=_now - timezone.timedelta(days=10),
            end_time=_now + timezone.timedelta(days=100),
            time_limit=timezone.timedelta(days=1),
            is_visible=True,
            scoreboard_visibility=Contest.SCOREBOARD_AFTER_PARTICIPATION,
            testers=('non_staff_tester',),
        )

        self.visible_scoreboard_contest = create_contest(
            key='visible_scoreboard',
            start_time=_now - timezone.timedelta(days=10),
            end_time=_now + timezone.timedelta(days=100),
            time_limit=timezone.timedelta(days=1),
            is_visible=True,
            scoreboard_visibility=Contest.SCOREBOARD_VISIBLE,
            testers=('non_staff_tester',),
        )

        self.full_hidden_scoreboard_contest = create_contest(
            key='full_hidden_board',
            start_time=_now - timezone.timedelta(days=100),
            end_time=_now - timezone.timedelta(days=1),
            time_limit=timezone.timedelta(days=1),
            is_visible=True,
            scoreboard_visibility=Contest.SCOREBOARD_HIDDEN,
            authors=('non_staff_author',),
            curators=('staff_contest_edit_own',),
            testers=('non_staff_tester',),
            spectators=('non_staff_spectator',),
        )

        self.future_contest = create_contest(
            key='future_contest',
            start_time=_now + timezone.timedelta(days=3),
            end_time=_now + timezone.timedelta(days=5),
            time_limit=timezone.timedelta(days=1),
            is_visible=True,
            scoreboard_visibility=Contest.SCOREBOARD_AFTER_CONTEST,
            authors=('non_staff_author',),
            curators=('staff_contest_edit_own',),
            testers=('non_staff_tester',),
            spectators=('non_staff_spectator',),
        )

        self.tester_see_scoreboard_contest = create_contest(
            key='tester_see_board',
            start_time=_now - timezone.timedelta(days=100),
            end_time=_now + timezone.timedelta(days=100),
            time_limit=timezone.timedelta(days=1),
            is_visible=True,
            scoreboard_visibility=Contest.SCOREBOARD_HIDDEN,
            testers=('non_staff_tester',),
            tester_see_scoreboard=True,
        )

        self.public_limit_organization_join_contest = create_contest(
            key='limit_org_join',
            start_time=_now - timezone.timedelta(days=2),
            end_time=_now + timezone.timedelta(days=2),
            time_limit=timezone.timedelta(days=1),
            is_visible=True,
            limit_join_organizations=True,
            join_organizations=('open',),
            scoreboard_visibility=Contest.SCOREBOARD_VISIBLE,
            authors=('non_staff_author',),
            curators=('staff_contest_edit_own',),
            testers=('non_staff_tester',),
        )

        for contest_key in ('contest_scoreboard', 'particip_scoreboard', 'visible_scoreboard'):
            create_contest_participation(
                contest=contest_key,
                user='normal_during_window',
                real_start=_now - timezone.timedelta(hours=1),
                virtual=ContestParticipation.LIVE,
            )

            create_contest_participation(
                contest=contest_key,
                user='normal_after_window',
                real_start=_now - timezone.timedelta(days=3),
                virtual=ContestParticipation.LIVE,
            )

        create_contest_participation(
            contest='particip_scoreboard',
            user='normal',
            real_start=_now - timezone.timedelta(days=3),
            virtual=ContestParticipation.LIVE,
        )

        create_contest_participation(
            contest='particip_scoreboard',
            user='normal',
            real_start=_now + timezone.timedelta(days=101),
            virtual=ContestParticipation.SPECTATE,
        )

        create_contest_participation(
            contest='full_hidden_board',
            user='normal_after_window',
            real_start=_now - timezone.timedelta(days=5),
            virtual=ContestParticipation.LIVE,
        )

        self.users['normal'].profile.current_contest = create_contest_participation(
            contest='hidden_scoreboard',
            user='normal',
        )
        self.users['normal'].profile.save()

        self.hidden_scoreboard_contest.update_user_count()

        self.private_contest = create_contest(
            key='private',
            start_time=_now - timezone.timedelta(days=5),
            end_time=_now - timezone.timedelta(days=3),
            is_visible=True,
            is_private=True,
            is_organization_private=True,
            private_contestants=('staff_contest_edit_own',),
            testers=('non_staff_tester',),
        )

        self.organization_private_contest = create_contest(
            key='organization_private',
            start_time=_now - timezone.timedelta(days=5),
            end_time=_now + timezone.timedelta(days=6),
            is_visible=True,
            is_organization_private=True,
            organizations=('open',),
            view_contest_scoreboard=('normal',),
            testers=('non_staff_tester',),
        )

        self.future_organization_private_contest = create_contest(
            key='future_org_private',
            start_time=_now + timezone.timedelta(days=3),
            end_time=_now + timezone.timedelta(days=6),
            is_visible=True,
            is_organization_private=True,
            organizations=('open',),
            view_contest_scoreboard=('normal',),
            testers=('non_staff_tester',),
        )

        self.private_user_contest = create_contest(
            key='private_user',
            start_time=_now - timezone.timedelta(days=3),
            end_time=_now + timezone.timedelta(days=6),
            is_visible=True,
            is_private=True,
            testers=('non_staff_tester',),
        )

        self.non_visible_contest = create_contest(
            key='non_visible_contest',
            start_time=_now - timezone.timedelta(days=3),
            end_time=_now + timezone.timedelta(days=6),
            is_visible=False,
        )

        self.non_visible_contest_with_tester = create_contest(
            key='non_visible_w_tester',
            start_time=_now - timezone.timedelta(days=3),
            end_time=_now + timezone.timedelta(days=6),
            is_visible=False,
            testers=('non_staff_tester',),
        )

    def setUp(self):
        self.users['normal'].profile.refresh_from_db()

    def test_basic_contest(self):
        self.assertTrue(self.basic_contest.show_scoreboard)
        self.assertEqual(self.basic_contest.contest_window_length, timezone.timedelta(days=101))
        self.assertIsInstance(self.basic_contest._now, timezone.datetime)
        self.assertTrue(self.basic_contest.started)
        self.assertIsNone(self.basic_contest.time_before_start)
        self.assertIsInstance(self.basic_contest.time_before_end, timezone.timedelta)
        self.assertFalse(self.basic_contest.ended)
        self.assertEqual(str(self.basic_contest), self.basic_contest.name)
        self.assertEqual(self.basic_contest.get_label_for_problem(0), '1')

    def test_hidden_scoreboard_contest(self):
        self.assertFalse(self.hidden_scoreboard_contest.show_scoreboard)
        for i in range(3):
            with self.subTest(contest_problem_index=i):
                self.assertEqual(self.hidden_scoreboard_contest.get_label_for_problem(i), str(i))
        self.assertEqual(self.hidden_scoreboard_contest.user_count, 1)

    def test_private_contest(self):
        self.assertTrue(self.private_contest.started)
        self.assertIsNone(self.private_contest.time_before_start)
        self.assertIsNone(self.private_contest.time_before_end)

    def test_organization_private_contest(self):
        self.assertTrue(self.organization_private_contest.started)
        self.assertTrue(self.organization_private_contest.show_scoreboard)
        self.assertFalse(self.organization_private_contest.ended)
        self.assertIsNone(self.organization_private_contest.time_before_start)
        self.assertIsInstance(self.organization_private_contest.time_before_end, timezone.timedelta)

    def test_future_organization_private_contest(self):
        self.assertFalse(self.future_organization_private_contest.started)
        self.assertFalse(self.future_organization_private_contest.show_scoreboard)
        self.assertFalse(self.future_organization_private_contest.ended)
        self.assertIsInstance(self.future_organization_private_contest.time_before_start, timezone.timedelta)
        self.assertIsInstance(self.future_organization_private_contest.time_before_end, timezone.timedelta)

    def test_basic_contest_methods(self):
        with self.assertRaises(Contest.Inaccessible):
            self.basic_contest.access_check(self.users['normal'])

        data = {
            'superuser': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_live_joinable_by': self.assertFalse,  # author
                'is_spectatable_by': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
                'is_in_contest': self.assertFalse,
            },
            'staff_contest_edit_own': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_live_joinable_by': self.assertFalse,  # author
                'is_spectatable_by': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
                'is_in_contest': self.assertFalse,
            },
            'staff_contest_see_all': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_live_joinable_by': self.assertTrue,
                'is_spectatable_by': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'staff_contest_edit_all': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_live_joinable_by': self.assertTrue,
                'is_spectatable_by': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
                'is_in_contest': self.assertFalse,
            },
            'normal': {
                # scoreboard checks don't do accessibility checks
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_live_joinable_by': self.assertTrue,
                'is_spectatable_by': self.assertTrue,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'non_staff_tester': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'anonymous': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertFalse,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.basic_contest, data)

    def test_spectating_requires_assignment_opt_in(self):
        """Students cannot spectate unless the assignment explicitly permits it."""
        self.basic_contest.allow_spectate = False
        self.basic_contest.save(update_fields=('allow_spectate',))

        self.assertFalse(self.basic_contest.is_spectatable_by(self.users['normal']))
        # Assignment staff retain their existing ability to inspect the contest.
        self.assertTrue(self.basic_contest.is_spectatable_by(self.users['superuser']))

        self.basic_contest.allow_spectate = True
        self.basic_contest.save(update_fields=('allow_spectate',))
        self.assertTrue(self.basic_contest.is_spectatable_by(self.users['normal']))

    def test_hidden_scoreboard_contest_methods(self):
        data = {
            'staff_contest_edit_own': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'staff_contest_see_all': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'staff_contest_edit_all': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
                'is_in_contest': self.assertFalse,
            },
            'non_staff_spectator': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'normal': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertFalse,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertTrue,
            },
            'anonymous': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.hidden_scoreboard_contest, data)

    def test_contest_hidden_scoreboard_non_staff_author_contest_methods(self):
        data = {
            'staff_contest_edit_own': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
                'is_in_contest': self.assertFalse,
            },
            'non_staff_author': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,  # listed author -> edit rights
                'is_in_contest': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.hidden_scoreboard_non_staff_author, data)

    def test_contest_hidden_scoreboard_contest_methods(self):
        data = {
            'normal_before_window': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
                'is_live_joinable_by': self.assertTrue,
                'is_spectatable_by': self.assertTrue,
                'has_completed_contest': self.assertFalse,
            },
            'normal_during_window': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
                'has_completed_contest': self.assertFalse,
            },
            'normal_after_window': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertFalse,
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertTrue,
                'has_completed_contest': self.assertTrue,
            },
            'non_staff_tester': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertTrue,
                'has_completed_contest': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.contest_hidden_scoreboard_contest, data)

    def test_particip_hidden_scoreboard_contest_methods(self):
        data = {
            'normal_before_window': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
                'has_completed_contest': self.assertFalse,
            },
            'normal_during_window': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
                'has_completed_contest': self.assertFalse,
            },
            'normal_after_window': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'has_completed_contest': self.assertTrue,
            },
            'normal': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'has_completed_contest': self.assertTrue,
            },
            'non_staff_tester': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
                'has_completed_contest': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.particip_hidden_scoreboard_contest, data)

    def test_visible_scoreboard_contest_methods(self):
        data = {
            'normal_before_window': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'has_completed_contest': self.assertFalse,
            },
            'normal_during_window': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'has_completed_contest': self.assertFalse,
            },
            'normal_after_window': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'has_completed_contest': self.assertTrue,
            },
            'non_staff_tester': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'has_completed_contest': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.visible_scoreboard_contest, data)

    def test_full_hidden_scoreboard_contest_methods(self):
        data = {
            'superuser': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
            },
            'non_staff_tester': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
            },
            'non_staff_author': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
            },
            'staff_contest_edit_own': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
            },
            'staff_contest_edit_all': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
            },
            'normal': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
            },
            'normal_after_window': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.full_hidden_scoreboard_contest, data)

    def test_tester_see_scoreboard_contest_methods(self):
        data = {
            'superuser': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
            },
            'non_staff_tester': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
            },
            'normal': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.tester_see_scoreboard_contest, data)

    def test_public_limit_organization_join_contest(self):
        data = {
            'non_staff_tester': {
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertTrue,
            },
            'non_staff_author': {
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertTrue,
            },
            'staff_contest_edit_own': {
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertTrue,  # curator
            },
            'staff_contest_edit_all': {
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertFalse,
            },
            'normal': {
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertFalse,
            },
            'normal_open_org': {
                'is_live_joinable_by': self.assertTrue,
                'is_spectatable_by': self.assertTrue,
            },
            'normal_after_window': {
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertFalse,  # not in org
            },
        }
        self._test_object_methods_with_users(self.public_limit_organization_join_contest, data)

    def test_future_contest_methods(self):
        data = {
            'non_staff_spectator': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
            },
            'non_staff_tester': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
            },
            'non_staff_author': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
            },
            'staff_contest_edit_own': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
            },
            'staff_contest_edit_all': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
            },
            'normal': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.future_contest, data)

    def test_private_contest_methods(self):
        # User must be in org and in private user list
        with self.assertRaises(Contest.PrivateContest):
            self.private_contest.access_check(self.users['normal_open_org'])
        self.private_contest.private_contestants.add(self.users['normal_open_org'].profile)
        with self.assertRaises(Contest.PrivateContest):
            self.private_contest.access_check(self.users['normal_open_org'])
        self.private_contest.organizations.add(self.organizations['open'])

        data = {
            'normal_open_org': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_live_joinable_by': self.assertTrue,
                'is_spectatable_by': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'staff_contest_see_all': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'anonymous': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'non_staff_tester': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.private_contest, data)

    def test_organization_private_contest_methods(self):
        data = {
            'staff_contest_edit_own': {
                # scoreboard checks don't do accessibility checks
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'staff_contest_see_all': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'staff_contest_edit_all': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
                'is_in_contest': self.assertFalse,
            },
            'normal': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_live_joinable_by': self.assertTrue,
                'is_spectatable_by': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'non_staff_tester': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'anonymous': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.organization_private_contest, data)

    def test_future_organization_private_contest_methods(self):
        data = {
            'staff_contest_edit_own': {
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertTrue,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'staff_contest_see_all': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'staff_contest_edit_all': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
                'is_in_contest': self.assertFalse,
            },
            'normal': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'non_staff_tester': {
                # False because contest has not begun
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'anonymous': {
                # False because contest has not begun
                'can_see_own_scoreboard': self.assertFalse,
                'can_see_full_scoreboard': self.assertFalse,
                'is_live_joinable_by': self.assertFalse,
                'is_spectatable_by': self.assertFalse,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.future_organization_private_contest, data)

    def test_private_user_contest_methods(self):
        data = {
            'superuser': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
                'is_in_contest': self.assertFalse,
            },
            'normal': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'non_staff_tester': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'anonymous': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.private_user_contest, data)

    def test_non_visible_contest_contest_methods(self):
        data = {
            'superuser': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
                'is_in_contest': self.assertFalse,
            },
            'normal': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            # not set as tester, in case something silly is happening
            'non_staff_tester': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'anonymous': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.non_visible_contest, data)

    def test_non_visible_contest_with_tester_contest_methods(self):
        data = {
            'superuser': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
                'is_in_contest': self.assertFalse,
            },
            'normal': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'non_staff_tester': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
            'anonymous': {
                'can_see_own_scoreboard': self.assertTrue,
                'can_see_full_scoreboard': self.assertTrue,
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_in_contest': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.non_visible_contest_with_tester, data)

    def test_contests_list(self):
        for name, user in self.users.items():
            with self.subTest(user=name):
                # We only care about consistency between Contest.is_accessible_by and Contest.get_visible_contests
                contest_keys = []
                for contest in Contest.objects.prefetch_related('testers', 'private_contestants', 'organizations'):
                    if contest.is_accessible_by(user):
                        contest_keys.append(contest.key)

                self.assertCountEqual(
                    Contest.get_visible_contests(user).values_list('key', flat=True),
                    contest_keys,
                )

    def test_contest_clean(self):
        _now = timezone.now()
        contest = create_contest(
            key='contest',
            start_time=_now,
            end_time=_now - timezone.timedelta(days=1),
            problem_label_script='invalid',
            format_config={'invalid': 'invalid'},
        )
        with self.assertRaisesRegex(ValidationError, 'ended before it starts'):
            contest.full_clean()
        contest.end_time = _now
        with self.assertRaisesRegex(ValidationError, 'ended before it starts'):
            contest.full_clean()
        contest.end_time = _now + timezone.timedelta(days=1)
        with self.assertRaisesRegex(ValidationError, 'does not use extra configuration'):
            contest.full_clean()
        contest.format_config = {}
        with self.assertRaisesRegex(ValidationError, 'Contest problem label script'):
            contest.full_clean()
        contest.problem_label_script = """
            function(n)
                return n
            end
        """
        # Test for bad problem label script caching
        with self.assertRaisesRegex(ValidationError, 'Contest problem label script'):
            contest.full_clean()
        del contest.get_label_for_problem
        with self.assertRaisesRegex(ValidationError, 'should return a string'):
            contest.full_clean()
        contest.problem_label_script = ''
        del contest.get_label_for_problem
        contest.full_clean()

    def test_normal_user_current_contest(self):
        current_contest = self.users['normal'].profile.current_contest
        self.assertIsNotNone(current_contest)

        current_contest.set_disqualified(True)
        self.users['normal'].profile.refresh_from_db()
        self.assertTrue(current_contest.is_disqualified)
        self.assertIsNone(self.users['normal'].profile.current_contest)
        self.assertEqual(current_contest.score, -9999)

        current_contest.set_disqualified(False)
        self.users['normal'].profile.refresh_from_db()
        self.assertFalse(current_contest.is_disqualified)
        self.assertIsNone(self.users['normal'].profile.current_contest)
        self.assertEqual(current_contest.score, 0)

    def test_live_participation(self):
        participation = ContestParticipation.objects.get(
            contest=self.hidden_scoreboard_contest,
            user=self.users['normal'].profile,
            virtual=ContestParticipation.LIVE,
        )
        self.assertTrue(participation.live)
        self.assertFalse(participation.spectate)
        self.assertEqual(participation.end_time, participation.contest.end_time)
        self.assertFalse(participation.ended)
        self.assertIsInstance(participation.time_remaining, timezone.timedelta)

    def test_spectating_participation(self):
        participation = create_contest_participation(
            contest='hidden_scoreboard',
            user='superuser',
            virtual=ContestParticipation.SPECTATE,
        )

        self.assertFalse(participation.live)
        self.assertTrue(participation.spectate)
        self.assertEqual(participation.start, participation.contest.start_time)
        self.assertEqual(participation.end_time, participation.contest.end_time)

    def test_virtual_participation(self):
        participation = create_contest_participation(
            contest='private',
            user='superuser',
            virtual=1,
        )

        self.assertFalse(participation.live)
        self.assertFalse(participation.spectate)
        self.assertEqual(participation.start, participation.real_start)
        self.assertIsInstance(participation.end_time, timezone.datetime)

    def test_generate_unique_key_skips_occupied_c_series(self):
        first = Contest.generate_unique_key()
        self.assertRegex(first, r'^c\d{6}$')
        create_contest(
            key=first,
            start_time=timezone.now() - timezone.timedelta(days=1),
            end_time=timezone.now() + timezone.timedelta(days=1),
            authors=('superuser',),
        )
        second = Contest.generate_unique_key()
        self.assertRegex(second, r'^c\d{6}$')
        self.assertNotEqual(second, first)

    @patch('judge.models.contest.Contest.objects.values_list')
    def test_generate_unique_key_uses_one_existing_key_query(self, values_list):
        values_list.return_value = ['c000000', 'c000002']
        self.assertEqual(Contest.generate_unique_key(), 'c000001')
        values_list.assert_called_once_with('key', flat=True)


class ContestTagTestCase(TestCase):
    @classmethod
    def setUpTestData(self):
        self.basic_tag = ContestTag.objects.create(
            name='basic',
            color='#fff',
        )
        self.dark_tag = ContestTag.objects.create(
            name='dark',
            color='#010001',
        )

    def test_basic_tag(self):
        self.assertEqual(str(self.basic_tag), self.basic_tag.name)
        self.assertEqual(self.basic_tag.text_color, '#000')

    def test_dark_tag(self):
        self.assertEqual(self.dark_tag.text_color, '#fff')


class MinValueOrNoneValidatorTestCase(SimpleTestCase):
    def test_both_integers(self):
        self.assertIsNone(MinValueOrNoneValidator(-1)(100))
        self.assertIsNone(MinValueOrNoneValidator(0)(0))
        self.assertIsNone(MinValueOrNoneValidator(100)(100))

    def test_integer_bound_none_value(self):
        self.assertIsNone(MinValueOrNoneValidator(-100)(None))
        self.assertIsNone(MinValueOrNoneValidator(0)(None))
        self.assertIsNone(MinValueOrNoneValidator(100)(None))

    def test_none_bound_integer_value(self):
        self.assertIsNone(MinValueOrNoneValidator(None)(-100))
        self.assertIsNone(MinValueOrNoneValidator(None)(0))
        self.assertIsNone(MinValueOrNoneValidator(None)(100))

    def test_both_none(self):
        self.assertIsNone(MinValueOrNoneValidator(None)(None))

    def test_fail(self):
        with self.assertRaises(ValidationError):
            MinValueOrNoneValidator(0)(-1)

        with self.assertRaises(ValidationError):
            MinValueOrNoneValidator(100)(0)


class ContestSEBTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.users.update({
            'seb_editor': create_user(username='seb_editor'),
            'seb_tester': create_user(username='seb_tester'),
            'seb_spectator': create_user(username='seb_spectator'),
            'seb_edit_all': create_user(
                username='seb_edit_all',
                user_permissions=('edit_all_contest',),
            ),
        })

        self.seb_contest = create_contest(
            key='seb',
            seb_required=True,
            seb_browser_exam_keys='a' * 64,
            seb_config_keys='b' * 64,
            authors=('seb_editor',),
            testers=('seb_tester',),
            spectators=('seb_spectator',),
        )

        self.seb_org_contest = create_contest(
            key='seborg',
            seb_required=True,
            seb_browser_exam_keys='a' * 64,
            seb_config_keys='b' * 64,
            is_organization_private=True,
            organizations=('open',),
        )

    def test_parse_seb_keys(self):
        self.assertEqual(Contest._parse_seb_keys(''), [])
        self.assertEqual(Contest._parse_seb_keys('\n  \n'), [])
        self.assertEqual(
            Contest._parse_seb_keys('%s  # Windows 3.7\n\n%s comment\n' % ('A' * 64, 'b' * 64)),
            ['a' * 64, 'b' * 64],
        )

    def test_seb_allowed_keys(self):
        self.assertEqual(self.seb_contest.seb_allowed_bek(), ['a' * 64])
        self.assertEqual(self.seb_contest.seb_allowed_config_keys(), ['b' * 64])

    def test_user_bypasses_seb(self):
        contest = self.seb_contest
        self.assertFalse(contest.user_bypasses_seb(self.users['anonymous']))
        self.assertFalse(contest.user_bypasses_seb(self.users['normal']))
        self.assertTrue(contest.user_bypasses_seb(self.users['seb_editor']))
        self.assertTrue(contest.user_bypasses_seb(self.users['seb_tester']))
        self.assertTrue(contest.user_bypasses_seb(self.users['seb_spectator']))
        self.assertTrue(contest.user_bypasses_seb(self.users['seb_edit_all']))
        self.assertTrue(contest.user_bypasses_seb(self.users['superuser']))

    def test_org_admin_bypasses_seb(self):
        self.assertTrue(self.seb_org_contest.user_bypasses_seb(self.users['staff_organization_admin']))
        self.assertFalse(self.seb_org_contest.user_bypasses_seb(self.users['normal']))
        # admin of an unrelated organization does not bypass
        self.assertFalse(self.seb_contest.user_bypasses_seb(self.users['staff_organization_admin']))

    def test_seb_clean_requires_keys(self):
        contest = create_contest(key='sebclean')
        contest.seb_required = True
        contest.seb_browser_exam_keys = ''
        contest.seb_config_keys = ''
        with self.assertRaisesRegex(ValidationError, 'Browser Exam Key hash is required'):
            contest.full_clean()
        contest.seb_browser_exam_keys = 'c' * 64
        with self.assertRaisesRegex(ValidationError, 'Config Key hash is required'):
            contest.full_clean()
        contest.seb_config_keys = 'd' * 64
        contest.full_clean()

    def test_seb_clean_rejects_malformed_keys(self):
        contest = create_contest(key='sebcleanhex')
        contest.seb_required = True
        contest.seb_browser_exam_keys = 'not-a-hash'
        contest.seb_config_keys = 'd' * 64
        with self.assertRaisesRegex(ValidationError, 'not a 64-character hex hash'):
            contest.full_clean()
        contest.seb_browser_exam_keys = 'c' * 64
        contest.seb_config_keys = 'd' * 63  # too short
        with self.assertRaisesRegex(ValidationError, 'not a 64-character hex hash'):
            contest.full_clean()
        contest.seb_config_keys = '%s  # trailing comments are fine' % ('D' * 64)
        contest.full_clean()

    def test_seb_clean_not_required_when_disabled(self):
        contest = create_contest(key='sebcleanoff')
        contest.seb_required = False
        contest.seb_browser_exam_keys = ''
        contest.seb_config_keys = ''
        contest.full_clean()


class ContestProblemDisplayNameTestCase(CommonDataMixin, TestCase):
    def test_override_wins_over_problem_name(self):
        problem = create_problem('disp-helper', name='Library Name')
        contest = create_contest(key='disphelper')
        cp = create_contest_problem(contest=contest, problem=problem, display_name='Assignment Name')
        self.assertEqual(contest_problem_display_name(cp), 'Assignment Name')
        self.assertEqual(problem.name, 'Library Name')

    def test_empty_override_falls_back_to_problem_name(self):
        problem = create_problem('disp-empty', name='Original Title')
        contest = create_contest(key='dispemptycp')
        cp = create_contest_problem(contest=contest, problem=problem, display_name='')
        self.assertEqual(contest_problem_display_name(cp), 'Original Title')
        self.assertEqual(contest_problem_display_name(cp, 'en'), 'Original Title')
