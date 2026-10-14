"""Teachers finalize their own exams.

Finalize / Un-finalize used to need ``judge.edit_all_contest`` (site contest admin). The rule is
now the one for editing the assignment (``Contest.is_changeable_by``: ``edit_own_contest`` plus
author/curator), on the editor page, its POST endpoints and the Django admin action alike.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models.notification import ensure_instructors_group
from judge.models.tests.util import CommonDataMixin, create_contest, create_user


def _instructor(username, **kwargs):
    user = create_user(username=username, **kwargs)
    user.groups.add(ensure_instructors_group())
    return User.objects.get(id=user.id)  # drop the permission cache


class TeacherFinalizeTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.author = _instructor('fin_author')
        cls.other_instructor = _instructor('fin_other')
        # Listed as curator (can grade) but has no edit_own_contest: cannot edit the assignment.
        cls.grading_curator = create_user(username='fin_grader')
        cls.student = create_user(username='fin_student')
        now = timezone.now()
        cls.contest = create_contest(
            key='finexam', name='Final exam', format_name='exam',
            start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=1),
            authors=('fin_author',), curators=('fin_grader',),
        )

    def _editor(self, user):
        self.client.force_login(user)
        return self.client.get(reverse('contest_edit', args=[self.contest.key]))

    def _post(self, user, name):
        self.client.force_login(user)
        with mock.patch('judge.tasks.finalize_contest_results') as fin, \
                mock.patch('judge.tasks.unfinalize_contest_results') as unfin, \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse(name, args=[self.contest.key]))
        return response, fin, unfin

    def test_author_sees_finalize_button(self):
        response = self._editor(self.author)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="finalize-btn"')
        self.assertNotContains(response, 'id="unfinalize-btn"')

    def test_author_sees_unfinalize_button_once_finalized(self):
        self.contest.results_finalized = True
        self.contest.save(update_fields=['results_finalized'])
        response = self._editor(self.author)
        self.assertContains(response, 'id="unfinalize-btn"')
        self.assertNotContains(response, 'id="finalize-btn"')

    def test_author_can_finalize(self):
        response, fin, unfin = self._post(self.author, 'contest_finalize')
        self.assertEqual(response.status_code, 200, response.content)
        fin.s.assert_called_once_with('finexam')
        fin.s.return_value.delay.assert_called_once_with()
        unfin.s.assert_not_called()

    def test_author_can_unfinalize(self):
        response, fin, unfin = self._post(self.author, 'contest_unfinalize')
        self.assertEqual(response.status_code, 200, response.content)
        unfin.s.assert_called_once_with('finexam')
        fin.s.assert_not_called()

    def test_others_are_refused(self):
        for user in (self.other_instructor, self.grading_curator, self.student):
            for name in ('contest_finalize', 'contest_unfinalize'):
                response, fin, unfin = self._post(user, name)
                self.assertEqual(response.status_code, 403, (user.username, name))
                fin.s.assert_not_called()
                unfin.s.assert_not_called()

    def test_others_get_no_button(self):
        for user in (self.other_instructor, self.grading_curator, self.student):
            response = self._editor(user)
            # The editor itself is refused, so no finalize button can be shown.
            self.assertEqual(response.status_code, 403, user.username)
            self.assertNotContains(response, 'finalize-btn', status_code=403)

    def test_curator_with_edit_own_contest_can_finalize(self):
        # Same rule as editing: a co-editor (curator with edit_own_contest) may finalize too.
        co_editor = _instructor('fin_coeditor')
        self.contest.curators.add(co_editor.profile)
        self.assertContains(self._editor(co_editor), 'id="finalize-btn"')
        response, fin, _unfin = self._post(co_editor, 'contest_finalize')
        self.assertEqual(response.status_code, 200)
        fin.s.assert_called_once_with('finexam')


class AdminFinalizeSameRuleTestCase(CommonDataMixin, TestCase):
    """The Django admin action applies the same rule (``ContestAdmin.has_change_permission``)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.author = _instructor('adm_fin_author', is_staff=True)
        cls.grader = create_user(username='adm_fin_grader', is_staff=True)
        cls.contest = create_contest(key='admfinexam', format_name='exam',
                                     authors=('adm_fin_author',), curators=('adm_fin_grader',))

    def _post(self, user):
        self.client.force_login(user)
        with mock.patch('judge.tasks.finalize_contest_results') as fin, \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('admin:judge_contest_finalize', args=[self.contest.id]))
        return response, fin

    def test_author_can_finalize_in_admin(self):
        response, fin = self._post(self.author)
        self.assertEqual(response.status_code, 302)
        fin.s.assert_called_once_with('admfinexam')

    def test_grading_curator_refused_in_admin(self):
        response, fin = self._post(self.grader)
        self.assertEqual(response.status_code, 403)
        fin.s.assert_not_called()
