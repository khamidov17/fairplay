from django.contrib import admin
from django.db.models import Count
from django.forms import ModelForm
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _, ngettext

from judge.models import SEBConfigPreset
from judge.widgets import PrivateFileInput


class SEBConfigPresetForm(ModelForm):
    class Meta:
        model = SEBConfigPreset
        fields = '__all__'
        # Private object storage has no linkable URL — see PrivateFileInput.
        widgets = {'config_file': PrivateFileInput}


class SEBConfigPresetAdmin(admin.ModelAdmin):
    """Publish the ready-made Safe Exam Browser configurations instructors pick from.

    Contests link to a preset rather than copying it, so editing a row here
    changes every assignment using it. That is deliberate — one fix propagates —
    but it also means a mid-exam edit re-keys students out, which
    ``SEBConfigPreset.clean()`` refuses. Use **Duplicate** to version instead.
    """

    form = SEBConfigPresetForm
    list_display = ('name', 'is_default', 'is_active', 'contest_count', 'updated_on', 'updated_by')
    list_filter = ('is_default', 'is_active')
    search_fields = ('name', 'description')
    readonly_fields = ('current_config_file', 'updated_on', 'updated_by')
    actions = ('duplicate_preset',)
    fieldsets = (
        (None, {'fields': ('name', 'description', 'is_default', 'is_active')}),
        (_('Configuration'), {
            'fields': ('current_config_file', 'config_file', 'browser_exam_keys', 'config_keys', 'quit_url',
                       'require_browser_exam_key'),
            'description': _('Build the .seb file in the SEB Configuration Tool with the Start URL set to this '
                             "site's home page, a custom Browser Exam Key under Security, and a Quit URL. Then "
                             'upload it here and paste both key hashes.'),
        }),
        (_('Bookkeeping'), {'fields': ('updated_on', 'updated_by')}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(contest_count_=Count('contests'))

    @admin.display(description=_('assignments'), ordering='contest_count_')
    def contest_count(self, obj):
        return obj.contest_count_

    @admin.display(description=_('current file'))
    def current_config_file(self, obj):
        # The upload widget cannot show the stored file (no linkable URL), so
        # offer the permission-checked streaming view instead.
        if obj is None or not obj.pk or not obj.config_file:
            return _('No file uploaded yet.')
        return format_html('<a href="{}">{}</a>', reverse('seb_preset_config', args=(obj.pk,)),
                           _('Download current .seb'))

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.profile
        super().save_model(request, obj, form, change)

    @admin.action(description=_('Duplicate selected configurations'))
    def duplicate_preset(self, request, queryset):
        # The safe way to change a preset that a live exam depends on: copy it,
        # edit the copy, then mark the copy default.
        count = 0
        for preset in queryset:
            preset.pk = None
            preset._state.adding = True
            preset.is_default = False
            preset.name = self._unique_copy_name(preset.name)
            preset.updated_by = request.profile
            preset.save()
            count += 1
        self.message_user(request, ngettext('%d configuration duplicated.',
                                            '%d configurations duplicated.', count) % count)

    @staticmethod
    def _unique_copy_name(name):
        base = gettext('%s (copy)') % name
        candidate, suffix = base, 2
        while SEBConfigPreset.objects.filter(name=candidate).exists():
            candidate = '%s %d' % (base, suffix)
            suffix += 1
        return candidate[:100]
