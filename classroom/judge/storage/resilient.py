import hashlib
import logging
import posixpath
import re
import uuid

from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError, \
    EndpointConnectionError, ReadTimeoutError
from django.conf import settings
from django.core.files.storage import Storage
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('judge.storage')

#: Extensions are whitelisted into the key; anything else is dropped (the original
#: filename is the future SubmissionFile model's business, never the key's).
_EXT_RE = re.compile(r'\.[a-z0-9]{1,10}$')

#: Primary-store failures that mean "try the backup" rather than "bubble up".
#: OSError covers paramiko/socket trouble and local FileSystemStorage in dev/tests.
CONNECTION_ERRORS = (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError,
                     BotoCoreError, OSError)


class StorageUnavailableError(Exception):
    """Both the primary store and the backup failed to serve the file."""


def generate_submission_key(submission_id, original_name, now=None):
    """Object key valid on both S3 and Box SFTP: generated charset only (Box strips
    special characters from names), year/month sharded (Box lists at most 100k
    entries per directory)."""
    now = now or timezone.now()
    ext = posixpath.splitext(original_name)[1].lower()
    if not _EXT_RE.fullmatch(ext):
        ext = ''
    return 'submissions/%04d/%02d/%d/%s%s' % (now.year, now.month, submission_id, uuid.uuid4().hex, ext)


def generate_assignment_draft_key(draft_id, original_name, now=None):
    """Object key for a file answer a student has saved but not submitted.

    Deliberately outside the ``submissions/`` tree: a draft is not a submission, and the two have
    opposite housekeeping rules. An object here that no draft row points at is garbage to sweep;
    one under ``submissions/`` is a student's graded work and must never be swept."""
    now = now or timezone.now()
    ext = posixpath.splitext(original_name)[1].lower()
    if not _EXT_RE.fullmatch(ext):
        ext = ''
    return 'assignment_drafts/%04d/%02d/%d/%s%s' % (
        now.year, now.month, draft_id or 0, uuid.uuid4().hex, ext,
    )


def generate_class_material_key(class_id, original_name, now=None):
    """Object key for course-class materials (same store as submission files)."""
    now = now or timezone.now()
    ext = posixpath.splitext(original_name)[1].lower()
    if not _EXT_RE.fullmatch(ext):
        ext = ''
    return 'class_materials/%04d/%02d/%d/%s%s' % (
        now.year, now.month, class_id or 0, uuid.uuid4().hex, ext,
    )


def generate_editor_attachment_key(user_id, original_name, now=None):
    """Object key for files attached from the rich-text editor (same store as submissions)."""
    now = now or timezone.now()
    ext = posixpath.splitext(original_name)[1].lower()
    if not _EXT_RE.fullmatch(ext):
        ext = ''
    return 'editor_attachments/%04d/%02d/%d/%s%s' % (
        now.year, now.month, user_id or 0, uuid.uuid4().hex, ext,
    )


def generate_problem_import_key(draft_id, original_name, now=None):
    """Object key for a test-data ZIP held by an Excel import draft (hidden test data: private store,
    random name). The file is deleted once the problem is created or the draft is discarded."""
    now = now or timezone.now()
    ext = posixpath.splitext(original_name)[1].lower()
    if not _EXT_RE.fullmatch(ext):
        ext = ''
    return 'import_test_data/%04d/%02d/%d/%s%s' % (now.year, now.month, draft_id or 0, uuid.uuid4().hex, ext)


def generate_seb_config_key(scope, original_name, now=None):
    """Object key for Safe Exam Browser ``.seb`` configs (same store as submission files).

    ``scope`` separates staff-published presets from per-contest uploads; it is
    sanitised because it reaches an object key (Box strips odd characters from
    names, and a caller-supplied '/' would silently reshape the hierarchy)."""
    now = now or timezone.now()
    ext = posixpath.splitext(original_name)[1].lower()
    if not _EXT_RE.fullmatch(ext):
        ext = ''
    scope = re.sub(r'[^a-z0-9_-]', '', str(scope).lower())[:40] or 'misc'
    return 'seb_configs/%04d/%02d/%s/%s%s' % (now.year, now.month, scope, uuid.uuid4().hex, ext)


class ResilientStorage(Storage):
    """Composite storage: every write goes to the primary and is queued for an async
    mirror to the backup; reads fall back to the backup when the primary is sick.

    The future file-upload question type only ever sees the plain Django Storage API.
    """

    def __init__(self, primary, backup=None, mirror_enabled=None):
        self.primary = primary
        self.backup = backup
        if mirror_enabled is None:
            mirror_enabled = settings.DMOJ_BACKUP_MIRROR_ENABLED
        self.mirror_enabled = mirror_enabled and backup is not None

    # -- writes ------------------------------------------------------------------

    def _save(self, name, content):
        # We hash the content in a first pass and rewind before handing it to the
        # primary; a non-rewindable stream would be stored truncated while the record
        # claims the full digest. Fail loudly instead of silently corrupting.
        if not getattr(content, 'seekable', lambda: True)():
            raise ValueError('ResilientStorage needs seekable content (it is hashed before '
                             'storage); wrap non-seekable streams in a File/ContentFile first')
        hasher = hashlib.sha256()
        size = 0
        for chunk in content.chunks():
            hasher.update(chunk)
            size += len(chunk)
        if hasattr(content, 'seek'):
            content.seek(0)

        name = self.primary._save(name, content)

        from judge.models.stored_file import StoredFileRecord
        try:
            record, _ = StoredFileRecord.objects.update_or_create(path=name, defaults={
                'sha256': hasher.hexdigest(),
                'size': size,
                'status': StoredFileRecord.STATUS_PENDING,
                'mirrored_at': None,
                'attempts': 0,
                'last_error': '',
            })
        except Exception:
            # The primary object is already written; without a tracking row it is an
            # orphan the mirror/restore machinery can never see. Best-effort remove it.
            # (An enclosing request that rolls back *after* we return can still orphan
            # the object — that residual window is accepted.)
            try:
                self.primary.delete(name)
            except Exception:
                logger.exception('could not clean up orphan primary object %s', name)
            raise
        if self.mirror_enabled:
            # on_commit: the mirror task must only fire once the row referencing this
            # file is durable; outside a transaction it fires immediately.
            transaction.on_commit(lambda: self._enqueue_mirror(record.id))
        return name

    def _enqueue_mirror(self, record_id):
        from judge.tasks import mirror_to_backup
        mirror_to_backup.delay(record_id)

    # -- reads -------------------------------------------------------------------

    def _open(self, name, mode='rb'):
        try:
            # Cheap HEAD-equivalent: forces connection/permission/404 errors to surface
            # *before* FileResponse starts streaming (django-storages opens lazily).
            self.primary.size(name)
        except Exception as e:
            if not self.mirror_enabled or not self._should_fall_back(e):
                raise
            if self._is_deleted(name):
                # Deliberately deleted from the primary. The backup still holds the
                # bytes for disaster restore, but we must not resurrect a deleted file
                # on a routine read — re-raise the primary's error (404 semantics).
                raise
            logger.warning('Primary storage failed for %s (%s); falling back to backup',
                           name, type(e).__name__)
            return self._open_backup(name, e)
        return self.primary.open(name, mode)

    def _is_deleted(self, name):
        from judge.models.stored_file import StoredFileRecord
        status = (StoredFileRecord.objects.filter(path=name)
                  .values_list('status', flat=True).first())
        return status == StoredFileRecord.STATUS_DELETED

    def _should_fall_back(self, exc):
        if isinstance(exc, ClientError):
            code = str(exc.response.get('Error', {}).get('Code', ''))
            status = exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0)
            # A missing primary object with a mirrored backup copy is exactly the
            # data-loss case the backup exists for.
            return code in ('404', 'NoSuchKey') or status >= 500
        return isinstance(exc, CONNECTION_ERRORS)

    def _open_backup(self, name, primary_exc):
        try:
            if hasattr(self.backup, 'open_stream'):
                return self.backup.open_stream(name)
            return self.backup.open(name, 'rb')
        except FileNotFoundError:
            # Never mirrored: keep 404 semantics rather than masking as a 5xx.
            raise
        except Exception as e:
            raise StorageUnavailableError(
                f'both stores failed for {name}: primary {primary_exc!r}, backup {e!r}') from e

    # -- deletes: primary only; the backup keeps everything (that is its job) -----

    def delete(self, name):
        self.primary.delete(name)
        from judge.models.stored_file import StoredFileRecord
        record = StoredFileRecord.objects.filter(path=name).first()
        if record is not None:
            if record.status == StoredFileRecord.STATUS_PENDING:
                logger.warning('%s deleted before it was mirrored; the backup will never have it', name)
            record.status = StoredFileRecord.STATUS_DELETED
            record.save(update_fields=['status'])

    # -- plain delegation ----------------------------------------------------------

    def exists(self, name):
        return self.primary.exists(name)

    def size(self, name):
        return self.primary.size(name)

    def url(self, name):
        return self.primary.url(name)

    def get_available_name(self, name, max_length=None):
        return self.primary.get_available_name(name, max_length=max_length)

    def get_accessed_time(self, name):
        return self.primary.get_accessed_time(name)

    def get_created_time(self, name):
        return self.primary.get_created_time(name)

    def get_modified_time(self, name):
        return self.primary.get_modified_time(name)
