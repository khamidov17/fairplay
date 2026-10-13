from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from judge.models import ProblemImportDraft


class Command(BaseCommand):
    help = 'Delete expired Excel problem-import drafts and their temporary ZIP attachments.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=7,
            help='Delete drafts not updated within this many days (default: 7).',
        )

    def handle(self, *args, **options):
        days = options['days']
        if days < 1:
            raise CommandError('--days must be at least 1.')
        cutoff = timezone.now() - timedelta(days=days)
        drafts = ProblemImportDraft.objects.filter(updated_at__lt=cutoff)
        count = drafts.count()
        drafts.delete()
        self.stdout.write(self.style.SUCCESS(
            'Deleted {} expired import draft(s).'.format(count),
        ))
