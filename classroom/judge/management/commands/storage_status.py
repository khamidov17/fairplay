from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from judge.models import StoredFileRecord


class Command(BaseCommand):
    help = 'Show submission-file backup mirror status: counts per state, backup lag, recent errors.'

    def add_arguments(self, parser):
        parser.add_argument('--quiet', action='store_true',
                            help='no output; exit nonzero if any record is FAILED or ABANDONED')

    def handle(self, *args, **options):
        alerting = StoredFileRecord.objects.filter(status__in=(
            StoredFileRecord.STATUS_FAILED, StoredFileRecord.STATUS_ABANDONED))
        if options['quiet']:
            if alerting.exists():
                raise CommandError(f'{alerting.count()} records need attention (FAILED/ABANDONED)')
            return

        for status, label in StoredFileRecord.STATUS_CHOICES:
            count = StoredFileRecord.objects.filter(status=status).count()
            self.stdout.write(f'{label}: {count}')

        oldest = StoredFileRecord.objects.filter(status=StoredFileRecord.STATUS_PENDING) \
                                         .order_by('created_on').first()
        if oldest:
            self.stdout.write(f'oldest pending: {oldest.path} '
                              f'(age {timezone.now() - oldest.created_on})')

        recent_errors = StoredFileRecord.objects.exclude(last_error='').order_by('-created_on')[:10]
        if recent_errors:
            self.stdout.write('\nrecent errors:')
            for record in recent_errors:
                self.stdout.write(f'  [{record.get_status_display()}] {record.path}: {record.last_error}')
