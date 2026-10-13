import json
import logging
import sys
from collections import defaultdict
from math import e

from django.core.cache import cache
from django.db.models import Case, Count, ExpressionWrapper, F, Q, When
from django.db.models.fields import FloatField
from django.utils import timezone
from django.utils.translation import gettext_noop

from judge.models import ContestParticipation, ContestSubmission, Problem, Submission

__all__ = [
    'assignment_problem_stats', 'contest_completed_ids', 'get_result_data',
    'log_workspace_progress', 'participation_submit_progress',
    'user_completed_ids', 'user_editable_ids', 'user_tester_ids',
]

progress_log = logging.getLogger('judge.workspace.progress')


def _ensure_progress_log_handler():
    # Attach our own console handler so INFO lines show in docker logs even when
    # the root logger stays at WARNING (Django's default).
    if progress_log.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    progress_log.addHandler(handler)
    progress_log.setLevel(logging.INFO)
    progress_log.propagate = False


def _clip_progress_value(value, limit=400):
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        return [_clip_progress_value(item, 120) for item in value[:40]]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        return text[:limit] + '…'
    return text


def log_workspace_progress(step, participation=None, progress=None, **fields):
    """One grep-able line for the assignment progress / leave-dialog data path."""
    _ensure_progress_log_handler()
    payload = {'step': step}
    if participation is not None:
        profile = getattr(participation, 'user', None)
        payload['user'] = getattr(profile, 'username', None)
        payload['participation'] = getattr(participation, 'id', None)
        contest = getattr(participation, 'contest', None)
        payload['contest'] = getattr(contest, 'key', None)
    if progress:
        payload['submitted'] = progress.get('submitted')
        payload['unsubmitted'] = progress.get('unsubmitted')
        payload['total'] = progress.get('total')
        payload['submitted_names'] = _clip_progress_value(progress.get('submitted_names'))
        payload['unsubmitted_names'] = _clip_progress_value(progress.get('unsubmitted_names'))
    for key, value in fields.items():
        payload[key] = _clip_progress_value(value)
    progress_log.info('workspace-progress %s', json.dumps(payload, ensure_ascii=False, default=str))


def assignment_problem_stats(contest):
    """Per-problem Users / AC rate from this assignment's live submissions.

    Users = distinct students who submitted the problem in this assignment.
    AC rate = AC submissions / all submissions in this assignment.
    Practice / global submissions and virtual/spectate joins are ignored.

    Counted from each submission's own ContestSubmission → participation,
    not from all participations on the contest.
    """
    rows = (
        ContestSubmission.objects.filter(
            participation__contest=contest,
            participation__virtual=ContestParticipation.LIVE,
        )
        .values('problem__problem_id')
        .annotate(
            user_count=Count('participation__user', distinct=True),
            submission_total=Count('id'),
            submission_ac=Count('id', filter=Q(submission__result='AC')),
        )
    )
    stats = {}
    for row in rows:
        total = row['submission_total']
        stats[row['problem__problem_id']] = {
            'user_count': row['user_count'],
            'ac_rate': (100.0 * row['submission_ac'] / total) if total else 0.0,
        }
    return stats


def user_tester_ids(profile):
    return set(Problem.testers.through.objects.filter(profile=profile).values_list('problem_id', flat=True))


def user_editable_ids(profile):
    return set(Problem.get_editable_problems(profile.user).values_list('id', flat=True))


def _leave_problem_label(contest_problem, index):
    display = getattr(contest_problem, 'display_name', '') or ''
    name = display or contest_problem.problem.name
    return '%s. %s' % (index, name)


def participation_submit_progress(participation, log_step=None, **log_fields):
    """How many assignment problems this participation has already submitted.

    Counts stay on the leave form for tracing even while the dialog hides them.
    Pass log_step only on the submit/leave hot spots — page renders stay quiet.
    """
    empty = {
        'total': 0, 'submitted': 0, 'unsubmitted': 0,
        'submitted_names': [], 'unsubmitted_names': [],
    }
    if participation is None:
        return empty
    contest_problems = list(
        participation.contest.contest_problems.select_related('problem').order_by('order'),
    )
    submitted_ids = set(participation.submissions.values_list('problem_id', flat=True))
    submitted_names = []
    unsubmitted_names = []
    for index, contest_problem in enumerate(contest_problems, 1):
        label = _leave_problem_label(contest_problem, index)
        if contest_problem.id in submitted_ids:
            submitted_names.append(label)
        else:
            unsubmitted_names.append(label)
    total = len(contest_problems)
    submitted = len(submitted_names)
    result = {
        'total': total,
        'submitted': submitted,
        'unsubmitted': total - submitted,
        'submitted_names': submitted_names,
        'unsubmitted_names': unsubmitted_names,
    }
    if log_step:
        log_workspace_progress(log_step, participation, progress=result, **log_fields)
    return result


def contest_completed_ids(participation):
    key = 'contest_complete:%d' % participation.id
    result = cache.get(key)
    if result is None:
        result = set(participation.submissions.filter(submission__result='AC', points__gte=F('problem__points'))
                     .values_list('problem__problem_id', flat=True).distinct())
        cache.set(key, result, 86400)
    return result


def user_completed_ids(profile):
    key = 'user_complete:%d' % profile.id
    result = cache.get(key)
    if result is None:
        result = set(Submission.objects.filter(user=profile, result='AC', case_points__gte=F('case_total'))
                     .values_list('problem_id', flat=True).distinct())
        cache.set(key, result, 86400)
    return result


def contest_attempted_ids(participation):
    key = 'contest_attempted:%s' % participation.id
    result = cache.get(key)
    if result is None:
        result = set(participation.submissions.values_list('problem__problem_id', flat=True).distinct())
        cache.set(key, result, 86400)
    return result


def user_attempted_ids(profile):
    key = 'user_attempted:%s' % profile.id
    result = cache.get(key)
    if result is None:
        result = set(profile.submission_set.values_list('problem_id', flat=True).distinct())
        cache.set(key, result, 86400)
    return result


def _get_result_data(results):
    return {
        'categories': [
            # Using gettext_noop here since this will be tacked into the cache, so it must be language neutral.
            # The caller, SubmissionList.get_result_data will run gettext on the name.
            {'code': 'AC', 'name': gettext_noop('Accepted'), 'count': results['AC']},
            {'code': 'WA', 'name': gettext_noop('Wrong'), 'count': results['WA']},
            {'code': 'CE', 'name': gettext_noop('Compile Error'), 'count': results['CE']},
            {'code': 'TLE', 'name': gettext_noop('Timeout'), 'count': results['TLE']},
            {'code': 'ERR', 'name': gettext_noop('Error'),
             'count': results['MLE'] + results['OLE'] + results['IR'] + results['RTE'] + results['AB'] + results['IE']},
        ],
        'total': sum(results.values()),
    }


def get_result_data(*args, **kwargs):
    if args:
        submissions = args[0]
        if kwargs:
            raise ValueError("Can't pass both queryset and keyword filters")
    else:
        submissions = Submission.objects.filter(**kwargs) if kwargs is not None else Submission.objects
    raw = submissions.values('result').annotate(count=Count('result')).values_list('result', 'count')
    return _get_result_data(defaultdict(int, raw))


def hot_problems(duration, limit):
    cache_key = 'hot_problems:%d:%d' % (duration.total_seconds(), limit)
    qs = cache.get(cache_key)
    if qs is None:
        qs = Problem.get_public_problems() \
                    .filter(submission__date__gt=timezone.now() - duration, points__gt=3, points__lt=25)
        qs0 = qs.annotate(k=Count('submission__user', distinct=True)).order_by('-k').values_list('k', flat=True)

        if not qs0:
            return []
        # make this an aggregate
        mx = float(qs0[0])

        qs = qs.annotate(unique_user_count=Count('submission__user', distinct=True))
        # fix braindamage in excluding CE
        qs = qs.annotate(submission_volume=Count(Case(
            When(submission__result='AC', then=1),
            When(submission__result='WA', then=1),
            When(submission__result='IR', then=1),
            When(submission__result='RTE', then=1),
            When(submission__result='TLE', then=1),
            When(submission__result='OLE', then=1),
            output_field=FloatField(),
        )))
        qs = qs.annotate(ac_volume=Count(Case(
            When(submission__result='AC', then=1),
            output_field=FloatField(),
        )))
        qs = qs.filter(unique_user_count__gt=max(mx / 3.0, 1))

        qs = qs.annotate(ordering=ExpressionWrapper(
            0.5 * F('points') * (0.4 * F('ac_volume') / F('submission_volume') + 0.6 * F('ac_rate')) +
            100 * e ** (F('unique_user_count') / mx), output_field=FloatField(),
        )).order_by('-ordering').defer('description')[:limit]

        cache.set(cache_key, qs, 900)
    return qs
