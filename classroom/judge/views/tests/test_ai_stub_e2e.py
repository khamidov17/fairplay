"""AI features end to end against a local OpenAI-compatible stub (``scripts/ai_stub.py``).

The real ``judge.llm`` LiteLLM backend (the ``openai`` client) talks HTTP to the stub, which runs
in a thread on a free port. Each failure mode the stub offers (HTTP 500 carrying a fake provider
secret, 429, timeout, prose instead of JSON, a non-JSON body, empty content) must leave the student
with a sane message, nothing 500s, the answer stays saved for manual grading, the request is not
counted against the student, and nothing from the provider reaches the student. Model feedback that
quotes the reference answer, accepted blank answers or the rubric is withheld.
"""
import importlib.util
import json
import os
import threading
from datetime import timedelta
from http.server import ThreadingHTTPServer

from django.conf import settings
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge import llm
from judge.ai_grading_quota import get_remaining, rate_limit_count, rate_limit_remaining_count, student_quota_limit
from judge.models import (
    ContestParticipation, ContestSubmission, Language, ProblemBlank, ProblemFillBlankConfig,
    ProblemShortAnswerConfig, Submission, SubmissionSource, SubmissionTestCase,
)
from judge.models.question import GRADE_AI
from judge.models.tests.util import create_contest, create_contest_problem, create_problem, create_user
from judge.question_types import ai_grading

REFERENCE = 'Lists are mutable so you can change them after creation, tuples are immutable.'
RUBRIC = 'Award full marks only when mutability versus immutability is the explained distinction.'
ANSWER = 'You can append to a list but a tuple stays fixed.'
# Strings that must never reach a student: the stub's fake provider secret / internal URL,
# exception class names, and the stub's own address.
LEAK_MARKERS = ('stub-PROVIDER', '10.0.0.5', 'Traceback', 'APIStatusError', 'InternalServerError',
                'APITimeoutError', 'APIConnectionError', 'RateLimitError', 'LLMError', 'sk-test-key')


def withheld():
    return str(getattr(ai_grading, 'WITHHELD_FEEDBACK', '<no redaction>'))


def feedback_leaks(*args, **kwargs):
    return ai_grading.feedback_leaks(*args, **kwargs)


def _load_stub():
    path = os.path.join(settings.BASE_DIR, 'scripts', 'ai_stub.py')
    spec = importlib.util.spec_from_file_location('fairplay_ai_stub', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AIStubMixin:
    """Start the stub once per class and point ``judge.llm`` at it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.stub = _load_stub()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), cls.stub.Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls._llm_settings = override_settings(
            LLM_PROVIDER='litellm',
            LITELLM_API_BASE='http://127.0.0.1:%d' % cls.server.server_address[1],
            LITELLM_API_KEY='sk-test-key',
            LITELLM_DEFAULT_MODEL='stub-model',
            LITELLM_TIMEOUT=1,
        )
        cls._llm_settings.enable()

    @classmethod
    def tearDownClass(cls):
        cls._llm_settings.disable()
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        cache.clear()
        llm._backend = None
        self.addCleanup(setattr, llm, '_backend', None)
        self.set_mode('ok')
        self.stub.STATE['log'].clear()

    def set_mode(self, mode, delay=None):
        self.stub.STATE['mode'] = mode
        if delay is not None:
            self.stub.STATE['delay'] = delay

    def stub_calls(self, feature=None):
        return [c for c in self.stub.STATE['log'] if feature is None or c['feature'] == feature]

    def assertNoLeak(self, text):
        for marker in LEAK_MARKERS:
            self.assertNotIn(marker, text)


class ShortAnswerAIGradeE2ETestCase(AIStubMixin, TestCase):
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.student = create_user(username='aistub_student')
        cls.teacher = create_user(username='aistub_teacher', user_permissions=('edit_own_contest',))
        cls.problem = create_problem(code='aistubsa', submission_type='S', points=10, partial=True,
                                     description='In one or two sentences, explain lists vs tuples.')
        ProblemShortAnswerConfig.objects.create(
            problem=cls.problem, reference_answer=REFERENCE, ai_enabled=True, ai_rubric=RUBRIC,
            grading_table=[{'name': 'Concept', 'max_points': 5, 'strictness': 'normal'},
                           {'name': 'Application', 'max_points': 5, 'strictness': 'normal'}],
        )
        cls.contest = create_contest(
            key='aistubhw', authors=('aistub_teacher',), is_visible=True,
            start_time=timezone.now() - timedelta(hours=1), end_time=timezone.now() + timedelta(days=1),
            allow_student_ai_grading=True, allow_ai_hints=True,
        )
        cls.cp = create_contest_problem(contest=cls.contest, problem=cls.problem, points=10, partial=True)
        cls.part = ContestParticipation.objects.create(contest=cls.contest, user=cls.student.profile)
        cls.submission = Submission.objects.create(
            user=cls.student.profile, problem=cls.problem, language=Language.objects.get(key='PY3'),
            contest_object=cls.contest, status='D', result='IR', points=0, case_points=0, case_total=10,
        )
        SubmissionSource.objects.create(submission=cls.submission, source=json.dumps({'text': ANSWER}))
        ContestSubmission.objects.create(submission=cls.submission, problem=cls.cp, participation=cls.part,
                                         points=0)
        SubmissionTestCase.objects.create(submission=cls.submission, case=1, status='IR', points=0, total=10,
                                          extended_feedback='Request AI grading to score this submission.')

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.student)

    def grade(self):
        return self.client.post(reverse('submission_ai_grade', args=[self.submission.id]))

    def status_page(self):
        response = self.client.get(reverse('submission_status', args=[self.submission.id]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def fresh(self):
        return Submission.objects.get(pk=self.submission.pk)

    def feedbacks(self):
        return list(SubmissionTestCase.objects.filter(submission=self.submission)
                    .order_by('case').values_list('extended_feedback', flat=True))

    def test_ok_scores_each_rubric_row_with_feedback(self):
        response = self.grade()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['ok'])
        sub = self.fresh()
        self.assertIn(sub.result, ('AC', 'WA'))
        self.assertGreater(sub.case_points, 0)
        self.assertEqual(sub.case_total, 10)
        self.assertEqual(self.feedbacks(), ['Stub feedback for Concept: reasoning is incomplete.',
                                            'Stub feedback for Application: reasoning is incomplete.'])
        call = self.stub_calls('grade_short_answer')[0]
        self.assertTrue(call['has_auth'])
        self.assertEqual(call['response_format'], {'type': 'json_object'})
        html = self.status_page()
        self.assertIn('Stub feedback for Concept', html)
        self.assertNotIn(REFERENCE, html)
        self.assertNotIn(RUBRIC, html)
        self.assertEqual(rate_limit_remaining_count(self.student.profile.id, self.problem.id), rate_limit_count() - 1)

    def test_failure_modes_leave_answer_for_manual_grading_and_refund(self):
        for mode in ('error', 'ratelimit', 'timeout', 'garbage', 'badjson', 'empty'):
            with self.subTest(mode=mode):
                cache.clear()
                self.set_mode(mode, delay=3)
                response = self.grade()
                self.assertEqual(response.status_code, 503, response.content)
                body = response.content.decode()
                self.assertIn('AI grader is unavailable', response.json()['error'])
                self.assertNoLeak(body)
                sub = self.fresh()
                self.assertEqual((sub.status, sub.result, sub.points), ('D', 'IR', 0))
                self.assertEqual(self.feedbacks(), ['Awaiting manual grading (AI grader unavailable).'])
                # The failed request does not count against the student.
                self.assertEqual(rate_limit_remaining_count(self.student.profile.id, self.problem.id),
                                 rate_limit_count())
                self.assertEqual(get_remaining(self.student.profile.id), student_quota_limit())
                html = self.status_page()
                self.assertNoLeak(html)
                self.assertNotIn(REFERENCE, html)

    def test_failed_rerun_keeps_the_stored_ai_score(self):
        self.assertEqual(self.grade().status_code, 200)
        before = self.fresh()
        feedback_before = self.feedbacks()
        self.assertGreater(before.points, 0)
        self.set_mode('error')
        self.assertEqual(self.grade().status_code, 503)
        after = self.fresh()
        self.assertEqual((after.result, after.points, after.case_points), (before.result, before.points,
                                                                           before.case_points))
        self.assertEqual(self.feedbacks(), feedback_before)
        self.assertEqual(ContestSubmission.objects.get(submission=self.submission).points,
                         ContestSubmission.objects.get(submission=self.submission).points)
        self.assertGreater(ContestSubmission.objects.get(submission=self.submission).points, 0)

    def test_feedback_quoting_the_reference_answer_is_withheld(self):
        self.set_mode('leak')
        self.assertEqual(self.grade().status_code, 200)
        self.assertEqual(self.feedbacks(), [withheld()] * 2)
        html = self.status_page()
        self.assertNotIn(REFERENCE, html)
        self.assertNotIn('mutable so you can change them', html)

    def test_reply_with_one_row_is_not_full_marks(self):
        self.set_mode('partial')  # one row only, at full marks
        self.assertEqual(self.grade().status_code, 200)
        sub = self.fresh()
        self.assertEqual(sub.case_total, 10)
        self.assertLessEqual(sub.case_points, 5)
        self.assertEqual(sub.result, 'WA')

    def test_teacher_failure_message_and_no_quota(self):
        self.client.force_login(self.teacher)
        self.set_mode('error')
        response = self.grade()
        self.assertEqual(response.status_code, 503)
        self.assertNoLeak(response.content.decode())


class HiddenResultsExamAIGradeTestCase(AIStubMixin, TestCase):
    """In an exam whose results are hidden, the student may not run AI grading: the score and
    feedback could not be shown, and the run itself must not tell them anything."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.student = create_user(username='aistubexam_student')
        cls.problem = create_problem(code='aistubexsa', submission_type='S', points=10)
        ProblemShortAnswerConfig.objects.create(problem=cls.problem, reference_answer=REFERENCE, ai_enabled=True)
        cls.contest = create_contest(
            key='aistubexam', format_name='exam', is_visible=True, allow_student_ai_grading=True,
            start_time=timezone.now() - timedelta(hours=1), end_time=timezone.now() + timedelta(days=1),
        )
        cls.cp = create_contest_problem(contest=cls.contest, problem=cls.problem, points=10, hide_results=True)
        cls.part = ContestParticipation.objects.create(contest=cls.contest, user=cls.student.profile)
        cls.submission = Submission.objects.create(
            user=cls.student.profile, problem=cls.problem, language=Language.objects.get(key='PY3'),
            contest_object=cls.contest, status='D', result='IR', points=0, case_points=0, case_total=10,
        )
        SubmissionSource.objects.create(submission=cls.submission, source=json.dumps({'text': ANSWER}))
        ContestSubmission.objects.create(submission=cls.submission, problem=cls.cp, participation=cls.part,
                                         points=0)

    def test_student_cannot_trigger_and_model_is_not_called(self):
        client = Client()
        client.force_login(self.student)
        self.assertFalse(self.submission.results_visible_to(self.student))
        response = client.post(reverse('submission_ai_grade', args=[self.submission.id]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['error'], 'AI grading is not available while results are hidden.')
        self.assertEqual(self.stub_calls(), [])
        page = client.get(reverse('submission_status', args=[self.submission.id]))
        self.assertNotContains(page, 'id="ai-grade-btn"')


class FillBlankAIE2ETestCase(AIStubMixin, TestCase):
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.student = create_user(username='aistubfb_student')
        cls.problem = create_problem(code='aistubfb', submission_type='B', points=4, partial=True,
                                     description='Python prints text with the ___ function and counts with ___.')
        ProblemFillBlankConfig.objects.create(problem=cls.problem, grading_mode=GRADE_AI)
        ProblemBlank.objects.create(problem=cls.problem, order=0, label='', answers='print')
        ProblemBlank.objects.create(problem=cls.problem, order=1, label='', answers='len')
        cls.contest = create_contest(
            key='aistubfbhw', is_visible=True, allow_student_ai_grading=True,
            start_time=timezone.now() - timedelta(hours=1), end_time=timezone.now() + timedelta(days=1),
        )
        cls.cp = create_contest_problem(contest=cls.contest, problem=cls.problem, points=4, partial=True)
        cls.part = ContestParticipation.objects.create(contest=cls.contest, user=cls.student.profile)
        cls.submission = Submission.objects.create(
            user=cls.student.profile, problem=cls.problem, language=Language.objects.get(key='PY3'),
            contest_object=cls.contest, status='D', result='IR', points=0, case_points=0, case_total=4,
        )
        SubmissionSource.objects.create(submission=cls.submission,
                                        source=json.dumps({'answers': ['echo', 'size']}))
        ContestSubmission.objects.create(submission=cls.submission, problem=cls.cp, participation=cls.part,
                                         points=0)

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.student)

    def test_leaked_accepted_answer_is_withheld(self):
        self.set_mode('leak')
        response = self.client.post(reverse('submission_ai_grade', args=[self.submission.id]))
        self.assertEqual(response.status_code, 200, response.content)
        feedback = list(SubmissionTestCase.objects.filter(submission=self.submission)
                        .order_by('case').values_list('extended_feedback', flat=True))
        self.assertEqual(feedback, [withheld()] * 2)
        html = self.client.get(reverse('submission_status', args=[self.submission.id])).content.decode()
        self.assertNotIn('The correct answer is', html)

    def test_model_failure_falls_back_to_exact_match_and_says_so(self):
        self.set_mode('error')
        response = self.client.post(reverse('submission_ai_grade', args=[self.submission.id]))
        self.assertEqual(response.status_code, 503)
        self.assertNoLeak(response.content.decode())
        sub = Submission.objects.get(pk=self.submission.pk)
        # Exact matching still scored the answer (both blanks wrong).
        self.assertEqual((sub.status, sub.result, sub.points), ('D', 'WA', 0))
        self.assertEqual(rate_limit_remaining_count(self.student.profile.id, self.problem.id), rate_limit_count())


class BatchAIGradeE2ETestCase(AIStubMixin, TestCase):
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.teacher = create_user(username='aistubbatch_teacher', user_permissions=('edit_own_contest',))
        cls.student = create_user(username='aistubbatch_student')
        cls.problem = create_problem(code='aistubbatch', submission_type='S', points=10, partial=True)
        ProblemShortAnswerConfig.objects.create(problem=cls.problem, reference_answer=REFERENCE, ai_enabled=True)
        cls.contest = create_contest(
            key='aistubbatch', authors=('aistubbatch_teacher',), is_visible=True,
            start_time=timezone.now() - timedelta(hours=1), end_time=timezone.now() + timedelta(days=1),
        )
        cls.cp = create_contest_problem(contest=cls.contest, problem=cls.problem, points=10, partial=True)
        cls.part = ContestParticipation.objects.create(contest=cls.contest, user=cls.student.profile)
        cls.submission = Submission.objects.create(
            user=cls.student.profile, problem=cls.problem, language=Language.objects.get(key='PY3'),
            contest_object=cls.contest, status='D', result='IR', points=0, case_points=0, case_total=10,
        )
        SubmissionSource.objects.create(submission=cls.submission, source=json.dumps({'text': ANSWER}))
        ContestSubmission.objects.create(submission=cls.submission, problem=cls.cp, participation=cls.part,
                                         points=0)

    def run_batch(self):
        from judge.tasks.contest import batch_ai_grade
        return batch_ai_grade.apply(args=[self.contest.key]).get()

    def test_batch_ok_then_outage_reports_failures_and_keeps_scores(self):
        client = Client()
        client.force_login(self.teacher)
        self.assertEqual(self.run_batch(), 1)
        scored = Submission.objects.get(pk=self.submission.pk)
        self.assertGreater(scored.points, 0)
        status = client.get(reverse('contest_grade_ai', args=[self.contest.key])).json()
        self.assertEqual((status['graded'], status['failed']), (1, 0))

        self.set_mode('error')
        self.assertEqual(self.run_batch(), 0)
        after = Submission.objects.get(pk=self.submission.pk)
        self.assertEqual((after.result, after.points), (scored.result, scored.points))
        status = client.get(reverse('contest_grade_ai', args=[self.contest.key])).json()
        self.assertEqual((status['graded'], status['failed']), (0, 1))
        page = client.get(reverse('contest_grade', args=[self.contest.key]))
        self.assertContains(page, 'id="ai-batch-failed"')
        self.assertNoLeak(page.content.decode())


class AIHintE2ETestCase(AIStubMixin, TestCase):
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.student = create_user(username='aistubhint_student')
        cls.problem = create_problem(code='aistubhint', is_public=True)
        cls.contest = create_contest(
            key='aistubhinthw', is_visible=True, allow_ai_hints=True,
            start_time=timezone.now() - timedelta(hours=1), end_time=timezone.now() + timedelta(days=1),
        )
        cls.cp = create_contest_problem(contest=cls.contest, problem=cls.problem)
        cls.part = ContestParticipation.objects.create(contest=cls.contest, user=cls.student.profile)
        cls.submission = Submission.objects.create(
            user=cls.student.profile, problem=cls.problem, language=Language.objects.get(key='PY3'),
            contest_object=cls.contest, status='D', result='WA',
        )
        SubmissionSource.objects.create(submission=cls.submission, source='print(1)')
        ContestSubmission.objects.create(submission=cls.submission, problem=cls.cp, participation=cls.part,
                                         points=0)

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.student)
        self.student.profile.current_contest = self.part
        self.student.profile.save()

    def hint(self):
        return self.client.post(reverse('submission_ai_hint', args=[self.submission.id]))

    def test_hint_ok_counts_down(self):
        response = self.hint()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('Stub hint', response.json()['hint'])
        self.assertEqual(response.json()['remaining'], settings.DMOJ_AI_HINT_WINDOW_LIMIT - 1)
        self.assertEqual(len(self.stub_calls('hint')), 1)

    def test_hint_failure_modes_are_refunded_and_do_not_leak(self):
        for mode in ('error', 'timeout', 'badjson', 'empty'):
            with self.subTest(mode=mode):
                self.set_mode(mode, delay=3)
                response = self.hint()
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()['error'], 'Could not reach the AI service. Please try again later.')
                self.assertNoLeak(response.content.decode())
        self.set_mode('ok')
        self.assertEqual(self.hint().json()['remaining'], settings.DMOJ_AI_HINT_WINDOW_LIMIT - 1)

    def test_status_page_quota_wording_is_per_window(self):
        page = self.client.get(reverse('submission_status', args=[self.submission.id])).content.decode()
        self.assertIn('id="ai-hint-btn"', page)
        self.assertNotIn('remaining today', page)


class LLMBackendE2ETestCase(AIStubMixin, TestCase):
    def test_every_failure_mode_raises_llm_error(self):
        messages = [{'role': 'system', 'content': 'You are a programming tutor.'},
                    {'role': 'user', 'content': 'x'}]
        self.assertIn('Stub hint', llm.chat(messages).text)
        for mode in ('error', 'ratelimit', 'timeout', 'badjson', 'empty'):
            with self.subTest(mode=mode):
                self.set_mode(mode, delay=3)
                with self.assertRaises(llm.LLMError):
                    llm.chat(messages)


class AuthoringAIE2ETestCase(AIStubMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.teacher = create_user(username='aistubauthor', user_permissions=('create_private_problem',))

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.teacher)

    def post(self, name, payload):
        return self.client.post(reverse(name), data=json.dumps(payload), content_type='application/json')

    def test_difficulty_and_decompose_ok(self):
        response = self.post('problem_create_v2_ai_difficulty', {'description': 'Explain lists vs tuples.'})
        self.assertEqual(response.json(), {'level': 'medium', 'label': 'Medium'})
        response = self.post('problem_create_v2_ai_decompose', {'description': 'Explain.', 'total_points': 10})
        self.assertEqual(response.json()['sub_questions'],
                         [{'name': 'Concept', 'max_points': 5.0}, {'name': 'Application', 'max_points': 5.0}])

    def test_failures_are_503_without_provider_detail(self):
        for mode in ('error', 'badjson', 'empty', 'timeout'):
            with self.subTest(mode=mode):
                self.set_mode(mode, delay=3)
                for name in ('problem_create_v2_ai_difficulty', 'problem_create_v2_ai_decompose'):
                    response = self.post(name, {'description': 'Explain lists vs tuples.', 'total_points': 10})
                    self.assertEqual(response.status_code, 503)
                    self.assertNoLeak(response.content.decode())


class FeedbackLeakDetectionTestCase(TestCase):
    def test_short_secret_must_appear_whole(self):
        self.assertTrue(feedback_leaks('Use print here.', ['print']))
        self.assertFalse(feedback_leaks('Your printing is off.', ['print']))

    def test_long_secret_leaks_on_a_five_word_run(self):
        secret = 'Lists are mutable so you can change them after creation.'
        self.assertTrue(feedback_leaks('Remember: LISTS are Mutable, so YOU can... later', [secret]))
        self.assertFalse(feedback_leaks('Think about whether lists are mutable.', [secret]))

    def test_text_the_student_already_has_is_not_a_leak(self):
        self.assertFalse(feedback_leaks('You wrote print, which is right.', ['print'],
                                        public_text='print'))

    def test_cjk_secret(self):
        self.assertTrue(feedback_leaks('参考答案：列表是可变的而元组不是', ['列表是可变的而元组不是']))
        self.assertFalse(feedback_leaks('请再想想可变性', ['列表是可变的而元组不是']))

    def test_markup_and_empty(self):
        self.assertTrue(feedback_leaks('<b>print</b>', ['print']))
        self.assertFalse(feedback_leaks('', ['print']))
        self.assertFalse(feedback_leaks('anything', ['', None]))
