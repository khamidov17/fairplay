"""Collecting the drafts a participation still holds when its assignment closes."""
import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from judge.models import AssignmentDraft, Contest, ContestParticipation, ContestProblem, ContestSubmission, \
    Language, ProblemMCQOption, ProblemTemplate, Submission
from judge.models.tests.util import create_contest, create_problem, create_user
from judge.tasks.contest import auto_grade_contest
from judge.utils.assignment_harvest import harvest_participation
from judge.utils.submission_create import create_submission


class HarvestTestMixin:
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.teacher = create_user(username='harvest_teacher')
        cls.student = create_user(username='harvest_student')
        cls.python = Language.objects.get(key='PY3')
        cls.code = create_problem('harvest-code', submission_type='C', is_public=False,
                                  allowed_languages=('PY3',), points=50)
        cls.mcq = create_problem('harvest-mcq', submission_type='M', is_public=False, points=50)
        cls.option = ProblemMCQOption.objects.create(problem=cls.mcq, order=1, text='right', is_correct=True)
        ProblemMCQOption.objects.create(problem=cls.mcq, order=2, text='wrong', is_correct=False)
        cls.contest = create_contest(
            key='harvestasn', name='Harvest assignment', start_time=now - timedelta(hours=2),
            end_time=now + timedelta(hours=1), authors=('harvest_teacher',), is_visible=True,
        )
        cls.code_problem = ContestProblem.objects.create(
            contest=cls.contest, problem=cls.code, points=50, order=1,
        )
        cls.mcq_problem = ContestProblem.objects.create(
            contest=cls.contest, problem=cls.mcq, points=50, order=2,
        )

    def setUp(self):
        self.participation = ContestParticipation.objects.create(
            contest=self.contest, user=self.student.profile,
        )

    def _code_draft(self, source='print(1)', participation=None):
        return AssignmentDraft.objects.create(
            participation=participation or self.participation, problem=self.code,
            content=source, language=self.python,
        )

    def _mcq_draft(self, participation=None, selected=None):
        return AssignmentDraft.objects.create(
            participation=participation or self.participation, problem=self.mcq,
            content=json.dumps({'selected': selected if selected is not None else [self.option.id]}),
        )

    def _submissions(self):
        return Submission.objects.filter(user=self.student.profile, contest_object=self.contest)


class HarvestCoreTestCase(HarvestTestMixin, TestCase):
    """A surviving draft means "never submitted, or edited since" — both need collecting."""

    def test_forgotten_draft_becomes_a_submission(self):
        draft = self._code_draft()
        created = harvest_participation(self.participation)
        self.assertEqual(len(created), 1)
        submission = created[0]
        self.assertEqual(submission.problem, self.code)
        self.assertEqual(submission.source.source, 'print(1)')
        self.assertEqual(submission.language, self.python)
        draft.refresh_from_db()
        self.assertIsNotNone(draft.harvested_at)
        self.assertEqual(draft.content, '')

    def test_draft_edited_after_a_submission_is_submitted_again(self):
        Submission.objects.create(user=self.student.profile, problem=self.code, language=self.python)
        self._code_draft('print(2)')
        created = harvest_participation(self.participation)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].source.source, 'print(2)')

    def test_no_draft_means_nothing_is_submitted(self):
        self.assertEqual(harvest_participation(self.participation), [])
        self.assertFalse(self._submissions().exists())

    def test_mcq_draft_is_graded_by_the_normal_path(self):
        self._mcq_draft()
        created = harvest_participation(self.participation)
        self.assertEqual(len(created), 1)
        submission = created[0]
        submission.refresh_from_db()
        self.assertEqual(submission.status, 'D')
        self.assertEqual(submission.result, 'AC')
        self.assertEqual(json.loads(submission.source.source)['selected'], [self.option.id])

    def test_harvested_work_belongs_to_the_participation(self):
        self._code_draft()
        submission = harvest_participation(self.participation)[0]
        self.assertEqual(submission.contest_object, self.contest)
        contest_submission = ContestSubmission.objects.get(submission=submission)
        self.assertEqual(contest_submission.participation, self.participation)
        self.assertEqual(contest_submission.problem, self.code_problem)

    def test_harvested_work_counts_towards_the_scoreboard(self):
        self._mcq_draft()
        harvest_participation(self.participation)
        self.participation.recompute_results()
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.score, 50)

    def test_all_drafts_are_collected(self):
        self._code_draft()
        self._mcq_draft()
        created = harvest_participation(self.participation)
        self.assertEqual({submission.problem_id for submission in created}, {self.code.id, self.mcq.id})


class HarvestIdempotencyTestCase(HarvestTestMixin, TestCase):
    """Deadline task, recovery sweep and part locking can all fire near each other."""

    def test_second_harvest_creates_nothing(self):
        self._code_draft()
        self.assertEqual(len(harvest_participation(self.participation)), 1)
        self.assertEqual(harvest_participation(self.participation), [])
        self.assertEqual(self._submissions().count(), 1)

    def test_already_harvested_draft_is_skipped(self):
        self._code_draft()
        AssignmentDraft.objects.update(harvested_at=timezone.now())
        self.assertEqual(harvest_participation(self.participation), [])
        self.assertFalse(self._submissions().exists())

    def test_failed_submission_releases_the_claim_so_a_retry_can_collect_it(self):
        # Losing an answer is worse than submitting it twice, so a claim that produced no
        # submission must not leave the draft looking collected.
        draft = self._code_draft()
        with mock.patch('judge.utils.assignment_harvest.create_submission', side_effect=RuntimeError('boom')):
            with self.assertLogs('judge.assignment_harvest', level='ERROR'):
                self.assertEqual(harvest_participation(self.participation), [])
        draft.refresh_from_db()
        self.assertIsNone(draft.harvested_at)
        self.assertEqual(draft.content, 'print(1)')
        self.assertEqual(len(harvest_participation(self.participation)), 1)


class HarvestExclusionTestCase(HarvestTestMixin, TestCase):
    """Every exclusion here is an answer that deliberately does not get submitted."""

    def test_empty_draft_is_not_submitted(self):
        AssignmentDraft.objects.create(
            participation=self.participation, problem=self.code, content='   \n', language=self.python,
        )
        self.assertEqual(harvest_participation(self.participation), [])

    def test_untouched_starter_template_is_not_submitted(self):
        ProblemTemplate.objects.create(problem=self.code, language=self.python, code='def solve():\n    pass\n')
        self._code_draft('def solve():\n    pass\n')
        self.assertEqual(harvest_participation(self.participation), [])
        self.assertFalse(self._submissions().exists())

    def test_mcq_draft_with_no_selection_is_not_submitted(self):
        self._mcq_draft(selected=[])
        self.assertEqual(harvest_participation(self.participation), [])

    def test_spectator_is_not_harvested(self):
        spectator = ContestParticipation.objects.create(
            contest=self.contest, user=self.student.profile, virtual=ContestParticipation.SPECTATE,
        )
        self._code_draft(participation=spectator)
        self.assertEqual(harvest_participation(spectator), [])
        self.assertFalse(self._submissions().exists())

    def test_teacher_bypassing_assignment_rules_is_not_harvested(self):
        participation = ContestParticipation.objects.create(
            contest=self.contest, user=self.teacher.profile,
        )
        AssignmentDraft.objects.create(
            participation=participation, problem=self.code, content='print(1)', language=self.python,
        )
        self.assertEqual(harvest_participation(participation), [])

    def test_disqualified_participation_is_not_harvested(self):
        self._code_draft()
        self.participation.set_disqualified(True)
        self.assertEqual(harvest_participation(self.participation), [])
        self.assertFalse(self._submissions().exists())

    def test_problem_at_its_submission_limit_is_not_harvested(self):
        self.mcq_problem.max_submissions = 1
        self.mcq_problem.save(update_fields=['max_submissions'])
        submission = Submission.objects.create(
            user=self.student.profile, problem=self.mcq, language=Language.get_answer_language(),
            contest_object=self.contest, status='D', result='WA',
        )
        ContestSubmission.objects.create(
            submission=submission, problem=self.mcq_problem, participation=self.participation,
        )
        self._mcq_draft()
        self.assertEqual(harvest_participation(self.participation), [])

    def test_problem_removed_from_the_assignment_is_skipped(self):
        self._code_draft()
        self.code_problem.delete()
        self.assertEqual(harvest_participation(self.participation), [])

    def test_submission_limit_is_not_exceeded_across_one_harvest(self):
        # Two problems, one of which is already full: the other still has to be collected.
        self.mcq_problem.max_submissions = 1
        self.mcq_problem.save(update_fields=['max_submissions'])
        submission = Submission.objects.create(
            user=self.student.profile, problem=self.mcq, language=Language.get_answer_language(),
            contest_object=self.contest, status='D', result='WA',
        )
        ContestSubmission.objects.create(
            submission=submission, problem=self.mcq_problem, participation=self.participation,
        )
        self._mcq_draft()
        self._code_draft()
        created = harvest_participation(self.participation)
        self.assertEqual([s.problem_id for s in created], [self.code.id])


class _NoProgress:
    """Stub for judge.utils.celery.Progress so the task runs without a Celery result backend."""

    def __init__(self, *args, **kwargs):
        self.done = self.total = 0

    def did(self, delta):
        self.done += delta

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@override_settings(ASSIGNMENT_HARVEST_ENABLED=True)
class HarvestBeforeAutoGradeTestCase(HarvestTestMixin, TestCase):
    """With ``ai_grading_auto_delay_minutes = 0`` auto-grade lands on the deadline itself.

    That is the same instant auto-submit is scheduled for, and the re-grade works off the latest
    submission per problem — so a paper collected after that snapshot would never be scored. The
    task collects first, and these tests pin that order down.
    """

    def setUp(self):
        super().setUp()
        Contest.objects.filter(pk=self.contest.pk).update(ai_grading_auto_delay_minutes=0)

    def _hand_in_mcq(self):
        submission = Submission.objects.create(
            user=self.student.profile, problem=self.mcq, language=Language.get_answer_language(),
            contest_object=self.contest, status='D', result='WA',
        )
        ContestSubmission.objects.create(
            submission=submission, problem=self.mcq_problem, participation=self.participation,
        )
        return submission

    def test_draft_is_collected_before_the_regrade_picks_its_targets(self):
        self._mcq_draft()

        with mock.patch('judge.tasks.contest.Progress', _NoProgress), \
                mock.patch.object(Submission, 'judge', autospec=True) as mock_judge:
            auto_grade_contest(self.contest.key)

        submission = self._submissions().get(problem=self.mcq)
        # Collecting grades on submit (no ``rejudge``), the auto-grade pass re-grades. Both calls
        # landing on the same submission, in that order, is what "collected first" means here.
        self.assertEqual(
            [(call.args[0].id, bool(call.kwargs.get('rejudge'))) for call in mock_judge.call_args_list],
            [(submission.id, False), (submission.id, True)],
        )

    def test_answer_never_submitted_by_hand_still_gets_scored(self):
        self._mcq_draft()

        with mock.patch('judge.tasks.contest.Progress', _NoProgress):
            self.assertEqual(auto_grade_contest(self.contest.key), 1)

        submission = self._submissions().get(problem=self.mcq)
        self.assertEqual(submission.result, 'AC')
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.score, 50)

    def test_nothing_to_collect_leaves_the_regrade_unchanged(self):
        submission = self._hand_in_mcq()

        with mock.patch('judge.tasks.contest.Progress', _NoProgress), \
                mock.patch.object(Submission, 'judge', autospec=True) as mock_judge:
            auto_grade_contest(self.contest.key)

        self.assertEqual([call.args[0].id for call in mock_judge.call_args_list], [submission.id])

    def test_a_failed_collection_does_not_cost_the_class_its_regrade(self):
        submission = self._hand_in_mcq()

        with mock.patch('judge.tasks.contest.Progress', _NoProgress), \
                mock.patch('judge.utils.assignment_harvest.harvest_participation',
                           side_effect=RuntimeError('boom')), \
                mock.patch.object(Submission, 'judge', autospec=True) as mock_judge:
            with self.assertLogs('judge.assignment_harvest', level='ERROR'):
                auto_grade_contest(self.contest.key)

        self.assertEqual([call.args[0].id for call in mock_judge.call_args_list], [submission.id])


class HarvestScoringTestMixin:
    """One student who answered correctly, submitted, then edited their answer into a wrong one.

    That is the shape auto-submit changes the outcome of, so it is the one worth scoring: the
    collected answer is worse than what the student already had on the record.
    """
    fixtures = ['language_all.json']

    format_name = 'default'

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.student = create_user(username='harvestscore_%s' % cls.format_name)
        cls.mcq = create_problem('hscore-m-%s' % cls.format_name, submission_type='M',
                                 is_public=False, points=100)
        cls.right = ProblemMCQOption.objects.create(problem=cls.mcq, order=1, text='right', is_correct=True)
        cls.wrong = ProblemMCQOption.objects.create(problem=cls.mcq, order=2, text='wrong', is_correct=False)
        cls.contest = create_contest(
            key='hscore%s' % cls.format_name, name='Harvest scoring %s' % cls.format_name,
            format_name=cls.format_name, start_time=now - timedelta(hours=2),
            end_time=now + timedelta(hours=1), is_visible=True,
        )
        cls.contest_problem = ContestProblem.objects.create(
            contest=cls.contest, problem=cls.mcq, points=100, order=1,
        )

    def setUp(self):
        self.participation = ContestParticipation.objects.create(
            contest=self.contest, user=self.student.profile,
        )

    def _submit_by_hand(self, option):
        """The submit the student made themselves, through the same path the view uses.

        Backdated because "latest" is decided on the timestamp, and two submissions made in the
        same test are otherwise separated by microseconds — enough to pass, not enough to trust.
        """
        submission = create_submission(
            self.student.profile, self.mcq, language=Language.get_answer_language(),
            answer={'selected': [option.id]},
            participation=self.participation, contest_problem=self.contest_problem,
        )
        Submission.objects.filter(pk=submission.pk).update(date=timezone.now() - timedelta(minutes=10))
        return submission

    def _draft(self, option):
        return AssignmentDraft.objects.create(
            participation=self.participation, problem=self.mcq,
            content=json.dumps({'selected': [option.id]}),
        )

    def _score(self):
        self.participation = ContestParticipation.objects.get(pk=self.participation.pk)
        self.participation.recompute_results()
        self.participation.refresh_from_db()
        return self.participation.score


class HarvestDefaultFormatScoringTestCase(HarvestScoringTestMixin, TestCase):
    """Default format scores the best submission, so collection can only ever help."""

    def test_collecting_a_worse_answer_does_not_cost_the_student_their_score(self):
        self._submit_by_hand(self.right)
        self.assertEqual(self._score(), 100)
        self._draft(self.wrong)

        collected = harvest_participation(self.participation)

        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0].result, 'WA')
        # Both submissions are on the record; the best of them is what counts.
        self.assertEqual(self._score(), 100)

    def test_collecting_an_answer_never_submitted_scores_it(self):
        self._draft(self.right)

        harvest_participation(self.participation)

        self.assertEqual(self._score(), 100)


class HarvestExamFormatScoringTestCase(HarvestScoringTestMixin, TestCase):
    """Exam format scores the latest submission for question types that work that way, so the
    collected answer *is* the answer — including when it is worse than one already submitted.

    This is the trade-off the submit-area copy warns about, and it is worth a test of its own:
    silently reverting to the better attempt would be a friendlier outcome but a different exam.
    """

    format_name = 'exam'

    def _finalize(self):
        # Exam results stay out of the total until the assignment is finalized.
        Contest.objects.filter(pk=self.contest.pk).update(results_finalized=True)

    def test_the_collected_answer_is_the_one_that_counts(self):
        self._submit_by_hand(self.right)
        self._draft(self.wrong)

        harvest_participation(self.participation)
        self._finalize()

        self.assertEqual(self._score(), 0)

    def test_a_better_collected_answer_also_counts(self):
        # Latest means latest in both directions, not "worst".
        self._submit_by_hand(self.wrong)
        self._draft(self.right)

        harvest_participation(self.participation)
        self._finalize()

        self.assertEqual(self._score(), 100)

    def test_without_a_draft_the_hand_submitted_answer_stands(self):
        self._submit_by_hand(self.right)

        self.assertEqual(harvest_participation(self.participation), [])
        self._finalize()

        self.assertEqual(self._score(), 100)
