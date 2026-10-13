"""Server-side answer drafts for assignment participants.

Students answer assignment problems in the workspace, but nothing reaches the server until they
press Submit. Anything they typed and did not submit used to live only in ``localStorage``, so it
was lost when the assignment closed. A draft mirrors the in-progress answer on the server so the
auto-submit that runs when the assignment closes has something to submit.

A draft is deliberately *not* a ``Submission``: it must stay out of submission lists, scoreboards,
Grade pages and exports. Keeping it in its own table makes that structural rather than a filter
every query has to remember.

Successfully submitting a problem deletes its draft, so a surviving draft means exactly one of two
things: the problem was never submitted, or it was submitted and then edited again.
"""
import json

from django.db import models
from django.db.models import CASCADE
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from judge.models.contest import ContestParticipation
from judge.models.problem import Problem, ProblemSubmissionType, ProblemTemplate
from judge.models.runtime import Language
from judge.storage import generate_assignment_draft_key, submission_storage

__all__ = ['AssignmentDraft', 'AssignmentDraftFile']


def _json_payload(content):
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


class AssignmentDraft(models.Model):
    """The latest unsubmitted answer a participant has for one assignment problem."""

    participation = models.ForeignKey(ContestParticipation, verbose_name=_('participation'),
                                      related_name='drafts', on_delete=CASCADE)
    problem = models.ForeignKey(Problem, verbose_name=_('problem'), related_name='assignment_drafts',
                                on_delete=CASCADE)
    # Serialized the same way the matching question type writes SubmissionSource, so harvesting can
    # hand the payload straight to the grader: raw source for code, {"selected": [...]} for
    # multiple choice, {"answers": [...]} for fill-in-the-blank, {"text": "..."} for short answer.
    content = models.TextField(verbose_name=_('draft content'), max_length=65536, blank=True)
    language = models.ForeignKey(Language, verbose_name=_('language'), null=True, blank=True,
                                 on_delete=models.SET_NULL)
    updated_on = models.DateTimeField(verbose_name=_('last saved'), auto_now=True)
    harvested_at = models.DateTimeField(verbose_name=_('harvested on'), null=True, blank=True,
                                        help_text=_('When auto-submit turned this draft into a submission.'))

    def __str__(self):
        return _('Draft of %(user)s for %(problem)s') % {
            'user': self.participation.user.username,
            'problem': self.problem.code,
        }

    def has_content(self):
        """Whether this draft holds a real answer worth submitting.

        Blank answers and untouched starter code are not answers: auto-submitting them would file
        empty work on the student's behalf and, for a whole class at once, put it through the judge.
        """
        submission_type = self.problem.submission_type
        if submission_type == ProblemSubmissionType.CODE:
            source = (self.content or '').strip()
            if not source:
                return False
            if self.language_id is None:
                return False
            starter = ProblemTemplate.starter_code(self.language, self.problem_id)
            return source != (starter or '').strip()
        if submission_type == ProblemSubmissionType.MCQ:
            payload = _json_payload(self.content)
            return bool(payload and payload.get('selected'))
        if submission_type == ProblemSubmissionType.FILL_BLANK:
            payload = _json_payload(self.content)
            answers = (payload or {}).get('answers') or []
            return any(str(answer).strip() for answer in answers)
        if submission_type == ProblemSubmissionType.SHORT_ANSWER:
            payload = _json_payload(self.content)
            return bool(str((payload or {}).get('text') or '').strip())
        if submission_type == ProblemSubmissionType.FILE:
            # A file draft is the uploaded object, not this text field.
            return hasattr(self, 'upload')
        return bool((self.content or '').strip())

    class Meta:
        unique_together = ('participation', 'problem')
        indexes = [
            # Harvesting scans unharvested drafts for one contest, so lead with the flag.
            models.Index(fields=['harvested_at', 'participation']),
        ]
        verbose_name = _('assignment draft')
        verbose_name_plural = _('assignment drafts')


def assignment_draft_upload_to(instance, filename):
    """upload_to: generated object key; the student's own filename stays on ``original_name``."""
    return generate_assignment_draft_key(instance.draft_id, filename)


class AssignmentDraftFile(models.Model):
    """The file a participant uploaded as an in-progress answer to a file-upload problem.

    Every other question type fits in ``AssignmentDraft.content``; a file does not, so it gets a
    row of its own and an object in ``submission_storage`` — the same store the submitted file
    ends up in, so harvesting is a copy within one backend rather than a move between two.

    ``original_name`` is kept here for the same reason ``SubmissionFile`` keeps it: the storage key
    is generated, and the student's own filename is what a teacher is offered on download. Harvest
    carries it across, so the file a teacher grades is named the way the student saved it.
    """
    draft = models.OneToOneField(AssignmentDraft, verbose_name=_('draft'), related_name='upload',
                                 on_delete=CASCADE)
    file = models.FileField(verbose_name=_('uploaded file'), storage=submission_storage,
                            upload_to=assignment_draft_upload_to, max_length=255)
    original_name = models.CharField(verbose_name=_('original filename'), max_length=255)
    size = models.PositiveBigIntegerField(verbose_name=_('file size'), default=0)
    uploaded_on = models.DateTimeField(verbose_name=_('uploaded on'), auto_now=True)

    def __str__(self):
        return self.original_name

    class Meta:
        verbose_name = _('assignment draft file')
        verbose_name_plural = _('assignment draft files')


@receiver(pre_delete, sender=AssignmentDraftFile)
def _assignment_draft_file_delete_stored_object(sender, instance, **kwargs):
    """Drop the bytes with the row, including when the draft or participation cascades away.

    Harvesting has already copied anything worth keeping into a ``SubmissionFile``, so what is
    left here is a student's unsubmitted work: storing it past the row that explains it is both a
    bill and a place to leak it from.
    """
    if not instance.file:
        return
    try:
        instance.file.delete(save=False)
    except Exception:
        # A store that is briefly unreachable must not block deleting the row; the object is then
        # unreferenced, which is what the storage sweep is for.
        pass
