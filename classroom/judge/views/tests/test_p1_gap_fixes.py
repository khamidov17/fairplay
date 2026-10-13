"""Controls for the P1 gap fixes (docs/classroom/reports/P1-gaps.md).

``test_p1_verification`` shows each gap is closed. These tests pin that the legitimate flows
around each fix still work: results come back after Finalize, ordinary verdicts still count,
roster imports still fill in SSO accounts, teachers still find their students, and so on.
"""
import io
import json
import os
import tempfile
import zipfile
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import (Contest, Language, Problem, ProblemBlank, ProblemFillBlankConfig,
                          Profile, Submission)
from judge.models.tests.util import (create_contest, create_contest_participation, create_contest_problem,
                                     create_organization, create_problem, create_user)
from judge.views.tests.test_fill_blank import blank_post_data
from judge.views.tests.test_hidden_results import HiddenResultsBase


class G1AfterFinalizeTestCase(HiddenResultsBase):
    """G-1: the hidden answer is masked until Finalize, then counts everywhere as before."""

    def _solved_codes(self):
        response = self.client.get(reverse('user_problems', args=[self.student.username]))
        self.assertEqual(response.status_code, 200)
        solved = {p['code'] for group in response.context['best_submissions'] for p in group['problems']}
        pp = {entry.problem_code for entry in response.context['pp_breakdown']}
        return solved, pp

    def test_solved_page_and_pp_breakdown_show_it_after_finalize(self):
        self._finalize()
        solved, pp = self._solved_codes()
        self.assertIn(self.mcq.code, solved)
        self.assertIn(self.mcq.code, pp)

    def test_practice_ac_still_listed_while_exam_answer_is_hidden(self):
        other = create_problem(code='hrpractice', points=5, is_public=True)
        Submission.objects.create(user=self.student.profile, problem=other, language=Language.get_python3(),
                                  status='D', result='AC', points=5, case_points=5, case_total=5)
        solved, pp = self._solved_codes()
        self.assertIn('hrpractice', solved)
        self.assertIn('hrpractice', pp)
        self.assertNotIn(self.mcq.code, solved)
        self.assertNotIn(self.mcq.code, pp)

    def test_hide_solved_hides_it_after_finalize(self):
        Contest.objects.filter(id=self.contest.id).update(
            start_time=timezone.now() - timedelta(days=2), end_time=timezone.now() - timedelta(hours=1))
        self._finalize()
        self._login(self.student)
        response = self.client.get(reverse('problem_list'), {'hide_solved': '1'})
        self.assertNotIn(self.mcq.code, {p.code for p in response.context['problems']})

    def test_language_stats_count_visible_ac(self):
        Submission.objects.create(user=self.other.profile, problem=self.mcq, language=Language.get_python3(),
                                  status='D', result='AC', points=10, case_points=10, case_total=10)
        data = self.client.get(reverse('language_stats_data_ac')).json()
        self.assertEqual(sum(data['datasets'][0]['data']), 1)
        self._finalize()
        data = self.client.get(reverse('language_stats_data_ac')).json()
        self.assertEqual(sum(data['datasets'][0]['data']), 3)

    def _export(self, results):
        from judge.tasks.user import prepare_user_data
        cache_dir = tempfile.mkdtemp()
        options = {'submission_download': True, 'submission_results': results, 'submission_problem_glob': '*',
                   'comment_download': False}
        with override_settings(DMOJ_USER_DATA_CACHE=cache_dir):
            prepare_user_data.apply(args=(self.student.profile.id, json.dumps(options)))
        with zipfile.ZipFile(os.path.join(cache_dir, '%d.zip' % self.student.profile.id)) as archive:
            names = archive.namelist()
            return json.loads(archive.read('submissions/info.json')) if 'submissions/info.json' in names else {}

    def test_data_export_keeps_the_hidden_answer_without_its_verdict(self):
        # No result filter: the student's own answer is in the archive, its verdict is not.
        row = self._export([])[str(self.sub.id)]
        self.assertIsNone(row['result'])
        self.assertIsNone(row['case_points'])

    def test_data_export_shows_verdict_after_finalize(self):
        self._finalize()
        row = self._export(['AC'])[str(self.sub.id)]
        self.assertEqual(row['result'], 'AC')


class G1ReviewFollowUpTestCase(HiddenResultsBase):
    """Two more paths the independent review found."""

    def test_status_stats_skip_hidden(self):
        data = self.client.get(reverse('stats_data_status')).json()
        self.assertEqual(sum(data['datasets'][0]['data']) if data.get('datasets') else 0, 0)
        self._finalize()
        data = self.client.get(reverse('stats_data_status')).json()
        self.assertEqual(sum(data['datasets'][0]['data']), 2)

    def test_has_any_solves_ignores_hidden_verdict(self):
        from judge.models import Profile
        self.assertFalse(Profile.objects.get(id=self.student.profile.id).has_any_solves)
        self._finalize()
        self.assertTrue(Profile.objects.get(id=self.student.profile.id).has_any_solves)


class G7ControlsTestCase(HiddenResultsBase):
    """G-7: the block covers every place a verdict is shown at once, and nothing more."""

    def _homework(self):
        homework = create_contest(key='hrhw', format_name='default', is_visible=True, problems=(),
                                  start_time=timezone.now() - timedelta(hours=1),
                                  end_time=timezone.now() + timedelta(days=1))
        create_contest_problem(contest=homework, problem=self.mcq, points=10, order=1)
        return homework

    def _submit_in(self, user, contest):
        self._login(user)
        self.assertEqual(self.client.post(reverse('contest_join', args=[contest.key])).status_code, 302)
        return self.client.post(reverse('problem_submit', args=[self.mcq.code]), {'options': str(self.right.id)})

    def test_concurrent_homework_that_shows_results_is_closed(self):
        homework = self._homework()
        friend = create_user(username='hr_hw_friend')
        response = self._submit_in(friend, homework)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Submission.objects.filter(user=friend.profile, problem=self.mcq).exists())

    def test_homework_opens_again_once_the_exam_is_over(self):
        Contest.objects.filter(id=self.contest.id).update(
            start_time=timezone.now() - timedelta(days=2), end_time=timezone.now() - timedelta(hours=1))
        homework = self._homework()
        friend = create_user(username='hr_hw_later')
        self._submit_in(friend, homework)
        self.assertTrue(Submission.objects.filter(user=friend.profile, problem=self.mcq).exists())

    def test_harvest_does_not_hand_in_through_another_contest(self):
        # Review follow-up: the auto-submit path applies the same rule as a direct submit.
        import json as _json
        from judge.models import AssignmentDraft
        from judge.utils.assignment_harvest import harvest_participation
        homework = self._homework()
        friend = create_user(username='hr_hw_harvest')
        participation = create_contest_participation(contest=homework, user=friend.profile)
        AssignmentDraft.objects.create(participation=participation, problem=self.mcq,
                                       content=_json.dumps({'selected': [self.right.id]}))
        self.assertEqual(harvest_participation(participation), [])
        self.assertFalse(Submission.objects.filter(user=friend.profile, problem=self.mcq).exists())

    def test_editor_may_still_test_submit_in_another_contest(self):
        homework = self._homework()
        author = create_user(username='hr_author', user_permissions=('edit_own_problem',))
        self.mcq.authors.add(author.profile)
        self._submit_in(author, homework)
        self.assertTrue(Submission.objects.filter(user=author.profile, problem=self.mcq).exists())


class CA1ControlsTestCase(HiddenResultsBase):
    """CA-1: imports still fill in SSO-only accounts; they only stop touching login fields of others."""

    def _upload(self, course, rows):
        from judge.utils.import_export import process_user_csv
        csv = 'username,first_name,last_name,email\n' + ''.join('%s\n' % r for r in rows)
        return process_user_csv(io.BytesIO(csv.encode()), organization=course, update_existing=True)

    def test_sso_only_member_gets_its_email_updated(self):
        course = create_organization(name='ca1sso')
        self._upload(course, ['ab321,A,B,old@duke.edu'])
        user = User.objects.get(username='ab321')
        self.assertFalse(user.has_usable_password())
        self._upload(course, ['ab321,Ann,Bee,new@duke.edu'])
        user.refresh_from_db()
        self.assertEqual((user.first_name, user.email), ('Ann', 'new@duke.edu'))

    def test_password_member_is_left_alone(self):
        course = create_organization(name='ca1pw')
        member = create_user(username='ca1member', email='mine@example.com', is_active=False,
                             first_name='Old', last_name='Own')
        member.set_password('pw')
        member.save()
        course.members.add(member.profile)
        self._upload(course, ['ca1member,New,Name,other@example.com'])
        member.refresh_from_db()
        self.assertEqual((member.first_name, member.last_name), ('Old', 'Own'))
        self.assertEqual(member.email, 'mine@example.com')
        self.assertFalse(member.is_active)
        self.assertTrue(course.members.filter(id=member.profile.id).exists())

    def test_google_login_never_links_to_an_sso_account_by_email(self):
        # Review follow-up: a roster e-mail is unverified, so a social login with that address
        # must not be associated with the SSO-only account (associate_by_email).
        from unittest import mock
        from judge.social_auth import associate_by_email
        sso = User.objects.create_user(username='ab777', email='planted@gmail.com')  # unusable password
        backend = mock.Mock()
        backend.strategy.storage.user.get_users_by_email.return_value = [sso]
        backend.setting.return_value = None
        self.assertIsNone(associate_by_email(backend, {'email': 'planted@gmail.com'}))
        pw = create_user(username='ca1pwuser', email='real@gmail.com')
        backend.strategy.storage.user.get_users_by_email.return_value = [pw]
        self.assertEqual(associate_by_email(backend, {'email': 'real@gmail.com'})['user'], pw)

    def test_social_auth_member_email_unchanged(self):
        from social_django.models import UserSocialAuth
        course = create_organization(name='ca1social')
        member = User.objects.create_user(username='ca1social', email='me@gmail.com')
        UserSocialAuth.objects.create(user=member, provider='google-oauth2', uid='me@gmail.com')
        profile, _created = Profile.objects.get_or_create(user=member)
        course.members.add(profile)
        self._upload(course, ['ca1social,A,B,attacker@example.com'])
        member.refresh_from_db()
        self.assertEqual(member.email, 'me@gmail.com')


class CA4ControlsTestCase(HiddenResultsBase):
    """CA-4: the mention search still finds people one shares a course with."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.course = create_organization(name='ca4course')
        cls.classmate = create_user(username='ca4mate', first_name='Mate', last_name='Person')
        cls.searcher = create_user(username='ca4searcher')
        cls.course_admin = create_user(username='ca4admin')
        cls.course.members.add(cls.classmate.profile, cls.searcher.profile)
        cls.course.admins.add(cls.course_admin.profile)

    def _search(self, user, term):
        self.client.force_login(user)
        return self.client.get(reverse('martor_search_user'), {'username': term}).json().get('data', [])

    def test_student_finds_classmate_by_username_without_real_name(self):
        rows = self._search(self.searcher, 'ca4mate')
        self.assertEqual([r['username'] for r in rows], ['ca4mate'])
        self.assertNotIn('Mate', rows[0]['name'])
        self.assertNotIn('Mate', rows[0]['text'])

    def test_course_admin_sees_real_names(self):
        rows = self._search(self.course_admin, 'ca4mate')
        self.assertEqual([r['username'] for r in rows], ['ca4mate'])
        self.assertIn('Mate', rows[0]['name'])

    def test_one_character_term_returns_nothing(self):
        self.assertEqual(self._search(self.course_admin, 'c'), [])


class CA7ControlsTestCase(HiddenResultsBase):
    def test_social_signup_ordinary_username_is_fine(self):
        from judge.social_auth import UsernameForm
        self.assertTrue(UsernameForm({'username': 'jane_doe'}).is_valid())

    def test_sso_still_binds_to_an_imported_account(self):
        from judge.views.user import netid_may_bind_existing_user
        imported = User.objects.create_user(username='ab125', email='ab125@duke.edu')  # unusable password
        self.assertTrue(netid_may_bind_existing_user(imported, 'ab125@duke.edu'))


class AuthoringLegacyControlsTestCase(HiddenResultsBase):
    """AUTH-05 / AUTH-10 in the fill-in-the-blank editor, and the allowed cases of both."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.creator = create_user(username='fb_creator',
                                  user_permissions=('create_private_problem', 'edit_own_problem'))
        cls.marker = create_user(username='fb_marker', user_permissions=(
            'create_private_problem', 'edit_own_problem', 'problem_full_markup'))
        cls.foreign = create_organization(name='fbforeign')
        cls.taught = create_organization(name='fbtaught')
        cls.taught.instructors.add(cls.creator.profile)

    def _fill_blank(self, code, description, authors):
        problem = create_problem(code=code, submission_type='B', is_full_markup=True, description=description,
                                 authors=authors)
        ProblemFillBlankConfig.objects.create(problem=problem)
        blank = ProblemBlank.objects.create(problem=problem, order=0, label='', answers='Paris')
        return problem, blank

    def _edit_data(self, problem, blank, description):
        data = blank_post_data([('', 'Paris')], name=problem.name, description=description)
        data['blanks-INITIAL_FORMS'] = '1'
        data['blanks-0-id'] = str(blank.id)
        return data

    def test_fill_blank_cannot_attach_a_course_not_taught(self):
        self.client.force_login(self.creator)
        data = blank_post_data([('', 'Paris')], name='FB foreign')
        data['organizations'] = [self.foreign.id]
        self.client.post(reverse('problem_fillblank_create'), data)
        problem = Problem.objects.filter(name='FB foreign').first()
        self.assertTrue(problem is None or not problem.organizations.filter(id=self.foreign.id).exists())

    def test_fill_blank_attaches_a_course_taught(self):
        self.client.force_login(self.creator)
        data = blank_post_data([('', 'Paris')], name='FB taught')
        data['organizations'] = [self.taught.id]
        self.assertEqual(self.client.post(reverse('problem_fillblank_create'), data).status_code, 302)
        self.assertIn(self.taught, Problem.objects.get(name='FB taught').organizations.all())

    def test_fill_blank_keeps_full_markup_description(self):
        problem, blank = self._fill_blank('fbfm', '<b>trusted</b>', ('fb_creator',))
        self.client.force_login(self.creator)
        self.client.post(reverse('problem_fillblank_edit', args=[problem.code]),
                         self._edit_data(problem, blank, '<script>alert(1)</script>'))
        problem.refresh_from_db()
        self.assertEqual(problem.description, '<b>trusted</b>')

    def test_full_markup_problem_other_fields_still_editable(self):
        problem, blank = self._fill_blank('fbfm2', '<b>trusted</b>', ('fb_creator',))
        self.client.force_login(self.creator)
        data = self._edit_data(problem, blank, '<b>trusted</b>')
        data['points'] = 7
        self.assertEqual(self.client.post(reverse('problem_fillblank_edit', args=[problem.code]), data).status_code,
                         302)
        problem.refresh_from_db()
        self.assertEqual(problem.points, 7)

    def test_full_markup_permission_may_edit_the_statement(self):
        problem, blank = self._fill_blank('fbfm3', '<b>trusted</b>', ('fb_marker',))
        self.client.force_login(self.marker)
        self.client.post(reverse('problem_fillblank_edit', args=[problem.code]),
                         self._edit_data(problem, blank, '<i>new</i>'))
        problem.refresh_from_db()
        self.assertEqual(problem.description, '<i>new</i>')

    def test_existing_course_kept_when_a_coauthor_resaves(self):
        problem, blank = self._fill_blank('fbco', 'plain', ('fb_creator',))
        Problem.objects.filter(id=problem.id).update(is_full_markup=False)
        problem.organizations.add(self.foreign)  # attached earlier by someone who teaches it
        self.client.force_login(self.creator)
        data = self._edit_data(problem, blank, 'plain')
        data['organizations'] = [self.foreign.id]
        self.assertEqual(self.client.post(reverse('problem_fillblank_edit', args=[problem.code]), data).status_code,
                         302)
        self.assertIn(self.foreign, problem.organizations.all())


class ExamLockControlsTestCase(HiddenResultsBase):
    """Leave keeps a student in a running locked assignment; otherwise it works as before."""

    def _sit_and_leave(self, contest, user, participation=None):
        participation = participation or create_contest_participation(contest=contest, user=user.profile)
        profile = user.profile
        profile.current_contest = participation
        profile.save()
        self._login(user)
        self.client.post(reverse('contest_leave', args=[contest.key]))
        profile.refresh_from_db()
        return profile

    def _running(self):
        Contest.objects.filter(id=self.contest.id).update(
            start_time=timezone.now() - timedelta(hours=1), end_time=timezone.now() + timedelta(hours=2))

    def test_site_unlocked_after_the_window_ends(self):
        self._running()
        self._sit_and_leave(self.contest, self.student, self.part)
        self.assertEqual(self.client.get(reverse('problem_list')).status_code, 302)
        Contest.objects.filter(id=self.contest.id).update(end_time=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get(reverse('problem_list')).status_code, 200)

    def test_assignment_page_still_reachable_after_leave(self):
        self._running()
        profile = self._sit_and_leave(self.contest, self.student, self.part)
        self.assertEqual(profile.current_contest_id, self.part.id)
        self.assertEqual(self.client.get(reverse('contest_view', args=[self.contest.key])).status_code, 200)

    def test_leaving_an_unlocked_homework_unlocks_the_site(self):
        homework = create_contest(key='lockhw', format_name='default', is_visible=True, problems=(),
                                  start_time=timezone.now() - timedelta(hours=1),
                                  end_time=timezone.now() + timedelta(days=1))
        self.assertFalse(homework.locks_site_during_participation)
        profile = self._sit_and_leave(homework, create_user(username='lock_hw_student'))
        self.assertIsNone(profile.current_contest)
        self.assertEqual(self.client.get(reverse('problem_list')).status_code, 200)

    def test_holding_a_participation_without_being_in_it_does_not_lock(self):
        # Only being in the assignment locks; a participation alone (e.g. before Leave existed,
        # or after an editor removed the student) does not.
        self._running()
        self._login(self.student)
        self.assertEqual(self.client.get(reverse('problem_list')).status_code, 200)

    def test_site_icons_pass_the_lock(self):
        self._running()
        profile = self.student.profile
        profile.current_contest = self.part
        profile.save()
        self._login(self.student)
        response = self.client.get('/favicon-32x32.png')
        self.assertEqual(response.status_code, 302)
        self.assertIn('icons/', response['Location'])
