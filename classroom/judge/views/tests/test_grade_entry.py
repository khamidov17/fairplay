"""Grade-desk entry points: ranking cells and the submission list/status “grade” link.

1. Contest editors clicking a ranking problem cell land on that assignment’s
   Grade page with the scoring submission selected. Students still link ranking
   cells to the per-user submission list (including classmates).
2. The submission-page grade link opens practice Grade (all practice rows
   the viewer can edit) or the assignment Grade page, matching the
   submission’s contest_object.
"""
import json
from datetime import timedelta
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import ContestParticipation, ContestSubmission, Language, Submission, SubmissionSource
from judge.models.tests.util import CommonDataMixin, create_contest, create_contest_problem, \
    create_problem, create_user
from judge.utils import contest_grade
from judge.views.tests.test_contest_grade import GradeTestBase


def _grade_payload(html):
    return json.loads(html.split('var submissions = ')[1].split(';\n')[0])


class RankingGradeDeepLinkTestCase(GradeTestBase):
    """Editors’ ranking problem cells deep-link into Grade; students do not."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.contest.is_visible = True
        cls.contest.save(update_fields=['is_visible'])
        cls.participation.recompute_results()
        cls.ranking_url = reverse('contest_ranking', args=[cls.contest.key])
        cls.grade_href = '%s?submission=%d' % (
            reverse('contest_grade', args=[cls.contest.key]), cls.submission.id)
        cls.user_subs_href = reverse(
            'contest_user_submissions',
            args=[cls.contest.key, cls.student.username, cls.problem.code],
        )

    def test_editor_ranking_cell_opens_grade_with_submission(self):
        response = self._client(self.curator).get(self.ranking_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(self.grade_href, html)
        self.assertNotIn(self.user_subs_href, html)

    def test_student_ranking_own_cell_may_link_user_submissions(self):
        response = self._client(self.student).get(self.ranking_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(self.user_subs_href, html)
        self.assertNotIn(self.grade_href, html)

    def test_student_ranking_other_cell_links_user_submissions(self):
        classmate = create_user(username='grade_rank_classmate')
        part = ContestParticipation.objects.create(
            contest=self.contest, user=classmate.profile,
            real_start=timezone.now() - timedelta(hours=1),
        )
        sub = Submission.objects.create(
            user=classmate.profile, problem=self.problem,
            language=Language.objects.get(key='PY3'),
            result='AC', status='D', contest_object=self.contest,
            points=80.0, case_total=100,
        )
        ContestSubmission.objects.create(
            submission=sub, problem=self.contest_problem,
            participation=part, points=80.0,
        )
        part.recompute_results()
        classmate_href = reverse(
            'contest_user_submissions',
            args=[self.contest.key, classmate.username, self.problem.code],
        )
        response = self._client(self.student).get(self.ranking_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(classmate_href, html)
        self.assertIn(self.user_subs_href, html)
        self.assertNotIn(self.grade_href, html)

    def test_grade_page_deep_link_js_clears_filters(self):
        html = self._client(self.curator).get(self.grade_url).content.decode()
        self.assertIn("$('#f-queue').val('all')", html)
        self.assertIn("$('#f-part').val('all')", html)

    def test_ranking_scoring_map_is_scoped_to_rendered_participations(self):
        """A per-user participation page must not scan the whole contest."""
        classmate = create_user(username='grade_rank_scoped')
        other_part = ContestParticipation.objects.create(
            contest=self.contest, user=classmate.profile,
            real_start=timezone.now() - timedelta(hours=1),
        )
        other_part.recompute_results()

        seen = []
        real = contest_grade.scoring_submissions_for_contest

        def spy(contest, live_only=False, participation_ids=None):
            seen.append(participation_ids)
            return real(contest, live_only=live_only, participation_ids=participation_ids)

        with mock.patch.object(contest_grade, 'scoring_submissions_for_contest', spy):
            response = self._client(self.curator).get(
                reverse('contest_participation', args=[self.contest.key, self.student.username]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(seen, 'editor ranking should build a scoping map')
        for ids in seen:
            self.assertIsNotNone(ids, 'ranking must scope the scan, not walk the whole contest')
            self.assertNotIn(other_part.id, ids)

    def test_ranking_scoring_map_not_built_for_students(self):
        seen = []
        real = contest_grade.scoring_submissions_for_contest

        def spy(contest, live_only=False, participation_ids=None):
            seen.append(participation_ids)
            return real(contest, live_only=live_only, participation_ids=participation_ids)

        with mock.patch.object(contest_grade, 'scoring_submissions_for_contest', spy):
            response = self._client(self.student).get(self.ranking_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, [])


class SubmissionGradeEntryTestCase(CommonDataMixin, TestCase):
    """List + status grade links, and the practice vs assignment Grade lists."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.teacher = create_user(
            username='grade_entry_teacher',
            user_permissions=('edit_own_problem', 'edit_own_contest'),
        )
        cls.other_teacher = create_user(
            username='grade_entry_other',
            user_permissions=('edit_own_problem',),
        )
        cls.student = create_user(username='grade_entry_student')
        cls.practice_problem = create_problem(
            code='grade-entry-prac', name='Practice Prob', is_public=True, points=100,
            authors=('grade_entry_teacher',),
        )
        cls.other_problem = create_problem(
            code='grade-entry-other', name='Other Practice', is_public=True, points=100,
            authors=('grade_entry_other',),
        )
        cls.assignment_problem = create_problem(
            code='grade-entry-asgn', name='Assignment Prob', is_public=True, points=100,
        )
        lang = Language.get_python3()
        now = timezone.now()
        cls.practice_sub = Submission.objects.create(
            user=cls.student.profile, problem=cls.practice_problem,
            language=lang, result='AC', status='D', points=100, case_total=100,
            time=0.1, memory=1024,
        )
        SubmissionSource.objects.create(submission=cls.practice_sub, source='print(1)')
        cls.practice_older = Submission.objects.create(
            user=cls.student.profile, problem=cls.practice_problem,
            language=lang, result='WA', status='D', points=0, case_total=100,
            time=0.1, memory=1024,
        )
        Submission.objects.filter(id=cls.practice_older.id).update(date=now - timedelta(hours=2))
        SubmissionSource.objects.create(submission=cls.practice_older, source='print(0)')
        cls.other_practice_sub = Submission.objects.create(
            user=cls.student.profile, problem=cls.other_problem,
            language=lang, result='AC', status='D', points=50, case_total=100,
            time=0.1, memory=1024,
        )
        SubmissionSource.objects.create(submission=cls.other_practice_sub, source='print(2)')

        cls.contest = create_contest(
            key='gradeentry', name='Grade Entry HW', is_visible=True,
            start_time=now - timedelta(days=1),
            end_time=now + timedelta(days=1),
            authors=('grade_entry_teacher',),
        )
        cp = create_contest_problem(contest=cls.contest, problem=cls.assignment_problem)
        part = ContestParticipation.objects.create(
            contest=cls.contest, user=cls.student.profile, real_start=now - timedelta(hours=1),
        )
        cls.contest_sub = Submission.objects.create(
            user=cls.student.profile, problem=cls.assignment_problem,
            language=lang, result='WA', status='D', points=40, case_total=100,
            contest_object=cls.contest, time=0.1, memory=1024,
        )
        SubmissionSource.objects.create(submission=cls.contest_sub, source='print(3)')
        ContestSubmission.objects.create(
            submission=cls.contest_sub, problem=cp, participation=part, points=40,
        )
        cls.practice_grade_url = reverse('practice_grade')
        cls.contest_grade_url = reverse('contest_grade', args=[cls.contest.key])

    def _client(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_list_grade_link_practice(self):
        content = self._client(self.teacher).get(reverse('all_submissions')).content
        expected = ('%s?submission=%d' % (self.practice_grade_url, self.practice_sub.id)).encode()
        self.assertIn(expected, content)
        self.assertIn(b'sub-grade-link', content)

    def test_list_grade_link_assignment(self):
        content = self._client(self.teacher).get(reverse('all_submissions')).content
        expected = ('%s?submission=%d' % (self.contest_grade_url, self.contest_sub.id)).encode()
        self.assertIn(expected, content)

    def test_list_grade_link_hidden_for_student(self):
        content = self._client(self.student).get(reverse('all_submissions')).content
        self.assertNotIn(b'sub-grade-link', content)

    def test_status_grade_link_practice_and_assignment(self):
        teacher = self._client(self.teacher)
        prac = teacher.get(reverse('submission_status', args=[self.practice_sub.id]))
        self.assertEqual(prac.status_code, 200)
        self.assertContains(prac, '%s?submission=%d' % (self.practice_grade_url, self.practice_sub.id))

        asgn = teacher.get(reverse('submission_status', args=[self.contest_sub.id]))
        self.assertEqual(asgn.status_code, 200)
        self.assertContains(asgn, '%s?submission=%d' % (self.contest_grade_url, self.contest_sub.id))

        student = self._client(self.student).get(reverse('submission_status', args=[self.practice_sub.id]))
        self.assertNotContains(student, 'sub-grade-link')

    def test_practice_grade_lists_only_editable_practice(self):
        response = self._client(self.teacher).get(
            self.practice_grade_url + '?submission=%d' % self.practice_sub.id)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('Practice Grade', html)
        self.assertNotIn('id="f-part"', html)
        payload = _grade_payload(html)
        ids = {row['id'] for row in payload}
        self.assertIn(self.practice_sub.id, ids)
        self.assertIn(self.practice_older.id, ids)
        self.assertNotIn(self.other_practice_sub.id, ids)
        self.assertNotIn(self.contest_sub.id, ids)
        by_id = {row['id']: row for row in payload}
        self.assertTrue(by_id[self.practice_sub.id]['is_latest'])
        self.assertFalse(by_id[self.practice_older.id]['is_latest'])

    def test_assignment_grade_lists_only_that_assignment(self):
        response = self._client(self.teacher).get(
            self.contest_grade_url + '?submission=%d' % self.contest_sub.id)
        self.assertEqual(response.status_code, 200)
        ids = {row['id'] for row in _grade_payload(response.content.decode())}
        self.assertIn(self.contest_sub.id, ids)
        self.assertNotIn(self.practice_sub.id, ids)

    def test_practice_grade_forbidden_for_student(self):
        self.assertEqual(self._client(self.student).get(self.practice_grade_url).status_code, 404)
        save = self._client(self.student).post(reverse('practice_grade_save'), {
            'submission': self.practice_sub.id, 'score': '10',
        })
        self.assertEqual(save.status_code, 403)

    def test_practice_grade_save_rejects_assignment_submission(self):
        response = self._client(self.teacher).post(reverse('practice_grade_save'), {
            'submission': self.contest_sub.id, 'score': '10',
        })
        self.assertEqual(response.status_code, 404)

    def test_practice_grade_page_is_bounded_and_says_so(self):
        """The desk inlines full source per row, so it caps the list and says it did."""
        with override_settings(DMOJ_PRACTICE_GRADE_PAGE_SIZE=1):
            response = self._client(self.teacher).get(self.practice_grade_url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        payload = _grade_payload(html)
        self.assertEqual([row['id'] for row in payload], [self.practice_sub.id])
        self.assertIn('most recent submissions', html)

    def test_practice_grade_page_pins_deep_link_outside_the_window(self):
        """A ?submission= link to an older row still opens on that row."""
        with override_settings(DMOJ_PRACTICE_GRADE_PAGE_SIZE=1):
            response = self._client(self.teacher).get(
                self.practice_grade_url + '?submission=%d' % self.practice_older.id)
        self.assertEqual(response.status_code, 200)
        payload = _grade_payload(response.content.decode())
        by_id = {row['id']: row for row in payload}
        self.assertIn(self.practice_older.id, by_id)
        # Pinned from outside the window: it is not this pair's final version.
        self.assertFalse(by_id[self.practice_older.id]['is_latest'])
        self.assertTrue(by_id[self.practice_sub.id]['is_latest'])

    def test_practice_grade_save_reaches_rows_outside_the_page_window(self):
        """The page cap must not shrink what Save is allowed to grade."""
        with override_settings(DMOJ_PRACTICE_GRADE_PAGE_SIZE=1):
            response = self._client(self.teacher).post(reverse('practice_grade_save'), {
                'submission': self.practice_older.id, 'score': '5',
            })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['score'], 5)

    def test_practice_grade_save_rejects_non_finite_score(self):
        for value in ('nan', 'inf', '-inf'):
            response = self._client(self.teacher).post(reverse('practice_grade_save'), {
                'submission': self.practice_sub.id, 'score': value,
            })
            self.assertEqual(response.status_code, 400, value)

    def test_practice_grade_save_updates_score(self):
        response = self._client(self.teacher).post(reverse('practice_grade_save'), {
            'submission': self.practice_sub.id, 'score': '80', 'note': 'ok',
        })
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['score'], 80)
        self.practice_sub.refresh_from_db()
        self.assertEqual(self.practice_sub.review_note, 'ok')
