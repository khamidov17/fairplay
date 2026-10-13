"""Create, record and dispatch a submission without a request.

Submissions used to be born only inside ``ProblemSubmit.form_valid``, which reads the request for
the profile, the problem and the current participation. Auto-submit has none of those: it runs in a
Celery task on behalf of a student who is no longer connected. Rather than grow a second, parallel
implementation that would drift from the real one, both callers go through here — that is what
makes an auto-submitted answer indistinguishable from one the student submitted by hand.
"""
from django.db import transaction

from judge.models import ContestSubmission, ProblemSubmissionType, Submission, SubmissionSource

__all__ = ['create_submission']


def create_submission(profile, problem, *, language, answer, participation=None,
                      contest_problem=None, judge_id=None):
    """Record ``answer`` as a new submission by ``profile`` and send it for grading.

    ``answer`` is the source string for code problems, and the grader's cleaned-data dict for
    every other question type (what ``QuestionType.store_answer`` expects).

    Pass ``participation`` together with ``contest_problem`` to file the submission inside an
    assignment; omit both for a plain out-of-contest submission.

    Callers are responsible for the pre-submit guards (submission caps, bans, part locks): this
    function assumes the answer is allowed to be submitted.
    """
    from judge.question_types import get_grader
    from judge.utils.contest_ai import contest_defers_ai, mark_awaiting_batch_ai, supersede_older_in_flight

    is_code = problem.submission_type == ProblemSubmissionType.CODE

    with transaction.atomic():
        submission = Submission(user=profile, problem=problem, language=language)
        if contest_problem is not None:
            submission.contest_object = participation.contest
            if participation.live:
                submission.locked_after = submission.contest_object.locked_after
            submission.save()
            ContestSubmission.objects.create(
                submission=submission, problem=contest_problem, participation=participation,
            )
        else:
            submission.save()

        if is_code:
            # Prime the reverse relation so grading does not re-fetch what we just wrote.
            submission.source = SubmissionSource.objects.create(submission=submission, source=answer)
        else:
            get_grader(problem.submission_type).store_answer(submission, answer)

    supersede_older_in_flight(submission)
    if is_code:
        submission.judge(force_judge=True, judge_id=judge_id)
    elif contest_defers_ai(submission):
        mark_awaiting_batch_ai(submission)
    else:
        submission.judge(force_judge=True)

    return submission
