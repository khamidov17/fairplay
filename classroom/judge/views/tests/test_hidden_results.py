"""Exam result hiding beyond display: filters, counts, aggregates, rankings, solved markers
(G-1), "solved" source visibility (G-6) and practice submits during an exam (G-7).

``Submission.results_visible_to`` masks a row's displayed verdict. These tests pin that every
query on the real ``result`` / ``points`` applies the same rule (``judge.utils.result_visibility``)
so a student cannot learn a hidden exam verdict through it.
"""
import json
import re
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import (ContestSubmission, Language, ProblemMCQConfig, ProblemMCQOption, Submission,
                          SubmissionSource)
from judge.models.tests.util import (CommonDataMixin, create_contest, create_contest_participation,
                                     create_contest_problem, create_problem, create_user)
from judge.utils.problems import assignment_problem_stats, user_completed_ids


class HiddenResultsBase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.teacher = create_user(username='hr_teacher', user_permissions=('edit_own_contest',))
        cls.student = cls.users['normal']
        cls.other = create_user(username='hr_other')
        cls.contest = create_contest(key='hrexam', format_name='exam', is_visible=True,
                                     authors=('hr_teacher',), problems=())
        # Public within the site so that "solved" visibility / practice submits are reachable.
        cls.mcq = create_problem(code='hrmcq', submission_type='M', points=10, is_public=True,
                                 submission_source_visibility_mode='S')
        ProblemMCQConfig.objects.create(problem=cls.mcq, multiple_select=False)
        cls.wrong = ProblemMCQOption.objects.create(problem=cls.mcq, order=0, text='wrong', is_correct=False)
        cls.right = ProblemMCQOption.objects.create(problem=cls.mcq, order=1, text='right', is_correct=True)
        cls.cp = create_contest_problem(contest=cls.contest, problem=cls.mcq, points=10, order=1)
        cls.part = create_contest_participation(contest=cls.contest, user=cls.student.profile)
        cls.other_part = create_contest_participation(contest=cls.contest, user=cls.other.profile)
        cls.sub = cls._exam_sub(cls.part, 'AC', 10)
        cls.other_sub = cls._exam_sub(cls.other_part, 'AC', 10)

    @classmethod
    def _exam_sub(cls, participation, result, points):
        sub = Submission.objects.create(
            user=participation.user, problem=cls.mcq, language=Language.get_answer_language(),
            status='D', result=result, points=points, case_points=points, case_total=10,
            time=0.1, memory=1024, contest_object=cls.contest,
        )
        SubmissionSource.objects.create(submission=sub, source=json.dumps({'selected': [cls.right.id]}))
        ContestSubmission.objects.create(submission=sub, problem=cls.cp, participation=participation, points=points)
        return sub

    def setUp(self):
        cache.clear()

    def _login(self, user):
        self.client.force_login(user)

    def _finalize(self):
        self.contest.results_finalized = True
        self.contest.save(update_fields=['results_finalized'])
        cache.clear()

    def _list_ids(self, response):
        self.assertEqual(response.status_code, 200)
        return {s.id for s in response.context['submissions']}


class HiddenResultFiltersTestCase(HiddenResultsBase):
    """G-1."""

    def test_precondition_row_is_masked(self):
        self.assertTrue(self.sub.contest_results_hidden)
        self.assertFalse(self.sub.results_visible_to(self.student))

    def test_status_filter_does_not_match_hidden_verdict(self):
        self._login(self.student)
        url = reverse('all_user_submissions', args=[self.student.username])
        self.assertNotIn(self.sub.id, self._list_ids(self.client.get(url, {'status': 'AC'})))
        self.assertNotIn(self.sub.id, self._list_ids(self.client.get(url, {'status': 'WA'})))
        # Unfiltered, the (masked) row is still listed.
        self.assertIn(self.sub.id, self._list_ids(self.client.get(url)))
        url = reverse('contest_user_submissions', args=[self.contest.key, self.student.username, self.mcq.code])
        self.assertNotIn(self.sub.id, self._list_ids(self.client.get(url, {'status': 'AC'})))

    def test_result_counts_skip_hidden_verdicts(self):
        self._login(self.student)
        url = reverse('all_user_submissions', args=[self.student.username])
        data = self.client.get(url, {'results': ''}).json()
        counts = {c['code']: c['count'] for c in data['categories']}
        self.assertEqual(counts['AC'], 0)
        self.assertEqual(data['total'], 0)

    def test_editor_still_filters_and_counts(self):
        self._login(self.teacher)
        url = reverse('contest_user_submissions', args=[self.contest.key, self.student.username, self.mcq.code])
        self.assertIn(self.sub.id, self._list_ids(self.client.get(url, {'status': 'AC'})))
        data = self.client.get(url, {'results': ''}).json()
        self.assertEqual({c['code']: c['count'] for c in data['categories']}['AC'], 1)

    def test_filters_work_again_after_finalize(self):
        self._finalize()
        self._login(self.student)
        url = reverse('all_user_submissions', args=[self.student.username])
        self.assertIn(self.sub.id, self._list_ids(self.client.get(url, {'status': 'AC'})))

    def test_api_result_filter_skips_hidden(self):
        self._login(self.student)
        for user in (self.student, self.other):
            data = self.client.get('/api/v2/submissions', {'user': user.username, 'result': 'AC'}).json()
            ids = [obj['id'] for obj in data['data']['objects']]
            self.assertEqual(ids, [], user.username)
        data = self.client.get('/api/v2/submissions', {'user': self.student.username}).json()
        self.assertEqual([obj['id'] for obj in data['data']['objects']], [self.sub.id])

    def test_api_user_solved_problems_skip_hidden(self):
        self._login(self.other)
        data = self.client.get('/api/v2/user/%s' % self.student.username).json()
        self.assertNotIn(self.mcq.code, data['data']['object']['solved_problems'])

    def test_assignment_ac_rate_hidden_from_students(self):
        stats = assignment_problem_stats(self.contest, self.student)
        self.assertEqual(stats[self.mcq.id]['ac_rate'], 0.0)
        self.assertEqual(stats[self.mcq.id]['user_count'], 2)
        self.assertEqual(assignment_problem_stats(self.contest, self.teacher)[self.mcq.id]['ac_rate'], 100.0)

    def test_contest_page_ac_rate_hidden_from_students(self):
        self._login(self.student)
        response = self.client.get(reverse('contest_view', args=[self.contest.key]))
        self.assertEqual(response.status_code, 200)
        problem = next(p for p in response.context['contest_problems'] if p.id == self.mcq.id)
        self.assertEqual(problem.ac_rate, 0.0)

    def test_contest_stats_skip_hidden(self):
        from judge.models import Contest
        Contest.objects.filter(id=self.contest.id).update(
            start_time=timezone.now() - timedelta(days=2), end_time=timezone.now() - timedelta(days=1))
        self._login(self.student)
        response = self.client.get(reverse('contest_stats', args=[self.contest.key]))
        self.assertEqual(response.status_code, 200)
        stats = json.loads(response.context['stats'])
        for dataset in stats['problem_status_count']['datasets']:
            self.assertEqual(sum(dataset['data']), 0, dataset['label'])

    def test_ranked_list_skips_hidden(self):
        url = reverse('contest_ranked_submissions', args=[self.contest.key, self.mcq.code])
        self._login(self.student)
        self.assertEqual(self._list_ids(self.client.get(url)), set())
        self._login(self.teacher)
        self.assertEqual(self._list_ids(self.client.get(url)), {self.sub.id, self.other_sub.id})

    def test_solved_markers_skip_hidden(self):
        self.assertNotIn(self.mcq.id, user_completed_ids(self.student.profile))
        self.assertFalse(self.mcq.is_solved_by(self.student))
        self._finalize()
        self.assertIn(self.mcq.id, user_completed_ids(self.student.profile))
        self.assertTrue(self.mcq.is_solved_by(self.student))

    def test_public_problem_stats_and_profile_points_skip_hidden(self):
        self.mcq.update_stats()
        self.mcq.refresh_from_db()
        self.assertEqual(self.mcq.user_count, 0)
        profile = self.student.profile
        profile.calculate_points()
        profile.refresh_from_db()
        self.assertEqual(profile.problem_count, 0)
        self.assertEqual(profile.points, 0)


class HiddenResultSourceVisibilityTestCase(HiddenResultsBase):
    """G-6."""

    def test_classmate_cannot_open_submission_during_exam(self):
        self._login(self.other)
        response = self.client.get(reverse('submission_status', args=[self.sub.id]))
        self.assertEqual(response.status_code, 403)
        # Same generic answer whatever one's own (hidden) verdict is: no "Solve ..." hint.
        self.assertNotContains(response, 'in order to view it', status_code=403)
        self.assertFalse(self.sub.can_see_detail(self.other))

    def test_side_channel_identical_for_right_and_wrong_answer(self):
        Submission.objects.filter(id=self.other_sub.id).update(result='WA', points=0)
        self._login(self.other)
        wrong = self.client.get(reverse('submission_status', args=[self.sub.id]))
        Submission.objects.filter(id=self.other_sub.id).update(result='AC', points=10)
        right = self.client.get(reverse('submission_status', args=[self.sub.id]))
        self.assertEqual(wrong.status_code, right.status_code)

        def strip_csrf(content):
            return re.sub(rb"csrfmiddlewaretoken' value='[^']*'|csrfmiddlewaretoken\" value=\"[^\"]*\"", b'',
                          content)
        self.assertEqual(strip_csrf(wrong.content), strip_csrf(right.content))

    def test_non_code_solved_visibility_is_own_only_in_practice(self):
        self._finalize()
        practice = create_problem(code='hrprac', submission_type='M', points=10, is_public=True,
                                  submission_source_visibility_mode='S')
        mine = Submission.objects.create(user=self.student.profile, problem=practice,
                                         language=Language.get_answer_language(), status='D', result='AC',
                                         points=10)
        Submission.objects.create(user=self.other.profile, problem=practice,
                                  language=Language.get_answer_language(), status='D', result='AC', points=10)
        self.assertFalse(mine.can_see_detail(self.other))

    def test_code_solved_visibility_still_works_in_practice(self):
        code = create_problem(code='hrcode', submission_type='C', points=10, is_public=True,
                              submission_source_visibility_mode='S')
        lang = Language.objects.get(key='PY3')
        mine = Submission.objects.create(user=self.student.profile, problem=code, language=lang,
                                         status='D', result='AC', points=10)
        Submission.objects.create(user=self.other.profile, problem=code, language=lang,
                                  status='D', result='AC', points=10)
        self.assertTrue(mine.can_see_detail(self.other))


class PracticeSubmitDuringExamTestCase(HiddenResultsBase):
    """G-7."""

    def _practice_submit(self, user):
        self._login(user)
        return self.client.post(reverse('problem_submit', args=[self.mcq.code]), {'options': str(self.right.id)})

    def test_practice_submit_blocked_while_exam_running(self):
        before = Submission.objects.count()
        response = self._practice_submit(self.student)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Submission.objects.count(), before)
        # Someone not enrolled in the exam is blocked too (they could pass answers on).
        response = self._practice_submit(create_user(username='hr_outsider'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Submission.objects.count(), before)

    def test_practice_submit_blocked_before_exam_starts(self):
        from judge.models import Contest
        Contest.objects.filter(id=self.contest.id).update(
            start_time=timezone.now() + timedelta(days=1), end_time=timezone.now() + timedelta(days=2))
        self.assertEqual(self._practice_submit(self.student).status_code, 403)

    def test_practice_submit_allowed_after_exam_or_finalize(self):
        self._finalize()
        self.assertEqual(self._practice_submit(self.student).status_code, 302)

    def test_editor_can_still_test_submit(self):
        self.mcq.authors.add(self.users['staff_problem_edit_own'].profile)
        self.assertEqual(self._practice_submit(self.users['staff_problem_edit_own']).status_code, 302)


class ResultsHiddenQueryMatchesModelTestCase(HiddenResultsBase):
    """The query helper and ``Submission.contest_results_hidden`` agree row by row."""

    def test_q_matches_property(self):
        from judge.models import ContestProblem
        from judge.utils.result_visibility import results_hidden_q, results_visible_q
        lang = Language.objects.get(key='PY3')
        code = create_problem(code='hrqcode', submission_type='C', points=10)
        code_cp = create_contest_problem(contest=self.contest, problem=code, points=10, order=2)
        forced = create_problem(code='hrqforced', submission_type='C', points=10)
        forced_cp = create_contest_problem(contest=self.contest, problem=forced, points=10, order=3,
                                           hide_results=True)
        shown = create_problem(code='hrqshown', submission_type='M', points=10)
        shown_cp = create_contest_problem(contest=self.contest, problem=shown, points=10, order=4,
                                          hide_results=False)
        plain = create_contest(key='hrqplain', format_name='default', problems=())
        plain_cp = create_contest_problem(contest=plain, problem=shown, points=10, order=1)
        plain_part = create_contest_participation(contest=plain, user=self.student.profile)
        for cp, participation in ((code_cp, self.part), (forced_cp, self.part), (shown_cp, self.part),
                                  (plain_cp, plain_part)):
            sub = Submission.objects.create(user=self.student.profile, problem=cp.problem, language=lang,
                                            status='D', result='AC', points=10, contest_object=cp.contest)
            ContestSubmission.objects.create(submission=sub, problem=cp, participation=participation, points=10)
        Submission.objects.create(user=self.student.profile, problem=shown, language=lang, status='D',
                                  result='AC', points=10)          # practice
        Submission.objects.create(user=self.student.profile, problem=self.mcq, language=lang, status='D',
                                  result='AC', points=10, contest_object=self.contest)  # no ContestSubmission
        self.assertTrue(ContestProblem.objects.filter(id=forced_cp.id).exists())

        def check():
            subs = list(Submission.objects.all())
            expected_hidden = {s.id for s in subs if s.contest_results_hidden}
            self.assertEqual(set(Submission.objects.filter(results_hidden_q()).values_list('id', flat=True)),
                             expected_hidden)
            self.assertEqual(set(Submission.objects.exclude(results_hidden_q()).values_list('id', flat=True)),
                             {s.id for s in subs} - expected_hidden)
            student_visible = {s.id for s in subs if s.results_visible_to(self.student)}
            self.assertEqual(
                set(Submission.objects.filter(results_visible_q(self.student)).values_list('id', flat=True)),
                student_visible)
            teacher_visible = {s.id for s in subs if s.results_visible_to(self.teacher)}
            self.assertEqual(
                set(Submission.objects.filter(results_visible_q(self.teacher)).values_list('id', flat=True)),
                teacher_visible)
            return expected_hidden

        self.assertEqual(len(check()), 3)   # the two exam MCQs and the forced-hidden code problem
        self._finalize()
        self.assertEqual(check(), set())
