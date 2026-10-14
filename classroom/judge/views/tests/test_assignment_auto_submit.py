"""Getting the auto-submit to fire at the right moment, and recovering when it does not.

Collecting drafts is tested in ``judge.models.tests.test_assignment_harvest``; what is pinned down
here is *when* it is triggered — one shared task for one shared deadline, a task per student when
they each have their own, both re-pointed when a teacher moves the deadline, and a cron sweep that
catches whatever the ETA tasks lost.
"""
import json
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.management.commands.show_assignment_drafts import export_path_under, safe_export_leaf
from judge.models import AssignmentDraft, Contest, ContestParticipation, ContestProblem, Language, \
    ProblemMCQOption, Submission
from judge.models.tests.util import create_contest, create_problem, create_user
from judge.tasks.assignment_harvest import harvest_contest_drafts, harvest_participation_drafts
from judge.utils.assignment_schedule import reschedule_assignment_harvest, schedule_contest_harvest, \
    schedule_participation_harvest

CONTEST_APPLY_ASYNC = 'judge.tasks.assignment_harvest.harvest_contest_drafts.apply_async'
STUDENT_APPLY_ASYNC = 'judge.tasks.assignment_harvest.harvest_participation_drafts.apply_async'


class AutoSubmitScheduleMixin:
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.student = create_user(username='autosubmit_student')
        cls.mcq = create_problem('autosubmit-mcq', submission_type='M', is_public=False, points=50)
        cls.option = ProblemMCQOption.objects.create(problem=cls.mcq, order=1, text='right', is_correct=True)
        ProblemMCQOption.objects.create(problem=cls.mcq, order=2, text='wrong', is_correct=False)
        cls.contest = create_contest(
            key='autosubmitasn', name='Auto-submit assignment', start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=1), is_visible=True,
        )
        cls.contest_problem = ContestProblem.objects.create(
            contest=cls.contest, problem=cls.mcq, points=50, order=1,
        )

    def setUp(self):
        self.participation = ContestParticipation.objects.create(
            contest=self.contest, user=self.student.profile,
            real_start=timezone.now() - timedelta(minutes=30),
        )

    def _draft(self, participation=None):
        return AssignmentDraft.objects.create(
            participation=participation or self.participation, problem=self.mcq,
            content=json.dumps({'selected': [self.option.id]}),
        )

    def _reload_participation(self):
        """Re-fetch so ``end_time`` is recomputed from the contest as it now stands."""
        self.participation = ContestParticipation.objects.select_related('contest').get(pk=self.participation.pk)
        return self.participation

    def _end_assignment(self, ago=timedelta(hours=1)):
        Contest.objects.filter(pk=self.contest.pk).update(end_time=timezone.now() - ago)
        self.contest.refresh_from_db()
        self._reload_participation()

    def _harvested_submissions(self):
        return Submission.objects.filter(contest_object=self.contest)


@override_settings(ASSIGNMENT_HARVEST_ENABLED=True)
class ContestLevelScheduleTestCase(AutoSubmitScheduleMixin, TestCase):
    """One shared deadline gets one task, and it fans out only to students who left work behind."""

    @override_settings(CLASSROOM_ETA_HORIZON=timedelta(days=1))  # deadline within one hop
    def test_the_assignment_is_scheduled_at_its_end_time(self):
        with mock.patch(CONTEST_APPLY_ASYNC) as apply_async:
            apply_async.return_value = mock.Mock(id='task-contest')
            schedule_contest_harvest(self.contest.id)

        apply_async.assert_called_once_with(args=[self.contest.key], eta=self.contest.end_time)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.harvest_scheduled_task_id, 'task-contest')

    def test_an_assignment_that_already_ended_is_not_scheduled(self):
        # The sweep is what covers a deadline in the past; an ETA there would fire instantly.
        Contest.objects.filter(pk=self.contest.pk).update(end_time=timezone.now() - timedelta(minutes=5))
        with mock.patch(CONTEST_APPLY_ASYNC) as apply_async:
            schedule_contest_harvest(self.contest.id)

        apply_async.assert_not_called()
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.harvest_scheduled_task_id, '')

    def test_only_students_who_left_a_draft_are_queued(self):
        # A class where everybody submitted by hand must not cost one task per student.
        idle = ContestParticipation.objects.create(
            contest=self.contest, user=create_user(username='autosubmit_idle').profile,
        )
        self._draft()
        self._end_assignment()

        with mock.patch('judge.tasks.assignment_harvest.harvest_participation_drafts.delay') as delay:
            self.assertEqual(harvest_contest_drafts(self.contest.key), 1)

        delay.assert_called_once_with(self.participation.pk)
        self.assertNotIn(idle.pk, [call.args[0] for call in delay.call_args_list])

    def test_an_already_collected_draft_is_not_queued_again(self):
        draft = self._draft()
        AssignmentDraft.objects.filter(pk=draft.pk).update(harvested_at=timezone.now())

        with mock.patch('judge.tasks.assignment_harvest.harvest_participation_drafts.delay') as delay:
            self.assertEqual(harvest_contest_drafts(self.contest.key), 0)

        delay.assert_not_called()


@override_settings(ASSIGNMENT_HARVEST_ENABLED=True)
class ParticipationLevelScheduleTestCase(AutoSubmitScheduleMixin, TestCase):
    """A personal time limit means the assignment does not close for everybody at once."""

    def setUp(self):
        super().setUp()
        Contest.objects.filter(pk=self.contest.pk).update(time_limit=timedelta(minutes=45))
        self._reload_participation()

    def test_each_student_is_scheduled_at_their_own_deadline(self):
        with mock.patch(STUDENT_APPLY_ASYNC) as apply_async:
            apply_async.return_value = mock.Mock(id='task-student')
            schedule_participation_harvest(self.participation.pk)

        apply_async.assert_called_once_with(
            args=[self.participation.pk], eta=self.participation.real_start + timedelta(minutes=45),
        )
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.harvest_scheduled_task_id, 'task-student')

    def test_the_shared_task_is_dropped_once_students_have_their_own_deadline(self):
        # Both levels firing for the same student would just race each other.
        Contest.objects.filter(pk=self.contest.pk).update(harvest_scheduled_task_id='task-stale')

        with mock.patch(CONTEST_APPLY_ASYNC) as apply_async, \
                mock.patch('celery.result.AsyncResult') as async_result:
            schedule_contest_harvest(self.contest.id)

        apply_async.assert_not_called()
        async_result.assert_called_once_with('task-stale')
        async_result.return_value.revoke.assert_called_once_with()
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.harvest_scheduled_task_id, '')

    def test_a_spectator_is_not_scheduled(self):
        spectator = ContestParticipation.objects.create(
            contest=self.contest, user=create_user(username='autosubmit_spectator').profile,
            virtual=ContestParticipation.SPECTATE,
        )

        with mock.patch(STUDENT_APPLY_ASYNC) as apply_async:
            schedule_participation_harvest(spectator.pk)

        apply_async.assert_not_called()


@override_settings(ASSIGNMENT_HARVEST_ENABLED=True)
class DeadlineChangeTestCase(AutoSubmitScheduleMixin, TestCase):
    """A moved deadline has to take the collection with it, in both directions."""

    def _reschedule(self, new_task_id='task-new'):
        with mock.patch(CONTEST_APPLY_ASYNC) as apply_async, \
                mock.patch('celery.result.AsyncResult') as async_result:
            apply_async.return_value = mock.Mock(id=new_task_id)
            reschedule_assignment_harvest(self.contest.id)
        return apply_async, async_result

    def _move_end_time(self, delta):
        Contest.objects.filter(pk=self.contest.pk).update(
            harvest_scheduled_task_id='task-old', end_time=timezone.now() + delta,
        )

    @override_settings(CLASSROOM_ETA_HORIZON=timedelta(days=1))  # deadline within one hop
    def test_extending_the_deadline_moves_the_collection_later(self):
        self._move_end_time(timedelta(hours=3))
        apply_async, async_result = self._reschedule()

        async_result.assert_called_once_with('task-old')
        async_result.return_value.revoke.assert_called_once_with()
        self.contest.refresh_from_db()
        self.assertEqual(apply_async.call_args.kwargs['eta'], self.contest.end_time)
        self.assertEqual(self.contest.harvest_scheduled_task_id, 'task-new')

    def test_bringing_the_deadline_forward_moves_the_collection_earlier(self):
        self._move_end_time(timedelta(minutes=10))
        apply_async, async_result = self._reschedule()

        async_result.assert_called_once_with('task-old')
        async_result.return_value.revoke.assert_called_once_with()
        self.contest.refresh_from_db()
        self.assertEqual(apply_async.call_args.kwargs['eta'], self.contest.end_time)
        self.assertEqual(self.contest.harvest_scheduled_task_id, 'task-new')

    def test_adding_a_time_limit_swaps_the_shared_task_for_per_student_ones(self):
        Contest.objects.filter(pk=self.contest.pk).update(
            harvest_scheduled_task_id='task-old', time_limit=timedelta(minutes=45),
        )

        with mock.patch(CONTEST_APPLY_ASYNC) as contest_apply, \
                mock.patch(STUDENT_APPLY_ASYNC) as student_apply, \
                mock.patch('celery.result.AsyncResult'):
            student_apply.return_value = mock.Mock(id='task-student')
            reschedule_assignment_harvest(self.contest.id)

        contest_apply.assert_not_called()
        student_apply.assert_called_once()
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.harvest_scheduled_task_id, 'task-student')

    def test_removing_a_time_limit_revokes_the_per_student_tasks(self):
        ContestParticipation.objects.filter(pk=self.participation.pk).update(
            harvest_scheduled_task_id='task-student-old',
        )

        with mock.patch(CONTEST_APPLY_ASYNC) as contest_apply, \
                mock.patch(STUDENT_APPLY_ASYNC) as student_apply, \
                mock.patch('celery.result.AsyncResult') as async_result:
            contest_apply.return_value = mock.Mock(id='task-new')
            reschedule_assignment_harvest(self.contest.id)

        student_apply.assert_not_called()
        self.assertIn('task-student-old', [call.args[0] for call in async_result.call_args_list])
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.harvest_scheduled_task_id, '')


@override_settings(ASSIGNMENT_HARVEST_ENABLED=True)
class DeadlineGuardTestCase(AutoSubmitScheduleMixin, TestCase):
    """A leftover ETA must not collect papers while the student can still write."""

    def test_a_shared_deadline_still_in_the_future_queues_nobody(self):
        self._draft()

        with mock.patch('judge.tasks.assignment_harvest.harvest_participation_drafts.delay') as delay:
            self.assertEqual(harvest_contest_drafts(self.contest.key), 0)

        delay.assert_not_called()
        self.assertFalse(self._harvested_submissions().exists())

    def test_a_student_still_inside_their_window_is_not_collected(self):
        self._draft()

        with mock.patch.object(Submission, 'judge', autospec=True):
            self.assertEqual(harvest_participation_drafts(self.participation.pk), 0)

        self.assertFalse(self._harvested_submissions().exists())
        draft = AssignmentDraft.objects.get(participation=self.participation)
        self.assertIsNone(draft.harvested_at)

    def test_moving_the_deadline_later_stops_the_old_task(self):
        self._draft()
        self._end_assignment(ago=timedelta(minutes=1))
        Contest.objects.filter(pk=self.contest.pk).update(
            end_time=timezone.now() + timedelta(hours=2),
        )
        self.contest.refresh_from_db()
        self._reload_participation()

        with mock.patch('judge.tasks.assignment_harvest.harvest_participation_drafts.delay') as delay:
            self.assertEqual(harvest_contest_drafts(self.contest.key), 0)
        delay.assert_not_called()

        with mock.patch.object(Submission, 'judge', autospec=True):
            self.assertEqual(harvest_participation_drafts(self.participation.pk), 0)
        self.assertFalse(self._harvested_submissions().exists())

    def test_the_deadline_task_collects_once_the_window_has_closed(self):
        self._draft()
        self._end_assignment(ago=timedelta(minutes=1))

        with mock.patch.object(Submission, 'judge', autospec=True):
            self.assertEqual(harvest_participation_drafts(self.participation.pk), 1)
        self.assertEqual(self._harvested_submissions().count(), 1)

    def test_reschedule_command_schedules_open_assignments(self):
        with mock.patch(CONTEST_APPLY_ASYNC) as apply_async:
            apply_async.return_value = mock.Mock(id='task-backfill')
            out = StringIO()
            call_command('reschedule_assignment_harvests', stdout=out)

        apply_async.assert_called()
        self.assertIn('scheduled', out.getvalue())
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.harvest_scheduled_task_id, 'task-backfill')

    def test_reschedule_command_skips_ended_assignments(self):
        self._end_assignment()

        with mock.patch(CONTEST_APPLY_ASYNC) as apply_async:
            out = StringIO()
            call_command('reschedule_assignment_harvests', stdout=out)

        apply_async.assert_not_called()
        self.assertIn('scheduled 0', out.getvalue())


@override_settings(ASSIGNMENT_HARVEST_ENABLED=True)
class RecoverySweepTestCase(AutoSubmitScheduleMixin, TestCase):
    """The ETA tasks live in worker memory; this is what stops a restart from costing a class."""

    def _sweep(self, *args):
        out, err = StringIO(), StringIO()
        call_command('harvest_assignment_drafts', *args, stdout=out, stderr=err)
        return out.getvalue()

    def test_a_draft_the_deadline_task_missed_is_collected(self):
        self._end_assignment()
        draft = self._draft()

        self._sweep()

        draft.refresh_from_db()
        self.assertIsNotNone(draft.harvested_at)
        self.assertEqual(self._harvested_submissions().count(), 1)

    def test_an_assignment_that_is_still_running_is_left_alone(self):
        self._draft()

        self._sweep()

        self.assertFalse(self._harvested_submissions().exists())

    def test_the_deadline_task_is_given_its_grace_period_first(self):
        # The scheduled task owns the deadline itself; the sweep only steps in once enough time
        # has passed that it is clear the task did not run.
        self._end_assignment(ago=timedelta(minutes=1))
        self._draft()

        self._sweep()

        self.assertFalse(self._harvested_submissions().exists())

    def test_a_personal_deadline_inside_a_running_assignment_is_collected(self):
        # The assignment is still open, but this student's own window closed an hour ago.
        Contest.objects.filter(pk=self.contest.pk).update(time_limit=timedelta(minutes=10))
        ContestParticipation.objects.filter(pk=self.participation.pk).update(
            real_start=timezone.now() - timedelta(hours=1),
        )
        self._draft()

        self._sweep()

        self.assertEqual(self._harvested_submissions().count(), 1)

    def test_a_student_still_inside_their_personal_window_is_not_collected(self):
        # A time limit puts classmates on different deadlines, so "the assignment is time-limited"
        # is as far as the query can narrow: submitting this student now would cut their time short.
        Contest.objects.filter(pk=self.contest.pk).update(time_limit=timedelta(hours=2))
        ContestParticipation.objects.filter(pk=self.participation.pk).update(
            real_start=timezone.now() - timedelta(minutes=10),
        )
        self._draft()

        self._sweep()

        self.assertFalse(self._harvested_submissions().exists())

    def test_sweeping_twice_does_not_submit_twice(self):
        self._end_assignment()
        self._draft()

        self._sweep()
        self._sweep()

        self.assertEqual(self._harvested_submissions().count(), 1)

    def test_the_sweep_can_be_restricted_to_one_assignment(self):
        self._end_assignment()
        self._draft()

        self._sweep('--contest', 'someotherassignment')
        self.assertFalse(self._harvested_submissions().exists())

        self._sweep('--contest', self.contest.key)
        self.assertEqual(self._harvested_submissions().count(), 1)

    def test_the_limit_caps_the_work_and_the_next_run_continues(self):
        self._end_assignment()
        other = ContestParticipation.objects.create(
            contest=self.contest, user=create_user(username='autosubmit_other').profile,
        )
        self._draft()
        self._draft(participation=other)

        output = self._sweep('--limit', '1')

        self.assertEqual(self._harvested_submissions().count(), 1)
        self.assertIn('1 participation(s) still due', output)

        self._sweep()
        self.assertEqual(self._harvested_submissions().count(), 2)

    def test_the_sweep_exits_nonzero_when_a_student_cannot_be_collected(self):
        # Cron MAILTO is the alert channel: a silent success here would hide lost answers.
        self._end_assignment()
        self._draft()

        with mock.patch('judge.management.commands.harvest_assignment_drafts.harvest_participation',
                        side_effect=RuntimeError('boom')):
            with self.assertRaises(CommandError):
                self._sweep()


@override_settings(ASSIGNMENT_HARVEST_ENABLED=True)
class WholeClassDeadlineTestCase(TestCase):
    """The worst case for the judge: one shared deadline, so every unsubmitted answer in the class
    arrives in the same second.

    What keeps that survivable is the shape of the fan-out. The deadline task only *finds* who left
    work and queues one task each — it creates nothing itself, so no single task holds a worker for
    the length of a class or loses the whole assignment when it dies. And because collection is
    driven by the draft table, each student contributes at most one submission per problem, which
    is what bounds the burst and keeps auto-submit inside the same in-flight budget
    (``DMOJ_SUBMISSION_LIMIT``) a student could reach by hand.
    """
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.python = Language.objects.get(key='PY3')
        cls.first = create_problem('wholeclass-a', submission_type='C', is_public=False,
                                   allowed_languages=('PY3',), points=50)
        cls.second = create_problem('wholeclass-b', submission_type='C', is_public=False,
                                    allowed_languages=('PY3',), points=50)
        cls.contest = create_contest(
            key='wholeclassasn', name='Whole class assignment', start_time=now - timedelta(hours=1),
            end_time=now - timedelta(minutes=1), is_visible=True,
        )
        cls.contest_problems = {
            problem: ContestProblem.objects.create(
                contest=cls.contest, problem=problem, points=50, order=order,
            )
            for order, problem in enumerate((cls.first, cls.second), start=1)
        }

    def setUp(self):
        # Five students share the deadline: three left work behind, two submitted and left.
        self.participations = {}
        for index in range(5):
            student = create_user(username='wholeclass_%d' % index)
            self.participations[index] = ContestParticipation.objects.create(
                contest=self.contest, user=student.profile,
            )
        self._draft(0, self.first)
        self._draft(1, self.first)
        self._draft(2, self.first)
        self._draft(2, self.second)
        self.drafted = {self.participations[i].pk for i in (0, 1, 2)}

    def _draft(self, index, problem, source='print(1)'):
        return AssignmentDraft.objects.create(
            participation=self.participations[index], problem=problem,
            content=source, language=self.python,
        )

    def _submissions(self, participation=None):
        queryset = Submission.objects.filter(contest_object=self.contest)
        if participation is not None:
            queryset = queryset.filter(user=participation.user)
        return queryset

    def _collect_the_class(self):
        """Run the deadline task, then the per-student tasks it queued, as a worker would."""
        with mock.patch('judge.tasks.assignment_harvest.harvest_participation_drafts.delay') as delay:
            queued = harvest_contest_drafts(self.contest.key)
        dispatched = [call.args[0] for call in delay.call_args_list]
        with mock.patch.object(Submission, 'judge', autospec=True) as judge:
            for participation_id in dispatched:
                harvest_participation_drafts(participation_id)
        return queued, dispatched, judge

    def test_the_deadline_task_queues_the_class_and_creates_nothing_itself(self):
        with mock.patch('judge.tasks.assignment_harvest.harvest_participation_drafts.delay') as delay:
            queued = harvest_contest_drafts(self.contest.key)

        self.assertEqual(queued, 3)
        self.assertEqual({call.args[0] for call in delay.call_args_list}, self.drafted)
        # Everything the class owes is still owed: this task hands out work, it does not do it.
        self.assertFalse(self._submissions().exists())

    def test_students_who_submitted_by_hand_are_not_queued(self):
        # An assignment where everybody submitted must cost one query, not one task per student.
        idle = {self.participations[i].pk for i in (3, 4)}

        with mock.patch('judge.tasks.assignment_harvest.harvest_participation_drafts.delay') as delay:
            harvest_contest_drafts(self.contest.key)

        self.assertFalse(idle & {call.args[0] for call in delay.call_args_list})

    def test_each_student_is_collected_in_their_own_task(self):
        queued, dispatched, judge = self._collect_the_class()

        self.assertEqual(queued, len(dispatched))
        # Four drafts across three students, and every one of them landed.
        self.assertEqual(self._submissions().count(), 4)
        self.assertEqual(self._submissions(self.participations[2]).count(), 2)
        self.assertFalse(self._submissions(self.participations[3]).exists())

    def test_every_collected_code_answer_is_queued_for_judging_exactly_once(self):
        # Queued, not judged inline: the burst has to go through the judge queue like any submit.
        _queued, _dispatched, judge = self._collect_the_class()

        judged = [call.args[0].id for call in judge.call_args_list]
        self.assertCountEqual(judged, list(self._submissions().values_list('id', flat=True)))
        self.assertTrue(all(call.kwargs.get('force_judge') for call in judge.call_args_list))

    def test_a_student_gets_at_most_one_submission_per_problem(self):
        # This is what bounds the deadline burst: the class contributes one submission per
        # unanswered problem, never a multiple of it, so auto-submit cannot push a student past
        # the in-flight budget their own submits are held to.
        self._collect_the_class()

        for participation in self.participations.values():
            for problem in (self.first, self.second):
                self.assertLessEqual(
                    self._submissions(participation).filter(problem=problem).count(), 1,
                )

    def test_collecting_the_class_twice_does_not_double_the_burst(self):
        # The recovery sweep overlaps the deadline task by design, so a second pass has to be free.
        self._collect_the_class()
        self.assertEqual(self._submissions().count(), 4)

        queued, _dispatched, _judge = self._collect_the_class()

        self.assertEqual(queued, 0)
        self.assertEqual(self._submissions().count(), 4)


@override_settings(ASSIGNMENT_HARVEST_ENABLED=True)
class LeaveHarvestTestCase(AutoSubmitScheduleMixin, TestCase):
    """One Leave is one harvest. The participation stays; the deadline still collects leftovers."""

    def _join(self):
        self.student.profile.current_contest = self.participation
        self.student.profile.save()
        self.client.force_login(self.student)

    def _leave(self):
        return self.client.post(reverse('contest_leave', args=[self.contest.key]))

    def test_leaving_collects_the_saved_draft(self):
        self._join()
        draft = self._draft()

        response = self._leave()

        self.assertRedirects(response, reverse('contest_view', args=[self.contest.key]),
                             fetch_redirect_response=False)
        draft.refresh_from_db()
        self.assertIsNotNone(draft.harvested_at)
        self.assertEqual(self._harvested_submissions().count(), 1)
        self.student.profile.refresh_from_db()
        self.assertIsNone(self.student.profile.current_contest)
        self.assertTrue(ContestParticipation.objects.filter(pk=self.participation.pk).exists())

    def test_leaving_again_without_a_new_draft_does_not_submit_twice(self):
        self._join()
        self._draft()
        self._leave()

        self._join()
        self._leave()

        self.assertEqual(self._harvested_submissions().count(), 1)

    def test_leaving_still_exits_if_collection_fails(self):
        self._join()
        draft = self._draft()

        with mock.patch('judge.utils.assignment_harvest.harvest_participation',
                        side_effect=RuntimeError('boom')):
            with self.assertLogs('judge.views.contests', level='ERROR'):
                self._leave()

        self.student.profile.refresh_from_db()
        self.assertIsNone(self.student.profile.current_contest)
        draft.refresh_from_db()
        self.assertIsNone(draft.harvested_at)
        self.assertFalse(self._harvested_submissions().exists())

    def test_a_student_who_left_can_still_be_collected_at_the_deadline(self):
        # Leave does not cancel the participation. Collection failed this time, so the
        # unharvested draft is still sitting on that row when the window closes.
        self._join()
        draft = self._draft()
        with mock.patch('judge.utils.assignment_harvest.harvest_participation',
                        side_effect=RuntimeError('boom')):
            with self.assertLogs('judge.views.contests', level='ERROR'):
                self._leave()

        self._end_assignment()
        call_command('harvest_assignment_drafts', stdout=StringIO(), stderr=StringIO())

        draft.refresh_from_db()
        self.assertIsNotNone(draft.harvested_at)
        self.assertEqual(self._harvested_submissions().count(), 1)

    def test_coming_back_after_leave_leaves_a_new_draft_for_the_deadline(self):
        self._join()
        draft = self._draft()
        self._leave()
        self.assertEqual(self._harvested_submissions().count(), 1)

        AssignmentDraft.objects.filter(pk=draft.pk).update(
            content=json.dumps({'selected': [self.option.id]}),
            harvested_at=None,
        )
        self._end_assignment()
        call_command('harvest_assignment_drafts', stdout=StringIO(), stderr=StringIO())

        self.assertEqual(self._harvested_submissions().count(), 2)

    def test_a_spectator_leave_does_not_collect(self):
        spectator_user = create_user(username='autosubmit_leave_spectator')
        spectator = ContestParticipation.objects.create(
            contest=self.contest, user=spectator_user.profile,
            virtual=ContestParticipation.SPECTATE,
        )
        AssignmentDraft.objects.create(
            participation=spectator, problem=self.mcq,
            content=json.dumps({'selected': [self.option.id]}),
        )
        spectator_user.profile.current_contest = spectator
        spectator_user.profile.save()
        self.client.force_login(spectator_user)

        self.client.post(reverse('contest_leave', args=[self.contest.key]))

        self.assertFalse(self._harvested_submissions().exists())
        spectator_user.profile.refresh_from_db()
        self.assertIsNone(spectator_user.profile.current_contest)


class HarvestDisabledTestCase(AutoSubmitScheduleMixin, TestCase):
    """Default switch: drafts stay on the server and are never collected."""

    def test_leave_does_not_collect(self):
        self.student.profile.current_contest = self.participation
        self.student.profile.save()
        self.client.force_login(self.student)
        draft = self._draft()

        self.client.post(reverse('contest_leave', args=[self.contest.key]))

        draft.refresh_from_db()
        self.assertIsNone(draft.harvested_at)
        self.assertFalse(self._harvested_submissions().exists())
        self.student.profile.refresh_from_db()
        self.assertIsNone(self.student.profile.current_contest)

    def test_deadline_task_does_not_queue(self):
        self._draft()
        self._end_assignment()
        with mock.patch('judge.tasks.assignment_harvest.harvest_participation_drafts.delay') as delay:
            self.assertEqual(harvest_contest_drafts(self.contest.key), 0)
        delay.assert_not_called()

    def test_sweep_does_not_collect(self):
        draft = self._draft()
        self._end_assignment()
        call_command('harvest_assignment_drafts', stdout=StringIO(), stderr=StringIO())
        draft.refresh_from_db()
        self.assertIsNone(draft.harvested_at)
        self.assertFalse(self._harvested_submissions().exists())

    def test_schedule_does_not_enqueue(self):
        with mock.patch(CONTEST_APPLY_ASYNC) as apply_async:
            schedule_contest_harvest(self.contest.id)
        apply_async.assert_not_called()
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.harvest_scheduled_task_id, '')


class ShowAssignmentDraftsCommandTestCase(TestCase):
    """CLI is the diagnostic for leftover drafts after the teacher Drafts page was removed."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.teacher = create_user(username='drafts_teacher')
        cls.student = create_user(username='drafts_student')
        cls.contest = create_contest(
            key='draftsview', name='Drafts view',
            start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=1),
            is_visible=True, authors=('drafts_teacher',),
        )
        cls.problem = create_problem('drafts-prob', is_public=False, allowed_languages=('PY3',))
        ContestProblem.objects.create(contest=cls.contest, problem=cls.problem, points=50, order=1)
        cls.participation = ContestParticipation.objects.create(
            contest=cls.contest, user=cls.student.profile,
        )
        AssignmentDraft.objects.create(
            participation=cls.participation, problem=cls.problem, content='print(1)',
            language=Language.objects.get(key='PY3'),
        )

    def test_command_prints_the_saved_source(self):
        out = StringIO()
        call_command('show_assignment_drafts', self.contest.key, stdout=out)
        text = out.getvalue()
        self.assertIn('drafts_student', text)
        self.assertIn('print(1)', text)

    def test_the_drafts_page_is_gone(self):
        self.client.force_login(self.teacher)
        response = self.client.get('/contest/%s/drafts' % self.contest.key)
        self.assertEqual(response.status_code, 404)


class SafeExportPathTestCase(SimpleTestCase):
    """--save-files must not follow a student filename out of the chosen directory."""

    def test_unix_and_windows_traversal_stay_inside_save_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            save = Path(tmp) / 'exports'
            save.mkdir()
            unix = export_path_under(save, 'alice', 'p1', '../../etc/passwd')
            windows = export_path_under(save, 'alice', 'p1', '..\\..\\evil.txt')
            self.assertTrue(unix.is_relative_to(save.resolve()))
            self.assertTrue(windows.is_relative_to(save.resolve()))
            self.assertEqual(unix.name, 'alice_p1_passwd')
            self.assertEqual(windows.name, 'alice_p1_evil.txt')

    def test_dotdot_filename_falls_back(self):
        self.assertEqual(safe_export_leaf('..'), 'file')
        self.assertEqual(safe_export_leaf(''), 'file')
        with tempfile.TemporaryDirectory() as tmp:
            dest = export_path_under(tmp, 'alice', 'p1', '..')
            self.assertTrue(dest.is_relative_to(Path(tmp).resolve()))
            self.assertEqual(dest.name, 'alice_p1_upload')
