"""Contest-level AI Feedback and post-end objective auto-grade.

Teachers may run AI Feedback from the submission button or Grade-page batch
when the problem has AI config, or (lightweight) when the assignment allows
student Feedback on a supported type. For students in an assignment, the
``allow_student_ai_grading`` flag is the master switch: it alone controls
whether View AI Feedback appears on supported problem types (programming,
short answer, fill-blank). Practice (no contest) still requires problem-level
AI enable. Empty rubric / grading tables fall back to Overall at runtime.
Submit never auto-invokes the LLM.

``ai_grading_auto_delay_minutes`` schedules objective machine grading after
``end_time`` (MCQ / true-false / exact fill-blank) — not the LLM.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)

AI_BATCH_PENDING = 'AI_BATCH_PENDING'


def problem_has_ai_grading(problem):
    """True when this problem has problem-level AI Feedback / rubric config on."""
    from judge.models import ProblemSubmissionType
    from judge.models.question import GRADE_AI

    st = problem.submission_type
    if st == ProblemSubmissionType.SHORT_ANSWER:
        try:
            return bool(problem.short_answer_config.ai_enabled)
        except Exception:
            return False
    if st == ProblemSubmissionType.FILL_BLANK:
        try:
            return problem.fill_blank_config.grading_mode == GRADE_AI
        except Exception:
            return False
    if st == ProblemSubmissionType.CODE:
        try:
            return bool(problem.code_config.ai_enabled)
        except Exception:
            return False
    return False


def problem_supports_ai_feedback(problem):
    """Problem types that can send statement + answer to the LLM for Feedback."""
    from judge.models import ProblemSubmissionType

    return problem.submission_type in (
        ProblemSubmissionType.CODE,
        ProblemSubmissionType.SHORT_ANSWER,
        ProblemSubmissionType.FILL_BLANK,
    )


def lightweight_ai_feedback_ok(submission, *, user=None, contest=None):
    """Assignment Allow ON enables Feedback without problem-level AI config."""
    if not problem_supports_ai_feedback(submission.problem):
        return False
    if user is None:
        try:
            user = submission.user.user
        except Exception:
            user = None
    resolved = resolve_contest_for_ai(submission, user=user, contest=contest)
    return resolved is not None and student_allowed_ai_grading(resolved)


def ai_feedback_callable(submission, *, user=None, contest=None):
    """Whether an on-demand / batch AI Feedback pass can run for this row."""
    return (
        problem_has_ai_grading(submission.problem)
        or lightweight_ai_feedback_ok(submission, user=user, contest=contest)
    )


def problem_is_objective_machine_graded(problem):
    """MCQ / true-false / exact fill-blank — machine score, no LLM."""
    from judge.models import ProblemSubmissionType

    st = problem.submission_type
    if st == ProblemSubmissionType.MCQ:
        return True
    if st == ProblemSubmissionType.FILL_BLANK:
        return not problem_has_ai_grading(problem)
    return False


def contest_ai_mode(contest):
    if contest is None:
        return None
    return getattr(contest, 'ai_grading_mode', None) or 'off'


def student_allowed_ai_grading(contest):
    """Whether students may request on-demand AI for this contest."""
    if contest is None:
        return True
    if hasattr(contest, 'allow_student_ai_grading'):
        return bool(contest.allow_student_ai_grading)
    # Pre-migration / legacy rows: realtime mode meant students could request.
    return contest_ai_mode(contest) == 'realtime'


def resolve_contest_for_ai(submission, *, user=None, contest=None):
    """Which assignment's Allow Student Use AI Feedback gate applies.

    Prefer ``submission.contest_object`` (the submit's real assignment). If the
    row has no contest, fall back to an explicit workspace contest or the
    viewer's active participation — but only when that contest actually
    contains this problem, so practice opens are not mis-gated.
    """
    linked = getattr(submission, 'contest_object', None)
    if linked is not None:
        return linked

    candidates = []
    if contest is not None:
        candidates.append(contest)
    if user is not None and getattr(user, 'is_authenticated', False):
        try:
            current = user.profile.current_contest
            if current is not None:
                candidates.append(current.contest)
        except Exception:
            pass

    problem_id = getattr(submission, 'problem_id', None) or getattr(
        getattr(submission, 'problem', None), 'id', None,
    )
    if problem_id is None:
        return None
    for candidate in candidates:
        try:
            if candidate.problems.filter(id=problem_id).exists():
                return candidate
        except Exception:
            continue
    return None


def should_run_ai_now(submission, *, batch_pass=False, manual_trigger=False):
    """Whether this submission should hit the LLM *right now*.

    Submit never auto-invokes the model — only ``manual_trigger`` (student/teacher
    button) or ``batch_pass`` (teacher batch job) may run AI grading.
    """
    if not ai_feedback_callable(submission):
        return False
    if batch_pass or getattr(submission, '_ai_batch_pass', False):
        return True
    if manual_trigger:
        return True
    return False


def contest_awaits_student_ai(submission):
    """Submit stores the answer; the LLM runs only on an explicit request.

    Code problems are excluded: they always flow through the judge bridge and
    receive a real verdict. For code, ``IR`` means Invalid Return — a genuine
    judge result — never the synthetic "awaiting AI" placeholder that the
    internally-graded AI types (short answer / AI fill-blank) produce.
    """
    if not problem_supports_ai_feedback(submission.problem):
        return False
    from judge.models import ProblemSubmissionType
    if submission.problem.submission_type == ProblemSubmissionType.CODE:
        return False
    # Practice and every contest: never auto-call the LLM on submit.
    return ai_feedback_callable(submission)


def contest_defers_ai(submission):
    """Legacy batch-mode placeholder; new contests never defer to auto AI."""
    contest = getattr(submission, 'contest_object', None)
    return contest_ai_mode(contest) == 'batch' and problem_has_ai_grading(submission.problem)


def is_latest_submission(submission):
    """Latest by date (then id) for the same user, problem, and contest (or none)."""
    from judge.models import Submission

    qs = Submission.objects.filter(user_id=submission.user_id, problem_id=submission.problem_id)
    if submission.contest_object_id:
        qs = qs.filter(contest_object_id=submission.contest_object_id)
    else:
        qs = qs.filter(contest_object__isnull=True)
    latest_id = qs.order_by('-date', '-id').values_list('id', flat=True).first()
    return latest_id == submission.id


def supersede_older_in_flight(new_submission):
    """Abort older queued/grading attempts so only the newest submit is judged.

    Used when students may request AI (or practice): if the previous attempt is
    still in the queue, drop it and grade the new one.
    """
    from judge.models import ProblemSubmissionType, Submission

    contest = getattr(new_submission, 'contest_object', None)
    if not ai_feedback_callable(new_submission):
        return 0
    if contest is not None and not student_allowed_ai_grading(contest):
        return 0

    qs = Submission.objects.filter(
        user_id=new_submission.user_id,
        problem_id=new_submission.problem_id,
        status__in=('QU', 'P', 'G'),
    ).exclude(id=new_submission.id)
    if new_submission.contest_object_id:
        qs = qs.filter(contest_object_id=new_submission.contest_object_id)
    else:
        qs = qs.filter(contest_object__isnull=True)

    aborted = 0
    for old in qs:
        try:
            # Internal (short-answer / fill-blank) AI never sits on the judge bridge.
            if old.problem.submission_type != ProblemSubmissionType.CODE:
                Submission.objects.filter(id=old.id, status__in=('QU', 'P', 'G')).update(
                    status='AB', result='AB',
                )
            else:
                old.abort()
        except Exception:
            logger.exception('Could not abort superseded submission %s', old.id)
            Submission.objects.filter(id=old.id, status__in=('QU', 'P', 'G')).update(
                status='AB', result='AB',
            )
        aborted += 1
    return aborted


def mark_awaiting_batch_ai(submission):
    """Store a finished-looking row that the batch job will overwrite."""
    submission.status = 'D'
    submission.result = 'IE'
    submission.error = AI_BATCH_PENDING
    submission.case_points = 0
    submission.case_total = float(submission.problem.points or 0)
    submission.save(update_fields=['status', 'result', 'error', 'case_points', 'case_total'])


def persist_code_ai_rows(submission, rows):
    """Write supplementary AI rubric rows (batch=-1) and recompute the combined score.

    The final submission score becomes ``test-case*(1-w) + clamped-AI*w`` (see
    ``judge.question_types.code.combine_code_score``), stored on
    ``submission.points`` with the AI component on ``submission.ai_points``.
    The contest standing is refreshed so rankings use the combined score too,
    and a ``grading-end`` event notifies the status page of the update.
    Returns the number of rows written.
    """
    from judge.models import SubmissionTestCase
    from judge.question_types.code import AI_GRADE_BATCH, combine_code_score
    from judge.submission_grading import finalize_ai_combined

    if not rows:
        return 0
    SubmissionTestCase.objects.filter(submission=submission, batch=AI_GRADE_BATCH).delete()
    SubmissionTestCase.objects.bulk_create([
        SubmissionTestCase(
            submission=submission, case=r['case'], status=r['status'],
            time=0, memory=0, points=r['points'], total=r['total'],
            batch=r.get('batch', AI_GRADE_BATCH), feedback=r.get('feedback', ''),
            extended_feedback=r.get('extended_feedback', ''),
        )
        for r in rows
    ])
    ai_raw = sum(r.get('points') or 0 for r in rows)
    ai_raw_total = sum(r.get('total') or 0 for r in rows)
    ai_score, final = combine_code_score(
        submission.case_points, submission.case_total,
        submission.problem.points or 0, ai_raw, ai_raw_total,
        partial=submission.problem.partial,
    )
    finalize_ai_combined(submission, ai_score=ai_score, final_points=final)
    _notify_status(submission)
    return len(rows)


def run_ai_for_submission(submission, *, batch_pass=False, manual_trigger=False, rejudge_user=None):
    """Grade one submission with AI (code rubric or internal LLM types).

    Returns a short status string. Skips (and does not error) when this is no
    longer the latest submit for that user/problem.
    """
    from judge.models import ProblemSubmissionType
    from judge.question_types.code import supplementary_ai_grade

    if not is_latest_submission(submission):
        return 'skipped-not-latest'
    if not ai_feedback_callable(submission):
        return 'skipped-no-ai'
    if not should_run_ai_now(submission, batch_pass=batch_pass, manual_trigger=manual_trigger):
        return 'skipped-mode'

    st = submission.problem.submission_type
    if st == ProblemSubmissionType.CODE:
        # Only grade rubric rows once the judge has finished (status D ⇒ test
        # cases are written). Grading a queued/running submission computes the
        # case offset before the real rows exist, colliding with them later on
        # SubmissionTestCase's unique (submission, case) constraint.
        if submission.status != 'D':
            return 'skipped-not-graded'
        rows = supplementary_ai_grade(submission)
        if not rows:
            return 'no-rows'
        persist_code_ai_rows(submission, rows)
        return 'code-ai'
    submission._ai_batch_pass = True
    submission.judge(rejudge=True, force_judge=True, rejudge_user=rejudge_user)
    return 'internal-ai'


def _notify_status(submission):
    try:
        from judge import event_poster as event
        from judge.models import Submission as SubModel
        event.post('sub_%s' % SubModel.get_id_secret(submission.id), {'type': 'grading-end'})
    except Exception:
        logger.exception('Could not post grading-end for submission %s', submission.id)


def latest_submissions_for_contest(contest):
    """Map (participation_id, contest_problem_id) → latest Submission."""
    from judge.models import ContestSubmission

    latest = {}
    qs = (ContestSubmission.objects
          .filter(participation__contest=contest)
          .select_related('submission', 'submission__problem')
          .order_by('submission__date', 'submission_id'))
    for cs in qs:
        latest[(cs.participation_id, cs.problem_id)] = cs.submission
    return latest


def student_can_see_code_ai(user, submission):
    """Whether the supplementary code-AI table is visible to this viewer."""
    if user is None or not user.is_authenticated:
        return False
    if not submission.results_visible_to(user):
        return False
    is_owner = False
    try:
        is_owner = submission.user.user_id == user.id
    except Exception:
        is_owner = False
    is_staff_view = user.is_staff or user.has_perm('judge.see_private_submission')
    try:
        contest = submission.contest_object
        if contest is not None and contest.is_editable_by(user):
            is_staff_view = True
    except Exception:
        pass
    if is_staff_view:
        return True
    if not is_owner:
        return False
    contest = resolve_contest_for_ai(submission, user=user)
    if contest is None or student_allowed_ai_grading(contest):
        return True
    if bool(getattr(contest, 'ai_grading_batch_finished_at', None)):
        return True
    return False


def student_can_trigger_ai(user, submission, *, contest=None):
    """Show the on-demand AI Feedback button.

    Students in an assignment: assignment ``allow_student_ai_grading`` is the
    master switch on supported types (problem-level AI config optional).
    Practice (no contest): problem-level AI enable only.
    Teachers: may force when the problem has AI config, or when assignment
    Allow enables the lightweight path (bypass assignment-off only when the
    problem itself has AI Feedback configured).
    """
    from judge.ai_grading_quota import get_remaining, is_teacher_viewer

    if user is None or not user.is_authenticated:
        return False
    if not problem_supports_ai_feedback(submission.problem):
        return False
    try:
        is_owner = submission.user.user_id == user.id
    except Exception:
        is_owner = False
    teacher = is_teacher_viewer(user, submission)
    if not is_owner and not teacher:
        return False
    if submission.status != 'D':
        return False
    if not is_latest_submission(submission):
        return False

    contest = resolve_contest_for_ai(submission, user=user, contest=contest)
    if teacher:
        # Problem AI on → always (assignment-off bypass). Else lightweight only.
        if problem_has_ai_grading(submission.problem):
            return True
        return contest is not None and student_allowed_ai_grading(contest)
    if contest is not None:
        # Master switch: assignment Allow alone; no problem-level requirement.
        if not student_allowed_ai_grading(contest):
            return False
    elif not problem_has_ai_grading(submission.problem):
        return False
    if student_quota_disabled():
        return False
    try:
        profile_id = submission.user_id
    except Exception:
        return False
    if get_remaining(profile_id) <= 0:
        return False
    # Rate limit is enforced on POST (soft “try again later”); keep the
    # button visible so students get that message instead of a ban.
    return True


def student_quota_disabled():
    from judge.ai_grading_quota import student_quota_limit
    return student_quota_limit() <= 0


def ai_grade_context(user, submission, *, contest=None):
    """Template/API context for the on-demand AI grading button."""
    from judge.ai_grading_quota import (
        get_remaining, is_rate_limited, is_teacher_viewer, rate_limit_count,
        rate_limit_remaining_count, rate_limit_remaining_seconds, rate_window_seconds,
    )

    ctx = {
        'can_trigger': student_can_trigger_ai(user, submission, contest=contest),
        'is_teacher': is_teacher_viewer(user, submission),
        'remaining': 0,
        'rate_limited': False,
        'rate_limit_seconds': 0,
        'quota_disabled': student_quota_disabled() and not is_teacher_viewer(user, submission),
        # Sliding-window rule, surfaced to the student as "(N uses remaining
        # this window)" plus a tooltip with the exact limits.
        'rate_remaining': 0,
        'rate_limit': rate_limit_count(),
        'rate_window_minutes': max(1, rate_window_seconds() // 60),
    }
    if not ctx['is_teacher'] and user is not None and user.is_authenticated:
        try:
            profile_id = submission.user_id
        except Exception:
            profile_id = None
        if profile_id is not None:
            ctx['remaining'] = get_remaining(profile_id)
            ctx['rate_remaining'] = rate_limit_remaining_count(profile_id, submission.problem_id)
            ctx['rate_limited'] = is_rate_limited(profile_id, submission.problem_id)
            ctx['rate_limit_seconds'] = rate_limit_remaining_seconds(profile_id, submission.problem_id)
    return ctx


def schedule_batch_ai_grade(contest_id):
    """(Re)schedule post-end objective auto-grade, or clear when delay is 0."""
    from django.conf import settings

    from judge.models import Contest

    contest = Contest.objects.filter(pk=contest_id).first()
    if contest is None:
        return
    old_tid = contest.ai_grading_scheduled_task_id or ''
    if old_tid:
        try:
            from celery.result import AsyncResult
            AsyncResult(old_tid).revoke()
        except Exception:
            logger.exception('Could not revoke previous auto-grade task %s', old_tid)
        contest.ai_grading_scheduled_task_id = ''

    delay = int(contest.ai_grading_auto_delay_minutes)
    # −1 (or any negative) = Off: do not schedule auto-grade after end.
    if delay < 0:
        contest.save(update_fields=['ai_grading_scheduled_task_id'])
        return
    if contest.end_time is None:
        contest.save(update_fields=['ai_grading_scheduled_task_id'])
        return

    try:
        eta = contest.end_time + timedelta(minutes=delay)
        if eta <= timezone.now():
            contest.save(update_fields=['ai_grading_scheduled_task_id'])
            return
        if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
            # Tests / eager workers would fire immediately; teacher still has buttons.
            contest.save(update_fields=['ai_grading_scheduled_task_id'])
            return
        from judge.tasks.contest import auto_grade_contest
        result = auto_grade_contest.apply_async(args=[contest.key], eta=eta)
        contest.ai_grading_scheduled_task_id = result.id or ''
    except Exception:
        logger.exception('Could not schedule auto-grade for contest %s', contest.key)
        contest.ai_grading_scheduled_task_id = ''
    contest.save(update_fields=['ai_grading_scheduled_task_id'])


def ai_mode_help_text():
    return _(
        'Teachers can always run AI Feedback from the Grade page when a problem '
        'has AI Feedback configured. Enable “Allow Student Use AI Feedback” so '
        'students can request Feedback on supported problem types '
        '(programming, short answer, fill-in-the-blank) from the result page '
        '(latest submission only), even if the problem’s own AI Feedback is off. '
        'Auto grade after end re-grades objective questions only and does not '
        'call the LLM; set it to Off (−1) to cancel a scheduled auto-run after '
        'you grade manually.',
    )
