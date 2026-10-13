from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _, ngettext

from judge.models import StoredFileRecord


class StoredFileRecordAdmin(admin.ModelAdmin):
    list_display = ('path', 'status', 'size', 'attempts', 'created_on', 'mirrored_at')
    list_filter = ('status',)
    search_fields = ('path',)
    readonly_fields = ('path', 'sha256', 'size', 'status', 'created_on', 'mirrored_at',
                       'attempts', 'last_error')
    actions = ('retry_mirror', 'force_remirror')
    date_hierarchy = 'created_on'

    def has_add_permission(self, request):
        return False  # rows are created by the storage layer only

    @admin.action(description=_('Retry mirror to backup now'))
    def retry_mirror(self, request, queryset):
        """Queue the records that still have mirroring to do.

        MIRRORED, DELETED and ABANDONED rows are short-circuited by mirror_record(),
        so queuing them would only report work that never happens — they are counted
        as skipped instead. Use "Force re-mirror" for a deliberate re-upload.
        """
        from judge.tasks import mirror_to_backup
        retryable = queryset.filter(status__in=(StoredFileRecord.STATUS_PENDING,
                                                StoredFileRecord.STATUS_FAILED))
        count = 0
        for record_id in retryable.values_list('id', flat=True):
            mirror_to_backup.delay(record_id)
            count += 1
        self.message_user(request, ngettext('%d mirror task queued.', '%d mirror tasks queued.',
                                            count) % count, messages.SUCCESS)

        skipped = queryset.count() - count
        if skipped:
            self.message_user(request, ngettext(
                '%d record skipped: already mirrored, deleted or abandoned.',
                '%d records skipped: already mirrored, deleted or abandoned.',
                skipped) % skipped, messages.WARNING)

    @admin.action(description=_('Force re-mirror (re-upload to backup)'))
    def force_remirror(self, request, queryset):
        """Reset already-mirrored or abandoned records and upload them again.

        For the cases the plain retry deliberately refuses: a backup copy found
        corrupt by the sweep's verification, or an ABANDONED record whose primary
        object an operator has since restored. DELETED rows are never touched — that
        would resurrect a file that was deliberately removed.
        """
        from judge.tasks import mirror_to_backup
        resettable = queryset.exclude(status=StoredFileRecord.STATUS_DELETED)
        record_ids = list(resettable.values_list('id', flat=True))
        StoredFileRecord.objects.filter(id__in=record_ids).update(
            status=StoredFileRecord.STATUS_PENDING, mirrored_at=None, attempts=0, last_error='')
        for record_id in record_ids:
            mirror_to_backup.delay(record_id)

        count = len(record_ids)
        self.message_user(request, ngettext(
            '%d record reset to pending and queued for re-upload.',
            '%d records reset to pending and queued for re-upload.',
            count) % count, messages.WARNING)
