import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from moss import MOSS

from judge.models import Contest, ContestMoss, ContestParticipation, ContestSubmission, Submission
from judge.utils.celery import Progress

__all__ = ('rescore_contest', 'finalize_contest_results', 'unfinalize_contest_results',
           'run_moss', 'batch_ai_grade', 'auto_grade_contest', 'ai_rejudge_submission')


def _refresh_hidden_result_aggregates(contest):
    """Recompute what is derived from verdicts that just became hidden or visible.

    Solved markers (cached), public problem stats and profile points leave out hidden exam
    verdicts (``judge.utils.result_visibility``); after (un)finalizing, refresh them so they
    neither keep a now-hidden verdict nor miss a newly revealed one.
    """
    from django.core.cache import cache
    keys = []
    for participation_id, profile_id in contest.users.values_list('id', 'user_id'):
        keys += ['user_complete:%d' % profile_id, 'contest_complete:%d' % participation_id]
    if keys:
        cache.delete_many(keys)
    for problem in contest.problems.all():
        problem._updating_stats_only = True
        problem.update_stats()
    for participation in contest.users.select_related('user').iterator():
        participation.user._updating_stats_only = True
        participation.user.calculate_points()


@shared_task(bind=True)
def rescore_contest(self, contest_key):
    contest = Contest.objects.get(key=contest_key)
    participations = contest.users

    rescored = 0
    with Progress(self, participations.count(), stage=_('Recalculating contest scores')) as p:
        for participation in participations.iterator():
            participation.recompute_results()
            rescored += 1
            if rescored % 10 == 0:
                p.done = rescored
    return rescored


@shared_task(bind=True)
def finalize_contest_results(self, contest_key):
    """End-of-contest finalize for exam-style contests.

    Clears ``run_pretests_only`` and sets ``results_finalized`` (revealing hidden results and folding
    them into the scoreboard), then rejudges **only the latest** submission per (participation, problem)
    on the final config — code on full testcases via the bridge, internally-graded types (e.g. MCQ)
    immediately, picking up any answer-key correction — and recomputes the standings. Submissions a
    teacher already scored on the Grade page (``reviewed_by`` set) are left untouched so the override
    is not wiped. Re-runnable.
    """
    contest = Contest.objects.get(key=contest_key)
    contest.run_pretests_only = False
    contest.results_finalized = True
    contest.save(update_fields=['run_pretests_only', 'results_finalized'])

    # Latest submission per (participation, problem): ordered by date, last write wins.
    latest = {}
    for contest_submission in (ContestSubmission.objects
                               .filter(participation__contest=contest)
                               .select_related('submission')
                               .order_by('submission__date', 'submission_id')):
        latest[(contest_submission.participation_id, contest_submission.problem_id)] = contest_submission.submission

    rejudged = 0
    with Progress(self, len(latest), stage=_('Rejudging latest submissions')) as p:
        for submission in latest.values():
            # A teacher override on the Grade page must survive finalize: rejudge
            # would recompute Submission.points from test cases and wipe it.
            if submission.reviewed_by_id:
                continue
            submission.judge(rejudge=True, force_judge=True)
            rejudged += 1
            if rejudged % 10 == 0:
                p.done = rejudged

    # Recompute every participation so newly revealed problems fold into the totals (the latest code
    # rejudges land asynchronously and recompute themselves again as they finish).
    for participation in contest.users.iterator():
        participation.recompute_results()
    _refresh_hidden_result_aggregates(contest)

    return rejudged


@shared_task(bind=True)
def unfinalize_contest_results(self, contest_key):
    """Reverse a finalize: re-hide an exam-style contest's results until it is finalized again.

    Clears ``results_finalized`` and recomputes every participation so the newly re-hidden problems
    drop back out of the scoreboard totals. Display masking re-applies on its own (it reads
    ``results_finalized`` live), but the stored scoreboard ``format_data`` must be recomputed here.
    Submissions are left as graded — only the finalize state and standings are reverted. Re-runnable.
    """
    contest = Contest.objects.get(key=contest_key)
    contest.results_finalized = False
    contest.save(update_fields=['results_finalized'])

    rescored = 0
    with Progress(self, contest.users.count(), stage=_('Recalculating contest scores')) as p:
        for participation in contest.users.iterator():
            participation.recompute_results()
            rescored += 1
            if rescored % 10 == 0:
                p.done = rescored
    _refresh_hidden_result_aggregates(contest)
    return rescored


@shared_task(bind=True)
def run_moss(self, contest_key):
    moss_api_key = settings.MOSS_API_KEY
    if moss_api_key is None:
        raise ImproperlyConfigured('No MOSS API Key supplied')

    contest = Contest.objects.get(key=contest_key)
    ContestMoss.objects.filter(contest=contest).delete()

    length = len(ContestMoss.LANG_MAPPING) * contest.problems.count()
    moss_results = []

    with Progress(self, length, stage=_('Running MOSS')) as p:
        for problem in contest.problems.all():
            for dmoj_lang, moss_lang in ContestMoss.LANG_MAPPING:
                result = ContestMoss(contest=contest, problem=problem, language=dmoj_lang)

                subs = Submission.objects.filter(
                    contest__participation__virtual__in=(ContestParticipation.LIVE, ContestParticipation.SPECTATE),
                    contest_object=contest,
                    problem=problem,
                    language__common_name=dmoj_lang,
                ).order_by('-points').values_list('user__user__username', 'source__source')

                if subs.exists():
                    moss_call = MOSS(moss_api_key, language=moss_lang, matching_file_limit=100,
                                     comment='%s - %s' % (contest.key, problem.code))

                    users = set()

                    for username, source in subs:
                        if username in users:
                            continue
                        users.add(username)
                        moss_call.add_file_from_memory(username, source.encode('utf-8'))

                    result.url = moss_call.process()
                    result.submission_count = len(users)

                moss_results.append(result)
                p.did(1)

    ContestMoss.objects.bulk_create(moss_results)

    return len(moss_results)


@shared_task(bind=True)
def batch_ai_grade(self, contest_key, auto=False):
    """Grade the latest submission per (participation, problem) with AI.

    Triggered by the Grade-page “Run AI grading” button. ``auto`` is retained for
    older scheduled jobs; new auto-runs use :func:`auto_grade_contest` instead.
    Idempotent if ``auto`` and a batch already finished.
    """
    from judge.utils.contest_ai import (
        latest_submissions_for_contest, problem_has_ai_grading, run_ai_for_submission,
    )

    with transaction.atomic():
        contest = Contest.objects.select_for_update().get(key=contest_key)
        if auto and contest.ai_grading_batch_finished_at is not None:
            return 0
        # Serialize concurrent runs (double-clicked button, or auto vs manual):
        # skip when a run already started and has not finished within the last
        # 2h. A crashed worker leaves ``started_at`` set; the 2h window lets a
        # later run retake the lock.
        started = contest.ai_grading_batch_started_at
        if started is not None and contest.ai_grading_batch_finished_at is None \
                and timezone.now() - started < timedelta(hours=2):
            return 0
        contest.ai_grading_batch_started_at = timezone.now()
        contest.ai_grading_batch_finished_at = None
        contest.save(update_fields=['ai_grading_batch_started_at', 'ai_grading_batch_finished_at'])

    latest = latest_submissions_for_contest(contest)
    targets = [sub for sub in latest.values() if problem_has_ai_grading(sub.problem)]
    graded = 0
    with Progress(self, max(len(targets), 1), stage=_('AI grading latest submissions')) as p:
        for submission in targets:
            try:
                run_ai_for_submission(submission, batch_pass=True)
            except Exception:
                logger = __import__('logging').getLogger(__name__)
                logger.exception('Batch AI grading failed for submission %s', submission.id)
            else:
                graded += 1
            p.did(1)

    contest.ai_grading_batch_finished_at = timezone.now()
    contest.save(update_fields=['ai_grading_batch_finished_at'])
    for participation in contest.users.iterator():
        participation.recompute_results()
    return graded


@shared_task(bind=True)
def auto_grade_contest(self, contest_key):
    """Re-grade latest objective submissions (no LLM).

    Scheduled after ``end_time`` + ``ai_grading_auto_delay_minutes``. Multiple
    choice, true/false, and exact fill-blank are rejudged; AI types are skipped.
    """
    from judge.utils.assignment_harvest import assignment_harvest_enabled, harvest_participation
    from judge.utils.contest_ai import (
        latest_submissions_for_contest, problem_is_objective_machine_graded,
    )

    contest = Contest.objects.get(key=contest_key)
    # With a zero delay this runs at the same instant as auto-submit, and re-grading before the
    # papers are in would miss them. Harvesting here is idempotent and normally a no-op; skipped
    # entirely when the harvest switch is off.
    if assignment_harvest_enabled():
        for participation in contest.users.select_related('contest', 'user__user').iterator():
            try:
                harvest_participation(participation)
            except Exception:
                logging.getLogger('judge.assignment_harvest').exception(
                    'Pre-grade auto-submit failed for participation %s', participation.pk)

    latest = latest_submissions_for_contest(contest)
    targets = [sub for sub in latest.values() if problem_is_objective_machine_graded(sub.problem)]
    graded = 0
    with Progress(self, max(len(targets), 1), stage=_('Auto-grading objective submissions')) as p:
        for submission in targets:
            try:
                submission.judge(rejudge=True, force_judge=True)
            except Exception:
                logger = __import__('logging').getLogger(__name__)
                logger.exception('Objective auto-grade failed for submission %s', submission.id)
            else:
                graded += 1
            p.did(1)
    for participation in contest.users.iterator():
        participation.recompute_results()
    return graded


@shared_task
def ai_rejudge_submission(submission_id, user_id=None):
    """Run LLM grading for one short-answer / AI fill-blank submission off the web worker.

    Queued by :func:`judge.views.widgets.rejudge_submission` so gunicorn does not
    block on the model call. ``user_id`` is the rejudge requester (for revisions).
    """
    from django.contrib.auth.models import User

    from judge.utils.contest_ai import run_ai_for_submission

    try:
        submission = Submission.objects.select_related('problem', 'user').get(id=submission_id)
    except Submission.DoesNotExist:
        return 'missing'

    rejudge_user = None
    if user_id is not None:
        rejudge_user = User.objects.filter(id=user_id).first()

    try:
        return run_ai_for_submission(
            submission, manual_trigger=True, rejudge_user=rejudge_user,
        )
    except Exception:
        logger = __import__('logging').getLogger(__name__)
        logger.exception('AI rejudge failed for submission %s', submission_id)
        return 'error'
