"""Tests for the teacher-side contest grading workspace (judge.views.contests.ContestGrade).

The grade workspace is the most sensitive screen a teacher has: it embeds every
student's *source code* (fully attacker-controlled) into inline JavaScript, and
its save endpoint can override a student's score. Two properties are therefore
non-negotiable and locked down here:

1. **XSS safety** — submission source must be serialized script-safely so a
   student cannot break out of the ``<script>`` block (stored XSS against the
   teacher who opens the page).
2. **Authorization** — only real editors may open the page or change a score.
   A full role matrix (superuser / edit_all / author / curator / tester /
   spectator / org-admin / student / anonymous) plus *mutation* tests (grant a
   permission, observe access flip) pin the exact boundary, because a grade
   override leaking to a student or tester is a grade-integrity breach.

Layout of this file:

* ``ContestGradePermissionMatrixTestCase``  — GET the page as every role.
* ``ContestGradeSavePermissionMatrixTestCase`` — POST a score as every role.
* ``ContestGradeSaveValidationTestCase``   — input validation + CSRF + isolation.
* ``ContestGradeMutationTestCase``         — grant/revoke flips access (mutation).
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
from unittest import mock, skipUnless

from django.contrib.auth.models import Permission  # noqa: F401 (documented in module docstring)
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponseForbidden
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import ContestParticipation, ContestProblem, ContestSubmission, Language, Submission, \
    SubmissionManualGrade, SubmissionSource
from judge.models.tests.util import create_contest, create_problem, create_user

XSS_SOURCE = '</script><script>alert(document.cookie)</script>'


def _csrf_failure_plain(request, reason=''):
    """Stand-in for ``django.views.csrf.csrf_failure`` used by the CSRF test.

    The real failure view renders ``403.html`` through the Django template
    engine; on Python 3.14 + Django 4.2 that render crashes the test client
    (``BaseContext.__copy__`` uses ``copy(super())``, removed in 3.14). The
    *security contract* under test — the CSRF middleware rejects a token-less
    POST before the view runs — does not depend on the failure page, so we
    swap in a plain 403 via ``CSRF_FAILURE_VIEW``.
    """
    return HttpResponseForbidden('CSRF rejected')


class GradeTestBase(TestCase):
    """Shared fixture: one contest populated with every role plus a submission.

    Subclasses re-use the same ``setUpTestData`` (built once per class), so the
    database stays small while every role is covered.
    """

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        # ── Roles (each username is self-documenting) ───────────────────────
        cls.superuser = create_user(username='grade_superuser', is_superuser=True, is_staff=True)
        cls.edit_all = create_user(username='grade_edit_all', user_permissions=('edit_all_contest',))
        cls.author = create_user(username='grade_author', user_permissions=('edit_own_contest',))
        cls.curator = create_user(username='grade_curator', user_permissions=('edit_own_contest',))
        # A curator *without* edit_own_contest: being listed as a curator is not
        # enough on its own — the permission must also be held.
        cls.curator_no_perm = create_user(username='grade_curator_noperm')
        # Holds edit_own_contest but is NOT an author/curator of this contest.
        cls.edit_own_outsider = create_user(username='grade_edit_own_out', user_permissions=('edit_own_contest',))
        cls.tester = create_user(username='grade_tester')
        cls.spectator = create_user(username='grade_spectator')
        cls.org_admin = create_user(username='grade_org_admin', user_permissions=('organization_admin',))
        cls.student = create_user(username='grade_student')

        # ── Contest + problem + a graded submission ─────────────────────────
        cls.problem = create_problem('grade-perm-prob')
        cls.contest = create_contest(
            key='gradeperm',
            name='Grade Perm',
            start_time=timezone.now() - timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1),
            authors=('grade_author',),
            curators=('grade_curator', 'grade_curator_noperm'),
            testers=('grade_tester',),
            spectators=('grade_spectator',),
        )
        cls.contest_problem = ContestProblem.objects.create(
            contest=cls.contest, problem=cls.problem, points=100, order=1)
        cls.participation = ContestParticipation.objects.create(
            contest=cls.contest, user=cls.student.profile,
            real_start=timezone.now() - timedelta(hours=1),
        )
        # case_total=100 makes the manual-score ceiling a deterministic 100.
        cls.submission = Submission.objects.create(
            user=cls.student.profile, problem=cls.problem,
            language=Language.objects.get(key='PY3'),
            result='WA', status='D', contest_object=cls.contest,
            points=50.0, case_total=100,
        )
        SubmissionSource.objects.create(submission=cls.submission, source='print(1)')
        cls.contest_submission = ContestSubmission.objects.create(
            submission=cls.submission, problem=cls.contest_problem,
            participation=cls.participation, points=50.0,
        )

        cls.grade_url = reverse('contest_grade', args=[cls.contest.key])
        cls.save_url = reverse('contest_grade_save', args=[cls.contest.key])
        cls.release_url = reverse('contest_grade_release', args=[cls.contest.key])

    def _client(self, user):
        """Return a fresh test client logged in as ``user`` (or anonymous)."""
        client = Client()
        if user is not None:
            client.force_login(user)
        return client


class ContestGradePermissionMatrixTestCase(GradeTestBase):
    """GET the grade page as every role.

    The view deliberately raises ``Http404`` (not 403) for authenticated
    non-editors so the contest's existence is not leaked by the status code.
    """

    def _assert_get(self, user, expected_status):
        response = self._client(user).get(self.grade_url)
        self.assertEqual(response.status_code, expected_status,
                         'role=%s expected %d got %d' % (getattr(user, 'username', 'anon'),
                                                         expected_status, response.status_code))

    def test_editors_get_200(self):
        """Actual editors reach the page.

        Merged policy (origin/develop): author/curator *membership* is the grant
        — a curator listed on the contest may grade even without the global
        ``edit_own_contest`` permission (the Teachers group doesn't carry it).
        """
        for user in (self.superuser, self.edit_all, self.author, self.curator,
                     self.curator_no_perm):
            self._assert_get(user, 200)

    def test_non_editors_get_404(self):
        """Everyone who is NOT an author/curator — including privileged-but-
        non-editor roles — is 404.

        Tester, spectator and org-admin can *view* contests but must never be
        able to open the grading workspace; holding ``edit_own_contest`` without
        being a member of this contest is likewise not enough.
        """
        for user in (self.edit_own_outsider, self.tester,
                     self.spectator, self.org_admin, self.student):
            self._assert_get(user, 404)

    def test_anonymous_redirects_to_login(self):
        response = Client().get(self.grade_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_unknown_contest_is_404(self):
        """A non-existent contest key is 404 even for a full editor."""
        response = self._client(self.edit_all).get(reverse('contest_grade', args=['nope']))
        self.assertEqual(response.status_code, 404)


class ContestGradeSavePermissionMatrixTestCase(GradeTestBase):
    """POST a manual score as every role.

    Anonymous → 401, authenticated non-editor → 403, editor → 200. The split
    between 401/403 is intentional: it lets the frontend distinguish "please
    log in" from "you may not do this".
    """

    def _post(self, user, submission_id, score='50'):
        return self._client(user).post(self.save_url, {'submission': submission_id, 'score': score})

    def test_anonymous_gets_401(self):
        response = Client().post(self.save_url, {'submission': self.submission.id, 'score': 50})
        self.assertEqual(response.status_code, 401)

    def test_editors_can_save(self):
        # Author/curator membership alone grants grading rights (merged policy);
        # a curator without the global permission can save too.
        for user in (self.superuser, self.edit_all, self.author, self.curator,
                     self.curator_no_perm):
            response = self._post(user, self.submission.id, score=60)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.json()['ok'])

    def test_non_editors_get_403(self):
        for user in (self.edit_own_outsider, self.tester,
                     self.spectator, self.org_admin, self.student):
            response = self._post(user, self.submission.id, score=60)
            self.assertEqual(response.status_code, 403,
                             'role=%s expected 403 got %d' % (user.username, response.status_code))

    def test_tester_cannot_override_their_own_or_others_score(self):
        """Regression guard: testers see submissions but must not grade them."""
        response = self._post(self.tester, self.submission.id, score=60)
        self.assertEqual(response.status_code, 403)


class ContestGradeSaveValidationTestCase(GradeTestBase):
    """Input validation, CSRF and cross-contest isolation for the save endpoint."""

    def _post(self, **data):
        return self._client(self.curator).post(self.save_url, data)

    def test_save_persists_score(self):
        response = self._post(submission=self.submission.id, score='42.5')
        self.assertEqual(response.status_code, 200)
        self.submission.refresh_from_db()
        # The machine score is preserved; the manual override lives in its own table.
        self.assertEqual(self.submission.points, 50.0)
        grade = SubmissionManualGrade.objects.get(submission=self.submission)
        self.assertEqual(grade.points, 42.5)
        self.assertEqual(response.json()['score'], 42.5)

    def test_missing_submission_id_is_400(self):
        self.assertEqual(self._post(score='50').status_code, 400)

    def test_non_integer_submission_id_is_400(self):
        self.assertEqual(self._post(submission='abc', score='50').status_code, 400)

    def test_non_numeric_score_is_400(self):
        self.assertEqual(self._post(submission=self.submission.id, score='abc').status_code, 400)

    def test_unknown_submission_is_404(self):
        self.assertEqual(self._post(submission=999999, score='50').status_code, 404)

    def test_cross_contest_submission_is_404(self):
        """A submission belonging to a *different* contest must not be gradable.

        Guards against an editor of contest A reaching into contest B via a raw
        submission id. The query is scoped by ``contest_object=self.object``.
        """
        other = create_contest(
            key='othercontest', name='Other',
            start_time=timezone.now() - timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1),
            authors=('grade_author',),
        )
        other_sub = Submission.objects.create(
            user=self.student.profile, problem=self.problem,
            language=Language.objects.get(key='PY3'),
            result='WA', status='D', contest_object=other,
            points=50.0, case_total=100,
        )
        response = self._post(submission=other_sub.id, score='70')
        self.assertEqual(response.status_code, 404)

    def test_negative_score_clamped_to_zero(self):
        response = self._post(submission=self.submission.id, score='-999')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            SubmissionManualGrade.objects.get(submission=self.submission).points, 0.0)
        self.assertEqual(response.json()['score'], 0.0)

    def test_over_max_score_clamped_to_total(self):
        response = self._post(submission=self.submission.id, score='9999')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            SubmissionManualGrade.objects.get(submission=self.submission).points, 100.0)  # case_total ceiling
        self.assertEqual(response.json()['score'], 100.0)

    def test_blank_score_treated_as_zero(self):
        response = self._post(submission=self.submission.id, score='')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            SubmissionManualGrade.objects.get(submission=self.submission).points, 0.0)

    # ── Review note (评语) persistence ──────────────────────────────────────

    def test_save_persists_review_note_and_reviewer(self):
        response = self._post(submission=self.submission.id, score='42', note='Good attempt, review edge cases.')
        self.assertEqual(response.status_code, 200)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.review_note, 'Good attempt, review edge cases.')
        self.assertEqual(self.submission.reviewed_by, self.curator.profile)
        self.assertIsNotNone(self.submission.reviewed_at)
        self.assertEqual(response.json()['note'], 'Good attempt, review edge cases.')

    def test_save_without_note_keeps_existing_note(self):
        """Omitting the note field must NOT wipe a previously saved note."""
        self.submission.review_note = 'kept'
        self.submission.save(update_fields=['review_note'])
        response = self._post(submission=self.submission.id, score='50')
        self.assertEqual(response.status_code, 200)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.review_note, 'kept')

    def test_save_with_blank_note_clears_it(self):
        self.submission.review_note = 'to be cleared'
        self.submission.save(update_fields=['review_note'])
        response = self._post(submission=self.submission.id, score='50', note='')
        self.assertEqual(response.status_code, 200)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.review_note, '')

    @override_settings(CSRF_FAILURE_VIEW='judge.views.tests.test_contest_grade._csrf_failure_plain')
    def test_csrf_enforced(self):
        """A POST without a CSRF token must be rejected, even for an editor.

        The real browser sends ``X-CSRFToken`` (see resources/common.js
        ``$.ajaxSetup``); this test proves the endpoint is not accidentally
        ``csrf_exempt`` and cannot be hit cross-site. ``CSRF_FAILURE_VIEW`` is
        swapped for a plain 403 so the assertion is about the middleware
        rejection, not about the 403 page's own render (see helper docstring).
        """
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.curator)
        response = client.post(self.save_url, {'submission': self.submission.id, 'score': '80'})
        self.assertEqual(response.status_code, 403)
        # The view never ran, so no manual grade may be created.
        self.assertFalse(SubmissionManualGrade.objects.filter(submission=self.submission).exists())


class ContestGradeMutationTestCase(GradeTestBase):
    """Mutation tests: flip one permission dimension and assert access flips.

    These are the strongest guard against a too-broad ``is_editable_by``: if
    granting ``edit_own_contest`` to a random student suddenly lets them grade,
    the gate is wrong. If *membership* alone doesn't let a curator grade, the
    gate is wrong too (merged policy: membership is the grant).
    """

    def _get_status(self, user):
        return self._client(user).get(self.grade_url).status_code

    def test_granting_edit_own_to_non_editor_still_denied(self):
        """Permission alone is not enough — the user must also be a member."""
        outsider = self.edit_own_outsider
        # Already has edit_own_contest but is not an author/curator → still 404.
        self.assertEqual(self._get_status(outsider), 404)

    def test_promote_student_to_curator_grants_access(self):
        """Curator membership alone flips a student into a grader.

        Permission is irrelevant here: being added to the curator list is the
        grant (merged origin/develop policy).
        """
        self.assertEqual(self._get_status(self.student), 404)  # baseline
        self.contest.curators.add(self.student.profile)
        self.assertEqual(self._get_status(self.student), 200)

    def test_removing_curator_relationship_revokes_access(self):
        """Removing the curator relationship (even keeping the permission) revokes."""
        self.assertEqual(self._get_status(self.curator), 200)  # baseline
        self.contest.curators.remove(self.curator.profile)
        self.assertEqual(self._get_status(self.curator), 404)

    def test_curator_membership_alone_grants_access(self):
        """A curator with NO global permission can still grade (course workflow).

        The Teachers group carries no ``edit_own_contest``, so teacher-curators
        rely on membership alone — this is the load-bearing merged policy.
        """
        self.assertEqual(self._get_status(self.curator_no_perm), 200)
        # Sanity: the gate is user-scoped, not contest-scoped-global.
        self.assertEqual(self._get_status(self.edit_all), 200)


class ContestGradeXSSRegressionTestCase(TestCase):
    """Keep the original stored-XSS regression pinned (source break-out)."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.editor = create_user(username='grade_xss_editor', user_permissions=('edit_all_contest',))
        cls.student = create_user(username='grade_xss_student')
        cls.problem = create_problem('grade-xss-prob')
        cls.contest = create_contest(
            key='gradexss', name='Grade XSS',
            start_time=timezone.now() - timedelta(days=1),
            end_time=timezone.now() + timedelta(days=1),
            authors=('grade_xss_editor',),
        )
        ContestProblem.objects.create(contest=cls.contest, problem=cls.problem, points=100, order=1)
        cls.submission = Submission.objects.create(
            user=cls.student.profile, problem=cls.problem,
            language=Language.objects.get(key='PY3'),
            result='WA', status='D', contest_object=cls.contest,
        )
        SubmissionSource.objects.create(submission=cls.submission, source=XSS_SOURCE)

    def test_grade_page_escapes_script_breakout_in_source(self):
        response = self._client().get(reverse('contest_grade', args=[self.contest.key]))
        self.assertEqual(response.status_code, 200)
        content = response.content

        # The literal `</script><script>…` must never appear in the rendered page.
        self.assertNotIn(b'</script><script>', content)
        self.assertNotIn(XSS_SOURCE.encode(), content)

        # The source must be present, but only in script-safe escaped form
        # (`|tojson` escapes `<`/`>`/`&`/`'` to \u003c etc.).
        self.assertIn(b'\\u003c/script\\u003e\\u003cscript\\u003e', content)

    def _client(self):
        client = Client()
        client.force_login(self.editor)
        return client


class ContestGradePayloadTestCase(GradeTestBase):
    """The grade page's embedded JSON carries everything the reference UI needs:
    review-note state, problem statements, virtual/latest markers, release flag."""

    def _page_json(self):
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        # Pull the `submissions` / `problems` / `released` JS variables out of
        # the rendered script block.
        html = response.content.decode()
        submissions = re.search(r'var submissions = (\[.*?\]);', html, re.S)
        problems = re.search(r'var problems = (\[.*?\]);', html, re.S)
        self.assertIsNotNone(submissions, 'submissions JSON missing from page')
        self.assertIsNotNone(problems, 'problems JSON missing from page')
        return json.loads(submissions.group(1)), json.loads(problems.group(1))

    def test_payload_includes_review_state_and_description(self):
        subs, probs = self._page_json()
        self.assertEqual(len(subs), 1)
        self.assertEqual(len(probs), 1)
        self.assertIn('review_note', subs[0])
        self.assertIn('reviewed_by', subs[0])
        self.assertIn('reviewed_at', subs[0])
        self.assertIn('is_latest', subs[0])
        self.assertIn('virtual', subs[0])
        # Problem library must carry the statement (题面) for the detail pane.
        self.assertEqual(probs[0]['code'], self.problem.code)
        self.assertIn('description', probs[0])

    def test_release_flag_in_context(self):
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('grade_released_count', response.context)
        self.assertIn('grade_unreleased_count', response.context)
        self.assertEqual(response.context['grade_released_count'], 0)
        self.assertEqual(response.context['grade_unreleased_count'], 0)
        # Saving a manual grade creates an unreleased override.
        self._client(self.curator).post(
            self.save_url, {'submission': self.submission.id, 'score': '80'})
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.context['grade_released_count'], 0)
        self.assertEqual(response.context['grade_unreleased_count'], 1)

    def test_release_badge_requires_a_released_manual_score(self):
        """Machine-only rows must not be presented as manually released."""
        subs, _ = self._page_json()
        row = next(row for row in subs if row['id'] == self.submission.id)
        self.assertIsNone(row['manual_points'])
        self.assertFalse(row['released'])

        self._client(self.curator).post(
            self.save_url, {'submission': self.submission.id, 'score': '80'})
        subs, _ = self._page_json()
        row = next(row for row in subs if row['id'] == self.submission.id)
        self.assertEqual(row['manual_points'], 80.0)
        self.assertFalse(row['released'])

        self._client(self.curator).post(self.release_url)
        subs, _ = self._page_json()
        row = next(row for row in subs if row['id'] == self.submission.id)
        self.assertEqual(row['manual_points'], 80.0)
        self.assertTrue(row['released'])

        html = self._client(self.curator).get(self.grade_url).content.decode()
        self.assertIn('if (s.manual_points != null && s.released)', html)
        self.assertNotIn('if (s.manual_points == null || s.released)', html)


class ContestGradeJSValidityTestCase(GradeTestBase):
    """The inline <script> the grade page renders must be valid JavaScript.

    Student-controlled content (source code, review notes) is embedded in the
    page. Even though it is escaped, a mis-rendered ``{{ }}`` or an escaping
    regression would produce a syntax error that silently breaks the whole
    grading UI. We parse the rendered block with ``node --check``; hosts without
    node skip (documented, not a fake pass).
    """

    @skipUnless(shutil.which('node'), 'node executable not available on this host')
    def test_inline_script_parses(self):
        # Push JS-hostile strings through the pipeline: quotes, </textarea>,
        # and a </script> breakout in the source.
        self.submission.review_note = "Note with 'single' and \"double\" quotes </textarea>"
        self.submission.save(update_fields=['review_note'])
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()

        blocks = re.findall(r'<script type="text/javascript">(.*?)</script>', html, re.S)
        self.assertTrue(blocks, 'no inline <script> block found on the grade page')
        for js in blocks:
            fd, path = tempfile.mkstemp(suffix='.js')
            try:
                with os.fdopen(fd, 'w') as f:
                    f.write(js)
                proc = subprocess.run(['node', '--check', path], capture_output=True, text=True)
            finally:
                os.unlink(path)
            self.assertEqual(proc.returncode, 0, 'inline JS failed node --check:\n%s' % proc.stderr)


class ContestGradeWorkflowTestCase(GradeTestBase):
    """End-to-end grade flow: load the page, grade with a review note, reload.

    Exercises the real teacher loop — open workspace → save score + 评语 →
    reopen → the new state is reflected in the embedded JSON the JS renders.
    """

    def test_full_grade_flow(self):
        client = self._client(self.curator)

        # 1. Open the grade page; the problem must be listed in the filter/library.
        page = client.get(self.grade_url)
        self.assertEqual(page.status_code, 200)
        html0 = page.content.decode()
        self.assertIn('grade-perm-prob', html0)

        # 2. Save a manual grade with a review note.
        resp = client.post(self.save_url, {
            'submission': self.submission.id, 'score': '77', 'note': 'Nice work',
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['score'], 77.0)
        self.assertEqual(body['note'], 'Nice work')

        # 3. Reload: the new score + note are in the submissions JSON payload.
        page2 = client.get(self.grade_url)
        html = page2.content.decode()
        self.assertIn('"points": 77.0', html)
        self.assertIn('"review_note": "Nice work"', html)

        # 4. DB agrees: the machine score is untouched, the manual grade is stored.
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.points, 50.0)
        grade = SubmissionManualGrade.objects.get(submission=self.submission)
        self.assertEqual(grade.points, 77.0)
        self.assertEqual(self.submission.review_note, 'Nice work')
        self.assertEqual(self.submission.reviewed_by, self.curator.profile)


class ContestGradeReleaseTestCase(GradeTestBase):
    """The "Release All" endpoint releases every manual grade in the contest.

    Same authorization contract as the save endpoint: 401 anonymous, 403
    authenticated non-editor, 200 editor. Releasing reveals a teacher's manual
    override to the student, so it is exactly as sensitive as editing a score.
    """

    def _post(self, user):
        return self._client(user).post(self.release_url)

    def _save_grade(self, score='77'):
        """Save a manual grade for the shared submission and return the record."""
        self._client(self.curator).post(
            self.save_url, {'submission': self.submission.id, 'score': score})
        return SubmissionManualGrade.objects.get(submission=self.submission)

    def test_anonymous_gets_401(self):
        self.assertEqual(Client().post(self.release_url).status_code, 401)

    def test_release_all_marks_manual_grades_released(self):
        grade = self._save_grade()
        self.assertFalse(grade.released)
        response = self._post(self.curator)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['released'])
        grade.refresh_from_db()
        self.assertTrue(grade.released)

    def test_release_all_is_idempotent(self):
        self._save_grade()
        first = self._post(self.curator)
        self.assertEqual(first.status_code, 200)
        second = self._post(self.edit_all)
        self.assertEqual(second.status_code, 200)
        grade = SubmissionManualGrade.objects.get(submission=self.submission)
        self.assertTrue(grade.released)

    def test_release_all_does_not_touch_submission_points(self):
        self._save_grade('77')
        self._post(self.curator)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.points, 50.0)

    def test_non_editors_get_403(self):
        # curator_no_perm is intentionally excluded: curator membership alone
        # grants grading rights under the merged policy (see is_editable_by).
        for user in (self.edit_own_outsider, self.tester,
                     self.spectator, self.org_admin, self.student):
            response = self._post(user)
            self.assertEqual(response.status_code, 403,
                             'role=%s expected 403 got %d' % (user.username, response.status_code))

    def test_tester_cannot_release(self):
        """Testers see submissions but must never be able to release grades."""
        self.assertEqual(self._post(self.tester).status_code, 403)

    def test_get_is_side_effect_free(self):
        """GET must never release (DMOJ convention: Http404 from the GET handler
        becomes a generic-message page via ContestMixin.dispatch)."""
        grade = self._save_grade()
        self.assertFalse(grade.released)
        self._client(self.curator).get(self.release_url)
        grade.refresh_from_db()
        self.assertFalse(grade.released)


class ContestGradeFileUploadTestCase(GradeTestBase):
    """File-upload submissions expose a download link (never their source) on the grade page."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # A FILE problem + submission carrying a SubmissionFile upload.
        cls.file_problem = create_problem('grade-file-prob', submission_type='F', points=10)
        ContestProblem.objects.create(contest=cls.contest, problem=cls.file_problem, points=10, order=2)
        cls.file_submission = Submission.objects.create(
            user=cls.student.profile, problem=cls.file_problem,
            language=Language.objects.get(key='PY3'),
            result='QU', status='D', contest_object=cls.contest,
            points=0.0, case_total=10,
        )
        # SubmissionFile.file uses submission_storage; point it at a temp dir.
        from django.core.files.storage import FileSystemStorage
        from judge.models import SubmissionFile
        from judge.storage.resilient import ResilientStorage
        tmp = tempfile.TemporaryDirectory()
        cls._tmp = tmp
        storage = ResilientStorage(primary=FileSystemStorage(location=tmp.name), backup=None)
        cls._patch = mock.patch.object(SubmissionFile._meta.get_field('file'), 'storage', storage)
        cls._patch.start()
        cls.upload = SubmissionFile.objects.create(
            submission=cls.file_submission,
            file=SimpleUploadedFile('report.pdf', b'%PDF-1.4 hello'),
            original_name='report.pdf',
            size=14,
        )

    @classmethod
    def tearDownClass(cls):
        try:
            cls._patch.stop()
            cls._tmp.cleanup()
        finally:
            super().tearDownClass()

    def test_file_submission_serializes_upload_not_source(self):
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        subs = json.loads(re.search(r'var submissions = (\[.*?\]);', response.content.decode(), re.S).group(1))
        file_row = next(s for s in subs if s['id'] == self.file_submission.id)
        self.assertEqual(file_row['problem_type'], 'F')
        self.assertIsNotNone(file_row['upload'])
        self.assertEqual(file_row['upload']['original_name'], 'report.pdf')
        self.assertEqual(file_row['upload']['download_url'],
                         reverse('submission_file_download', args=(self.file_submission.id,)))
        # File submissions carry no source; the page must not embed a code block for them.
        self.assertEqual(file_row['source'], '')

    def test_non_file_submission_has_no_upload_key(self):
        response = self._client(self.curator).get(self.grade_url)
        subs = json.loads(re.search(r'var submissions = (\[.*?\]);', response.content.decode(), re.S).group(1))
        code_row = next(s for s in subs if s['id'] == self.submission.id)
        self.assertIsNone(code_row['upload'])

    def test_attempts_filter_dropdown_rendered(self):
        """The MCQ/attempt aggregation dropdown is present for every grade page."""
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="f-attempts"', html)
        self.assertIn('Highest score', html)
        self.assertIn('Last submission', html)


class ContestGradeAttemptAggregationTestCase(GradeTestBase):
    """Payload carries ``problem_type`` so the frontend can aggregate MCQ attempts (best/latest)."""

    def test_payload_includes_problem_type_per_submission(self):
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        subs = json.loads(re.search(r'var submissions = (\[.*?\]);', response.content.decode(), re.S).group(1))
        self.assertGreaterEqual(len(subs), 1)
        for s in subs:
            self.assertIn('problem_type', s)


class ContestGradeWorkspaceLayoutTestCase(GradeTestBase):
    """The grade page now uses the problem-workspace shell (``ws-app`` /
    ``ws-toolbar`` / slide-in drawer / split panes) instead of the old two-column
    grid. The Django test client does not execute JavaScript, so these tests pin
    the *static* layout contract the workspace JS relies on: shell classes,
    toolbar actions, drawer filters, pane mounts and the rich-editor assets.
    """

    def _page_html(self):
        response = self._client(self.curator).get(self.grade_url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_workspace_shell_present(self):
        html = self._page_html()
        for element in (
            'id="grade-app"', 'class="ws-app"',
            'class="ws-toolbar has-leave"', 'ws-toolbar-center',
            'id="ws-body"', 'class="ws-body layout-lr"',
            'id="ws-drawer"', 'class="ws-drawer grade-drawer"',
            'id="drawer-backdrop"',
            'id="pane-left"', 'class="ws-pane ws-pane-left"',
            'id="pane-right"', 'class="ws-pane ws-pane-right"',
            'id="ws-splitter"', 'class="ws-splitter"',
            'id="grade-right"',
            'id="grade-result-pane"', 'id="grade-review-pane"',
            'id="grade-v-splitter"',
        ):
            self.assertIn(element, html, 'missing workspace shell element: %s' % element)

    def test_toolbar_actions_present(self):
        html = self._page_html()
        for element in (
            'id="btn-overview"', 'id="ctx-home"', 'id="btn-prev"', 'id="btn-next"',
            'id="layout-toggle"', 'id="layout-mode"',
            'id="btn-release"', 'id="btn-rejudge"', 'id="btn-save-current"',
        ):
            self.assertIn(element, html, 'missing toolbar element: %s' % element)
        # The context label links back to the contest detail page.
        self.assertIn('href="%s"' % reverse('contest_view', args=[self.contest.key]), html)

    def test_drawer_filters_present(self):
        html = self._page_html()
        for element in (
            'id="f-queue"', 'id="f-part"', 'id="f-problem"', 'id="f-user"',
            'id="f-attempts"', 'id="left-summary"', 'id="drawer-progress"',
            'id="drawer-close"', 'id="submission-list"',
        ):
            self.assertIn(element, html, 'missing drawer element: %s' % element)
        # The problem filter carries every contest problem as a server-side option.
        self.assertIn('value="%s"' % self.problem.code, html)

    def test_filter_option_sets(self):
        html = self._page_html()
        # Queue: all / final versions / ungraded / graded.
        for option in ('value="all"', 'value="final"', 'value="ungraded"', 'value="graded"'):
            self.assertIn(option, html, 'missing queue option: %s' % option)
        # Participation: live / virtual.
        for option in ('value="live"', 'value="virtual"'):
            self.assertIn(option, html, 'missing participation option: %s' % option)
        # Attempts aggregation: best / latest.
        for option in ('value="best"', 'value="latest"'):
            self.assertIn(option, html, 'missing attempts option: %s' % option)

    def test_pane_mounts_present(self):
        html = self._page_html()
        for element in (
            'id="pane-left-body"', 'id="left-hint"',
            'id="result-body"', 'id="result-hint"',
            'id="review-body"', 'id="review-editor-mount"',
        ):
            self.assertIn(element, html, 'missing pane mount: %s' % element)

    def test_fullscreen_workspace_class(self):
        """html_class / body_class carry ``workspace-page`` (full-screen shell,
        same as the problem workspace page)."""
        self.assertIn('workspace-page', self._page_html())

    def test_initial_empty_states(self):
        html = self._page_html()
        self.assertIn('Select a submission on the left to start grading.', html)
        self.assertIn('Select a submission to see its judging result.', html)

    def test_rich_editor_assets_loaded(self):
        html = self._page_html()
        self.assertIn('rich_editor.css', html)
        self.assertIn('rich_editor.js', html)
        self.assertIn('vendor/katex.min.css', html)

    def test_script_block_and_json_vars_present(self):
        """The single inline script block still carries the |tojson payloads the
        JS renders from (regex contract shared with ContestGradePayloadTestCase)."""
        html = self._page_html()
        self.assertIn('<script type="text/javascript">', html)
        self.assertIsNotNone(re.search(r'var submissions = (\[.*?\]);', html, re.S))
        self.assertIsNotNone(re.search(r'var problems = (\[.*?\]);', html, re.S))

    def test_ai_feedback_table_contract_in_script(self):
        """Grade desk JS must render the same Detailed Feedback columns as the
        submission status page for short-answer / fill-blank."""
        html = self._page_html()
        self.assertIn('gd-ai-grading', html)
        self.assertIn('Detailed Feedback', html)
        self.assertIn("problem_type === 'S'", html)
