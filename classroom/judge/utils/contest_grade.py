"""Shared helpers for the teacher Grade page.

"Final version" is the live submission the contest format would actually score:
latest for exam types with ``score_uses_latest`` (and ECOO), otherwise best.
Virtual participations are never final.
"""
from __future__ import annotations

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from judge.models import ContestParticipation, ContestSubmission, Problem, Submission, SubmissionManualGrade
from judge.models.problem import ProblemSubmissionType


def scoring_submissions_for_contest(contest, live_only=False, participation_ids=None):
    """Map (participation_id, contest_problem_id) → scoring Submission.

    When the format scores latest, the newest ``(date, id)`` wins; otherwise
    the highest ``ContestSubmission.points`` wins, with ``(date, id)`` as the
    tie-break. ``live_only=True`` drops virtual participations (grade-page
    final marker). Ranking editor links pass ``live_only=False`` so a virtual
    row still deep-links the attempt that cell represents.

    ``participation_ids`` scopes the scan to the rows actually being rendered;
    the ranking views pass the page's participations so a per-user page does
    not walk every submission in the contest. ``None`` means the whole contest
    (the Grade desk, which needs every row anyway); an empty sequence short-
    circuits to an empty map.
    """
    if participation_ids is not None:
        participation_ids = list(participation_ids)
        if not participation_ids:
            return {}

    fmt = contest.format
    latest = {}
    best = {}
    qs = (ContestSubmission.objects
          .filter(participation__contest=contest)
          .select_related('submission', 'problem__problem')
          .order_by('submission__date', 'submission_id'))
    if live_only:
        qs = qs.filter(participation__virtual=ContestParticipation.LIVE)
    if participation_ids is not None:
        qs = qs.filter(participation_id__in=participation_ids)
    for cs in qs:
        key = (cs.participation_id, cs.problem_id)
        sub = cs.submission
        stamp = (sub.date, sub.id)
        if fmt.counts_as_latest(sub):
            latest[key] = (stamp, cs)
        pts = cs.points if cs.points is not None else (sub.points or 0)
        cand = (pts, stamp)
        prev = best.get(key)
        if prev is None or cand > prev[0]:
            best[key] = (cand, cs)

    result = {}
    for key, (stamp, cs) in latest.items():
        use_latest = fmt.scoring_uses_latest(cs.problem)
        chosen = cs if use_latest else best[key][1]
        result[key] = chosen.submission
    return result


def final_submissions_for_contest(contest):
    """Map (live participation_id, contest_problem_id) → scoring Submission."""
    return scoring_submissions_for_contest(contest, live_only=True)


def final_submission_ids_for_contest(contest):
    return {sub.id for sub in final_submissions_for_contest(contest).values()}


def practice_final_submission_ids(submissions):
    """Ids of the newest practice submission per ``(user_id, problem_id)``.

    ``submissions`` must already be newest-first (``-date``).
    """
    seen = set()
    ids = set()
    for sub in submissions:
        key = (sub.user_id, sub.problem_id)
        if key in seen:
            continue
        seen.add(key)
        ids.add(sub.id)
    return ids


def grade_desk_url(submission, user):
    """Grade-page URL that selects ``submission``, or ``None`` if the user cannot grade it."""
    if user is None or not user.is_authenticated:
        return None
    contest = submission.contest_object
    if contest is not None:
        if contest.is_editable_by(user):
            return '%s?submission=%d' % (reverse('contest_grade', args=[contest.key]), submission.id)
        return None
    if submission.problem.is_editable_by(user):
        return '%s?submission=%d' % (reverse('practice_grade'), submission.id)
    return None


def can_open_practice_grade(user):
    """True when the user can grade at least one practice problem."""
    if user is None or not user.is_authenticated:
        return False
    return Problem.get_editable_problems(user).exists()


#: How many practice rows the Grade desk inlines into the page. The desk ships
#: full source + every test case per row inside one <script> block, so an
#: unbounded list is the whole submission table for a superuser. Override with
#: ``DMOJ_PRACTICE_GRADE_PAGE_SIZE``.
PRACTICE_GRADE_PAGE_SIZE = 300


# Everything ``build_grade_payload`` reads per row, so the desk costs a fixed number of
# queries instead of ~3 per submission (source, contest problem, MCQ options / blanks).
GRADE_ROW_SELECT = ('user__user', 'problem', 'language', 'reviewed_by__user', 'contest__participation',
                    'contest__problem', 'source')
GRADE_ROW_PREFETCH = ('test_cases', 'problem__fill_blank_config', 'problem__short_answer_config',
                      'problem__mcq_options', 'problem__blanks', 'manual_grade')


def practice_grade_queryset(user):
    """Practice submissions the user is allowed to grade (no contest/assignment).

    This is the **permission scope**, not the page: Save / Status resolve any
    submission id through it, so it must stay unbounded. The rendered page uses
    ``practice_grade_page`` instead.
    """
    problems = Problem.get_editable_problems(user)
    return (Submission.objects
            .filter(contest_object__isnull=True, problem__in=problems)
            .select_related(*GRADE_ROW_SELECT)
            .prefetch_related(*GRADE_ROW_PREFETCH)
            .order_by('-date'))


def practice_grade_page(user, pinned_id=None, limit=None):
    """The bounded slice of practice rows the Grade desk renders.

    Returns ``(submissions, final_ids, truncated)``. Newest ``limit`` rows, plus
    the ``?submission=`` deep-link target when it falls outside that window so a
    link from the submission list never lands on an empty desk.

    ``final_ids`` is computed from the window (which is newest-first, so the
    first row seen per ``(user, problem)`` is that pair's final version). A
    pinned row from outside the window is checked against the full scope, since
    the window cannot tell whether something newer exists for its pair.
    """
    if limit is None:
        limit = getattr(settings, 'DMOJ_PRACTICE_GRADE_PAGE_SIZE', PRACTICE_GRADE_PAGE_SIZE)
    scope = practice_grade_queryset(user)
    window = list(scope[:limit + 1])
    truncated = len(window) > limit
    if truncated:
        window = window[:limit]

    final_ids = practice_final_submission_ids(window)

    submissions = window
    if pinned_id is not None and not any(sub.id == pinned_id for sub in window):
        pinned = scope.filter(id=pinned_id).first()
        if pinned is not None:
            submissions = window + [pinned]
            newer_exists = (Submission.objects
                            .filter(contest_object__isnull=True, user_id=pinned.user_id,
                                    problem_id=pinned.problem_id, date__gt=pinned.date)
                            .exists())
            if not newer_exists:
                final_ids.add(pinned.id)
    return submissions, final_ids, truncated


def contest_grade_queryset(contest):
    return (Submission.objects.filter(contest_object=contest)
            .select_related(*GRADE_ROW_SELECT)
            .prefetch_related(*GRADE_ROW_PREFETCH)
            .order_by('-date'))


def build_grade_payload(submissions, final_ids):
    """JSON-serializable rows for the grade workspace drawer."""
    from judge.question_types import get_grader
    from judge.views.problem import get_workspace_answer_display

    payload = []
    for sub in submissions:
        is_latest = sub.id in final_ids
        try:
            source = sub.source.source
        except Submission.source.RelatedObjectDoesNotExist:
            source = ''
        upload_info = None
        if sub.problem.submission_type == ProblemSubmissionType.FILE:
            try:
                upload = sub.upload
                upload_info = {
                    'original_name': upload.original_name,
                    'size': upload.size,
                    'download_url': reverse('submission_file_download', args=(sub.id,)),
                }
            except Exception:
                upload_info = None
        contest_submission = sub.contest_or_none
        if contest_submission is not None:
            display_points = contest_submission.points
        else:
            display_points = sub.points
        display_total = grade_points_total(sub)
        manual_grade = sub.manual_grade_override
        if manual_grade is not None and manual_grade.points is not None:
            manual_points = manual_grade.points
            machine_points = manual_grade.machine_points
            released = manual_grade.released
            # The grade desk always shows the teacher's override, independent of
            # the (release-gated) effective score stored on ContestSubmission.
            display_points = manual_points
        else:
            manual_points = None
            machine_points = display_points
            released = False
        answer_display = get_workspace_answer_display(sub) if source else []
        # "Pending" means awaiting teacher review: a manually-graded type
        # (file upload) with no score yet. Everything else resolves on its own —
        # a code submission still in the judge queue is NOT pending.
        if manual_grade is not None or sub.points is not None:
            is_pending = False
        else:
            is_pending = get_grader(sub.problem.submission_type).is_manual
        payload.append({
            'id': sub.id,
            'user': sub.user.user.username,
            'problem': sub.problem.code,
            'problem_type': sub.problem.submission_type,
            'lang': sub.language.short_name or sub.language.key,
            'date': timezone.localtime(sub.date).strftime('%Y-%m-%d %H:%M'),
            'result': sub.short_status,
            'points': display_points,
            'total': display_total,
            'time': sub.time,
            'memory': sub.memory,
            'source': source,
            'answer_display': answer_display,
            'is_pending': is_pending,
            'upload': upload_info,
            'test_cases': [
                {
                    'case': tc.case,
                    'status': tc.status,
                    'time': tc.time,
                    'memory': tc.memory,
                    'points': tc.points,
                    'total': tc.total,
                    'batch': tc.batch,
                    'feedback': tc.feedback or '',
                    'extended_feedback': tc.extended_feedback or '',
                } for tc in sub.test_cases.all()
            ],
            'case_points': sub.case_points,
            'case_total': sub.case_total,
            'review_note': sub.review_note or '',
            'reviewed_by': sub.reviewed_by.user.username if sub.reviewed_by else None,
            'reviewed_at': timezone.localtime(sub.reviewed_at).strftime('%Y-%m-%d %H:%M') if sub.reviewed_at else None,
            'manual_points': manual_points,
            'machine_points': machine_points,
            'released': released,
            'virtual': bool(contest_submission and contest_submission.participation_id
                            and contest_submission.participation.virtual),
            'is_latest': is_latest,
        })
    return payload


def machine_assignment_points(submission, contest_problem):
    """Judge/machine score scaled onto ``ContestProblem.points``.

    Never reads ``ContestSubmission.points`` (Save overwrites that with the
    manual score for ranking). Matches ``Submission.update_contest``'s machine
    formula: case ratio onto the assignment total, with the same ``partial``
    all-or-nothing rule; if there are no cases, scale ``submission.points``
    from ``problem.points``. When an AI rubric contributed (``ai_points`` set),
    the combined ``submission.points`` is scaled instead, matching the ranking.
    """
    total = contest_problem.points
    problem_total = submission.problem.points if submission.problem_id else None
    if submission.ai_points is not None and problem_total:
        # Code with a supplementary AI rubric: ``submission.points`` holds the
        # combined (test cases + AI) score. Scale it exactly like
        # ``submission_grading._refresh_contest_score`` does for the ranking,
        # so the student sees the same number as the ranking and exports.
        pts = round((submission.points or 0) / problem_total * total, 3)
        if not contest_problem.partial and pts != total:
            pts = 0
        return pts
    if submission.case_total and submission.case_total > 0:
        pts = round(submission.case_points / submission.case_total * total, 3)
        if not contest_problem.partial and pts != total:
            pts = 0
        return pts
    if problem_total:
        return round((submission.points or 0) / problem_total * total, 3)
    return submission.points


def student_display_points(submission):
    """Student-visible numerator for a submission (workspace / list / status).

    Gated on whether the submission has a ``ContestSubmission`` and whether a
    manual override has been **released** — not on ``request.in_contest``.

    * Assignment/exam: denominator is ``ContestProblem.points``. Numerator is
      the released manual score, else the ``machine_points`` snapshot, else a
      recomputed machine assignment score. ``ContestSubmission.points`` is not
      used (Save already wrote the manual score there for ranking).
    * Practice: denominator is ``problem.points`` (never ``case_total``).
      Numerator is the released manual score, else ``machine_points``, else
      ``submission.points``.
    """
    manual = submission.manual_grade_override
    if manual is not None and manual.released and manual.points is not None:
        return manual.points
    if manual is not None and manual.machine_points is not None:
        return manual.machine_points
    contest_submission = submission.contest_or_none
    if contest_submission is not None:
        return machine_assignment_points(submission, contest_submission.problem)
    return submission.points


def grade_points_total(submission):
    """Denominator for Grade-desk display, status poll, and Save clamp.

    Assignment: ``ContestProblem.points``. Practice / detached: ``problem.points``.
    Never ``case_total`` — MCQ judging is often 0/1 on a 100-point problem.
    """
    contest_submission = submission.contest_or_none
    if contest_submission is not None:
        return contest_submission.problem.points
    return submission.problem.points


def student_display_points_total(submission):
    """Student-visible denominator: same scale as the Grade desk."""
    return grade_points_total(submission)


def student_display_released(submission):
    """Whether a concrete manual score was released for this submission."""
    manual = submission.manual_grade_override
    return manual is not None and manual.released and manual.points is not None


def attach_student_display_score(submission):
    """Set ``display_points``, ``display_points_total``, and ``display_released``."""
    submission.display_points = student_display_points(submission)
    submission.display_points_total = student_display_points_total(submission)
    submission.display_released = student_display_released(submission)
    return submission


def attach_student_display_scores(submissions):
    for submission in submissions:
        attach_student_display_score(submission)
    return submissions


def apply_manual_grade(profile, submission, score, note=None):
    """Persist a manual override for ``submission``. Returns ``(grade, score, total)``."""
    contest_submission = submission.contest_or_none
    total = grade_points_total(submission)
    if contest_submission is not None:
        machine_points = contest_submission.points
    else:
        machine_points = submission.points
    score = min(max(score, 0), total)

    existing = SubmissionManualGrade.objects.filter(submission=submission).first()
    if existing is not None:
        if existing.machine_points is None:
            existing.machine_points = machine_points
        existing.points = score
        existing.save(update_fields=['points', 'machine_points'])
        grade = existing
    else:
        grade = SubmissionManualGrade.objects.create(
            submission=submission, points=score, machine_points=machine_points,
        )

    if note is not None:
        submission.review_note = note
    submission.reviewed_by = profile
    submission.reviewed_at = timezone.now()
    submission.save(update_fields=['review_note', 'reviewed_by', 'reviewed_at'])

    if contest_submission is not None:
        # The ranking reflects the manual override only once it is released;
        # until then the machine score stands, so a teacher who saves (but has
        # not yet released) a grade never leaks it through the scoreboard.
        # Re-saving an already-released override still propagates its new score.
        effective = grade.points if (grade.released and grade.points is not None) else contest_submission.points
        if contest_submission.points != effective:
            contest_submission.points = effective
            contest_submission.save(update_fields=['points'])
            contest_submission.participation.recompute_results()

    return grade, score, total


def sync_released_manual_grades(contest):
    """Propagate released manual scores into ``ContestSubmission.points`` and recompute.

    ``ContestGradeRelease`` flips ``released=True`` on every manual grade in the
    contest; this folds those overrides into the effective contest score so the
    ranking (``ContestParticipation.score`` + per-problem ``format_data``) and the
    CSV exports reflect the release. Practice submissions have no
    ``ContestSubmission`` row and are ignored here.
    """
    grades = (SubmissionManualGrade.objects
              .filter(submission__contest_object=contest, released=True, points__isnull=False)
              .select_related('submission__contest__participation'))
    participation_ids = set()
    for grade in grades:
        contest_submission = grade.submission.contest_or_none
        if contest_submission is None:
            continue
        if contest_submission.points != grade.points:
            contest_submission.points = grade.points
            contest_submission.save(update_fields=['points'])
        participation_ids.add(contest_submission.participation_id)
    # Fetch every affected participation in one query.  Release All can touch
    # thousands of submissions, so looking up one participation per id here
    # turns an otherwise bulk operation into an avoidable N+1 query pattern.
    participations = ContestParticipation.objects.in_bulk(participation_ids)
    for participation in participations.values():
        participation.recompute_results()
