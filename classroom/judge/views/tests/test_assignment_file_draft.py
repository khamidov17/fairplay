"""File answers that are saved but not submitted, and what becomes of them at the deadline.

Every other question type fits in ``AssignmentDraft.content``, so a file needs its own upload and
its own object in storage. Two things make it different from the text drafts and are what these
tests are mostly about: an upload can fail halfway, and a file that has been collected exists in
two places at once until the draft copy is dropped.
"""
import tempfile
from datetime import timedelta
from unittest import mock

from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import AssignmentDraft, AssignmentDraftFile, ContestParticipation, ContestProblem, \
    ProblemFileConfig, Submission, SubmissionFile
from judge.models.tests.util import create_contest, create_problem, create_user
from judge.storage.resilient import ResilientStorage
from judge.utils.assignment_harvest import harvest_participation
from judge.utils.contest_grade import build_grade_payload

PDF = b'%PDF-1.4 the student answer'


def _pdf(name='report.pdf', content=PDF):
    return SimpleUploadedFile(name, content, content_type='application/pdf')


class FileDraftTestMixin:
    fixtures = ['language_all.json']

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.teacher = create_user(username='filedraft_teacher')
        cls.student = create_user(username='filedraft_student')
        cls.problem = create_problem('filedraft-report', submission_type='F', is_public=False,
                                     points=50, partial=True)
        ProblemFileConfig.objects.create(problem=cls.problem, allowed_extensions='pdf',
                                         max_file_size=1024)
        cls.contest = create_contest(
            key='filedraftasn', name='File draft assignment', start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=1), authors=('filedraft_teacher',), is_visible=True,
        )
        cls.contest_problem = ContestProblem.objects.create(
            contest=cls.contest, problem=cls.problem, points=50, order=1,
        )

    def setUp(self):
        super().setUp()
        # Draft files and submission files share one backend in production, so promoting a draft
        # is a copy within a single store. Pointing both at the same temp dir keeps that true.
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = ResilientStorage(primary=FileSystemStorage(location=self._tmp.name), backup=None)
        self._patches = [
            mock.patch.object(AssignmentDraftFile._meta.get_field('file'), 'storage', self.storage),
            mock.patch.object(SubmissionFile._meta.get_field('file'), 'storage', self.storage),
        ]
        for patch in self._patches:
            patch.start()
        self.participation = ContestParticipation.objects.create(
            contest=self.contest, user=self.student.profile,
        )
        self.client = Client()
        self.client.force_login(self.student)
        self.student.profile.current_contest = self.participation
        self.student.profile.save()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()
        super().tearDown()

    def _url(self, problem=None):
        return reverse('assignment_draft', args=[self.contest.key, (problem or self.problem).code])

    def _upload(self, upload=None, **kwargs):
        return self.client.post(self._url(**kwargs), {'file': upload or _pdf()})

    def _draft_file(self):
        return AssignmentDraftFile.objects.filter(draft__participation=self.participation).first()


class FileDraftUploadTestCase(FileDraftTestMixin, TestCase):
    """Saving a file answer, and the ways an upload can go wrong without costing the old one."""

    def test_uploading_saves_the_file_as_a_draft(self):
        response = self._upload()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['file'], {'name': 'report.pdf', 'size': len(PDF)})
        stored = self._draft_file()
        self.assertEqual(stored.original_name, 'report.pdf')
        self.assertEqual(stored.size, len(PDF))
        self.assertEqual(stored.file.read(), PDF)
        # The generated key is what reaches storage; the student's own name stays on the row.
        self.assertTrue(stored.file.name.startswith('assignment_drafts/'))
        self.assertTrue(stored.draft.has_content())

    def test_uploading_again_replaces_the_previous_file(self):
        self._upload()
        superseded = self._draft_file().file.name

        self._upload(_pdf('final.pdf', b'%PDF-1.4 the better answer'))

        self.assertEqual(AssignmentDraftFile.objects.count(), 1)
        stored = self._draft_file()
        self.assertEqual(stored.original_name, 'final.pdf')
        self.assertEqual(stored.file.read(), b'%PDF-1.4 the better answer')
        # Keeping the old object would leave unsubmitted work lying around with nothing pointing
        # at it, and bill for it forever.
        self.assertFalse(self.storage.exists(superseded))

    def test_a_rejected_file_leaves_the_saved_one_alone(self):
        # Picking the wrong file by mistake must not cost the student the answer they had.
        self._upload()
        kept = self._draft_file().file.name

        response = self._upload(SimpleUploadedFile('notes.exe', b'MZ', content_type='application/exe'))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._draft_file().file.name, kept)
        self.assertTrue(self.storage.exists(kept))

    def test_a_file_of_the_wrong_type_is_not_saved(self):
        # The submit path refuses this file, so a draft of it could only ever fail at the deadline.
        response = self._upload(SimpleUploadedFile('notes.exe', b'MZ', content_type='application/exe'))

        self.assertEqual(response.status_code, 400)
        self.assertIn('not allowed', response.json()['error'])
        self.assertIsNone(self._draft_file())

    def test_a_file_over_the_size_limit_is_not_saved(self):
        response = self._upload(_pdf('huge.pdf', b'%PDF-1.4 ' + b'x' * 2048))

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self._draft_file())

    def test_saving_nothing_is_rejected(self):
        response = self.client.post(self._url(), {})

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self._draft_file())

    def test_a_spectator_cannot_save_a_file(self):
        # The file branch has to be behind the same gate as the text one: a spectator's answer is
        # never collected, so a draft of theirs is work auto-submit must not act on.
        ContestParticipation.objects.filter(pk=self.participation.pk).update(
            virtual=ContestParticipation.SPECTATE,
        )

        response = self._upload()

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self._draft_file())

    def test_deleting_the_draft_takes_the_file_with_it(self):
        self._upload()
        name = self._draft_file().file.name

        response = self.client.delete(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AssignmentDraftFile.objects.exists())
        self.assertFalse(self.storage.exists(name))

    def test_a_new_file_makes_a_collected_draft_collectable_again(self):
        self._upload()
        AssignmentDraft.objects.filter(participation=self.participation).update(
            harvested_at=timezone.now(),
        )

        self._upload(_pdf('second.pdf', b'%PDF-1.4 second attempt'))

        draft = AssignmentDraft.objects.get(participation=self.participation)
        self.assertIsNone(draft.harvested_at)

    def test_the_saved_file_is_shown_on_the_problem_page(self):
        # A file input cannot be prefilled, so without this line the page is indistinguishable
        # from one where nothing was ever saved.
        self._upload()

        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))

        self.assertEqual(response.context['workspace_draft_upload'],
                         {'name': 'report.pdf', 'size': len(PDF)})
        self.assertContains(response, 'Saved: report.pdf')

    def test_a_problem_with_no_saved_file_shows_nothing(self):
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))

        self.assertIsNone(response.context['workspace_draft_upload'])
        self.assertNotContains(response, 'Saved: ')

    def test_uploading_after_hand_submit_saves_a_new_draft(self):
        # A successful submit deletes the draft. Picking another file has to create one again,
        # otherwise a student who revises after submitting once has nothing for auto-submit.
        self.client.post(reverse('problem_submit', args=[self.problem.code]), {'file': _pdf()})

        response = self._upload(_pdf('revised.pdf', b'%PDF-1.4 the revised answer'))

        self.assertEqual(response.status_code, 200)
        stored = self._draft_file()
        self.assertEqual(stored.original_name, 'revised.pdf')
        self.assertEqual(stored.file.read(), b'%PDF-1.4 the revised answer')
        page = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertEqual(page.context['workspace_draft_upload'],
                         {'name': 'revised.pdf', 'size': len(b'%PDF-1.4 the revised answer')})


class FileDraftHarvestTestCase(FileDraftTestMixin, TestCase):
    """At the deadline the saved file has to become a submission a teacher can actually open."""

    def _harvest(self):
        return harvest_participation(self.participation)

    def test_the_saved_file_becomes_a_submission(self):
        self._upload()

        created = self._harvest()

        self.assertEqual(len(created), 1)
        submission = created[0]
        self.assertEqual(submission.problem, self.problem)
        self.assertEqual(submission.contest_object, self.contest)
        self.assertEqual(submission.upload.original_name, 'report.pdf')
        self.assertEqual(submission.upload.size, len(PDF))
        self.assertEqual(submission.upload.file.read(), PDF)

    def test_the_submission_gets_its_own_copy_of_the_bytes(self):
        # Assigning the draft's FieldFile straight to the submission would re-point the new row at
        # the *same* object rather than copying it, and dropping the draft would then delete the
        # file the teacher is meant to grade. Reading it back after the draft is gone is the only
        # way to tell the two apart.
        self._upload()
        draft_key = self._draft_file().file.name

        submission = self._harvest()[0]

        self.assertNotEqual(submission.upload.file.name, draft_key)
        self.assertTrue(submission.upload.file.name.startswith('submissions/'))
        self.assertFalse(AssignmentDraftFile.objects.exists())
        self.assertFalse(self.storage.exists(draft_key))
        self.assertEqual(submission.upload.file.read(), PDF)

    def test_the_teacher_sees_the_file_on_the_grade_page(self):
        self._upload()
        submission = self._harvest()[0]

        row = build_grade_payload([submission], {submission.id})[0]

        self.assertEqual(row['problem_type'], 'F')
        self.assertEqual(row['upload']['original_name'], 'report.pdf')
        self.assertEqual(row['upload']['download_url'],
                         reverse('submission_file_download', args=(submission.id,)))

    def test_a_one_shot_store_stream_is_still_collected(self):
        # Production reads go to S3: the object is there, but the stream cannot rewind.
        # ResilientStorage hashes then writes, so wrapping that stream used to raise,
        # release the claim, and leave Grade with only the hand-submitted file.
        self._upload()

        original_open = self.storage.primary.open

        class OneShotStream:
            def __init__(self, data):
                self._data = data
                self._pos = 0
                self.closed = False

            def read(self, size=-1):
                if size is None or size < 0:
                    chunk = self._data[self._pos:]
                    self._pos = len(self._data)
                    return chunk
                chunk = self._data[self._pos:self._pos + size]
                self._pos += len(chunk)
                return chunk

            def seekable(self):
                return False

            def seek(self, *args, **kwargs):
                raise OSError('not seekable')

            def close(self):
                self.closed = True

        def open_once(name, mode='rb'):
            return OneShotStream(original_open(name, mode).read())

        with mock.patch.object(self.storage.primary, 'open', side_effect=open_once):
            created = self._harvest()

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].upload.original_name, 'report.pdf')
        self.assertEqual(created[0].upload.file.read(), PDF)

    def test_the_teacher_can_download_what_was_collected(self):
        self._upload()
        submission = self._harvest()[0]
        self.client.force_login(self.teacher)

        response = self.client.get(reverse('submission_file_download', args=(submission.id,)))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), PDF)

    def test_a_file_problem_with_no_upload_submits_nothing(self):
        # The row can exist without a file: it is created before the bytes are stored.
        AssignmentDraft.objects.create(participation=self.participation, problem=self.problem)

        self.assertEqual(self._harvest(), [])
        self.assertFalse(Submission.objects.filter(contest_object=self.contest).exists())

    def test_collecting_twice_does_not_submit_twice(self):
        self._upload()

        self._harvest()
        self.assertEqual(self._harvest(), [])

        self.assertEqual(Submission.objects.filter(contest_object=self.contest).count(), 1)

    def test_a_file_submitted_by_hand_is_not_collected_again(self):
        self._upload()
        self.client.post(reverse('problem_submit', args=[self.problem.code]), {'file': _pdf()})

        self.assertEqual(self._harvest(), [])
        self.assertEqual(Submission.objects.filter(contest_object=self.contest).count(), 1)

    def test_a_new_file_after_hand_submit_is_saved_and_collected(self):
        # Submit once, then pick a replacement: that second file is the answer auto-submit must
        # still collect. The first upload is already a real submission and must be left alone.
        self._upload()
        self.client.post(reverse('problem_submit', args=[self.problem.code]), {'file': _pdf()})

        response = self._upload(_pdf('revised.pdf', b'%PDF-1.4 the revised answer'))

        self.assertEqual(response.status_code, 200)
        stored = self._draft_file()
        self.assertEqual(stored.original_name, 'revised.pdf')
        self.assertTrue(stored.draft.has_content())

        created = self._harvest()

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].upload.original_name, 'revised.pdf')
        self.assertEqual(created[0].upload.file.read(), b'%PDF-1.4 the revised answer')
        self.assertEqual(Submission.objects.filter(contest_object=self.contest).count(), 2)

    def test_a_file_the_store_cannot_produce_costs_only_itself(self):
        # Reading a file draft goes to the object store, which fails in ways this code has no list
        # of. Whatever comes back, the student's other answers still have to be collected, and the
        # unreadable draft must not be left holding a claim: the claim is what tells every later
        # sweep to skip it, so an answer that keeps one without a submission is unrecoverable.
        essay = create_problem('filedraft-essay', submission_type='S', is_public=False,
                               points=10, partial=True)
        ContestProblem.objects.create(contest=self.contest, problem=essay, points=10, order=2)
        AssignmentDraft.objects.create(participation=self.participation, problem=essay,
                                       content='{"text": "the rest of the paper"}')
        self._upload()
        self.storage.delete(self._draft_file().file.name)

        created = self._harvest()

        self.assertEqual([submission.problem_id for submission in created], [essay.id])
        self.assertIsNone(AssignmentDraft.objects.get(problem=self.problem).harvested_at)
