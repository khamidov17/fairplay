"""Query-level result hiding for exam-style assignments.

``Submission.contest_results_hidden`` / ``results_visible_to`` mask the verdict and score a
viewer *sees* on one row. Anything that filters, counts, aggregates or orders by the real
``result`` / ``points`` must apply the same rule, or the masked verdict leaks through the query
(``?status=AC`` returning a row, a result count, an AC rate, a "best solutions" row, a solved
marker). This module is the single place that turns the rule into ``Q`` objects:

* ``results_hidden_q()``: submissions whose results are masked right now (viewer independent);
* ``results_visible_q(user)``: submissions whose real result ``user`` may see (mirrors
  ``Submission.results_visible_to``);
* ``visible_results(queryset, user)``: ``queryset`` narrowed to those.

Every helper takes a ``prefix`` so it can be used from a related model (e.g.
``prefix='submission__'`` on ``ContestSubmission``).
"""
from django.db.models import Q

__all__ = ['hidden_result_formats', 'hidden_result_types', 'problem_has_hidden_results',
           'problem_in_active_hidden_exam', 'results_hidden_q', 'results_visible_q', 'sees_all_results',
           'solved_source_visibility_applies', 'visible_results']


def hidden_result_formats():
    """Contest format names that can hide results (``has_hidden_results``)."""
    from judge.contest_format import formats
    return sorted(name for name, cls in formats.items() if cls.has_hidden_results)


def hidden_result_types():
    """Question types whose results are hidden by default in such contests."""
    from judge.question_types import all_types
    return sorted(t.submission_type for t in all_types() if t.hide_results_in_contest)


def results_hidden_q(prefix=''):
    """``Q`` matching submissions whose results are currently masked from ordinary viewers.

    Same rule as ``Submission.contest_results_hidden``: an exam-style contest that is not
    finalized, and a contest problem whose ``hide_results`` override (or, when unset, the
    question-type default) hides results.
    """
    p = prefix
    problem_hidden = (
        Q(**{p + 'contest__problem__hide_results': True}) |
        (Q(**{p + 'contest__problem__hide_results__isnull': True}) &
         Q(**{p + 'contest__problem__problem__submission_type__in': hidden_result_types()}))
    )
    return (
        Q(**{p + 'contest_object__format_name__in': hidden_result_formats()}) &
        Q(**{p + 'contest_object__results_finalized': False}) &
        Q(**{p + 'contest__isnull': False}) &
        problem_hidden
    )


def sees_all_results(user):
    return user.is_authenticated and user.has_perm('judge.view_all_submission')


def _exempt_q(user, prefix):
    """Submissions ``user`` may see through the mask (editors, submission viewers, testers)."""
    from judge.models import Contest, Problem
    p = prefix
    profile = user.profile
    contest_ids = list(
        Contest.objects.filter(format_name__in=hidden_result_formats(), results_finalized=False)
        .filter(
            Q(authors=profile) | Q(curators=profile) | Q(view_contest_submissions=profile) |
            Q(tester_see_submissions=True, testers=profile),
        )
        .values_list('id', flat=True).distinct(),
    )
    exempt = Q(**{p + 'problem__in': Problem.get_editable_problems(user).values('id')})
    if contest_ids:
        exempt |= Q(**{p + 'contest_object_id__in': contest_ids})
    return exempt


def results_visible_q(user, prefix=''):
    """``Q`` matching submissions whose real result/points ``user`` may see (or filter on)."""
    if sees_all_results(user):
        return Q()
    visible = ~results_hidden_q(prefix)
    if user.is_authenticated:
        visible |= _exempt_q(user, prefix)
    return visible


def visible_results(queryset, user, prefix=''):
    """Narrow ``queryset`` to rows whose real result/points ``user`` may see."""
    if sees_all_results(user):
        return queryset
    return queryset.filter(results_visible_q(user, prefix))


def _hidden_exam_problems(problem):
    """``ContestProblem`` rows using ``problem`` with hidden results in a non-finalized exam."""
    from judge.models import ContestProblem
    hidden = Q(hide_results=True)
    if problem.submission_type in hidden_result_types():
        hidden |= Q(hide_results__isnull=True)
    return ContestProblem.objects.filter(
        hidden, problem=problem,
        contest__format_name__in=hidden_result_formats(), contest__results_finalized=False,
    )


def problem_has_hidden_results(problem):
    """Whether ``problem`` is used, with hidden results, in an exam that is not finalized yet."""
    return _hidden_exam_problems(problem).exists()


def problem_in_active_hidden_exam(problem):
    """Whether ``problem`` is used, with hidden results, in an exam that is running or upcoming."""
    from django.utils import timezone
    return _hidden_exam_problems(problem).filter(
        Q(contest__end_time__isnull=True) | Q(contest__end_time__gt=timezone.now()),
    ).exists()


def solved_source_visibility_applies(problem):
    """Whether "solved" source visibility (solvers may read others' submissions) applies.

    Only for code: for answers (choice, blanks, essays, uploads) reading a classmate's
    submission is reading their answer. Never while the problem is used in an exam whose
    results are hidden, since being let in (or not) would reveal one's own hidden verdict.
    """
    from judge.question_types import get_grader
    try:
        if not get_grader(problem.submission_type).routes_to_bridge:
            return False
    except NotImplementedError:
        return False
    return not problem_has_hidden_results(problem)
