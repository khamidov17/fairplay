import io
import json
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from judge.forms import MCQSubmitForm
from judge.models import (
    Class, Language, Organization, Problem, ProblemMCQConfig, ProblemMCQOption,
    Submission, SubmissionSource,
)
from judge.models.tests.util import CommonDataMixin, create_problem, create_user
from judge.submission_grading import grade_internally
from judge.utils.simple_xlsx import build_xlsx_workbook, read_xlsx_rows


def option_post_data(rows, multiple=False, shuffle=False, **problem_fields):
    """Build POST data for the MCQ create/edit form + option formset."""
    data = {
        'name': problem_fields.get('name', 'Quiz Q1'),
        'description': problem_fields.get('description', 'What is 2+2?'),
        'points': problem_fields.get('points', 10),
        'options-TOTAL_FORMS': str(len(rows)),
        'options-INITIAL_FORMS': str(problem_fields.get('initial_forms', 0)),
        'options-MIN_NUM_FORMS': '0',
        'options-MAX_NUM_FORMS': '1000',
    }
    if problem_fields.get('partial'):
        data['partial'] = 'on'
    if problem_fields.get('is_public'):
        data['is_public'] = 'on'
    if multiple:
        data['multiple_select'] = 'on'
    if shuffle:
        data['shuffle_options'] = 'on'
    for i, (text, correct) in enumerate(rows):
        data['options-%d-text' % i] = text
        data['options-%d-order' % i] = str(i)
        data['options-%d-id' % i] = ''
        if correct:
            data['options-%d-is_correct' % i] = 'on'
    return data


class MCQAuthoringViewTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.users.update({
            'creator': create_user(
                username='creator',
                user_permissions=('create_private_problem', 'edit_own_problem'),
            ),
            'staff_adder': create_user(
                username='staff_adder',
                is_staff=True,
                user_permissions=('add_problem', 'create_private_problem'),
            ),
            'publisher': create_user(
                username='publisher',
                user_permissions=('create_private_problem', 'change_public_visibility', 'edit_own_problem'),
            ),
        })

    def test_add_menu_reflects_permissions(self):
        v2_url = reverse('problem_create_v2').encode()
        contest_url = reverse('admin:judge_contest_add').encode()

        self.client.force_login(self.users['creator'])
        content = self.client.get(reverse('problem_list')).content
        self.assertIn(b'Add problem', content)
        self.assertIn(v2_url, content)
        self.assertNotIn(contest_url, content)

        self.client.force_login(self.users['staff_adder'])
        content = self.client.get(reverse('problem_list')).content
        self.assertIn(v2_url, content)

        self.client.force_login(self.users['normal'])
        content = self.client.get(reverse('problem_list')).content
        self.assertNotIn(b'Add problem', content)
        self.assertNotIn(v2_url, content)

    def test_create_requires_permission(self):
        self.client.force_login(self.users['normal'])
        self.assertEqual(self.client.get(reverse('problem_mcq_create')).status_code, 404)
        self.client.force_login(self.users['creator'])
        self.assertEqual(self.client.get(reverse('problem_mcq_create')).status_code, 200)

    def test_create_page_hides_excel_actions(self):
        self.client.force_login(self.users['creator'])
        response = self.client.get(reverse('problem_mcq_create'))
        self.assertNotContains(response, 'Download Excel Template', html=False)
        self.assertNotContains(response, 'Upload Excel', html=False)
        self.assertNotContains(response, 'id="mcq-excel-input"', html=False)

    def test_excel_actions_require_same_permission_as_create_page(self):
        restricted_creator = create_user(
            username='restricted_creator',
            user_permissions=('edit_own_problem',),
        )
        self.client.force_login(restricted_creator)

        create_url = reverse('problem_mcq_create')
        self.assertEqual(self.client.get(create_url).status_code, 404)
        self.assertEqual(self.client.get('{}?download_excel_template=1'.format(create_url)).status_code, 404)

        workbook = build_xlsx_workbook([
            ('Problems', [[
                'name', 'description', 'points', 'partial', 'is_public',
                'organizations', 'classes', 'multiple_select', 'shuffle_options',
                'options', 'correct_options',
            ]]),
        ])
        response = self.client.post(
            create_url,
            {
                'excel_file': SimpleUploadedFile(
                    'mcq.xlsx',
                    workbook,
                    content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                ),
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_create_flow(self):
        self.client.force_login(self.users['creator'])
        data = option_post_data(
            [('4', True), ('3', False), ('5', False)], name='Two plus two', points=7)
        response = self.client.post(reverse('problem_mcq_create'), data)
        self.assertEqual(response.status_code, 302)

        problem = Problem.objects.get(name='Two plus two')
        # Creating lands the teacher on the new problem itself.
        self.assertEqual(response['Location'], reverse('problem_detail', args=[problem.code]))
        self.assertEqual(problem.submission_type, 'M')
        self.assertEqual(problem.points, 7)
        # Creator is recorded as an author so they can edit it afterwards.
        self.assertIn(self.users['creator'].profile, problem.authors.all())
        # Code-centric required fields were auto-filled.
        self.assertTrue(problem.code)
        self.assertIsNotNone(problem.group_id)
        self.assertEqual(problem.mcq_options.count(), 3)
        self.assertEqual(problem.mcq_options.filter(is_correct=True).count(), 1)
        self.assertTrue(hasattr(problem, 'mcq_config'))

    def test_create_rejects_no_correct_answer(self):
        self.client.force_login(self.users['creator'])
        data = option_post_data([('a', False), ('b', False)], name='Bad quiz')
        response = self.client.post(reverse('problem_mcq_create'), data)
        self.assertEqual(response.status_code, 200)  # re-rendered with error
        self.assertFalse(Problem.objects.filter(name='Bad quiz').exists())
        # The validation prompt must be visible on the front-end page, not only in admin.
        self.assertIn(b'Mark at least one choice as correct', response.content)

    def test_create_with_organization_is_org_private(self):
        org = self.organizations['open']
        self.client.force_login(self.users['creator'])
        data = option_post_data([('4', True), ('3', False)], name='Org quiz')
        data['organizations'] = [org.id]
        response = self.client.post(reverse('problem_mcq_create'), data)
        self.assertEqual(response.status_code, 302)

        problem = Problem.objects.get(name='Org quiz')
        self.assertTrue(problem.is_organization_private)
        self.assertIn(org, problem.organizations.all())

    def test_create_public_non_org_rejected_without_permission(self):
        # create_private_problem alone must not let a teacher publish a fully public problem;
        # mirrors ProblemAdmin.save_model's change_public_visibility guard.
        self.client.force_login(self.users['creator'])
        data = option_post_data([('4', True), ('3', False)], name='Sneaky public', is_public=True)
        response = self.client.post(reverse('problem_mcq_create'), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Problem.objects.filter(name='Sneaky public').exists())

    def test_create_public_org_private_allowed_without_permission(self):
        # The guard is not over-broad: an org-private problem may still be public.
        org = self.organizations['open']
        self.client.force_login(self.users['creator'])
        data = option_post_data([('4', True), ('3', False)], name='Public org quiz', is_public=True)
        data['organizations'] = [org.id]
        response = self.client.post(reverse('problem_mcq_create'), data)
        self.assertEqual(response.status_code, 302)

        problem = Problem.objects.get(name='Public org quiz')
        self.assertTrue(problem.is_public)
        self.assertTrue(problem.is_organization_private)

    def test_create_public_allowed_with_permission(self):
        self.client.force_login(self.users['publisher'])
        data = option_post_data([('4', True), ('3', False)], name='Truly public', is_public=True)
        response = self.client.post(reverse('problem_mcq_create'), data)
        self.assertEqual(response.status_code, 302)

        problem = Problem.objects.get(name='Truly public')
        self.assertTrue(problem.is_public)

    def test_create_rejects_class_without_organization(self):
        org = self.organizations['open']
        cls = Class.objects.create(organization=org, name='Algo 101', slug='algo-101')
        self.client.force_login(self.users['creator'])
        data = option_post_data([('4', True), ('3', False)], name='Classless quiz')
        data['classes'] = [cls.id]  # no organizations selected
        response = self.client.post(reverse('problem_mcq_create'), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Problem.objects.filter(name='Classless quiz').exists())

    def test_create_rejects_single_with_two_correct(self):
        self.client.force_login(self.users['creator'])
        data = option_post_data([('a', True), ('b', True)], name='Ambiguous')
        response = self.client.post(reverse('problem_mcq_create'), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Problem.objects.filter(name='Ambiguous').exists())

    def test_mcq_excel_template_download(self):
        self.client.force_login(self.users['creator'])
        response = self.client.get('{}?download_excel_template=1'.format(reverse('problem_mcq_create')))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertIn('mcq_batch_upload_template.xlsx', response['Content-Disposition'])
        rows = read_xlsx_rows(io.BytesIO(response.content))
        self.assertEqual(
            rows[0],
            [
                'name', 'description', 'points', 'partial', 'is_public',
                'organizations', 'classes', 'multiple_select', 'shuffle_options',
                'options', 'correct_options',
            ],
        )

    def test_mcq_excel_batch_create(self):
        org = self.organizations['open']
        org.admins.add(self.users['creator'].profile)
        cls = Class.objects.create(organization=org, name='MCQ Class', slug='mcq-class')
        cls.admins.add(self.users['creator'].profile)
        self.client.force_login(self.users['creator'])
        workbook = build_xlsx_workbook([
            ('Problems', [
                [
                    'name', 'description', 'points', 'partial', 'is_public',
                    'organizations', 'classes', 'multiple_select', 'shuffle_options',
                    'options', 'correct_options',
                ],
                [
                    'Excel MCQ 1', 'Pick all primes', '8', 'true', 'false',
                    org.slug, cls.slug, 'true', 'true',
                    '2 | 3 | 4', '1 | 2',
                ],
                [
                    'Excel MCQ 2', '2 + 2 = ?', '', 'false', 'false',
                    '', '', 'false', 'false',
                    '3 | 4 | 5', '2',
                ],
                [
                    'Excel MCQ 3', 'Zero-point practice', '0', 'false', 'false',
                    '', '', 'false', 'false',
                    'A | B', '1',
                ],
            ]),
        ])
        response = self.client.post(
            reverse('problem_mcq_create'),
            {
                'excel_file': SimpleUploadedFile(
                    'mcq.xlsx',
                    workbook,
                    content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['message'], 'Created 3 multiple-choice problems from Excel.')
        self.assertEqual(payload['redirect_url'], reverse('problem_list'))
        self.assertEqual(len(payload['created']), 3)

        problem1 = Problem.objects.get(name='Excel MCQ 1')
        self.assertEqual(problem1.submission_type, 'M')
        self.assertEqual(problem1.points, 8)
        self.assertTrue(problem1.partial)
        self.assertTrue(problem1.is_organization_private)
        self.assertCountEqual(problem1.organizations.values_list('slug', flat=True), [org.slug])
        self.assertCountEqual(problem1.classes.values_list('slug', flat=True), [cls.slug])
        self.assertEqual(problem1.mcq_options.count(), 3)
        self.assertEqual(problem1.mcq_options.filter(is_correct=True).count(), 2)
        self.assertTrue(problem1.mcq_config.multiple_select)
        self.assertTrue(problem1.mcq_config.shuffle_options)

        problem2 = Problem.objects.get(name='Excel MCQ 2')
        self.assertEqual(problem2.mcq_options.count(), 3)
        self.assertEqual(problem2.mcq_options.filter(is_correct=True).count(), 1)
        self.assertFalse(problem2.mcq_config.multiple_select)

        problem3 = Problem.objects.get(name='Excel MCQ 3')
        self.assertEqual(problem3.points, 0)
        self.assertEqual(problem3.mcq_options.count(), 2)
        self.assertEqual(problem3.mcq_options.filter(is_correct=True).count(), 1)
        self.assertFalse(problem3.mcq_config.multiple_select)

    def test_mcq_excel_batch_create_rejects_organization_outside_visible_scope(self):
        hidden_org = Organization.objects.create(
            name='Hidden Org',
            slug='hidden-org',
            short_name='HORG',
            about='hidden',
        )
        self.client.force_login(self.users['creator'])
        workbook = build_xlsx_workbook([
            ('Problems', [
                [
                    'name', 'description', 'points', 'partial', 'is_public',
                    'organizations', 'classes', 'multiple_select', 'shuffle_options',
                    'options', 'correct_options',
                ],
                [
                    'Excel MCQ Hidden', 'Pick one', '5', 'false', 'false',
                    hidden_org.slug, '', 'false', 'false',
                    'A | B', '1',
                ],
            ]),
        ])
        response = self.client.post(
            reverse('problem_mcq_create'),
            {
                'excel_file': SimpleUploadedFile(
                    'mcq.xlsx',
                    workbook,
                    content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                ),
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertJSONEqual(
            response.content.decode(),
            {
                'ok': False,
                'message': 'Organization "hidden-org" was not found.',
            },
        )
        self.assertFalse(Problem.objects.filter(name='Excel MCQ Hidden').exists())

    def test_edit_permission_and_manually_managed(self):
        problem = create_problem(
            code='editmcq', submission_type='M', is_public=True, is_manually_managed=True,
            authors=('creator',))
        ProblemMCQConfig.objects.create(problem=problem)
        ProblemMCQOption.objects.create(problem=problem, order=0, text='a', is_correct=True)

        # Non-editor -> 404.
        self.client.force_login(self.users['normal'])
        self.assertEqual(self.client.get(reverse('problem_mcq_edit', args=['editmcq'])).status_code, 404)

        # Editor -> 200, even though is_manually_managed is set (that guard must not apply).
        self.client.force_login(self.users['creator'])
        self.assertEqual(self.client.get(reverse('problem_mcq_edit', args=['editmcq'])).status_code, 200)


class MCQSubmitViewTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.problem = create_problem(code='quizsub', submission_type='M', points=10, is_public=True)
        ProblemMCQConfig.objects.create(problem=self.problem, multiple_select=False)
        self.opt_a = ProblemMCQOption.objects.create(problem=self.problem, order=0, text='wrong', is_correct=False)
        self.opt_b = ProblemMCQOption.objects.create(problem=self.problem, order=1, text='right', is_correct=True)

    def test_submit_correct_grades_internally(self):
        self.client.force_login(self.users['normal'])
        response = self.client.post(reverse('problem_submit', args=['quizsub']), {'options': str(self.opt_b.id)})
        self.assertEqual(response.status_code, 302)

        sub = Submission.objects.get(problem=self.problem, user=self.users['normal'].profile)
        self.assertEqual(sub.status, 'D')
        self.assertEqual(sub.result, 'AC')
        self.assertEqual(sub.points, 10)
        self.assertEqual(sub.language.key, 'ANSWER')
        self.assertEqual(json.loads(sub.source.source)['selected'], [self.opt_b.id])

    def test_submit_wrong(self):
        self.client.force_login(self.users['normal'])
        self.client.post(reverse('problem_submit', args=['quizsub']), {'options': str(self.opt_a.id)})
        sub = Submission.objects.get(problem=self.problem, user=self.users['normal'].profile)
        self.assertEqual(sub.result, 'WA')
        self.assertEqual(sub.points, 0)

    def test_mcq_not_blocked_by_other_problems_in_flight_code(self):
        """MCQ must not hit the judge-queue cap from unrelated code submissions."""
        from judge.models import Judge
        from judge.models.tests.util import create_problem

        other = create_problem(code='queueother', is_public=True, allowed_languages=('PY3',))
        lang = Language.objects.get(key='PY3')
        judge = Judge.objects.create(name='mcqguardjudge', auth_key='mcqguardkey', online=True)
        judge.problems.add(other)
        judge.runtimes.add(lang)
        for _ in range(2):
            Submission.objects.create(
                user=self.users['normal'].profile, problem=other, language=lang,
                status='P', result='QU',
            )

        self.client.force_login(self.users['normal'])
        response = self.client.post(
            reverse('problem_submit', args=['quizsub']),
            {'options': str(self.opt_b.id), 'workspace': '1'},
            HTTP_X_WORKSPACE_SUBMIT='1',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(
            Submission.objects.filter(problem=self.problem, user=self.users['normal'].profile).exists(),
        )

    def test_submit_page_does_not_leak_answer_key(self):
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('problem_submit', args=['quizsub']))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'is_correct', response.content)

    def test_problem_page_shows_choices_not_code_editor(self):
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('problem_detail', args=['quizsub']))
        self.assertEqual(response.status_code, 200)
        # No Ace code editor on an MCQ problem page; the choices are shown instead.
        self.assertNotIn(b'id_source', response.content)
        self.assertIn(b'name="options"', response.content)

    def test_option_fenced_code_renders_as_code_block(self):
        problem = create_problem(code='quizmd', submission_type='M', points=10, is_public=True)
        ProblemMCQConfig.objects.create(problem=problem, multiple_select=False)
        ProblemMCQOption.objects.create(
            problem=problem, order=0,
            text='```java\nint i = 0;\nSystem.out.print(i);\n```',
            is_correct=False,
        )
        ProblemMCQOption.objects.create(problem=problem, order=1, text='A plain choice', is_correct=True)

        form = MCQSubmitForm(problem=problem, profile=self.users['normal'].profile)
        html = str(form['options'])
        self.assertIn('code-block', html)
        self.assertIn('code-block-lang', html)
        self.assertIn('mcq-option-body', html)
        self.assertIn('System', html)
        self.assertNotIn('```java', html)
        self.assertIn('A plain choice', html)

        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('problem_detail', args=['quizmd']))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'code-block')
        self.assertContains(response, 'System')
        self.assertNotContains(response, '```java')
        self.assertNotContains(response, 'is_correct')

    def test_multiple_select_option_fenced_code_renders(self):
        problem = create_problem(code='quizmdmulti', submission_type='M', points=10, is_public=True)
        ProblemMCQConfig.objects.create(problem=problem, multiple_select=True)
        ProblemMCQOption.objects.create(
            problem=problem, order=0,
            text='```\nfor (int i = 1; i <= 5; i++) {}\n```',
            is_correct=True,
        )
        ProblemMCQOption.objects.create(problem=problem, order=1, text='Other', is_correct=False)

        form = MCQSubmitForm(problem=problem, profile=self.users['normal'].profile)
        html = str(form['options'])
        self.assertIn('type="checkbox"', html)
        self.assertIn('<pre', html)
        self.assertIn('for (int i = 1; i &lt;= 5; i++)', html)
        self.assertNotIn('```', html)

    def test_rejudge_does_not_hit_bridge(self):
        sub = Submission.objects.create(
            user=self.users['normal'].profile, problem=self.problem,
            language=Submission._meta.get_field('language').related_model.get_answer_language(),
        )
        SubmissionSource.objects.create(submission=sub, source=json.dumps({'selected': [self.opt_b.id]}))
        grade_internally(sub)

        with mock.patch('judge.models.submission.judge_submission') as bridge:
            sub.judge(rejudge=True, force_judge=True)
        bridge.assert_not_called()
        sub.refresh_from_db()
        self.assertEqual(sub.result, 'AC')
        self.assertNotEqual(sub.result, 'IE')
