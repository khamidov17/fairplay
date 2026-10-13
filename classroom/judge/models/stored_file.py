from django.db import models
from django.utils.translation import gettext_lazy as _

__all__ = ['StoredFileRecord']


class StoredFileRecord(models.Model):
    """Backup-mirror bookkeeping for one object in the submission file store.

    Created by ResilientStorage on every save; drives the async Box mirror task,
    the reconciliation sweep, integrity verification, and backup restore. The
    ``path`` is simultaneously the S3 object key and the Box SFTP path under
    DMOJ_BOX_SFTP_ROOT, so restore never needs to list the backup remote.
    """
    STATUS_PENDING = 'P'
    STATUS_MIRRORED = 'M'
    STATUS_FAILED = 'F'
    STATUS_DELETED = 'D'
    STATUS_ABANDONED = 'A'
    STATUS_CHOICES = (
        (STATUS_PENDING, _('Pending mirror')),
        (STATUS_MIRRORED, _('Mirrored')),
        (STATUS_FAILED, _('Mirror failed')),
        (STATUS_DELETED, _('Deleted from primary')),
        (STATUS_ABANDONED, _('Abandoned (needs manual action)')),
    )

    path = models.CharField(verbose_name=_('storage path'), max_length=255, unique=True)
    sha256 = models.CharField(verbose_name=_('SHA-256'), max_length=64)
    size = models.PositiveBigIntegerField(verbose_name=_('size (bytes)'))
    status = models.CharField(verbose_name=_('status'), max_length=1, choices=STATUS_CHOICES,
                              default=STATUS_PENDING)
    created_on = models.DateTimeField(verbose_name=_('created on'), auto_now_add=True)
    mirrored_at = models.DateTimeField(verbose_name=_('mirrored at'), null=True, blank=True)
    attempts = models.PositiveIntegerField(verbose_name=_('mirror attempts'), default=0)
    last_error = models.TextField(verbose_name=_('last error'), blank=True)

    def __str__(self):
        return f'{self.path} [{self.get_status_display()}]'

    class Meta:
        indexes = [models.Index(fields=('status', 'created_on'))]
        verbose_name = _('stored file record')
        verbose_name_plural = _('stored file records')
