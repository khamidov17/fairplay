"""Shared submission-finalization logic.

``finalize_submission`` is the single source of truth for "a submission just reached a terminal
score": it computes the scaled points, persists them, and runs every downstream effect (user
points, problem stats, contest standings, cache invalidation, websocket events). It is used both
by the judge bridge (``JudgeHandler.on_grading_end``, for code) and by ``grade_internally`` (for
internally-graded question types such as multiple-choice), so the two paths stay in lockstep.
"""
from judge import event_poster as event
from judge.caching import finished_submission

__all__ = ['finalize_submission', 'grade_internally']


def finalize_submission(submission, *, case_points, case_total, result,
                        time=0.0, memory=0.0, status='D', error=None, save=True):
    """Persist a terminal grading outcome and propagate it everywhere code grading would.

    ``case_points``/``case_total`` are the raw per-case aggregate; the problem-scaled
    ``submission.points`` is derived here exactly as ``on_grading_end`` does (round to 3,
    all-or-nothing when the problem is not partial). Returns the scaled points.
    """
    problem = submission.problem
    sub_points = round(case_points / case_total * problem.points if case_total > 0 else 0, 3)
    if not problem.partial and sub_points != problem.points:
        sub_points = 0

    submission.case_points = case_points
    submission.case_total = case_total
    submission.status = status
    submission.result = result
    submission.time = time
    submission.memory = memory
    submission.points = sub_points
    if error is not None:
        submission.error = error
    if save:
        submission.save()

    if problem.is_public and not problem.is_organization_private:
        submission.user._updating_stats_only = True
        submission.user.calculate_points()

    problem._updating_stats_only = True
    problem.update_stats()
    submission.update_contest()

    finished_submission(submission)

    if submission.contest_results_hidden:
        # Exam-style contest, results masked: announce only that grading ended — never the verdict/score.
        event.post('sub_%s' % submission.id_secret, {'type': 'grading-end'})
    else:
        event.post('sub_%s' % submission.id_secret, {
            'type': 'grading-end',
            'time': time,
            'memory': memory,
            'points': float(case_points),
            'total': float(problem.points),
            'result': submission.result,
        })
    if hasattr(submission, 'contest'):
        participation = submission.contest.participation
        event.post('contest_%d' % participation.contest_id, {'type': 'update'})

    return sub_points


def grade_internally(submission):
    """Grade a non-code submission without the judge bridge, then finalize it.

    Looks up the registered grader for the problem's submission type, records any per-item
    feedback as ``SubmissionTestCase`` rows (so the status page renders normally), and calls
    ``finalize_submission`` with the grader's outcome.
    """
    from judge.models import SubmissionTestCase
    from judge.question_types import get_grader

    grader = get_grader(submission.problem.submission_type)
    outcome = grader.grade(submission)

    if _keeps_stored_ai_score(submission, outcome):
        # A plain rejudge (Finalize, bulk / admin rejudge, auto-grade) of an
        # AI-graded answer does not call the LLM, so the grader only returns an
        # "awaiting AI" placeholder. Keep the stored AI score instead of
        # overwriting it with 0; re-scoring with AI needs an explicit AI run
        # (``run_ai_for_submission``: Grade-desk batch, rejudge widget, button).
        # Teacher manual grades live in SubmissionManualGrade and are untouched.
        if submission.rejudged_date is not None:
            type(submission).objects.filter(pk=submission.pk).update(rejudged_date=submission.rejudged_date)
        return submission.points

    SubmissionTestCase.objects.filter(submission=submission).delete()
    if outcome.cases:
        SubmissionTestCase.objects.bulk_create([
            SubmissionTestCase(
                submission=submission, case=case['case'], status=case['status'],
                time=0, memory=0, points=case.get('points', 0), total=case.get('total', 0),
                feedback=case.get('feedback', '') or '',
                extended_feedback=case.get('extended_feedback', '') or '',
            )
            for case in outcome.cases
        ])
    submission.current_testcase = len(outcome.cases)

    return finalize_submission(
        submission, case_points=outcome.case_points, case_total=outcome.case_total,
        result=outcome.result, status='D', error='',
    )


# Placeholder results the AI-graded types return when the LLM was not invoked.
_AI_PLACEHOLDER_RESULTS = ('IR', 'IE')
# Terminal results an AI grading pass stores.
_AI_SCORED_RESULTS = ('AC', 'WA')


def _keeps_stored_ai_score(submission, outcome):
    """Whether ``outcome`` is a no-LLM placeholder that must not replace a stored AI score."""
    if outcome.result not in _AI_PLACEHOLDER_RESULTS or outcome.case_points:
        return False
    if submission.status != 'D' or submission.result not in _AI_SCORED_RESULTS:
        return False
    from judge.utils.contest_ai import problem_supports_ai_feedback, should_run_ai_now
    if not problem_supports_ai_feedback(submission.problem):
        return False
    # An explicit AI run (batch pass / manual trigger) always re-scores.
    return not should_run_ai_now(submission)


def finalize_ai_combined(submission, *, ai_score, final_points):
    """Apply the supplementary AI score to an already-finalized code submission.

    Called once AI grading completes (after test-case finalization): persists
    ``ai_points`` / ``points`` and re-runs the downstream effects (user points,
    problem stats, contest standing, cache invalidation) with the combined
    score. The websocket event is posted by the caller
    (``judge.utils.contest_ai.persist_code_ai_rows``), which owns both paths.
    """
    from judge.models import Submission as SubmissionModel

    SubmissionModel.objects.filter(pk=submission.pk).update(ai_points=ai_score, points=final_points)
    submission.ai_points = ai_score
    submission.points = final_points

    problem = submission.problem
    if problem.is_public and not problem.is_organization_private:
        submission.user._updating_stats_only = True
        submission.user.calculate_points()

    problem._updating_stats_only = True
    problem.update_stats()
    submission.update_contest()
    _refresh_contest_score(submission)

    finished_submission(submission)


def _refresh_contest_score(submission):
    """Recompute the contest score from the *combined* score ratio.

    Mirrors ``Submission.update_contest`` but maps the combined
    (test-case + AI) submission score onto the contest problem's scale, so
    rankings and exports use the AI-inclusive score. A *released* manual
    override still wins over the machine score; an unreleased one stays
    hidden from the ranking.
    """
    try:
        contest = submission.contest
    except AttributeError:
        return

    contest_problem = contest.problem
    problem = submission.problem
    contest.points = round(
        submission.points / problem.points * contest_problem.points, 3,
    ) if problem.points > 0 else 0.0
    if not contest_problem.partial and contest.points != contest_problem.points:
        contest.points = 0
    manual_grade = submission.manual_grade_override
    if manual_grade is not None and manual_grade.points is not None and manual_grade.released:
        contest.points = manual_grade.points
    contest.save()
    contest.participation.recompute_results()
