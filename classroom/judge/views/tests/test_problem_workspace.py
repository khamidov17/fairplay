import json
from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    ContestSubmission,
    Judge,
    Language,
    Problem,
    ProblemBlank,
    ProblemMCQConfig,
    ProblemMCQOption,
    ProblemTemplate,
    Submission,
    SubmissionManualGrade,
    SubmissionSource,
    SubmissionTestCase,
)
from judge.models.tests.util import (
    create_contest,
    create_contest_participation,
    create_contest_problem,
    create_organization,
    create_problem,
    create_user,
)

AUTOSAVE_HINT = 'Draft saved. Press Submit to record this answer for grading.'
AUTOSAVE_HINT_HARVEST = 'Draft saved. If you do not submit, it is submitted automatically at the deadline.'
LEAVE_WARN = (
    'You must press Submit on every problem. '
    'Unsubmitted answers will not count toward your grade.'
)
LEAVE_NOTE = 'Only answers you have already submitted will be kept.'
LEAVE_HARVEST_WARN = (
    'When you leave, answers you have not submitted yet will be submitted automatically.'
)
AUTOSUBMIT_PROMISE = 'Your saved answers are submitted automatically when the assignment closes.'
CONTRADICTORY_COPY = (
    LEAVE_HARVEST_WARN,
    AUTOSUBMIT_PROMISE,
    'Leaving submits your saved answers now.',
)


class ProblemWorkspaceTestCase(TestCase):
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.user = create_user(username='workspace-user')
        cls.language = Language.objects.get(key='PY3')
        cls.judge = Judge.objects.create(name='workspace-judge', auth_key='workspace-key', online=True)
        cls.judge.runtimes.add(cls.language)
        cls.problem = create_problem(code='workspace-code', is_public=True)
        cls.problem.allowed_languages.add(cls.language)
        cls.ide_problem = create_problem(code='idepractice', is_public=True)
        cls.judge.problems.add(cls.problem, cls.ide_problem)

        cls.mcq = create_problem(code='workspace-mcq', is_public=True, submission_type='M')
        ProblemMCQConfig.objects.create(problem=cls.mcq, multiple_select=False)
        cls.mcq_opt_wrong = ProblemMCQOption.objects.create(
            problem=cls.mcq, order=0, text='Option Alpha', is_correct=False,
        )
        cls.mcq_opt_right = ProblemMCQOption.objects.create(
            problem=cls.mcq, order=1, text='Option Beta', is_correct=True,
        )

        cls.author = create_user(username='workspace-author')
        cls.contest = create_contest(
            key='wsassign', name='Workspace Assignment',
            start_time=timezone.now() - timezone.timedelta(hours=1),
            end_time=timezone.now() + timezone.timedelta(hours=2),
            is_visible=True,
            authors=('workspace-author',),
        )
        cls.contest_problem = create_contest_problem(
            contest=cls.contest, problem=cls.problem, points=100, order=1,
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        self.contest.refresh_from_db()

    def test_detail_uses_workspace_shell_and_legacy_escape_hatch(self):
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(response, 'id="workspace"')
        self.assertContains(response, 'id="ws-drawer"')
        legacy = self.client.get(reverse('problem_detail', args=[self.problem.code]) + '?legacy=1')
        self.assertNotContains(legacy, 'id="workspace"')

    def test_run_rejects_language_not_allowed_by_source_problem(self):
        response = self.client.post(reverse('problem_workspace_run', args=[self.problem.code]), {
            'source': 'print(1)', 'language': 999999, 'input': '',
        })
        self.assertEqual(response.status_code, 400)

    def test_run_uses_idepractice_without_contest_submission(self):
        with mock.patch.object(Submission, 'judge'):
            response = self.client.post(reverse('problem_workspace_run', args=[self.problem.code]), {
                'source': 'print(1)', 'language': self.language.id, 'input': 'x',
            })
        self.assertEqual(response.status_code, 200, response.content)
        run = Submission.objects.get(id=response.json()['submission_id'])
        self.assertEqual(run.problem.code, 'idepractice')
        self.assertFalse(hasattr(run, 'contest_submission'))
        self.assertIn('/ide/', response.json()['submission_url'])

    def test_workspace_page_uses_shared_run_result(self):
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(response, 'ide_run_result.js')
        self.assertContains(response, 'id="workspace-run-output"')
        self.assertContains(response, 'id="run-message"')
        self.assertNotContains(response, 'does not use hidden tests')
        self.assertNotContains(response, 'Custom Run reuses')

    def test_workspace_run_status_hides_stdout_for_student(self):
        self.contest.hide_sandbox_output = True
        self.contest.save(update_fields=['hide_sandbox_output'])
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        with mock.patch.object(Submission, 'judge'):
            response = self.client.post(reverse('problem_workspace_run', args=[self.problem.code]), {
                'source': 'print(99)', 'language': self.language.id, 'input': '',
            })
        self.assertEqual(response.status_code, 200, response.content)
        run = Submission.objects.get(id=response.json()['submission_id'])
        run.status = 'D'
        run.result = 'AC'
        run.time = 0.02
        run.memory = 1024
        run.save()
        SubmissionTestCase.objects.create(
            submission=run, case=1, status='AC', time=0.02, memory=1024, output='99\n',
        )
        status = self.client.get(response.json()['submission_url'])
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertNotIn('output', body)
        self.assertTrue(body['output_hidden'])
        self.assertEqual(body['result'], 'AC')

    def test_workspace_run_status_keeps_ir_traceback_and_author_sees_stdout(self):
        self.contest.hide_sandbox_output = True
        self.contest.save(update_fields=['hide_sandbox_output'])
        student_part = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = student_part
        self.user.profile.save()
        with mock.patch.object(Submission, 'judge'):
            response = self.client.post(reverse('problem_workspace_run', args=[self.problem.code]), {
                'source': 'raise ZeroDivisionError()', 'language': self.language.id, 'input': '',
            })
        run = Submission.objects.get(id=response.json()['submission_id'])
        run.status = 'D'
        run.result = 'IR'
        run.time = 0.01
        run.memory = 512
        run.save()
        SubmissionTestCase.objects.create(
            submission=run, case=1, status='IR', time=0.01, memory=512,
            output='should-hide', feedback='ZeroDivisionError',
            extended_feedback='Traceback (most recent call last):\nZeroDivisionError',
        )
        body = self.client.get(response.json()['submission_url']).json()
        self.assertNotIn('output', body)
        self.assertIn('ZeroDivisionError', body['feedback'])
        self.assertIn('Traceback', body['extended_feedback'])

        author_part = create_contest_participation(contest=self.contest, user=self.author.profile)
        self.author.profile.current_contest = author_part
        self.author.profile.save()
        self.client.force_login(self.author)
        with mock.patch.object(Submission, 'judge'):
            author_resp = self.client.post(reverse('problem_workspace_run', args=[self.problem.code]), {
                'source': 'print(7)', 'language': self.language.id, 'input': '',
            })
        author_run = Submission.objects.get(id=author_resp.json()['submission_id'])
        author_run.status = 'D'
        author_run.result = 'AC'
        author_run.time = 0.01
        author_run.memory = 256
        author_run.save()
        SubmissionTestCase.objects.create(
            submission=author_run, case=1, status='AC', time=0.01, memory=256, output='7\n',
        )
        author_body = self.client.get(author_resp.json()['submission_url']).json()
        self.assertIn('7', author_body['output'])
        self.assertNotIn('output_hidden', author_body)

    def test_selected_own_submission_exposes_result_and_source_for_editor_restore(self):
        submission = Submission.objects.create(
            user=self.user.profile, problem=self.problem, language=self.language,
            result='AC', status='D', points=100, time=0.01, memory=1024,
        )
        SubmissionSource.objects.create(submission=submission, source='print(42)')
        response = self.client.get(
            reverse('problem_detail', args=[self.problem.code]) + '?submission=%d' % submission.id,
        )
        self.assertContains(response, 'id="submitted-source"')
        self.assertContains(response, 'print(42)')
        self.assertContains(response, 'id="btn-load-editor"')
        self.assertContains(response, 'id="workspace-result-source"')
        self.assertContains(response, 'result-source-details')

    def test_mcq_result_shows_human_readable_submitted_source(self):
        submission = Submission.objects.create(
            user=self.user.profile, problem=self.mcq, language=Language.get_answer_language(),
            result='AC', status='D', points=100, time=0, memory=0,
        )
        SubmissionSource.objects.create(
            submission=submission,
            source=json.dumps({'selected': [self.mcq_opt_right.id]}),
        )
        status = self.client.get(reverse(
            'problem_workspace_submission_status', args=[self.mcq.code, submission.id],
        ))
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertTrue(any('Option Beta' in (row.get('value') or '') for row in body['answer_display']))
        self.assertIn('comments_html', body)
        detail = self.client.get(
            reverse('problem_detail', args=[self.mcq.code]) + '?submission=%d' % submission.id,
        )
        self.assertContains(detail, 'Option Beta')
        self.assertContains(detail, 'Submitted source')
        self.assertNotContains(detail, 'Your answer')
        self.assertNotContains(detail, 'Raw submitted answer')
        self.assertContains(detail, 'id="workspace-result-comments"')

    def test_short_answer_workspace_exposes_upload_urls_for_students(self):
        short = create_problem(code='ws-shortup', is_public=True, submission_type='S')
        response = self.client.get(reverse('problem_detail', args=[short.code]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('editor_file_uploader'))
        self.assertContains(response, 'data-file-upload-url')
        self.assertContains(response, 'data-file-accept="image/*"')
        self.assertNotContains(response, 'data-image-upload-url')

    def test_short_answer_workspace_embeds_answer_json_for_client_math(self):
        short = create_problem(code='ws-mathsrc', is_public=True, submission_type='S')
        md = 'Area is $$a^2+b^2$$.'
        submission = Submission.objects.create(
            user=self.user.profile, problem=short, language=Language.get_answer_language(),
            result='AC', status='D', points=100, time=0, memory=0,
        )
        SubmissionSource.objects.create(submission=submission, source=json.dumps({'text': md}))
        detail = self.client.get(
            reverse('problem_detail', args=[short.code]) + '?submission=%d' % submission.id,
        )
        self.assertContains(detail, 'id="workspace-answer-display"')
        self.assertContains(detail, 'a^2+b^2')
        self.assertContains(detail, 'vendor/katex.min.css')

    def test_short_answer_submitted_source_renders_markdown_table(self):
        short = create_problem(code='workspace-short', is_public=True, submission_type='S')
        md = '| A | B |\n| --- | --- |\n| 1 | 2 |'
        submission = Submission.objects.create(
            user=self.user.profile, problem=short, language=Language.get_answer_language(),
            result='AC', status='D', points=100, time=0, memory=0,
        )
        SubmissionSource.objects.create(submission=submission, source=json.dumps({'text': md}))
        status = self.client.get(reverse(
            'problem_workspace_submission_status', args=[short.code, submission.id],
        ))
        self.assertEqual(status.status_code, 200)
        row = status.json()['answer_display'][0]
        self.assertIn('| A | B |', row['value'])
        self.assertIn('<table', row['html'])
        detail = self.client.get(
            reverse('problem_detail', args=[short.code]) + '?submission=%d' % submission.id,
        )
        self.assertContains(detail, '<table')
        self.assertContains(detail, 'result-source-md')

    def test_fill_blank_result_shows_each_blank_on_its_own_row(self):
        fb = create_problem(code='workspace-fillblank', is_public=True, submission_type='B')
        ProblemBlank.objects.create(problem=fb, order=0, answers='Paris')
        ProblemBlank.objects.create(problem=fb, order=1, answers='1789')
        submission = Submission.objects.create(
            user=self.user.profile, problem=fb, language=Language.get_answer_language(),
            result='AC', status='D', points=100, time=0, memory=0,
        )
        SubmissionSource.objects.create(
            submission=submission,
            source=json.dumps({'answers': ['submit', 'submit']}),
        )
        detail = self.client.get(
            reverse('problem_detail', args=[fb.code]) + '?submission=%d' % submission.id,
        )
        self.assertContains(detail, 'class="result-source-row"', count=2)
        self.assertContains(detail, 'Blank 1: submit')
        self.assertContains(detail, 'Blank 2: submit')
        self.assertNotContains(detail, 'Blank 1: submit Blank 2: submit')

    def test_workspace_submissions_list_returns_oj_style_html_rows(self):
        submission = Submission.objects.create(
            user=self.user.profile, problem=self.problem, language=self.language,
            result='AC', status='D', points=100, case_points=100, case_total=100,
            time=0.01, memory=1024,
        )
        response = self.client.get(
            reverse('problem_workspace_submissions', args=[self.problem.code]) + '?scope=my',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['scope'], 'my')
        self.assertEqual(len(payload['rows']), 1)
        html = payload['rows'][0]['html']
        self.assertIn('submission-row', html)
        self.assertIn('ws-submission-link', html)
        self.assertIn('view', html)
        self.assertIn(str(submission.id), html)

    def test_workspace_submit_returns_json_and_status_payload(self):
        with mock.patch.object(Submission, 'judge'):
            response = self.client.post(
                reverse('problem_submit', args=[self.problem.code]),
                {'workspace': '1', 'source': 'print(1)', 'language': self.language.id, 'judge': ''},
                HTTP_X_WORKSPACE_SUBMIT='1',
            )
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        submission = Submission.objects.get(id=payload['submission_id'])
        self.assertEqual(submission.problem, self.problem)
        status = self.client.get(payload['status_url'])
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertEqual(body['id'], submission.id)
        self.assertIn('status', body)
        self.assertIn('source', body)
        self.assertEqual(body['source'], 'print(1)')

    def test_workspace_submit_still_succeeds_if_ai_grade_payload_breaks(self):
        with mock.patch.object(Submission, 'judge'):
            with mock.patch(
                'judge.views.problem.workspace_ai_grade_fields',
                side_effect=TypeError("ai_grade_context() got an unexpected keyword argument 'contest'"),
            ):
                response = self.client.post(
                    reverse('problem_submit', args=[self.problem.code]),
                    {'workspace': '1', 'source': 'print(1)', 'language': self.language.id, 'judge': ''},
                    HTTP_X_WORKSPACE_SUBMIT='1',
                )
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertIn('submission_id', payload)
        self.assertTrue(Submission.objects.filter(id=payload['submission_id']).exists())

    def test_assignment_workspace_submit_includes_updated_leave_progress(self):
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        with mock.patch.object(Submission, 'judge'):
            response = self.client.post(
                reverse('problem_submit', args=[self.problem.code]),
                {'workspace': '1', 'source': 'print(1)', 'language': self.language.id, 'judge': ''},
                HTTP_X_WORKSPACE_SUBMIT='1',
            )
        self.assertEqual(response.status_code, 200, response.content)
        progress = response.json()['leave_progress']
        self.assertEqual(progress['submitted'], 1)
        self.assertEqual(progress['unsubmitted'], 0)
        self.assertEqual(progress['total'], 1)
        self.assertTrue(any('workspace-code' in name for name in progress['submitted_names']))
        self.assertEqual(progress['unsubmitted_names'], [])

    def test_assignment_workspace_submit_logs_leave_progress_trace(self):
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        with mock.patch.object(Submission, 'judge'):
            with self.assertLogs('judge.workspace.progress', level='INFO') as captured:
                response = self.client.post(
                    reverse('problem_submit', args=[self.problem.code]),
                    {'workspace': '1', 'source': 'print(1)', 'language': self.language.id, 'judge': ''},
                    HTTP_X_WORKSPACE_SUBMIT='1',
                )
        self.assertEqual(response.status_code, 200, response.content)
        joined = '\n'.join(captured.output)
        self.assertIn('workspace-progress', joined)
        self.assertIn('"step": "json_submit"', joined)
        self.assertIn('"submitted": 1', joined)

    def test_workspace_progress_trace_endpoint_logs_client_snapshot(self):
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        with self.assertLogs('judge.workspace.progress', level='INFO') as captured:
            response = self.client.post(
                reverse('workspace_progress_trace'),
                data=json.dumps({
                    'step': 'client_leave_dialog',
                    'href': '/problem/workspace-code',
                    'leave_submitted': '0',
                    'leave_unsubmitted': '1',
                    'leave_total': '1',
                    'bar_label': '0 / 1',
                }),
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {'ok': True})
        joined = '\n'.join(captured.output)
        self.assertIn('"step": "client_leave_dialog"', joined)
        self.assertIn('"leave_submitted": "0"', joined)
        self.assertIn('workspace-code', joined)

    def test_assignment_my_submissions_filters_by_contest_object(self):
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()

        in_contest = Submission.objects.create(
            user=self.user.profile, problem=self.problem, language=self.language,
            result='AC', status='D', points=100, contest_object=self.contest,
            time=0.01, memory=1024,
        )
        ContestSubmission.objects.create(
            submission=in_contest, problem=self.contest_problem, participation=participation,
        )
        practice = Submission.objects.create(
            user=self.user.profile, problem=self.problem, language=self.language,
            result='WA', status='D', points=0, contest_object=None,
            time=0.01, memory=1024,
        )

        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertEqual(response.context['workspace_mode'], 'assignment')
        ids = [row.id for row in response.context['workspace_submissions']]
        self.assertEqual(ids, [in_contest.id])
        self.assertNotIn(practice.id, ids)
        self.assertContains(response, '?submission=%d' % in_contest.id)
        self.assertNotContains(response, 'data-sub="all"')
        self.assertNotContains(response, 'data-sub="best"')

    def test_assignment_rejects_practice_submission_query(self):
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()

        in_contest = Submission.objects.create(
            user=self.user.profile, problem=self.problem, language=self.language,
            result='AC', status='D', points=100, contest_object=self.contest,
            time=0.01, memory=1024,
        )
        ContestSubmission.objects.create(
            submission=in_contest, problem=self.contest_problem, participation=participation,
        )
        SubmissionSource.objects.create(submission=in_contest, source='print("contest")')
        practice = Submission.objects.create(
            user=self.user.profile, problem=self.problem, language=self.language,
            result='WA', status='D', points=0, contest_object=None,
            time=0.01, memory=1024,
        )
        SubmissionSource.objects.create(submission=practice, source='print("practice")')

        blocked = self.client.get(
            reverse('problem_detail', args=[self.problem.code]) + '?submission=%d' % practice.id,
        )
        self.assertEqual(blocked.context['workspace_mode'], 'assignment')
        self.assertIsNone(blocked.context['workspace_submission'])

        allowed = self.client.get(
            reverse('problem_detail', args=[self.problem.code]) + '?submission=%d' % in_contest.id,
        )
        self.assertIsNotNone(allowed.context['workspace_submission'])
        self.assertEqual(allowed.context['workspace_submission']['id'], in_contest.id)

    def test_assignment_scope_all_and_best_forbidden(self):
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()

        in_contest = Submission.objects.create(
            user=self.user.profile, problem=self.problem, language=self.language,
            result='AC', status='D', points=100, contest_object=self.contest,
            time=0.01, memory=1024,
        )
        ContestSubmission.objects.create(
            submission=in_contest, problem=self.contest_problem, participation=participation,
        )
        practice = Submission.objects.create(
            user=self.user.profile, problem=self.problem, language=self.language,
            result='WA', status='D', points=0, contest_object=None,
            time=0.01, memory=1024,
        )

        for scope in ('all', 'best'):
            response = self.client.get(
                reverse('problem_workspace_submissions', args=[self.problem.code]) + '?scope=%s' % scope,
            )
            self.assertEqual(response.status_code, 403, scope)
            self.assertIn('error', response.json())

        mine = self.client.get(
            reverse('problem_workspace_submissions', args=[self.problem.code]) + '?scope=my',
        )
        self.assertEqual(mine.status_code, 200)
        ids = [row['id'] for row in mine.json()['rows']]
        self.assertEqual(ids, [in_contest.id])
        self.assertNotIn(practice.id, ids)

    def test_assignment_status_rejects_practice_submission(self):
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()

        practice = Submission.objects.create(
            user=self.user.profile, problem=self.problem, language=self.language,
            result='WA', status='D', points=0, contest_object=None,
            time=0.01, memory=1024,
        )
        response = self.client.get(
            reverse('problem_workspace_submission_status', args=[self.problem.code, practice.id]),
        )
        self.assertEqual(response.status_code, 403)

    def test_practice_still_shows_all_and_best_tabs(self):
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertEqual(response.context['workspace_mode'], 'practice')
        self.assertContains(response, 'data-sub="all"')
        self.assertContains(response, 'data-sub="best"')

    def test_practice_workspace_submissions_json_without_current_contest(self):
        submission = Submission.objects.create(
            user=self.user.profile, problem=self.problem, language=self.language,
            result='AC', status='D', points=100, time=0.01, memory=1024,
        )
        response = self.client.get(
            reverse('problem_workspace_submissions', args=[self.problem.code]) + '?scope=my',
        )
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload['scope'], 'my')
        self.assertEqual([row['id'] for row in payload['rows']], [submission.id])

    def test_practice_drawer_has_filter_controls(self):
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertEqual(response.context['workspace_mode'], 'practice')
        self.assertContains(response, 'id="fp-status"', html=False)
        self.assertContains(response, 'id="fp-type"', html=False)
        self.assertContains(response, 'id="fp-course"', html=False)
        self.assertContains(response, 'id="fp-tags"', html=False)
        self.assertContains(response, 'id="fp-ac-min"', html=False)
        self.assertContains(response, 'id="fp-diff"', html=False)
        self.assertNotContains(response, 'id="fp-group"', html=False)
        self.assertNotContains(response, 'id="fp-author"', html=False)
        self.assertNotContains(response, 'data-author-ids', html=False)
        self.assertIn('workspace_organizations', response.context)

    def test_assignment_editor_preview_drawer_matches_participation_filters(self):
        from urllib.parse import urlencode

        names = json.dumps({
            self.problem.code: 'Renamed One',
            self.ide_problem.code: 'Renamed Two',
        })
        query = urlencode({
            'assignment_preview': '1',
            'preview_codes': '%s,%s' % (self.problem.code, self.ide_problem.code),
            'preview_names': names,
        })
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]) + '?' + query)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['workspace_mode'], 'assignment')
        self.assertTrue(response.context['workspace_assignment_preview'])
        self.assertFalse(response.context['workspace_in_contest'])
        codes = [item.code for item in response.context['workspace_problems']]
        self.assertEqual(codes, [self.problem.code, self.ide_problem.code])
        self.assertEqual(response.context['workspace_problems'][0].effective_name, 'Renamed One')
        self.assertEqual(response.context['title'], 'Renamed One')
        html = response.content.decode()
        self.assertIn('id="drawer-filter-status"', html)
        self.assertNotIn('id="fp-type"', html)
        self.assertIn('Renamed One', html)
        self.assertIn('Renamed Two', html)
        self.assertIn('assignment_preview=1', html)

    def test_practice_drawer_author_filter_for_teachers(self):
        teacher = create_user(username='ws-author-teacher', user_permissions=('edit_own_problem',))
        self.problem.authors.add(teacher.profile)
        self.client.force_login(teacher)
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertTrue(response.context['can_see_problem_authors'])
        self.assertContains(response, 'id="fp-author"', html=False)
        self.assertContains(response, 'data-author-ids', html=False)
        self.assertContains(response, str(teacher.profile.id), html=False)

    def test_workspace_toolbar_has_progress_not_in_drawer(self):
        practice = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(practice, 'ws-toolbar-progress')
        self.assertContains(practice, 'id="toolbar-progress-bar"', html=False)
        self.assertNotContains(practice, 'id="drawer-bar"', html=False)
        self.assertNotContains(practice, 'id="drawer-solved-frac"', html=False)

        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        assignment = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertEqual(assignment.context['workspace_mode'], 'assignment')
        self.assertContains(assignment, 'ws-toolbar-progress')
        self.assertContains(assignment, 'id="toolbar-progress-bar"', html=False)
        self.assertContains(assignment, 'ws-toolbar-progress is-hidden')
        self.assertNotContains(practice, 'ws-toolbar-progress is-hidden')
        self.assertNotContains(assignment, 'id="drawer-progress"', html=False)
        self.assertNotContains(practice, 'id="drawer-close"', html=False)
        self.assertNotContains(assignment, 'id="drawer-close"', html=False)

    def test_practice_course_filter_lists_membership_without_public_sentinel(self):
        mine = create_organization(name='WsMine')
        other = create_organization(name='WsOther')
        self.user.profile.organizations.add(mine)
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        names = {org.name for org in response.context['workspace_organizations']}
        self.assertIn(mine.name, names)
        self.assertNotIn(other.name, names)
        course = response.content.decode().split('id="fp-course"', 1)[1].split('</select>', 1)[0]
        self.assertNotIn('value="public"', course)
        self.assertNotRegex(course, r'>\s*Public\s*<')
        self.assertIn(mine.name, course)
        self.assertNotIn(other.name, course)
        # Public problems stay in the drawer; filtering is "all courses" when none are selected.
        self.assertContains(response, 'data-public="1"', html=False)

    def test_anonymous_problem_detail_stays_in_practice(self):
        # `authed and current_contest` is False for guests; that must not be
        # treated as an active participation (bool has no .contest).
        response = Client().get(reverse('problem_detail', args=[self.problem.code]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['workspace_in_contest'])
        self.assertEqual(response.context['workspace_mode'], 'practice')

    def test_assignment_drawer_stays_on_contest_for_other_problems(self):
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()

        on_assignment = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertEqual(on_assignment.context['workspace_mode'], 'assignment')
        self.assertTrue(on_assignment.context['workspace_in_contest'])
        self.assertEqual(
            [cp.problem.code for cp in on_assignment.context['workspace_problems']],
            [self.problem.code],
        )

        # Public problem outside this assignment must not switch the drawer to practice.
        outside = self.client.get(reverse('problem_detail', args=[self.mcq.code]))
        self.assertEqual(outside.context['workspace_mode'], 'assignment')
        self.assertTrue(outside.context['workspace_in_contest'])
        self.assertEqual(
            [cp.problem.code for cp in outside.context['workspace_problems']],
            [self.problem.code],
        )

    def test_expired_live_window_rejoins_as_spectator(self):
        from judge.models import ContestParticipation

        contest = create_contest(
            key='wsexpired', name='Expired Window',
            start_time=timezone.now() - timezone.timedelta(hours=2),
            end_time=timezone.now() + timezone.timedelta(hours=2),
            time_limit=timezone.timedelta(minutes=1),
            is_visible=True,
            authors=('workspace-author',),
        )
        create_contest_participation(
            contest=contest, user=self.user.profile,
            real_start=timezone.now() - timezone.timedelta(hours=1),
        )
        self.user.profile.current_contest = None
        self.user.profile.save()

        response = self.client.post(reverse('contest_join', args=[contest.key]))
        self.assertEqual(response.status_code, 302)
        self.user.profile.refresh_from_db()
        self.assertIsNotNone(self.user.profile.current_contest)
        self.assertTrue(self.user.profile.current_contest.spectate)
        self.assertEqual(self.user.profile.current_contest.virtual, ContestParticipation.SPECTATE)

    def test_assignment_workspace_exposes_context_id_for_drafts(self):
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertEqual(response.context['workspace_mode'], 'assignment')
        self.assertContains(response, 'data-context-id="%s"' % self.contest.key)
        self.assertContains(response, 'data-return-url="%s"' % reverse('contest_view', args=[self.contest.key]))
        self.assertContains(response, 'id="workspace-submit-form"')

    def test_expired_personal_window_leaves_the_workspace(self):
        contest = create_contest(
            key='wsended', name='Ended Window',
            start_time=timezone.now() - timezone.timedelta(hours=2),
            end_time=timezone.now() + timezone.timedelta(hours=2),
            time_limit=timezone.timedelta(minutes=1),
            is_visible=True,
            authors=('workspace-author',),
        )
        create_contest_problem(contest=contest, problem=self.problem, points=100, order=1)
        participation = create_contest_participation(
            contest=contest, user=self.user.profile,
            real_start=timezone.now() - timezone.timedelta(hours=1),
        )
        self.user.profile.current_contest = participation
        self.user.profile.save()
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertRedirects(response, reverse('contest_view', args=[contest.key]))

    def test_ended_assignment_leaves_the_workspace(self):
        contest = create_contest(
            key='wsclosed', name='Closed Assignment',
            start_time=timezone.now() - timezone.timedelta(hours=2),
            end_time=timezone.now() - timezone.timedelta(minutes=1),
            is_visible=True,
            authors=('workspace-author',),
        )
        create_contest_problem(contest=contest, problem=self.problem, points=100, order=1)
        participation = create_contest_participation(contest=contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertRedirects(response, reverse('contest_view', args=[contest.key]))

    def test_ended_assignment_does_not_bounce_other_pages(self):
        contest = create_contest(
            key='wshomeend', name='Home End Assignment',
            start_time=timezone.now() - timezone.timedelta(hours=2),
            end_time=timezone.now() - timezone.timedelta(minutes=1),
            is_visible=True,
            authors=('workspace-author',),
        )
        participation = create_contest_participation(contest=contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)

    def test_assignment_toolbar_puts_leave_on_submit_row(self):
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(response, 'ws-toolbar has-leave')
        self.assertContains(response, 'ws-toolbar-center')
        self.assertContains(response, 'id="btn-submit"')
        self.assertContains(response, reverse('contest_leave', args=[self.contest.key]))
        self.assertContains(response, 'assignment-leave-form')
        self.assertContains(response, 'Leave')
        self.assertTrue(response.context['workspace_show_timer'])
        self.assertFalse(response.context['workspace_timer_is_duration'])
        self.assertContains(response, 'id="ws-timer"')
        self.assertContains(response, 'Validity remaining')
        html = response.content.decode()
        toolbar = html.split('class="ws-toolbar', 1)[1].split('class="ws-body', 1)[0]
        self.assertLess(toolbar.index('ws-toolbar-center'), toolbar.index('id="ws-timer"'))
        self.assertLess(toolbar.index('id="ws-timer"'), toolbar.index('submit-cluster'))
        self.assertLess(toolbar.index('assignment-leave-form'), toolbar.index('submit-cluster-sep'))
        self.assertLess(toolbar.index('submit-cluster-sep'), toolbar.index('id="btn-submit"'))
        self.assertLess(toolbar.index('submit-cluster'), toolbar.index('assignment-leave-form'))
        self.assertIn('data-timer-window-seconds=', html)
        self.assertContains(response, 'data-submitted="0"')
        self.assertContains(response, 'data-total="1"')
        self.assertContains(response, 'data-unsubmitted="1"')
        self.assertContains(response, 'data-unsubmitted-names')
        self.assertContains(response, '1. workspace-code')
        self.assertContains(response, LEAVE_WARN)
        self.assertNotContains(response, LEAVE_NOTE)
        self.assertContains(response, 'leave-confirm-stats" hidden')
        self.assertNotContains(response, LEAVE_HARVEST_WARN)
        self.assertNotContains(response, AUTOSUBMIT_PROMISE)
        self.assertNotContains(response, 'Submit your work and exit Safe Exam Browser')

    def test_assignment_toolbar_timer_uses_duration_when_time_limit_is_set(self):
        contest = create_contest(
            key='wsduration', name='Duration Workspace',
            start_time=timezone.now() - timezone.timedelta(hours=1),
            end_time=timezone.now() + timezone.timedelta(hours=2),
            time_limit=timezone.timedelta(minutes=30),
            is_visible=True,
            authors=('workspace-author',),
        )
        create_contest_problem(contest=contest, problem=self.problem, points=100, order=1)
        participation = create_contest_participation(contest=contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertTrue(response.context['workspace_show_timer'])
        self.assertTrue(response.context['workspace_timer_is_duration'])
        self.assertEqual(response.context['workspace_timer_window_seconds'], 30 * 60)
        self.assertContains(response, 'id="ws-timer"')
        self.assertContains(response, 'Duration remaining')
        self.assertNotContains(response, 'Validity remaining')
        html = response.content.decode()
        toolbar = html.split('class="ws-toolbar', 1)[1].split('class="ws-body', 1)[0]
        self.assertLess(toolbar.index('id="ws-timer"'), toolbar.index('submit-cluster'))
        self.assertLess(toolbar.index('submit-cluster'), toolbar.index('assignment-leave-form'))
        self.assertLess(toolbar.index('assignment-leave-form'), toolbar.index('id="btn-submit"'))

    def test_assignment_leave_progress_counts_submitted_problems(self):
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        submission = Submission.objects.create(
            user=self.user.profile, problem=self.problem, language=self.language,
            result='AC', status='D', points=100, contest_object=self.contest,
            time=0.01, memory=1024,
        )
        ContestSubmission.objects.create(
            submission=submission, problem=self.contest_problem, participation=participation,
        )
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(response, 'data-submitted="1"')
        self.assertContains(response, 'data-total="1"')
        self.assertContains(response, 'data-unsubmitted="0"')
        self.assertContains(response, '1. workspace-code')

    def test_practice_workspace_has_no_leave_button(self):
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertEqual(response.context['workspace_mode'], 'practice')
        self.assertNotContains(response, 'assignment-leave-form')
        self.assertNotContains(response, reverse('contest_leave', args=[self.contest.key]))
        self.assertFalse(response.context['workspace_show_timer'])
        self.assertNotContains(response, 'id="ws-timer"')

    def test_assignment_submit_area_says_press_submit(self):
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertFalse(response.context['workspace_autosubmit'])
        self.assertContains(response, AUTOSAVE_HINT)
        self.assertNotContains(response, AUTOSAVE_HINT_HARVEST)

    def test_practice_submit_area_promises_nothing(self):
        # Nothing collects practice drafts, so the same line here would be a lie.
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertEqual(response.context['workspace_mode'], 'practice')
        self.assertFalse(response.context['workspace_autosubmit'])
        self.assertNotContains(response, AUTOSAVE_HINT)

    def test_the_assignment_surfaces_agree_that_leave_does_not_submit(self):
        # Leave dialog is shared by the assignment page, problem list and workspace.
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        for page in (reverse('contest_view', args=[self.contest.key]),
                     reverse('problem_list'),
                     reverse('problem_detail', args=[self.problem.code])):
            with self.subTest(page=page):
                response = self.client.get(page)
                self.assertContains(response, LEAVE_WARN)
                self.assertNotContains(response, LEAVE_NOTE)
                self.assertContains(response, 'leave-confirm-stats" hidden')
                self.assertNotContains(response, AUTOSUBMIT_PROMISE)
                for stale in CONTRADICTORY_COPY:
                    self.assertNotContains(response, stale)

    def test_assignment_workspace_asks_the_student_to_submit_themselves(self):
        participation = create_contest_participation(contest=self.contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertTrue(response.context['workspace_manual_submit_notice'])
        self.assertEqual(response.context['workspace_participation_id'], participation.pk)
        self.assertContains(response, 'data-manual-submit-notice="1"')
        self.assertContains(response, 'data-participation-id="%s"' % participation.pk)
        self.assertContains(response, 'dkuojShowAssignmentNotice')
        self.assertContains(response, LEAVE_WARN)
        html = response.content.decode()
        self.assertIn('DKUOJ_PREEXAM_TITLE', html)
        self.assertIn('A kind reminder', html)
        self.assertIn('data-timer-window-seconds=', html)
        self.assertGreater(response.context['workspace_timer_window_seconds'], 0)

    def test_a_spectator_is_not_promised_a_submission_they_will_not_get(self):
        # Spectating work is never collected, so the promise has to follow the collection rather
        # than merely "is inside an assignment".
        from judge.models import ContestParticipation

        participation = create_contest_participation(
            contest=self.contest, user=self.user.profile, virtual=ContestParticipation.SPECTATE,
        )
        self.user.profile.current_contest = participation
        self.user.profile.save()
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertEqual(response.context['workspace_mode'], 'assignment')
        self.assertFalse(response.context['workspace_autosubmit'])
        self.assertFalse(response.context['workspace_manual_submit_notice'])
        self.assertNotContains(response, 'data-manual-submit-notice="1"')
        self.assertNotContains(response, AUTOSAVE_HINT)

    def test_practice_workspace_form_disables_browser_autocomplete(self):
        response = self.client.get(reverse('problem_detail', args=[self.mcq.code]))
        self.assertEqual(response.context['workspace_mode'], 'practice')
        self.assertNotContains(response, 'data-context-id="wsassign"')
        # Fill-blank/MCQ fields get autocomplete off via JS and/or form widgets.
        content = response.content.decode()
        self.assertTrue(
            'autocomplete="off"' in content or 'autocomplete="new-password"' in content,
            'expected autocomplete disabled on workspace answer form',
        )

    def test_workspace_empty_code_submit_returns_json_error(self):
        response = self.client.post(
            reverse('problem_submit', args=[self.problem.code]),
            {'workspace': '1', 'source': '', 'language': self.language.id, 'judge': ''},
            HTTP_X_WORKSPACE_SUBMIT='1',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response['Content-Type'].split(';')[0], 'application/json')
        self.assertIn('error', response.json())
        self.assertNotIn('<!DOCTYPE', response.content.decode()[:40])

    def test_mcq_answer_display_uses_snapshot_when_options_recreated(self):
        from judge.views.problem import get_workspace_answer_display

        submission = Submission.objects.create(
            user=self.user.profile, problem=self.mcq, language=Language.get_answer_language(),
            result='WA', status='D', points=0, time=0, memory=0,
        )
        # Legacy raw ids (option deleted) + snapshot from a newer submit path.
        SubmissionSource.objects.create(
            submission=submission,
            source=json.dumps({
                'selected': [999001],
                'choices': [{'id': 999001, 'order': 0, 'text': 'Snapshotted True'}],
            }),
        )
        rows = get_workspace_answer_display(submission)
        self.assertTrue(any('Snapshotted True' in (row.get('value') or '') for row in rows))

        stale = Submission.objects.create(
            user=self.user.profile, problem=self.mcq, language=Language.get_answer_language(),
            result='WA', status='D', points=0, time=0, memory=0,
        )
        SubmissionSource.objects.create(
            submission=stale,
            source=json.dumps({'selected': [999002]}),
        )
        stale_rows = get_workspace_answer_display(stale)
        self.assertTrue(stale_rows)
        self.assertNotIn('999002', stale_rows[0]['value'])
        self.assertIn('no longer available', stale_rows[0]['value'])

    def test_ai_hints_tab_unavailable_copy_is_simple(self):
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(response, 'AI hints are not available for this problem.')
        self.assertNotContains(response, 'complexity budget')
        self.assertNotContains(response, 'smallest counterexample')
        self.assertNotContains(response, 'input-output relation')
        self.assertNotContains(response, 'classic status page')

    def test_ai_hints_tab_available_copy_is_simple(self):
        problem = create_problem(code='ws-ai-hint', is_public=True, ai_hints_enabled=True)
        with mock.patch('judge.ai_hints.is_configured', return_value=True):
            response = self.client.get(reverse('problem_detail', args=[problem.code]))
        self.assertContains(response, 'These hints help you think.')
        self.assertContains(response, 'Submit a solution first.')
        self.assertContains(response, 'Get AI Hint')
        self.assertNotContains(response, 'classic status page')
        self.assertNotContains(response, 'non-AC formal')
        self.assertNotContains(response, 'complexity budget')

    def test_ai_hints_possible_when_assignment_allows(self):
        problem = create_problem(code='ws-ai-asg', is_public=True, ai_hints_enabled=False)
        contest = create_contest(
            key='wsaiasg', name='AI Assignment',
            start_time=timezone.now() - timezone.timedelta(hours=1),
            end_time=timezone.now() + timezone.timedelta(days=1),
            allow_ai_hints=True,
            is_visible=True,
        )
        create_contest_problem(contest=contest, problem=problem, points=100, order=1)
        participation = create_contest_participation(contest=contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        with mock.patch('judge.ai_hints.is_configured', return_value=True):
            response = self.client.get(reverse('problem_detail', args=[problem.code]))
        self.assertTrue(response.context['ai_hints_possible'])
        problem.refresh_from_db()
        self.assertFalse(problem.ai_hints_enabled)

    def test_mcq_result_shows_verdict_and_score_over_max(self):
        self.mcq.points = 10
        self.mcq.save(update_fields=['points'])
        submission = Submission.objects.create(
            user=self.user.profile, problem=self.mcq, language=Language.get_answer_language(),
            result='WA', status='D', points=0, time=0, memory=0,
        )
        SubmissionSource.objects.create(
            submission=submission,
            source=json.dumps({'selected': [self.mcq_opt_wrong.id]}),
        )
        status = self.client.get(reverse(
            'problem_workspace_submission_status', args=[self.mcq.code, submission.id],
        ))
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertEqual(body['result'], 'WA')
        self.assertEqual(body['points'], 0)
        self.assertEqual(body['points_total'], 10)
        detail = self.client.get(
            reverse('problem_detail', args=[self.mcq.code]) + '?submission=%d' % submission.id,
        )
        self.assertContains(detail, 'Judging results')
        self.assertContains(detail, 'case-WA')
        self.assertContains(detail, 'Score:')
        self.assertContains(detail, '0 / 10')
        self.assertNotContains(detail, ' · 0')
        self.assertNotContains(detail, 'Execution results')
        self.assertNotContains(detail, 'Execution Results')
        self.assertNotContains(detail, 'Test case #')

    def test_exam_drawer_hides_mcq_score_until_finalize(self):
        exam = create_contest(
            key='wsexamhide', name='Workspace Exam',
            format_name='exam', is_visible=True,
            start_time=timezone.now() - timezone.timedelta(hours=1),
            end_time=timezone.now() + timezone.timedelta(hours=2),
            authors=('workspace-author',),
        )
        exam_mcq = create_contest_problem(contest=exam, problem=self.mcq, points=10, order=1)
        participation = create_contest_participation(contest=exam, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        submission = Submission.objects.create(
            user=self.user.profile, problem=self.mcq, language=Language.get_answer_language(),
            result='AC', status='D', points=10, contest_object=exam, time=0, memory=0,
        )
        ContestSubmission.objects.create(
            submission=submission, problem=exam_mcq, participation=participation, points=10,
        )

        hidden = self.client.get(reverse('problem_detail', args=[self.mcq.code]))
        self.assertEqual(hidden.context['workspace_mode'], 'exam')
        self.assertTrue(hidden.context['workspace_exam_hide_scores'])
        self.assertContains(hidden, 'drawer-score tried')
        self.assertContains(hidden, '—/10')
        self.assertContains(hidden, 'attempted-problem-color')
        self.assertNotContains(hidden, 'solved-problem-color')
        self.assertNotContains(hidden, '10/10')

        exam.results_finalized = True
        exam.save(update_fields=['results_finalized'])
        shown = self.client.get(reverse('problem_detail', args=[self.mcq.code]))
        self.assertFalse(shown.context['workspace_exam_hide_scores'])
        self.assertContains(shown, 'drawer-score got')
        self.assertContains(shown, '10/10')
        self.assertContains(shown, 'solved-problem-color')

    def test_exam_drawer_uses_latest_mcq_score_after_finalize(self):
        exam = create_contest(
            key='wsexamlatest', name='Workspace Exam Latest',
            format_name='exam', is_visible=True,
            start_time=timezone.now() - timezone.timedelta(hours=1),
            end_time=timezone.now() + timezone.timedelta(hours=2),
            authors=('workspace-author',),
            results_finalized=True,
        )
        exam_mcq = create_contest_problem(contest=exam, problem=self.mcq, points=10, order=1)
        participation = create_contest_participation(contest=exam, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        first = Submission.objects.create(
            user=self.user.profile, problem=self.mcq, language=Language.get_answer_language(),
            result='AC', status='D', points=10, contest_object=exam, time=0, memory=0,
        )
        ContestSubmission.objects.create(
            submission=first, problem=exam_mcq, participation=participation, points=10,
        )
        Submission.objects.filter(id=first.id).update(date=timezone.now() - timezone.timedelta(minutes=5))
        latest = Submission.objects.create(
            user=self.user.profile, problem=self.mcq, language=Language.get_answer_language(),
            result='WA', status='D', points=0, contest_object=exam, time=0, memory=0,
        )
        ContestSubmission.objects.create(
            submission=latest, problem=exam_mcq, participation=participation, points=0,
        )

        response = self.client.get(reverse('problem_detail', args=[self.mcq.code]))
        self.assertEqual(response.context['workspace_best_points'][self.mcq.id], 0)
        self.assertContains(response, 'drawer-score tried')
        self.assertContains(response, '0/10')
        self.assertContains(response, 'attempted-problem-color')
        self.assertNotContains(response, 'solved-problem-color')

    def test_workspace_prefills_problem_template_for_selected_language(self):
        starter = 'def solve():\n    # problem-template-marker\n    pass\n'
        ProblemTemplate.objects.create(problem=self.problem, language=self.language, code=starter)
        self.user.profile.language = self.language
        self.user.profile.save()

        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(response, 'problem-template-marker')
        self.assertContains(response, 'id="workspace-language-templates"')
        templates = response.context['workspace_language_templates']
        self.assertEqual(templates[str(self.language.id)], starter)
        self.assertEqual(response.context['form'].initial.get('source'), starter)
        html = response.content.decode()
        start = html.find('id="workspace-language-templates">')
        self.assertNotEqual(start, -1)
        start = html.find('>', start) + 1
        payload = json.loads(html[start:html.find('</script>', start)])
        self.assertEqual(payload[str(self.language.id)], starter)

    def test_language_template_ajax_prefers_problem_template(self):
        self.language.template = 'print("language-default")\n'
        self.language.save()
        ProblemTemplate.objects.create(
            problem=self.problem, language=self.language, code='print("problem-starter")\n',
        )
        response = self.client.get(reverse('language_template_ajax'), {
            'id': self.language.id,
            'problem_id': self.problem.id,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), 'print("problem-starter")\n')

    def test_language_template_ajax_falls_back_to_language_template(self):
        self.language.template = 'print("language-default")\n'
        self.language.save()
        response = self.client.get(reverse('language_template_ajax'), {
            'id': self.language.id,
            'problem_id': self.problem.id,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), 'print("language-default")\n')

    def _released_exam_submission(self, problem, *, result, note, manual_points, machine_points, finalized):
        exam = create_contest(
            key='wsrel-%s' % problem.code[:12], name='Release exam',
            format_name='exam', is_visible=True,
            start_time=timezone.now() - timezone.timedelta(hours=1),
            end_time=timezone.now() + timezone.timedelta(hours=2),
            authors=('workspace-author',),
            results_finalized=finalized,
        )
        contest_problem = create_contest_problem(contest=exam, problem=problem, points=10, order=1)
        participation = create_contest_participation(contest=exam, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save()
        submission = Submission.objects.create(
            user=self.user.profile, problem=problem, language=Language.get_answer_language(),
            result=result, status='QU' if result == 'QU' else 'D',
            points=machine_points, contest_object=exam, time=0, memory=0,
            review_note=note,
        )
        ContestSubmission.objects.create(
            submission=submission, problem=contest_problem, participation=participation,
            points=manual_points,
        )
        SubmissionManualGrade.objects.create(
            submission=submission, points=manual_points, machine_points=machine_points, released=True,
        )
        return submission

    def test_exam_hides_released_score_and_note_until_finalize(self):
        short = create_problem(code='ws-rel-short', is_public=True, submission_type='S', points=10)
        note = 'Check the units before simplifying.'
        hidden = self._released_exam_submission(
            short, result='IR', note=note, manual_points=8, machine_points=0, finalized=False,
        )
        status = self.client.get(reverse(
            'problem_workspace_submission_status', args=[short.code, hidden.id],
        ))
        self.assertIsNone(status.json()['points'])
        self.assertEqual(status.json()['review_note_html'], '')
        self.assertFalse(status.json()['released'])
        page = self.client.get(reverse('problem_detail', args=[short.code]) + '?submission=%d' % hidden.id)
        self.assertContains(page, 'Results hidden')
        self.assertNotContains(page, note)
        self.assertNotContains(page, 'Teacher feedback')

    def test_finalized_short_answer_shows_released_score_and_note(self):
        short = create_problem(code='ws-rel-short2', is_public=True, submission_type='S', points=10)
        note = 'Check the units before simplifying.'
        submission = self._released_exam_submission(
            short, result='IR', note=note, manual_points=8, machine_points=0, finalized=True,
        )
        status = self.client.get(reverse(
            'problem_workspace_submission_status', args=[short.code, submission.id],
        ))
        body = status.json()
        self.assertEqual(body['points'], 8)
        self.assertEqual(body['points_total'], 10)
        self.assertTrue(body['released'])
        self.assertIn('units', body['review_note_html'])
        page = self.client.get(reverse('problem_detail', args=[short.code]) + '?submission=%d' % submission.id)
        self.assertContains(page, 'Answer saved')
        self.assertContains(page, 'Score:')
        self.assertContains(page, '8 / 10')
        self.assertContains(page, 'Teacher feedback')
        self.assertContains(page, note)

    def test_finalized_file_shows_released_score_before_judge_status(self):
        upload = create_problem(code='ws-rel-file', is_public=True, submission_type='F', points=10)
        note = 'Cite the data table.'
        submission = self._released_exam_submission(
            upload, result='QU', note=note, manual_points=7, machine_points=0, finalized=True,
        )
        page = self.client.get(reverse('problem_detail', args=[upload.code]) + '?submission=%d' % submission.id)
        self.assertContains(page, '7 / 10')
        self.assertContains(page, note)
        self.assertNotContains(page, 'Awaiting grading')

    def test_code_result_html_includes_released_note(self):
        submission = Submission.objects.create(
            user=self.user.profile, problem=self.problem, language=self.language,
            result='WA', status='D', points=40, time=0.1, memory=128,
            review_note='Handle the empty list.',
        )
        SubmissionManualGrade.objects.create(
            submission=submission, points=55, machine_points=40, released=True,
        )
        response = self.client.get(reverse('submission_testcases_query'), {'id': submission.id})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Teacher feedback')
        self.assertContains(response, 'Handle the empty list.')
        self.assertContains(response, '55')


class ProblemWorkspaceActionsTestCase(TestCase):
    """Title-row edit/delete/clone icons and remaining Problem info Actions."""

    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        cls.student = create_user(username='ws-actions-student')
        cls.author_no_perm = create_user(username='ws-actions-author-noperm')
        cls.editor = create_user(
            username='ws-actions-editor',
            user_permissions=('edit_own_problem', 'clone_problem'),
        )
        cls.cloner = create_user(
            username='ws-actions-cloner',
            user_permissions=('clone_problem',),
        )
        cls.language = Language.objects.get(key='PY3')
        cls.problem = create_problem(
            code='wsactown',
            is_public=True,
            authors=('ws-actions-editor', 'ws-actions-author-noperm'),
        )
        cls.problem.allowed_languages.add(cls.language)

    def setUp(self):
        self.client = Client()

    def test_student_sees_voting_group_but_not_edit_delete_or_clone(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(response, 'ws-action-groups')
        self.assertContains(response, 'data-action-group="voting"')
        self.assertContains(response, 'data-problem-actions')
        self.assertNotContains(response, 'data-action-group="manage"')
        self.assertNotContains(response, 'class="ws-title-actions"')
        self.assertNotContains(response, 'data-action="delete-problem"')
        self.assertNotContains(response, 'data-problem-action="edit"')
        self.assertNotContains(response, 'data-problem-action="clone"')
        self.assertNotContains(response, reverse('problem_clone', args=[self.problem.code]))
        self.assertNotContains(response, 'data-can-delete="1"')
        self.assertContains(response, 'data-can-delete="0"')
        self.assertContains(response, 'Voting available after solving')
        self.assertContains(response, 'Voting')
        self.assertNotContains(response, 'Manage problem')
        self.assertContains(response, 'ws-action-hint')

    def test_author_without_edit_perm_does_not_see_delete(self):
        self.client.force_login(self.author_no_perm)
        self.assertFalse(self.problem.is_editable_by(self.author_no_perm))
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertNotContains(response, 'data-action="delete-problem"')
        self.assertNotContains(response, 'data-action-group="manage"')
        self.assertNotContains(response, 'data-problem-action="edit"')
        self.assertNotContains(response, 'data-problem-action="clone"')
        self.assertNotContains(response, 'data-can-delete="1"')

    def test_editor_sees_title_icons_and_delete_wired_to_bulk_delete(self):
        self.client.force_login(self.editor)
        self.assertTrue(self.problem.is_editable_by(self.editor))
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(response, 'class="ws-title-actions"')
        self.assertNotContains(response, 'data-action-group="manage"')
        self.assertNotContains(response, 'Manage problem')
        self.assertContains(response, 'data-action-group="tickets"')
        self.assertContains(response, 'Tickets')
        self.assertContains(response, 'Voting')
        self.assertContains(response, reverse('problem_edit_v2', args=[self.problem.code]))
        self.assertContains(response, reverse('problem_clone', args=[self.problem.code]))
        self.assertContains(response, 'data-problem-action="edit"')
        self.assertContains(response, 'data-action="delete-problem"')
        self.assertContains(response, 'data-problem-action="delete"')
        self.assertContains(response, 'data-problem-action="clone"')
        self.assertContains(response, 'data-can-delete="1"')
        self.assertContains(response, 'id="ws-problem-delete-form"')
        self.assertContains(response, reverse('problem_bulk_delete'))
        self.assertContains(response, 'id="ws-delete-modal"')
        self.assertNotContains(response, 'problem_window.js')
        html = response.content.decode()
        title_actions = html.split('class="ws-title-actions"', 1)[1].split('</span>', 1)[0]
        self.assertIn('data-problem-action="edit"', title_actions)
        self.assertIn('data-action="delete-problem"', title_actions)
        self.assertIn('data-problem-action="clone"', title_actions)
        self.assertIn('fa-pencil', title_actions)
        self.assertIn('fa-trash', title_actions)
        self.assertIn('fa-copy', title_actions)
        form_html = html[html.find('id="ws-problem-delete-form"'):html.find('id="ws-delete-modal"')]
        self.assertIn('name="codes"', form_html)
        self.assertIn(self.problem.code, form_html)
        self.assertIn('name="next"', form_html)
        self.assertIn(reverse('problem_list'), form_html)

    def test_clone_perm_shows_clone_icon_without_edit_or_delete(self):
        self.client.force_login(self.cloner)
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(response, 'class="ws-title-actions"')
        self.assertContains(response, 'data-problem-action="clone"')
        self.assertContains(response, reverse('problem_clone', args=[self.problem.code]))
        self.assertNotContains(response, 'data-problem-action="edit"')
        self.assertNotContains(response, 'data-action="delete-problem"')
        self.assertNotContains(response, reverse('problem_edit_v2', args=[self.problem.code]))

    def test_student_cannot_delete_via_existing_endpoint(self):
        self.client.force_login(self.student)
        response = self.client.post(reverse('problem_bulk_delete'), {
            'codes': [self.problem.code],
            'next': reverse('problem_list'),
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Problem.objects.filter(code=self.problem.code).exists())
