"""Variant / mutation tests for the contest grading workspace.

Complements ``test_contest_grade.py`` (role matrix, validation, XSS). This file
pins the *edge* and *interaction* behaviors that the role/validation suite does
not touch, in a "variant" (hostile / extreme input) and "mutation" (grant /
revoke / interaction) style:

* **Rejudge endpoint** (``submission_rejudge``) as reached from the grade page:
  id validation, the staff+permission+editor gate, method check, queue
  dispatch, redirect passthrough. This is the endpoint behind the teacher's
  "Rejudge" button — the button is disabled without staff + ``rejudge_submission``.
* **Grade status endpoint** (``contest_grade_status``) the page polls after a
  rejudge: authorization, input validation, and the returned status/points.
* **canRejudge flag** on the grade page (button enablement per role).
* **Save-endpoint variants**: infinity / huge exponents / NaN / booleans /
  hex floats, fractional scores, whitespace ids, negative / zero / huge ids.
* **Release variants**: double-toggle, per-contest isolation, score side effects.
* **Payload variants**: newest-version marker, virtual badge, empty contest.
* **Interactions**: a rejudge's machine scoring overwrites the manual grade;
  and the propagation contract — a manual grade must reach the student's
  *contest* score (``ContestSubmission.points`` + ``recompute_results``).

Tests that assert a *desired* contract the current code does not yet meet are
marked ``expectedFailure`` and cite the gap; the accompanying passing tests
pin the current behavior so the diff is visible when the gap is fixed.
"""

import json
from datetime import timedelta
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import ContestParticipation, ContestProblem, ContestSubmission, Language, Submission, \
    SubmissionManualGrade
from judge.models.tests.util import create_contest, create_contest_participation, create_contest_problem, \
    create_problem, create_user
from judge.views.tests.test_contest_grade import GradeTestBase


class ContestGradeRejudgeEndpointTestCase(GradeTestBase):
    """The rejudge endpoint the grade page's Rejudge button posts to."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Staff with every permission needed to rejudge this problem's submissions.
        cls.staff_editor = create_user(
            username='grade_var_staff', is_staff=True,
            user_permissions=('edit_all_contest', 'edit_own_problem', 'edit_all_problem',
                              'rejudge_submission'),
        )
        # Problem author (is_editable_by True) but NOT staff → must be denied.
        cls.problem_author = create_user(
            username='grade_var_author',
            user_permissions=('edit_own_problem', 'rejudge_submission'),
        )
        cls.problem.authors.add(cls.problem_author.profile)
        # Staff but missing the rejudge_submission permission.
        cls.staff_no_perm = create_user(
            username='grade_var_staff_noperm', is_staff=True,
            user_permissions=('edit_all_contest',),
        )
        cls.rejudge_url = reverse('submission_rejudge')

    def _rejudge(self, user, data):
        return self._client(user).post(self.rejudge_url, data)

    def test_missing_id_is_400(self):
        self.assertEqual(self._rejudge(self.superuser, {}).status_code, 400)

    def test_non_digit_id_is_400(self):
        for bad in ('abc', '-1', '1.5', '1e3', '0x10', ''):
            with self.subTest(bad=bad):
                self.assertEqual(self._rejudge(self.superuser, {'id': bad}).status_code, 400)

    def test_unknown_submission_is_400(self):
        self.assertEqual(self._rejudge(self.superuser, {'id': '99999999'}).status_code, 400)

    def test_get_method_is_405(self):
        self.assertEqual(self._client(self.superuser).get(self.rejudge_url).status_code, 405)

    def test_anonymous_is_redirected_to_login(self):
        response = Client().post(self.rejudge_url, {'id': str(self.submission.id)})
        self.assertEqual(response.status_code, 302)

    def test_curator_cannot_rejudge(self):
        # is_subs_manageable_by requires is_staff — a course teacher (curator)
        # never gets the Rejudge button enabled, and the endpoint refuses too.
        self.assertEqual(
            self._rejudge(self.curator, {'id': str(self.submission.id)}).status_code, 403)

    def test_problem_author_without_staff_cannot_rejudge(self):
        self.assertEqual(
            self._rejudge(self.problem_author, {'id': str(self.submission.id)}).status_code, 403)

    def test_staff_without_rejudge_perm_cannot_rejudge(self):
        self.assertEqual(
            self._rejudge(self.staff_no_perm, {'id': str(self.submission.id)}).status_code, 403)

    def test_superuser_queues_rejudge(self):
        with mock.patch.object(Submission, 'judge') as judge:
            response = self._rejudge(self.superuser, {'id': str(self.submission.id)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'success')
        judge.assert_called_once()
        self.assertTrue(judge.call_args.kwargs['rejudge'])
        self.assertEqual(judge.call_args.kwargs['rejudge_user'], self.superuser)

    def test_staff_editor_queues_rejudge(self):
        with mock.patch.object(Submission, 'judge') as judge:
            response = self._rejudge(self.staff_editor, {'id': str(self.submission.id)})
        self.assertEqual(response.status_code, 200)
        judge.assert_called_once()

    def test_path_param_redirects_after_queueing(self):
        with mock.patch.object(Submission, 'judge'):
            response = self._rejudge(
                self.superuser, {'id': str(self.submission.id), 'path': '/problem/grade-perm-prob'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/problem/grade-perm-prob')

    def test_short_answer_rejudge_runs_ai_not_ir_placeholder(self):
        """Teachers clicking Rejudge on ANS must queue the LLM path (Celery),
        not plain grade_internally which would stamp IR when AI mode is off."""
        from judge.models import ContestProblem, ContestSubmission, Language, ProblemShortAnswerConfig, SubmissionSource
        from judge.models.tests.util import create_contest_participation

        sa = create_problem(code='grade-var-sa', submission_type='S', points=100)
        ProblemShortAnswerConfig.objects.create(
            problem=sa, reference_answer='ok', ai_enabled=True,
        )
        cp = ContestProblem.objects.create(contest=self.contest, problem=sa, points=100, order=9)
        part = create_contest_participation(contest=self.contest, user=self.student.profile)
        sub = Submission.objects.create(
            user=self.student.profile, problem=sa,
            language=Language.get_default_language(),
            result='IR', status='D', contest_object=self.contest,
            points=0, case_points=0, case_total=100,
        )
        SubmissionSource.objects.create(submission=sub, source='{"text":"bad to computer"}')
        ContestSubmission.objects.create(submission=sub, problem=cp, participation=part, points=0)

        with mock.patch('judge.tasks.contest.ai_rejudge_submission.delay') as delay, \
                mock.patch.object(Submission, 'judge') as judge:
            response = self._rejudge(self.superuser, {'id': str(sub.id)})
        self.assertEqual(response.status_code, 200)
        delay.assert_called_once_with(sub.id, self.superuser.id)
        judge.assert_not_called()
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'QU')
        self.assertIsNotNone(sub.rejudged_date)


class ContestGradeCanRejudgeFlagTestCase(GradeTestBase):
    """The grade page's Rejudge button is only enabled for staff + permission."""

    def test_flag_true_for_superuser(self):
        response = self._client(self.superuser).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'var canRejudge = true')

    def test_flag_false_for_curator(self):
        # A course teacher can grade but never rejudge (needs staff) — the
        # button stays disabled; this is why the teacher-side rejudge appears inert.
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'var canRejudge = false')
        self.assertContains(response, 'id="btn-rejudge" disabled')

    def test_flag_false_for_author(self):
        response = self._client(self.author).get(self.grade_url)
        self.assertContains(response, 'var canRejudge = false')


class ContestGradeSaveVariantTestCase(GradeTestBase):
    """Hostile / fractional / extreme score and id variants for the save endpoint.

    Every test posts its own score and asserts on that result, so the tests stay
    order-independent despite sharing the fixture submission.
    """

    def _save(self, data, user=None):
        return self._client(user or self.curator).post(self.save_url, data)

    def test_missing_score_treated_as_zero(self):
        response = self._save({'submission': str(self.submission.id)})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            SubmissionManualGrade.objects.get(submission=self.submission).points, 0.0)

    def test_negative_zero_stays_zero(self):
        response = self._save({'submission': str(self.submission.id), 'score': '-0'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            SubmissionManualGrade.objects.get(submission=self.submission).points, 0.0)

    def test_infinity_rejected(self):
        # Non-finite scores are rejected (400) rather than clamped: they are
        # never a real grade, and NaN cannot be stored at all.
        for value in ('inf', '-inf', '1e309'):
            response = self._save({'submission': str(self.submission.id), 'score': value})
            self.assertEqual(response.status_code, 400, value)

    def test_fractional_score_persisted(self):
        response = self._save({'submission': str(self.submission.id), 'score': '3.14159'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            SubmissionManualGrade.objects.get(submission=self.submission).points, 3.14159)

    def test_boolean_string_rejected(self):
        self.assertEqual(
            self._save({'submission': str(self.submission.id), 'score': 'True'}).status_code, 400)
        self.assertEqual(
            self._save({'submission': str(self.submission.id), 'score': 'False'}).status_code, 400)

    def test_hex_float_string_rejected(self):
        self.assertEqual(
            self._save({'submission': str(self.submission.id), 'score': '0x10'}).status_code, 400)

    def test_truncated_exponent_rejected(self):
        self.assertEqual(
            self._save({'submission': str(self.submission.id), 'score': '1e'}).status_code, 400)

    def test_negative_submission_id_is_404(self):
        self.assertEqual(self._save({'submission': '-5', 'score': '10'}).status_code, 404)

    def test_zero_submission_id_is_404(self):
        self.assertEqual(self._save({'submission': '0', 'score': '10'}).status_code, 404)

    def test_huge_submission_id_is_404(self):
        self.assertEqual(
            self._save({'submission': '999999999999', 'score': '10'}).status_code, 404)

    def test_submission_id_with_whitespace_parses(self):
        response = self._save({'submission': ' %s ' % self.submission.id, 'score': '42'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            SubmissionManualGrade.objects.get(submission=self.submission).points, 42.0)

    def test_nan_score_does_not_crash(self):
        # A hostile 'nan' / 'inf' payload is rejected with a 400, never a 500
        # (float('nan') passes the clamp and NaN cannot be stored in a DOUBLE).
        for value in ('nan', 'inf', '-inf', '1e309'):
            response = self._save({'submission': str(self.submission.id), 'score': value})
            self.assertEqual(response.status_code, 400, value)
        self.assertFalse(SubmissionManualGrade.objects.filter(submission=self.submission).exists())


class ContestGradeScorePropagationTestCase(GradeTestBase):
    """Manual grade vs the student's *contest* score (ContestSubmission).

    The grade page reads ``Submission.points``, and the score students actually
    see (ranking / per-problem cells / participation.score) is computed by the
    contest format from ``ContestSubmission.points`` via
    ``participation.recompute_results()``. A manual grade must therefore only
    reach the contest score once it is *released* — before that the machine
    score stands, so saving an override never leaks it through the scoreboard.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.participation.recompute_results()

    def test_manual_grade_preserves_base_submission_points(self):
        # The machine score must NOT be overwritten by the teacher's override:
        # the override lives in SubmissionManualGrade and is folded into the
        # contest pipeline, while Submission.points keeps the judge's machine score.
        response = self._client(self.curator).post(
            self.save_url, {'submission': str(self.submission.id), 'score': '90'})
        self.assertEqual(response.status_code, 200, response.content)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.points, 50.0)
        self.assertEqual(
            SubmissionManualGrade.objects.get(submission=self.submission).points, 90.0)

    def test_manual_grade_keeps_machine_contest_score_until_release(self):
        # The override lives in SubmissionManualGrade; ContestSubmission.points
        # (what the ranking reads) must stay on the machine score until release.
        response = self._client(self.curator).post(
            self.save_url, {'submission': str(self.submission.id), 'score': '90'})
        self.assertEqual(response.status_code, 200, response.content)
        self.contest_submission.refresh_from_db()
        self.assertEqual(self.contest_submission.points, 50.0)
        self._client(self.curator).post(self.release_url)
        self.contest_submission.refresh_from_db()
        self.assertEqual(self.contest_submission.points, 90.0)

    def test_release_recomputes_participation_score(self):
        # participation.score (scoreboard/ranking) shows machine until the
        # manual grade is released, then folds in the released override.
        response = self._client(self.curator).post(
            self.save_url, {'submission': str(self.submission.id), 'score': '90'})
        self.assertEqual(response.status_code, 200, response.content)
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.score, 50.0)
        self._client(self.curator).post(self.release_url)
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.score, 90.0)


class ContestGradeRejudgeInteractionTestCase(GradeTestBase):
    """Manual grades are fragile: a rejudge's machine scoring recomputes the
    base Submission.points from case data, wiping the teacher's override."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

    def test_rejudge_machine_score_does_not_wipe_released_manual_grade(self):
        # Teacher overrides the score to 90 and releases it...
        response = self._client(self.curator).post(
            self.save_url, {'submission': str(self.submission.id), 'score': '90'})
        self.assertEqual(response.status_code, 200, response.content)
        self._client(self.curator).post(self.release_url)
        # ...then the judge finishes a rejudge: machine points recompute to 60.
        self.submission.case_points = 60
        self.submission.case_total = 100
        self.submission.save(update_fields=['case_points', 'case_total'])
        self.submission.points = round(60 / 100 * 100, 1)
        self.submission.save(update_fields=['points'])
        self.submission.update_contest()
        self.submission.refresh_from_db()
        self.contest_submission.refresh_from_db()
        # The base machine score is recomputed to 60, but the released manual
        # override (90) still wins for the actual contest score — a rejudge
        # must not wipe a released grade.
        self.assertEqual(self.submission.points, 60.0)
        self.assertEqual(self.contest_submission.points, 90.0)

    def test_rejudge_keeps_machine_score_for_unreleased_manual_grade(self):
        # An unreleased override must NOT survive a rejudge as the contest
        # score: the machine score (60) stands until the teacher releases.
        self._client(self.curator).post(
            self.save_url, {'submission': str(self.submission.id), 'score': '90'})
        self.submission.case_points = 60
        self.submission.case_total = 100
        self.submission.save(update_fields=['case_points', 'case_total'])
        self.submission.points = round(60 / 100 * 100, 1)
        self.submission.save(update_fields=['points'])
        self.submission.update_contest()
        self.submission.refresh_from_db()
        self.contest_submission.refresh_from_db()
        self.assertEqual(self.submission.points, 60.0)
        self.assertEqual(self.contest_submission.points, 60.0)
        # The teacher's grade record survives regardless.
        grade = SubmissionManualGrade.objects.get(submission=self.submission)
        self.assertEqual(grade.points, 90.0)
        self.assertFalse(grade.released)


class ContestGradeReleaseVariantTestCase(GradeTestBase):
    """Release All edge cases: idempotency, isolation, and side effects."""

    def _save_grade(self, score='77'):
        self._client(self.curator).post(
            self.save_url, {'submission': str(self.submission.id), 'score': score})
        return SubmissionManualGrade.objects.get(submission=self.submission)

    def test_release_all_is_idempotent(self):
        self._save_grade()
        client = self._client(self.curator)
        first = client.post(self.release_url)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(json.loads(first.content)['released'])
        second = client.post(self.release_url)
        self.assertTrue(json.loads(second.content)['released'])
        grade = SubmissionManualGrade.objects.get(submission=self.submission)
        self.assertTrue(grade.released)

    def test_release_does_not_touch_submission_points(self):
        self._save_grade('77')
        self._client(self.curator).post(self.release_url)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.points, 50.0)

    def test_release_is_isolated_between_contests(self):
        other = create_contest(
            key='gradevar_other', name='Other',
            start_time=timezone.now() - timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1),
            authors=('grade_superuser',),
        )
        other_problem = create_problem('gradevar_other_prob')
        ContestProblem.objects.create(contest=other, problem=other_problem, points=100, order=1)
        other_sub = Submission.objects.create(
            user=self.student.profile, problem=other_problem,
            language=Language.objects.get(key='PY3'), result='WA', status='D',
            contest_object=other, points=50.0, case_total=100,
        )
        other_grade = SubmissionManualGrade.objects.create(
            submission=other_sub, points=70.0, machine_points=50.0)
        my_grade = self._save_grade()

        other_url = reverse('contest_grade_release', args=[other.key])
        self._client(self.superuser).post(other_url)
        other_grade.refresh_from_db()
        my_grade.refresh_from_db()
        self.assertTrue(other_grade.released)
        self.assertFalse(my_grade.released)

    def test_release_batch_loads_affected_participations_once(self):
        """Release All must not query ContestParticipation once per student."""
        classmate = create_user(username='gradevar_release_classmate')
        other_part = create_contest_participation(contest=self.contest, user=classmate.profile)
        other_sub = Submission.objects.create(
            user=classmate.profile, problem=self.problem,
            language=Language.objects.get(key='PY3'), result='WA', status='D',
            contest_object=self.contest, points=25.0, case_total=100,
        )
        ContestSubmission.objects.create(
            submission=other_sub, problem=self.contest_problem,
            participation=other_part, points=25.0,
        )
        SubmissionManualGrade.objects.create(
            submission=other_sub, points=70.0, machine_points=25.0,
        )
        self._save_grade('77')

        manager = ContestParticipation.objects
        with mock.patch.object(manager, 'in_bulk', wraps=manager.in_bulk) as in_bulk:
            response = self._client(self.curator).post(self.release_url)

        self.assertEqual(response.status_code, 200)
        in_bulk.assert_called_once()
        self.assertEqual(set(in_bulk.call_args.args[0]), {self.participation.id, other_part.id})
        self.participation.refresh_from_db()
        other_part.refresh_from_db()
        self.assertEqual(self.participation.score, 77.0)
        self.assertEqual(other_part.score, 70.0)

    def test_release_anonymous_is_401(self):
        response = Client().post(self.release_url)
        self.assertEqual(response.status_code, 401)

    def test_release_get_does_not_release(self):
        # ContestMixin.dispatch turns the GET handler's Http404 into a generic
        # page — the security property is that a GET can never release grades.
        grade = self._save_grade()
        self.assertFalse(grade.released)
        self._client(self.curator).get(self.release_url)
        grade.refresh_from_db()
        self.assertFalse(grade.released)


class ContestGradePayloadVariantTestCase(GradeTestBase):
    """Data-shape variants in the grade page's embedded submission payload."""

    def _grade_payload(self, user=None):
        response = self._client(user or self.superuser).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        # |tojson output is single-line; the submissions array is the first
        # `var submissions = ...;` statement in the inline script.
        return json.loads(html.split('var submissions = ')[1].split(';\n')[0])

    def test_payload_orders_newest_first_and_marks_latest(self):
        now = timezone.now()
        Submission.objects.filter(id=self.submission.id).update(date=now - timedelta(hours=1))
        older = Submission.objects.create(
            user=self.student.profile, problem=self.problem,
            language=Language.objects.get(key='PY3'), result='WA', status='D',
            contest_object=self.contest, points=20.0, case_total=100,
        )
        Submission.objects.filter(id=older.id).update(date=now - timedelta(hours=2))
        ContestSubmission.objects.create(
            submission=older, problem=self.contest_problem,
            participation=self.participation, points=20.0,
        )
        payload = self._grade_payload()
        by_id = {s['id']: s for s in payload}
        self.assertTrue(by_id[self.submission.id]['is_latest'])
        self.assertFalse(by_id[older.id]['is_latest'])
        # Newest submission appears first in the list (queryset orders -date).
        ids_in_order = [s['id'] for s in payload]
        self.assertLess(ids_in_order.index(self.submission.id), ids_in_order.index(older.id))

    def test_default_format_final_is_best_not_newest(self):
        now = timezone.now()
        Submission.objects.filter(id=self.submission.id).update(date=now - timedelta(hours=2))
        newer_worse = Submission.objects.create(
            user=self.student.profile, problem=self.problem,
            language=Language.objects.get(key='PY3'), result='WA', status='D',
            contest_object=self.contest, points=10.0, case_total=100,
        )
        Submission.objects.filter(id=newer_worse.id).update(date=now - timedelta(hours=1))
        ContestSubmission.objects.create(
            submission=newer_worse, problem=self.contest_problem,
            participation=self.participation, points=10.0,
        )
        by_id = {s['id']: s for s in self._grade_payload()}
        self.assertTrue(by_id[self.submission.id]['is_latest'])
        self.assertFalse(by_id[newer_worse.id]['is_latest'])

    def test_virtual_is_never_final(self):
        virtual_part = ContestParticipation.objects.create(
            contest=self.contest, user=self.student.profile, virtual=1,
            real_start=timezone.now(),
        )
        sub = Submission.objects.create(
            user=self.student.profile, problem=self.problem,
            language=Language.objects.get(key='PY3'), result='AC', status='D',
            contest_object=self.contest, points=100.0, case_total=100,
        )
        ContestSubmission.objects.create(
            submission=sub, problem=ContestProblem.objects.get(contest=self.contest),
            participation=virtual_part, points=100.0,
        )
        payload = self._grade_payload()
        by_id = {s['id']: s for s in payload}
        self.assertTrue(by_id[sub.id]['virtual'])
        self.assertFalse(by_id[self.submission.id]['virtual'])
        # Higher-scoring / newer virtual must never become the live final version.
        self.assertTrue(by_id[self.submission.id]['is_latest'])
        self.assertFalse(by_id[sub.id]['is_latest'])

    def test_empty_contest_payload_is_empty_list(self):
        empty = create_contest(
            key='gradevar_empty', name='Empty',
            start_time=timezone.now() - timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1),
            authors=('grade_superuser',),
        )
        response = self._client(self.superuser).get(reverse('contest_grade', args=[empty.key]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'var submissions = []')

    def test_review_note_and_reviewer_in_payload(self):
        self.submission.review_note = 'looks good'
        self.submission.reviewed_by = self.curator.profile
        self.submission.reviewed_at = timezone.now()
        self.submission.save(update_fields=['review_note', 'reviewed_by', 'reviewed_at'])
        payload = self._grade_payload()
        by_id = {s['id']: s for s in payload}
        self.assertEqual(by_id[self.submission.id]['review_note'], 'looks good')
        self.assertEqual(by_id[self.submission.id]['reviewed_by'], 'grade_curator')


class ContestGradeStatusTestCase(GradeTestBase):
    """The status endpoint the grade page polls while a rejudge runs."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.status_url = reverse('contest_grade_status', args=[cls.contest.key])

    def _status(self, user, query):
        return self._client(user).get(self.status_url, query)

    def test_anonymous_is_401(self):
        self.assertEqual(Client().get(self.status_url, {'submission': str(self.submission.id)}).status_code, 401)

    def test_non_editor_is_403(self):
        self.assertEqual(
            self._status(self.student, {'submission': str(self.submission.id)}).status_code, 403)
        self.assertEqual(
            self._status(self.tester, {'submission': str(self.submission.id)}).status_code, 403)

    def test_editor_gets_status_json(self):
        response = self._status(self.curator, {'submission': str(self.submission.id)})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['id'], self.submission.id)
        self.assertTrue(data['ok'])
        self.assertEqual(data['status'], 'D')
        self.assertEqual(data['points'], 50.0)
        self.assertEqual(data['total'], 100)
        self.assertTrue(data['is_graded'])
        self.assertIn('test_cases', data)

    def test_status_test_cases_include_ai_feedback_fields(self):
        """Poll payload must keep feedback/extended_feedback so the grade desk
        can show the same Detailed Feedback as the submission status page."""
        from judge.models import SubmissionTestCase
        SubmissionTestCase.objects.filter(submission=self.submission).delete()
        SubmissionTestCase.objects.create(
            submission=self.submission, case=1, status='WA', points=11.5, total=100,
            feedback='Overall', extended_feedback='Too brief — missing self-replication.',
        )
        data = json.loads(self._status(self.curator, {'submission': str(self.submission.id)}).content)
        self.assertEqual(len(data['test_cases']), 1)
        tc = data['test_cases'][0]
        self.assertEqual(tc['feedback'], 'Overall')
        self.assertIn('self-replication', tc['extended_feedback'])
        self.assertEqual(tc['total'], 100)
        self.assertIn('batch', tc)

    def test_missing_submission_param_is_400(self):
        self.assertEqual(self._status(self.curator, {}).status_code, 400)

    def test_non_integer_submission_param_is_400(self):
        for bad in ('abc', '1.5', '1e3'):
            with self.subTest(bad=bad):
                self.assertEqual(self._status(self.curator, {'submission': bad}).status_code, 400)

    def test_negative_submission_param_is_404(self):
        # int('-1') parses; only the lookup fails → 404 (same as the save endpoint).
        self.assertEqual(self._status(self.curator, {'submission': '-1'}).status_code, 404)

    def test_unknown_submission_is_404(self):
        self.assertEqual(self._status(self.curator, {'submission': '99999999'}).status_code, 404)

    def test_cross_contest_submission_is_404(self):
        other = create_contest(
            key='gradevar_st_other', name='Other',
            start_time=timezone.now() - timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1),
            authors=('grade_superuser',),
        )
        other_url = reverse('contest_grade_status', args=[other.key])
        response = self._client(self.superuser).get(
            other_url, {'submission': str(self.submission.id)})
        self.assertEqual(response.status_code, 404)

    def test_post_is_405(self):
        # http_method_names = ['get'] — polling must never mutate anything.
        response = self._client(self.curator).post(
            self.status_url, {'submission': str(self.submission.id)})
        self.assertEqual(response.status_code, 405)

    def test_status_reflects_rejudge_in_flight(self):
        # A queued submission reports is_graded=False so the page keeps polling.
        self.submission.status = 'QU'
        self.submission.save(update_fields=['status'])
        data = json.loads(self._status(self.curator, {'submission': str(self.submission.id)}).content)
        self.assertEqual(data['status'], 'QU')
        self.assertFalse(data['is_graded'])
        # Restore for other tests in the class.
        self.submission.status = 'D'
        self.submission.save(update_fields=['status'])


class ContestGradeExamFinalTestCase(TestCase):
    """Exam format: final version is the latest live MCQ even if it scores worse."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.editor = create_user(username='grade_exam_editor', user_permissions=('edit_own_contest',))
        cls.student = create_user(username='grade_exam_student')
        cls.contest = create_contest(
            key='gradeexamfv', format_name='exam',
            authors=('grade_exam_editor',),
            start_time=timezone.now() - timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1),
        )
        cls.mcq = create_problem('gradeexam-mcq', submission_type='M', points=100)
        cls.cp = create_contest_problem(contest=cls.contest, problem=cls.mcq, points=100, order=1)
        cls.part = create_contest_participation(contest=cls.contest, user=cls.student.profile)
        lang = Language.get_answer_language()
        now = timezone.now()
        cls.older = Submission.objects.create(
            user=cls.student.profile, problem=cls.mcq, language=lang,
            result='AC', status='D', contest_object=cls.contest, points=100, case_total=100,
        )
        Submission.objects.filter(id=cls.older.id).update(date=now - timedelta(hours=2))
        ContestSubmission.objects.create(
            submission=cls.older, problem=cls.cp, participation=cls.part, points=100,
        )
        cls.newer = Submission.objects.create(
            user=cls.student.profile, problem=cls.mcq, language=lang,
            result='WA', status='D', contest_object=cls.contest, points=0, case_total=100,
        )
        Submission.objects.filter(id=cls.newer.id).update(date=now - timedelta(hours=1))
        ContestSubmission.objects.create(
            submission=cls.newer, problem=cls.cp, participation=cls.part, points=0,
        )
        cls.grade_url = reverse('contest_grade', args=[cls.contest.key])

    def _payload(self):
        client = Client()
        client.force_login(self.editor)
        html = client.get(self.grade_url).content.decode('utf-8')
        return json.loads(html.split('var submissions = ')[1].split(';\n')[0])

    def test_exam_mcq_final_is_latest_even_if_worse(self):
        by_id = {s['id']: s for s in self._payload()}
        self.assertTrue(by_id[self.newer.id]['is_latest'])
        self.assertFalse(by_id[self.older.id]['is_latest'])
