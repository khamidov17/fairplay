import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage

from judge.storage.resilient import ResilientStorage, StorageUnavailableError, \
    generate_assignment_draft_key, generate_class_material_key, generate_editor_attachment_key, \
    generate_problem_import_key, generate_submission_key

__all__ = ['submission_storage', 'ResilientStorage', 'StorageUnavailableError',
           'generate_assignment_draft_key', 'generate_class_material_key',
           'generate_editor_attachment_key', 'generate_problem_import_key', 'generate_submission_key']

_submission_storage = None


def submission_storage():
    """Process-wide submission-file storage. Pass this callable itself as
    FileField(storage=submission_storage) so nothing is baked into migrations and
    tests can substitute their own ResilientStorage."""
    global _submission_storage
    if _submission_storage is None:
        _submission_storage = ResilientStorage(primary=_make_primary(), backup=_make_backup())
    return _submission_storage


def _local_root():
    return settings.DMOJ_SUBMISSION_FILE_ROOT or os.path.join(settings.BASE_DIR, 'submission_files')


def _make_primary():
    if settings.DMOJ_S3_ENDPOINT_URL:
        from judge.storage.s3 import SubmissionFileStorage
        return SubmissionFileStorage()
    # Dev/CI mode: no object store configured.
    return FileSystemStorage(location=_local_root())


def _make_backup():
    if not settings.DMOJ_BACKUP_MIRROR_ENABLED:
        return None
    if settings.DMOJ_BOX_SFTP_USER:
        from judge.storage.box import BoxSFTPStorage
        return BoxSFTPStorage()
    # Mirror enabled without Box credentials: local directory backup (dev/tests).
    return FileSystemStorage(location=_local_root() + '_backup')
