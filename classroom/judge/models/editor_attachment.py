import uuid

from django.db import models
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from judge.storage import generate_editor_attachment_key, submission_storage

__all__ = ['EditorAttachment']


def editor_attachment_upload_to(instance, filename):
    """upload_to: opaque OSS key (the human-facing name stays on ``original_name``)."""
    return generate_editor_attachment_key(instance.uploaded_by_id, filename)


class EditorAttachment(models.Model):
    """A file inserted into Markdown via the rich-text editor.

    Bytes live in ``submission_storage`` (S3/OSS primary + async Box mirror), same
    pipeline as class materials and file-upload submissions. The public URL is a
    Django download view keyed by ``uuid`` — never a raw MEDIA or presigned URL.
    """
    uuid = models.UUIDField(verbose_name=_('uuid'), default=uuid.uuid4, unique=True, editable=False)
    uploaded_by = models.ForeignKey(
        'Profile', verbose_name=_('uploaded by'), null=True, blank=True,
        related_name='editor_attachments', on_delete=models.SET_NULL,
    )
    original_name = models.CharField(verbose_name=_('original filename'), max_length=255)
    file = models.FileField(
        verbose_name=_('file'), storage=submission_storage,
        upload_to=editor_attachment_upload_to, max_length=255,
    )
    size = models.PositiveBigIntegerField(verbose_name=_('file size'), default=0)
    created = models.DateTimeField(verbose_name=_('created'), auto_now_add=True)

    def __str__(self):
        return self.original_name

    class Meta:
        verbose_name = _('editor attachment')
        verbose_name_plural = _('editor attachments')

    def delete_stored_file(self):
        if not self.file:
            return
        try:
            self.file.delete(save=False)
        except Exception:
            self.file = None


@receiver(pre_delete, sender=EditorAttachment)
def _editor_attachment_delete_oss_file(sender, instance, **kwargs):
    instance.delete_stored_file()
