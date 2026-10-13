"""Dual-role coverage of contest AI grading across modes and problem types.

Creates two homeworks (on-demand / realtime), two exams (batch), and two
assignments with AI grading off. Each uses a distinct problem set mixing
AI-capable types (code+rubric, short answer, fill-blank AI) and types that
must never call AI (MCQ, file upload, code without AI, fill-blank exact).

Student and teacher clients hit the same URLs for every case.
"""
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Contest, ContestSubmission, Language, ProblemBlank, ProblemCodeConfig, ProblemFileConfig,
    ProblemFillBlankConfig, ProblemMCQConfig, ProblemMCQOption, ProblemShortAnswerConfig,
    Submission, SubmissionSource, SubmissionTestCase,
)
from judge.models.question import GRADE_AI, GRADE_EXACT
from judge.models.tests.util import (
    CommonDataMixin, create_contest, create_contest_participation, create_contest_problem,
    create_problem, create_user,
)
from judge.utils.contest_ai import (
    contest_awaits_student_ai, problem_has_ai_grading, problem_supports_ai_feedback,
    should_run_ai_now, student_can_trigger_ai,
)


def _done_sub(user, problem, contest, source, *, status='D', result='AC', language=None):
    lang = language or Language.get_default_language()
    sub = Submission.objects.create(
        user=user.profile, problem=problem, language=lang,
        status=status, result=result, contest_object=contest,
        time=0, memory=0, points=0,
        case_points=0, case_total=float(problem.points or 0),
    )
    SubmissionSource.objects.create(submission=sub, source=source)
    return sub


class AIGradingDualRoleTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.teacher = create_user(username='aie2e_teacher')
        cls.student = create_user(username='aie2e_student')
        cls.other = create_user(username='aie2e_other')
        cls.py = Language.objects.get(key='PY3')
        cls.answer = Language.get_answer_language()
        now = timezone.now()

        def bank(prefix, kinds):
            """Build a distinct problem list. kinds is a list of type tags."""
            problems = []
            for i, kind in enumerate(kinds, start=1):
                code = f'{prefix}p{i}{kind}'
                if kind == 'codeai':
                    p = create_problem(code=code, submission_type='C', points=100, is_public=True)
                    p.allowed_languages.add(cls.py)
                    ProblemCodeConfig.objects.create(problem=p, ai_enabled=True, ai_rubric='style')
                elif kind == 'codeno':
                    p = create_problem(code=code, submission_type='C', points=100, is_public=True)
                    p.allowed_languages.add(cls.py)
                    ProblemCodeConfig.objects.create(problem=p, ai_enabled=False)
                elif kind == 'mcq':
                    p = create_problem(code=code, submission_type='M', points=10, is_public=True)
                    ProblemMCQConfig.objects.create(problem=p, multiple_select=False)
                    ProblemMCQOption.objects.create(problem=p, order=0, text='wrong', is_correct=False)
                    ProblemMCQOption.objects.create(problem=p, order=1, text='right', is_correct=True)
                elif kind == 'sa':
                    p = create_problem(code=code, submission_type='S', points=20, is_public=True)
                    ProblemShortAnswerConfig.objects.create(
                        problem=p, reference_answer='42', ai_rubric='full credit if 42',
                        ai_enabled=True,
                    )
                elif kind == 'fbai':
                    p = create_problem(code=code, submission_type='B', points=10, is_public=True)
                    ProblemFillBlankConfig.objects.create(problem=p, grading_mode=GRADE_AI, ai_rubric='blanks')
                    ProblemBlank.objects.create(problem=p, order=0, answers='yes', points=10)
                elif kind == 'fbex':
                    p = create_problem(code=code, submission_type='B', points=10, is_public=True)
                    ProblemFillBlankConfig.objects.create(problem=p, grading_mode=GRADE_EXACT)
                    ProblemBlank.objects.create(problem=p, order=0, answers='paris', points=10)
                elif kind == 'file':
                    p = create_problem(code=code, submission_type='F', points=10, is_public=True)
                    ProblemFileConfig.objects.create(problem=p, allowed_extensions='pdf')
                else:
                    raise ValueError(kind)
                problems.append((kind, p))
            return problems

        specs = [
            ('hw1', 'Homework 1 Algorithms', True, 'default',
             ['codeai', 'mcq', 'file']),
            ('hw2', 'Homework 2 Written', True, 'default',
             ['sa', 'fbex', 'codeno']),
            ('ex1', 'Exam 1 Midterm', False, 'exam',
             ['codeai', 'mcq', 'fbai']),
            ('ex2', 'Exam 2 Final', False, 'exam',
             ['sa', 'codeno', 'file']),
            ('off1', 'Practice Off A', False, 'default',
             ['codeai', 'mcq']),
            ('off2', 'Practice Off B', False, 'default',
             ['sa', 'fbai']),
        ]
        cls.contests = {}
        for key, name, allow_student, fmt, kinds in specs:
            contest = create_contest(
                key=key, name=name, authors=('aie2e_teacher',),
                allow_student_ai_grading=allow_student,
                ai_grading_mode=(
                    Contest.AI_GRADING_REALTIME if allow_student else Contest.AI_GRADING_OFF
                ),
                format_name=fmt, is_visible=True,
                start_time=now - timezone.timedelta(hours=1),
                end_time=now + timezone.timedelta(hours=3),
            )
            problems = bank(key, kinds)
            cps = []
            for order, (kind, problem) in enumerate(problems, start=1):
                cps.append((kind, problem, create_contest_problem(
                    contest=contest, problem=problem, points=problem.points, order=order,
                )))
            part = create_contest_participation(contest=contest, user=cls.student.profile)
            create_contest_participation(contest=contest, user=cls.other.profile)
            cls.contests[key] = {
                'contest': contest, 'allow_student': allow_student, 'problems': cps,
                'part': part, 'format': fmt,
            }

    def setUp(self):
        cache.clear()
        self.student_client = Client()
        self.student_client.force_login(self.student)
        self.teacher_client = Client()
        self.teacher_client.force_login(self.teacher)
        self.other_client = Client()
        self.other_client.force_login(self.other)

    def _subs_for(self, key):
        info = self.contests[key]
        rows = []
        for kind, problem, cp in info['problems']:
            if kind in ('codeai', 'codeno'):
                src, lang = 'print(1)', self.py
            elif kind == 'mcq':
                opt = problem.mcq_options.get(is_correct=True)
                src, lang = '{"selected": [%d]}' % opt.id, self.answer
            elif kind == 'sa':
                src, lang = '{"text": "42"}', self.answer
            elif kind in ('fbai', 'fbex'):
                src, lang = '{"answers": ["yes"]}', self.answer
            else:
                src, lang = '{"file": "x.pdf"}', self.answer
            sub = _done_sub(self.student, problem, info['contest'], src, language=lang)
            ContestSubmission.objects.create(
                submission=sub, problem=cp, participation=info['part'], points=0,
            )
            rows.append((kind, problem, sub))
        return rows

    def _status(self, client, sub):
        return client.get(reverse('submission_status', args=[sub.id]))

    def _ai_post(self, client, sub):
        return client.post(reverse('submission_ai_grade', args=[sub.id]))

    def test_problem_type_ai_capability(self):
        mapping = {
            'codeai': True, 'codeno': False, 'mcq': False, 'sa': True,
            'fbai': True, 'fbex': False, 'file': False,
        }
        for info in self.contests.values():
            for kind, problem, _cp in info['problems']:
                self.assertEqual(
                    problem_has_ai_grading(problem), mapping[kind],
                    msg='%s %s' % (info['contest'].key, kind),
                )

    def test_homework_on_demand_never_auto_grades(self):
        for key in ('hw1', 'hw2'):
            for kind, problem, _cp in self.contests[key]['problems']:
                sub = _done_sub(self.student, problem, self.contests[key]['contest'], 'x')
                self.assertFalse(should_run_ai_now(sub), msg='%s %s' % (key, kind))
                # Assignment Allow ON → all supported types (problem AI optional).
                if problem_supports_ai_feedback(problem):
                    self.assertTrue(should_run_ai_now(sub, manual_trigger=True),
                                    msg='%s %s' % (key, kind))
                else:
                    self.assertFalse(should_run_ai_now(sub, manual_trigger=True),
                                     msg='%s %s' % (key, kind))

    def test_exam_batch_only_runs_on_batch_pass(self):
        for key in ('ex1', 'ex2'):
            for kind, problem, _cp in self.contests[key]['problems']:
                sub = _done_sub(self.student, problem, self.contests[key]['contest'], 'x')
                self.assertFalse(should_run_ai_now(sub))
                if problem_has_ai_grading(problem):
                    self.assertTrue(should_run_ai_now(sub, batch_pass=True))

    def test_off_never_auto_runs_ai_teachers_can_trigger(self):
        for key in ('off1', 'off2'):
            for kind, problem, _cp in self.contests[key]['problems']:
                sub = _done_sub(self.student, problem, self.contests[key]['contest'], 'x')
                self.assertFalse(should_run_ai_now(sub))
                self.assertFalse(student_can_trigger_ai(self.student, sub))
                if problem_has_ai_grading(problem):
                    self.assertTrue(student_can_trigger_ai(self.teacher, sub))
                else:
                    self.assertFalse(student_can_trigger_ai(self.teacher, sub))

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=0)
    def test_student_quota_zero_hides_button_teacher_sees_it_on_homework_ai(self):
        for key in ('hw1', 'hw2'):
            for kind, problem, sub in self._subs_for(key):
                supports = problem_supports_ai_feedback(problem)
                self.assertFalse(student_can_trigger_ai(self.student, sub), msg=kind)
                student_page = self._status(self.student_client, sub)
                self.assertEqual(student_page.status_code, 200)
                self.assertNotContains(student_page, 'View AI Feedback')
                teacher_page = self._status(self.teacher_client, sub)
                self.assertEqual(teacher_page.status_code, 200)
                if supports:
                    self.assertTrue(student_can_trigger_ai(self.teacher, sub), msg=kind)
                    self.assertContains(teacher_page, 'View AI Feedback')
                else:
                    self.assertFalse(student_can_trigger_ai(self.teacher, sub), msg=kind)
                    self.assertNotContains(teacher_page, 'View AI Feedback')

    def test_exam_student_no_button_teacher_can_trigger_ai_types(self):
        for key in ('ex1', 'ex2'):
            for kind, problem, sub in self._subs_for(key):
                student_page = self._status(self.student_client, sub)
                self.assertEqual(student_page.status_code, 200)
                self.assertNotContains(student_page, 'View AI Feedback')
                teacher_page = self._status(self.teacher_client, sub)
                self.assertEqual(teacher_page.status_code, 200)
                if problem_has_ai_grading(problem):
                    self.assertTrue(student_can_trigger_ai(self.teacher, sub), msg='%s %s' % (key, kind))
                    self.assertContains(teacher_page, 'View AI Feedback')
                else:
                    self.assertNotContains(teacher_page, 'View AI Feedback')

    def test_off_student_hidden_teacher_can_force_ai(self):
        """AI grading Off: students never see the button; teachers still can
        force a pass so they can verify LLM scores (same as grade-desk Rejudge)."""
        for key in ('off1', 'off2'):
            for kind, problem, sub in self._subs_for(key):
                student_page = self._status(self.student_client, sub)
                self.assertEqual(student_page.status_code, 200)
                self.assertNotContains(student_page, 'View AI Feedback')
                teacher_page = self._status(self.teacher_client, sub)
                self.assertEqual(teacher_page.status_code, 200)
                if problem_has_ai_grading(problem):
                    self.assertTrue(student_can_trigger_ai(self.teacher, sub), msg='%s %s' % (key, kind))
                    self.assertContains(teacher_page, 'View AI Feedback')
                else:
                    self.assertFalse(student_can_trigger_ai(self.teacher, sub), msg='%s %s' % (key, kind))
                    self.assertNotContains(teacher_page, 'View AI Feedback')

    def test_other_student_cannot_view_or_trigger(self):
        _, _, sub = self._subs_for('hw1')[0]
        page = self._status(self.other_client, sub)
        self.assertIn(page.status_code, (302, 403))
        resp = self._ai_post(self.other_client, sub)
        self.assertIn(resp.status_code, (302, 403))

    def test_anonymous_redirected(self):
        _, _, sub = self._subs_for('hw1')[0]
        anon = Client()
        self.assertEqual(anon.get(reverse('submission_status', args=[sub.id])).status_code, 302)
        self.assertEqual(anon.post(reverse('submission_ai_grade', args=[sub.id])).status_code, 302)

    def test_older_submission_cannot_trigger(self):
        kind, problem, cp = self.contests['hw1']['problems'][0]
        older = _done_sub(self.student, problem, self.contests['hw1']['contest'], 'old')
        newer = _done_sub(self.student, problem, self.contests['hw1']['contest'], 'new')
        self.assertFalse(student_can_trigger_ai(self.teacher, older))
        self.assertTrue(student_can_trigger_ai(self.teacher, newer))

    def test_queued_submission_cannot_trigger(self):
        kind, problem, cp = self.contests['hw1']['problems'][0]
        sub = _done_sub(self.student, problem, self.contests['hw1']['contest'], 'q', status='QU', result=None)
        self.assertFalse(student_can_trigger_ai(self.teacher, sub))
        self.assertFalse(student_can_trigger_ai(self.student, sub))

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=0)
    def test_student_post_rejected_when_quota_zero(self):
        for kind, problem, sub in self._subs_for('hw1'):
            if not problem_has_ai_grading(problem):
                continue
            resp = self._ai_post(self.student_client, sub)
            self.assertEqual(resp.status_code, 403, msg=kind)
            self.assertIn('not available', resp.json()['error'].lower())

    def test_teacher_post_skips_quota_and_runs(self):
        ai_sub = None
        for kind, problem, sub in self._subs_for('hw1'):
            if problem_has_ai_grading(problem):
                ai_sub = sub
                break
        with mock.patch('judge.llm.is_configured', return_value=True), \
                mock.patch('judge.utils.contest_ai.run_ai_for_submission', return_value='code-ai') as run:
            resp = self._ai_post(self.teacher_client, ai_sub)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()['ok'])
        run.assert_called_once()
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs.get('manual_trigger'))

    def test_teacher_post_on_mcq_forbidden(self):
        for kind, problem, sub in self._subs_for('hw1'):
            if kind == 'mcq':
                resp = self._ai_post(self.teacher_client, sub)
                self.assertEqual(resp.status_code, 403)
                return
        self.fail('no mcq')

    def test_unconfigured_llm_returns_503_for_teacher(self):
        for kind, problem, sub in self._subs_for('hw2'):
            if kind != 'sa':
                continue
            with mock.patch('judge.llm.is_configured', return_value=False):
                resp = self._ai_post(self.teacher_client, sub)
            self.assertEqual(resp.status_code, 503)
            return
        self.fail('no sa')

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=10)
    def test_student_with_quota_can_trigger_homework_ai_only(self):
        cache.clear()
        for key in ('hw1', 'hw2', 'ex1', 'off1'):
            for kind, problem, sub in self._subs_for(key):
                supports = problem_supports_ai_feedback(problem)
                if key in ('hw1', 'hw2') and supports:
                    # Master switch: assignment Allow alone on supported types.
                    self.assertTrue(student_can_trigger_ai(self.student, sub),
                                    msg='%s %s' % (key, kind))
                    page = self._status(self.student_client, sub)
                    self.assertContains(page, 'View AI Feedback')
                else:
                    self.assertFalse(student_can_trigger_ai(self.student, sub),
                                     msg='%s %s' % (key, kind))

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=10)
    def test_student_rate_limit_five_per_problem(self):
        cache.clear()
        with mock.patch('judge.llm.is_configured', return_value=True), \
                mock.patch('judge.utils.contest_ai.run_ai_for_submission', return_value='ok'):
            sa = [row for row in self._subs_for('hw2') if row[0] == 'sa'][0][2]
            for i in range(5):
                resp = self._ai_post(self.student_client, sa)
                self.assertEqual(resp.status_code, 200, 'attempt %s %s' % (i, resp.content))
            resp = self._ai_post(self.student_client, sa)
            self.assertEqual(resp.status_code, 429)
            self.assertIn('try again later', resp.json()['error'].lower())
            # Button stays available; limit is soft (no ban).
            page = self._status(self.student_client, sa)
            self.assertContains(page, 'View AI Feedback')
            # A different AI problem is still allowed (limit is per problem).
            code_ai = [row for row in self._subs_for('hw1') if row[0] == 'codeai'][0][2]
            resp = self._ai_post(self.student_client, code_ai)
            self.assertEqual(resp.status_code, 200, resp.content)

    def test_contest_pages_explain_mode_to_both_roles(self):
        for key in ('hw1', 'hw2'):
            url = reverse('contest_view', args=[key])
            for client in (self.student_client, self.teacher_client):
                html = client.get(url).content.decode().lower()
                self.assertIn('on demand', html, msg='%s' % key)
        for key in ('ex1', 'ex2', 'off1', 'off2'):
            url = reverse('contest_view', args=[key])
            for client in (self.student_client, self.teacher_client):
                html = client.get(url).content.decode().lower()
                self.assertNotIn('on demand', html, msg='%s' % key)

    def test_grade_page_batch_button_teacher_only(self):
        # Button shows whenever the contest has AI-enabled problems.
        for key, expect_btn in (
            ('hw1', True), ('hw2', True), ('ex1', True), ('ex2', True),
            ('off1', True), ('off2', True),
        ):
            url = reverse('contest_grade', args=[key])
            teacher = self.teacher_client.get(url)
            self.assertEqual(teacher.status_code, 200, key)
            html = teacher.content.decode()
            if expect_btn:
                if teacher.context is not None:
                    self.assertTrue(teacher.context['grade_ai_batch'])
                self.assertIn('aiBatchMode = true', html)
                self.assertTrue(('Batch grading' in html) or ('AI Feedback' in html))
            else:
                if teacher.context is not None:
                    self.assertFalse(teacher.context['grade_ai_batch'])
                self.assertIn('aiBatchMode = false', html)
            student = self.student_client.get(url)
            self.assertIn(student.status_code, (302, 403, 404))

    def test_batch_endpoint_student_forbidden_teachers_ok(self):
        student = self.student_client.post(reverse('contest_grade_ai', args=['ex1']))
        self.assertEqual(student.status_code, 403)
        with mock.patch('judge.tasks.contest.batch_ai_grade.delay'):
            with self.captureOnCommitCallbacks(execute=True):
                teacher_hw = self.teacher_client.post(reverse('contest_grade_ai', args=['hw1']))
                teacher_off = self.teacher_client.post(reverse('contest_grade_ai', args=['off1']))
                teacher_ex = self.teacher_client.post(reverse('contest_grade_ai', args=['ex1']))
        self.assertEqual(teacher_hw.status_code, 200, teacher_hw.content)
        self.assertEqual(teacher_off.status_code, 200, teacher_off.content)
        self.assertEqual(teacher_ex.status_code, 200, teacher_ex.content)
        self.assertTrue(teacher_ex.json()['ok'])

    def test_create_form_shows_student_ai_checkbox(self):
        editor = create_user(
            username='aie2e_editor',
            user_permissions=(
                'add_contest', 'edit_own_contest', 'edit_all_contest',
                'create_private_contest', 'change_contest_visibility',
            ),
        )
        client = Client()
        client.force_login(editor)
        html = client.get(reverse('contest_create')).content.decode()
        self.assertIn('id_allow_student_ai_grading', html)
        self.assertIn('Allow Student Use AI Feedback', html)
        self.assertIn('Allow Student Use AI Hint', html)
        self.assertNotIn('id_ai_grading_mode', html)

    def test_http_submit_short_answer_homework_does_not_queue_ai(self):
        info = self.contests['hw2']
        sa = [p for kind, p, _cp in info['problems'] if kind == 'sa'][0]
        self.student.profile.current_contest = info['part']
        self.student.profile.save()
        resp = self.student_client.post(
            reverse('problem_submit', args=[sa.code]),
            {'answer': 'the answer is 42'},
        )
        self.assertEqual(resp.status_code, 302, resp.content)
        sub = Submission.objects.filter(user=self.student.profile, problem=sa).latest('id')
        self.assertEqual(sub.contest_object_id, info['contest'].id)
        self.assertEqual(sub.status, 'D')
        self.assertNotEqual(sub.status, 'QU')
        self.assertEqual(sub.result, 'IR')

    def test_http_submit_mcq_never_offers_ai(self):
        info = self.contests['hw1']
        mcq = [p for kind, p, _cp in info['problems'] if kind == 'mcq'][0]
        opt = mcq.mcq_options.get(is_correct=True)
        self.student.profile.current_contest = info['part']
        self.student.profile.save()
        resp = self.student_client.post(
            reverse('problem_submit', args=[mcq.code]),
            {'options': str(opt.id)},
        )
        self.assertEqual(resp.status_code, 302, resp.content)
        sub = Submission.objects.filter(user=self.student.profile, problem=mcq).latest('id')
        self.assertEqual(sub.result, 'AC')
        page = self._status(self.student_client, sub)
        self.assertNotContains(page, 'View AI Feedback')
        page = self._status(self.teacher_client, sub)
        self.assertNotContains(page, 'View AI Feedback')

    def test_http_submit_fill_blank_exact_still_machine_grades_with_feedback_button(self):
        """Exact fill-blank keeps machine grading; assignment Allow still offers Feedback."""
        info = self.contests['hw2']
        fb = [p for kind, p, _cp in info['problems'] if kind == 'fbex'][0]
        self.student.profile.current_contest = info['part']
        self.student.profile.save()
        resp = self.student_client.post(
            reverse('problem_submit', args=[fb.code]),
            {'fb_0': 'paris'},
        )
        self.assertEqual(resp.status_code, 302, resp.content)
        sub = Submission.objects.filter(user=self.student.profile, problem=fb).latest('id')
        self.assertFalse(problem_has_ai_grading(fb))
        self.assertTrue(problem_supports_ai_feedback(fb))
        self.assertEqual(sub.result, 'AC')
        self.assertContains(self._status(self.student_client, sub), 'View AI Feedback')
        self.assertContains(self._status(self.teacher_client, sub), 'View AI Feedback')

    def test_http_submit_exam_fill_blank_ai_awaits_teacher(self):
        info = self.contests['ex1']
        fb = [p for kind, p, _cp in info['problems'] if kind == 'fbai'][0]
        self.student.profile.current_contest = info['part']
        self.student.profile.save()
        resp = self.student_client.post(
            reverse('problem_submit', args=[fb.code]),
            {'fb_0': 'yes'},
        )
        self.assertEqual(resp.status_code, 302, resp.content)
        sub = Submission.objects.filter(user=self.student.profile, problem=fb).latest('id')
        self.assertEqual(sub.status, 'D')
        self.assertEqual(sub.result, 'IR')
        self.assertNotEqual(sub.error, 'AI_BATCH_PENDING')
        student_page = self._status(self.student_client, sub)
        self.assertContains(student_page, 'Results hidden')
        self.assertNotContains(student_page, 'View AI Feedback')
        teacher_page = self._status(self.teacher_client, sub)
        self.assertContains(teacher_page, 'Answer saved')
        self.assertContains(teacher_page, 'View AI Feedback')

    def test_http_submit_exam_short_answer_awaits_teacher(self):
        info = self.contests['ex2']
        sa = [p for kind, p, _cp in info['problems'] if kind == 'sa'][0]
        self.student.profile.current_contest = info['part']
        self.student.profile.save()
        resp = self.student_client.post(
            reverse('problem_submit', args=[sa.code]),
            {'answer': 'batch me'},
        )
        self.assertEqual(resp.status_code, 302, resp.content)
        sub = Submission.objects.filter(user=self.student.profile, problem=sa).latest('id')
        self.assertEqual(sub.status, 'D')
        self.assertEqual(sub.result, 'IR')
        self.assertNotEqual(sub.error, 'AI_BATCH_PENDING')
        page = self._status(self.student_client, sub)
        self.assertContains(page, 'Results hidden')
        self.assertNotContains(page, 'View AI Feedback')
        teacher_page = self._status(self.teacher_client, sub)
        self.assertContains(teacher_page, 'Answer saved')
        self.assertContains(teacher_page, 'View AI Feedback')

    def test_http_submit_off_short_answer_finishes_without_student_ai_button(self):
        info = self.contests['off2']
        sa = [p for kind, p, _cp in info['problems'] if kind == 'sa'][0]
        self.student.profile.current_contest = info['part']
        self.student.profile.save()
        resp = self.student_client.post(
            reverse('problem_submit', args=[sa.code]),
            {'answer': 'should not ai'},
        )
        self.assertEqual(resp.status_code, 302, resp.content)
        sub = Submission.objects.filter(user=self.student.profile, problem=sa).latest('id')
        self.assertEqual(sub.status, 'D')
        self.assertNotEqual(sub.error, 'AI_BATCH_PENDING')
        self.assertNotContains(self._status(self.student_client, sub), 'View AI Feedback')
        # Teachers can still force AI even when students are not allowed.
        self.assertContains(self._status(self.teacher_client, sub), 'View AI Feedback')

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=10)
    def test_practice_short_answer_defers_to_ai_button(self):
        """No contest → submit stores the answer; LLM runs only on button."""
        p = create_problem(code='practiceai', submission_type='S', points=10, is_public=True)
        ProblemShortAnswerConfig.objects.create(problem=p, reference_answer='x', ai_enabled=True)
        self.student_client.post(
            reverse('problem_submit', args=[p.code]),
            {'answer': 'maybe x'},
        )
        sub = Submission.objects.filter(user=self.student.profile, problem=p).latest('id')
        self.assertFalse(should_run_ai_now(sub))
        self.assertTrue(should_run_ai_now(sub, manual_trigger=True))
        self.assertEqual(sub.result, 'IR')
        page = self._status(self.student_client, sub)
        self.assertContains(page, 'View AI Feedback')

    def test_practice_short_answer_ai_disabled_hides_button(self):
        p = create_problem(code='practiceoff', submission_type='S', points=10, is_public=True)
        ProblemShortAnswerConfig.objects.create(problem=p, reference_answer='x', ai_enabled=False)
        self.student_client.post(
            reverse('problem_submit', args=[p.code]),
            {'answer': 'maybe x'},
        )
        sub = Submission.objects.filter(user=self.student.profile, problem=p).latest('id')
        self.assertFalse(problem_has_ai_grading(p))
        self.assertFalse(should_run_ai_now(sub, manual_trigger=True))
        self.assertEqual(sub.result, 'IR')
        page = self._status(self.student_client, sub)
        self.assertContains(page, 'Answer saved')
        self.assertContains(page, 'Your answer has been saved.')
        self.assertNotContains(page, 'AI grading')
        self.assertNotContains(page, 'View AI Feedback')
        self.assertFalse(student_can_trigger_ai(self.student, sub))
        self.assertFalse(student_can_trigger_ai(self.teacher, sub))


class CodeAIJudgeIRTestCase(CommonDataMixin, TestCase):
    """A judge ``IR`` (Invalid Return) on a code+AI problem is a real verdict.

    Before the fix, ``contest_awaits_student_ai`` treated every AI-graded problem
    (code included) as "awaiting AI" on submit, so a genuine judge ``IR`` was
    rendered as "Answer saved" and the real test-case results were hidden.
    Code problems always flow through the judge bridge, so their ``IR`` is an
    Invalid Return — never the synthetic awaiting-AI placeholder used by the
    internally-graded AI types (short answer / AI fill-blank).
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.teacher = create_user(
            username='codeir_teacher',
            user_permissions=('view_all_submission', 'rejudge_submission'),
        )
        cls.student = create_user(username='codeir_student')
        cls.py = Language.objects.get(key='PY3')
        cls.codeai = create_problem(code='codeir-ai', submission_type='C', points=10, is_public=True)
        cls.codeai.allowed_languages.add(cls.py)
        ProblemCodeConfig.objects.create(problem=cls.codeai, ai_enabled=True, ai_rubric='style')
        cls.codeno = create_problem(code='codeir-no', submission_type='C', points=10, is_public=True)
        cls.codeno.allowed_languages.add(cls.py)
        ProblemCodeConfig.objects.create(problem=cls.codeno, ai_enabled=False)
        cls.sa = create_problem(code='codeir-sa', submission_type='S', points=10, is_public=True)
        ProblemShortAnswerConfig.objects.create(problem=cls.sa, reference_answer='42', ai_enabled=True)

    def setUp(self):
        cache.clear()
        self.student_client = Client()
        self.student_client.force_login(self.student)
        self.teacher_client = Client()
        self.teacher_client.force_login(self.teacher)

    def _code_sub(self, problem, result, *cases):
        sub = Submission.objects.create(
            user=self.student.profile, problem=problem, language=self.py,
            status='D', result=result, points=0,
            case_points=0, case_total=float(problem.points or 0),
            time=0, memory=0,
        )
        SubmissionSource.objects.create(submission=sub, source='print(1)')
        for i, status in enumerate(cases, start=1):
            SubmissionTestCase.objects.create(
                submission=sub, case=i, status=status, points=0, total=5, time=0, memory=0,
            )
        return sub

    def _status(self, client, sub):
        return client.get(reverse('submission_status', args=[sub.id]))

    def test_contest_awaits_student_ai_excludes_code(self):
        code_ai_sub = self._code_sub(self.codeai, 'WA', 'WA', 'WA')
        code_no_sub = self._code_sub(self.codeno, 'AC', 'AC')
        sa_sub = Submission.objects.create(
            user=self.student.profile, problem=self.sa, language=Language.get_answer_language(),
            status='D', result='IR', points=0, case_points=0, case_total=10,
        )
        SubmissionSource.objects.create(submission=sa_sub, source='{"text": "42"}')
        self.assertFalse(contest_awaits_student_ai(code_ai_sub))
        self.assertFalse(contest_awaits_student_ai(code_no_sub))
        # The internally-graded AI types still await the on-demand pass.
        self.assertTrue(contest_awaits_student_ai(sa_sub))

    def test_code_ir_judge_verdict_is_not_rendered_as_answer_saved(self):
        """A code+AI submission whose judge verdict is IR (Invalid Return) must
        show the real execution results — never the 'Answer saved' awaiting-AI
        state. The on-demand AI button must still be available for the teacher."""
        sub = self._code_sub(self.codeai, 'IR', 'IR', 'IR', 'IR')
        self.assertFalse(contest_awaits_student_ai(sub))
        widget = self.teacher_client.get(
            reverse('submission_testcases_query'), {'id': sub.id},
        )
        self.assertEqual(widget.status_code, 200, widget.content)
        self.assertContains(widget, 'Test results')
        self.assertNotContains(widget, 'Answer saved')
        # The real IR verdict rows are shown.
        self.assertContains(widget, 'case-IR')
        # Teachers can still trigger the supplementary AI rubric pass.
        page = self._status(self.teacher_client, sub)
        self.assertContains(page, 'View AI Feedback')
        self.assertNotContains(page, 'Answer saved')

    def test_code_wa_verdict_is_not_rendered_as_answer_saved(self):
        """Same for a plain WA verdict — only the synthetic awaiting-AI result
        may ever show 'Answer saved'."""
        sub = self._code_sub(self.codeai, 'WA', 'WA', 'WA', 'WA')
        self.assertFalse(contest_awaits_student_ai(sub))
        widget = self.teacher_client.get(
            reverse('submission_testcases_query'), {'id': sub.id},
        )
        self.assertEqual(widget.status_code, 200, widget.content)
        self.assertContains(widget, 'Test results')
        self.assertNotContains(widget, 'Answer saved')
        self.assertContains(widget, 'case-WA')

    def test_code_without_ai_never_awaits(self):
        sub = self._code_sub(self.codeno, 'IR', 'IR', 'IR')
        self.assertFalse(contest_awaits_student_ai(sub))
        widget = self.teacher_client.get(
            reverse('submission_testcases_query'), {'id': sub.id},
        )
        self.assertEqual(widget.status_code, 200, widget.content)
        self.assertContains(widget, 'Test results')
        self.assertNotContains(widget, 'Answer saved')
        self.assertNotContains(widget, 'View AI Feedback')
