"""Independent verification of the P1 fixes (docs/classroom/reports/P1-verification.md).

Each test here showed a path that was still open after the P1 fixes. They were written as
expected failures; all of them are closed now (docs/classroom/reports/P1-gaps.md) and they run as
ordinary regression tests. The comment above each test names the finding.
"""
import json
import os
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from judge.models import Contest, Submission
from judge.models.tests.util import create_contest, create_contest_problem, create_user
from judge.views.tests.test_hidden_results import HiddenResultsBase


class G1RemainingPathsTestCase(HiddenResultsBase):
    """G-1: hidden exam verdicts must not be inferable from any query on the real result/points."""

    def test_precondition(self):
        self.assertTrue(self.sub.contest_results_hidden)
        self.assertTrue(self.mcq.is_public)

    # Closed: G-1: /user/<u>/solved lists hidden exam scores (public problems)
    def test_user_solved_page_skips_hidden_exam_scores(self):
        # Anyone, even anonymous (or the student's own second, logged-out device), can open it.
        response = self.client.get(reverse('user_problems', args=[self.student.username]))
        self.assertEqual(response.status_code, 200)
        codes = {p['code'] for group in response.context['best_submissions'] for p in group['problems']}
        self.assertNotIn(self.mcq.code, codes)

    # Closed: G-1: the performance-points breakdown uses the raw points too
    def test_user_pp_breakdown_skips_hidden_exam_scores(self):
        response = self.client.get(reverse('user_problems', args=[self.student.username]))
        self.assertEqual(response.status_code, 200)
        codes = {entry.problem_code for entry in response.context['pp_breakdown']}
        self.assertNotIn(self.mcq.code, codes)

    # Closed: G-1: /problems/?hide_solved=1 drops the problem on a hidden AC
    def test_problem_list_hide_solved_ignores_hidden_verdict(self):
        # After the exam ends (lock off) but before Finalize, results are still hidden.
        Contest.objects.filter(id=self.contest.id).update(
            start_time=timezone.now() - timedelta(days=2), end_time=timezone.now() - timedelta(hours=1))
        self._login(self.student)
        control = self.client.get(reverse('problem_list'), {'hide_solved': '0'})
        self.assertIn(self.mcq.code, {p.code for p in control.context['problems']})
        response = self.client.get(reverse('problem_list'), {'hide_solved': '1'})
        self.assertEqual(response.status_code, 200)
        codes = {p.code for p in response.context['problems']}
        self.assertIn(self.mcq.code, codes)

    # Closed: G-1 (low, noisy): global AC-per-language counter counts hidden verdicts
    def test_language_ac_stats_skip_hidden(self):
        data = self.client.get(reverse('language_stats_data_ac')).json()
        self.assertEqual(sum(data['datasets'][0]['data']), 0)


class G7RemainingPathsTestCase(HiddenResultsBase):
    """G-7: an exam problem must not work as an answer checker anywhere while the exam is open."""

    # Closed: G-7: virtual participation in an older contest that reuses the problem
    def test_virtual_participation_in_old_contest_with_same_problem(self):
        old = create_contest(key='hroldhw', format_name='default', is_visible=True, problems=(),
                             allow_virtual_participation=True,
                             start_time=timezone.now() - timedelta(days=200),
                             end_time=timezone.now() - timedelta(days=150))
        create_contest_problem(contest=old, problem=self.mcq, points=10, order=1)
        friend = create_user(username='hr_friend')
        self._login(friend)
        response = self.client.post(reverse('contest_join', args=[old.key]))
        self.assertEqual(response.status_code, 302)
        self.client.post(reverse('problem_submit', args=[self.mcq.code]), {'options': str(self.right.id)})
        sub = Submission.objects.filter(user=friend.profile, problem=self.mcq).order_by('-id').first()
        # Either the submit is refused, or its verdict is not shown while the exam runs.
        self.assertTrue(sub is None or not sub.results_visible_to(friend),
                        'verdict %s shown at once' % (sub and sub.result))


class G1UserDataExportTestCase(HiddenResultsBase):
    """G-1: the "download your data" archive (when DMOJ_USER_DATA_DOWNLOAD is on)."""

    # Closed: G-1: prepare_user_data writes the real result/case_points of hidden exam answers
    def test_data_export_masks_hidden_exam_results(self):
        import tempfile
        import zipfile
        from django.test import override_settings
        from judge.tasks.user import prepare_user_data
        cache_dir = tempfile.mkdtemp()
        options = {'submission_download': True, 'submission_results': ['AC'], 'submission_problem_glob': '*',
                   'comment_download': False}
        with override_settings(DMOJ_USER_DATA_CACHE=cache_dir):
            prepare_user_data.apply(args=(self.student.profile.id, json.dumps(options)))
        with zipfile.ZipFile(os.path.join(cache_dir, '%d.zip' % self.student.profile.id)) as archive:
            names = archive.namelist()
            info = json.loads(archive.read('submissions/info.json')) if 'submissions/info.json' in names else {}
        # The ?result=AC filter must not select it, and the archive must not carry its verdict.
        row = info.get(str(self.sub.id))
        self.assertTrue(row is None or row['result'] is None, row)


class CA1TwoStepImportTestCase(HiddenResultsBase):
    """CA-1: a course admin's roster import must not rewrite another person's e-mail."""

    # Closed: CA-1: the first upload links the account to the course, the second one then rewrites it
    def test_second_upload_rewrites_email_of_an_outside_account(self):
        import io
        from judge.models.tests.util import create_organization
        from judge.utils.import_export import process_user_csv
        course = create_organization(name='ca1course')
        victim = create_user(username='ca1victim', email='victim@example.com', is_active=True)
        victim.set_password('pw')
        victim.save()
        csv = b'username,first_name,last_name,email\nca1victim,A,B,attacker@example.com\n'
        process_user_csv(io.BytesIO(csv), organization=course, update_existing=True)
        process_user_csv(io.BytesIO(csv), organization=course, update_existing=True)
        victim.refresh_from_db()
        self.assertEqual(victim.email, 'victim@example.com')


class CA4MentionSearchTestCase(HiddenResultsBase):
    """CA-4: user search must not list real names next to NetIDs to anyone logged in."""

    # Closed: CA-4: /widgets/martor/search-user is unscoped (any student, 1 character, unlisted users)
    def test_martor_mention_search_is_scoped(self):
        hidden = create_user(username='ab123', first_name='Real', last_name='Name')
        hidden.profile.is_unlisted = True
        hidden.profile.save()
        stranger = create_user(username='ca4stranger')   # shares no course with anyone
        self.client.force_login(stranger)
        data = self.client.get(reverse('martor_search_user'), {'username': 'a'}).json()
        usernames = [row['username'] for row in data.get('data', [])]
        self.assertNotIn('ab123', usernames)


class CA7SocialSignupTestCase(HiddenResultsBase):
    """CA-7: nobody but the NetID owner may end up in the account an SSO login lands in."""

    # Closed: CA-7: the social-auth sign-up form does not apply SSO_RESERVED_USERNAME_REGEX
    def test_social_signup_cannot_pick_a_netid_shaped_username(self):
        from judge.social_auth import UsernameForm
        self.assertFalse(UsernameForm({'username': 'ab123'}).is_valid())

    # Closed: CA-7: a social-auth account has no usable password, so SSO binds the NetID to it
    def test_sso_does_not_bind_to_a_social_auth_account(self):
        from django.contrib.auth.models import User
        from social_django.models import UserSocialAuth
        from judge.views.user import netid_may_bind_existing_user
        squatter = User.objects.create_user(username='ab124', email='squatter@gmail.com')  # unusable password
        UserSocialAuth.objects.create(user=squatter, provider='google-oauth2', uid='squatter@gmail.com')
        self.assertFalse(netid_may_bind_existing_user(squatter, 'ab124@duke.edu'))


class AuthoringLegacyEditorTestCase(HiddenResultsBase):
    """AUTH-05 / AUTH-10 through the legacy MCQ editor (/problems/new/mcq, /problem/<code>/mcq)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from judge.models.tests.util import create_organization
        cls.creator = create_user(username='legacy_creator',
                                  user_permissions=('create_private_problem', 'edit_own_problem'))
        cls.foreign_course = create_organization(name='legacyforeign')   # the creator does not teach it

    def _post_data(self, **fields):
        from judge.views.tests.test_mcq import option_post_data
        return option_post_data([('4', True), ('3', False)], **fields)

    # Closed: AUTH-10: the legacy MCQ editor attaches any course (create_v2 and the Excel import are scoped)
    def test_legacy_mcq_editor_cannot_attach_a_course_not_taught(self):
        from judge.models import Problem
        self.client.force_login(self.creator)
        data = self._post_data(name='Legacy foreign')
        data['organizations'] = [self.foreign_course.id]
        self.client.post(reverse('problem_mcq_create'), data)
        problem = Problem.objects.filter(name='Legacy foreign').first()
        self.assertTrue(problem is None or not problem.organizations.filter(id=self.foreign_course.id).exists())

    # Closed: AUTH-05: the legacy MCQ editor rewrites a full-markup statement without problem_full_markup
    def test_legacy_mcq_editor_keeps_full_markup_description(self):
        from judge.models import ProblemMCQConfig, ProblemMCQOption
        from judge.models.tests.util import create_problem
        problem = create_problem(code='legacyfm', submission_type='M', is_full_markup=True,
                                 description='<b>trusted</b>', authors=('legacy_creator',))
        ProblemMCQConfig.objects.create(problem=problem)
        a = ProblemMCQOption.objects.create(problem=problem, order=0, text='4', is_correct=True)
        b = ProblemMCQOption.objects.create(problem=problem, order=1, text='3')
        self.client.force_login(self.creator)
        data = self._post_data(name=problem.name, description='<script>alert(1)</script>', initial_forms=2)
        data['options-0-id'], data['options-1-id'] = str(a.id), str(b.id)
        self.client.post(reverse('problem_mcq_edit', args=[problem.code]), data)
        problem.refresh_from_db()
        self.assertEqual(problem.description, '<b>trusted</b>')
