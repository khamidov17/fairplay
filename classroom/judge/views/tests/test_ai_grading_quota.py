"""Tests for student AI grading quota and per-problem rate limiting.

Covers the public API of ``judge.ai_grading_quota``:

* the global per-user quota (``DMOJ_AI_GRADING_STUDENT_QUOTA``),
* the per-(user, problem) sliding rate limit — window length and max requests
  per window — driven by the settings ``DMOJ_AI_GRADING_RATE_WINDOW_SECONDS`` /
  ``DMOJ_AI_GRADING_RATE_LIMIT``,
* teacher exemption (``is_teacher_viewer``),
* the on-demand AI grading endpoint reporting the remaining window.
"""
import re
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from judge.ai_grading_quota import (
    get_remaining,
    is_rate_limited,
    is_teacher_viewer,
    rate_limit_count,
    rate_limit_remaining_count,
    rate_limit_remaining_seconds,
    rate_window_seconds,
    refund,
    student_quota_limit,
    try_reserve,
)
from judge.models import Language, Submission, SubmissionSource
from judge.models.tests.util import CommonDataMixin, create_contest, create_problem, create_user


class _FakeClock:
    """Controllable replacement for ``time.time``.

    Advances one second per call so every ``time.time()`` inside
    ``try_reserve`` returns a distinct, increasing timestamp.
    """

    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        self.now += 1.0
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _sub(user, problem, contest=None, *, source='x'):
    """Build a done submission row (never actually judged)."""
    sub = Submission.objects.create(
        user=user.profile, problem=problem, language=Language.get_default_language(),
        status='D', result='AC', contest_object=contest,
        time=0, memory=0, points=0,
        case_points=0, case_total=float(problem.points or 0),
    )
    SubmissionSource.objects.create(submission=sub, source=source)
    return sub


class AIGradingQuotaTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.student = create_user(username='ai_quota_student')
        # A contest author with no special perms: only their contest membership
        # may grant teacher-viewer status.
        cls.contest_author = create_user(username='ai_quota_author')

    def setUp(self):
        cache.clear()

    # ------------------------------------------------------------------
    # Settings accessors
    # ------------------------------------------------------------------

    def test_settings_defaults(self):
        # Values shipped in dmoj/settings.py: students get a 100-request global
        # quota, rate limit 5 requests per 300s sliding window.
        self.assertEqual(student_quota_limit(), 100)
        self.assertEqual(rate_window_seconds(), 300)
        self.assertEqual(rate_limit_count(), 5)

    @override_settings(
        DMOJ_AI_GRADING_STUDENT_QUOTA=20,
        DMOJ_AI_GRADING_RATE_LIMIT=3,
        DMOJ_AI_GRADING_RATE_WINDOW_SECONDS=60,
    )
    def test_settings_accessors_reflect_overrides(self):
        self.assertEqual(student_quota_limit(), 20)
        self.assertEqual(rate_window_seconds(), 60)
        self.assertEqual(rate_limit_count(), 3)

    # ------------------------------------------------------------------
    # Global per-user quota
    # ------------------------------------------------------------------

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=0)
    def test_quota_disabled_blocks_reserve(self):
        ok, err = try_reserve(self.student.profile.id, 1)
        self.assertFalse(ok)
        self.assertEqual(err, 'quota_disabled')
        # A disabled quota reports zero remaining, never a negative number.
        self.assertEqual(get_remaining(self.student.profile.id), 0)

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=10)
    def test_reserve_decrements_quota(self):
        uid = self.student.profile.id
        self.assertEqual(get_remaining(uid), 10)
        ok, err = try_reserve(uid, 7)
        self.assertTrue(ok, err)
        self.assertEqual(get_remaining(uid), 9)

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=2)
    def test_quota_exhausted_returns_error_and_rolls_back(self):
        uid = self.student.profile.id
        for _ in range(2):
            ok, err = try_reserve(uid, 1)
            self.assertTrue(ok, err)
        ok, err = try_reserve(uid, 1)
        self.assertFalse(ok)
        self.assertEqual(err, 'quota_exhausted')
        # The overflowing increment is refunded so the counter never exceeds
        # the limit and remaining never goes negative.
        self.assertEqual(get_remaining(uid), 0)

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=10)
    def test_refund_restores_a_slot(self):
        uid = self.student.profile.id
        try_reserve(uid, 1)
        try_reserve(uid, 1)
        self.assertEqual(get_remaining(uid), 8)
        refund(uid)
        self.assertEqual(get_remaining(uid), 9)

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=10)
    def test_quota_is_independent_per_user(self):
        other = create_user(username='ai_quota_other')
        self.assertEqual(get_remaining(other.profile.id), 10)
        for _ in range(3):
            try_reserve(self.student.profile.id, 1)
        self.assertEqual(get_remaining(self.student.profile.id), 7)
        self.assertEqual(get_remaining(other.profile.id), 10)

    # ------------------------------------------------------------------
    # Per-problem rate limit
    # ------------------------------------------------------------------

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=100)
    def test_default_rate_limit_five_per_window(self):
        uid = self.student.profile.id
        pid = 42
        for i in range(5):
            ok, err = try_reserve(uid, pid)
            self.assertTrue(ok, 'attempt %d: %s' % (i, err))
        self.assertTrue(is_rate_limited(uid, pid))
        ok, err = try_reserve(uid, pid)
        self.assertFalse(ok)
        self.assertTrue(err.startswith('rate_limited'))

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=100)
    def test_rate_limited_attempts_stay_blocked(self):
        uid = self.student.profile.id
        pid = 42
        for _ in range(5):
            try_reserve(uid, pid)
        # Repeating the request during the cooldown keeps failing.
        for _ in range(3):
            ok, err = try_reserve(uid, pid)
            self.assertFalse(ok)
            self.assertTrue(err.startswith('rate_limited'))

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=100)
    def test_rate_limit_is_isolated_per_problem(self):
        uid = self.student.profile.id
        for _ in range(5):
            try_reserve(uid, 1)
        self.assertTrue(is_rate_limited(uid, 1))
        # A different problem has its own budget.
        self.assertFalse(is_rate_limited(uid, 2))
        ok, err = try_reserve(uid, 2)
        self.assertTrue(ok, err)

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=100)
    def test_rate_limit_is_isolated_per_user(self):
        other = create_user(username='ai_quota_rate_other')
        for _ in range(5):
            try_reserve(self.student.profile.id, 1)
        self.assertTrue(is_rate_limited(self.student.profile.id, 1))
        self.assertFalse(is_rate_limited(other.profile.id, 1))
        ok, err = try_reserve(other.profile.id, 1)
        self.assertTrue(ok, err)

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=100)
    def test_rate_limited_attempt_does_not_consume_quota(self):
        uid = self.student.profile.id
        pid = 42
        for _ in range(5):
            try_reserve(uid, pid)
        remaining_before = get_remaining(uid)
        self.assertEqual(remaining_before, 95)
        # The blocked attempt returns at the top of try_reserve and must not
        # increment the global quota counter.
        ok, _ = try_reserve(uid, pid)
        self.assertFalse(ok)
        self.assertEqual(get_remaining(uid), remaining_before)

    @override_settings(
        DMOJ_AI_GRADING_STUDENT_QUOTA=10,
        DMOJ_AI_GRADING_RATE_LIMIT=2,
    )
    def test_rate_limit_count_is_configurable(self):
        pid = 99
        uid = self.student.profile.id
        for _ in range(2):
            ok, err = try_reserve(uid, pid)
            self.assertTrue(ok, err)
        self.assertTrue(is_rate_limited(uid, pid))
        ok, err = try_reserve(uid, pid)
        self.assertFalse(ok)
        self.assertTrue(err.startswith('rate_limited'))
        # Soft limit: another attempt still counts as rate_limited (no ban key).
        ok2, err2 = try_reserve(uid, pid)
        self.assertFalse(ok2)
        self.assertTrue(err2.startswith('rate_limited'))

    @override_settings(
        DMOJ_AI_GRADING_STUDENT_QUOTA=10,
        DMOJ_AI_GRADING_RATE_LIMIT=2,
        DMOJ_AI_GRADING_RATE_WINDOW_SECONDS=300,
    )
    def test_window_prunes_stale_timestamps(self):
        """Old requests fall out of the window, so the student can try again."""
        pid = 5
        uid = self.student.profile.id
        clock = _FakeClock()
        with mock.patch('judge.ai_grading_quota.time.time', clock):
            ok, err = try_reserve(uid, pid)
            self.assertTrue(ok, err)
            ok, err = try_reserve(uid, pid)
            self.assertTrue(ok, err)
            self.assertTrue(is_rate_limited(uid, pid))
            # Enough time passes that both reservations are outside the window.
            clock.advance(600)
            self.assertFalse(is_rate_limited(uid, pid))
            ok, err = try_reserve(uid, pid)
            self.assertTrue(ok, err)
            self.assertFalse(is_rate_limited(uid, pid))

    @override_settings(
        DMOJ_AI_GRADING_STUDENT_QUOTA=10,
        DMOJ_AI_GRADING_RATE_LIMIT=1,
    )
    def test_rate_limit_remaining_seconds(self):
        pid = 7
        uid = self.student.profile.id
        # Window not filled yet: 0 remaining seconds.
        self.assertEqual(rate_limit_remaining_seconds(uid, pid), 0)
        try_reserve(uid, pid)
        self.assertTrue(is_rate_limited(uid, pid))
        secs = rate_limit_remaining_seconds(uid, pid)
        self.assertGreater(secs, 0)
        # The oldest reservation ages out after the configured window.
        self.assertLessEqual(secs, rate_window_seconds() + 1)

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=10)
    def test_rate_limit_remaining_count(self):
        pid = 11
        uid = self.student.profile.id
        # The whole window is available before any call.
        self.assertEqual(rate_limit_remaining_count(uid, pid), rate_limit_count())
        try_reserve(uid, pid)
        self.assertEqual(rate_limit_remaining_count(uid, pid), rate_limit_count() - 1)
        for _ in range(rate_limit_count() - 1):
            try_reserve(uid, pid)
        self.assertEqual(rate_limit_remaining_count(uid, pid), 0)

    @override_settings(
        DMOJ_AI_GRADING_STUDENT_QUOTA=10,
        DMOJ_AI_GRADING_RATE_LIMIT=1,
        DMOJ_AI_GRADING_RATE_WINDOW_SECONDS=42,
    )
    def test_remaining_seconds_respects_configured_window(self):
        pid = 7
        uid = self.student.profile.id
        ok, err = try_reserve(uid, pid)
        self.assertTrue(ok, err)
        # Hitting the window limit reports a wait bounded by the configured
        # window length.
        self.assertTrue(is_rate_limited(uid, pid))
        secs = rate_limit_remaining_seconds(uid, pid)
        self.assertGreater(secs, 0)
        self.assertLessEqual(secs, 43)

    # ------------------------------------------------------------------
    # Teacher exemption
    # ------------------------------------------------------------------

    def test_is_teacher_viewer_false_for_anonymous(self):
        self.assertFalse(is_teacher_viewer(self.users['anonymous'], None))

    def test_is_teacher_viewer_false_for_plain_student(self):
        problem = create_problem(code='ai_quota_problem', is_public=True)
        sub = _sub(self.student, problem)
        self.assertFalse(is_teacher_viewer(self.student, sub))

    def test_is_teacher_viewer_false_for_bare_staff(self):
        # Staff flag alone is NOT teacher status — explicit permissions are
        # required (never group/roster membership).
        staff = create_user(username='ai_quota_staff', is_staff=True)
        problem = create_problem(code='ai_quota_staff', is_public=True)
        sub = _sub(self.student, problem)
        self.assertFalse(is_teacher_viewer(staff, sub))

    def test_is_teacher_viewer_false_with_bare_rejudge_permission(self):
        # G-5: a site-wide permission alone does not make someone the teacher
        # of a submission they do not edit.
        grader = create_user(username='ai_quota_grader', user_permissions=('rejudge_submission',))
        problem = create_problem(code='ai_quota_grader', is_public=True)
        sub = _sub(self.student, problem)
        self.assertFalse(is_teacher_viewer(grader, sub))

    def test_is_teacher_viewer_true_for_practice_problem_editor(self):
        author = create_user(username='ai_quota_prob_author', user_permissions=('edit_own_problem',))
        problem = create_problem(code='ai_quota_prob_author', is_public=True, authors=('ai_quota_prob_author',))
        sub = _sub(self.student, problem)
        self.assertTrue(is_teacher_viewer(author, sub))

    def test_is_teacher_viewer_true_with_edit_all_contest_permission(self):
        editor = create_user(username='ai_quota_editor', user_permissions=('edit_all_contest',))
        problem = create_problem(code='ai_quota_editor', is_public=True)
        sub = _sub(self.student, problem)
        self.assertTrue(is_teacher_viewer(editor, sub))

    def test_is_teacher_viewer_false_with_create_course_permission(self):
        # G-5: judge.add_organization is held by every instructor (migration
        # 0206). It must not let a teacher of course A re-run AI scoring on a
        # submission of course B's assignment, nor on someone else's practice.
        teacher = create_user(username='ai_quota_site_teacher', user_permissions=('add_organization',))
        problem = create_problem(code='ai_quota_teacher', is_public=True)
        sub = _sub(self.student, problem)
        self.assertFalse(is_teacher_viewer(teacher, sub))
        contest = create_contest(key='aiq_other', authors=('ai_quota_author',))
        sub = _sub(self.student, problem, contest)
        self.assertFalse(is_teacher_viewer(teacher, sub))

    def test_is_teacher_viewer_true_for_contest_editor(self):
        # The author has no special permissions; only their contest
        # membership grants teacher status.
        contest = create_contest(key='ai_quota_contest', authors=('ai_quota_author',))
        problem = create_problem(code='ai_quota_contest', is_public=True)
        sub = _sub(self.student, problem, contest)
        self.assertTrue(is_teacher_viewer(self.contest_author, sub))
        # A plain student still does not count as a teacher for that contest.
        self.assertFalse(is_teacher_viewer(self.student, sub))

    # ------------------------------------------------------------------
    # End-to-end: the on-demand AI grading view
    # ------------------------------------------------------------------

    @override_settings(
        DMOJ_AI_GRADING_STUDENT_QUOTA=10,
        DMOJ_AI_GRADING_RATE_LIMIT=2,
        DMOJ_AI_GRADING_RATE_WINDOW_SECONDS=42,
    )
    def test_ai_grade_view_reports_configured_window(self):
        """The 429 response tells the student to wait within the configured window."""
        problem = create_problem(code='ai_quota_sa', submission_type='S', points=20, is_public=True)
        sub = _sub(self.student, problem)
        # Exhaust the per-problem budget without going through the view.
        uid = self.student.profile.id
        for _ in range(2):
            ok, err = try_reserve(uid, problem.id)
            self.assertTrue(ok, err)
        self.assertTrue(is_rate_limited(uid, problem.id))

        client = Client()
        client.force_login(self.student)
        with mock.patch('judge.llm.is_configured', return_value=True):
            resp = client.post(reverse('submission_ai_grade', args=[sub.id]))
        self.assertEqual(resp.status_code, 429, resp.content)
        match = re.search(rb'Try again in (\d+) seconds', resp.content)
        self.assertIsNotNone(match, resp.content)
        # The reported wait comes from the configured 42s window, not from
        # any hardcoded default (which used to be 300s).
        secs = int(match.group(1))
        self.assertGreater(secs, 0)
        self.assertLessEqual(secs, 43)

    def test_ai_grade_view_refuses_instructor_of_another_course(self):
        """G-5: an instructor (add_organization) who does not edit the assignment
        cannot re-run AI scoring on its submissions through the view."""
        from judge.models import ProblemShortAnswerConfig
        problem = create_problem(code='ai_quota_g5', submission_type='S', points=10, is_public=True)
        ProblemShortAnswerConfig.objects.create(problem=problem, ai_enabled=True, reference_answer='r')
        contest = create_contest(key='ai_quota_g5', authors=('ai_quota_author',))
        sub = _sub(self.student, problem, contest)
        outsider = create_user(username='ai_quota_g5_outsider', user_permissions=('add_organization',))

        client = Client()
        client.force_login(outsider)
        with mock.patch('judge.llm.is_configured', return_value=True), \
                mock.patch('judge.utils.contest_ai.run_ai_for_submission') as run_ai:
            resp = client.post(reverse('submission_ai_grade', args=[sub.id]))
        self.assertIn(resp.status_code, (403, 404), resp.content)
        run_ai.assert_not_called()

class AIGradingRateLimitConcurrencyTestCase(TestCase):
    """AI-8: parallel requests cannot all read the same short rate list and pass."""

    @override_settings(DMOJ_AI_GRADING_STUDENT_QUOTA=100, DMOJ_AI_GRADING_RATE_LIMIT=5)
    def test_parallel_burst_respects_the_window(self):
        import threading
        import time as real_time

        from judge import ai_grading_quota

        cache.clear()
        real_recent = ai_grading_quota._recent_timestamps

        def slow_recent(user_id, problem_id):
            # Widen the read-to-write gap so an unlocked implementation lets
            # every thread read the same (empty) list.
            result = real_recent(user_id, problem_id)
            real_time.sleep(0.05)
            return result

        barrier = threading.Barrier(10)
        results = []

        def worker():
            barrier.wait()
            results.append(try_reserve(4242, 7)[0])

        with mock.patch.object(ai_grading_quota, '_recent_timestamps', side_effect=slow_recent):
            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(sum(results), 5)
        self.assertEqual(get_remaining(4242), 95)
