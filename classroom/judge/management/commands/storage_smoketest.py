import uuid

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from judge.models import StoredFileRecord
from judge.storage import submission_storage
from judge.storage.mirror import mirror_record


class Command(BaseCommand):
    help = ('End-to-end probe of the submission-file storage against the real backends: '
            'write through ResilientStorage, mirror synchronously, read back from both '
            'stores, clean up. Run once at deploy time.')

    def handle(self, *args, **options):
        storage = submission_storage()
        payload = b'DKUOJ storage smoketest ' + uuid.uuid4().hex.encode()
        name = f'smoketest/{uuid.uuid4().hex}.txt'

        name = storage.save(name, ContentFile(payload))
        self.stdout.write(f'saved to primary: {name}')
        record = StoredFileRecord.objects.get(path=name)

        try:
            if storage.mirror_enabled:
                mirror_record(record)
                record.refresh_from_db()
                self.stdout.write(f'mirrored to backup: status={record.get_status_display()}')
            else:
                self.stdout.write('mirror disabled (DMOJ_BACKUP_MIRROR_ENABLED off); skipping backup leg')

            with storage.primary.open(name, 'rb') as f:
                if f.read() != payload:
                    raise CommandError('primary read-back mismatch')
            self.stdout.write('primary read-back OK')

            if storage.mirror_enabled:
                opener = getattr(storage.backup, 'open_stream', None) or \
                    (lambda n: storage.backup.open(n, 'rb'))
                f = opener(name)
                try:
                    if f.read() != payload:
                        raise CommandError('backup read-back mismatch')
                finally:
                    f.close()
                self.stdout.write('backup read-back OK')
        finally:
            storage.delete(name)
            if storage.mirror_enabled and storage.backup.exists(name):
                storage.backup.delete(name)
            StoredFileRecord.objects.filter(path=name).delete()
            self.stdout.write('cleaned up probe file')

        self.stdout.write(self.style.SUCCESS('storage smoketest passed'))
