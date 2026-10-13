"""Regression tests for the contest grade/editor fixes of this change set.

Covers four contracts that were broken or unguarded:

1. **Editing a contest must not erase submission history.** ``_sync_problems``
   used to delete-and-recreate every ``ContestProblem`` row; because
   ``ContestSubmission.problem`` is a FK with ``on_delete=CASCADE``, that
   cascade-deleted every ``ContestSubmission`` and the ``contest_submission_delete``
   signal then detached each ``Submission.contest_object`` — so after ANY edit
   (including a pure duration change) all prior submissions silently vanished
   from the grade page / workspace / exports. Rows are now upserted in place.
2. **Grade page totals use the assignment-specified points.** The student
   workspace shows ``ContestSubmission.points / ContestProblem.points``; the
   grade page used ``case_total / problem.points`` (the problem's own scale) and
   disagreed with the student view whenever the assignment re-weights a problem.
3. **Non-code answers render human-readably on the grade page.** MCQ /
   fill-blank / short-answer submissions now ship an ``answer_display`` list
   (same rows as the student workspace) instead of raw JSON source.
4. **Pending queue semantics.** ``is_pending`` means "awaiting teacher review":
   a manually-graded type (file upload) with no score yet. The old
   "require teacher review after AI scoring" per-problem flag is retired — the
   Grade desk replaced that queue — so an AI-scored answer is not pending, and
   neither is a code submission still sitting in the judge queue.
"""

import json
from datetime import timedelta

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    ContestParticipation,
    ContestProblem,
    ContestSubmission,
    Language,
    ProblemFillBlankConfig,
    ProblemShortAnswerConfig,
    Submission,
    SubmissionManualGrade,
    SubmissionSource,
)
from judge.models.tests.util import create_problem, create_user
from judge.views.tests.test_contest_grade import GradeTestBase


class ContestEditPreservesSubmissionsTestCase(GradeTestBase):
    """Editing a contest (e.g. adding duration) must not erase submission history.

    Regression for: after increasing the duration and saving, every previous
    submission disappeared from the assignment. Root cause: _sync_problems did
    delete-and-recreate on ContestProblem, which cascade-deleted ContestSubmission
    and detached Submission.contest_object.
    """

    def _edit(self, **overrides):
        """POST a full edit payload (with the problem list) to the editor."""
        editor = create_user(
            username='sync_editor',
            user_permissions=('edit_own_contest', 'edit_all_contest'),
        )
        self.contest.authors.add(editor.profile)
        now = timezone.now()
        payload = {
            'name': self.contest.name, 'key': self.contest.key,
            'start_time': (now - timedelta(days=1)).isoformat(),
            'end_time': (now + timedelta(days=2)).isoformat(),
            'duration_hours': 0, 'duration_minutes': 0,
            'is_visible': False, 'is_rated': False, 'use_clarifications': True,
            'hide_problem_tags': False, 'hide_problem_authors': False,
            'allow_virtual_participation': True, 'run_pretests_only': False,
            'rate_all': False, 'tester_see_submissions': False, 'tester_see_scoreboard': False,
            'show_short_display': False, 'is_private': False, 'is_organization_private': False,
            'limit_join_organizations': False,
            'format_name': 'default', 'scoreboard_visibility': 'V', 'points_precision': 3,
            'authors': [editor.profile.id], 'curators': [], 'testers': [], 'spectators': [],
            'organizations': [], 'classes': [], 'join_organizations': [], 'tags': [],
            'private_contestants': [], 'rate_exclude': [], 'view_contest_scoreboard': [],
            'view_contest_submissions': [], 'banned_users': [],
        }
        payload.update(overrides)
        client = Client()
        client.force_login(editor)
        return client.post(
            reverse('contest_edit', args=[self.contest.key]),
            data=json.dumps(payload), content_type='application/json',
        )

    def _make_contest_submission(self, contest_problem=None, points=50.0):
        """Reuse the live ContestSubmission from GradeTestBase."""
        cs = self.contest_submission
        if contest_problem is not None and cs.problem_id != contest_problem.id:
            cs.problem = contest_problem
        cs.points = points
        cs.save()
        return cs

    def test_edit_preserves_contest_submission_links(self):
        """A re-save with the same problem list keeps ContestSubmission rows."""
        self._make_contest_submission()
        self.assertTrue(ContestSubmission.objects.filter(submission=self.submission).exists())

        response = self._edit(problems=[{'id': self.problem.id, 'points': 100}])
        self.assertEqual(response.status_code, 200, response.content)

        # The ContestSubmission link must survive the edit.
        self.assertTrue(
            ContestSubmission.objects.filter(submission=self.submission).exists(),
            'ContestSubmission was cascade-deleted by the contest edit',
        )
        # ... and the submission must still be associated with the contest.
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.contest_object_id, self.contest.id)
        self.assertEqual(self.submission.contest_or_none.points, 50.0)

    def test_edit_with_duration_change_preserves_submissions(self):
        """The exact user-reported scenario: change duration, keep submissions."""
        # Give the contest an initial per-student duration so the edit is a
        # genuine 1h → 2h extension.
        from datetime import timedelta as td
        self.contest.time_limit = td(hours=1)
        self.contest.save(update_fields=['time_limit'])
        self._make_contest_submission()
        response = self._edit(
            problems=[{'id': self.problem.id, 'points': 100}],
            duration_hours=2, duration_minutes=0,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.time_limit, td(hours=2))
        self.assertTrue(ContestSubmission.objects.filter(submission=self.submission).exists())

    def test_problem_points_update_keeps_same_contest_problem_pk(self):
        """Updating per-problem points edits the row in place (PK stable)."""
        cp = ContestProblem.objects.get(contest=self.contest, problem=self.problem)
        self._make_contest_submission(contest_problem=cp)
        old_pk = cp.pk

        response = self._edit(problems=[{'id': self.problem.id, 'points': 250}])
        self.assertEqual(response.status_code, 200, response.content)

        cp.refresh_from_db()
        self.assertEqual(cp.pk, old_pk, 'ContestProblem was recreated instead of updated')
        self.assertEqual(cp.points, 250)
        self.assertTrue(ContestSubmission.objects.filter(problem=cp).exists())

    def test_removing_problem_detaches_but_keeps_submission_row(self):
        """Removing a problem from the contest is the only allowed deletion:
        its ContestSubmission goes, and the Submission row survives unlinked."""
        self._make_contest_submission()
        response = self._edit(problems=[])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(ContestSubmission.objects.filter(submission=self.submission).exists())
        self.assertTrue(Submission.objects.filter(id=self.submission.id).exists())


class ContestGradeTotalUsesAssignmentPointsTestCase(GradeTestBase):
    """The grade page must show the same points/total the student sees.

    Student workspace: ContestSubmission.points / ContestProblem.points.
    Old grade page:   Submission.points  / (case_total or problem.points).
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Re-weight the assignment: the problem is worth 100 pts on its own
        # scale, but this assignment gives it only 25.
        ContestProblem.objects.filter(contest=cls.contest, problem=cls.problem).update(points=25)
        cls.contest_problem.refresh_from_db()
        cls.contest_submission.points = 12.5
        cls.contest_submission.save(update_fields=['points'])

    def _grade_payload(self):
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        return json.loads(html.split('var submissions = ')[1].split(';\n')[0])

    def test_total_uses_assignment_points(self):
        payload = self._grade_payload()
        row = next(s for s in payload if s['id'] == self.submission.id)
        self.assertEqual(row['total'], 25)      # ContestProblem.points, not 100
        self.assertEqual(row['points'], 12.5)   # ContestSubmission.points, not 50

    def test_standalone_submission_uses_problem_points_not_case_total(self):
        """No ContestSubmission: Grade total is problem.points, never case_total."""
        lone = Submission.objects.create(
            user=self.student.profile, problem=self.problem,
            language=Language.objects.get(key='PY3'), result='AC', status='D',
            contest_object=self.contest, points=1.0, case_total=100,
        )
        payload = self._grade_payload()
        row = next(s for s in payload if s['id'] == lone.id)
        self.assertEqual(row['total'], self.problem.points)  # 1, not case_total 100
        self.assertEqual(row['points'], 1.0)

    def test_save_endpoint_clamps_to_assignment_total(self):
        response = self._client(self.curator).post(
            self.save_url, {'submission': self.submission.id, 'score': '9999'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['score'], 25.0)  # clamped to assignment total


class ContestGradeAnswerDisplayTestCase(GradeTestBase):
    """MCQ / fill-blank / short-answer submissions ship readable answer rows."""

    def test_mcq_answer_display(self):
        from judge.models import ProblemMCQOption
        mcq = create_problem('grade-mcq', submission_type='M')
        ProblemMCQOption.objects.create(problem=mcq, order=0, text='Alpha', is_correct=False)
        right = ProblemMCQOption.objects.create(problem=mcq, order=1, text='Beta', is_correct=True)
        ContestProblem.objects.create(contest=self.contest, problem=mcq, points=10, order=2)
        sub = Submission.objects.create(
            user=self.student.profile, problem=mcq,
            language=Language.objects.get(key='PY3'), result='AC', status='D',
            contest_object=self.contest, points=10.0, case_total=1,
        )
        SubmissionSource.objects.create(
            submission=sub, source=json.dumps({'selected': [right.id],
                                               'choices': [{'id': right.id, 'order': 1, 'text': 'Beta'}]}))

        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        payload = json.loads(html.split('var submissions = ')[1].split(';\n')[0])
        row = next(s for s in payload if s['id'] == sub.id)
        self.assertTrue(row['answer_display'])
        self.assertEqual(row['answer_display'][0]['value'], 'B. Beta')
        # Raw JSON must not be the only thing shown for these types.
        self.assertNotIn('"selected"', row['answer_display'][0]['value'])

    def test_fill_blank_answer_display(self):
        from judge.models import ProblemBlank
        fb = create_problem('grade-fillblank', submission_type='B')
        ProblemBlank.objects.create(problem=fb, order=0, answers='Paris\nparis', points=1)
        ContestProblem.objects.create(contest=self.contest, problem=fb, points=5, order=3)
        sub = Submission.objects.create(
            user=self.student.profile, problem=fb,
            language=Language.objects.get(key='PY3'), result='AC', status='D',
            contest_object=self.contest, points=5.0, case_total=1,
        )
        SubmissionSource.objects.create(submission=sub, source=json.dumps({'answers': ['Paris']}))

        response = self._client(self.curator).get(self.grade_url)
        payload = json.loads(response.content.decode('utf-8').split('var submissions = ')[1].split(';\n')[0])
        row = next(s for s in payload if s['id'] == sub.id)
        self.assertEqual(row['answer_display'][0]['value'], 'Paris')

    def test_short_answer_display_includes_rendered_table_html(self):
        short = create_problem('grade-short', submission_type='S')
        ContestProblem.objects.create(contest=self.contest, problem=short, points=5, order=4)
        md = '| A | B |\n| --- | --- |\n| 1 | 2 |'
        sub = Submission.objects.create(
            user=self.student.profile, problem=short,
            language=Language.get_answer_language(), result='AC', status='D',
            contest_object=self.contest, points=5.0, case_total=1,
        )
        SubmissionSource.objects.create(submission=sub, source=json.dumps({'text': md}))
        response = self._client(self.curator).get(self.grade_url)
        payload = json.loads(response.content.decode('utf-8').split('var submissions = ')[1].split(';\n')[0])
        row = next(s for s in payload if s['id'] == sub.id)
        self.assertEqual(row['answer_display'][0]['value'], md)
        self.assertIn('<table', row['answer_display'][0]['html'])

    def test_description_is_rendered_html(self):
        from judge.models import Problem
        Problem.objects.filter(id=self.problem.id).update(
            description='## Heading\n\nSome **bold** text')
        self.problem.refresh_from_db()
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        payload = json.loads(html.split('var problems = ')[1].split(';\n')[0])
        meta = next(p for p in payload if p['code'] == self.problem.code)
        self.assertIn('<h4>', meta['description_html'])
        self.assertIn('<strong>', meta['description_html'])
        self.assertNotIn('## Heading', meta['description_html'])


class ContestGradePendingSemanticsTestCase(GradeTestBase):
    """is_pending must mean "awaiting teacher review", not "missing a score"."""

    def _payload_row(self, sub):
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        payload = json.loads(html.split('var submissions = ')[1].split(';\n')[0])
        return next(s for s in payload if s['id'] == sub.id)

    def test_code_submission_with_points_is_not_pending(self):
        row = self._payload_row(self.submission)
        self.assertEqual(row['points'], 50.0)
        self.assertFalse(row['is_pending'])

    def test_ai_graded_fill_blank_with_a_score_is_not_pending(self):
        """``requires_manual_review`` is retired: an AI-scored answer resolves itself."""
        from judge.models import ProblemBlank
        fb = create_problem('grade-pending-fb', submission_type='B')
        ProblemBlank.objects.create(problem=fb, order=0, answers='x', points=1)
        ContestProblem.objects.create(contest=self.contest, problem=fb, points=5, order=4)
        ProblemFillBlankConfig.objects.create(problem=fb, grading_mode='ai')
        sub = Submission.objects.create(
            user=self.student.profile, problem=fb,
            language=Language.objects.get(key='PY3'), result='AC', status='D',
            contest_object=self.contest, points=5.0, case_total=1,
        )
        SubmissionSource.objects.create(submission=sub, source=json.dumps({'answers': ['x']}))
        self.assertFalse(self._payload_row(sub)['is_pending'])

    def test_requires_manual_review_field_is_gone(self):
        self.assertNotIn(
            'requires_manual_review',
            [f.name for f in ProblemFillBlankConfig._meta.get_fields()])
        self.assertNotIn(
            'requires_manual_review',
            [f.name for f in ProblemShortAnswerConfig._meta.get_fields()])

    def test_queued_code_submission_is_not_pending(self):
        """A code submission waiting on the judge resolves itself — not pending."""
        queued = Submission.objects.create(
            user=self.student.profile, problem=self.problem,
            language=Language.objects.get(key='PY3'), result=None, status='QU',
            contest_object=self.contest, points=None, case_total=0,
        )
        row = self._payload_row(queued)
        self.assertIsNone(row['points'])
        self.assertFalse(row['is_pending'])

    def test_virtual_submission_flagged(self):
        """V badge data: virtual participations are still marked."""
        virtual_part = ContestParticipation.objects.create(
            contest=self.contest, user=self.student.profile, virtual=1, real_start=timezone.now(),
        )
        sub = Submission.objects.create(
            user=self.student.profile, problem=self.problem,
            language=Language.objects.get(key='PY3'), result='WA', status='D',
            contest_object=self.contest, points=10.0, case_total=100,
        )
        ContestSubmission.objects.create(
            submission=sub, problem=ContestProblem.objects.get(contest=self.contest),
            participation=virtual_part, points=10.0,
        )
        self.assertTrue(self._payload_row(sub)['virtual'])


class RequireManualReviewRetiredTestCase(GradeTestBase):
    """The review-required flag is retired: the editor neither writes nor prefills it."""

    def test_create_page_ignores_ai_review_payload(self):
        from judge.models import Problem
        from judge.views.problem_create_v2 import ProblemCreateV2View

        editor = create_user(username='pmr_editor', user_permissions=('create_private_problem',))
        client = Client()
        client.force_login(editor)
        payload = {
            'type': 'blank', 'name': 'Review me', 'description': 'Q {{blank}}',
            'points': 4, 'blanks': ['ans'], 'problemTypes': ['Graph Theory'],
            'ai': True, 'aiReview': True, 'rubric': 'any',
        }
        response = client.post(reverse('problem_create_v2'), data=json.dumps(payload),
                               content_type='application/json')
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertTrue(ProblemFillBlankConfig.objects.filter(problem=problem).exists())
        self.assertNotIn('aiReview', ProblemCreateV2View()._build_edit_data(problem))

    def test_short_answer_create_ignores_ai_review_payload(self):
        from judge.models import Problem
        from judge.views.problem_create_v2 import ProblemCreateV2View

        editor = create_user(username='pmr_editor2', user_permissions=('create_private_problem',))
        client = Client()
        client.force_login(editor)
        payload = {
            'type': 'short', 'name': 'Review short', 'description': 'Explain X.',
            'points': 4, 'shortAnswer': 'the answer', 'aiReview': True,
            'problemTypes': ['Graph Theory'],
        }
        response = client.post(reverse('problem_create_v2'), data=json.dumps(payload),
                               content_type='application/json')
        self.assertEqual(response.status_code, 200)
        problem = Problem.objects.get(code=response.json()['code'])
        self.assertTrue(ProblemShortAnswerConfig.objects.filter(problem=problem).exists())
        self.assertNotIn('aiReview', ProblemCreateV2View()._build_edit_data(problem))


class GradePageFilterDefaultsTestCase(GradeTestBase):
    """Queue defaults to 'final version'; Participation defaults to 'Live'."""

    def test_queue_defaults_to_final(self):
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        # The final-version option is the selected default.
        self.assertIn('<option value="final" selected>', html)

    def test_participation_defaults_to_live(self):
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('<option value="live" selected>', html)


class ManualGradePriorityTestCase(GradeTestBase):
    """Manual grade override: separate table, actual score prefers it, and the
    student-visible score only switches to it after release."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cp = cls.contest_problem

    def test_save_keeps_machine_points_and_stores_manual_grade(self):
        response = self._client(self.curator).post(
            self.save_url, {'submission': self.submission.id, 'score': '88'})
        self.assertEqual(response.status_code, 200)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.points, 50.0)  # machine untouched
        grade = SubmissionManualGrade.objects.get(submission=self.submission)
        self.assertEqual(grade.points, 88.0)
        self.assertFalse(grade.released)
        self.assertEqual(grade.machine_points, 50.0)
        # Until released, the contest score (ranking) stays on the machine score.
        self.contest_submission.refresh_from_db()
        self.assertEqual(self.contest_submission.points, 50.0)

    def test_release_propagates_manual_grade_to_contest_score(self):
        self._client(self.curator).post(
            self.save_url, {'submission': self.submission.id, 'score': '88'})
        self.contest_submission.refresh_from_db()
        self.assertEqual(self.contest_submission.points, 50.0)  # machine until release

        self._client(self.curator).post(self.release_url)
        self.contest_submission.refresh_from_db()
        self.assertEqual(self.contest_submission.points, 88.0)  # released manual wins

    def test_release_all_reveals_manual_grade(self):
        self._client(self.curator).post(
            self.save_url, {'submission': self.submission.id, 'score': '88'})
        grade = SubmissionManualGrade.objects.get(submission=self.submission)
        self.assertFalse(grade.released)
        response = self._client(self.curator).post(self.release_url)
        self.assertEqual(response.status_code, 200)
        grade.refresh_from_db()
        self.assertTrue(grade.released)

    def test_workspace_shows_machine_until_released(self):
        from judge.views.problem import workspace_display_points
        self._client(self.curator).post(
            self.save_url, {'submission': self.submission.id, 'score': '88'})

        sub = Submission.objects.get(id=self.submission.id)
        # Not released: the student still sees the machine score (50), even
        # when they are no longer in the contest (no request.in_contest).
        self.assertEqual(workspace_display_points(sub), 50.0)

        SubmissionManualGrade.objects.filter(submission=sub).update(released=True)
        sub = Submission.objects.get(id=self.submission.id)
        # Released: the student now sees the manual override (88).
        self.assertEqual(workspace_display_points(sub), 88.0)

    def test_workspace_display_ignores_in_contest_flag(self):
        """Student-visible score follows ContestSubmission, not request.in_contest."""
        from judge.views.problem import workspace_display_points, workspace_display_points_total
        self._client(self.curator).post(
            self.save_url, {'submission': self.submission.id, 'score': '88'})
        sub = Submission.objects.get(id=self.submission.id)
        self.assertEqual(workspace_display_points(sub), 50.0)
        self.assertEqual(workspace_display_points_total(sub), 100)


def _row_html(content, submission_id):
    marker = ('id="%d"' % submission_id).encode()
    start = content.find(marker)
    if start < 0:
        return b''
    end = content.find(b'class="submission-row"', start + 1)
    return content[start:end if end != -1 else None]


def _score_html(content, submission_id):
    row = _row_html(content, submission_id)
    start = row.find(b'class="score"')
    if start < 0:
        return b''
    start = row.find(b'>', start) + 1
    end = row.find(b'</div>', start)
    return row[start:end].strip()


class StudentVisibleScoreTestCase(GradeTestBase):
    """Unified student-visible score: list, status, workspace; Release gate; assignment scale."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.problem.is_public = True
        cls.problem.points = 100
        cls.problem.save(update_fields=['is_public', 'points'])
        cls.contest.is_visible = True
        cls.contest.save(update_fields=['is_visible'])
        cls.submission.case_points = 50
        cls.submission.case_total = 100
        cls.submission.points = 50
        cls.submission.time = 0.1
        cls.submission.memory = 1024
        cls.submission.save(update_fields=['case_points', 'case_total', 'points', 'time', 'memory'])

        cls.practice_problem = create_problem(
            'grade-prac-score', is_public=True, points=100,
        )
        lang = Language.objects.get(key='PY3')
        cls.practice_sub = Submission.objects.create(
            user=cls.student.profile, problem=cls.practice_problem,
            language=lang, result='AC', status='D', points=50.0,
            case_points=50, case_total=100, time=0.1, memory=1024,
        )
        SubmissionSource.objects.create(submission=cls.practice_sub, source='print(1)')
        cls.practice_mcq_sub = Submission.objects.create(
            user=cls.student.profile, problem=cls.practice_problem,
            language=lang, result='AC', status='D', points=100.0,
            case_points=1, case_total=1, time=0, memory=0,
        )
        SubmissionSource.objects.create(submission=cls.practice_mcq_sub, source='print(2)')
        cls.practice_grade_url = reverse('practice_grade')
        cls.practice_save_url = reverse('practice_grade_save')
        cls.practice_release_url = reverse('practice_grade_release')
        cls.practice_status_url = reverse('practice_grade_status')

    def _list_as(self, user, submission):
        response = self._client(user).get(
            reverse('all_user_submissions', kwargs={'user': self.student.username}))
        self.assertEqual(response.status_code, 200)
        return _score_html(response.content, submission.id)

    def _status_as(self, user, submission):
        return self._client(user).get(reverse('submission_status', args=[submission.id]))

    def _workspace_as(self, user, submission):
        return self._client(user).get(
            reverse('problem_detail', args=[submission.problem.code])
            + '?submission=%d' % submission.id,
        )

    def _practice_grade_payload(self):
        response = self._client(self.superuser).get(self.practice_grade_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        return json.loads(html.split('var submissions = ')[1].split(';\n')[0])

    def test_practice_grade_payload_uses_problem_points_not_case_total(self):
        row = next(s for s in self._practice_grade_payload()
                   if s['id'] == self.practice_mcq_sub.id)
        self.assertEqual(row['total'], 100)
        self.assertNotEqual(row['total'], self.practice_mcq_sub.case_total)

    def test_practice_save_does_not_clamp_to_case_total(self):
        save = self._client(self.superuser).post(
            self.practice_save_url, {'submission': self.practice_mcq_sub.id, 'score': '88'})
        self.assertEqual(save.status_code, 200, save.content)
        body = save.json()
        self.assertEqual(body['score'], 88.0)
        self.assertEqual(body['total'], 100)
        self.assertEqual(
            SubmissionManualGrade.objects.get(submission=self.practice_mcq_sub).points, 88.0)

    def test_practice_grade_status_uses_problem_points(self):
        response = self._client(self.superuser).get(
            self.practice_status_url, {'submission': self.practice_mcq_sub.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['total'], 100)

    def test_practice_list_uses_problem_points_not_case_total(self):
        score = self._list_as(self.student, self.practice_mcq_sub)
        self.assertIn(b'100 / 100', score)
        self.assertNotIn(b'1 / 1', score)

    def test_practice_save_without_release_keeps_machine_on_list_and_workspace(self):
        save = self._client(self.superuser).post(
            self.practice_save_url, {'submission': self.practice_sub.id, 'score': '88'})
        self.assertEqual(save.status_code, 200, save.content)
        score = self._list_as(self.student, self.practice_sub)
        self.assertIn(b'50 / 100', score)
        self.assertNotIn(b'88 / 100', score)

        workspace = self._workspace_as(self.student, self.practice_sub)
        self.assertEqual(workspace.status_code, 200)
        self.assertEqual(workspace.context['workspace_submission']['points'], 50.0)
        self.assertEqual(workspace.context['workspace_submission']['points_total'], 100)
        self.assertFalse(workspace.context['workspace_submission']['released'])
        row = next(s for s in workspace.context['workspace_submissions']
                   if s.id == self.practice_sub.id)
        self.assertEqual(row.display_points, 50.0)
        self.assertEqual(row.display_points_total, 100)
        self.assertFalse(row.display_released)

    def test_practice_release_shows_manual_on_workspace_and_list(self):
        self._client(self.superuser).post(
            self.practice_save_url, {'submission': self.practice_sub.id, 'score': '88'})
        release = self._client(self.superuser).post(self.practice_release_url)
        self.assertEqual(release.status_code, 200, release.content)

        score = self._list_as(self.student, self.practice_sub)
        self.assertIn(b'88 / 100', score)

        workspace = self._workspace_as(self.student, self.practice_sub)
        self.assertEqual(workspace.context['workspace_submission']['points'], 88.0)
        self.assertEqual(workspace.context['workspace_submission']['points_total'], 100)
        self.assertTrue(workspace.context['workspace_submission']['released'])
        row = next(s for s in workspace.context['workspace_submissions']
                   if s.id == self.practice_sub.id)
        self.assertEqual(row.display_points, 88.0)
        self.assertTrue(row.display_released)
        self.assertContains(workspace, 'ws-released-mini')

    def test_released_null_manual_grade_is_not_a_student_release(self):
        """Release All may flag an empty record, but there is no score to publish."""
        from judge.utils.contest_grade import attach_student_display_score

        SubmissionManualGrade.objects.create(
            submission=self.submission, points=None, machine_points=50, released=True,
        )
        sub = Submission.objects.get(id=self.submission.id)
        attach_student_display_score(sub)
        self.assertEqual(sub.display_points, 50)
        self.assertFalse(sub.display_released)

    def test_left_assignment_still_uses_assignment_scale_and_release_gate(self):
        """Not in_contest / already left: still ContestProblem.points + Release."""
        from judge.views.problem import workspace_display_points, workspace_display_points_total
        self.student.profile.current_contest = None
        self.student.profile.save()

        self._client(self.curator).post(
            self.save_url, {'submission': self.submission.id, 'score': '88'})
        sub = Submission.objects.get(id=self.submission.id)
        self.assertEqual(workspace_display_points(sub), 50.0)
        self.assertEqual(workspace_display_points_total(sub), 100)

        workspace = self._workspace_as(self.student, self.submission)
        self.assertEqual(workspace.status_code, 200)
        self.assertEqual(workspace.context['workspace_submission']['points'], 50.0)
        self.assertEqual(workspace.context['workspace_submission']['points_total'], 100)

        SubmissionManualGrade.objects.filter(submission=sub).update(released=True)
        sub = Submission.objects.get(id=self.submission.id)
        self.assertEqual(workspace_display_points(sub), 88.0)

    def test_save_without_release_status_and_list_hide_manual(self):
        self._client(self.curator).post(
            self.save_url, {'submission': self.submission.id, 'score': '88'})

        score = self._list_as(self.student, self.submission)
        self.assertIn(b'50 / 100', score)
        self.assertNotIn(b'88 / 100', score)

        status = self._status_as(self.student, self.submission)
        self.assertEqual(status.status_code, 200)
        self.assertContains(status, '50/100')
        self.assertNotContains(status, '88/100')
        # Parenthetical must not leak ranking ContestSubmission.points (88).
        self.assertNotContains(status, '88/100 points')

    def test_list_reweighted_assignment_uses_contest_problem_not_case_points(self):
        """100-point cases reweighted to 25 on the assignment → 12.5/25, not 50/100."""
        ContestProblem.objects.filter(pk=self.contest_problem.pk).update(points=25)
        self.contest_problem.refresh_from_db()
        # Stale ranking cell (unscaled 50) must not become the student-visible score.
        self.contest_submission.points = 50
        self.contest_submission.save(update_fields=['points'])

        from judge.views.problem import workspace_display_points, workspace_display_points_total
        sub = Submission.objects.get(id=self.submission.id)
        self.assertEqual(workspace_display_points(sub), 12.5)
        self.assertEqual(workspace_display_points_total(sub), 25)

        score = self._list_as(self.student, self.submission)
        self.assertIn(b'12.5 / 25', score)
        self.assertNotIn(b'50 / 100', score)

        status = self._status_as(self.student, self.submission)
        self.assertContains(status, '12.5/25')
        self.assertNotContains(status, '50/100')

    def test_reweighted_save_without_release_keeps_machine_scale(self):
        ContestProblem.objects.filter(pk=self.contest_problem.pk).update(points=25)
        self.contest_problem.refresh_from_db()
        self.contest_submission.points = 12.5
        self.contest_submission.save(update_fields=['points'])

        self._client(self.curator).post(
            self.save_url, {'submission': self.submission.id, 'score': '20'})
        self.contest_submission.refresh_from_db()
        self.assertEqual(self.contest_submission.points, 12.5)

        score = self._list_as(self.student, self.submission)
        self.assertIn(b'12.5 / 25', score)
        self.assertNotIn(b'20 / 25', score)

        status = self._status_as(self.student, self.submission)
        self.assertContains(status, '12.5/25')
        self.assertNotContains(status, '20/25')


class CodeAIRubricStudentScoreTestCase(GradeTestBase):
    """G-10: for code with a supplementary AI rubric, the student-visible score
    equals the ranking (``ContestSubmission.points``), not the test cases alone."""

    def test_student_display_matches_ranking(self):
        from judge.submission_grading import _refresh_contest_score
        from judge.utils.contest_grade import student_display_points
        self.problem.points = 10
        self.problem.partial = True
        self.problem.save(update_fields=['points', 'partial'])
        self.contest_problem.points = 10
        self.contest_problem.partial = True
        self.contest_problem.save(update_fields=['points', 'partial'])
        # Test cases 6/10, AI rubric 9/10 → combined score 7.2 on the problem.
        Submission.objects.filter(id=self.submission.id).update(
            case_points=6, case_total=10, points=7.2, ai_points=9.0, result='WA')
        sub = Submission.objects.get(id=self.submission.id)
        _refresh_contest_score(sub)
        self.contest_submission.refresh_from_db()
        self.assertAlmostEqual(self.contest_submission.points, 7.2)
        sub = Submission.objects.get(id=self.submission.id)
        self.assertAlmostEqual(student_display_points(sub), self.contest_submission.points)
