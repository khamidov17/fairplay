"""Fixes from clicking through every page as admin, instructor, TA, student and anonymous
(docs/classroom/reports/click-through.md). Each test fails on the code before its fix."""
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import Contest
from judge.models.notification import ensure_instructors_group
from judge.models.tests.util import (CommonDataMixin, create_contest, create_contest_participation,
                                     create_contest_problem, create_organization, create_problem, create_user)

CLASSROOM = Path(settings.BASE_DIR)


class InstructorCanEditOwnAssignmentTestCase(CommonDataMixin, TestCase):
    def test_instructor_opens_the_assignment_they_created(self):
        teacher = create_user(username='ct_teacher')
        teacher.groups.add(ensure_instructors_group())
        teacher = User.objects.get(id=teacher.id)  # drop the permission cache
        contest = create_contest(key='ctown', authors=('ct_teacher',))
        self.client.force_login(teacher)
        self.assertEqual(self.client.get(reverse('contest_edit', args=[contest.key])).status_code, 200)


class NetIDNotConfiguredTestCase(TestCase):
    @override_settings(AUTH_HOSTS={})
    def test_netid_button_explains_instead_of_500(self):
        response = self.client.get(reverse('login_netid_redirect'))
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, 'NetID sign-in is not configured', status_code=503)


class LocalAssetsTestCase(TestCase):
    """Nothing is loaded from a public CDN or gravatar.com (blocked or slow in mainland China)."""

    EXTERNAL = re.compile(r'(cdnjs\.cloudflare\.com|cdn\.jsdelivr\.net|unpkg\.com|ajax\.googleapis\.com|'
                          r'fonts\.googleapis\.com|static\.dmoj\.ca|gravatar\.com)')

    def test_templates_and_scripts_reference_no_cdn(self):
        offenders = []
        roots = [CLASSROOM / 'templates', CLASSROOM / 'martor' / 'templates', CLASSROOM / 'martor' / 'static',
                 CLASSROOM / 'resources']
        for root in roots:
            for path in root.rglob('*'):
                if path.suffix not in ('.html', '.js', '.css', '.scss') or 'libs' in path.parts:
                    continue
                if path.name.endswith('.min.js') or path.name in ('rich_editor.js',):
                    continue
                text = path.read_text(errors='ignore')
                for line_no, line in enumerate(text.splitlines(), 1):
                    if self.EXTERNAL.search(line) and 'never' not in line and 'upstream image' not in line:
                        offenders.append('%s:%d' % (path.relative_to(CLASSROOM), line_no))
        self.assertEqual(offenders, [])

    def test_library_urls_default_to_this_site(self):
        from judge.utils.assets import ace_url
        from judge.widgets.select2 import AdminSelect2Widget
        self.assertTrue(ace_url().startswith(settings.STATIC_URL))
        media = str(AdminSelect2Widget().media)
        self.assertIn(settings.STATIC_URL + 'libs/select2/select2.js', media)
        self.assertNotIn('//cdnjs', media)

    def test_avatar_is_drawn_locally_unless_gravatar_is_on(self):
        from judge.jinja2.gravatar import avatar_icon_url, gravatar
        user = create_user(username='ct_avatar', first_name='Zara', email='z@example.edu')
        url = gravatar(user.profile, 32)
        self.assertTrue(url.startswith('data:image/svg+xml'))
        self.assertIn('%3EZ%3C', url)   # the initial
        self.assertIsNone(avatar_icon_url(user.email))
        with override_settings(DMOJ_USE_GRAVATAR=True):
            self.assertTrue(gravatar(user.profile).startswith('https://www.gravatar.com/'))


class DeadLinksTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.teacher = create_user(username='ct_rank_teacher', user_permissions=('edit_own_contest',))
        cls.student = create_user(username='ct_rank_student')
        cls.problem = create_problem(code='ctrank', is_public=True)
        cls.exam = create_contest(key='ctexam', is_visible=True, authors=('ct_rank_teacher',),
                                  scoreboard_visibility=Contest.SCOREBOARD_AFTER_CONTEST,
                                  start_time=timezone.now() - timedelta(hours=1),
                                  end_time=timezone.now() + timedelta(hours=1))
        create_contest_problem(contest=cls.exam, problem=cls.problem, points=10, order=1)
        create_contest_participation(contest=cls.exam, user=cls.student.profile)

    def test_no_best_solutions_link_while_the_scoreboard_is_hidden(self):
        rank = reverse('contest_ranked_submissions', args=[self.exam.key, self.problem.code])
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(rank).status_code, 404)
        self.assertNotContains(self.client.get(reverse('contest_view', args=[self.exam.key])), rank)

    def test_editor_opens_best_solutions_before_the_start(self):
        Contest.objects.filter(id=self.exam.id).update(start_time=timezone.now() + timedelta(days=2),
                                                       end_time=timezone.now() + timedelta(days=3))
        self.client.force_login(self.teacher)
        rank = reverse('contest_ranked_submissions', args=[self.exam.key, self.problem.code])
        self.assertEqual(self.client.get(rank).status_code, 200)

    def test_no_view_link_to_a_classmates_answer(self):
        # "Solved" source visibility is for code only (G-6); the row must not offer the link.
        from judge.jinja2.submission import submission_layout
        from judge.models import Language, Submission
        mcq = create_problem(code='ctmcq', submission_type='M', is_public=True,
                             submission_source_visibility_mode='S')
        sub = Submission.objects.create(user=self.users['normal'].profile, problem=mcq,
                                        language=Language.get_answer_language(), status='D', result='AC')
        viewer = self.student
        can_view, _ = submission_layout(sub, viewer.profile.id, viewer, {mcq.id}, set(), set())
        self.assertFalse(can_view)
        self.assertFalse(sub.can_see_detail(viewer))

    def test_new_question_menu_needs_the_permission(self):
        from judge.subjects.access import menu_items_for
        course = create_organization(name='ctcourse')
        ta = create_user(username='ct_ta')
        keys = [item.key for item, _href in menu_items_for(ta, course, is_teacher=True)]
        self.assertNotIn('new_question', keys)
        teacher = create_user(username='ct_qteacher', user_permissions=('create_private_problem',))
        keys = [item.key for item, _href in menu_items_for(teacher, course, is_teacher=True)]
        self.assertIn('new_question', keys)


class AdminPagesTestCase(CommonDataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.users['superuser'])

    def test_change_list_has_no_removed_jquery_actions_call(self):
        response = self.client.get('/admin/judge/problem/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '.actions(')

    def test_admin_user_menu_avatar_is_local(self):
        response = self.client.get('/admin/')
        self.assertNotContains(response, 'gravatar.com')


class CommentEditPageTestCase(CommonDataMixin, TestCase):
    def test_comment_edit_page_renders_the_editor(self):
        from judge.models import Comment
        author = self.users['normal']
        create_problem(code='ctcomment', is_public=True)
        comment = Comment.objects.create(author=author.profile, page='p:ctcomment', body='hello')
        self.client.force_login(author)
        response = self.client.get(reverse('comment_edit', args=[comment.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'main-martor')
        self.assertContains(response, 'id="id_edit"')


class TestDataPageTestCase(CommonDataMixin, TestCase):
    def test_no_view_yaml_link_without_a_generated_init_yml(self):
        teacher = create_user(username='ct_data_teacher', user_permissions=('edit_own_problem',))
        problem = create_problem(code='ctdata', authors=('ct_data_teacher',))
        self.client.force_login(teacher)
        response = self.client.get(reverse('problem_data', args=[problem.code]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse('problem_data_init', args=[problem.code]))

    def test_short_answer_links_to_the_question_editor(self):
        teacher = create_user(username='ct_sa_teacher', user_permissions=('edit_own_problem',))
        problem = create_problem(code='ctsa', submission_type='S', is_public=True, authors=('ct_sa_teacher',))
        self.client.force_login(teacher)
        response = self.client.get(reverse('problem_detail', args=[problem.code]))
        self.assertNotContains(response, reverse('problem_data', args=[problem.code]))
