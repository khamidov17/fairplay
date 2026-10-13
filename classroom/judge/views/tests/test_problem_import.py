import json
from unittest import mock

from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from judge.llm.base import LLMResult
from judge.models import Language, Organization, Problem, ProblemImportDraft, ProblemImportImage, \
    ProblemShortAnswerConfig, Solution
from judge.models.tests.util import CommonDataMixin, create_problem_group, create_problem_type, create_user
from judge.utils.simple_xlsx import build_xlsx_workbook


class ProblemImportPreviewTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        create_problem_group(name='import-group')
        create_problem_type(name='import-type')
        cls.creator = create_user(
            username='import_creator',
            is_staff=True,
            user_permissions=('add_problem', 'create_private_problem'),
        )

    def setUp(self):
        self.client.force_login(self.creator)

    def upload(self, rows):
        workbook = build_xlsx_workbook([('Problems', rows)])
        return self.client.post(reverse('problem_import_upload'), {
            'excel_file': SimpleUploadedFile(
                'questions.xlsx', workbook,
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            ),
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_upload_creates_preview_draft_from_question_type_column(self):
        response = self.upload([
            [
                'name*', 'problem_type*', 'description*', 'answer*',
                'tag（less than 20 characters, spaced with commas)',
                'difficulty\n(easy:0; medium:1; difficult:2)', 'explanation',
            ],
            [
                'Prime check', 'multiple_choice', 'Which is prime?\nA. 4\nB. 5', 'B',
                'number theory, primes', '2', 'Prime is only divisible by 1 and itself.',
            ],
            [
                'Greeting', 'fill_blank', 'Complete: Hello ____', 'world', 'vocabulary', '0',
                'Use the familiar greeting.',
            ],
        ])
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        self.assertEqual(draft.items.count(), 2)
        self.assertEqual(draft.items.get(name='Prime check').submission_type, 'M')
        self.assertEqual(draft.items.get(name='Greeting').submission_type, 'B')
        self.assertEqual(draft.items.get(name='Prime check').tags, 'number theory, primes')
        self.assertEqual(draft.items.get(name='Prime check').difficulty, '2')
        self.assertEqual(draft.items.get(name='Prime check').description, 'Which is prime?')
        self.assertEqual(draft.items.get(name='Prime check').options, '4\n5')
        self.assertEqual(draft.items.get(name='Prime check').answer, 'B')
        self.assertEqual(draft.items.get(name='Prime check').correct_options, '2')
        self.assertEqual(draft.items.get(name='Greeting').solution, 'Use the familiar greeting.')
        self.assertEqual(self.client.get(payload['preview_url']).status_code, 200)

    def test_upload_matches_organization_slug_and_applies_it_on_create(self):
        organization = Organization.objects.create(
            name='CS 201', slug='cs201', short_name='CS201', about='',
        )
        # Instructors may attach problems only to courses they teach (AUTH-10).
        organization.admins.add(self.creator.profile)
        response = self.upload([
            ['name', 'question_type', 'description', 'organizations', 'options', 'answer'],
            ['Organization scope', 'multiple_choice', 'Choose the prime.', 'cs201', '4\n5', '2'],
        ])

        self.assertEqual(response.status_code, 200)
        item = ProblemImportDraft.objects.get(owner=self.creator.profile).items.get()
        self.assertFalse(item.config_needs_review)
        self.assertEqual(item.editor_data, {'organizations': [organization.pk]})

        response = self.client.post(
            reverse('problem_import_item_action', args=(item.draft_id, item.pk, 'create')),
            {
                'name': item.name,
                'question_type': item.submission_type,
                'description': item.description,
                'answer': item.answer,
                'options': item.options,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertSetEqual(
            set(Problem.objects.get(name='Organization scope').organizations.values_list('pk', flat=True)),
            {organization.pk},
        )

    def test_upload_matches_organization_without_classes_or_members(self):
        organization = Organization.objects.create(
            name='Empty Course', slug='empty-course', short_name='EMPTY', about='',
        )
        organization.admins.add(self.creator.profile)
        response = self.upload([
            ['name', 'question_type', 'description', 'organizations', 'options', 'answer'],
            ['Empty course scope', 'multiple_choice', 'Choose the prime.', 'empty-course', '4\n5', '2'],
        ])

        self.assertEqual(response.status_code, 200)
        item = ProblemImportDraft.objects.get(owner=self.creator.profile).items.get()
        self.assertFalse(item.config_needs_review)
        self.assertEqual(item.editor_data, {'organizations': [organization.pk]})

        response = self.client.post(
            reverse('problem_import_item_action', args=(item.draft_id, item.pk, 'create')),
            {
                'name': item.name,
                'question_type': item.submission_type,
                'description': item.description,
                'answer': item.answer,
                'options': item.options,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertSetEqual(
            set(Problem.objects.get(name='Empty course scope').organizations.values_list('pk', flat=True)),
            {organization.pk},
        )

    def test_upload_does_not_match_course_the_creator_does_not_teach(self):
        """AUTH-10: being a member (student) of a course, or not at all, is not enough to attach to it."""
        member_course = Organization.objects.create(
            name='CS 301', slug='cs301', short_name='CS301', about='',
        )
        self.creator.profile.organizations.add(member_course)
        Organization.objects.create(name='Other Course', slug='other-course', short_name='OTHER', about='')
        response = self.upload([
            ['name', 'question_type', 'description', 'organizations', 'options', 'answer'],
            ['Member scope', 'multiple_choice', 'Choose the prime.', 'cs301', '4\n5', '2'],
            ['Other scope', 'multiple_choice', 'Choose the prime.', 'other-course', '4\n5', '2'],
        ])

        self.assertEqual(response.status_code, 200)
        for item in ProblemImportDraft.objects.get(owner=self.creator.profile).items.all():
            self.assertTrue(item.config_needs_review)
            self.assertEqual(item.editor_data, {})

    def test_partial_organization_draft_keeps_imported_code_languages(self):
        organization = Organization.objects.create(
            name='CS 201', slug='cs201', short_name='CS201', about='',
        )
        # Instructors may attach problems only to courses they teach (AUTH-10).
        organization.admins.add(self.creator.profile)
        language = Language.objects.exclude(key='ANSWER').first()
        response = self.upload([
            ['name', 'question_type', 'description', 'organizations', 'allowed_languages'],
            ['Language scope', 'code', 'Print the input.', 'cs201', language.key],
        ])

        self.assertEqual(response.status_code, 200)
        item = ProblemImportDraft.objects.get(owner=self.creator.profile).items.get()
        self.assertEqual(item.editor_data, {'organizations': [organization.pk]})

        response = self.client.post(
            reverse('problem_import_item_action', args=(item.draft_id, item.pk, 'create')),
            {
                'name': item.name,
                'question_type': item.submission_type,
                'description': item.description,
            },
        )
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(name='Language scope')
        self.assertEqual(list(problem.allowed_languages.values_list('key', flat=True)), [language.key])

    @mock.patch('judge.views.problem_import.llm.chat')
    @mock.patch('judge.views.problem_import.llm.is_configured', return_value=True)
    def test_llm_formats_only_the_selected_multiple_choice_draft(self, _configured, chat):
        response = self.upload([
            ['name', 'question_type', 'description', 'answer'],
            ['Prime check', 'multiple_choice', 'Which is prime? A. 4 B. 5', 'B'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        chat.return_value = LLMResult(text=json.dumps({
            'options': [{'text': '4'}, {'text': '5'}],
            'option_block': 'A. 4 B. 5',
        }))
        response = self.client.post(reverse('problem_import_item_action', args=(draft.pk, item.pk, 'format')), {
            'name': item.name,
            'question_type': 'M',
            'description': item.description,
            'answer': item.answer,
            'options': '',
            'correct_options': '',
            'blanks': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        item.refresh_from_db()
        self.assertEqual(item.options, '4\n5')
        self.assertEqual(item.correct_options, '2')
        chat.assert_called_once()
        self.assertEqual(chat.call_args.kwargs['profile'], 'question_import')

    @mock.patch('judge.views.problem_import.llm.chat')
    @mock.patch('judge.views.problem_import.llm.is_configured', return_value=True)
    def test_llm_formats_multiple_choice_before_its_answer_is_valid(self, _configured, chat):
        self.upload([
            ['name', 'question_type', 'description', 'answer'],
            ['Needs answer review', 'multiple_choice', 'Which is prime? A. 4 B. 5', 'Z'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        chat.return_value = LLMResult(text=json.dumps({
            'options': [{'text': '4'}, {'text': '5'}],
            'option_block': 'A. 4 B. 5',
        }))

        response = self.client.post(
            reverse('problem_import_item_action', args=(draft.pk, item.pk, 'format')),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.options, '4\n5')
        self.assertEqual(item.correct_options, '')
        self.assertTrue(item.config_needs_review)

    def test_create_multiple_choice_from_reviewed_draft(self):
        self.upload([
            ['name', 'question_type', 'description'],
            ['Prime check', 'multiple_choice', 'Which number is prime?'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        response = self.client.post(reverse('problem_import_item_action', args=(draft.pk, item.pk, 'create')), {
            'name': item.name,
            'question_type': 'M',
            'description': item.description,
            'answer': '2',
            'solution': '5 is the only prime option.',
            'tags': 'number theory, primes',
            'difficulty': '2',
            'options': '4\n5',
            'correct_options': '2',
            'blanks': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        problem = Problem.objects.get(name='Prime check')
        self.assertEqual(problem.submission_type, 'M')
        self.assertEqual(problem.mcq_options.filter(is_correct=True).count(), 1)
        self.assertSetEqual(
            set(problem.types.values_list('name', flat=True)),
            {'import-type', 'number_theory', 'primes', 'hard'},
        )
        self.assertEqual(Solution.objects.get(problem=problem).content, '5 is the only prime option.')

    def test_create_writes_admin_log_entry(self):
        self.upload([
            ['name', 'question_type', 'description'],
            ['Logged import', 'multiple_choice', 'Which number is prime?'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        response = self.client.post(reverse('problem_import_item_action', args=(draft.pk, item.pk, 'create')), {
            'name': item.name,
            'question_type': 'M',
            'description': item.description,
            'answer': '2',
            'options': '4\n5',
            'correct_options': '2',
            'blanks': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        problem = Problem.objects.get(name='Logged import')
        logs = LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(Problem),
            object_id=str(problem.pk),
            user=self.creator,
        )
        self.assertEqual(logs.count(), 1)
        self.assertEqual(logs.get().action_flag, ADDITION)
        self.assertEqual(logs.get().object_repr, 'Logged import')
        self.assertEqual(json.loads(logs.get().change_message), [{'added': {}}])

    def test_can_manually_save_unrecognized_multiple_choice_options_and_answer(self):
        self.upload([
            ['name', 'question_type', 'description', 'answer'],
            ['Manual MCQ', 'multiple_choice', 'Choose the correct value.', ''],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        preview = self.client.get(reverse('problem_import_preview', args=(draft.pk,)))
        self.assertContains(preview, 'import-editor-frame', html=False)
        self.assertContains(preview, 'import-problem-row', html=False)
        self.assertContains(preview, 'fill="#DE2121"', html=False)
        self.assertContains(preview, 'create-all-problems', html=False)
        self.assertContains(preview, reverse('problem_import_item_editor', args=(draft.pk, item.pk)), html=False)
        self.assertContains(
            preview,
            'src="{}"'.format(reverse('problem_import_item_editor', args=(draft.pk, item.pk))),
            html=False,
        )
        self.assertContains(preview, 'href="?item={}"'.format(item.pk), html=False)

        editor = self.client.get(reverse('problem_import_item_editor', args=(draft.pk, item.pk)))
        self.assertContains(editor, 'Save draft', html=False)
        self.assertContains(editor, 'id="options"', html=False)

        response = self.client.post(reverse('problem_import_item_action', args=(draft.pk, item.pk, 'save')), {
            'name': item.name,
            'question_type': 'M',
            'description': item.description,
            'answer': 'B',
            'options': 'first value\nsecond value',
            'correct_options': '',
            'blanks': '',
        })

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.options, 'first value\nsecond value')
        self.assertEqual(item.answer, 'B')

    def test_unified_editor_saves_its_configuration_to_the_import_draft(self):
        self.upload([
            ['name', 'question_type', 'description', 'answer'],
            ['Editor MCQ', 'multiple_choice', 'Choose the correct value.', ''],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        payload = {
            'type': 'multiple',
            'name': 'Editor MCQ revised',
            'description': 'Choose every prime number.',
            'points': 8,
            'is_public': False,
            'partial': True,
            'organizations': [],
            'group_id': '',
            'difficulty': 'hard',
            'problemTypes': ['number theory'],
            'options': [
                {'text': '4', 'correct': False},
                {'text': '5', 'correct': True},
                {'text': '7', 'correct': True},
            ],
            'solution': {'content': 'Both 5 and 7 are prime.'},
        }

        response = self.client.post(
            reverse('problem_import_item_action', args=(draft.pk, item.pk, 'editor')),
            data=json.dumps(payload), content_type='application/json', HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.name, 'Editor MCQ revised')
        self.assertEqual(item.answer, '2|3')
        self.assertEqual(item.options, '4\n5\n7')
        self.assertEqual(item.editor_data['points'], 8)
        self.assertFalse(Problem.objects.filter(name='Editor MCQ revised').exists())

        response = self.client.post(
            reverse('problem_import_item_action', args=(draft.pk, item.pk, 'create')),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(name='Editor MCQ revised')
        self.assertEqual(problem.points, 8)
        self.assertTrue(problem.partial)
        self.assertEqual(Solution.objects.get(problem=problem).content, 'Both 5 and 7 are prime.')

    def test_unreadable_code_test_data_requires_explicit_confirmation(self):
        self.upload([
            ['name', 'question_type', 'description', 'test_data'],
            ['Broken data', 'code', 'Read and print a number.', 'this is not test-data syntax'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        self.assertTrue(item.data_needs_review)
        self.assertIn('Test data must start', item.data_load_error)

        payload = {
            'type': 'programming',
            'name': item.name,
            'description': item.description,
            'points': 10,
            'is_public': False,
            'partial': False,
            'organizations': [],
            'group_id': '',
            'difficulty': 'medium',
            'problemTypes': [],
            'solution': {'content': ''},
            'languages': [],
            'time_limit': 1,
            'memory_limit': 256,
            'checker': '',
            'testcases': [],
        }
        url = reverse('problem_import_item_action', args=(draft.pk, item.pk, 'editor'))
        response = self.client.post(
            url, data=json.dumps(payload), content_type='application/json', HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertTrue(item.data_needs_review)
        self.assertEqual(item.test_data, 'this is not test-data syntax')

        payload['confirm_data_load'] = '1'
        response = self.client.post(
            url, data=json.dumps(payload), content_type='application/json', HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertFalse(item.data_needs_review)
        self.assertEqual(item.test_data, '')

    def test_editor_records_question_type_confirmation_in_the_draft(self):
        self.upload([
            ['name', 'question_type', 'description'],
            ['Confirm type', 'teacher-defined type', 'Read and print a number.'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        self.assertTrue(item.type_needs_review)

        payload = {
            'type': 'programming',
            'name': item.name,
            'description': item.description,
            'points': 10,
            'is_public': False,
            'partial': False,
            'organizations': [],
            'group_id': '',
            'difficulty': 'medium',
            'problemTypes': [],
            'solution': {'content': ''},
            'languages': [],
            'time_limit': 1,
            'memory_limit': 256,
            'checker': '',
            'testcases': [],
            'confirm_question_type': '1',
        }
        response = self.client.post(
            reverse('problem_import_item_action', args=(draft.pk, item.pk, 'editor')),
            data=json.dumps(payload), content_type='application/json', HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertFalse(item.type_needs_review)

    def test_unrecognized_excel_configuration_requires_review_confirmation(self):
        self.upload([
            ['name', 'question_type', 'description', 'options', 'answer', 'legacy checker mode'],
            ['Review configuration', 'multiple_choice', 'Choose the prime.', '4\n5', '2', 'strict'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        self.assertTrue(item.config_needs_review)
        self.assertEqual(item.unrecognized_configurations, [{'column': 'legacy checker mode', 'value': 'strict'}])

        response = self.client.post(
            reverse('problem_import_item_action', args=(draft.pk, item.pk, 'create')),
            {
                'name': item.name,
                'question_type': 'M',
                'description': item.description,
                'answer': item.answer,
                'options': item.options,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('unrecognized imported configuration', response.json()['message'])

    def test_editor_keeps_configuration_confirmation_after_a_later_draft_save(self):
        self.upload([
            ['name', 'question_type', 'description', 'options', 'answer', 'legacy checker mode'],
            ['Review configuration', 'multiple_choice', 'Choose the prime.', '4\n5', '2', 'strict'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        payload = {
            'type': 'single',
            'name': item.name,
            'description': item.description,
            'points': 10,
            'is_public': False,
            'partial': False,
            'organizations': [],
            'group_id': '',
            'difficulty': 'medium',
            'problemTypes': [],
            'solution': {'content': ''},
            'options': [
                {'text': '4', 'correct': False},
                {'text': '5', 'correct': True},
            ],
            'confirm_unrecognized_configurations': '1',
        }
        url = reverse('problem_import_item_action', args=(draft.pk, item.pk, 'editor'))
        response = self.client.post(
            url, data=json.dumps(payload), content_type='application/json', HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertFalse(item.config_needs_review)
        self.assertEqual(item.config_review_signature, '')
        self.assertEqual(item.unrecognized_configurations, [])

        payload.pop('confirm_unrecognized_configurations')
        response = self.client.post(
            url, data=json.dumps(payload), content_type='application/json', HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertFalse(item.config_needs_review)
        self.assertEqual(item.unrecognized_configurations, [])

    def test_configuration_confirmation_requires_programmatic_issues_to_be_fixed(self):
        self.upload([
            ['name', 'question_type', 'description', 'tags', 'legacy checker mode'],
            [
                'Configuration review', 'multiple_choice', 'Choose the prime.',
                'one|two|three|four|five|six', 'strict',
            ],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        payload = {
            'type': 'single',
            'name': item.name,
            'description': item.description,
            'points': 10,
            'is_public': False,
            'partial': False,
            'organizations': [],
            'group_id': '',
            'difficulty': 'medium',
            'problemTypes': ['one', 'two', 'three', 'four', 'five', 'six'],
            'solution': {'content': ''},
            'options': [
                {'text': '4', 'correct': False},
                {'text': '5', 'correct': True},
            ],
            'confirm_unrecognized_configurations': '1',
        }
        response = self.client.post(
            reverse('problem_import_item_action', args=(draft.pk, item.pk, 'editor')),
            data=json.dumps(payload), content_type='application/json', HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertTrue(item.config_needs_review)
        self.assertEqual(item.unrecognized_configurations[0]['column'], 'legacy checker mode')
        self.assertEqual(item.unrecognized_configurations[1]['column'], 'Tags')

    def test_editor_normalizes_duplicate_tags_from_an_existing_draft(self):
        self.upload([
            ['name', 'question_type', 'description', 'tags'],
            ['Tag cleanup', 'multiple_choice', 'Choose the prime.', 'Programming|Hard|programming|hard'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        self.assertEqual(item.tags, 'Programming, Hard')

        payload = {
            'type': 'single',
            'name': item.name,
            'description': item.description,
            'points': 10,
            'is_public': False,
            'partial': False,
            'organizations': [],
            'group_id': '',
            'difficulty': 'medium',
            'problemTypes': ['Programming', 'Hard', 'Programming', 'Hard'],
            'solution': {'content': ''},
            'options': [
                {'text': '4', 'correct': False},
                {'text': '5', 'correct': True},
            ],
        }
        response = self.client.post(
            reverse('problem_import_item_action', args=(draft.pk, item.pk, 'editor')),
            data=json.dumps(payload), content_type='application/json', HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.tags, 'Programming, Hard')

    def test_invalid_recognized_configuration_is_shown_for_revision(self):
        self.upload([
            ['name', 'question_type', 'description', 'options', 'answer'],
            ['Invalid answer', 'multiple_choice', 'Choose the prime.', '4\n5', 'Z'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()

        self.assertTrue(item.config_needs_review)
        self.assertEqual(item.unrecognized_configurations[0]['column'], 'Correct answer')
        self.assertIn('does not match an option', item.unrecognized_configurations[0]['reason'])

        editor = self.client.get(reverse('problem_import_item_editor', args=(draft.pk, item.pk)))
        self.assertContains(editor, 'Correct answer', html=False)
        self.assertContains(editor, 'does not match an option', html=False)

    def test_imports_short_answer_with_its_grading_configuration(self):
        self.upload([
            ['name', 'question_type', 'description', 'answer', 'rubric', 'grading_table'],
            [
                'Explain recursion', 'short answer', 'Explain recursion in your own words.',
                'A function calls itself with a base case.', 'Mention a base case.',
                '[{"name": "Explanation", "max_points": 10, "strictness": "normal"}]',
            ],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()

        self.assertEqual(item.submission_type, 'S')
        response = self.client.post(reverse('problem_import_item_action', args=(draft.pk, item.pk, 'create')), {
            'name': item.name,
            'question_type': 'S',
            'description': item.description,
            'answer': item.answer,
            'rubric': item.rubric,
            'grading_table': item.grading_table,
            'options': '',
            'correct_options': '',
            'blanks': '',
        })

        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(name='Explain recursion')
        config = ProblemShortAnswerConfig.objects.get(problem=problem)
        self.assertEqual(config.reference_answer, 'A function calls itself with a base case.')
        self.assertEqual(config.ai_rubric, 'Mention a base case.')
        self.assertEqual(config.grading_table[0]['max_points'], 10)

    def test_imports_true_false_as_a_true_false_multiple_choice_question(self):
        self.upload([
            ['name', 'question_type', 'description', 'answer'],
            ['Binary statement', 'true/false', 'Binary search needs sorted data.', 'true'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()

        self.assertTrue(item.is_true_false)
        self.assertEqual(item.options, 'True\nFalse')
        self.assertEqual(item.answer, '1')
        response = self.client.post(reverse('problem_import_item_action', args=(draft.pk, item.pk, 'create')), {
            'name': item.name,
            'question_type': 'M',
            'description': item.description,
            'answer': item.answer,
            'options': item.options,
            'correct_options': '',
            'blanks': '',
        })

        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(name='Binary statement')
        self.assertTrue(problem.mcq_config.is_true_false)

    def test_single_create_rejects_an_unconfirmed_question_type(self):
        self.upload([
            ['name', 'question_type', 'description'],
            ['Review type', 'Written response', 'Confirm the imported type before creating.'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()

        response = self.client.post(reverse('problem_import_item_action', args=(draft.pk, item.pk, 'create')), {
            'name': item.name,
            'question_type': item.submission_type,
            'description': item.description,
            'answer': '',
            'options': '',
            'correct_options': '',
            'blanks': '',
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn('Select or confirm', response.json()['message'])
        self.assertFalse(Problem.objects.filter(name='Review type').exists())

    @mock.patch('judge.views.problem_import.store_docx_image')
    def test_docx_figure_is_private_in_preview_then_promoted_on_create(self, store_docx_image):
        self.upload([
            ['name', 'question_type', 'description'],
            ['Figure question', 'multiple_choice', 'Choose the figure.'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        image = ProblemImportImage.objects.create(item=item, filename='figure.png', content=b'png-data')
        image_url = reverse('problem_import_image', args=(draft.pk, image.token))
        item.description = 'Choose the figure.\n\n![]({})'.format(image_url)
        item.save(update_fields=['description'])

        preview = self.client.get(image_url)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.content, b'png-data')

        store_docx_image.return_value = ('martor/final.png', '/media/martor/final.png')
        response = self.client.post(reverse('problem_import_item_action', args=(draft.pk, item.pk, 'create')), {
            'name': item.name,
            'question_type': 'M',
            'description': item.description,
            'answer': 'A',
            'options': 'first\nsecond',
            'correct_options': '1',
            'blanks': '',
        })

        self.assertTrue(response.json()['ok'])
        problem = Problem.objects.get(name='Figure question')
        self.assertIn('/media/martor/final.png', problem.description)
        self.assertFalse(ProblemImportImage.objects.filter(pk=image.pk).exists())
        store_docx_image.assert_called_once_with('figure.png', b'png-data')

    @mock.patch('judge.views.problem_import.store_docx_image')
    def test_fill_blank_creation_keeps_promoted_docx_image_urls(self, store_docx_image):
        self.upload([
            ['name', 'question_type', 'description', 'answer'],
            ['Figure blank', 'fill_blank', 'The figure shows [[blank]].', 'a triangle'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)
        item = draft.items.get()
        image = ProblemImportImage.objects.create(item=item, filename='figure.png', content=b'png-data')
        image_url = reverse('problem_import_image', args=(draft.pk, image.token))
        item.description = 'The figure shows [[blank]].\n\n![]({})'.format(image_url)
        item.blanks = ''
        item.save(update_fields=['description', 'blanks'])
        store_docx_image.return_value = ('martor/final.png', '/media/martor/final.png')

        response = self.client.post(reverse('problem_import_item_action', args=(draft.pk, item.pk, 'create')), {
            'name': item.name,
            'question_type': 'B',
            'description': item.description,
            'answer': item.answer,
            'options': '',
            'correct_options': '',
            'blanks': '',
        })

        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(name='Figure blank')
        self.assertIn('/media/martor/final.png', problem.description)
        self.assertNotIn(image_url, problem.description)

    def test_create_all_creates_all_reviewed_items(self):
        self.upload([
            ['name', 'question_type', 'description', 'answer'],
            ['Prime check', 'multiple_choice', 'Which is prime?\nA. 4\nB. 5', 'B'],
            ['Capital', 'fill_blank', 'The capital of France is [[blank]].', 'Paris'],
        ])
        draft = ProblemImportDraft.objects.get(owner=self.creator.profile)

        response = self.client.post(
            reverse('problem_import_create_all', args=(draft.pk,)),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(response.json()['count'], 2)
        self.assertEqual(draft.items.filter(created_problem__isnull=False).count(), 2)
        self.assertEqual(Problem.objects.filter(name__in=['Prime check', 'Capital']).count(), 2)
        created = Problem.objects.filter(name__in=['Prime check', 'Capital'])
        logs = LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(Problem),
            object_id__in=[str(pk) for pk in created.values_list('pk', flat=True)],
            action_flag=ADDITION,
            user=self.creator,
        )
        self.assertEqual(logs.count(), 2)
