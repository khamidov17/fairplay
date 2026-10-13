import json
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from judge.models import Class, Problem, ProblemBlank, ProblemFillBlankConfig, Submission, SubmissionSource
from judge.models.tests.util import CommonDataMixin, create_problem, create_user
from judge.submission_grading import grade_internally


def blank_post_data(rows, case_sensitive=False, trim_whitespace=True, **problem_fields):
    """Build POST data for the fill-blank create/edit form + blank formset.

    ``rows`` is a list of (label, answers) tuples.
    """
    data = {
        'name': problem_fields.get('name', 'Fill Q1'),
        'description': problem_fields.get('description', 'The capital of France is ___.'),
        'points': problem_fields.get('points', 10),
        'blanks-TOTAL_FORMS': str(len(rows)),
        'blanks-INITIAL_FORMS': str(problem_fields.get('initial_forms', 0)),
        'blanks-MIN_NUM_FORMS': '0',
        'blanks-MAX_NUM_FORMS': '1000',
    }
    if problem_fields.get('partial'):
        data['partial'] = 'on'
    if problem_fields.get('is_public'):
        data['is_public'] = 'on'
    if case_sensitive:
        data['case_sensitive'] = 'on'
    if trim_whitespace:
        data['trim_whitespace'] = 'on'
    for i, (label, answers) in enumerate(rows):
        data['blanks-%d-label' % i] = label
        data['blanks-%d-answers' % i] = answers
        data['blanks-%d-order' % i] = str(i)
        data['blanks-%d-id' % i] = ''
    return data


class FillBlankAuthoringViewTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.users.update({
            'creator': create_user(
                username='creator',
                user_permissions=('create_private_problem', 'edit_own_problem'),
            ),
            'publisher': create_user(
                username='publisher',
                user_permissions=('create_private_problem', 'change_public_visibility', 'edit_own_problem'),
            ),
        })

    def test_add_menu_reflects_permissions(self):
        # The add-problem entry point is now the unified create-v2 tab, not a per-type link.
        v2_url = reverse('problem_create_v2').encode()

        self.client.force_login(self.users['creator'])
        content = self.client.get(reverse('problem_list')).content
        self.assertIn(b'Add problem', content)
        self.assertIn(v2_url, content)

        self.client.force_login(self.users['normal'])
        content = self.client.get(reverse('problem_list')).content
        self.assertNotIn(v2_url, content)

    def test_create_requires_permission(self):
        self.client.force_login(self.users['normal'])
        self.assertEqual(self.client.get(reverse('problem_fillblank_create')).status_code, 404)
        self.client.force_login(self.users['creator'])
        self.assertEqual(self.client.get(reverse('problem_fillblank_create')).status_code, 200)

    def test_create_flow(self):
        self.client.force_login(self.users['creator'])
        data = blank_post_data(
            [('', 'Paris'), ('', '1789\nseventeen eighty-nine')], name='Geo quiz', points=7)
        response = self.client.post(reverse('problem_fillblank_create'), data)
        self.assertEqual(response.status_code, 302)

        problem = Problem.objects.get(name='Geo quiz')
        self.assertEqual(response['Location'], reverse('problem_detail', args=[problem.code]))
        self.assertEqual(problem.submission_type, 'B')
        self.assertEqual(problem.points, 7)
        self.assertIn(self.users['creator'].profile, problem.authors.all())
        self.assertTrue(problem.code)
        self.assertIsNotNone(problem.group_id)
        self.assertEqual(problem.blanks.count(), 2)
        self.assertTrue(hasattr(problem, 'fill_blank_config'))

    def test_create_rejects_no_blanks(self):
        # Zero blank rows: the formset is valid but our validator must reject the problem.
        self.client.force_login(self.users['creator'])
        data = blank_post_data([], name='Empty quiz')
        response = self.client.post(reverse('problem_fillblank_create'), data)
        self.assertEqual(response.status_code, 200)  # re-rendered with error
        self.assertFalse(Problem.objects.filter(name='Empty quiz').exists())
        self.assertIn(b'Add at least one blank', response.content)

    def test_create_with_organization_is_org_private(self):
        org = self.organizations['open']
        self.client.force_login(self.users['creator'])
        data = blank_post_data([('', 'Paris')], name='Org quiz')
        data['organizations'] = [org.id]
        response = self.client.post(reverse('problem_fillblank_create'), data)
        self.assertEqual(response.status_code, 302)

        problem = Problem.objects.get(name='Org quiz')
        self.assertTrue(problem.is_organization_private)
        self.assertIn(org, problem.organizations.all())

    def test_create_public_non_org_rejected_without_permission(self):
        self.client.force_login(self.users['creator'])
        data = blank_post_data([('', 'Paris')], name='Sneaky public', is_public=True)
        response = self.client.post(reverse('problem_fillblank_create'), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Problem.objects.filter(name='Sneaky public').exists())

    def test_create_public_org_private_allowed_without_permission(self):
        org = self.organizations['open']
        self.client.force_login(self.users['creator'])
        data = blank_post_data([('', 'Paris')], name='Public org quiz', is_public=True)
        data['organizations'] = [org.id]
        response = self.client.post(reverse('problem_fillblank_create'), data)
        self.assertEqual(response.status_code, 302)

        problem = Problem.objects.get(name='Public org quiz')
        self.assertTrue(problem.is_public)
        self.assertTrue(problem.is_organization_private)

    def test_create_public_allowed_with_permission(self):
        self.client.force_login(self.users['publisher'])
        data = blank_post_data([('', 'Paris')], name='Truly public', is_public=True)
        response = self.client.post(reverse('problem_fillblank_create'), data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Problem.objects.get(name='Truly public').is_public)

    def test_create_rejects_class_without_organization(self):
        org = self.organizations['open']
        cls = Class.objects.create(organization=org, name='Algo 101', slug='algo-101')
        self.client.force_login(self.users['creator'])
        data = blank_post_data([('', 'Paris')], name='Classless quiz')
        data['classes'] = [cls.id]  # no organizations selected
        response = self.client.post(reverse('problem_fillblank_create'), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Problem.objects.filter(name='Classless quiz').exists())

    def test_edit_permission_and_wrong_type(self):
        problem = create_problem(
            code='editfb', submission_type='B', is_public=True, authors=('creator',))
        ProblemFillBlankConfig.objects.create(problem=problem)
        ProblemBlank.objects.create(problem=problem, order=0, answers='Paris')

        self.client.force_login(self.users['normal'])
        self.assertEqual(self.client.get(reverse('problem_fillblank_edit', args=['editfb'])).status_code, 404)

        self.client.force_login(self.users['creator'])
        self.assertEqual(self.client.get(reverse('problem_fillblank_edit', args=['editfb'])).status_code, 200)

        # A non-fill-blank problem must not be editable through this view.
        create_problem(code='notfb', submission_type='M', authors=('creator',))
        self.assertEqual(self.client.get(reverse('problem_fillblank_edit', args=['notfb'])).status_code, 404)

    def test_edit_clearing_answers_removes_blank(self):
        # Regression: clearing an existing blank's answers must delete that ProblemBlank, not leave
        # a stale row with the old accepted answers still affecting grading.
        problem = create_problem(code='clearfb', submission_type='B', authors=('creator',))
        ProblemFillBlankConfig.objects.create(problem=problem)
        b0 = ProblemBlank.objects.create(problem=problem, order=0, answers='Paris')
        b1 = ProblemBlank.objects.create(problem=problem, order=1, answers='1789')

        self.client.force_login(self.users['creator'])
        data = blank_post_data(
            [('', 'Paris'), ('', '')], name=problem.name, initial_forms=2)
        data['blanks-0-id'] = b0.id
        data['blanks-1-id'] = b1.id
        response = self.client.post(reverse('problem_fillblank_edit', args=['clearfb']), data)
        self.assertEqual(response.status_code, 302)

        # The emptied blank is gone; only the answered one survives, re-sequenced to order 0.
        self.assertFalse(ProblemBlank.objects.filter(id=b1.id).exists())
        remaining = list(problem.blanks.all())
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].id, b0.id)
        self.assertEqual(remaining[0].order, 0)


class FillBlankSubmitViewTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.problem = create_problem(code='fbsub', submission_type='B', points=10, partial=True, is_public=True)
        ProblemFillBlankConfig.objects.create(problem=self.problem)
        self.b0 = ProblemBlank.objects.create(problem=self.problem, order=0, answers='Paris')
        self.b1 = ProblemBlank.objects.create(problem=self.problem, order=1, answers='1789')

    def _post(self, **fields):
        return self.client.post(reverse('problem_submit', args=['fbsub']), fields)

    def test_submit_correct_grades_internally(self):
        self.client.force_login(self.users['normal'])
        response = self._post(fb_0='  paris ', fb_1='1789')
        self.assertEqual(response.status_code, 302)

        sub = Submission.objects.get(problem=self.problem, user=self.users['normal'].profile)
        self.assertEqual(sub.status, 'D')
        self.assertEqual(sub.result, 'AC')
        self.assertEqual(sub.points, 10)
        self.assertEqual(sub.language.key, 'ANSWER')
        self.assertEqual(json.loads(sub.source.source)['answers'], ['  paris ', '1789'])

    def test_submit_partial_wrong(self):
        self.client.force_login(self.users['normal'])
        self._post(fb_0='Paris', fb_1='nope')
        sub = Submission.objects.get(problem=self.problem, user=self.users['normal'].profile)
        self.assertEqual(sub.result, 'WA')
        self.assertEqual(sub.points, 5)

    def test_submit_page_does_not_leak_answers(self):
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('problem_submit', args=['fbsub']))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'Paris', response.content)
        self.assertNotIn(b'1789', response.content)

    def test_problem_page_shows_inputs_not_code_editor(self):
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('problem_detail', args=['fbsub']))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'id_source', response.content)
        self.assertIn(b'name="fb_0"', response.content)
        self.assertIn(b'name="fb_1"', response.content)

    def test_fill_blank_inputs_disable_browser_autocomplete(self):
        from judge.forms import FillBlankSubmitForm

        form = FillBlankSubmitForm(problem=self.problem)
        for name in form.fields:
            html = str(form[name])
            self.assertIn('autocomplete="new-password"', html, msg='%s missing autocomplete=new-password' % name)
            self.assertIn('readonly', html, msg='%s missing readonly autofill guard' % name)
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('problem_detail', args=['fbsub']))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'autocomplete="new-password"', response.content)
        self.assertIn(b'name="fb_0"', response.content)

    def test_resubmit_prefills_previous_answers(self):
        self.client.force_login(self.users['normal'])
        self._post(fb_0='Paris', fb_1='nope')
        sub = Submission.objects.get(problem=self.problem, user=self.users['normal'].profile)
        # Use the dedicated resubmit route so old_submission is populated and prefills the form.
        response = self.client.get(reverse('problem_submit', args=['fbsub', sub.id]))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'value="Paris"', response.content)
        self.assertIn(b'value="nope"', response.content)

    def test_rejudge_does_not_hit_bridge(self):
        sub = Submission.objects.create(
            user=self.users['normal'].profile, problem=self.problem,
            language=Submission._meta.get_field('language').related_model.get_answer_language(),
        )
        SubmissionSource.objects.create(submission=sub, source=json.dumps({'answers': ['Paris', '1789']}))
        grade_internally(sub)

        with mock.patch('judge.models.submission.judge_submission') as bridge:
            sub.judge(rejudge=True, force_judge=True)
        bridge.assert_not_called()
        sub.refresh_from_db()
        self.assertEqual(sub.result, 'AC')
