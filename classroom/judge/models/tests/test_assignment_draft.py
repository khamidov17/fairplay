import json

from django.test import TestCase
from django.urls import reverse

from judge.models import AssignmentDraft, Language, ProblemMCQOption, ProblemSubmissionType, ProblemTemplate
from judge.models.tests.util import CommonDataMixin, create_contest, create_contest_participation, create_problem
from judge.utils.assignment_draft import format_draft_preview


class AssignmentDraftContentTestCase(CommonDataMixin, TestCase):
    """``has_content`` decides whether auto-submit will file this draft, so an answer that is
    really blank -- including code left exactly as the starter template -- must read as empty."""

    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.contest = create_contest(key='draftcontest', authors=('superuser',))
        self.participation = create_contest_participation(contest=self.contest, user='normal')
        self.python = Language.objects.get(key='PY3')
        self.problems = {
            submission_type: create_problem(code='draft%s' % submission_type, submission_type=submission_type)
            for submission_type, _label in ProblemSubmissionType.CHOICES
        }

    def _draft(self, submission_type, content, language=None):
        return AssignmentDraft(
            participation=self.participation, problem=self.problems[submission_type],
            content=content, language=language,
        )

    def test_code_blank_and_whitespace_are_empty(self):
        for content in ('', '   \n\t  '):
            with self.subTest(content=repr(content)):
                self.assertFalse(self._draft(ProblemSubmissionType.CODE, content, self.python).has_content())

    def test_code_with_source_has_content(self):
        draft = self._draft(ProblemSubmissionType.CODE, 'print(1)', self.python)
        self.assertTrue(draft.has_content())

    def test_code_without_language_is_empty(self):
        # Nothing can be submitted without a language, so treat it as no answer rather than
        # letting harvest fail later.
        self.assertFalse(self._draft(ProblemSubmissionType.CODE, 'print(1)').has_content())

    def test_code_equal_to_problem_template_is_empty(self):
        problem = self.problems[ProblemSubmissionType.CODE]
        ProblemTemplate.objects.create(problem=problem, language=self.python, code='def solve():\n    pass\n')
        draft = self._draft(ProblemSubmissionType.CODE, 'def solve():\n    pass\n', self.python)
        self.assertFalse(draft.has_content())

    def test_code_edited_from_template_has_content(self):
        problem = self.problems[ProblemSubmissionType.CODE]
        ProblemTemplate.objects.create(problem=problem, language=self.python, code='def solve():\n    pass\n')
        draft = self._draft(ProblemSubmissionType.CODE, 'def solve():\n    return 1\n', self.python)
        self.assertTrue(draft.has_content())

    def test_code_equal_to_language_template_is_empty(self):
        self.python.template = 'import sys\n'
        self.python.save(update_fields=['template'])
        self.addCleanup(Language.objects.filter(pk=self.python.pk).update, template='')
        self.assertFalse(self._draft(ProblemSubmissionType.CODE, 'import sys\n', self.python).has_content())

    def test_mcq_no_selection_is_empty(self):
        for content in ('', '{}', json.dumps({'selected': []}), 'not json'):
            with self.subTest(content=content):
                self.assertFalse(self._draft(ProblemSubmissionType.MCQ, content).has_content())

    def test_mcq_with_selection_has_content(self):
        draft = self._draft(ProblemSubmissionType.MCQ, json.dumps({'selected': [3]}))
        self.assertTrue(draft.has_content())

    def test_mcq_preview_shows_choice_text_instead_of_row_ids(self):
        problem = self.problems[ProblemSubmissionType.MCQ]
        option = ProblemMCQOption.objects.create(problem=problem, order=0, text='Paris', is_correct=True)
        ProblemMCQOption.objects.create(problem=problem, order=1, text='Lyon', is_correct=False)
        draft = self._draft(ProblemSubmissionType.MCQ, json.dumps({'selected': [option.id]}))
        preview = format_draft_preview(draft)
        self.assertIn('Paris', preview)
        self.assertNotIn('Lyon', preview)
        self.assertNotIn('True', preview)

    def test_fill_blank_all_blank_is_empty(self):
        for content in ('', json.dumps({'answers': []}), json.dumps({'answers': ['', '  ']})):
            with self.subTest(content=content):
                self.assertFalse(self._draft(ProblemSubmissionType.FILL_BLANK, content).has_content())

    def test_fill_blank_any_answer_has_content(self):
        draft = self._draft(ProblemSubmissionType.FILL_BLANK, json.dumps({'answers': ['', '42']}))
        self.assertTrue(draft.has_content())

    def test_short_answer_blank_is_empty(self):
        for content in ('', json.dumps({'text': ''}), json.dumps({'text': '\n \t'})):
            with self.subTest(content=content):
                self.assertFalse(self._draft(ProblemSubmissionType.SHORT_ANSWER, content).has_content())

    def test_short_answer_with_text_has_content(self):
        draft = self._draft(ProblemSubmissionType.SHORT_ANSWER, json.dumps({'text': 'because'}))
        self.assertTrue(draft.has_content())

    def test_file_draft_without_upload_is_empty(self):
        self.assertFalse(self._draft(ProblemSubmissionType.FILE, '').has_content())


class AssignmentDraftUniquenessTestCase(CommonDataMixin, TestCase):
    """One draft per participation and problem: saving again must overwrite, never accumulate."""

    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.contest = create_contest(key='draftunique', authors=('superuser',))
        self.participation = create_contest_participation(contest=self.contest, user='normal')
        self.problem = create_problem(code='draftunique1')

    def test_update_or_create_replaces_content(self):
        AssignmentDraft.objects.update_or_create(
            participation=self.participation, problem=self.problem, defaults={'content': 'first'},
        )
        AssignmentDraft.objects.update_or_create(
            participation=self.participation, problem=self.problem, defaults={'content': 'second'},
        )
        drafts = AssignmentDraft.objects.filter(participation=self.participation, problem=self.problem)
        self.assertEqual(drafts.count(), 1)
        self.assertEqual(drafts.get().content, 'second')


class AssignmentDraftAdminTestCase(CommonDataMixin, TestCase):
    """Staff may look at drafts to diagnose a missed auto-submit, but never edit a student's answer."""

    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.contest = create_contest(key='draftadmin', authors=('superuser',))
        self.participation = create_contest_participation(contest=self.contest, user='normal')
        self.problem = create_problem(code='draftadmin1')

    def setUp(self):
        self.draft = AssignmentDraft.objects.create(
            participation=self.participation, problem=self.problem, content='print(1)',
        )
        self.client.force_login(self.users['superuser'])

    def test_changelist_and_detail_open(self):
        self.assertEqual(self.client.get(reverse('admin:judge_assignmentdraft_changelist')).status_code, 200)
        detail = reverse('admin:judge_assignmentdraft_change', args=(self.draft.pk,))
        self.assertEqual(self.client.get(detail).status_code, 200)

    def test_content_is_not_editable(self):
        detail = reverse('admin:judge_assignmentdraft_change', args=(self.draft.pk,))
        self.client.post(detail, {'content': 'tampered'})
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.content, 'print(1)')

    def test_add_page_is_unavailable(self):
        self.assertEqual(self.client.get(reverse('admin:judge_assignmentdraft_add')).status_code, 403)
