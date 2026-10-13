import random
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max, Min, Q
from django.utils import timezone

from judge.models import StoredFileRecord
from judge.storage.mirror import MirrorTerminalError, mirror_record, verify_record


class Command(BaseCommand):
    help = ('Reconciliation sweep for the submission-file backup mirror: re-mirrors stale '
            'PENDING records (lost Celery tasks) and FAILED records inline, without needing '
            'a Celery worker. ABANDONED records (terminal: zero-byte / primary gone / too '
            'many attempts) are left alone so they are not re-attempted forever. Run from '
            'cron (recommended: */15). Exits nonzero while any record is FAILED or '
            'ABANDONED, so cron MAILTO doubles as the alert channel.')

    def add_arguments(self, parser):
        parser.add_argument('--stale-minutes', type=int, default=15,
                            help='re-mirror PENDING records older than this (default 15)')
        parser.add_argument('--limit', type=int, default=500, metavar='N',
                            help='mirror at most N records per run (default 500); the '
                                 'remainder is reported and picked up by the next run')
        parser.add_argument('--max-attempts', type=int, default=20, metavar='N',
                            help='abandon a record once it has failed N times (default 20), '
                                 'instead of retrying it every sweep forever')
        parser.add_argument('--verify', type=int, default=0, metavar='N',
                            help='also sha256-verify N random MIRRORED files on both stores')

    def handle(self, *args, **options):
        if not settings.DMOJ_BACKUP_MIRROR_ENABLED:
            self.stdout.write('DMOJ_BACKUP_MIRROR_ENABLED is off; nothing to do.')
            return

        cutoff = timezone.now() - timedelta(minutes=options['stale_minutes'])
        limit = options['limit']
        # One index-served query, capped and streamed: the sweep must not grow a Python
        # list of every stale record on a table that only gets bigger.
        due = (StoredFileRecord.objects
               .filter(Q(status=StoredFileRecord.STATUS_PENDING, created_on__lt=cutoff) |
                       Q(status=StoredFileRecord.STATUS_FAILED))
               .order_by('created_on'))
        due_count = due.count()

        swept = errors = abandoned = 0
        for record in due[:limit].iterator():
            if record.attempts >= options['max_attempts']:
                # Without this a permanently broken record is retried every 15 minutes
                # forever; ABANDONED still fails the run, so it stays visible.
                StoredFileRecord.objects.filter(id=record.id).update(
                    status=StoredFileRecord.STATUS_ABANDONED,
                    last_error=(f'abandoned after {record.attempts} mirror attempts; '
                                f'last error: {record.last_error}')[:5000])
                abandoned += 1
                self.stderr.write(f'{record.path}: abandoned after {record.attempts} attempts')
                continue
            swept += 1
            try:
                mirror_record(record)
            except MirrorTerminalError:
                errors += 1
            except Exception as e:
                errors += 1
                self.stderr.write(f'{record.path}: {type(e).__name__}: {e}')

        if due_count > limit:
            self.stdout.write(f'--limit {limit} reached; {due_count - limit} record(s) still '
                              f'due, the next run continues from there')

        corrupt = healed = 0
        if options['verify']:
            for record in self._sample_mirrored(options['verify']):
                result = verify_record(record)
                if result['primary'] is True and result['backup'] is False:
                    # Primary intact, backup corrupt: queue a re-mirror from the primary
                    # (next sweep re-uploads). None (unreadable) is left alone as transient.
                    StoredFileRecord.objects.filter(id=record.id).update(
                        status=StoredFileRecord.STATUS_PENDING, mirrored_at=None)
                    healed += 1
                    self.stdout.write(f'VERIFY: backup corrupt, queued re-mirror {record.path}')
                elif result['primary'] is False or result['backup'] is False:
                    corrupt += 1
                    self.stderr.write(f'VERIFY FAILED {record.path}: {result}')

        pending = StoredFileRecord.objects.filter(status=StoredFileRecord.STATUS_PENDING)
        failed_count = StoredFileRecord.objects.filter(status=StoredFileRecord.STATUS_FAILED).count()
        abandoned_count = StoredFileRecord.objects.filter(
            status=StoredFileRecord.STATUS_ABANDONED).count()
        oldest = pending.order_by('created_on').first()
        lag = (timezone.now() - oldest.created_on) if oldest else None
        self.stdout.write(f'swept={swept} errors={errors} abandoned={abandoned} healed={healed} '
                          f'pending={pending.count()} failed={failed_count} '
                          f'abandoned_total={abandoned_count} oldest_pending_age={lag or "-"}')

        if failed_count or abandoned_count or corrupt:
            raise CommandError(f'{failed_count} FAILED, {abandoned_count} ABANDONED '
                               f'(manual action needed), {corrupt} corrupt')

    def _sample_mirrored(self, count):
        """Random MIRRORED records, without listing the table.

        Seeks into the (status, created_on) index at random points in time rather than
        materializing every id, so the cost is O(count) index seeks instead of a full
        scan plus an O(rows) Python list. Records that follow a long quiet period are
        slightly over-represented — acceptable for a spot check.
        """
        qs = StoredFileRecord.objects.filter(status=StoredFileRecord.STATUS_MIRRORED)
        bounds = qs.aggregate(first=Min('created_on'), last=Max('created_on'))
        if bounds['first'] is None:
            return []

        span = (bounds['last'] - bounds['first']).total_seconds()
        picked = {}
        stalled = 0
        # Stop once N are drawn, or once repeated draws stop finding anything new
        # (asking for more samples than the table holds must not spin).
        while len(picked) < count and stalled <= count:
            pivot = bounds['first'] + timedelta(seconds=random.uniform(0, span))
            record = (qs.filter(created_on__gte=pivot).order_by('created_on').first() or
                      qs.order_by('-created_on').first())
            if record is None:
                break
            if record.id in picked:
                stalled += 1
            else:
                picked[record.id] = record
                stalled = 0
        return list(picked.values())
