from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import Language, ProblemSubmissionType, ProblemTranslation, Submission, SubmissionSource
from judge.models.tests.util import CommonDataMixin, create_contest, create_contest_participation, \
    create_contest_problem, create_problem, create_user
from judge.views.submission import _submission_contest_problem_name, attach_contest_problem_display_names


class SubmissionListFilterTest(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.users.update({
            'alice': create_user(username='alice'),
            'bob': create_user(username='bob'),
            'staff_rejudge_only': create_user(
                username='staff_rejudge_only',
                is_staff=True,
                user_permissions=('rejudge_submission',),
            ),
        })
        cls.problem_a = create_problem(code='alpha', name='Alpha Problem', is_public=True)
        cls.problem_a.authors.set([cls.users['staff_problem_edit_own'].profile])
        cls.problem_a.curators.set([cls.users['staff_problem_edit_own'].profile])
        cls.problem_b = create_problem(code='beta', name='Beta Problem', is_public=True)
        cls.problem_mcq = create_problem(
            code='mcq1', name='MCQ Problem', is_public=True,
            submission_type=ProblemSubmissionType.MCQ,
        )
        lang = Language.get_python3()
        answer_lang = Language.get_answer_language()

        cls.sub_alice_a = Submission.objects.create(
            user=cls.users['alice'].profile,
            problem=cls.problem_a,
            language=lang,
            result='AC',
            status='D',
            case_points=100,
            case_total=100,
            time=0.12,
            memory=2048,
        )
        cls.sub_bob_b = Submission.objects.create(
            user=cls.users['bob'].profile,
            problem=cls.problem_b,
            language=lang,
            result='WA',
            status='D',
            case_points=0,
            case_total=100,
            time=0.05,
            memory=1024,
        )
        cls.sub_mcq = Submission.objects.create(
            user=cls.users['alice'].profile,
            problem=cls.problem_mcq,
            language=answer_lang,
            result='AC',
            status='D',
            case_points=100,
            case_total=100,
            time=0,
            memory=0,
        )

        cls.contest = create_contest(
            key='fall25',
            name='Fall Contest',
            is_visible=True,
            start_time=timezone.now() - timezone.timedelta(days=1),
            end_time=timezone.now() + timezone.timedelta(days=1),
        )
        create_contest_problem(contest=cls.contest, problem=cls.problem_a)
        create_contest_participation(contest=cls.contest, user=cls.users['alice'].profile)
        cls.sub_contest = Submission.objects.create(
            user=cls.users['alice'].profile,
            problem=cls.problem_a,
            language=lang,
            result='AC',
            status='D',
            case_points=100,
            case_total=100,
            time=0.2,
            memory=4096,
            contest_object=cls.contest,
        )

    def _get(self, query=''):
        self.client.force_login(self.users['superuser'])
        return self.client.get(reverse('all_submissions') + query)

    def test_redesign_chrome_present(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        content = response.content
        self.assertIn(b'submission-list-redesign', content)
        self.assertIn(b'sub-result', content)
        self.assertIn(b'filter-problem', content)
        self.assertIn(b'sub-filters', content)
        self.assertNotIn(b'sub-staff-bar', content)
        self.assertIn(b'sub-row-check', content)
        self.assertIn(b'sub-grade-link', content)
        self.assertIn(b'btn-bulk-rejudge', content)
        self.assertIn(b'btn-bulk-rescore', content)
        # Bulk actions sit in the filter toolbar, not a separate title row.
        self.assertLess(content.find(b'id="filter-form"'), content.find(b'id="btn-bulk-rejudge"'))
        self.assertLess(content.find(b'id="btn-bulk-rejudge"'), content.find(b'id="submissions-table"'))

    def test_filter_by_problem_code(self):
        content = self._get('?problem=alpha').content
        self.assertIn(b'alpha', content)
        self.assertNotIn(('id="%d"' % self.sub_bob_b.id).encode(), content)

    def test_filter_by_multiple_problems(self):
        content = self._get('?problem=alpha&problem=beta').content
        self.assertIn(('id="%d"' % self.sub_alice_a.id).encode(), content)
        self.assertIn(('id="%d"' % self.sub_bob_b.id).encode(), content)
        self.assertNotIn(('id="%d"' % self.sub_mcq.id).encode(), content)

    def test_filter_by_user(self):
        content = self._get('?user=alice').content
        self.assertIn(b'alice', content)
        # Bob's beta submission should be filtered out of the table rows.
        self.assertNotIn(('id="%d"' % self.sub_bob_b.id).encode(), content)

    def test_filter_by_contest_key(self):
        content = self._get('?contest=fall25').content
        self.assertIn(('id="%d"' % self.sub_contest.id).encode(), content)
        self.assertNotIn(('id="%d"' % self.sub_bob_b.id).encode(), content)

    def test_filter_practice_only(self):
        content = self._get('?contest=__none__').content
        self.assertIn(('id="%d"' % self.sub_alice_a.id).encode(), content)
        self.assertIn(('id="%d"' % self.sub_bob_b.id).encode(), content)
        self.assertNotIn(('id="%d"' % self.sub_contest.id).encode(), content)

    def test_filter_by_status(self):
        content = self._get('?status=WA').content
        self.assertIn(('id="%d"' % self.sub_bob_b.id).encode(), content)
        self.assertNotIn(('id="%d"' % self.sub_alice_a.id).encode(), content)

    def test_non_code_usage_shows_dashes(self):
        content = self._get('?problem=mcq1').content
        row_marker = ('id="%d"' % self.sub_mcq.id).encode()
        self.assertIn(row_marker, content)
        # Non-programming rows should not show numeric time like "0.00s".
        start = content.find(row_marker)
        end = content.find(b'class="submission-row"', start + 1)
        row_html = content[start:end if end != -1 else None]
        self.assertIn(b'<div class="time">---</div>', row_html)
        self.assertIn(b'<div class="memory">---</div>', row_html)
        self.assertNotIn(b'0.00s', row_html)

    def test_staff_bar_hidden_for_normal_user(self):
        self.client.force_login(self.users['normal'])
        content = self.client.get(reverse('all_submissions')).content
        self.assertNotIn(b'sub-staff-bar', content)
        self.assertNotIn(b'sub-row-check', content)
        self.assertNotIn(b'sub-grade-link', content)
        self.assertNotIn(b'Practice Grade', content)
        self.assertNotIn(b'btn-bulk-rejudge', content)
        self.assertNotIn(b'btn-bulk-rescore', content)

    def test_staff_bar_hidden_without_edit_own_problem(self):
        """rejudge_submission alone is not enough for bulk UI (matches bulk_rejudge)."""
        self.client.force_login(self.users['staff_rejudge_only'])
        content = self.client.get(reverse('all_submissions')).content
        self.assertNotIn(b'sub-staff-bar', content)
        self.assertNotIn(b'sub-row-check', content)
        self.assertNotIn(b'btn-bulk-rejudge', content)
        self.assertNotIn(b'btn-bulk-rescore', content)

    def test_grade_hidden_for_student_own_submission(self):
        self.client.force_login(self.users['alice'])
        content = self.client.get(
            reverse('all_user_submissions', kwargs={'user': 'alice'}),
        ).content
        self.assertIn(('id="%d"' % self.sub_alice_a.id).encode(), content)
        self.assertNotIn(b'sub-grade-link', content)
        self.assertNotIn(b'Practice Grade', content)

    def test_ajax_filter_select2_endpoints(self):
        self.client.force_login(self.users['superuser'])
        problem = self.client.get(reverse('submission_problem_filter_select2'), {'term': 'alpha'})
        self.assertEqual(problem.status_code, 200)
        problem_ids = [row['id'] for row in problem.json()['results']]
        self.assertIn('alpha', problem_ids)

        contest = self.client.get(reverse('submission_contest_filter_select2'), {'term': ''})
        self.assertEqual(contest.status_code, 200)
        contest_ids = [row['id'] for row in contest.json()['results']]
        self.assertIn('__none__', contest_ids)
        self.assertIn('fall25', contest_ids)

    def test_bulk_rescore(self):
        self.client.force_login(self.users['staff_problem_edit_all_with_rejudge'])
        self.sub_alice_a.points = 0
        self.sub_alice_a.save(update_fields=['points'])
        response = self.client.post(reverse('submission_bulk_rescore'), {'ids': [self.sub_alice_a.id]})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['processed'], 1)
        self.sub_alice_a.refresh_from_db()
        self.assertEqual(self.sub_alice_a.points, self.problem_a.points)

    def test_bulk_rescore_scoped_to_owned_problems(self):
        """Limited staff may only rescore submissions for problems they edit."""
        staff = self.users['staff_problem_edit_own']
        self.client.force_login(staff)

        self.sub_alice_a.points = 0
        self.sub_alice_a.save(update_fields=['points'])
        self.sub_bob_b.points = 0
        self.sub_bob_b.save(update_fields=['points'])

        # Out of scope (problem B has no author/curator link to staff).
        response = self.client.post(reverse('submission_bulk_rescore'), {'ids': [self.sub_bob_b.id]})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['processed'], 0)
        self.sub_bob_b.refresh_from_db()
        self.assertEqual(self.sub_bob_b.points, 0)

        # In scope (staff is author+curator of problem A); distinct avoids double-count.
        response = self.client.post(
            reverse('submission_bulk_rescore'),
            {'ids': [self.sub_alice_a.id, self.sub_bob_b.id]},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['processed'], 1)
        self.sub_alice_a.refresh_from_db()
        self.sub_bob_b.refresh_from_db()
        self.assertEqual(self.sub_alice_a.points, self.problem_a.points)
        self.assertEqual(self.sub_bob_b.points, 0)

    @override_settings(DMOJ_SUBMISSIONS_REJUDGE_LIMIT=1)
    def test_bulk_rescore_lot_limit(self):
        """Without rejudge_submission_lot, rescoring more than the limit is forbidden."""
        self.client.force_login(self.users['staff_problem_edit_all_with_rejudge'])
        response = self.client.post(
            reverse('submission_bulk_rescore'),
            {'ids': [self.sub_alice_a.id, self.sub_bob_b.id]},
        )
        self.assertEqual(response.status_code, 403)
        data = response.json()
        self.assertFalse(data['ok'])
        self.assertEqual(data['processed'], 0)
        self.assertIn('THAT many', data['message'])

    def test_filter_form_uses_first_page_href(self):
        response = self._get('?status=AC')
        self.assertEqual(response.status_code, 200)
        # DiggPaginatorMixin sets first_page_href to '.' (+ current query) so filters
        # reset to page 1 instead of submitting against /submissions/<page>.
        self.assertContains(response, 'id="filter-form"', html=False)
        self.assertContains(response, 'action=".?status=AC"')
        self.assertContains(response, 'href=".?status=AC"')
        self.assertNotContains(response, 'action="/submissions/"')

    def test_bulk_rejudge_requires_permission(self):
        self.client.force_login(self.users['normal'])
        response = self.client.post(reverse('submission_bulk_rejudge'), {'ids': [self.sub_alice_a.id]})
        self.assertEqual(response.status_code, 403)


class SubmissionTitleConsistencyTest(CommonDataMixin, TestCase):
    """List, detail, and poll rows must resolve the same problem title."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.problem = create_problem(code='title-i18n', name='Canonical Title', is_public=True)
        ProblemTranslation.objects.create(
            problem=cls.problem, language='zh-hans', name='中文标题', description='d',
        )
        cls.user = create_user(username='title_student')
        lang = Language.get_python3()
        cls.practice = Submission.objects.create(
            user=cls.user.profile, problem=cls.problem, language=lang,
            result='AC', status='D', case_points=100, case_total=100,
            time=0.1, memory=2048,
        )
        cls.contest = create_contest(
            key='titleasgn',
            name='Title Assignment',
            is_visible=True,
            start_time=timezone.now() - timezone.timedelta(days=1),
            end_time=timezone.now() + timezone.timedelta(days=1),
        )
        create_contest_problem(
            contest=cls.contest, problem=cls.problem, display_name='Homework Alias',
        )
        create_contest_participation(contest=cls.contest, user=cls.user.profile)
        cls.assignment = Submission.objects.create(
            user=cls.user.profile, problem=cls.problem, language=lang,
            result='AC', status='D', case_points=100, case_total=100,
            time=0.1, memory=2048, contest_object=cls.contest,
        )
        SubmissionSource.objects.create(submission=cls.practice, source='print(1)')
        SubmissionSource.objects.create(submission=cls.assignment, source='print(1)')

    def test_list_fallback_matches_detail_when_translation_not_prefetched(self):
        # No _trans / _i18n_name: list used to fall back to the canonical name.
        listed = attach_contest_problem_display_names([self.practice], 'zh-hans')
        self.assertEqual(listed[0].effective_problem_name, '中文标题')
        self.assertEqual(
            _submission_contest_problem_name(self.practice, 'zh-hans'),
            listed[0].effective_problem_name,
        )

    def test_list_uses_prefetched_translation(self):
        self.practice.problem._trans = list(self.problem.translations.filter(language='zh-hans'))
        listed = attach_contest_problem_display_names([self.practice], 'zh-hans')
        self.assertEqual(listed[0].effective_problem_name, '中文标题')

    def test_assignment_alias_matches_on_list_and_detail(self):
        listed = attach_contest_problem_display_names([self.assignment], 'zh-hans')
        self.assertEqual(listed[0].effective_problem_name, 'Homework Alias')
        self.assertEqual(
            _submission_contest_problem_name(self.assignment, 'zh-hans'),
            'Homework Alias',
        )

    def test_pages_and_poll_show_the_same_assignment_alias(self):
        self.client.force_login(self.users['superuser'])
        listing = self.client.get(reverse('all_submissions'))
        detail = self.client.get(reverse('submission_status', args=[self.assignment.id]))
        poll = self.client.get(reverse('submission_single_query'), {'id': self.assignment.id})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(poll.status_code, 200)
        row_marker = ('id="%d"' % self.assignment.id).encode()
        content = listing.content
        start = content.find(row_marker)
        end = content.find(b'class="submission-row"', start + 1)
        row_html = content[start:end if end != -1 else None]
        self.assertIn(b'Homework Alias', row_html)
        self.assertNotIn(b'Canonical Title', row_html)
        self.assertContains(detail, 'Homework Alias')
        self.assertContains(poll, 'Homework Alias')
