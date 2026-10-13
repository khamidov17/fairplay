"""Server-side answer drafts: who may save one, and what content is accepted."""
import json
from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import AssignmentDraft, ContestParticipation, ContestProblem, Language, ProblemMCQOption
from judge.models.tests.util import create_contest, create_problem, create_user


class AssignmentDraftAPITestCase(TestCase):
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.teacher = create_user(username='draftapi_teacher')
        cls.student = create_user(username='draftapi_student')
        cls.other = create_user(username='draftapi_other')
        cls.python = Language.objects.get(key='PY3')
        cls.code = create_problem('draftapi-code', submission_type='C', is_public=False,
                                  allowed_languages=('PY3',))
        cls.mcq = create_problem('draftapi-mcq', submission_type='M', is_public=False)
        cls.options = [
            ProblemMCQOption.objects.create(problem=cls.mcq, order=i, text='opt%d' % i, is_correct=(i == 1))
            for i in (1, 2)
        ]
        cls.contest = create_contest(
            key='draftapiasn', name='Draft API assignment',
            start_time=now - timedelta(hours=1), end_time=now + timedelta(days=1),
            authors=('draftapi_teacher',), is_visible=True,
        )
        ContestProblem.objects.create(contest=cls.contest, problem=cls.code, points=50, order=1)
        ContestProblem.objects.create(contest=cls.contest, problem=cls.mcq, points=50, order=2)
        cls.outside = create_problem('draftapi-outside', submission_type='C', is_public=True,
                                     allowed_languages=('PY3',))

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.student)

    def _join(self, user=None, **kwargs):
        user = user or self.student
        participation = ContestParticipation.objects.create(
            contest=self.contest, user=user.profile, **kwargs,
        )
        user.profile.current_contest = participation
        user.profile.save()
        return participation

    def _url(self, problem, contest=None):
        return reverse('assignment_draft', args=[contest or self.contest.key, problem.code])

    def _save(self, problem, content, **extra):
        data = {'content': content}
        data.update(extra)
        return self.client.post(self._url(problem), data)

    # --- happy path ---------------------------------------------------------

    def test_saves_code_draft_with_language(self):
        participation = self._join()
        response = self._save(self.code, 'print(1)', language=self.python.id)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['saved'])
        draft = AssignmentDraft.objects.get(participation=participation, problem=self.code)
        self.assertEqual(draft.content, 'print(1)')
        self.assertEqual(draft.language, self.python)

    def test_saving_again_overwrites(self):
        participation = self._join()
        self._save(self.code, 'first', language=self.python.id)
        self._save(self.code, 'second', language=self.python.id)
        drafts = AssignmentDraft.objects.filter(participation=participation, problem=self.code)
        self.assertEqual(drafts.count(), 1)
        self.assertEqual(drafts.get().content, 'second')

    def test_delete_removes_draft(self):
        participation = self._join()
        self._save(self.code, 'print(1)', language=self.python.id)
        response = self.client.delete(self._url(self.code))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AssignmentDraft.objects.filter(participation=participation).exists())

    def test_edit_after_harvest_makes_draft_harvestable_again(self):
        participation = self._join()
        AssignmentDraft.objects.create(
            participation=participation, problem=self.code, content='old',
            language=self.python, harvested_at=timezone.now(),
        )
        self._save(self.code, 'new', language=self.python.id)
        draft = AssignmentDraft.objects.get(participation=participation, problem=self.code)
        self.assertEqual(draft.content, 'new')
        self.assertIsNone(draft.harvested_at)

    # --- permissions --------------------------------------------------------

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        response = self._save(self.code, 'print(1)', language=self.python.id)
        self.assertEqual(response.status_code, 302)

    def test_not_joined_is_rejected(self):
        response = self._save(self.code, 'print(1)', language=self.python.id)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AssignmentDraft.objects.exists())

    def test_participation_in_another_assignment_is_rejected(self):
        other_contest = create_contest(key='draftapiother', name='Other', authors=('draftapi_teacher',))
        participation = ContestParticipation.objects.create(contest=other_contest, user=self.student.profile)
        self.student.profile.current_contest = participation
        self.student.profile.save()
        response = self._save(self.code, 'print(1)', language=self.python.id)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AssignmentDraft.objects.exists())

    def test_spectator_is_rejected(self):
        self._join(virtual=ContestParticipation.SPECTATE)
        response = self._save(self.code, 'print(1)', language=self.python.id)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AssignmentDraft.objects.exists())

    def test_teacher_bypassing_assignment_rules_is_rejected(self):
        self.client.force_login(self.teacher)
        self._join(user=self.teacher)
        response = self._save(self.code, 'print(1)', language=self.python.id)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AssignmentDraft.objects.exists())

    def test_closed_assignment_is_rejected(self):
        # A write after the window closed would either be lost or resurrect a harvested draft.
        self._join()
        self.contest.end_time = timezone.now() - timedelta(minutes=1)
        self.contest.save(update_fields=['end_time'])
        response = self._save(self.code, 'print(1)', language=self.python.id)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AssignmentDraft.objects.exists())

    def test_problem_outside_assignment_is_rejected(self):
        self._join()
        response = self._save(self.outside, 'print(1)', language=self.python.id)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(AssignmentDraft.objects.exists())

    def test_unknown_problem_is_404(self):
        self._join()
        url = reverse('assignment_draft', args=[self.contest.key, 'no-such-problem'])
        self.assertEqual(self.client.post(url, {'content': 'x'}).status_code, 404)

    # --- invalid input ------------------------------------------------------

    def test_disallowed_language_is_rejected(self):
        self._join()
        other_language = Language.objects.exclude(id=self.python.id).first()
        response = self._save(self.code, 'print(1)', language=other_language.id)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AssignmentDraft.objects.exists())

    def test_unknown_language_is_rejected(self):
        self._join()
        response = self._save(self.code, 'print(1)', language=999999)
        self.assertEqual(response.status_code, 400)

    def test_oversized_draft_is_rejected(self):
        self._join()
        response = self._save(self.code, 'x' * 70000, language=self.python.id)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AssignmentDraft.objects.exists())

    def test_malformed_mcq_content_is_rejected(self):
        self._join()
        self.assertEqual(self._save(self.mcq, 'not json').status_code, 400)
        self.assertEqual(self._save(self.mcq, json.dumps({'selected': 'nope'})).status_code, 400)
        self.assertFalse(AssignmentDraft.objects.exists())

    def test_mcq_content_is_normalized_to_this_problems_options(self):
        participation = self._join()
        payload = json.dumps({'selected': [self.options[1].id, 999999]})
        self.assertEqual(self._save(self.mcq, payload).status_code, 200)
        draft = AssignmentDraft.objects.get(participation=participation, problem=self.mcq)
        self.assertEqual(json.loads(draft.content), {'selected': [self.options[1].id]})

    def test_get_is_not_allowed(self):
        self._join()
        self.assertEqual(self.client.get(self._url(self.code)).status_code, 405)


class AssignmentDraftLifecycleTestCase(TestCase):
    """A draft's whole point is that it survives until the answer really reaches the server."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.teacher = create_user(username='draftlife_teacher')
        cls.student = create_user(username='draftlife_student')
        cls.python = Language.objects.get(key='PY3')
        cls.mcq = create_problem('draftlife-mcq', submission_type='M', is_public=False)
        cls.option = ProblemMCQOption.objects.create(problem=cls.mcq, order=1, text='a', is_correct=True)
        cls.contest = create_contest(
            key='draftlifeasn', name='Draft lifecycle', start_time=now - timedelta(hours=1),
            end_time=now + timedelta(days=1), authors=('draftlife_teacher',), is_visible=True,
        )
        cls.contest_problem = ContestProblem.objects.create(
            contest=cls.contest, problem=cls.mcq, points=10, order=1, max_submissions=1,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.student)
        self.participation = ContestParticipation.objects.create(
            contest=self.contest, user=self.student.profile,
        )
        self.student.profile.current_contest = self.participation
        self.student.profile.save()

    def _draft(self):
        return AssignmentDraft.objects.filter(participation=self.participation, problem=self.mcq)

    def _save_draft(self):
        return self.client.post(
            reverse('assignment_draft', args=[self.contest.key, self.mcq.code]),
            {'content': json.dumps({'selected': [self.option.id]})},
        )

    def _submit(self):
        return self.client.post(reverse('problem_submit', args=[self.mcq.code]),
                                {'options': str(self.option.id)})

    def test_successful_submit_clears_draft(self):
        self._save_draft()
        self.assertTrue(self._draft().exists())
        self._submit()
        self.assertFalse(self._draft().exists())

    def test_rejected_submit_keeps_draft(self):
        # Burn the single allowed submission, then try again: the retry is refused and the draft,
        # which is all auto-submit will have to work with, must survive.
        self._submit()
        self._save_draft()
        self.assertTrue(self._draft().exists())
        self._submit()
        self.assertTrue(self._draft().exists())

    def test_invalid_submit_keeps_draft(self):
        self._save_draft()
        self.client.post(reverse('problem_submit', args=[self.mcq.code]), {})
        self.assertTrue(self._draft().exists())


class WorkspaceDraftPageTestCase(TestCase):
    """The workspace only syncs drafts where the page hands it a draft URL, and prefers the
    server's copy over the browser's on load."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.teacher = create_user(username='draftpage_teacher')
        cls.student = create_user(username='draftpage_student')
        cls.python = Language.objects.get(key='PY3')
        cls.problem = create_problem('draftpage-code', submission_type='C', is_public=True,
                                     allowed_languages=('PY3',))
        cls.contest = create_contest(
            key='draftpageasn', name='Draft page', start_time=now - timedelta(hours=1),
            end_time=now + timedelta(days=1), authors=('draftpage_teacher',), is_visible=True,
        )
        ContestProblem.objects.create(contest=cls.contest, problem=cls.problem, points=10, order=1)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.student)

    def _join(self, **kwargs):
        participation = ContestParticipation.objects.create(
            contest=self.contest, user=self.student.profile, **kwargs,
        )
        self.student.profile.current_contest = participation
        self.student.profile.save()
        return participation

    def _page(self):
        return self.client.get(reverse('problem_detail', args=[self.problem.code]))

    def test_practice_page_has_no_draft_url(self):
        response = self._page()
        self.assertIsNone(response.context['workspace_draft_url'])
        self.assertNotContains(response, 'data-draft-url')

    def test_assignment_page_has_draft_url(self):
        self._join()
        response = self._page()
        self.assertEqual(
            response.context['workspace_draft_url'],
            reverse('assignment_draft', args=[self.contest.key, self.problem.code]),
        )
        self.assertContains(response, 'data-draft-url')

    def test_spectator_page_has_no_draft_url(self):
        self._join(virtual=ContestParticipation.SPECTATE)
        self.assertIsNone(self._page().context['workspace_draft_url'])

    def test_existing_draft_is_handed_to_the_page(self):
        participation = self._join()
        AssignmentDraft.objects.create(
            participation=participation, problem=self.problem, content='print(41)', language=self.python,
        )
        draft = self._page().context['workspace_server_draft']
        self.assertEqual(draft, {'content': 'print(41)', 'language': self.python.id})

    def test_harvested_draft_is_not_handed_to_the_page(self):
        # Already submitted on the student's behalf, so it is history, not work in progress.
        participation = self._join()
        AssignmentDraft.objects.create(
            participation=participation, problem=self.problem, content='print(41)',
            language=self.python, harvested_at=timezone.now(),
        )
        self.assertIsNone(self._page().context['workspace_server_draft'])
