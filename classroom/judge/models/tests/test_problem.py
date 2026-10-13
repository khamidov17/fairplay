from unittest import mock

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from judge.models import Language, LanguageLimit, Problem, ProblemData, ProblemType, Submission
from judge.models.problem import VotePermission, disallowed_characters_validator
from judge.models.tests.util import CommonDataMixin, create_contest, create_contest_participation, \
    create_organization, create_problem, create_problem_type, create_solution, create_user
from judge.utils.problem_create import apply_simple_problem_defaults


class ProblemTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()

        self.users.update({
            'staff_problem_edit_only_all': create_user(
                username='staff_problem_edit_only_all',
                is_staff=True,
                user_permissions=('edit_all_problem',),
            ),
        })

        create_problem_type(name='type')

        self.basic_problem = create_problem(
            code='basic',
            allowed_languages=Language.objects.values_list('key', flat=True),
            types=('type',),
            authors=('normal',),
            testers=('staff_problem_edit_public',),
        )

        limits = []
        for lang in Language.objects.filter(common_name=Language.get_python3().common_name):
            limits.append(
                LanguageLimit(
                    problem=self.basic_problem,
                    language=lang,
                    time_limit=100,
                    memory_limit=131072,
                ),
            )
        LanguageLimit.objects.bulk_create(limits)

        self.organization_private_problem = create_problem(
            code='organization_private',
            time_limit=2,
            is_public=True,
            is_organization_private=True,
            curators=('staff_problem_edit_own', 'staff_problem_edit_own_no_staff'),
        )

        self.problem_organization = create_organization(
            name='problem organization',
            admins=('normal', 'staff_problem_edit_public'),
        )
        self.organization_admin_private_problem = create_problem(
            code='org_admin_private',
            is_organization_private=True,
            organizations=('problem organization',),
        )
        self.organization_admin_problem = create_problem(
            code='organization_admin',
            organizations=('problem organization',),
        )

    def test_basic_problem(self):
        self.assertEqual(str(self.basic_problem), self.basic_problem.name)
        self.assertCountEqual(
            self.basic_problem.languages_list(),
            set(Language.objects.values_list('common_name', flat=True)),
        )
        self.basic_problem.user_count = -1000
        self.basic_problem.ac_rate = -1000
        self.basic_problem.update_stats()
        self.assertEqual(self.basic_problem.user_count, 0)
        self.assertEqual(self.basic_problem.ac_rate, 0)

        self.assertListEqual(list(self.basic_problem.author_ids), [self.users['normal'].profile.id])
        self.assertListEqual(list(self.basic_problem.editor_ids), [self.users['normal'].profile.id])
        self.assertListEqual(list(self.basic_problem.tester_ids), [self.users['staff_problem_edit_public'].profile.id])
        self.assertListEqual(list(self.basic_problem.usable_languages), [])
        self.assertListEqual(self.basic_problem.types_list, ['type'])
        self.assertSetEqual(self.basic_problem.usable_common_names, set())

        self.assertEqual(self.basic_problem.translated_name('ABCDEFGHIJK'), self.basic_problem.name)

        self.assertFalse(self.basic_problem.clarifications.exists())

    def test_new_problem_does_not_rename_data_root_on_later_save(self):
        problem = Problem(name='New code problem', description='Statement', points=1)
        apply_simple_problem_defaults(problem)

        with mock.patch.object(ProblemData, '_update_code') as update_code:
            problem.save()
            ProblemData.objects.create(problem=problem)
            problem.save()

        update_code.assert_not_called()

    def test_basic_problem_language_limits(self):
        for common_name, memory_limit in self.basic_problem.language_memory_limit:
            self.assertEqual(memory_limit, 131072)
        for common_name, time_limit in self.basic_problem.language_time_limit:
            self.assertEqual(time_limit, 100)

    def test_basic_problem_methods(self):
        self.assertTrue(self.basic_problem.is_editor(self.users['normal'].profile))

        data = {
            'superuser': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_edit_own': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'staff_problem_see_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
            },
            'staff_problem_edit_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_edit_public': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
            },
            'staff_problem_see_organization': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'normal': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
            },
            'anonymous': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.basic_problem, data)

    def test_organization_private_problem_methods(self):
        self.assertFalse(self.organization_private_problem.is_accessible_by(self.users['normal']))
        self.users['normal'].profile.organizations.add(self.organizations['open'])
        self.assertFalse(self.organization_private_problem.is_accessible_by(self.users['normal']))
        self.organization_private_problem.organizations.add(self.organizations['open'])

        data = {
            'staff_problem_edit_own': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
                'is_subs_manageable_by': self.assertTrue,
            },
            'staff_problem_see_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_subs_manageable_by': self.assertFalse,
            },
            'staff_problem_edit_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_edit_public': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_see_organization': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
            },
            'staff_problem_edit_all_with_rejudge': {
                'is_editable_by': self.assertTrue,
                'is_subs_manageable_by': self.assertTrue,
            },
            'staff_problem_edit_own_no_staff': {
                'is_editable_by': self.assertTrue,
                'is_subs_manageable_by': self.assertFalse,
            },
            'normal': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
            },
            'anonymous': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.organization_private_problem, data)

    def test_organization_admin_private_problem_methods(self):
        data = {
            'staff_problem_edit_own': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_subs_manageable_by': self.assertFalse,
            },
            'staff_problem_see_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_subs_manageable_by': self.assertFalse,
            },
            'staff_problem_edit_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_edit_public': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,  # problem is not public, so edit_public_problem doesn't apply
            },
            'staff_problem_see_organization': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'staff_organization_admin': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'normal': {
                'is_accessible_by': self.assertTrue,  # org admin can view org-private problems
                'is_editable_by': self.assertFalse,
            },
            'anonymous': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.organization_admin_private_problem, data)

    def test_organization_admin_problem_methods(self):
        data = {
            'staff_problem_edit_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_edit_public': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'staff_organization_admin': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'normal': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'anonymous': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.organization_admin_problem, data)

    def give_basic_problem_ac(self, user, points=None):
        Submission.objects.create(
            user=user.profile,
            problem=self.basic_problem,
            result='AC',
            points=self.basic_problem.points if points is None else points,
            language=Language.get_python3(),
        )

    def test_problem_voting_permissions(self):
        self.assertEqual(self.basic_problem.vote_permission_for_user(self.users['anonymous']), VotePermission.NONE)

        now = timezone.now()
        basic_contest = create_contest(
            key='basic',
            start_time=now - timezone.timedelta(days=1),
            end_time=now + timezone.timedelta(days=100),
            authors=('superuser', 'staff_contest_edit_own'),
            testers=('non_staff_tester',),
        )
        in_contest = create_user(username='in_contest')
        in_contest.profile.current_contest = create_contest_participation(
            user=in_contest,
            contest=basic_contest,
        )
        self.give_basic_problem_ac(in_contest)
        self.assertEqual(self.basic_problem.vote_permission_for_user(in_contest), VotePermission.NONE)

        unlisted = create_user(username='unlisted')
        unlisted.profile.is_unlisted = True
        self.give_basic_problem_ac(unlisted)
        self.assertEqual(self.basic_problem.vote_permission_for_user(unlisted), VotePermission.VIEW)

        banned_from_voting = create_user(username='banned_from_voting')
        banned_from_voting.profile.is_banned_from_problem_voting = True
        self.give_basic_problem_ac(banned_from_voting)
        self.assertEqual(self.basic_problem.vote_permission_for_user(banned_from_voting), VotePermission.VIEW)

        banned_from_problem = create_user(username='banned_from_problem')
        self.basic_problem.banned_users.add(banned_from_problem.profile)
        self.give_basic_problem_ac(banned_from_problem)
        self.assertEqual(self.basic_problem.vote_permission_for_user(banned_from_problem), VotePermission.VIEW)

        self.assertEqual(self.basic_problem.vote_permission_for_user(self.users['normal']), VotePermission.VIEW)

        self.give_basic_problem_ac(self.users['normal'])
        self.assertEqual(self.basic_problem.vote_permission_for_user(self.users['normal']), VotePermission.VOTE)

        partial_ac = create_user(username='partial_ac')
        self.give_basic_problem_ac(partial_ac, 0.5)  # ensure this value is not equal to its point value
        self.assertNotEqual(self.basic_problem.points, 0.5)
        self.assertEqual(self.basic_problem.vote_permission_for_user(partial_ac), VotePermission.VIEW)

    def test_problems_list(self):
        for name, user in self.users.items():
            with self.subTest(user=name):
                with self.subTest(list='accessible problems'):
                    # We only care about consistency between Problem.is_accessible_by and Problem.get_visible_problems
                    problem_codes = []
                    for problem in Problem.objects.prefetch_related('authors', 'curators', 'testers', 'organizations'):
                        if problem.is_accessible_by(user):
                            problem_codes.append(problem.code)

                    self.assertCountEqual(
                        Problem.get_visible_problems(user).distinct().values_list('code', flat=True),
                        problem_codes,
                    )

                with self.subTest(list='editable problems'):
                    # We only care about consistency between Problem.is_editable_by and Problem.get_editable_problems
                    problem_codes = []
                    for problem in Problem.objects.prefetch_related('authors', 'curators'):
                        if problem.is_editable_by(user):
                            problem_codes.append(problem.code)

                    self.assertCountEqual(
                        Problem.get_editable_problems(user).distinct().values_list('code', flat=True),
                        problem_codes,
                    )


class SolutionTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.users.update({
            'staff_solution_see_all': create_user(
                username='staff_solution_see_all',
                user_permissions=('see_private_solution',),
            ),
        })

        now = timezone.now()

        self.basic_solution = create_solution(problem='basic')

        self.private_solution = create_solution(
            problem='private',
            is_public=False,
            publish_on=now - timezone.timedelta(days=100),
        )

        self.unpublished_problem = create_problem(
            code='unpublished',
            name='Unpublished',
            authors=('staff_problem_edit_own',),
        )
        self.unpublished_solution = create_solution(
            problem=self.unpublished_problem,
            is_public=False,
            publish_on=now + timezone.timedelta(days=100),
            authors=('normal',),
        )

    def test_unpublished_solution(self):
        self.assertEqual(str(self.unpublished_solution), 'Editorial for Unpublished')

    def test_basic_solution_methods(self):
        data = {
            'superuser': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_solution_see_all': {
                'is_accessible_by': self.assertTrue,
            },
            'normal': {
                'is_accessible_by': self.assertTrue,
            },
            'anonymous': {
                'is_accessible_by': self.assertTrue,
            },
        }
        self._test_object_methods_with_users(self.basic_solution, data)

    def test_private_solution_methods(self):
        data = {
            'superuser': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_solution_see_all': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_own': {
                'is_accessible_by': self.assertFalse,
            },
            'staff_problem_see_all': {
                'is_accessible_by': self.assertFalse,
            },
            'staff_problem_edit_all': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_public': {
                'is_accessible_by': self.assertFalse,
            },
            'normal': {
                'is_accessible_by': self.assertFalse,
            },
            'anonymous': {
                'is_accessible_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.private_solution, data)

    def test_unpublished_solution_methods(self):
        data = {
            'staff_solution_see_all': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_own': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_all': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_public': {
                'is_accessible_by': self.assertFalse,
            },
            'normal': {
                'is_accessible_by': self.assertFalse,
            },
            'anonymous': {
                'is_accessible_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.unpublished_solution, data)


class ProblemDifficultyPropertiesTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        create_problem_type(name='easy', full_name='Easy')
        create_problem_type(name='medium', full_name='Medium')
        create_problem_type(name='hard', full_name='Hard')
        create_problem_type(name='code', full_name='Code')
        create_problem_type(name='fill_in_blank', full_name='Fill in Blank')
        create_problem_type(name='dp', full_name='Dynamic Programming')
        create_problem_type(name='greedy', full_name='Greedy')
        # Legacy auto chips persisted with non-canonical names (still must not show in Tags).
        create_problem_type(name='programming', full_name='Programming')
        create_problem_type(name='fill-in-blank', full_name='Fill-in-blank')

        cls.easy_problem = create_problem(
            code='diff-easy',
            is_public=True,
            types=('easy', 'code', 'dp'),
        )
        cls.polluted_tags = create_problem(
            code='diff-polluted',
            is_public=True,
            types=('easy', 'programming', 'fill-in-blank', 'dp', 'greedy'),
        )
        cls.hard_problem = create_problem(
            code='diff-hard',
            is_public=True,
            types=('hard', 'code', 'dp'),
        )
        cls.untagged_difficulty = create_problem(
            code='diff-none',
            is_public=True,
            types=('code', 'greedy'),
        )
        cls.multi_difficulty = create_problem(
            code='diff-multi',
            is_public=True,
            types=('easy', 'hard', 'medium', 'code'),
        )

    def test_difficulty_returns_first_matching_level_in_order(self):
        self.assertEqual(self.easy_problem.difficulty, 'easy')
        self.assertEqual(self.hard_problem.difficulty, 'hard')
        self.assertIsNone(self.untagged_difficulty.difficulty)
        self.assertEqual(self.multi_difficulty.difficulty, 'easy')

    def test_difficulty_label_uses_canonical_gettext_msgid(self):
        # Labels come from DIFFICULTY_LABELS (Easy/Medium/Hard), not DB full_name.
        self.assertEqual(self.easy_problem.difficulty_label, 'Easy')
        self.assertEqual(self.hard_problem.difficulty_label, 'Hard')
        self.assertIsNone(self.untagged_difficulty.difficulty_label)
        self.assertEqual(Problem.difficulty_display('medium'), 'Medium')
        self.assertIsNone(Problem.difficulty_display(None))

    def test_format_types_list_excludes_difficulty_and_knowledge(self):
        self.assertEqual(self.easy_problem.format_types_list, ['Programming'])

    def test_knowledge_types_list_excludes_format_and_difficulty(self):
        self.assertEqual(self.easy_problem.knowledge_types_list, ['Dynamic Programming'])
        self.assertEqual(self.untagged_difficulty.knowledge_types_list, ['Greedy'])

    def test_knowledge_types_list_excludes_auto_chips_and_dedupes(self):
        self.assertEqual(
            self.polluted_tags.knowledge_types_list,
            ['Dynamic Programming', 'Greedy'],
        )

    def test_is_knowledge_type_name_excludes_format_aliases(self):
        self.assertTrue(Problem.is_knowledge_type_name('dp'))
        self.assertFalse(Problem.is_knowledge_type_name('code'))
        self.assertFalse(Problem.is_knowledge_type_name('programming'))
        self.assertFalse(Problem.is_knowledge_type_name('编程题'))
        self.assertFalse(Problem.is_knowledge_type_name('single-choice'))
        self.assertFalse(Problem.is_knowledge_type_name('easy'))
        self.assertFalse(Problem.is_knowledge_type_name('hard'))


class CapitalizedDifficultyTypeTestCase(CommonDataMixin, TestCase):
    """MySQL treats Easy and easy as the same unique name; this suite only seeds capitalized IDs."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        create_problem_type(name='Easy', full_name='Easy')
        create_problem_type(name='Medium', full_name='Medium')
        create_problem_type(name='Hard', full_name='Hard')
        create_problem_type(name='dp', full_name='Dynamic Programming')
        cls.easy_problem = create_problem(
            code='diff-caps-easy',
            is_public=True,
            types=('Easy', 'dp'),
        )
        cls.hard_problem = create_problem(
            code='diff-caps-hard',
            is_public=True,
            types=('Hard', 'dp'),
        )

    def test_difficulty_recognizes_capitalized_category_ids(self):
        self.assertEqual(self.easy_problem.difficulty, 'easy')
        self.assertEqual(self.easy_problem.difficulty_label, 'Easy')
        self.assertEqual(self.hard_problem.difficulty, 'hard')
        self.assertEqual(self.hard_problem.difficulty_label, 'Hard')
        self.assertEqual(self.easy_problem.knowledge_types_list, ['Dynamic Programming'])
        self.assertTrue(Problem.is_difficulty_problem_type(ProblemType.objects.get(name='Easy')))
        self.assertTrue(Problem.is_difficulty_problem_type(ProblemType.objects.get(name='Hard')))
        self.assertEqual(Problem.canonical_difficulty_name('Easy'), 'easy')
        self.assertEqual(Problem.canonical_difficulty_name('Hard'), 'hard')
        self.assertEqual(Problem.canonical_difficulty_name('difficult'), 'hard')
        self.assertEqual(Problem.canonical_difficulty_name('困难'), 'hard')


class DisallowedCharactersValidatorTestCase(SimpleTestCase):
    def test_valid(self):
        with self.settings(DMOJ_PROBLEM_STATEMENT_DISALLOWED_CHARACTERS={'“', '”', '‘', '’'}):
            self.assertIsNone(disallowed_characters_validator(''))
            self.assertIsNone(disallowed_characters_validator('"\'string\''))

        with self.settings(DMOJ_PROBLEM_STATEMENT_DISALLOWED_CHARACTERS=set()):
            self.assertIsNone(disallowed_characters_validator(''))
            self.assertIsNone(disallowed_characters_validator('“”‘’'))

    def test_invalid(self):
        with self.settings(DMOJ_PROBLEM_STATEMENT_DISALLOWED_CHARACTERS={'“', '”', '‘', '’'}):
            with self.assertRaises(ValidationError, msg='Disallowed characters: “'):
                disallowed_characters_validator('“')
            with self.assertRaisesRegex(ValidationError, 'Disallowed characters: (?=.*‘)(?=.*’)'):
                disallowed_characters_validator('‘’')
