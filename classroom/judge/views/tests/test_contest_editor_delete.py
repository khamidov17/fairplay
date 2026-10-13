"""Delete control on the frontend add/edit assignment Save row.

Mirrors Django admin ContestAdmin single-object delete:
``judge.delete_contest`` plus edit access (``Contest.is_deletable_by``).
Add has no object, so there is no Delete — same as admin's add form.
Kept in this file so list-v2 bulk-delete tests can land without conflict.
"""

from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import Contest
from judge.models.tests.util import CommonDataMixin, create_contest, create_user


def _bottombar_html(html):
    marker = 'id="bottombar"'
    start = html.index(marker)
    div_start = html.rfind('<div', 0, start)
    return html[div_start:html.index('</div>', start) + len('</div>')]


class ContestEditorDeleteButtonTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.deleter = create_user(
            username='editor_page_deleter',
            is_staff=True,
            user_permissions=(
                'add_contest', 'edit_own_contest', 'edit_all_contest', 'delete_contest',
                'create_private_contest',
            ),
        )
        cls.editor = create_user(
            username='editor_page_no_delete',
            is_staff=True,
            user_permissions=(
                'add_contest', 'edit_own_contest', 'edit_all_contest', 'create_private_contest',
            ),
        )
        cls.own_deleter = create_user(
            username='editor_page_own_deleter',
            is_staff=True,
            user_permissions=('edit_own_contest', 'delete_contest'),
        )
        cls.student = create_user(username='editor_page_delete_student')
        _now = timezone.now()
        cls.contest = create_contest(
            key='editor_del_target',
            name='Editor Delete Target',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=1),
            is_visible=True,
            authors=('editor_page_deleter',),
            curators=('editor_page_own_deleter',),
        )
        cls.other = create_contest(
            key='editor_del_other',
            name='Editor Delete Other',
            start_time=_now - timedelta(days=1),
            end_time=_now + timedelta(days=1),
            is_visible=True,
            authors=('editor_page_deleter',),
        )

    def setUp(self):
        self.client = Client()

    def test_edit_shows_delete_when_deletable(self):
        self.client.force_login(self.deleter)
        resp = self.client.get(reverse('contest_edit', args=[self.contest.key]))
        self.assertEqual(resp.status_code, 200)
        bar = _bottombar_html(resp.content.decode())
        self.assertIn('id="editor-assignment-delete"', bar)
        self.assertIn('fa-trash', bar)
        self.assertIn(reverse('contest_delete', args=[self.contest.key]), resp.content.decode())
        self.assertContains(resp, 'id="editor-assignment-delete-form"')
        self.assertContains(resp, 'name="next"')
        self.assertIn('value="%s"' % reverse('contest_list'), resp.content.decode())

    def test_edit_hides_delete_without_delete_contest(self):
        self.client.force_login(self.editor)
        resp = self.client.get(reverse('contest_edit', args=[self.contest.key]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        bar = _bottombar_html(html)
        self.assertNotIn('id="editor-assignment-delete"', bar)
        self.assertNotIn(reverse('contest_delete', args=[self.contest.key]), html)

    def test_add_hides_delete_even_with_delete_contest(self):
        self.client.force_login(self.deleter)
        resp = self.client.get(reverse('contest_create'))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        bar = _bottombar_html(html)
        self.assertNotIn('id="editor-assignment-delete"', bar)
        self.assertNotIn('editor-assignment-delete-form', html)
        self.assertNotIn('/delete', bar)

    def test_curator_with_delete_contest_sees_delete(self):
        self.client.force_login(self.own_deleter)
        resp = self.client.get(reverse('contest_edit', args=[self.contest.key]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="editor-assignment-delete"', _bottombar_html(resp.content.decode()))

    def test_curator_cannot_delete_unrelated_assignment(self):
        self.client.force_login(self.own_deleter)
        resp = self.client.get(reverse('contest_edit', args=[self.other.key]))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Contest.objects.filter(key=self.other.key).exists())

    def test_student_cannot_see_edit_or_delete(self):
        self.client.force_login(self.student)
        resp = self.client.get(reverse('contest_edit', args=[self.contest.key]))
        self.assertEqual(resp.status_code, 403)
        create_resp = self.client.get(reverse('contest_create'))
        self.assertEqual(create_resp.status_code, 403)

    def test_post_deletes_and_redirects_to_list(self):
        target = create_contest(
            key='editor_del_post',
            name='Editor Delete Post',
            is_visible=True,
            authors=('editor_page_deleter',),
        )
        self.client.force_login(self.deleter)
        resp = self.client.post(reverse('contest_delete', args=[target.key]), {
            'next': reverse('contest_list'),
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('contest_list'))
        self.assertFalse(Contest.objects.filter(key='editor_del_post').exists())

    def test_post_denied_without_delete_contest(self):
        keep = create_contest(
            key='editor_del_keep',
            name='Editor Delete Keep',
            is_visible=True,
            authors=('editor_page_no_delete',),
        )
        self.client.force_login(self.editor)
        resp = self.client.post(reverse('contest_delete', args=[keep.key]), {
            'next': reverse('contest_list'),
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Contest.objects.filter(key='editor_del_keep').exists())

    def test_post_rejects_external_next_redirect(self):
        target = create_contest(
            key='editor_del_redir',
            name='Editor Delete Redir',
            is_visible=True,
            authors=('editor_page_deleter',),
        )
        self.client.force_login(self.deleter)
        resp = self.client.post(reverse('contest_delete', args=[target.key]), {
            'next': 'https://evil.example/phish',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('contest_list'))
        self.assertFalse(Contest.objects.filter(key='editor_del_redir').exists())
