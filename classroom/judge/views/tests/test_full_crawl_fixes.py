"""Fixes from the full crawl of every route for every role (docs/classroom/reports/agent-e-full-crawl.md).
Each test fails on the code before its fix."""

from django.contrib.flatpages.models import FlatPage
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import Class, Comment, Contest, Language, Submission, SubmissionSource
from judge.models.tests.util import (CommonDataMixin, create_blogpost, create_contest,
                                     create_contest_participation, create_contest_problem, create_organization,
                                     create_problem, create_user)


class AnonymousCourseFormsTestCase(TestCase):
    """Course forms send visitors to log in; their own checks used to run first and crash (500)."""

    @classmethod
    def setUpTestData(cls):
        cls.org = create_organization(name='fc_course', is_open=False)
        cls.section = Class.objects.create(organization=cls.org, name='Sec 1', slug='sec-1')

    def assertGoesToLogin(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302, url)
        self.assertIn(reverse('auth_login'), response['Location'])

    def test_request_to_join(self):
        self.assertGoesToLogin(reverse('request_organization', args=[self.org.id, self.org.slug]))

    def test_class_edit(self):
        self.assertGoesToLogin(reverse('class_edit', args=[self.org.id, self.org.slug,
                                                           self.section.id, self.section.slug]))

    def test_class_create_and_import(self):
        self.assertGoesToLogin(reverse('organization_create_class', args=[self.org.id, self.org.slug]))
        self.assertGoesToLogin(reverse('organization_import_users', args=[self.org.id, self.org.slug]))


class CommentWithoutHistoryTestCase(TestCase):
    def test_revision_ajax_is_404_not_500(self):
        # Comments made in the admin or by a script have no saved versions.
        author = create_user(username='fc_commenter')
        post = create_blogpost(title='fc-history', visible=True, content='hi')
        comment = Comment.objects.create(author=author.profile, page='b:%d' % post.id, body='hello')
        self.assertTrue(comment.is_accessible_by(author))
        response = self.client.get(reverse('comment_revision_ajax', args=[comment.id]))
        self.assertEqual(response.status_code, 404)


class NetIDButtonTestCase(TestCase):
    @override_settings(AUTH_HOSTS={})
    def test_login_page_hides_netid_when_not_configured(self):
        response = self.client.get(reverse('auth_login'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse('login_netid_redirect'))

    @override_settings(AUTH_HOSTS={'Duke-netid': 'https://sso.example.edu'})
    def test_login_page_shows_netid_when_configured(self):
        self.assertContains(self.client.get(reverse('auth_login')), reverse('login_netid_redirect'))


class PrivateAssignmentSubmissionListsTestCase(CommonDataMixin, TestCase):
    """A course-only assignment's per-user submission lists (and its name) are not shown to outsiders."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.course = create_organization(name='fc_private_course')
        cls.member = create_user(username='fc_member')
        cls.course.members.add(cls.member.profile)
        cls.outsider = create_user(username='fc_outsider')
        cls.contest = create_contest(
            key='fc_private', name='Secret quiz name', is_visible=True, is_organization_private=True,
            organizations=('fc_private_course',), start_time=timezone.now() - timezone.timedelta(days=2),
            end_time=timezone.now() - timezone.timedelta(days=1),
            scoreboard_visibility=Contest.SCOREBOARD_AFTER_CONTEST)
        create_contest_participation(contest=cls.contest, user=cls.member.profile)
        cls.url = reverse('contest_all_user_submissions', args=[cls.contest.key, 'fc_member'])

    def test_anonymous_and_outsider_get_404(self):
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_member_sees_the_list(self):
        self.client.force_login(self.member)
        self.assertContains(self.client.get(self.url), 'Secret quiz name')


class SubmissionPageProblemLinksTestCase(CommonDataMixin, TestCase):
    """A student's own answer to a question they can no longer open (an exam question after the
    exam) must not link to the question or to Resubmit: both are 404 for them."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.student = create_user(username='fc_student')
        cls.closed = create_problem(code='fc_closed', is_public=False)
        cls.open = create_problem(code='fc_open', is_public=True)
        py = Language.objects.get(key='PY3')
        cls.subs = {}
        for problem in (cls.closed, cls.open):
            sub = Submission.objects.create(user=cls.student.profile, problem=problem, language=py, status='D',
                                            result='AC', points=1, case_points=1, case_total=1)
            SubmissionSource.objects.create(submission=sub, source='print(1)')
            cls.subs[problem.code] = sub

    def test_no_dead_links_for_a_closed_question(self):
        self.client.force_login(self.student)
        sub = self.subs['fc_closed']
        for name in ('submission_source', 'submission_status'):
            response = self.client.get(reverse(name, args=[sub.id]))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, reverse('problem_detail', args=['fc_closed']) + '"')
            self.assertNotContains(response, reverse('problem_submit', args=['fc_closed', sub.id]))

    def test_links_stay_for_an_open_question(self):
        self.client.force_login(self.student)
        sub = self.subs['fc_open']
        response = self.client.get(reverse('submission_source', args=[sub.id]))
        self.assertContains(response, reverse('problem_detail', args=['fc_open']) + '"')
        self.assertContains(response, reverse('problem_submit', args=['fc_open', sub.id]))


class ContestSubmissionTabsInsideAnotherContestTestCase(CommonDataMixin, TestCase):
    """While a student sits assignment A, the tabs of assignment B's per-problem lists must not
    link to the problem's site-wide lists (404 while in A)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.student = create_user(username='fc_sitter')
        create_problem(code='fc_p_old', is_public=True)
        create_problem(code='fc_p_now', is_public=True)
        past = timezone.now() - timezone.timedelta(days=5)
        cls.old = create_contest(key='fc_old', is_visible=True, start_time=past,
                                 end_time=past + timezone.timedelta(hours=1))
        create_contest_problem(contest=cls.old, problem='fc_p_old')
        cls.now = create_contest(key='fc_now', is_visible=True)
        create_contest_problem(contest=cls.now, problem='fc_p_now')
        create_contest_participation(contest=cls.old, user=cls.student.profile)
        sitting = create_contest_participation(contest=cls.now, user=cls.student.profile)
        profile = cls.student.profile
        profile.current_contest = sitting
        profile.save()

    def test_tabs_stay_in_the_assignment(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('contest_ranked_submissions', args=['fc_old', 'fc_p_old']))
        self.assertEqual(response.status_code, 200)
        # The problem-wide lists really are closed to this student right now ...
        self.assertEqual(self.client.get(reverse('ranked_submissions', args=['fc_p_old'])).status_code, 404)
        # ... so the page does not link to them.
        self.assertNotContains(response, 'href="%s"' % reverse('ranked_submissions', args=['fc_p_old']))
        self.assertNotContains(response, 'href="%s"' % reverse('chronological_submissions', args=['fc_p_old']))
        self.assertNotContains(response, 'href="%s"' % reverse('user_submissions', args=['fc_p_old', 'fc_sitter']))
        self.assertContains(response, 'href="%s"' % reverse(
            'contest_user_submissions', args=['fc_old', 'fc_sitter', 'fc_p_old']))


class AceFullscreenToggleTestCase(TestCase):
    def test_toggle_is_not_a_link_to_the_parent_path(self):
        from django_ace import AceWidget
        html = AceWidget(mode='python').render('source', '')
        self.assertIn('class="django-ace-max_min"', html)
        self.assertNotIn('href="./"', html)

    def test_toggle_icons_are_not_missing_images(self):
        # ace/img/expand.png and contract.png are not shipped: each toggle was a 404.
        import re
        from pathlib import Path
        from django.conf import settings
        scss = (Path(settings.BASE_DIR) / 'resources' / 'ace-dmoj.scss').read_text()
        self.assertIsNone(re.search(r"url\([^)]*ace/img/", scss))


class SubmitPageWithoutJudgeTestCase(CommonDataMixin, TestCase):
    def test_template_request_needs_a_language(self):
        # With no judge online there is no language list; the page must not ask for a template
        # without a language id (a 404 on every visit).
        user = create_user(username='fc_submitter')
        create_problem(code='fc_code', is_public=True, allowed_languages=('PY3',))
        self.client.force_login(user)
        response = self.client.get(reverse('problem_submit', args=['fc_code']))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'if (!lang_id) return;')


class ReplyLinkForNewUsersTestCase(TestCase):
    def test_no_reply_link_without_a_comment_form(self):
        # Users without a solved problem get "You must solve at least one problem" instead of
        # a comment form, so a Reply link would only clone an empty form (JS error).
        author = create_user(username='fc_author')
        post = create_blogpost(title='fc-post', visible=True, content='hi')
        comment = Comment.objects.create(author=author.profile, page='b:%d' % post.id, body='first')
        reader = create_user(username='fc_reader')
        self.client.force_login(reader)
        response = self.client.get(post.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="comment-%d"' % comment.id)
        self.assertNotContains(response, 'reply_comment(%d)' % comment.id)
        reader.is_staff = True    # staff may always comment
        reader.save()
        self.assertContains(self.client.get(post.get_absolute_url()), 'reply_comment(%d)' % comment.id)


class AdminMenuAddLinksTestCase(TestCase):
    def test_no_add_link_for_models_nobody_may_add(self):
        # Submissions, profiles and point votes have no add permission in their admin; the side
        # menu showed a "+" to them anyway (403).
        admin = create_user(username='fc_admin', is_staff=True, is_superuser=True)
        self.client.force_login(admin)
        response = self.client.get(reverse('admin:index'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="%s"' % reverse('admin:judge_problem_add'))
        for name in ('judge_submission_add', 'judge_profile_add', 'judge_problempointsvote_add'):
            self.assertNotContains(response, 'href="%s"' % reverse('admin:' + name))


class LanguageFixtureEditorModesTestCase(TestCase):
    def test_every_ace_mode_exists(self):
        # "AWK", "pike" and "turing" are not Ace modes: the editor requested mode-AWK.js (404).
        import json
        from pathlib import Path
        from django.conf import settings
        base = Path(settings.BASE_DIR)
        ace = base / 'resources' / 'libs' / 'ace'
        if not ace.is_dir():
            self.skipTest('Ace is not installed (scripts/dev-assets.sh)')
        missing = []
        for name in ('language_small.json', 'language_all.json'):
            for row in json.loads((base / 'judge' / 'fixtures' / name).read_text()):
                mode = row.get('fields', {}).get('ace')
                if mode and not (ace / ('mode-%s.js' % mode)).exists():
                    missing.append('%s: %s' % (name, mode))
        self.assertEqual(missing, [])


class SeedDevDataTestCase(TestCase):
    fixtures = ['navbar']

    def test_seed_creates_the_about_page_the_navbar_links_to(self):
        # (Running the whole seed command here leaves auto-increment ids moved on MariaDB, which
        # breaks other modules' tests; the page is made by this one helper of the command.)
        from judge.management.commands.seed_dev_data import create_about_page
        self.assertEqual(self.client.get('/about/').status_code, 404)
        create_about_page()
        create_about_page()   # safe to run twice
        self.assertEqual(FlatPage.objects.filter(url='/about/').count(), 1)
        self.assertEqual(self.client.get('/about/').status_code, 200)
