import json
import os
import tempfile
import zipfile
from io import BytesIO
from unittest import mock

import yaml
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from reversion.models import Version

from judge.models import (Language, LanguageLimit, License, Problem, ProblemClarification, ProblemData,
                          ProblemFileConfig, ProblemFillBlankConfig, ProblemHarness, ProblemMCQConfig,
                          ProblemShortAnswerConfig, ProblemTemplate, ProblemTestCase, ProblemTranslation,
                          ProblemType, Solution, problem_data_storage)
from judge.models.tests.util import CommonDataMixin, create_organization, create_problem, create_user
from judge.utils.problem_create import default_problem_group
from judge.views.problem_create_v2 import ProblemCreateV2View


def _problem_log_entries(problem, user=None):
    qs = LogEntry.objects.filter(
        content_type=ContentType.objects.get_for_model(Problem),
        object_id=str(problem.pk),
    ).order_by('action_time', 'pk')
    if user is not None:
        qs = qs.filter(user=user)
    return list(qs)


class ProblemCreateV2ViewTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.group = default_problem_group()
        cls.users.update({
            'creator': create_user(
                username='mcq_creator',
                user_permissions=('create_private_problem', 'edit_own_problem'),
            ),
            'staff_adder': create_user(
                username='code_creator',
                is_staff=True,
                user_permissions=('add_problem', 'create_private_problem', 'change_public_visibility',
                                  'edit_own_problem'),
            ),
        })

    def test_get_requires_permission(self):
        self.client.force_login(self.users['normal'])
        self.assertEqual(self.client.get(reverse('problem_create_v2')).status_code, 404)

        self.client.force_login(self.users['creator'])
        self.assertEqual(self.client.get(reverse('problem_create_v2')).status_code, 200)

    def test_edit_data_user_pickers_use_name_username_label(self):
        creator = self.users['creator']
        creator.first_name = 'Zhang'
        creator.save(update_fields=['first_name'])
        problem = create_problem(code='namedauth', authors=('mcq_creator',))
        data = ProblemCreateV2View()._build_edit_data(problem)
        row = data['authors'][0]
        self.assertEqual(row['username'], 'mcq_creator')
        self.assertEqual(row['name'], 'Zhang')
        self.assertEqual(row['text'], 'Zhang (mcq_creator)')

    def test_create_private_problem_can_choose_programming(self):
        """Instructors have create_private_problem, not add_problem; programming must still be offered."""
        self.client.force_login(self.users['creator'])
        response = self.client.get(reverse('problem_create_v2'))
        self.assertTrue(response.context['can_create_code'])
        html = response.content.decode()
        self.assertNotRegex(html, r'name="qtype" value="programming"[^>]*disabled')
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'programming',
            'name': 'Instructor code',
            'description': 'print hello',
            'points': 10,
            'group_id': self.group.id,
        }
        posted = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(posted.status_code, 200, posted.content)
        self.assertEqual(Problem.objects.get(code=posted.json()['code']).submission_type, 'C')

    def test_create_page_uses_public_visible_label(self):
        self.client.force_login(self.users['creator'])
        response = self.client.get(reverse('problem_create_v2'))
        self.assertContains(response, 'Public visible')
        self.assertNotContains(response, 'publicly visible')
        self.assertNotRegex(response.content.decode(), r'id="f-public"[^>]*checked')
        self.assertContains(response, 'Editorial')
        self.assertContains(response, 'data-rich-mount="editorial"')
        self.assertNotContains(response, 'data-rich-mount="explain"')

    def test_course_dropdown_lists_taught_courses(self):
        """AUTH-10: only courses the instructor teaches are offered (not memberships, not other courses)."""
        member_org = create_organization(name='Member Course')
        admin_only_org = create_organization(name='Admin Only Course')
        outsider_org = create_organization(name='Other Course')
        creator = self.users['creator']
        creator.profile.organizations.add(member_org)
        admin_only_org.admins.add(creator.profile)

        self.client.force_login(creator)
        response = self.client.get(reverse('problem_create_v2'))
        names = {org['name'] for org in response.context['organizations']}
        self.assertTrue(response.context['can_create_organization'])
        self.assertTrue(response.context['can_change_public'])
        self.assertNotIn(member_org.name, names)
        self.assertIn(admin_only_org.name, names)
        self.assertNotIn(outsider_org.name, names)
        self.assertNotRegex(response.content.decode(), r'<select[^>]*id="f-org"[^>]*disabled')

    def test_course_field_readonly_without_create_private_problem(self):
        staff = create_user(
            username='staff_code_only',
            is_staff=True,
            user_permissions=('add_problem', 'edit_own_problem'),
        )
        self.client.force_login(staff)
        response = self.client.get(reverse('problem_create_v2'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['can_create_organization'])
        self.assertFalse(response.context['can_change_public'])
        self.assertContains(response, 'id="f-org"', html=False)
        self.assertRegex(response.content.decode(), r'<select[^>]*id="f-org"[^>]*disabled')

    def test_create_private_problem_can_attach_taught_course(self):
        org = create_organization(name='Unrelated Course')
        org.instructors.add(self.users['creator'].profile)
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'single',
            'name': 'Org scoped',
            'description': 'A course the instructor teaches is allowed.',
            'points': 5,
            'group_id': self.group.id,
            'organizations': [org.id],
            'is_public': True,
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertTrue(problem.organizations.filter(id=org.id).exists())
        self.assertTrue(problem.is_organization_private)
        self.assertTrue(problem.is_public)

    def test_create_private_cannot_make_fully_public_without_course(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'single',
            'name': 'Fully public',
            'description': 'Needs change_public_visibility.',
            'points': 5,
            'group_id': self.group.id,
            'is_public': True,
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_staff_without_create_private_cannot_attach_course(self):
        org = create_organization(name='Blocked Course')
        staff = create_user(
            username='staff_no_org',
            is_staff=True,
            user_permissions=('add_problem', 'edit_own_problem'),
        )
        self.client.force_login(staff)
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'programming',
            'name': 'No org',
            'description': 'Staff cannot assign courses.',
            'points': 10,
            'group_id': self.group.id,
            'organizations': [org.id],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertFalse(problem.organizations.exists())
        self.assertFalse(problem.is_organization_private)

    def test_create_mcq_via_json(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'single',
            'name': 'HTTP vs TCP',
            'description': 'Which is connection-oriented?',
            'points': 5,
            'group_id': self.group.id,
            'options': [
                {'text': 'HTTP', 'correct': False},
                {'text': 'TCP', 'correct': True},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        problem = Problem.objects.get(code=body['code'])
        self.assertEqual(body['id'], problem.id)
        self.assertEqual(body['name'], 'HTTP vs TCP')
        self.assertEqual(body['points'], 5)
        self.assertEqual(problem.name, 'HTTP vs TCP')
        self.assertTrue(ProblemMCQConfig.objects.filter(problem=problem, multiple_select=False).exists())

    def test_create_page_prefills_suggested_code(self):
        create_problem(code='p000000', name='Taken series', authors=('superuser',))
        self.client.force_login(self.users['creator'])
        response = self.client.get(reverse('problem_create_v2'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['suggested_code'], 'p000001')
        html = response.content.decode()
        self.assertIn('value="p000001"', html)
        self.assertIn('id="f-code"', html)

    def test_create_own_code_already_saved_opens_editor(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'single',
            'name': 'Retry save',
            'description': 'Same code twice.',
            'points': 5,
            'group_id': self.group.id,
            'code': 'p000000',
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }
        first = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(first.status_code, 200, first.content)
        retry = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(retry.status_code, 200, retry.content)
        body = retry.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body['already_saved'])
        self.assertEqual(body['code'], 'p000000')
        self.assertIn('already saved', body['message'].lower())
        self.assertEqual(Problem.objects.filter(code='p000000').count(), 1)
        self.assertEqual(body['redirect'], reverse('problem_edit_v2', args=['p000000']))

    def test_create_series_code_taken_by_other_saves_next(self):
        create_problem(code='p000000', name='Other problem', authors=('superuser',))
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'single',
            'name': 'Collision',
            'description': 'Series taken.',
            'points': 5,
            'group_id': self.group.id,
            'code': 'p000000',
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertTrue(body.get('id_changed'))
        self.assertEqual(body['id_changed_from'], 'p000000')
        self.assertEqual(body['code'], 'p000001')
        self.assertIn('p000001', body['message'])
        self.assertIn('p000000', body['message'])
        self.assertTrue(Problem.objects.filter(code='p000001', name='Collision').exists())

    def test_create_custom_code_taken_returns_400_with_id(self):
        create_problem(code='myquiz', name='Taken custom', authors=('superuser',))
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'single',
            'name': 'Dup custom',
            'description': 'Custom taken.',
            'points': 5,
            'group_id': self.group.id,
            'code': 'myquiz',
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        error = response.json()['error']
        self.assertIn('already exists', error)
        self.assertIn('myquiz', error)

    def test_create_multiple_choice_defaults_to_proportional_scoring(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'multiple',
            'name': 'Pick two',
            'description': 'Select all that apply.',
            'points': 10,
            'group_id': self.group.id,
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': True},
                {'text': 'C', 'correct': False},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertTrue(problem.partial)
        config = ProblemMCQConfig.objects.get(problem=problem)
        self.assertTrue(config.multiple_select)
        self.assertEqual(config.scoring_mode, 'proportional')
        self.assertIsNone(config.underselect_points)

    def test_create_multiple_choice_underselect_scoring(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'multiple',
            'name': 'Underselect',
            'description': 'Select all that apply.',
            'points': 10,
            'group_id': self.group.id,
            'mcqScoring': 'underselect',
            'mcqUnderselectPoints': 2,
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': True},
                {'text': 'C', 'correct': False},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        config = ProblemMCQConfig.objects.get(problem=problem)
        self.assertEqual(config.scoring_mode, 'underselect')
        self.assertEqual(config.underselect_points, 2)
        data = ProblemCreateV2View()._build_edit_data(problem)
        self.assertEqual(data['mcqScoring'], 'underselect')
        self.assertEqual(data['mcqUnderselectPoints'], 2)

    def test_create_code_with_testcases(self):
        self.client.force_login(self.users['staff_adder'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'programming',
            'name': 'A Plus B',
            'description': 'Read two integers.',
            'points': 10,
            'partial': True,
            'time_limit': 2,
            'memory_limit': 256,
            'group_id': self.group.id,
            'testcases': [
                {'input': '1 2\n', 'output': '3\n', 'points': 50, 'pretest': True},
                {'input': '0 0\n', 'output': '0\n', 'points': 50, 'pretest': False},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        problem = Problem.objects.get(code=body['code'])
        self.assertEqual(problem.cases.count(), 2)
        self.assertTrue(ProblemTestCase.objects.filter(dataset=problem, input_file='01.in').exists())

    @mock.patch('judge.judgeapi.update_problems')
    def test_create_code_notifies_judges_after_commit(self, update_problems):
        self.client.force_login(self.users['staff_adder'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'programming',
            'name': 'Judge Notify',
            'description': 'Read two integers.',
            'points': 10,
            'group_id': self.group.id,
            'testcases': [
                {'input': '1\n', 'output': '1\n', 'points': 10, 'pretest': False},
            ],
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('problem_create_v2'),
                data=json.dumps(payload),
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(update_problems.called)

    def test_rejects_unimplemented_type(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'compound',
            'name': 'Compound',
            'description': 'Not implemented yet',
            'points': 1,
            'group_id': self.group.id,
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(LANGUAGES=[('en', 'English'), ('zh-hans', 'Chinese (Simplified)')])
    def test_create_mcq_persists_advanced_fields(self):
        from django.contrib.auth.models import Permission
        creator = self.users['creator']
        creator.user_permissions.add(Permission.objects.get(codename='problem_full_markup'))
        self.client.force_login(creator)
        curator = self.users['normal'].profile
        license_obj = License.objects.create(
            key='cc-by-sa-4.0', link='https://example.com/license', name='CC BY-SA 4.0',
            display='CC BY-SA 4.0', text='license text',
        )
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'single',
            'name': 'Advanced wired',
            'description': 'Authors and license should persist.',
            'points': 5,
            'group_id': self.group.id,
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
            'curators': [curator.id],
            'license_id': license_obj.id,
            'is_full_markup': True,
            'view_test_cases': True,
            'summary': 'short summary',
            'submission_source_visibility_mode': 'S',
            'solution': {
                'is_public': True,
                'publish_on': '2030-01-02 15:30',
                'content': 'Official solution body',
                'authors': [self.users['creator'].profile.id],
            },
            'translations': [
                {'language': 'zh-hans', 'name': '进阶题', 'description': '中文描述'},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        problem = Problem.objects.get(code=body['code'])
        self.assertTrue(problem.authors.filter(id=self.users['creator'].profile.id).exists())
        self.assertTrue(problem.curators.filter(id=curator.id).exists())
        self.assertEqual(problem.license_id, license_obj.id)
        self.assertTrue(problem.is_full_markup)
        self.assertTrue(problem.view_test_cases)
        self.assertEqual(problem.summary, 'short summary')
        self.assertEqual(problem.submission_source_visibility_mode, 'S')
        sol = Solution.objects.get(problem=problem)
        self.assertTrue(sol.is_public)
        self.assertEqual(sol.content, 'Official solution body')
        self.assertTrue(sol.authors.filter(id=self.users['creator'].profile.id).exists())
        tr = ProblemTranslation.objects.get(problem=problem, language='zh-hans')
        self.assertEqual(tr.name, '进阶题')
        self.assertEqual(tr.description, '中文描述')

    @override_settings(LANGUAGES=[('en', 'English'), ('zh-hans', 'Chinese (Simplified)')])
    def test_edit_omitting_solution_and_translations_keeps_them(self):
        """Partial edit payloads must not wipe Advanced solution/translations."""
        self.client.force_login(self.users['creator'])
        create_payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'single',
            'name': 'Keep advanced',
            'description': 'Has solution and translation.',
            'points': 5,
            'group_id': self.group.id,
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
            'solution': {
                'is_public': True,
                'content': 'Keep me',
                'authors': [self.users['creator'].profile.id],
            },
            'translations': [
                {'language': 'zh-hans', 'name': '保留', 'description': '保留描述'},
            ],
        }
        create_resp = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(create_payload),
            content_type='application/json',
        )
        self.assertEqual(create_resp.status_code, 200)
        code = create_resp.json()['code']

        edit_payload = {
            'type': 'single',
            'name': 'Keep advanced (renamed)',
            'description': 'Updated description only.',
            'points': 5,
            'group_id': self.group.id,
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
            'edit_code': code,
        }
        edit_resp = self.client.post(
            reverse('problem_edit_v2', args=[code]),
            data=json.dumps(edit_payload),
            content_type='application/json',
        )
        self.assertEqual(edit_resp.status_code, 200)
        self.assertTrue(edit_resp.json()['ok'])
        problem = Problem.objects.get(code=code)
        self.assertEqual(problem.name, 'Keep advanced (renamed)')
        sol = Solution.objects.get(problem=problem)
        self.assertEqual(sol.content, 'Keep me')
        tr = ProblemTranslation.objects.get(problem=problem, language='zh-hans')
        self.assertEqual(tr.name, '保留')

    def test_edit_does_not_promote_curator_to_author(self):
        """Curators/admins editing must not be force-added to authors."""
        curator = create_user(
            username='mcq_curator',
            user_permissions=('create_private_problem', 'edit_own_problem'),
        )
        self.client.force_login(self.users['creator'])
        create_resp = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps({
                'problemTypes': ['Graph Theory'],
                'type': 'single',
                'name': 'Author ownership',
                'description': 'Keep authors stable on edit.',
                'points': 5,
                'group_id': self.group.id,
                'options': [
                    {'text': 'A', 'correct': True},
                    {'text': 'B', 'correct': False},
                ],
                'curators': [curator.profile.id],
            }),
            content_type='application/json',
        )
        self.assertEqual(create_resp.status_code, 200)
        code = create_resp.json()['code']
        problem = Problem.objects.get(code=code)
        self.assertTrue(problem.authors.filter(id=self.users['creator'].profile.id).exists())
        self.assertFalse(problem.authors.filter(id=curator.profile.id).exists())

        self.client.force_login(curator)
        edit_resp = self.client.post(
            reverse('problem_edit_v2', args=[code]),
            data=json.dumps({
                'type': 'single',
                'name': 'Author ownership (edited)',
                'description': 'Edited by curator.',
                'points': 5,
                'group_id': self.group.id,
                'options': [
                    {'text': 'A', 'correct': True},
                    {'text': 'B', 'correct': False},
                ],
                'authors': [self.users['creator'].profile.id],
                'curators': [curator.profile.id],
                'edit_code': code,
            }),
            content_type='application/json',
        )
        self.assertEqual(edit_resp.status_code, 200, edit_resp.content)
        self.assertTrue(edit_resp.json()['ok'])
        problem.refresh_from_db()
        self.assertEqual(list(problem.authors.values_list('id', flat=True)), [self.users['creator'].profile.id])
        self.assertTrue(problem.curators.filter(id=curator.profile.id).exists())

    def test_full_markup_requires_permission(self):
        self.client.force_login(self.users['creator'])  # no problem_full_markup
        resp = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps({
                'problemTypes': ['Graph Theory'],
                'type': 'single',
                'name': 'No full markup',
                'description': 'Should stay False.',
                'points': 1,
                'group_id': self.group.id,
                'options': [
                    {'text': 'A', 'correct': True},
                    {'text': 'B', 'correct': False},
                ],
                'is_full_markup': True,
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        problem = Problem.objects.get(code=resp.json()['code'])
        self.assertFalse(problem.is_full_markup)

    def test_unsupported_translation_language_rejected(self):
        self.client.force_login(self.users['creator'])
        resp = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps({
                'problemTypes': ['Graph Theory'],
                'type': 'single',
                'name': 'Bad translation lang',
                'description': 'Should 400.',
                'points': 1,
                'group_id': self.group.id,
                'options': [
                    {'text': 'A', 'correct': True},
                    {'text': 'B', 'correct': False},
                ],
                'translations': [
                    {'language': 'xx-not-a-lang', 'name': 'X', 'description': 'Y'},
                ],
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Unsupported translation language', resp.json()['error'])

    def test_create_code_persists_language_limits_templates_clarifications(self):
        lang = Language.objects.first()
        self.assertIsNotNone(lang)
        self.client.force_login(self.users['staff_adder'])
        resp = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps({
                'problemTypes': ['Graph Theory'],
                'type': 'programming',
                'name': 'Limits and templates',
                'description': 'Wire advanced inlines.',
                'points': 10,
                'group_id': self.group.id,
                'time_limit': 2,
                'memory_limit': 256,
                'testcases': [
                    {'input': '1\n', 'output': '1\n', 'points': 100},
                ],
                'language_limits': [
                    {'language': lang.key, 'time_limit': 3.5, 'memory_limit': 131072},
                ],
                'templates': [
                    {'language': lang.key, 'code': 'print(input())\n'},
                ],
                'clarifications': [
                    {'description': 'Input is a single integer.'},
                ],
                'edit_reason': 'Initial wiring of advanced inlines',
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        problem = Problem.objects.get(code=resp.json()['code'])
        lim = LanguageLimit.objects.get(problem=problem, language=lang)
        self.assertEqual(lim.time_limit, 3.5)
        self.assertEqual(lim.memory_limit, 131072)
        tmpl = ProblemTemplate.objects.get(problem=problem, language=lang)
        self.assertEqual(tmpl.code, 'print(input())\n')
        clar = ProblemClarification.objects.get(problem=problem)
        self.assertEqual(clar.description, 'Input is a single integer.')
        versions = Version.objects.get_for_object(problem)
        self.assertTrue(versions.exists())
        self.assertEqual(versions[0].revision.comment, 'Initial wiring of advanced inlines')

    def test_edit_clarifications_can_update_and_delete(self):
        self.client.force_login(self.users['creator'])
        create_resp = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps({
                'problemTypes': ['Graph Theory'],
                'type': 'single',
                'name': 'Clarify me',
                'description': 'Has clarifications.',
                'points': 5,
                'group_id': self.group.id,
                'options': [
                    {'text': 'A', 'correct': True},
                    {'text': 'B', 'correct': False},
                ],
                'clarifications': [
                    {'description': 'Keep this'},
                    {'description': 'Remove this'},
                ],
            }),
            content_type='application/json',
        )
        self.assertEqual(create_resp.status_code, 200)
        code = create_resp.json()['code']
        problem = Problem.objects.get(code=code)
        keep = ProblemClarification.objects.get(problem=problem, description='Keep this')
        remove = ProblemClarification.objects.get(problem=problem, description='Remove this')

        edit_resp = self.client.post(
            reverse('problem_edit_v2', args=[code]),
            data=json.dumps({
                'type': 'single',
                'name': 'Clarify me',
                'description': 'Has clarifications.',
                'points': 5,
                'group_id': self.group.id,
                'options': [
                    {'text': 'A', 'correct': True},
                    {'text': 'B', 'correct': False},
                ],
                'edit_code': code,
                'clarifications': [
                    {'id': keep.id, 'description': 'Keep this (updated)'},
                    {'id': remove.id, 'description': 'Remove this', 'delete': True},
                    {'description': 'Brand new'},
                ],
                'edit_reason': 'Update clarifications',
            }),
            content_type='application/json',
        )
        self.assertEqual(edit_resp.status_code, 200, edit_resp.content)
        descs = list(
            ProblemClarification.objects.filter(problem=problem).order_by('id').values_list('description', flat=True),
        )
        self.assertEqual(descs, ['Keep this (updated)', 'Brand new'])
        versions = Version.objects.get_for_object(problem)
        self.assertEqual(versions[0].revision.comment, 'Update clarifications')

    def test_ai_difficulty_endpoint(self):
        self.client.force_login(self.users['creator'])
        # Mock the LLM as unconfigured so the endpoint deterministically uses its heuristic
        # fallback instead of a real model call (which would hang/time out on a host that has a
        # configured-but-unreachable LLM, e.g. the production container).
        with mock.patch('judge.llm.is_configured', return_value=False):
            response = self.client.post(
                reverse('problem_create_v2_ai_difficulty'),
                data=json.dumps({'description': 'short'}),
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(response.json()['level'], ('easy', 'medium', 'hard'))

    def test_create_problem_type(self):
        self.client.force_login(self.users['creator'])
        response = self.client.post(
            reverse('problem_create_v2_problem_type'),
            data=json.dumps({'name': 'graph-theory'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['type']['name'], 'graph-theory')
        self.assertEqual(body['type']['full'], 'graph-theory')

    def test_create_problem_type_from_display_name_only(self):
        self.client.force_login(self.users['creator'])
        response = self.client.post(
            reverse('problem_create_v2_problem_type'),
            data=json.dumps({'full_name': 'Dynamic Programming'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['type']['full'], 'Dynamic Programming')

    def test_create_truefalse_as_mcq(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'truefalse', 'name': 'Sky is blue', 'description': 'Is the sky blue?',
            'points': 2, 'group_id': self.group.id, 'tfAnswer': 'true',
        }
        response = self.client.post(reverse('problem_create_v2'), data=json.dumps(payload),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertEqual(problem.submission_type, 'M')
        self.assertTrue(ProblemMCQConfig.objects.filter(problem=problem, multiple_select=False).exists())
        self.assertEqual(problem.mcq_options.count(), 2)
        self.assertTrue(problem.mcq_options.get(order=0).is_correct)
        self.assertFalse(problem.mcq_options.get(order=1).is_correct)

    def test_create_fill_blank(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'blank', 'name': 'Capital', 'description': 'Capital of France is {{blank}}.',
            'points': 3, 'group_id': self.group.id, 'blanks': ['Paris\nparis'],
        }
        response = self.client.post(reverse('problem_create_v2'), data=json.dumps(payload),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertEqual(problem.submission_type, 'B')
        # trim_whitespace defaults to True; case_sensitive defaults to False.
        self.assertTrue(ProblemFillBlankConfig.objects.filter(
            problem=problem, trim_whitespace=True, case_sensitive=False).exists())
        self.assertEqual(problem.blanks.count(), 1)
        # Each line is one accepted answer for the newline-splitting grader.
        self.assertEqual(problem.blanks.first().answers.splitlines(), ['Paris', 'paris'])

    def test_create_fill_blank_persists_per_blank_points(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'blank', 'name': 'Weighted blanks', 'description': 'Two blanks.',
            'points': 10, 'partial': True, 'group_id': self.group.id,
            'blanks': [
                {'answers': 'Paris', 'points': 7},
                {'answers': '1789', 'points': 3},
            ],
        }
        response = self.client.post(reverse('problem_create_v2'), data=json.dumps(payload),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertTrue(problem.partial)
        blanks = list(problem.blanks.order_by('order'))
        self.assertEqual([b.points for b in blanks], [7, 3])
        data = ProblemCreateV2View()._build_edit_data(problem)
        self.assertEqual(data['blanks'][0]['points'], 7)
        self.assertEqual(data['blanks'][1]['answers'], '1789')

    def test_create_fill_blank_ai_mode(self):
        # With ai=True the config records AI grading + rubric; grading itself falls back to exact
        # when no LLM is configured (see judge/question_types/fill_blank.py).
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'blank', 'name': 'Essay blank', 'description': 'Define recursion: {{blank}}',
            'points': 4, 'group_id': self.group.id, 'blanks': ['a function calling itself'],
            'ai': True, 'rubric': 'accept any answer mentioning self-reference',
        }
        response = self.client.post(reverse('problem_create_v2'), data=json.dumps(payload),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        config = ProblemFillBlankConfig.objects.get(problem=problem)
        self.assertEqual(config.grading_mode, 'ai')
        self.assertEqual(config.ai_rubric, 'accept any answer mentioning self-reference')

    def test_create_short_answer(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'short', 'name': 'Explain TCP', 'description': 'Explain TCP handshake.',
            'points': 5, 'group_id': self.group.id, 'shortAnswer': 'SYN, SYN-ACK, ACK', 'rubric': 'mention 3 steps',
        }
        response = self.client.post(reverse('problem_create_v2'), data=json.dumps(payload),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertEqual(problem.submission_type, 'S')
        config = ProblemShortAnswerConfig.objects.get(problem=problem)
        self.assertEqual(config.reference_answer, 'SYN, SYN-ACK, ACK')
        self.assertEqual(config.ai_rubric, 'mention 3 steps')
        self.assertFalse(config.ai_enabled)

    def test_create_short_answer_can_enable_ai_grading(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'short', 'name': 'Explain TCP', 'description': 'Explain TCP handshake.',
            'points': 5, 'group_id': self.group.id, 'shortAnswer': 'SYN, SYN-ACK, ACK',
            'ai': True, 'rubric': 'mention 3 steps',
        }
        response = self.client.post(reverse('problem_create_v2'), data=json.dumps(payload),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        config = ProblemShortAnswerConfig.objects.get(problem=problem)
        self.assertTrue(config.ai_enabled)
        data = ProblemCreateV2View()._build_edit_data(problem)
        self.assertTrue(data['ai'])

    def test_create_file_upload(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'file', 'name': 'Submit your report', 'description': 'Upload a PDF report.',
            'points': 20, 'group_id': self.group.id,
            'fileExtensions': 'pdf', 'fileMaxSizeMb': 10, 'fileInstructions': 'One PDF, no zip.',
        }
        response = self.client.post(reverse('problem_create_v2'), data=json.dumps(payload),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertEqual(problem.submission_type, 'F')
        self.assertTrue(problem.partial)
        config = ProblemFileConfig.objects.get(problem=problem)
        self.assertEqual(config.allowed_extensions, 'pdf')
        self.assertEqual(config.max_file_size, 10 * 1024 * 1024)
        self.assertEqual(config.instructions, 'One PDF, no zip.')

    def test_file_upload_edit_data_round_trip(self):
        """_build_edit_data must hand back MB so a resave does not shrink the limit each time."""
        self.client.force_login(self.users['creator'])
        response = self.client.post(reverse('problem_create_v2'), data=json.dumps({
            'problemTypes': ['Graph Theory'],
            'type': 'file', 'name': 'Round trip', 'description': 'x', 'points': 5,
            'group_id': self.group.id, 'fileExtensions': 'pdf', 'fileMaxSizeMb': 7,
        }), content_type='application/json')
        problem = Problem.objects.get(code=response.json()['code'])

        data = ProblemCreateV2View()._build_edit_data(problem)
        self.assertEqual(data['type'], 'file')
        # MB, not the raw byte count — otherwise the page would show 7340032 and resave 7 TB.
        self.assertEqual(data['fileMaxSizeMb'], 7)
        self.assertEqual(data['fileExtensions'], 'pdf')

        # Echoing that value back the way the page does must leave the stored bytes unchanged.
        response = self.client.post(reverse('problem_edit_v2', args=[problem.code]), data=json.dumps({
            'type': 'file', 'edit_code': problem.code, 'name': data['name'],
            'description': data['description'], 'points': data['points'], 'group_id': self.group.id,
            'fileExtensions': data['fileExtensions'], 'fileMaxSizeMb': data['fileMaxSizeMb'],
            'fileInstructions': data['fileInstructions'],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ProblemFileConfig.objects.get(problem=problem).max_file_size, 7 * 1024 * 1024)

    def test_create_file_upload_rejects_non_positive_size(self):
        self.client.force_login(self.users['creator'])
        response = self.client.post(reverse('problem_create_v2'), data=json.dumps({
            'problemTypes': ['Graph Theory'],
            'type': 'file', 'name': 'Bad cap', 'description': 'x', 'points': 5,
            'group_id': self.group.id, 'fileMaxSizeMb': 0,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Problem.objects.filter(name='Bad cap').exists())

    def test_create_fill_blank_multiple(self):
        # The create page writes one ProblemBlank per blank; alternatives are newline-separated.
        # End-to-end grading is covered by judge/views/tests/test_fill_blank.py.
        self.client.force_login(self.users['creator'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'blank', 'name': 'Math', 'description': '1+1={{blank}}, 2+2={{blank}}',
            'points': 2, 'group_id': self.group.id, 'blanks': ['2\ntwo', '4\nfour'],
        }
        response = self.client.post(reverse('problem_create_v2'), data=json.dumps(payload),
                                    content_type='application/json')
        problem = Problem.objects.get(code=response.json()['code'])
        blanks = list(problem.blanks.order_by('order'))
        self.assertEqual(len(blanks), 2)
        self.assertEqual(blanks[0].answers.splitlines(), ['2', 'two'])
        self.assertEqual(blanks[1].answers.splitlines(), ['4', 'four'])

    def test_ai_problem_type_endpoint(self):
        self.client.force_login(self.users['creator'])
        # Mock the LLM as unconfigured so the endpoint uses its deterministic substring fallback
        # instead of a real model call (which hangs on a configured-but-unreachable LLM).
        with mock.patch('judge.llm.is_configured', return_value=False):
            response = self.client.post(
                reverse('problem_create_v2_ai_problem_type'),
                data=json.dumps({'description': 'Find shortest path in a weighted graph using Dijkstra.'}),
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['suggestions'])

    def test_save_creates_missing_problem_types_and_lists_them(self):
        """AI / free-text tags that are not in the DB yet must be created and attached on save."""
        label = 'Brand New Algorithm Tag'
        self.assertFalse(ProblemType.objects.filter(full_name=label).exists())

        self.client.force_login(self.users['creator'])
        payload = {
            'type': 'single',
            'name': 'Typed MCQ',
            'description': 'A problem with a novel knowledge tag.',
            'points': 5,
            'group_id': self.group.id,
            'problemTypes': [label],
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])

        ptype = ProblemType.objects.get(full_name=label)
        self.assertIn(ptype, list(problem.types.all()))

        # Problem list surfaces knowledge filters from ProblemType rows.
        list_response = self.client.get(reverse('problem_list'))
        self.assertEqual(list_response.status_code, 200)
        knowledge_names = {t.full_name for t in list_response.context['knowledge_types']}
        self.assertIn(label, knowledge_names)

    def test_save_reuses_existing_problem_type_without_duplicating(self):
        existing = ProblemType.objects.create(name='graph', full_name='Graph Theory')
        before = ProblemType.objects.count()

        self.client.force_login(self.users['creator'])
        payload = {
            'type': 'single',
            'name': 'Existing type',
            'description': 'reuse',
            'points': 3,
            'group_id': self.group.id,
            'problemTypes': ['Graph Theory'],
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertEqual(ProblemType.objects.count(), before)
        self.assertEqual(list(problem.types.all()), [existing])

    def test_auto_type_difficulty_chips_not_stored_or_duplicated_on_edit(self):
        """UI auto chips (Programming/Fill-in-blank/Easy) must not pollute knowledge tags."""
        self.client.force_login(self.users['staff_adder'])
        knowledge = 'Graph Theory'
        create_payload = {
            'type': 'programming',
            'name': 'Tag dedupe',
            'description': 'auto chips should be ignored on save',
            'points': 10,
            'group_id': self.group.id,
            'difficulty': 'easy',
            # Mimic a buggy/legacy client that also posts the gray auto chips.
            'problemTypes': ['Programming', 'Easy', knowledge, 'Programming', 'Easy'],
            'testcases': [
                {'input': '1\n', 'output': '1\n', 'points': 10, 'pretest': False},
            ],
        }
        create_resp = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(create_payload),
            content_type='application/json',
        )
        self.assertEqual(create_resp.status_code, 200)
        code = create_resp.json()['code']
        problem = Problem.objects.get(code=code)

        type_names = list(problem.types.values_list('name', flat=True))
        type_full = list(problem.types.values_list('full_name', flat=True))
        self.assertIn('easy', type_names)
        self.assertEqual(problem.difficulty, 'easy')
        self.assertIn(knowledge, type_full)
        self.assertNotIn('Programming', type_full)
        self.assertEqual(type_full.count(knowledge), 1)
        self.assertEqual(type_names.count('easy'), 1)

        edit_get = self.client.get(reverse('problem_edit_v2', args=[code]))
        self.assertEqual(edit_get.status_code, 200)
        edit_data = json.loads(edit_get.context['edit_data_json'])
        self.assertEqual(edit_data['difficulty'], 'easy')
        self.assertEqual(edit_data['problemTypes'], [knowledge])
        self.assertNotIn('Programming', edit_data['problemTypes'])
        self.assertNotIn('Easy', edit_data['problemTypes'])

        # Re-save with the same polluted payload (create → edit cycle).
        edit_payload = dict(create_payload)
        edit_payload.update({
            'name': 'Tag dedupe edited',
            'edit_code': code,
            'problemTypes': ['Programming', 'Easy', knowledge, 'Fill-in-blank'],
        })
        edit_resp = self.client.post(
            reverse('problem_edit_v2', args=[code]),
            data=json.dumps(edit_payload),
            content_type='application/json',
        )
        self.assertEqual(edit_resp.status_code, 200)
        problem.refresh_from_db()
        # Invalidate cached_property from the earlier difficulty assertion.
        problem.__dict__.pop('difficulty', None)

        type_names = list(problem.types.values_list('name', flat=True))
        type_full = list(problem.types.values_list('full_name', flat=True))
        self.assertEqual(type_names.count('easy'), 1)
        self.assertEqual(type_full.count(knowledge), 1)
        self.assertNotIn('Programming', type_full)
        self.assertNotIn('Fill-in-blank', type_full)
        self.assertEqual(problem.difficulty, 'easy')
        self.assertEqual(problem.knowledge_types_list, [knowledge])

        # Blank + translated-style auto labels are also stripped; difficulty follows the field.
        blank_payload = {
            'type': 'blank',
            'name': 'Blank tag dedupe',
            'description': 'fill __blank__',
            'points': 5,
            'group_id': self.group.id,
            'difficulty': 'medium',
            'problemTypes': ['Fill-in-blank', '填空题', 'Medium', '中等', knowledge],
            'blanks': ['answer'],
        }
        self.client.force_login(self.users['creator'])
        blank_resp = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(blank_payload),
            content_type='application/json',
        )
        self.assertEqual(blank_resp.status_code, 200)
        blank = Problem.objects.get(code=blank_resp.json()['code'])
        blank_full = set(blank.types.values_list('full_name', flat=True))
        self.assertIn(knowledge, blank_full)
        self.assertNotIn('Fill-in-blank', blank_full)
        self.assertNotIn('填空题', blank_full)
        self.assertEqual(blank.difficulty, 'medium')

    def test_edit_omitting_problem_types_keeps_knowledge_tags(self):
        """Partial payloads that only send difficulty must not wipe knowledge tags."""
        knowledge = ProblemType.objects.create(name='graph-theory', full_name='Graph Theory')
        self.client.force_login(self.users['creator'])
        create_payload = {
            'type': 'single',
            'name': 'Tagged MCQ',
            'description': 'has tags',
            'points': 5,
            'group_id': self.group.id,
            'difficulty': 'easy',
            'problemTypes': ['Graph Theory'],
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }
        create_resp = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(create_payload),
            content_type='application/json',
        )
        self.assertEqual(create_resp.status_code, 200, create_resp.content)
        code = create_resp.json()['code']
        problem = Problem.objects.get(code=code)
        self.assertEqual(problem.difficulty, 'easy')
        self.assertIn(knowledge, list(problem.types.all()))

        edit_payload = {
            'type': 'single',
            'name': 'Tagged MCQ',
            'description': 'has tags',
            'points': 5,
            'group_id': self.group.id,
            'edit_code': code,
            'difficulty': 'hard',
            # intentionally omit problemTypes
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }
        edit_resp = self.client.post(
            reverse('problem_edit_v2', args=[code]),
            data=json.dumps(edit_payload),
            content_type='application/json',
        )
        self.assertEqual(edit_resp.status_code, 200, edit_resp.content)
        problem.refresh_from_db()
        problem.__dict__.pop('difficulty', None)
        self.assertEqual(problem.difficulty, 'hard')
        self.assertIn(knowledge, list(problem.types.all()))

    def test_create_without_problem_type_is_rejected(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'type': 'single',
            'name': 'No type',
            'description': 'missing tag',
            'points': 5,
            'group_id': self.group.id,
            'difficulty': 'medium',
            'problemTypes': [],
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('problem tag', response.json()['error'].lower())
        self.assertFalse(Problem.objects.filter(name='No type').exists())

    def test_create_omitting_problem_types_key_is_rejected(self):
        self.client.force_login(self.users['creator'])
        payload = {
            'type': 'single',
            'name': 'Omitted tag key',
            'description': 'no problemTypes key',
            'points': 5,
            'group_id': self.group.id,
            'difficulty': 'medium',
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('problem tag', response.json()['error'].lower())
        self.assertFalse(Problem.objects.filter(name='Omitted tag key').exists())

    def test_edit_explicit_empty_problem_types_is_rejected(self):
        knowledge = ProblemType.objects.create(name='dp-tag', full_name='Dynamic Programming')
        self.client.force_login(self.users['creator'])
        create_payload = {
            'type': 'single',
            'name': 'Clear Tags',
            'description': 'will clear',
            'points': 5,
            'group_id': self.group.id,
            'difficulty': 'medium',
            'problemTypes': ['Dynamic Programming'],
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }
        create_resp = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(create_payload),
            content_type='application/json',
        )
        self.assertEqual(create_resp.status_code, 200, create_resp.content)
        code = create_resp.json()['code']

        edit_payload = {
            'type': 'single',
            'name': 'Clear Tags',
            'description': 'cleared',
            'points': 5,
            'group_id': self.group.id,
            'edit_code': code,
            'difficulty': 'medium',
            'problemTypes': [],
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }
        edit_resp = self.client.post(
            reverse('problem_edit_v2', args=[code]),
            data=json.dumps(edit_payload),
            content_type='application/json',
        )
        self.assertEqual(edit_resp.status_code, 400, edit_resp.content)
        self.assertIn('problem tag', edit_resp.json()['error'].lower())
        problem = Problem.objects.get(code=code)
        problem.__dict__.pop('difficulty', None)
        self.assertEqual(problem.difficulty, 'medium')
        self.assertIn(knowledge, list(problem.types.all()))

    def test_edit_code_keeps_testcases_when_payload_omits_them(self):
        self.client.force_login(self.users['staff_adder'])
        create_payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'programming',
            'name': 'Keep Cases',
            'description': 'has cases',
            'points': 10,
            'group_id': self.group.id,
            'testcases': [
                {'input': '1\n', 'output': '1\n', 'points': 10, 'pretest': False},
            ],
        }
        create_resp = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(create_payload),
            content_type='application/json',
        )
        self.assertEqual(create_resp.status_code, 200)
        code = create_resp.json()['code']
        problem = Problem.objects.get(code=code)
        self.assertEqual(problem.cases.count(), 1)

        edit_payload = {
            'type': 'programming',
            'name': 'Keep Cases Edited',
            'description': 'still has cases',
            'points': 10,
            'group_id': self.group.id,
            'edit_code': code,
            # intentionally omit testcases
        }
        edit_resp = self.client.post(
            reverse('problem_edit_v2', args=[code]),
            data=json.dumps(edit_payload),
            content_type='application/json',
        )
        self.assertEqual(edit_resp.status_code, 200)
        problem.refresh_from_db()
        self.assertEqual(problem.name, 'Keep Cases Edited')
        self.assertEqual(problem.cases.count(), 1)

    def test_create_writes_admin_log_entry(self):
        self.client.force_login(self.users['creator'])
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps({
                'problemTypes': ['Graph Theory'],
                'type': 'single',
                'name': 'Logged create',
                'description': 'Statement text.',
                'points': 5,
                'group_id': self.group.id,
                'options': [
                    {'text': 'A', 'correct': True},
                    {'text': 'B', 'correct': False},
                ],
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        problem = Problem.objects.get(code=response.json()['code'])
        logs = _problem_log_entries(problem, self.users['creator'])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].action_flag, ADDITION)
        self.assertEqual(logs[0].object_repr, 'Logged create')
        self.assertEqual(json.loads(logs[0].change_message), [{'added': {}}])

    def test_edit_writes_admin_log_entry_with_changed_fields(self):
        self.client.force_login(self.users['creator'])
        create_resp = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps({
                'problemTypes': ['Graph Theory'],
                'type': 'single',
                'name': 'Before edit',
                'description': 'Original statement.',
                'points': 5,
                'group_id': self.group.id,
                'options': [
                    {'text': 'A', 'correct': True},
                    {'text': 'B', 'correct': False},
                ],
            }),
            content_type='application/json',
        )
        self.assertEqual(create_resp.status_code, 200, create_resp.content)
        code = create_resp.json()['code']
        problem = Problem.objects.get(code=code)

        edit_resp = self.client.post(
            reverse('problem_edit_v2', args=[code]),
            data=json.dumps({
                'type': 'single',
                'name': 'After edit',
                'description': 'Updated statement.',
                'points': 5,
                'group_id': self.group.id,
                'options': [
                    {'text': 'A', 'correct': True},
                    {'text': 'B', 'correct': False},
                ],
                'edit_code': code,
            }),
            content_type='application/json',
        )
        self.assertEqual(edit_resp.status_code, 200, edit_resp.content)
        logs = _problem_log_entries(problem, self.users['creator'])
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0].action_flag, ADDITION)
        self.assertEqual(logs[1].action_flag, CHANGE)
        problem.refresh_from_db()
        self.assertEqual(logs[1].object_repr, 'After edit')
        fields = json.loads(logs[1].change_message)[0]['changed']['fields']
        self.assertIn('problem name', fields)
        self.assertIn('problem body', fields)

    def test_edit_reason_replaces_structured_change_message(self):
        self.client.force_login(self.users['creator'])
        create_resp = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps({
                'problemTypes': ['Graph Theory'],
                'type': 'single',
                'name': 'Reasoned edit',
                'description': 'Original.',
                'points': 5,
                'group_id': self.group.id,
                'options': [
                    {'text': 'A', 'correct': True},
                    {'text': 'B', 'correct': False},
                ],
            }),
            content_type='application/json',
        )
        self.assertEqual(create_resp.status_code, 200, create_resp.content)
        code = create_resp.json()['code']
        edit_resp = self.client.post(
            reverse('problem_edit_v2', args=[code]),
            data=json.dumps({
                'type': 'single',
                'name': 'Reasoned edit',
                'description': 'Updated.',
                'points': 5,
                'group_id': self.group.id,
                'options': [
                    {'text': 'A', 'correct': True},
                    {'text': 'B', 'correct': False},
                ],
                'edit_code': code,
                'edit_reason': 'Clarify the statement',
            }),
            content_type='application/json',
        )
        self.assertEqual(edit_resp.status_code, 200, edit_resp.content)
        problem = Problem.objects.get(code=code)
        change = _problem_log_entries(problem, self.users['creator'])[-1]
        self.assertEqual(change.action_flag, CHANGE)
        self.assertEqual(change.change_message, 'Clarify the statement')


class ProblemCreateV2TestcaseZipBridgeTestCase(CommonDataMixin, TestCase):
    """create-v2 loose-file path vs test_data zip archive interoperability."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.group = default_problem_group()
        cls.users.update({
            'staff_adder': create_user(
                username='code_zip_bridge',
                is_staff=True,
                user_permissions=('add_problem', 'create_private_problem', 'change_public_visibility',
                                  'edit_own_problem'),
            ),
        })

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_location = problem_data_storage.location
        problem_data_storage.location = self._tmpdir.name

    def tearDown(self):
        problem_data_storage.location = self._old_location
        self._tmpdir.cleanup()

    def _make_code_problem(self):
        self.client.force_login(self.users['staff_adder'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'programming',
            'name': 'Zip Bridge',
            'description': 'bridge',
            'points': 10,
            'group_id': self.group.id,
            'testcases': [
                {'input': '1\n', 'output': '1\n', 'points': 10, 'pretest': False},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        return Problem.objects.get(code=response.json()['code'])

    def test_read_testcases_from_zip_when_loose_missing(self):
        problem = self._make_code_problem()
        # Replace loose files with a zip-backed case layout (as test_data would).
        for name in ('01.in', '01.out'):
            try:
                problem_data_storage.delete('%s/%s' % (problem.code, name))
            except Exception:
                pass
        buf = BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('sample.in', '9 9\n')
            zf.writestr('sample.out', '18\n')
        buf.seek(0)
        data, _ = ProblemData.objects.get_or_create(problem=problem)
        data.zipfile.save('data.zip', ContentFile(buf.read()), save=True)
        problem.cases.all().delete()
        ProblemTestCase.objects.create(
            dataset=problem, order=1, type='C',
            input_file='sample.in', output_file='sample.out',
            points=10, is_pretest=False,
        )
        ProblemTestCase.objects.create(
            dataset=problem, order=2, type='S',
            input_file='', output_file='', points=20, is_pretest=False,
        )
        ProblemTestCase.objects.create(
            dataset=problem, order=3, type='E',
            input_file='', output_file='', points=None, is_pretest=False,
        )

        view = ProblemCreateV2View()
        cases = view._read_testcases(problem)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]['input'], '9 9\n')
        self.assertEqual(cases[0]['output'], '18\n')

    def test_save_testcases_rebuilds_zip_for_classic_editor(self):
        problem = self._make_code_problem()
        buf = BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('old.in', 'old\n')
            zf.writestr('old.out', 'old\n')
        buf.seek(0)
        data, _ = ProblemData.objects.get_or_create(problem=problem)
        data.zipfile.save('legacy.zip', ContentFile(buf.read()), save=True)
        self.assertTrue(bool(data.zipfile))

        view = ProblemCreateV2View()
        problem.cases.all().delete()
        view._save_testcases(problem, [
            {'input': '2 3\n', 'output': '5\n', 'points': 10, 'pretest': True},
        ], checker='standard')
        data.refresh_from_db()
        self.assertTrue(bool(data.zipfile))
        with problem_data_storage.open('%s/01.in' % problem.code) as fh:
            self.assertEqual(fh.read().decode('utf-8'), '2 3\n')
        with problem_data_storage.open('%s/01.out' % problem.code) as fh:
            self.assertEqual(fh.read().decode('utf-8'), '5\n')
        # create-v2 now rebuilds data.zip so classic test_data stays in sync
        yml_path = os.path.join(problem_data_storage.location, problem.code, 'init.yml')
        if os.path.exists(yml_path):
            with open(yml_path, 'r', encoding='utf-8') as fh:
                self.assertIn('archive', fh.read())
        case = problem.cases.get(order=1)
        self.assertEqual(case.input_file, '01.in')
        self.assertTrue(case.is_pretest)
        data.refresh_from_db()
        self.assertEqual(data.checker, 'standard')

    def test_save_testcases_keeps_zip_filenames(self):
        problem = self._make_code_problem()
        view = ProblemCreateV2View()
        problem.cases.all().delete()
        view._save_testcases(problem, [
            {
                'input': '1\n', 'output': '2\n', 'points': 5, 'pretest': False,
                'input_file': 'sample.in', 'output_file': 'sample.out',
            },
        ], checker='floats')
        case = problem.cases.get(order=1)
        self.assertEqual(case.input_file, 'sample.in')
        self.assertEqual(case.output_file, 'sample.out')
        data = problem.data_files
        data.refresh_from_db()
        self.assertEqual(data.checker, 'floats')
        self.assertTrue(bool(data.zipfile))
        with zipfile.ZipFile(data.zipfile.path) as zf:
            self.assertIn('sample.in', zf.namelist())
            self.assertEqual(zf.read('sample.in').decode('utf-8'), '1\n')

    def test_zip_keeps_user_file_matches(self):
        """Classic test_data: zip is stored as-is; cases use the files the user picked."""
        self.client.force_login(self.users['staff_adder'])
        buf = BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('odd.txt', '1\n')
            zf.writestr('even.txt', '2\n')
            zf.writestr('notes.md', 'ignore\n')
        buf.seek(0)
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'programming',
            'name': 'Manual match',
            'description': 'user picks files',
            'points': 10,
            'group_id': self.group.id,
            'testcases': [
                {'input_file': 'odd.txt', 'output_file': 'even.txt', 'points': 10, 'pretest': True},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data={
                'payload': json.dumps(payload),
                'testcase_zip': SimpleUploadedFile('cases.zip', buf.read(), content_type='application/zip'),
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        problem = Problem.objects.get(code=response.json()['code'])
        case = problem.cases.get(type='C')
        self.assertEqual(case.input_file, 'odd.txt')
        self.assertEqual(case.output_file, 'even.txt')
        with zipfile.ZipFile(problem.data_files.zipfile.path) as zf:
            self.assertEqual(set(zf.namelist()), {'odd.txt', 'even.txt', 'notes.md'})

    def test_create_code_with_multipart_zip(self):
        """Large ZIP path: metadata in JSON field, archive as multipart file (classic limits)."""
        self.client.force_login(self.users['staff_adder'])
        buf = BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('dat1.in', '1 2\n')
            zf.writestr('dat1.out', '3\n')
            zf.writestr('dat2.in', '4 5\n')
            zf.writestr('dat2.out', '9\n')
        buf.seek(0)

        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'programming',
            'name': 'Multipart Zip',
            'description': 'zip upload',
            'points': 10,
            'group_id': self.group.id,
            'checker': 'standard',
            'testcases': [
                {'input_file': 'dat1.in', 'output_file': 'dat1.out', 'points': 4, 'pretest': True},
                {'input_file': 'dat2.in', 'output_file': 'dat2.out', 'points': 6, 'pretest': False},
            ],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data={
                'payload': json.dumps(payload),
                'testcase_zip': SimpleUploadedFile('cases.zip', buf.read(), content_type='application/zip'),
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body['ok'])
        problem = Problem.objects.get(code=body['code'])
        cases = list(problem.cases.filter(type='C').order_by('order'))
        self.assertEqual(len(cases), 2)
        self.assertEqual(cases[0].input_file, 'dat1.in')
        self.assertEqual(cases[0].points, 4)
        self.assertTrue(cases[0].is_pretest)
        self.assertEqual(cases[1].points, 6)
        data = problem.data_files
        self.assertTrue(bool(data.zipfile))
        self.assertEqual(data.checker, 'standard')
        with zipfile.ZipFile(data.zipfile.path) as zf:
            self.assertEqual(zf.read('dat1.in').decode('utf-8'), '1 2\n')
            self.assertEqual(zf.read('dat2.out').decode('utf-8'), '9\n')

    def test_multipart_zip_allows_unmatched_files(self):
        """Uploading a zip only unpacks it; unmatched members do not have to form pairs."""
        self.client.force_login(self.users['staff_adder'])
        buf = BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('only.in', '1\n')
        buf.seek(0)
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'programming',
            'name': 'Zip only',
            'description': 'x',
            'points': 1,
            'group_id': self.group.id,
            'testcases': [],
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data={
                'payload': json.dumps(payload),
                'testcase_zip': SimpleUploadedFile('lone.zip', buf.read(), content_type='application/zip'),
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertFalse(problem.cases.filter(type='C').exists())
        self.assertTrue(bool(problem.data_files.zipfile))

    def test_programming_saves_harness(self):
        lang = Language.objects.filter(common_name='Python').first()
        self.assertIsNotNone(lang)
        self.client.force_login(self.users['staff_adder'])
        payload = {
            'problemTypes': ['Graph Theory'],
            'type': 'programming',
            'name': 'Harnessed',
            'description': 'with harness',
            'points': 5,
            'group_id': self.group.id,
            'testcases': [
                {'input': '1\n', 'output': '1\n', 'points': 10, 'pretest': False},
            ],
            'harness': {
                'language_id': lang.id,
                'harness_code': 'def check():\n    pass\n',
                'skip_precompile': True,
                'run_student_main': False,
            },
        }
        response = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        problem = Problem.objects.get(code=response.json()['code'])
        harness = ProblemHarness.objects.get(problem=problem)
        self.assertEqual(harness.language_id, lang.id)
        self.assertTrue(harness.skip_precompile)
        self.assertIn('def check', harness.harness_code)
        cfg = self._harness_init_cfg(problem, harness.language.key)
        self.assertIn('def check', cfg['code'])
        self.assertTrue(cfg['skip_precompile'])
        self.assertNotIn('run_student_main', cfg)

    def _harness_init_cfg(self, problem, lang_key):
        path = os.path.join(problem_data_storage.location, problem.code, 'init.yml')
        self.assertTrue(os.path.exists(path), 'init.yml was not written for %s' % problem.code)
        with open(path, encoding='utf-8') as fh:
            init = yaml.safe_load(fh)
        self.assertIn('harness_grader', init)
        self.assertIn(lang_key, init['harness_grader'])
        return init['harness_grader'][lang_key]

    def test_edit_harness_updates_init_yml(self):
        lang = Language.objects.filter(common_name='Python').first()
        self.assertIsNotNone(lang)
        problem = self._make_code_problem()
        create_payload = {
            'type': 'programming',
            'name': problem.name,
            'description': problem.description,
            'points': 5,
            'group_id': self.group.id,
            'edit_code': problem.code,
            'testcases': [
                {'input': '1\n', 'output': '1\n', 'points': 10, 'pretest': False},
            ],
            'harness': {
                'language_id': lang.id,
                'harness_code': 'OLD_HARNESS = True\n',
                'skip_precompile': False,
                'run_student_main': False,
            },
        }
        create_resp = self.client.post(
            reverse('problem_edit_v2', args=[problem.code]),
            data=json.dumps(create_payload),
            content_type='application/json',
        )
        self.assertEqual(create_resp.status_code, 200, create_resp.content)
        cfg = self._harness_init_cfg(problem, lang.key)
        self.assertIn('OLD_HARNESS', cfg['code'])
        self.assertNotIn('skip_precompile', cfg)

        edit_payload = {
            'type': 'programming',
            'name': problem.name,
            'description': problem.description,
            'points': 5,
            'group_id': self.group.id,
            'edit_code': problem.code,
            'testcases': [
                {'input': '1\n', 'output': '1\n', 'points': 10, 'pretest': False},
            ],
            'harness': {
                'language_id': lang.id,
                'harness_code': 'NEW_HARNESS = True\n',
                'skip_precompile': True,
                'run_student_main': True,
            },
        }
        edit_resp = self.client.post(
            reverse('problem_edit_v2', args=[problem.code]),
            data=json.dumps(edit_payload),
            content_type='application/json',
        )
        self.assertEqual(edit_resp.status_code, 200, edit_resp.content)
        harness = ProblemHarness.objects.get(problem=problem)
        self.assertIn('NEW_HARNESS', harness.harness_code)
        cfg = self._harness_init_cfg(problem, lang.key)
        self.assertIn('NEW_HARNESS', cfg['code'])
        self.assertNotIn('OLD_HARNESS', cfg['code'])
        self.assertTrue(cfg['skip_precompile'])
        self.assertTrue(cfg['run_student_main'])

    def test_clear_harness_removes_grader_from_init_yml(self):
        lang = Language.objects.filter(common_name='Python').first()
        self.assertIsNotNone(lang)
        problem = self._make_code_problem()
        self.client.post(
            reverse('problem_edit_v2', args=[problem.code]),
            data=json.dumps({
                'type': 'programming',
                'name': problem.name,
                'description': problem.description,
                'points': 5,
                'group_id': self.group.id,
                'edit_code': problem.code,
                'testcases': [
                    {'input': '1\n', 'output': '1\n', 'points': 10, 'pretest': False},
                ],
                'harness': {
                    'language_id': lang.id,
                    'harness_code': 'def check():\n    pass\n',
                    'skip_precompile': True,
                },
            }),
            content_type='application/json',
        )
        self._harness_init_cfg(problem, lang.key)

        clear_resp = self.client.post(
            reverse('problem_edit_v2', args=[problem.code]),
            data=json.dumps({
                'type': 'programming',
                'name': problem.name,
                'description': problem.description,
                'points': 5,
                'group_id': self.group.id,
                'edit_code': problem.code,
                'testcases': [
                    {'input': '1\n', 'output': '1\n', 'points': 10, 'pretest': False},
                ],
                'harness': {'delete': True},
            }),
            content_type='application/json',
        )
        self.assertEqual(clear_resp.status_code, 200, clear_resp.content)
        self.assertFalse(ProblemHarness.objects.filter(problem=problem).exists())
        path = os.path.join(problem_data_storage.location, problem.code, 'init.yml')
        self.assertTrue(os.path.exists(path))
        with open(path, encoding='utf-8') as fh:
            init = yaml.safe_load(fh)
        self.assertNotIn('harness_grader', init or {})

    def test_harness_only_edit_updates_init_yml(self):
        lang = Language.objects.filter(common_name='Python').first()
        self.assertIsNotNone(lang)
        problem = self._make_code_problem()
        self.client.post(
            reverse('problem_edit_v2', args=[problem.code]),
            data=json.dumps({
                'type': 'programming',
                'name': problem.name,
                'description': problem.description,
                'points': 5,
                'group_id': self.group.id,
                'edit_code': problem.code,
                'testcases': [
                    {'input': '1\n', 'output': '1\n', 'points': 10, 'pretest': False},
                ],
                'harness': {
                    'language_id': lang.id,
                    'harness_code': 'FIRST = 1\n',
                },
            }),
            content_type='application/json',
        )
        omit_cases = self.client.post(
            reverse('problem_edit_v2', args=[problem.code]),
            data=json.dumps({
                'type': 'programming',
                'name': problem.name,
                'description': problem.description,
                'points': 5,
                'group_id': self.group.id,
                'edit_code': problem.code,
                'harness': {
                    'language_id': lang.id,
                    'harness_code': 'SECOND = 2\n',
                    'run_student_main': True,
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(omit_cases.status_code, 200, omit_cases.content)
        self.assertEqual(problem.cases.count(), 1)
        cfg = self._harness_init_cfg(problem, lang.key)
        self.assertIn('SECOND', cfg['code'])
        self.assertTrue(cfg['run_student_main'])

    def test_edit_page_links_to_test_data(self):
        problem = self._make_code_problem()
        self.client.force_login(self.users['staff_adder'])
        response = self.client.get(reverse('problem_edit_v2', args=[problem.code]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('problem_data', args=[problem.code]))
        self.assertContains(response, 'Edit test data')
        self.assertContains(response, 'Harness')
        self.assertContains(response, 'id="f-harness-monaco"')
        self.assertContains(response, 'libs/monaco/vs')


class ProblemCreateV2CapitalizedDifficultyTestCase(CommonDataMixin, TestCase):
    """Reuse Admin-seeded Easy/Medium/Hard category IDs instead of requiring lowercase names."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.group = default_problem_group()
        cls.users.update({
            'staff_adder': create_user(
                username='caps_diff_creator',
                is_staff=True,
                user_permissions=('add_problem', 'create_private_problem', 'change_public_visibility',
                                  'edit_own_problem'),
            ),
        })
        ProblemType.objects.create(name='Easy', full_name='Easy')
        ProblemType.objects.create(name='Hard', full_name='Hard')
        ProblemType.objects.create(name='impl', full_name='Implementation')

    def _save_programming(self, name, difficulty):
        return self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps({
                'type': 'programming',
                'name': name,
                'description': 'difficulty should display after save',
                'points': 10,
                'is_public': True,
                'group_id': self.group.id,
                'difficulty': difficulty,
                'problemTypes': ['Implementation'],
                'testcases': [
                    {'input': '1\n', 'output': '1\n', 'points': 10, 'pretest': False},
                ],
            }),
            content_type='application/json',
        )

    def test_save_easy_reuses_capitalized_type(self):
        self.client.force_login(self.users['staff_adder'])
        response = self._save_programming('test-program-noOrg', 'easy')
        self.assertEqual(response.status_code, 200, response.content)
        problem = Problem.objects.get(code=response.json()['code'])
        type_names = list(problem.types.values_list('name', flat=True))
        self.assertEqual(type_names.count('Easy'), 1)
        self.assertNotIn('easy', type_names)
        self.assertEqual(problem.difficulty, 'easy')
        self.assertEqual(ProblemType.objects.filter(name__iexact='easy').count(), 1)

        list_resp = self.client.get(reverse('problem_list'))
        self.assertEqual(list_resp.status_code, 200)
        self.assertContains(list_resp, 'diff-easy', html=False)

    def test_save_hard_reuses_capitalized_type(self):
        self.client.force_login(self.users['staff_adder'])
        response = self._save_programming('test-program-hard', 'hard')
        self.assertEqual(response.status_code, 200, response.content)
        problem = Problem.objects.get(code=response.json()['code'])
        type_names = list(problem.types.values_list('name', flat=True))
        self.assertEqual(type_names.count('Hard'), 1)
        self.assertNotIn('hard', type_names)
        self.assertEqual(problem.difficulty, 'hard')
        self.assertEqual(ProblemType.objects.filter(name__iexact='hard').count(), 1)

        list_resp = self.client.get(reverse('problem_list'))
        self.assertEqual(list_resp.status_code, 200)
        self.assertContains(list_resp, 'diff-hard', html=False)


class ProblemCreateV2SaveRedirectTestCase(CommonDataMixin, TestCase):
    """Save stays on the same tab: JSON redirect to the solving page, then Edit is a normal link."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.group = default_problem_group()
        cls.users.update({
            'creator': create_user(
                username='save_redirect_creator',
                user_permissions=('create_private_problem', 'edit_own_problem'),
            ),
        })

    def _create_payload(self, name='Save redirect problem'):
        return {
            'problemTypes': ['Graph Theory'],
            'type': 'single',
            'name': name,
            'description': 'After save, Edit must still work.',
            'points': 5,
            'group_id': self.group.id,
            'options': [
                {'text': 'A', 'correct': True},
                {'text': 'B', 'correct': False},
            ],
        }

    def test_create_page_uses_same_tab_redirect(self):
        self.client.force_login(self.users['creator'])
        response = self.client.get(reverse('problem_create_v2'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'location.href = next')
        self.assertNotContains(response, 'problem_window.js')
        self.assertNotContains(response, 'DKUOJProblemWindow')
        self.assertNotContains(response, 'openPreviewPlaceholder')
        self.assertNotContains(response, 'dkuoj-problem-preview')

    def test_save_redirects_to_detail_and_edit_is_ordinary_link(self):
        self.client.force_login(self.users['creator'])
        created = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(self._create_payload()),
            content_type='application/json',
        )
        self.assertEqual(created.status_code, 200, created.content)
        body = created.json()
        code = body['code']
        detail_url = reverse('problem_detail', args=[code])
        edit_url = reverse('problem_edit_v2', args=[code])
        self.assertTrue(body['ok'])
        self.assertEqual(body['redirect'], detail_url)
        self.assertNotIn('detail_url', body)
        self.assertNotIn('edit_url', body)
        self.assertNotIn('list_url', body)

        problem = Problem.objects.get(code=code)
        self.assertTrue(problem.is_editable_by(self.users['creator']))
        self.assertTrue(problem.is_accessible_by(self.users['creator']))

        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'data-problem-actions')
        self.assertContains(detail, 'data-problem-action="edit"')
        self.assertContains(detail, edit_url)
        self.assertNotContains(detail, 'problem_window.js')
        self.assertNotContains(detail, 'No such problem')

        edit = self.client.get(edit_url)
        self.assertEqual(edit.status_code, 200)
        self.assertEqual(edit.context['edit_code'], code)
        self.assertContains(edit, 'location.href = next')
        self.assertNotContains(edit, 'problem_window.js')

    def test_student_does_not_see_edit_or_delete(self):
        self.client.force_login(self.users['creator'])
        created = self.client.post(
            reverse('problem_create_v2'),
            data=json.dumps(self._create_payload('Public for student')),
            content_type='application/json',
        )
        self.assertEqual(created.status_code, 200, created.content)
        code = created.json()['code']
        problem = Problem.objects.get(code=code)
        problem.is_public = True
        problem.is_organization_private = False
        problem.save()

        student = create_user(username='save_redirect_student')
        self.client.force_login(student)
        detail = self.client.get(reverse('problem_detail', args=[code]))
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, 'data-problem-action="edit"')
        self.assertNotContains(detail, 'data-can-delete="1"')
        self.assertNotContains(detail, reverse('problem_edit_v2', args=[code]))

        edit = self.client.get(reverse('problem_edit_v2', args=[code]))
        self.assertEqual(edit.status_code, 404)
