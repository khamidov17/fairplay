"""Tests for the exam-style contest feature: per-problem result hiding, latest-vs-best scoring
(``ExamContestFormat``), submission masking, and the end-of-contest finalize task."""
from datetime import timedelta
from unittest import mock

from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Class,
    Contest,
    ContestParticipation,
    ContestSubmission,
    Language,
    Submission,
    SubmissionManualGrade,
)
from judge.models.tests.util import (
    CommonDataMixin,
    create_contest,
    create_contest_participation,
    create_contest_problem,
    create_problem,
    create_user,
)
from judge.tasks import finalize_contest_results, unfinalize_contest_results


class ExamContestTestMixin(CommonDataMixin):
    """Builds an exam contest with one code problem (visible/best) and one MCQ problem (hidden/latest)."""

    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.editor = create_user(username='exam_editor')
        self.contest = create_contest(
            key='exam', format_name='exam',
            authors=('exam_editor',),
            problems=(),
        )
        self.code_problem = create_problem(code='examcode', submission_type='C', points=100)
        self.mcq_problem = create_problem(code='exammcq', submission_type='M', points=100)
        self.code_cp = create_contest_problem(contest=self.contest, problem=self.code_problem, points=100, order=1)
        self.mcq_cp = create_contest_problem(contest=self.contest, problem=self.mcq_problem, points=100, order=2)
        self.participation = create_contest_participation(contest=self.contest, user=self.users['normal'].profile)

    def _add_contest_submission(self, contest_problem, *, points, minutes):
        """Create a Submission + ContestSubmission with controlled points and submission time."""
        sub = Submission.objects.create(
            user=self.participation.user, problem=contest_problem.problem,
            language=Language.get_answer_language(), points=points,
            case_points=points, case_total=100, result='AC' if points else 'WA', status='D',
            contest_object=contest_problem.contest,
        )
        # date is auto_now_add; force a deterministic time so "latest" is unambiguous.
        Submission.objects.filter(id=sub.id).update(date=timezone.now() + timedelta(minutes=minutes))
        sub.refresh_from_db()
        ContestSubmission.objects.create(
            submission=sub, problem=contest_problem, participation=self.participation, points=points)
        return sub


class ResultsHiddenResolutionTest(ExamContestTestMixin, TestCase):
    def test_type_default(self):
        # No override: MCQ hidden, code shown (from the question-type policy flags).
        self.assertTrue(self.mcq_cp.results_hidden)
        self.assertFalse(self.code_cp.results_hidden)

    def test_score_uses_latest_default(self):
        self.assertTrue(self.mcq_cp.score_uses_latest)
        self.assertFalse(self.code_cp.score_uses_latest)

    def test_format_scoring_uses_latest_matches_standings(self):
        exam = self.contest.format
        self.assertTrue(exam.scoring_uses_latest(self.mcq_cp))
        self.assertFalse(exam.scoring_uses_latest(self.code_cp))
        default_contest = create_contest(key='exam-default-fmt', authors=('exam_editor',))
        default_fmt = default_contest.format
        self.assertFalse(default_fmt.scoring_uses_latest(self.mcq_cp))
        self.assertFalse(default_fmt.scoring_uses_latest(self.code_cp))

    def test_override_forces_show_and_hide(self):
        self.mcq_cp.hide_results = False
        self.code_cp.hide_results = True
        self.assertFalse(self.mcq_cp.results_hidden)
        self.assertTrue(self.code_cp.results_hidden)


class FormatClassTemplateAccessTest(ExamContestTestMixin, TestCase):
    """Regression: the admin change-form gates the "Finalize results" button on
    ``{% if original.format_class.has_hidden_results %}``. ``format_class`` is a class, and Django's
    template engine auto-calls callables, so without ``do_not_call_in_templates`` it instantiates the
    class, fails (``__init__`` needs args), and falls back to an empty ``string_if_invalid`` — silently
    hiding the button. See judge/contest_format/base.py."""

    TEMPLATE = Template('{% if c.format_class.has_hidden_results %}Y{% else %}N{% endif %}')

    def _render(self, contest):
        return self.TEMPLATE.render(Context({'c': contest}))

    def test_exam_format_class_resolves_in_template(self):
        # Pre-fix this rendered 'N' (format_class collapsed to ''); it must resolve to the class.
        self.assertEqual(self._render(self.contest), 'Y')

    def test_default_format_still_falsy_in_template(self):
        plain = create_contest(key='plain_tpl', format_name='default', problems=())
        self.assertEqual(self._render(plain), 'N')


class ExamScoringTest(ExamContestTestMixin, TestCase):
    def test_latest_for_mcq_best_for_code_excluded_until_finalized(self):
        # Code: best of 50/100 -> 100. MCQ: best 100 (earlier), latest 40 (later).
        self._add_contest_submission(self.code_cp, points=50, minutes=1)
        self._add_contest_submission(self.code_cp, points=100, minutes=2)
        self._add_contest_submission(self.mcq_cp, points=100, minutes=1)
        self._add_contest_submission(self.mcq_cp, points=40, minutes=2)

        # While running (not finalized): MCQ excluded from the total, code counts at its best.
        self.participation.recompute_results()
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.score, 100)
        self.assertTrue((self.participation.format_data or {})[str(self.mcq_cp.id)].get('hidden'))

        # After finalize: MCQ folds in at its LATEST score (40), not its best (100).
        Contest.objects.filter(id=self.contest.id).update(results_finalized=True)
        # Reload so the participation's cached contest/format pick up results_finalized.
        self.participation = ContestParticipation.objects.get(id=self.participation.id)
        self.participation.recompute_results()
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.score, 140)
        self.assertFalse((self.participation.format_data or {})[str(self.mcq_cp.id)].get('hidden'))
        self.assertEqual((self.participation.format_data or {})[str(self.mcq_cp.id)]['points'], 40)


class SubmissionMaskingTest(ExamContestTestMixin, TestCase):
    def _mcq_submission(self):
        return self._add_contest_submission(self.mcq_cp, points=100, minutes=1)

    def test_owner_masked_editor_and_staff_not(self):
        sub = self._mcq_submission()
        self.assertTrue(sub.contest_results_hidden)
        # Owner cannot see their own hidden result.
        self.assertFalse(sub.results_visible_to(self.users['normal']))
        # Contest editor (author) and a view-all-submission staffer can.
        self.assertTrue(sub.results_visible_to(self.editor))
        self.assertTrue(sub.results_visible_to(self.users['superuser']))
        # Anonymous cannot.
        self.assertFalse(sub.results_visible_to(self.users['anonymous']))

    def test_code_problem_not_hidden(self):
        sub = self._add_contest_submission(self.code_cp, points=100, minutes=1)
        self.assertFalse(sub.contest_results_hidden)
        self.assertTrue(sub.results_visible_to(self.users['normal']))

    def test_revealed_after_finalize(self):
        sub = self._mcq_submission()
        Contest.objects.filter(id=self.contest.id).update(results_finalized=True)
        sub = Submission.objects.get(id=sub.id)  # drop cached_property / reload contest_object
        self.assertFalse(sub.contest_results_hidden)
        self.assertTrue(sub.results_visible_to(self.users['normal']))


class ReleasedGradeExamVisibilityTest(ExamContestTestMixin, TestCase):
    """Release changes the eventual score, never the exam's visibility gate."""

    def setUp(self):
        now = timezone.now()
        self.contest.start_time = now - timedelta(hours=1)
        self.contest.end_time = now + timedelta(hours=1)
        self.contest.is_visible = True
        self.contest.results_finalized = False
        self.contest.save(update_fields=['start_time', 'end_time', 'is_visible', 'results_finalized'])
        self.mcq_problem.is_public = True
        self.mcq_problem.save(update_fields=['is_public'])
        self.submission = self._add_contest_submission(self.mcq_cp, points=40, minutes=0)
        self.release_url = reverse('contest_grade_release', args=[self.contest.key])
        self.ranking_url = reverse('contest_ranking', args=[self.contest.key])
        self.submissions_url = reverse(
            'contest_user_submissions',
            args=[self.contest.key, self.users['normal'].username, self.mcq_problem.code],
        )

    def _student_client(self):
        client = self.client_class()
        client.force_login(self.users['normal'])
        return client

    def _release_manual_score(self, points=80):
        SubmissionManualGrade.objects.create(
            submission=self.submission, points=points, machine_points=40,
        )
        client = self.client_class()
        client.force_login(self.editor)
        response = client.post(self.release_url)
        self.assertEqual(response.status_code, 200, response.content)

    def _finalize_and_recompute(self):
        Contest.objects.filter(id=self.contest.id).update(results_finalized=True)
        self.participation = ContestParticipation.objects.get(id=self.participation.id)
        self.participation.recompute_results()
        self.participation.refresh_from_db()

    def test_release_during_exam_stays_hidden_on_ranking_and_submissions(self):
        self._release_manual_score(80)
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.score, 0)
        self.assertTrue(self.participation.format_data[str(self.mcq_cp.id)]['hidden'])

        client = self._student_client()
        ranking = client.get(self.ranking_url)
        self.assertEqual(ranking.status_code, 200)
        self.assertContains(ranking, 'submitted-hidden')
        self.assertNotContains(ranking, '>80<div class="solving-time">', html=False)

        submissions = client.get(self.submissions_url)
        self.assertEqual(submissions.status_code, 200)
        self.assertContains(submissions, 'submission-hidden')
        self.assertContains(submissions, 'Submitted')
        self.assertNotContains(submissions, '80 / 100')

    def test_finalized_exam_shows_released_manual_score_on_both_pages(self):
        self._release_manual_score(80)
        self._finalize_and_recompute()
        self.assertEqual(self.participation.score, 80)

        client = self._student_client()
        ranking = client.get(self.ranking_url)
        self.assertContains(ranking, '>80<div class="solving-time">', html=False)
        self.assertNotContains(ranking, 'submitted-hidden')
        submissions = client.get(self.submissions_url)
        self.assertContains(submissions, '80 / 100')
        self.assertNotContains(submissions, 'submission-hidden')

    def test_finalized_exam_shows_machine_score_when_manual_is_unreleased(self):
        SubmissionManualGrade.objects.create(
            submission=self.submission, points=80, machine_points=40, released=False,
        )
        self.participation.recompute_results()
        self._finalize_and_recompute()
        self.assertEqual(self.participation.score, 40)

        client = self._student_client()
        ranking = client.get(self.ranking_url)
        self.assertContains(ranking, '>40<div class="solving-time">', html=False)
        self.assertNotContains(ranking, '>80<div class="solving-time">', html=False)
        submissions = client.get(self.submissions_url)
        self.assertContains(submissions, '40 / 100')
        self.assertNotContains(submissions, '80 / 100')


class DefaultFormatUnaffectedTest(CommonDataMixin, TestCase):
    """Backward compatibility: a default-format contest hides nothing and scores by best."""

    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.contest = create_contest(key='plain', format_name='default', problems=())
        self.mcq_problem = create_problem(code='plainmcq', submission_type='M', points=100)
        self.cp = create_contest_problem(contest=self.contest, problem=self.mcq_problem, points=100, order=1)
        self.participation = create_contest_participation(contest=self.contest, user=self.users['normal'].profile)

    def _add(self, points, minutes):
        sub = Submission.objects.create(
            user=self.participation.user, problem=self.mcq_problem,
            language=Language.get_answer_language(), points=points,
            case_points=points, case_total=100, result='AC' if points else 'WA', status='D',
            contest_object=self.contest,
        )
        Submission.objects.filter(id=sub.id).update(date=timezone.now() + timedelta(minutes=minutes))
        ContestSubmission.objects.create(
            submission=sub, problem=self.cp, participation=self.participation, points=points)
        return sub

    def test_not_hidden_and_scored_by_best(self):
        first = self._add(points=100, minutes=1)
        self._add(points=40, minutes=2)
        # Even an MCQ submission is not masked outside an exam-format contest.
        self.assertFalse(first.contest_results_hidden)
        self.assertTrue(first.results_visible_to(self.users['normal']))
        # Default format counts the best (100), not the latest (40).
        self.participation.recompute_results()
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.score, 100)


class _NoProgress:
    """Stub for judge.utils.celery.Progress so the task runs without a Celery result backend."""

    def __init__(self, *args, **kwargs):
        self.done = self.total = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FinalizeContestResultsTest(ExamContestTestMixin, TestCase):
    def test_rejudges_only_latest_and_sets_flag(self):
        self._add_contest_submission(self.code_cp, points=50, minutes=1)
        latest_code = self._add_contest_submission(self.code_cp, points=100, minutes=2)
        self._add_contest_submission(self.mcq_cp, points=100, minutes=1)
        latest_mcq = self._add_contest_submission(self.mcq_cp, points=40, minutes=2)

        with mock.patch('judge.tasks.contest.Progress', _NoProgress), \
                mock.patch.object(Submission, 'judge', autospec=True) as mock_judge:
            finalize_contest_results(self.contest.key)

        rejudged_ids = {call.args[0].id for call in mock_judge.call_args_list}
        self.assertEqual(rejudged_ids, {latest_code.id, latest_mcq.id})
        for call in mock_judge.call_args_list:
            self.assertTrue(call.kwargs.get('rejudge'))
            self.assertTrue(call.kwargs.get('force_judge'))

        self.contest.refresh_from_db()
        self.assertTrue(self.contest.results_finalized)
        self.assertFalse(self.contest.run_pretests_only)

    def test_skips_teacher_reviewed_submission(self):
        latest_code = self._add_contest_submission(self.code_cp, points=100, minutes=2)
        latest_mcq = self._add_contest_submission(self.mcq_cp, points=40, minutes=2)
        latest_code.reviewed_by = self.editor.profile
        latest_code.save(update_fields=['reviewed_by'])

        with mock.patch('judge.tasks.contest.Progress', _NoProgress), \
                mock.patch.object(Submission, 'judge', autospec=True) as mock_judge:
            finalize_contest_results(self.contest.key)

        rejudged_ids = {call.args[0].id for call in mock_judge.call_args_list}
        self.assertEqual(rejudged_ids, {latest_mcq.id})
        self.assertNotIn(latest_code.id, rejudged_ids)


class UnfinalizeContestResultsTest(ExamContestTestMixin, TestCase):
    def test_clears_flag_and_rehides_scoreboard(self):
        self._add_contest_submission(self.code_cp, points=100, minutes=2)
        self._add_contest_submission(self.mcq_cp, points=40, minutes=2)

        # Simulate a finalized contest: the MCQ (hidden) is folded into the scoreboard.
        Contest.objects.filter(id=self.contest.id).update(results_finalized=True)
        self.participation = ContestParticipation.objects.get(id=self.participation.id)
        self.participation.recompute_results()
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.score, 140)

        with mock.patch('judge.tasks.contest.Progress', _NoProgress):
            unfinalize_contest_results(self.contest.key)

        self.contest.refresh_from_db()
        self.assertFalse(self.contest.results_finalized)

        # The hidden MCQ drops back out of the scoreboard total; only the code problem counts.
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.score, 100)
        self.assertTrue((self.participation.format_data or {})[str(self.mcq_cp.id)].get('hidden'))


class ContestCloneResetsFinalizeTest(ExamContestTestMixin, TestCase):
    def test_clone_does_not_inherit_finalized(self):
        Contest.objects.filter(id=self.contest.id).update(results_finalized=True)
        self.client.force_login(self.users['superuser'])
        resp = self.client.post(reverse('contest_clone', args=[self.contest.key]), {'key': 'examclone'})
        self.assertRedirects(resp, reverse('contest_edit', args=['examclone']))

        clone = Contest.objects.get(key='examclone')
        self.assertFalse(clone.results_finalized)
        # The source contest is untouched.
        self.contest.refresh_from_db()
        self.assertTrue(self.contest.results_finalized)


class ContestCloneInstructorAccessTest(ExamContestTestMixin, TestCase):
    def test_author_without_clone_contest_cannot_clone(self):
        instructor = create_user(
            username='contest_instructor',
            user_permissions=('create_private_contest',),
        )
        self.contest.authors.add(instructor.profile)
        self.client.force_login(instructor)

        resp = self.client.get(reverse('contest_clone', args=[self.contest.key]))
        self.assertEqual(resp.status_code, 403)

    def test_clone_contest_permission_can_clone(self):
        cloner = create_user(
            username='contest_cloner',
            user_permissions=('clone_contest', 'edit_own_contest', 'edit_all_contest'),
        )
        self.client.force_login(cloner)
        resp = self.client.get(reverse('contest_clone', args=[self.contest.key]))
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(reverse('contest_clone', args=[self.contest.key]), {'key': 'instructorclone'})
        self.assertRedirects(resp, reverse('contest_edit', args=['instructorclone']))
        clone = Contest.objects.get(key='instructorclone')
        self.assertIn(cloner.profile, clone.authors.all())

    def test_non_editor_denied_clone(self):
        self.client.force_login(self.users['normal'])
        resp = self.client.get(reverse('contest_clone', args=[self.contest.key]))
        self.assertEqual(resp.status_code, 403)


class ContestCloneCourseInstructorAccessTest(ExamContestTestMixin, TestCase):
    def test_course_instructor_cannot_clone_without_clone_contest(self):
        org = self.organizations['open']
        course_teacher = create_user(username='course_teacher')
        org.instructors.add(course_teacher.profile)
        self.contest.organizations.set([org])
        Contest.objects.filter(id=self.contest.id).update(is_organization_private=True)
        self.contest.refresh_from_db()
        self.assertNotIn(course_teacher.profile, self.contest.authors.all())

        self.client.force_login(course_teacher)
        resp = self.client.get(reverse('contest_clone', args=[self.contest.key]))
        self.assertEqual(resp.status_code, 403)


class CourseTeacherClassScopedContestAccessTest(ExamContestTestMixin, TestCase):
    def test_course_instructor_sees_class_scoped_contest(self):
        org = self.organizations['open']
        klass = Class.objects.create(
            organization=org, name='Scoped Class', slug='scoped-class', is_active=True,
        )
        course_teacher = create_user(username='scoped_course_teacher')
        org.instructors.add(course_teacher.profile)

        self.contest.organizations.clear()
        self.contest.classes.set([klass])
        Contest.objects.filter(id=self.contest.id).update(is_organization_private=True)
        self.contest.refresh_from_db()

        self.assertTrue(self.contest.is_accessible_by(course_teacher))
        self.assertIn(
            self.contest.key,
            Contest.get_visible_contests(course_teacher).values_list('key', flat=True),
        )

    def test_class_instructor_sees_sibling_class_contest(self):
        org = self.organizations['open']
        own_class = Class.objects.create(
            organization=org, name='Own Class', slug='own-class', is_active=True,
        )
        other_class = Class.objects.create(
            organization=org, name='Other Class', slug='other-class', is_active=True,
        )
        class_teacher = create_user(username='class_teacher')
        own_class.admins.add(class_teacher.profile)

        self.contest.organizations.clear()
        self.contest.classes.set([other_class])
        Contest.objects.filter(id=self.contest.id).update(is_organization_private=True)
        self.contest.refresh_from_db()

        self.assertTrue(self.contest.is_accessible_by(class_teacher))
        self.assertIn(
            self.contest.key,
            Contest.get_visible_contests(class_teacher).values_list('key', flat=True),
        )

        self.client.force_login(class_teacher)
        # Visibility is not clone: clone still requires ``clone_contest``.
        resp = self.client.get(reverse('contest_clone', args=[self.contest.key]))
        self.assertEqual(resp.status_code, 403)


class ContestCloneGeneralCourseAccessTest(ExamContestTestMixin, TestCase):
    def test_site_teacher_cannot_clone_without_clone_contest(self):
        from judge.models.notification import ensure_instructors_group
        from judge.views.organization import grant_site_teacher_role

        ensure_instructors_group()
        site_teacher = create_user(username='general_course_teacher')
        grant_site_teacher_role(site_teacher.profile)

        self.contest.organizations.clear()
        self.contest.classes.clear()
        Contest.objects.filter(id=self.contest.id).update(
            is_visible=True, is_private=False, is_organization_private=False,
        )
        self.contest.refresh_from_db()
        self.assertTrue(self.contest.is_general_course_assignment())
        self.assertNotIn(site_teacher.profile, self.contest.authors.all())

        self.client.force_login(site_teacher)
        resp = self.client.get(reverse('contest_clone', args=[self.contest.key]))
        self.assertEqual(resp.status_code, 403)

    def test_student_cannot_clone_general_course_assignment(self):
        self.contest.organizations.clear()
        self.contest.classes.clear()
        Contest.objects.filter(id=self.contest.id).update(
            is_visible=True, is_private=False, is_organization_private=False,
        )
        self.contest.refresh_from_db()

        self.client.force_login(self.users['normal'])
        resp = self.client.get(reverse('contest_clone', args=[self.contest.key]))
        self.assertEqual(resp.status_code, 403)


class FileUploadExamContestMixin(ExamContestTestMixin):
    """Adds a manually-graded file-upload problem to the exam contest."""

    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.file_problem = create_problem(code='examfile', submission_type='F', points=100)
        self.file_cp = create_contest_problem(
            contest=self.contest, problem=self.file_problem, points=100, order=3)

    def _ungraded_file_submission(self):
        """A file upload awaiting manual grading (QU, no points) — how it sits during the contest."""
        sub = Submission.objects.create(
            user=self.participation.user, problem=self.file_problem,
            language=Language.get_answer_language(), points=None,
            case_points=0, case_total=0, result=None, status='QU',
            contest_object=self.contest,
        )
        ContestSubmission.objects.create(
            submission=sub, problem=self.file_cp, participation=self.participation, points=0)
        return sub


class FileUploadResultsHiddenTest(FileUploadExamContestMixin, TestCase):
    def test_hidden_and_latest_by_default(self):
        # File upload opts into the same exam policy as MCQ (hidden + latest-scored).
        self.assertTrue(self.file_cp.results_hidden)
        self.assertTrue(self.file_cp.score_uses_latest)

    def test_ungraded_submission_masked_from_student_not_editor(self):
        sub = self._ungraded_file_submission()
        self.assertTrue(sub.contest_results_hidden)
        self.assertFalse(sub.results_visible_to(self.users['normal']))
        self.assertTrue(sub.results_visible_to(self.editor))
        self.assertTrue(sub.results_visible_to(self.users['superuser']))

    def test_hidden_until_finalized_then_scores_latest(self):
        # Teacher grades the uploads: an earlier one 100, the latest one 60.
        self._add_contest_submission(self.file_cp, points=100, minutes=1)
        self._add_contest_submission(self.file_cp, points=60, minutes=2)

        # While running: excluded from the visible total and marked hidden — a "submitted" marker on
        # the scoreboard, not a 0.
        self.participation.recompute_results()
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.score, 0)
        self.assertTrue((self.participation.format_data or {})[str(self.file_cp.id)].get('hidden'))

        # After finalize: folds in at the LATEST grade (60), not the best (100).
        Contest.objects.filter(id=self.contest.id).update(results_finalized=True)
        self.participation = ContestParticipation.objects.get(id=self.participation.id)
        self.participation.recompute_results()
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.score, 60)
        self.assertFalse((self.participation.format_data or {})[str(self.file_cp.id)].get('hidden'))


class FileUploadDefaultFormatNotHiddenTest(CommonDataMixin, TestCase):
    """Regression: a file-upload problem in a non-exam contest is not masked (would show its score)."""

    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.contest = create_contest(key='plainfile', format_name='default', problems=())
        self.file_problem = create_problem(code='plainfile_p', submission_type='F', points=100)
        self.cp = create_contest_problem(contest=self.contest, problem=self.file_problem, points=100, order=1)
        self.participation = create_contest_participation(contest=self.contest, user=self.users['normal'].profile)

    def test_not_hidden_outside_exam_format(self):
        sub = Submission.objects.create(
            user=self.participation.user, problem=self.file_problem,
            language=Language.get_answer_language(), points=None,
            case_points=0, case_total=0, result=None, status='QU',
            contest_object=self.contest,
        )
        ContestSubmission.objects.create(
            submission=sub, problem=self.cp, participation=self.participation, points=0)
        self.assertFalse(sub.contest_results_hidden)
        self.assertTrue(sub.results_visible_to(self.users['normal']))
