"""Deadline tasks (auto-submit, post-deadline auto-grade) reach a far ETA in hops.

With a Redis broker an ETA message stays unacknowledged in the worker until it runs, and Redis
re-delivers it every ``visibility_timeout`` (1 h): a homework due in a week gained a copy of its
auto-submit task every hour and ran it once per copy (seen with a real worker: one deadline task
ran three times with a 60 s timeout). Tasks are now scheduled at most ``CLASSROOM_ETA_HORIZON``
ahead; a hop that fires early and is still the stored task schedules the next one.
"""
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from judge.models import Contest, ContestParticipation, Submission
from judge.tasks.assignment_harvest import harvest_contest_drafts, harvest_participation_drafts
from judge.tasks.contest import auto_grade_contest
from judge.utils.assignment_schedule import schedule_contest_harvest, schedule_participation_harvest
from judge.utils.contest_ai import schedule_batch_ai_grade
from judge.views.tests.test_assignment_auto_submit import (AutoSubmitScheduleMixin, CONTEST_APPLY_ASYNC,
                                                           STUDENT_APPLY_ASYNC)

AUTO_GRADE_APPLY_ASYNC = 'judge.tasks.contest.auto_grade_contest.apply_async'
HORIZON = timedelta(minutes=45)


class HorizonSettingTestCase(TestCase):
    def test_horizon_stays_below_the_redis_visibility_timeout(self):
        timeout = settings.CELERY_BROKER_TRANSPORT_OPTIONS['visibility_timeout']
        self.assertLess(settings.CLASSROOM_ETA_HORIZON.total_seconds(), timeout)


@override_settings(ASSIGNMENT_HARVEST_ENABLED=True, CLASSROOM_ETA_HORIZON=HORIZON)
class AutoSubmitHopTestCase(AutoSubmitScheduleMixin, TestCase):
    def _set_end(self, delta, task_id=''):
        Contest.objects.filter(pk=self.contest.pk).update(end_time=timezone.now() + delta,
                                                          harvest_scheduled_task_id=task_id)
        self.contest.refresh_from_db()

    def _assert_one_hop(self, eta):
        self.assertLessEqual(eta, timezone.now() + HORIZON)
        self.assertGreater(eta, timezone.now() + HORIZON - timedelta(minutes=1))

    def test_a_deadline_a_week_away_is_scheduled_one_hop_ahead(self):
        self._set_end(timedelta(days=7))
        with mock.patch(CONTEST_APPLY_ASYNC) as apply_async:
            apply_async.return_value = mock.Mock(id='hop-1')
            schedule_contest_harvest(self.contest.id)
        self._assert_one_hop(apply_async.call_args.kwargs['eta'])
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.harvest_scheduled_task_id, 'hop-1')

    def test_a_deadline_within_the_horizon_is_scheduled_exactly(self):
        self._set_end(timedelta(minutes=10))
        with mock.patch(CONTEST_APPLY_ASYNC) as apply_async:
            apply_async.return_value = mock.Mock(id='hop-1')
            schedule_contest_harvest(self.contest.id)
        self.assertEqual(apply_async.call_args.kwargs['eta'], self.contest.end_time)

    def test_an_early_hop_schedules_the_next_one_and_collects_nothing(self):
        self._draft()
        self._set_end(timedelta(days=7), task_id='hop-1')
        with mock.patch(CONTEST_APPLY_ASYNC) as apply_async, mock.patch('celery.result.AsyncResult'), \
                mock.patch('judge.tasks.assignment_harvest.harvest_participation_drafts.delay') as delay:
            apply_async.return_value = mock.Mock(id='hop-2')
            result = harvest_contest_drafts.apply(args=[self.contest.key], task_id='hop-1')
        self.assertEqual(result.get(), 0)
        delay.assert_not_called()
        self._assert_one_hop(apply_async.call_args.kwargs['eta'])
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.harvest_scheduled_task_id, 'hop-2')

    def test_a_stale_early_hop_stops(self):
        # Replaced after a deadline change (its revoke was lost): it must not fork a second chain.
        self._set_end(timedelta(days=7), task_id='hop-new')
        with mock.patch(CONTEST_APPLY_ASYNC) as apply_async:
            harvest_contest_drafts.apply(args=[self.contest.key], task_id='hop-old')
        apply_async.assert_not_called()
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.harvest_scheduled_task_id, 'hop-new')

    def test_the_last_hop_collects(self):
        self._draft()
        Contest.objects.filter(pk=self.contest.pk).update(end_time=timezone.now() - timedelta(seconds=1),
                                                          harvest_scheduled_task_id='hop-9')
        with mock.patch('judge.tasks.assignment_harvest.harvest_participation_drafts.delay') as delay:
            result = harvest_contest_drafts.apply(args=[self.contest.key], task_id='hop-9')
        self.assertEqual(result.get(), 1)
        delay.assert_called_once_with(self.participation.pk)

    def test_a_personal_deadline_is_reached_in_hops(self):
        Contest.objects.filter(pk=self.contest.pk).update(time_limit=timedelta(days=3),
                                                          end_time=timezone.now() + timedelta(days=7))
        ContestParticipation.objects.filter(pk=self.participation.pk).update(harvest_scheduled_task_id='p-hop-1')
        with mock.patch(STUDENT_APPLY_ASYNC) as apply_async, mock.patch('celery.result.AsyncResult'):
            apply_async.return_value = mock.Mock(id='p-hop-2')
            schedule_participation_harvest(self.participation.pk)
            self._assert_one_hop(apply_async.call_args.kwargs['eta'])
            ContestParticipation.objects.filter(pk=self.participation.pk).update(
                harvest_scheduled_task_id='p-hop-2')
            apply_async.reset_mock()
            apply_async.return_value = mock.Mock(id='p-hop-3')
            result = harvest_participation_drafts.apply(args=[self.participation.pk], task_id='p-hop-2')
        self.assertEqual(result.get(), 0)
        self._assert_one_hop(apply_async.call_args.kwargs['eta'])
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.harvest_scheduled_task_id, 'p-hop-3')


@override_settings(CLASSROOM_ETA_HORIZON=HORIZON)
class AutoGradeHopTestCase(AutoSubmitScheduleMixin, TestCase):
    def _set(self, end_delta, delay_minutes=120, task_id=''):
        Contest.objects.filter(pk=self.contest.pk).update(
            end_time=timezone.now() + end_delta, ai_grading_auto_delay_minutes=delay_minutes,
            ai_grading_scheduled_task_id=task_id)
        self.contest.refresh_from_db()

    def test_auto_grade_two_hours_after_the_deadline_is_scheduled_one_hop_ahead(self):
        self._set(timedelta(minutes=5))
        with mock.patch(AUTO_GRADE_APPLY_ASYNC) as apply_async:
            apply_async.return_value = mock.Mock(id='ag-1')
            schedule_batch_ai_grade(self.contest.id)
        eta = apply_async.call_args.kwargs['eta']
        self.assertLessEqual(eta, timezone.now() + HORIZON)

    def test_an_early_hop_reschedules_and_does_not_grade(self):
        self._set(timedelta(minutes=5), task_id='ag-1')
        with mock.patch(AUTO_GRADE_APPLY_ASYNC) as apply_async, mock.patch('celery.result.AsyncResult'), \
                mock.patch.object(Submission, 'judge', autospec=True) as judge:
            apply_async.return_value = mock.Mock(id='ag-2')
            result = auto_grade_contest.apply(args=[self.contest.key], task_id='ag-1')
        self.assertEqual(result.get(), 0)
        judge.assert_not_called()
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.ai_grading_scheduled_task_id, 'ag-2')

    def test_a_stale_early_run_does_not_grade_or_reschedule(self):
        self._set(timedelta(minutes=5), task_id='ag-new')
        with mock.patch(AUTO_GRADE_APPLY_ASYNC) as apply_async, \
                mock.patch.object(Submission, 'judge', autospec=True) as judge:
            result = auto_grade_contest.apply(args=[self.contest.key], task_id='ag-old')
        self.assertEqual(result.get(), 0)
        apply_async.assert_not_called()
        judge.assert_not_called()

    def test_switched_off_after_queueing_does_not_grade(self):
        self._set(-timedelta(hours=3), delay_minutes=-1, task_id='ag-1')
        with mock.patch.object(Submission, 'judge', autospec=True) as judge:
            self.assertEqual(auto_grade_contest.apply(args=[self.contest.key], task_id='ag-1').get(), 0)
        judge.assert_not_called()
