from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from judge.models import StoredFileRecord
from judge.storage import submission_storage
from judge.storage.mirror import _stream_sha256


class Command(BaseCommand):
    help = ('Restore submission files from the Box backup to the primary S3 store. Driven '
            'by MIRRORED StoredFileRecord rows (never lists the backup remote), verified '
            'against the stored sha256. By default only copies objects missing from the '
            'primary; --force re-copies everything matched.')

    def add_arguments(self, parser):
        parser.add_argument('--prefix', default='', help='only restore paths with this prefix '
                                                         '(e.g. submissions/2026/)')
        parser.add_argument('--force', action='store_true',
                            help='overwrite objects that already exist on the primary')
        parser.add_argument('--dry-run', action='store_true', help='report what would be restored')

    def handle(self, *args, **options):
        storage = submission_storage()
        primary, backup = storage.primary, storage.backup
        if backup is None:
            raise CommandError('no backup storage configured (DMOJ_BACKUP_MIRROR_ENABLED off?)')

        records = StoredFileRecord.objects.filter(status=StoredFileRecord.STATUS_MIRRORED)
        if options['prefix']:
            records = records.filter(path__startswith=options['prefix'])

        restored = skipped = failed = 0
        for record in records.iterator():
            if not options['force'] and primary.exists(record.path):
                skipped += 1
                continue
            if options['dry_run']:
                self.stdout.write(f'would restore {record.path}')
                restored += 1
                continue
            try:
                # Verify the backup copy BEFORE touching the primary, so a corrupt or
                # unreadable backup can never make --force delete a good primary object.
                with backup.open(record.path, 'rb') as src:
                    digest, size = _stream_sha256(src)
                if digest != record.sha256 or size != record.size:
                    raise ValueError('backup copy fails sha256/size check; refusing to restore')

                self._copy_to_primary(primary, backup, record)
                restored += 1
                self.stdout.write(f'restored {record.path}')
            except Exception as e:
                failed += 1
                self.stderr.write(f'FAILED {record.path}: {type(e).__name__}: {e}')

        self.stdout.write(f'restored={restored} skipped={skipped} failed={failed}')
        if failed:
            raise CommandError(f'{failed} files failed to restore')

    def _copy_to_primary(self, primary, backup, record):
        """Copy the (already verified) backup object onto the primary, never leaving a
        corrupt object behind.

        Object stores overwrite a key atomically, so writing straight over the old
        object is safer than deleting it first — the delete is what opens the window
        where the primary holds nothing. FileSystemStorage is the exception: its _save
        opens with O_EXCL and silently writes to a *different* name when the path is
        taken, so there the old file has to go first.
        """
        if hasattr(primary, 'path') and primary.exists(record.path):
            primary.delete(record.path)

        written = None
        try:
            with backup.open(record.path, 'rb') as src:
                written = primary._save(record.path, File(src, name=record.path))
            if written != record.path:
                # The backend renamed instead of overwriting: record.path still holds
                # the old object, so the check below would pass while the restore
                # silently did nothing.
                raise ValueError(f'primary storage wrote {written} instead of {record.path}')

            with primary.open(record.path, 'rb') as check:
                digest, size = _stream_sha256(check)
            if digest != record.sha256 or size != record.size:
                raise ValueError('sha256/size mismatch after restore')
        except Exception:
            # An object that exists but is wrong would be skipped by the next run
            # (which only restores what is missing), so remove it and let a plain
            # rerun try again — the backup copy is still authoritative.
            stray = written if written and written != record.path else record.path
            try:
                if primary.exists(stray):
                    primary.delete(stray)
            except Exception:
                self.stderr.write(f'WARNING: {stray} may be left corrupt on the primary; '
                                  f'delete it before rerunning')
            raise
