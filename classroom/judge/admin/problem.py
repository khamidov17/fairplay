import io
import json
import os
import re
import zipfile
from copy import deepcopy
from operator import attrgetter
from xml.etree import ElementTree as ET

from django import forms
from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.forms import ModelForm
from django.http import HttpResponse, JsonResponse
from django.urls import path, reverse, reverse_lazy
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _, ngettext
from reversion.admin import VersionAdmin

from judge.models import Class, Language, LanguageLimit, Organization, Problem, ProblemClarification, ProblemGroup, \
    ProblemPointsVote, ProblemTemplate, ProblemTranslation, ProblemType, Profile, Solution, \
    SubmissionSourceAccess
from judge.utils import simple_xlsx
from judge.utils.language_matching import matching_languages
from judge.utils.problem_create import generate_problem_code as next_problem_code
from judge.utils.simple_xlsx import build_xlsx_workbook, read_xlsx_rows
from judge.utils.user_search import apply_profile_choice_labels, profile_choice_label
from judge.utils.views import NoBatchDeleteMixin
from judge.widgets import AdminHeavySelect2MultipleWidget, AdminMartorWidget, AdminSelect2MultipleWidget, \
    AdminSelect2Widget, CheckboxSelectMultipleWithSelectAll


MARKDOWN_UPLOAD_EXTENSIONS = ('.md', '.markdown', '.mdown', '.txt')
EXCEL_UPLOAD_EXTENSIONS = ('.xlsx',)
WORKBOOK_EXTENSION_NAMESPACES = {
    'mc': 'http://schemas.openxmlformats.org/markup-compatibility/2006',
    'x14': 'http://schemas.microsoft.com/office/spreadsheetml/2009/9/main',
    'x14ac': 'http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac',
    'xdr': 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing',
    'x15': 'http://schemas.microsoft.com/office/spreadsheetml/2010/11/main',
    'x15ac': 'http://schemas.microsoft.com/office/spreadsheetml/2010/11/ac',
    'xr': 'http://schemas.microsoft.com/office/spreadsheetml/2014/revision',
    'xr2': 'http://schemas.microsoft.com/office/spreadsheetml/2015/revision2',
    'xr3': 'http://schemas.microsoft.com/office/spreadsheetml/2016/revision3',
    'xr6': 'http://schemas.microsoft.com/office/spreadsheetml/2016/revision6',
    'xr10': 'http://schemas.microsoft.com/office/spreadsheetml/2016/revision10',
}
APP_PROPERTIES_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/extended-properties'
DOC_PROPERTIES_VT_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes'


def _excel_template_question_type_range():
    return 'B2:B{}'.format(settings.PROBLEM_EXCEL_TEMPLATE_MAX_ROW)


def _serialize_excel_xml(element):
    """Preserve namespaces named only by Office compatibility attributes."""
    markup_namespace = WORKBOOK_EXTENSION_NAMESPACES['mc']
    prefixes = set(element.attrib.get('{%s}Ignorable' % markup_namespace, '').split())
    prefixes.update(
        prefix
        for choice in element.findall('.//{%s}Choice' % markup_namespace)
        for prefix in choice.attrib.get('Requires', '').split()
    )
    content = ET.tostring(element, encoding='utf-8', xml_declaration=True)
    root_start = content.find(b'<', content.find(b'?>') + 2)
    root_end = content.find(b'>', root_start)
    root_tag = content[root_start:root_end]
    declarations = b''.join(
        ' xmlns:{}="{}"'.format(prefix, WORKBOOK_EXTENSION_NAMESPACES[prefix]).encode('utf-8')
        for prefix in sorted(prefixes)
        if prefix in WORKBOOK_EXTENSION_NAMESPACES and 'xmlns:{}='.format(prefix).encode('ascii') not in root_tag
    )
    return content[:root_end] + declarations + content[root_end:]


def decode_markdown_upload(uploaded_file):
    raw_bytes = uploaded_file.read()
    try:
        for encoding in ('utf-8-sig', 'utf-8', 'utf-16'):
            try:
                return raw_bytes.decode(encoding).replace('\r\n', '\n').replace('\r', '\n')
            except UnicodeDecodeError:
                continue
    finally:
        uploaded_file.seek(0)
    raise UnicodeDecodeError('utf-8', b'', 0, 1, 'Could not decode uploaded Markdown file.')


def validate_markdown_upload_filename(uploaded_file):
    filename = (uploaded_file.name or '').strip()
    if not filename.lower().endswith(MARKDOWN_UPLOAD_EXTENSIONS):
        raise forms.ValidationError(_('Only Markdown or text files are supported.'))
    return filename


def validate_excel_upload_filename(uploaded_file):
    filename = (uploaded_file.name or '').strip()
    if not filename.lower().endswith(EXCEL_UPLOAD_EXTENSIONS):
        raise forms.ValidationError(_('Only .xlsx Excel files are supported.'))
    return filename


class ProblemForm(ModelForm):
    change_message = forms.CharField(max_length=256, label=_('Edit reason'), required=False)

    def __init__(self, *args, **kwargs):
        super(ProblemForm, self).__init__(*args, **kwargs)
        apply_profile_choice_labels(self)
        self.fields['authors'].widget.can_add_related = False
        self.fields['curators'].widget.can_add_related = False
        self.fields['testers'].widget.can_add_related = False
        self.fields['banned_users'].widget.can_add_related = False
        self.fields['change_message'].widget.attrs.update({
            'placeholder': gettext('Describe the changes you made (optional)'),
        })

    def clean(self):
        cleaned_data = super().clean()
        organizations = cleaned_data.get('organizations')
        classes = cleaned_data.get('classes')

        if classes and organizations:
            org_ids = set(organizations.values_list('id', flat=True))
            invalid_classes = classes.exclude(organization_id__in=org_ids)
            if invalid_classes.exists():
                raise forms.ValidationError(
                    _('Selected classes must belong to selected organizations.'),
                )
        elif classes and not organizations:
            raise forms.ValidationError(
                _('You must select organizations before selecting classes.'),
            )

        return cleaned_data

    class Meta:
        widgets = {
            'authors': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'curators': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'testers': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'banned_users': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'organizations': AdminHeavySelect2MultipleWidget(data_view='organization_select2'),
            'classes': AdminHeavySelect2MultipleWidget(data_view='class_select2'),
            'types': AdminSelect2MultipleWidget,
            'group': AdminSelect2Widget,
            'description': AdminMartorWidget(attrs={'data-markdownfy-url': reverse_lazy('problem_preview')}),
        }


class SimpleProblemCreationForm(ProblemForm):
    CREATION_PRESET_DEFAULT = 'default'
    CREATION_PRESET_CUSTOM = 'custom'
    advanced_required_fields = ('code', 'group', 'time_limit', 'memory_limit', 'points')
    advanced_required_m2m_fields = ('types', 'allowed_languages')
    markdown_upload = forms.FileField(
        label=_('Upload Markdown file (optional)'),
        required=False,
        help_text=_(
            'Use the first level-1 heading as the problem name and the remaining content as the problem body.',
        ),
        widget=forms.ClearableFileInput(attrs={'accept': '.md,.markdown,text/markdown,text/plain'}),
    )

    creation_preset = forms.ChoiceField(
        label=_('Creation preset'),
        required=False,
        initial=CREATION_PRESET_DEFAULT,
        choices=(
            (CREATION_PRESET_DEFAULT, _('Default')),
            (CREATION_PRESET_CUSTOM, _('Custom')),
        ),
        help_text=_('Presets can automatically fill advanced settings.'),
    )
    embedded_advanced_mode = forms.BooleanField(required=False, widget=forms.HiddenInput())

    class Meta(ProblemForm.Meta):
        model = Problem
        fields = (
            'code', 'name', 'is_public', 'is_manually_managed', 'date', 'authors', 'curators', 'testers',
            'organizations', 'classes', 'submission_source_visibility_mode', 'is_full_markup',
            'view_test_cases', 'view_tester', 'ai_hints_enabled', 'description', 'license', 'og_image', 'summary',
            'types', 'group', 'points', 'partial', 'short_circuit', 'time_limit', 'memory_limit',
            'allowed_languages', 'banned_users',
        )

    def __init__(self, *args, **kwargs):
        super(SimpleProblemCreationForm, self).__init__(*args, **kwargs)
        for field_name in (
            'code', 'is_public', 'is_manually_managed', 'date', 'authors', 'curators', 'testers', 'organizations',
            'classes', 'submission_source_visibility_mode', 'is_full_markup', 'view_test_cases', 'view_tester',
            'ai_hints_enabled', 'license', 'og_image', 'summary', 'types', 'group', 'points', 'partial',
            'short_circuit', 'time_limit', 'memory_limit', 'allowed_languages', 'banned_users',
        ):
            self.fields[field_name].required = False
        self.fields['name'].required = False
        self.fields['description'].required = False
        self.fields['creation_preset'].choices = (
            (self.CREATION_PRESET_DEFAULT, _('Default')),
            (self.CREATION_PRESET_CUSTOM, _('Custom')),
        )

    def parse_markdown_upload(self, markdown_upload):
        content = decode_markdown_upload(markdown_upload)
        lines = content.splitlines()
        title = ''
        body_lines = []
        title_index = None

        for index, line in enumerate(lines):
            match = re.match(r'^\s*#\s+(.+?)\s*$', line)
            if match:
                title = match.group(1).strip()
                title_index = index
                break

        if not title:
            raise forms.ValidationError(
                _('The uploaded Markdown file must contain a level-1 heading like "# Problem Name".'),
            )

        for index, line in enumerate(lines):
            if index != title_index:
                body_lines.append(line)

        body = '\n'.join(body_lines).strip()
        return title, body

    def clean(self):
        cleaned_data = super().clean()
        advanced_mode = cleaned_data.get('embedded_advanced_mode')
        markdown_upload = cleaned_data.get('markdown_upload')

        if markdown_upload:
            try:
                uploaded_name, uploaded_description = self.parse_markdown_upload(markdown_upload)
            except UnicodeDecodeError:
                self.add_error('markdown_upload', _('The uploaded Markdown file must be UTF-8 encoded.'))
            except forms.ValidationError as error:
                self.add_error('markdown_upload', error)
            else:
                if not cleaned_data.get('name'):
                    cleaned_data['name'] = uploaded_name
                if not cleaned_data.get('description'):
                    cleaned_data['description'] = uploaded_description

        if not cleaned_data.get('name'):
            self.add_error('name', _('This field is required.'))
        if not cleaned_data.get('description'):
            self.add_error('description', _('This field is required.'))

        if getattr(self, 'default_group', None) is None:
            raise forms.ValidationError(_('You must create at least one problem group before creating a problem.'))
        if getattr(self, 'default_type', None) is None:
            raise forms.ValidationError(_('You must create at least one problem type before creating a problem.'))
        if not getattr(self, 'default_languages', None):
            raise forms.ValidationError(_('You must configure at least one language before creating a problem.'))

        if advanced_mode:
            for field_name in self.advanced_required_fields:
                if not cleaned_data.get(field_name):
                    self.add_error(field_name, _('This field is required in advanced mode.'))
            for field_name in self.advanced_required_m2m_fields:
                if not cleaned_data.get(field_name):
                    self.add_error(field_name, _('This field is required in advanced mode.'))
        else:
            generated_code = cleaned_data.get('code') or ''
            if not generated_code:
                generate_simple_code = getattr(self, 'generate_simple_code', None)
                if callable(generate_simple_code):
                    generated_code = generate_simple_code(cleaned_data.get('name') or '')
                else:
                    generated_code = getattr(self, 'generated_simple_code', '')
            cleaned_data['code'] = generated_code
            cleaned_data['is_public'] = False
            cleaned_data['is_manually_managed'] = False
            cleaned_data['date'] = None
            cleaned_data['submission_source_visibility_mode'] = SubmissionSourceAccess.FOLLOW
            cleaned_data['is_full_markup'] = False
            cleaned_data['view_test_cases'] = False
            cleaned_data['view_tester'] = False
            cleaned_data['ai_hints_enabled'] = False
            cleaned_data['license'] = None
            cleaned_data['og_image'] = ''
            cleaned_data['summary'] = ''
            cleaned_data['group'] = self.default_group
            cleaned_data['points'] = self.default_points
            cleaned_data['partial'] = False
            cleaned_data['short_circuit'] = False
            cleaned_data['time_limit'] = self.default_time_limit
            cleaned_data['memory_limit'] = self.default_memory_limit

        return cleaned_data


class ProblemCreatorListFilter(admin.SimpleListFilter):
    title = parameter_name = 'creator'

    def lookups(self, request, model_admin):
        queryset = Profile.objects.exclude(authored_problems=None).values_list('user__username', flat=True)
        return [(name, name) for name in queryset]

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset
        return queryset.filter(authors__user__username=self.value())


class LanguageLimitInlineForm(ModelForm):
    class Meta:
        widgets = {'language': AdminSelect2Widget}


class LanguageLimitInline(admin.TabularInline):
    model = LanguageLimit
    fields = ('language', 'time_limit', 'memory_limit')
    form = LanguageLimitInlineForm
    classes = ('embedded-advanced-fields',)


class ProblemClarificationForm(ModelForm):
    class Meta:
        widgets = {'description': AdminMartorWidget(attrs={'data-markdownfy-url': reverse_lazy('comment_preview')})}


class ProblemClarificationInline(admin.StackedInline):
    model = ProblemClarification
    fields = ('description',)
    form = ProblemClarificationForm
    extra = 0
    classes = ('embedded-advanced-fields',)


class ProblemSolutionForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(ProblemSolutionForm, self).__init__(*args, **kwargs)
        apply_profile_choice_labels(self)
        self.fields['authors'].widget.can_add_related = False

    class Meta:
        widgets = {
            'authors': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'content': AdminMartorWidget(attrs={'data-markdownfy-url': reverse_lazy('solution_preview')}),
        }


class ProblemSolutionInline(admin.StackedInline):
    model = Solution
    fields = ('is_public', 'publish_on', 'authors', 'content')
    form = ProblemSolutionForm
    extra = 0
    classes = ('embedded-advanced-fields',)


class ProblemTranslationForm(ModelForm):
    class Meta:
        widgets = {'description': AdminMartorWidget(attrs={'data-markdownfy-url': reverse_lazy('problem_preview')})}


class ProblemTranslationInline(admin.StackedInline):
    model = ProblemTranslation
    fields = ('language', 'name', 'description')
    form = ProblemTranslationForm
    extra = 0
    classes = ('embedded-advanced-fields',)

    def has_permission_full_markup(self, request, obj=None):
        if not obj:
            return True
        return request.user.has_perm('judge.problem_full_markup') or not obj.is_full_markup

    has_add_permission = has_change_permission = has_delete_permission = has_permission_full_markup


class ProblemTemplateInline(admin.StackedInline):
    model = ProblemTemplate
    fields = ('language', 'code')
    extra = 1
    classes = ('embedded-extended-fields',)


class ProblemAdmin(NoBatchDeleteMixin, VersionAdmin):
    add_fieldsets = (
        (None, {
            'fields': ('name', 'description', 'markdown_upload', 'creation_preset'),
        }),
        (_('Optional Basic'), {
            'classes': ('embedded-extended-fields',),
            'fields': (
                'organizations', 'classes', 'allowed_languages',
            ),
            'description': _('Organizations (Courses), allowed languages, and starter templates.'),
        }),
        (_('Advanced'), {
            'classes': ('embedded-advanced-fields',),
            'fields': (
                'code', 'is_public', 'is_manually_managed', 'date', 'authors', 'curators', 'testers',
                'submission_source_visibility_mode', 'is_full_markup',
                'view_test_cases', 'view_tester', 'ai_hints_enabled', 'license',
            ),
        }),
        (_('Advanced Social Media'), {
            'classes': ('embedded-advanced-fields',),
            'fields': ('og_image', 'summary'),
        }),
        (_('Advanced Taxonomy'), {
            'classes': ('embedded-advanced-fields',),
            'fields': ('types', 'group'),
        }),
        (_('Advanced Points'), {
            'classes': ('embedded-advanced-fields',),
            'fields': (('points', 'partial'), 'short_circuit'),
        }),
        (_('Advanced Limits'), {
            'classes': ('embedded-advanced-fields',),
            'fields': ('time_limit', 'memory_limit'),
        }),
        (_('Advanced Justice'), {
            'classes': ('embedded-advanced-fields',),
            'fields': ('banned_users',),
        }),
        (_('Advanced History'), {
            'classes': ('embedded-advanced-fields',),
            'fields': ('change_message',),
        }),
    )
    change_embedded_fieldsets = (
        (None, {
            'fields': ('name', 'description'),
        }),
        (_('Optional Basic'), {
            'classes': ('embedded-extended-fields',),
            'fields': (
                'organizations', 'classes', 'allowed_languages',
            ),
            'description': _('Organizations (Courses), allowed languages, and starter templates.'),
        }),
        (_('Advanced'), {
            'classes': ('embedded-advanced-fields',),
            'fields': (
                'code', 'is_public', 'is_manually_managed', 'date', 'authors', 'curators', 'testers',
                'submission_source_visibility_mode', 'is_full_markup',
                'view_test_cases', 'view_tester', 'ai_hints_enabled', 'license',
            ),
        }),
        (_('Advanced Social Media'), {
            'classes': ('embedded-advanced-fields',),
            'fields': ('og_image', 'summary'),
        }),
        (_('Advanced Taxonomy'), {
            'classes': ('embedded-advanced-fields',),
            'fields': ('types', 'group'),
        }),
        (_('Advanced Points'), {
            'classes': ('embedded-advanced-fields',),
            'fields': (('points', 'partial'), 'short_circuit'),
        }),
        (_('Advanced Limits'), {
            'classes': ('embedded-advanced-fields',),
            'fields': ('time_limit', 'memory_limit'),
        }),
        (_('Advanced Justice'), {
            'classes': ('embedded-advanced-fields',),
            'fields': ('banned_users',),
        }),
        (_('Advanced History'), {
            'classes': ('embedded-advanced-fields',),
            'fields': ('change_message',),
        }),
    )
    fieldsets = (
        (None, {
            'fields': (
                'code', 'name', 'is_public', 'is_manually_managed', 'submission_type', 'date',
                'authors', 'curators', 'testers',
                'organizations', 'classes', 'submission_source_visibility_mode', 'is_full_markup',
                'view_test_cases', 'view_tester', 'ai_hints_enabled',
                'description', 'license',
            ),
        }),
        (_('Social Media'), {'classes': ('collapse',), 'fields': ('og_image', 'summary')}),
        (_('Taxonomy'), {'fields': ('types', 'group')}),
        (_('Points'), {'fields': (('points', 'partial'), 'short_circuit')}),
        (_('Limits'), {'fields': ('time_limit', 'memory_limit')}),
        (_('Language'), {'fields': ('allowed_languages',)}),
        (_('Justice'), {'fields': ('banned_users',)}),
        (_('History'), {'fields': ('change_message',)}),
    )
    list_display = ['code', 'name', 'show_authors', 'points', 'is_public', 'show_public']
    ordering = ['code']
    search_fields = ('code', 'name', 'authors__user__username', 'curators__user__username')
    inlines = [ProblemTemplateInline, LanguageLimitInline, ProblemClarificationInline,
               ProblemSolutionInline, ProblemTranslationInline]
    list_max_show_all = 1000
    actions_on_top = True
    actions_on_bottom = True
    list_filter = ('is_public', 'group', 'types', ProblemCreatorListFilter)
    form = ProblemForm
    date_hierarchy = 'date'

    def is_advanced_add_mode(self, request, obj=None):
        if request.POST.get('_advanced_mode') == '1':
            return True
        return obj is None and request.GET.get('advanced') == '1'

    def is_embedded_advanced_mode(self, request, obj=None):
        return request.POST.get('embedded_advanced_mode') in ('1', 'on', 'true', 'True')

    def get_actions(self, request):
        actions = super(ProblemAdmin, self).get_actions(request)

        if request.user.has_perm('judge.change_public_visibility') or \
                request.user.has_perm('judge.create_private_problem'):
            func, name, desc = self.get_action('make_public')
            actions[name] = (func, name, desc)

            func, name, desc = self.get_action('make_private')
            actions[name] = (func, name, desc)

        func, name, desc = self.get_action('update_publish_date')
        actions[name] = (func, name, desc)

        return actions

    def get_default_group(self):
        return ProblemGroup.objects.order_by('full_name', 'name').first()

    def get_default_type(self):
        return ProblemType.objects.order_by('full_name', 'name').first()

    def get_default_languages(self):
        return list(Language.objects.order_by('id'))

    def get_default_time_limit(self):
        return min(max(1, settings.DMOJ_PROBLEM_MIN_TIME_LIMIT), settings.DMOJ_PROBLEM_MAX_TIME_LIMIT)

    def get_default_memory_limit(self):
        return min(max(262144, settings.DMOJ_PROBLEM_MIN_MEMORY_LIMIT), settings.DMOJ_PROBLEM_MAX_MEMORY_LIMIT)

    def get_default_points(self):
        return max(100, settings.DMOJ_PROBLEM_MIN_PROBLEM_POINTS)

    def generate_problem_code(self, name=''):
        return next_problem_code()

    def get_simple_preset_defaults(self, request):
        default_group = self.get_default_group()
        default_type = self.get_default_type()
        default_languages = self.get_default_languages()
        default_code = self.generate_problem_code(request.POST.get('name') or '')
        profile = getattr(request.user, 'profile', None)
        return {
            'values': {
                'code': default_code,
                'is_public': False,
                'is_manually_managed': False,
                'date': '',
                'authors': [profile.id] if profile else [],
                'curators': [],
                'testers': [],
                'organizations': [],
                'classes': [],
                'submission_source_visibility_mode': SubmissionSourceAccess.FOLLOW,
                'is_full_markup': False,
                'view_test_cases': False,
                'view_tester': False,
                'ai_hints_enabled': False,
                'license': '',
                'og_image': '',
                'summary': '',
                'types': [default_type.id] if default_type else [],
                'group': str(default_group.id) if default_group else '',
                'points': str(self.get_default_points()),
                'partial': False,
                'short_circuit': False,
                'time_limit': str(self.get_default_time_limit()),
                'memory_limit': str(self.get_default_memory_limit()),
                'allowed_languages': [lang.id for lang in default_languages],
                'banned_users': [],
            },
            'display': {
                'authors': [{'id': str(profile.id), 'text': profile_choice_label(profile)}] if profile else [],
                'types': [{'id': str(default_type.id), 'text': str(default_type)}] if default_type else [],
                'group': {'id': str(default_group.id), 'text': str(default_group)} if default_group else None,
            },
        }

    def apply_creation_defaults(self, request, obj):
        obj.code = obj.code or self.generate_problem_code(obj.name)
        obj.group = self.get_default_group()
        obj.time_limit = self.get_default_time_limit()
        obj.memory_limit = self.get_default_memory_limit()
        obj.points = self.get_default_points()
        obj.short_circuit = False
        obj.partial = False
        obj.is_public = False
        obj.is_manually_managed = False
        obj.date = None
        obj.license = None
        obj.og_image = ''
        obj.summary = ''
        obj.is_full_markup = False
        obj.submission_source_visibility_mode = SubmissionSourceAccess.FOLLOW
        obj.view_test_cases = False
        obj.view_tester = False
        obj.ai_hints_enabled = False
        obj.is_organization_private = False

    def get_fieldsets(self, request, obj=None):
        if obj is None and not self.is_advanced_add_mode(request, obj):
            return self.add_fieldsets
        if obj is not None:
            return self.change_embedded_fieldsets
        return super().get_fieldsets(request, obj)

    def get_readonly_fields(self, request, obj=None):
        fields = self.readonly_fields
        if not request.user.has_perm('judge.create_private_problem'):
            fields += ('organizations', 'classes')
            if not request.user.has_perm('judge.change_public_visibility'):
                fields += ('is_public',)
        if not request.user.has_perm('judge.change_manually_managed'):
            fields += ('is_manually_managed',)
        if not request.user.has_perm('judge.problem_full_markup'):
            fields += ('is_full_markup',)
            if obj and obj.is_full_markup:
                fields += ('description',)
        return fields

    @admin.display(description=_('authors'))
    def show_authors(self, obj):
        return ', '.join(map(attrgetter('user.username'), obj.authors.all()))

    @admin.display(description='')
    def show_public(self, obj):
        return format_html('<a href="{1}">{0}</a>', gettext('View on site'), obj.get_absolute_url())

    def _rescore(self, request, problem_id):
        from judge.tasks import rescore_problem
        transaction.on_commit(rescore_problem.s(problem_id).delay)

    @admin.display(description=_('Set publish date to now'))
    def update_publish_date(self, request, queryset):
        count = queryset.update(date=timezone.now())
        self.message_user(request, ngettext("%d problem's publish date successfully updated.",
                                            "%d problems' publish date successfully updated.",
                                            count) % count)

    @admin.display(description=_('Mark problems as public'))
    def make_public(self, request, queryset):
        if not request.user.has_perm('judge.change_public_visibility'):
            queryset = queryset.filter(is_organization_private=True)
        count = queryset.update(is_public=True)
        for problem_id in queryset.values_list('id', flat=True):
            self._rescore(request, problem_id)
        self.message_user(request, ngettext('%d problem successfully marked as public.',
                                            '%d problems successfully marked as public.',
                                            count) % count)

    @admin.display(description=_('Mark problems as private'))
    def make_private(self, request, queryset):
        if not request.user.has_perm('judge.change_public_visibility'):
            queryset = queryset.filter(is_organization_private=True)
        count = queryset.update(is_public=False)
        for problem_id in queryset.values_list('id', flat=True):
            self._rescore(request, problem_id)
        self.message_user(request, ngettext('%d problem successfully marked as private.',
                                            '%d problems successfully marked as private.',
                                            count) % count)

    def get_queryset(self, request):
        return Problem.get_editable_problems(request.user).prefetch_related('authors__user').distinct()

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return request.user.has_perm('judge.edit_own_problem')
        return obj.is_editable_by(request.user)

    def formfield_for_manytomany(self, db_field, request=None, **kwargs):
        if db_field.name == 'allowed_languages':
            kwargs['widget'] = CheckboxSelectMultipleWithSelectAll()
        return super(ProblemAdmin, self).formfield_for_manytomany(db_field, request, **kwargs)

    def get_form(self, request, obj=None, **kwargs):
        if obj is None and not self.is_advanced_add_mode(request, obj):
            kwargs['form'] = SimpleProblemCreationForm
        form = super(ProblemAdmin, self).get_form(request, obj, **kwargs)
        if obj is None and not self.is_advanced_add_mode(request, obj):
            form.default_group = self.get_default_group()
            form.default_type = self.get_default_type()
            form.default_languages = self.get_default_languages()
            form.default_points = self.get_default_points()
            form.default_time_limit = self.get_default_time_limit()
            form.default_memory_limit = self.get_default_memory_limit()
            form.generate_simple_code = self.generate_problem_code
            form.generated_simple_code = self.generate_problem_code(request.POST.get('name') or '')
            form.simple_preset_defaults = self.get_simple_preset_defaults(request)
        if 'authors' in form.base_fields:
            form.base_fields['authors'].queryset = Profile.objects.all()
        if 'classes' in form.base_fields:
            form.base_fields['classes'].queryset = Class.get_visible_classes(request.user)
        return form

    class Media:
        js = ('admin_class_filter.js', 'admin_problem_code_autofill.js')

    def get_urls(self):
        return [
            path(
                'import-markdown/',
                self.admin_site.admin_view(self.import_markdown_view),
                name='judge_problem_import_markdown',
            ),
            path(
                'batch-create-markdown/',
                self.admin_site.admin_view(self.batch_create_markdown_view),
                name='judge_problem_batch_create_markdown',
            ),
            path(
                'batch-create-excel/',
                self.admin_site.admin_view(self.batch_create_excel_view),
                name='judge_problem_batch_create_excel',
            ),
            path(
                'download-excel-template/',
                self.admin_site.admin_view(self.download_excel_template_view),
                name='judge_problem_download_excel_template',
            ),
        ] + super().get_urls()

    def _serialize_created_problem(self, problem):
        return {
            'id': problem.pk,
            'code': problem.code,
            'name': problem.name,
            'edit_url': reverse('admin:judge_problem_change', args=[problem.pk]),
        }

    def _split_excel_multi_value(self, value):
        if value is None:
            return []
        parts = [item.strip() for item in re.split(r'[\n,;|]+', str(value)) if item.strip()]
        return parts

    def _resolve_excel_tokens(self, queryset, tokens, label, resolvers):
        resolved = []
        seen = set()
        for token in tokens:
            matches = []
            for resolver in resolvers:
                matches.extend(resolver(queryset, token))
            unique_matches = []
            for match in matches:
                if match.pk not in {item.pk for item in unique_matches}:
                    unique_matches.append(match)
            if not unique_matches:
                raise forms.ValidationError(_('%(label)s "%(value)s" was not found.') % {
                    'label': label,
                    'value': token,
                })
            if len(unique_matches) > 1:
                raise forms.ValidationError(_('%(label)s "%(value)s" matched multiple records.') % {
                    'label': label,
                    'value': token,
                })
            match = unique_matches[0]
            if match.pk not in seen:
                resolved.append(match)
                seen.add(match.pk)
        return resolved

    def _resolve_excel_organizations(self, request, raw_value):
        tokens = self._split_excel_multi_value(raw_value)
        if not tokens:
            return []
        if not request.user.has_perm('judge.create_private_problem'):
            raise forms.ValidationError(_('You do not have permission to set organizations in Excel import.'))
        queryset = self._visible_excel_organizations(request.user)
        return self._resolve_excel_tokens(
            queryset,
            tokens,
            _('Organization'),
            (
                lambda qs, token: list(qs.filter(slug=token)),
                lambda qs, token: list(qs.filter(short_name=token)),
                lambda qs, token: list(qs.filter(name=token)),
                lambda qs, token: list(qs.filter(pk=int(token))) if token.isdigit() else [],
            ),
        )

    def _resolve_excel_classes(self, request, raw_value):
        tokens = self._split_excel_multi_value(raw_value)
        if not tokens:
            return []
        if not request.user.has_perm('judge.create_private_problem'):
            raise forms.ValidationError(_('You do not have permission to set classes in Excel import.'))
        queryset = Class.get_visible_classes(request.user)
        return self._resolve_excel_tokens(
            queryset,
            tokens,
            _('Class'),
            (
                lambda qs, token: list(qs.filter(slug=token)),
                lambda qs, token: list(qs.filter(name=token)),
                lambda qs, token: list(qs.filter(pk=int(token))) if token.isdigit() else [],
            ),
        )

    def _resolve_excel_languages(self, raw_value):
        tokens = self._split_excel_multi_value(raw_value)
        if not tokens:
            return self.get_default_languages()
        queryset = Language.objects.all()
        return self._resolve_excel_tokens(
            queryset,
            tokens,
            _('Language'),
            (
                lambda qs, token: list(qs.filter(key=token)),
                lambda qs, token: list(qs.filter(short_name=token)),
                lambda qs, token: list(qs.filter(common_name=token)),
                lambda qs, token: list(qs.filter(name=token)),
                lambda qs, token: list(qs.filter(pk=int(token))) if token.isdigit() else [],
                matching_languages,
            ),
        )

    def create_problem_with_basic_configuration(
        self,
        request,
        name,
        description,
        organizations=None,
        classes=None,
        allowed_languages=None,
    ):
        default_group = self.get_default_group()
        default_type = self.get_default_type()
        default_languages = allowed_languages if allowed_languages is not None else self.get_default_languages()

        if default_group is None:
            raise forms.ValidationError(_('You must create at least one problem group before creating a problem.'))
        if default_type is None:
            raise forms.ValidationError(_('You must create at least one problem type before creating a problem.'))
        if not default_languages:
            raise forms.ValidationError(_('You must configure at least one language before creating a problem.'))
        if classes and not organizations:
            raise forms.ValidationError(_('You must select organizations before selecting classes.'))
        if classes and any(item.organization_id not in {org.pk for org in organizations} for item in classes):
            raise forms.ValidationError(_('Selected classes must belong to selected organizations.'))

        problem = Problem(
            code=self.generate_problem_code(name),
            name=name,
            description=description,
            group=default_group,
            time_limit=self.get_default_time_limit(),
            memory_limit=self.get_default_memory_limit(),
            points=self.get_default_points(),
            short_circuit=False,
            partial=False,
            is_public=False,
            is_manually_managed=False,
            date=None,
            license=None,
            og_image='',
            summary='',
            is_full_markup=False,
            submission_source_visibility_mode=SubmissionSourceAccess.FOLLOW,
            view_test_cases=False,
            view_tester=False,
            ai_hints_enabled=False,
            is_organization_private=bool(organizations or classes),
        )
        problem.save()
        problem.types.set([default_type])
        problem.allowed_languages.set(default_languages)
        if organizations:
            problem.organizations.set(organizations)
        if classes:
            problem.classes.set(classes)
        if hasattr(request.user, 'profile'):
            problem.authors.set([request.user.profile])
        return problem

    def _excel_headers(self):
        return [
            'name', 'problem_type', 'description', 'answer', 'organizations', 'tag', 'difficulty', 'explanation',
            'allowed_languages', 'test_data',
        ]

    def _legacy_excel_headers(self):
        """Keep the retired direct-create endpoint compatible with existing API clients."""
        return ['name', 'description', 'organizations', 'classes', 'allowed_languages']

    def _visible_excel_organizations(self, user):
        if not user.is_authenticated or not user.has_perm('judge.create_private_problem'):
            return Organization.objects.none()
        if user.has_perm('judge.edit_all_organization'):
            return Organization.objects.order_by('name')
        return Organization.objects.filter(admins=user.profile).order_by('name')

    def _visible_excel_classes(self, user):
        if not user.is_authenticated or not user.has_perm('judge.create_private_problem'):
            return Class.objects.none()
        return Class.get_visible_classes(user).order_by('name')

    def _excel_template_rows(self, request):
        languages = self.get_default_languages()
        language_sample = ' | '.join(lang.key for lang in languages[:2]) if languages else 'PY3'
        org = self._visible_excel_organizations(request.user).first()
        return [
            self._excel_headers(),
            [
                'Sample Problem',
                'code',
                'Write the full problem statement here. You can use multiple lines.',
                '',
                org.slug if org else '',
                'implementation',
                '1',
                'Explain the intended solution here.',
                language_sample,
                'input data 1: 2 3\noutput data 1: 5',
            ],
        ]

    def _excel_reference_rows(self, request):
        org_names = ' | '.join(
            self._visible_excel_organizations(request.user).values_list('slug', flat=True)[:5],
        ) or 'organization-slug'
        class_names = ' | '.join(
            self._visible_excel_classes(request.user).values_list('slug', flat=True)[:5],
        ) or 'class-slug'
        language_keys = ' | '.join(
            Language.objects.order_by('key').values_list('key', flat=True)[:8],
        ) or 'PY3'
        return [
            ['Import Instructions', '', '', ''],
            ['Fill the editable Problems sheet; Example Problems is protected and ignored during import.', '', '', ''],
            ['field', 'required', 'how_to_fill', 'example'],
            ['name', 'yes', 'Problem title.', 'Two Sum'],
            [
                'problem_type', 'yes',
                'Choose code, multiple_choice, fill_blank, true_false, or short_answer from the Excel dropdown.',
                'multiple_choice',
            ],
            [
                'description', 'no',
                'Plain-text description. It may be supplemented by an embedded DOCX or Markdown file. PNG, JPG, '
                'and GIF figures inside DOCX are uploaded and inserted in place. For fill_blank, use [[blank]] for '
                'each input.',
                'The capital of France is [[blank]] and 2 + 2 = [[blank]].',
            ],
            [
                'answer', 'MCQ / fill_blank',
                'For MCQ use an option letter, number, or exact option text; separate multiple correct answers '
                'with |. For fill_blank use one answer per [[blank]] in order, separated with |. LLM never '
                'changes this cell.',
                'B  or  Paris|4',
            ],
            [
                'organizations', 'no',
                'Separate multiple values with "|" or ",". Use organization slug/name/id.',
                org_names,
            ],
            [
                'classes', 'no',
                'Separate multiple values with "|" or ",". Use visible class slug/name/id.',
                class_names,
            ],
            [
                'tag', 'no',
                'Separate up to five knowledge tags with commas. Each tag must be at most 20 characters.',
                'calculation, physics',
            ],
            [
                'difficulty', 'no',
                'Use 0 for easy, 1 for medium, or 2 for hard.',
                '1',
            ],
            ['explanation', 'no', 'Editorial content. It is added to the problem solution section.', 'Use addition.'],
            [
                'allowed_languages', 'no',
                'Separate full language names with "|" or ",". Names must match exactly.',
                language_keys,
            ],
            [
                'test_data', 'code only',
                'Use input data N: followed by output data N:. Repeat N = 1, 2, ... for multiple cases. For '
                '.in/.out files, embed one ZIP object in the same Excel row; preview uploads are not accepted.',
                'input data 1: 2 3\noutput data 1: 5\n\ninput data 2: 4 6\noutput data 2: 10',
            ],
            [
                'embedded DOCX / Markdown', 'optional',
                'Use Excel Insert -> Object -> Create from File, without Link. Leave Display as icon unchecked '
                'to preview the Word document in Excel and double-click it to revise. Keep the object on the '
                'same row as the question.',
                'The Word object on the sample MCQ row.',
            ],
        ]

    def _template_with_current_references(self, request, sample_path):
        """Keep the protected sample worksheet while creating a clean Problems worksheet."""
        reference_rows = self._excel_reference_rows(request)
        strings_path = 'xl/sharedStrings.xml'
        content_types_path = '[Content_Types].xml'
        app_properties_path = 'docProps/app.xml'
        namespace = {'x': simple_xlsx.MAIN_NS}
        question_type_range = _excel_template_question_type_range()

        with zipfile.ZipFile(sample_path) as source:
            workbook = ET.fromstring(source.read('xl/workbook.xml'))
            relationships = ET.fromstring(source.read('xl/_rels/workbook.xml.rels'))
            content_types = ET.fromstring(source.read(content_types_path))
            app_properties = ET.fromstring(source.read(app_properties_path))
            relation_targets = {
                relation.attrib.get('Id'): relation.attrib.get('Target', '')
                for relation in relationships
            }
            reference_sheet_path = ''
            example_sheet_path = ''
            example_sheet_entry = None
            for sheet in workbook.iter():
                if not sheet.tag.endswith('}sheet'):
                    continue
                target = relation_targets.get(sheet.attrib.get('{%s}id' % simple_xlsx.REL_NS), '')
                path = target.lstrip('/') if target.startswith('/') else 'xl/{}'.format(target)
                if sheet.attrib.get('name') == 'Problems':
                    example_sheet_path = path
                    example_sheet_entry = sheet
            for sheet_name in ('Instructions', 'Reference'):
                sheet = next(
                    (item for item in workbook.iter() if item.tag.endswith('}sheet')
                     and item.attrib.get('name') == sheet_name),
                    None,
                )
                if sheet is None:
                    continue
                target = relation_targets.get(sheet.attrib.get('{%s}id' % simple_xlsx.REL_NS), '')
                reference_sheet_path = target.lstrip('/') if target.startswith('/') else 'xl/{}'.format(target)
                break
            if not reference_sheet_path:
                raise ValueError('The Excel template must contain an Instructions or Reference worksheet.')
            if not example_sheet_path or example_sheet_entry is None:
                raise ValueError('The Excel template must contain a Problems worksheet.')

            sheets = workbook.find('x:sheets', namespace)
            if sheets is None:
                raise ValueError('The Excel template must contain a sheet list.')
            example_sheet_entry.set('name', 'Example Problems')
            example_sheet = ET.fromstring(source.read(example_sheet_path))
            protection = example_sheet.find('x:sheetProtection', namespace)
            if protection is None:
                protection = ET.Element(
                    '{%s}sheetProtection' % simple_xlsx.MAIN_NS,
                    {'sheet': '1', 'objects': '1', 'scenarios': '1'},
                )
                sheet_data_index = list(example_sheet).index(example_sheet.find('x:sheetData', namespace))
                example_sheet.insert(sheet_data_index + 1, protection)
            relationship_numbers = [
                int(match.group(1))
                for relationship in relationships
                for match in [re.match(r'rId(\d+)$', relationship.attrib.get('Id', ''))]
                if match
            ]
            relationship_id = 'rId{}'.format(max(relationship_numbers, default=0) + 1)
            sheet_ids = [int(sheet.attrib.get('sheetId', 0)) for sheet in sheets]
            sheet_id = max(sheet_ids, default=0) + 1
            problems_sheet_path = 'xl/worksheets/import_problems.xml'
            ET.SubElement(
                relationships,
                '{%s}Relationship' % simple_xlsx.PKG_REL_NS,
                {
                    'Id': relationship_id,
                    'Type': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet',
                    'Target': 'worksheets/import_problems.xml',
                },
            )
            problems_sheet_entry = ET.Element(
                '{%s}sheet' % simple_xlsx.MAIN_NS,
                {
                    'name': 'Problems',
                    'sheetId': str(sheet_id),
                    '{%s}id' % simple_xlsx.REL_NS: relationship_id,
                },
            )
            sheets.insert(list(sheets).index(example_sheet_entry), problems_sheet_entry)
            ET.SubElement(
                content_types,
                '{http://schemas.openxmlformats.org/package/2006/content-types}Override',
                {
                    'PartName': '/{}'.format(problems_sheet_path),
                    'ContentType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml',
                },
            )
            app_namespace = {'a': APP_PROPERTIES_NS, 'vt': DOC_PROPERTIES_VT_NS}
            worksheet_count = app_properties.find('a:HeadingPairs/vt:vector/vt:variant/vt:i4', app_namespace)
            titles = app_properties.find('a:TitlesOfParts/vt:vector', app_namespace)
            if worksheet_count is not None:
                worksheet_count.text = '3'
            if titles is not None:
                titles.clear()
                titles.set('size', '3')
                titles.set('baseType', 'lpstr')
                for name in ('Instructions', 'Problems', 'Example Problems'):
                    ET.SubElement(titles, '{%s}lpstr' % DOC_PROPERTIES_VT_NS).text = name

            shared_strings = ET.fromstring(source.read(strings_path))
            reference_sheet = ET.fromstring(source.read(reference_sheet_path))
            old_string_cells = len(reference_sheet.findall('.//x:c[@t="s"]', namespace))
            string_lookup = {}
            shared_string_values = []
            string_items = shared_strings.findall('x:si', namespace)
            for index, item in enumerate(string_items):
                value = ''.join(node.text or '' for node in item.iterfind('.//x:t', namespace))
                string_lookup.setdefault(value, index)
                shared_string_values.append(value)

            example_header = example_sheet.find('x:sheetData/x:row', namespace)
            problem_rows = [[
                shared_string_values[int(cell.find('x:v', namespace).text)]
                for cell in example_header.findall('x:c', namespace)
            ]]

            new_string_cells = 0
            next_string_index = len(string_items)
            for row in reference_rows + problem_rows:
                for value in row:
                    if value is None or value == '':
                        continue
                    new_string_cells += 1
                    value = str(value)
                    if value in string_lookup:
                        continue
                    string_lookup[value] = next_string_index
                    next_string_index += 1
                    item = ET.SubElement(shared_strings, '{%s}si' % simple_xlsx.MAIN_NS)
                    text = ET.SubElement(item, '{%s}t' % simple_xlsx.MAIN_NS)
                    if value != value.strip():
                        text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                    text.text = value

            # Reuse the example worksheet's layout while leaving the user-facing page editable and empty.
            problems_sheet = deepcopy(example_sheet)
            for child in list(problems_sheet):
                if child.tag.rsplit('}', 1)[-1] in {
                    'sheetProtection', 'dataValidations', 'drawing', 'legacyDrawing', 'oleObjects', 'pageSetup',
                }:
                    problems_sheet.remove(child)
            problem_sheet_data = problems_sheet.find('x:sheetData', namespace)
            problem_sheet_data.clear()
            header = deepcopy(example_header)
            header.attrib['r'] = '1'
            header_cells = header.findall('x:c', namespace)
            for column_index, value in enumerate(problem_rows[0]):
                cell = header_cells[column_index]
                cell.attrib['r'] = '{}1'.format(simple_xlsx._excel_column_name(column_index))
                cell.attrib['t'] = 's'
                cell.find('x:v', namespace).text = str(string_lookup[str(value)])
            problem_sheet_data.append(header)
            problem_dimension = problems_sheet.find('x:dimension', namespace)
            problem_dimension.set('ref', 'A1:{}1'.format(
                simple_xlsx._excel_column_name(len(problem_rows[0]) - 1),
            ))
            validations = ET.Element('{%s}dataValidations' % simple_xlsx.MAIN_NS, {'count': '1'})
            validation = ET.SubElement(
                validations,
                '{%s}dataValidation' % simple_xlsx.MAIN_NS,
                {'type': 'list', 'allowBlank': '1', 'showErrorMessage': '1', 'sqref': question_type_range},
            )
            ET.SubElement(validation, '{%s}formula1' % simple_xlsx.MAIN_NS).text = (
                '"code,multiple_choice,fill_blank,true_false,short_answer"'
            )
            phonetic = problems_sheet.find('x:phoneticPr', namespace)
            validation_index = (
                list(problems_sheet).index(phonetic) + 1 if phonetic is not None
                else list(problems_sheet).index(problem_sheet_data) + 1
            )
            problems_sheet.insert(validation_index, validations)

            sheet_data = reference_sheet.find('x:sheetData', namespace)
            sheet_data.clear()
            max_columns = max(len(row) for row in reference_rows)
            for row_index, row in enumerate(reference_rows, start=1):
                row_element = ET.SubElement(
                    sheet_data,
                    '{%s}row' % simple_xlsx.MAIN_NS,
                    {'r': str(row_index), 'spans': '1:{}'.format(max_columns)},
                )
                for column_index, value in enumerate(row):
                    if value is None or value == '':
                        continue
                    cell = ET.SubElement(
                        row_element,
                        '{%s}c' % simple_xlsx.MAIN_NS,
                        {
                            'r': '{}{}'.format(simple_xlsx._excel_column_name(column_index), row_index),
                            't': 's',
                        },
                    )
                    ET.SubElement(cell, '{%s}v' % simple_xlsx.MAIN_NS).text = str(string_lookup[str(value)])

            dimension = reference_sheet.find('x:dimension', namespace)
            dimension.set('ref', 'A1:{}{}'.format(
                simple_xlsx._excel_column_name(max_columns - 1), len(reference_rows),
            ))
            string_count = int(shared_strings.attrib.get('count', 0))
            shared_strings.set('count', str(string_count - old_string_cells + new_string_cells))
            shared_strings.set('uniqueCount', str(next_string_index))
            ET.register_namespace('', simple_xlsx.MAIN_NS)
            ET.register_namespace('r', simple_xlsx.REL_NS)
            for prefix, uri in WORKBOOK_EXTENSION_NAMESPACES.items():
                ET.register_namespace(prefix, uri)
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as target:
                for info in source.infolist():
                    if info.filename == strings_path:
                        content = ET.tostring(shared_strings, encoding='utf-8', xml_declaration=True)
                    elif info.filename == content_types_path:
                        content = ET.tostring(content_types, encoding='utf-8', xml_declaration=True)
                    elif info.filename == app_properties_path:
                        content = ET.tostring(app_properties, encoding='utf-8', xml_declaration=True)
                    elif info.filename == 'xl/workbook.xml':
                        content = _serialize_excel_xml(workbook)
                    elif info.filename == 'xl/_rels/workbook.xml.rels':
                        content = ET.tostring(relationships, encoding='utf-8', xml_declaration=True)
                    elif info.filename == reference_sheet_path:
                        content = _serialize_excel_xml(reference_sheet)
                    elif info.filename == example_sheet_path:
                        content = _serialize_excel_xml(example_sheet)
                    else:
                        content = source.read(info.filename)
                    target.writestr(info, content)
                target.writestr(
                    problems_sheet_path,
                    _serialize_excel_xml(problems_sheet),
                )
        return output.getvalue()

    def download_excel_template_view(self, request):
        if not request.user.has_perm('judge.edit_own_problem'):
            raise PermissionDenied

        # This example retains a real Excel Word object so teachers can see
        # exactly how embedded DOCX descriptions and their figures are imported.
        sample_path = os.path.join(settings.BASE_DIR, 'resources', 'problem_batch_upload_template.xlsx')
        if os.path.isfile(sample_path):
            payload = self._template_with_current_references(request, sample_path)
        else:
            payload = build_xlsx_workbook([
                (
                    'Problems', [self._excel_headers()],
                    [{'sqref': _excel_template_question_type_range(), 'values': (
                        'code', 'multiple_choice', 'fill_blank', 'true_false', 'short_answer',
                    )}],
                ),
                ('Example Problems', self._excel_template_rows(request), (), True),
                ('Reference', self._excel_reference_rows(request)),
            ])
        response = HttpResponse(
            payload,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = 'attachment; filename="problem_batch_upload_template.xlsx"'
        return response

    def import_markdown_view(self, request):
        if not request.user.has_perm('judge.edit_own_problem'):
            raise PermissionDenied
        if request.method != 'POST':
            return JsonResponse({'ok': False, 'message': gettext('Only POST is allowed.')}, status=405)

        uploaded_file = request.FILES.get('markdown_file')
        if uploaded_file is None:
            return JsonResponse({'ok': False, 'message': gettext('Please choose a Markdown file.')}, status=400)

        try:
            with transaction.atomic():
                problem = self.create_problem_from_markdown_upload(request, uploaded_file)
        except UnicodeDecodeError:
            return JsonResponse(
                {'ok': False, 'message': gettext('Could not decode this file. Please use UTF-8 Markdown.')},
                status=400,
            )
        except forms.ValidationError as error:
            return JsonResponse({'ok': False, 'message': error.message}, status=400)

        return JsonResponse({
            'ok': True,
            'message': gettext('Created 1 problem from Markdown.'),
            'created': [self._serialize_created_problem(problem)],
            'redirect_url': reverse('admin:judge_problem_changelist'),
        })

    def create_problem_from_markdown_upload(self, request, uploaded_file):
        validate_markdown_upload_filename(uploaded_file)
        form = SimpleProblemCreationForm()
        form.default_group = self.get_default_group()
        form.default_type = self.get_default_type()
        form.default_languages = self.get_default_languages()
        form.default_points = self.get_default_points()
        form.default_time_limit = self.get_default_time_limit()
        form.default_memory_limit = self.get_default_memory_limit()
        form.generate_simple_code = self.generate_problem_code

        name, description = form.parse_markdown_upload(uploaded_file)

        if form.default_group is None:
            raise forms.ValidationError(_('You must create at least one problem group before creating a problem.'))
        if form.default_type is None:
            raise forms.ValidationError(_('You must create at least one problem type before creating a problem.'))
        if not form.default_languages:
            raise forms.ValidationError(_('You must configure at least one language before creating a problem.'))

        return self.create_problem_with_basic_configuration(
            request,
            name,
            description,
            allowed_languages=form.default_languages,
        )

    def create_problem_from_excel_row(self, request, header_map, row, row_number):
        normalized = {}
        for key, index in header_map.items():
            normalized[key] = row[index].strip() if index < len(row) and row[index] is not None else ''

        if not any(normalized.values()):
            return None
        if not normalized['name']:
            raise forms.ValidationError(_('Row %(row)d is missing "name".') % {'row': row_number})
        if not normalized['description']:
            raise forms.ValidationError(_('Row %(row)d is missing "description".') % {'row': row_number})

        organizations = self._resolve_excel_organizations(request, normalized['organizations'])
        classes = self._resolve_excel_classes(request, normalized['classes'])
        allowed_languages = self._resolve_excel_languages(normalized['allowed_languages'])
        return self.create_problem_with_basic_configuration(
            request,
            normalized['name'],
            normalized['description'],
            organizations=organizations,
            classes=classes,
            allowed_languages=allowed_languages,
        )

    def batch_create_excel_view(self, request):
        if not request.user.has_perm('judge.edit_own_problem'):
            raise PermissionDenied
        if request.method != 'POST':
            return JsonResponse({'ok': False, 'message': gettext('Only POST is allowed.')}, status=405)

        uploaded_file = request.FILES.get('excel_file')
        if uploaded_file is None:
            return JsonResponse({'ok': False, 'message': gettext('Please choose an Excel file.')}, status=400)

        try:
            validate_excel_upload_filename(uploaded_file)
            rows = read_xlsx_rows(uploaded_file)
        except zipfile.BadZipFile:
            return JsonResponse({'ok': False, 'message': gettext('The Excel file is invalid.')}, status=400)
        except (ET.ParseError, KeyError):
            return JsonResponse({'ok': False, 'message': gettext('The Excel file is invalid.')}, status=400)
        except ValueError as error:
            return JsonResponse({'ok': False, 'message': gettext(str(error))}, status=400)
        except forms.ValidationError as error:
            return JsonResponse({'ok': False, 'message': error.message}, status=400)

        if not rows:
            return JsonResponse({'ok': False, 'message': gettext('The Excel file is empty.')}, status=400)

        header_row = [str(cell).strip().lower() for cell in rows[0]]
        required_headers = self._legacy_excel_headers()
        missing_headers = [header for header in required_headers if header not in header_row]
        if missing_headers:
            return JsonResponse({
                'ok': False,
                'message': gettext('Missing required columns: %(columns)s.') % {
                    'columns': ', '.join(missing_headers),
                },
            }, status=400)

        header_map = {header: header_row.index(header) for header in required_headers}
        created = []
        try:
            with transaction.atomic():
                for row_number, row in enumerate(rows[1:], start=2):
                    problem = self.create_problem_from_excel_row(request, header_map, row, row_number)
                    if problem is None:
                        continue
                    created.append(self._serialize_created_problem(problem))
        except forms.ValidationError as error:
            return JsonResponse({'ok': False, 'message': error.message}, status=400)

        if not created:
            return JsonResponse({
                'ok': False,
                'message': gettext('The Excel file does not contain any problem rows.'),
            }, status=400)

        return JsonResponse({
            'ok': True,
            'message': ngettext(
                'Created %d problem from Excel.',
                'Created %d problems from Excel.',
                len(created),
            ) % len(created),
            'created': created,
            'redirect_url': reverse('admin:judge_problem_changelist'),
        })

    def batch_create_markdown_view(self, request):
        if not request.user.has_perm('judge.edit_own_problem'):
            raise PermissionDenied
        if request.method != 'POST':
            return JsonResponse({'ok': False, 'message': gettext('Only POST is allowed.')}, status=405)

        uploaded_files = request.FILES.getlist('markdown_files')
        if not uploaded_files:
            return JsonResponse(
                {'ok': False, 'message': gettext('Please choose at least one Markdown file.')},
                status=400,
            )

        created = []
        try:
            with transaction.atomic():
                for uploaded_file in uploaded_files:
                    problem = self.create_problem_from_markdown_upload(request, uploaded_file)
                    created.append(self._serialize_created_problem(problem))
        except UnicodeDecodeError:
            return JsonResponse(
                {'ok': False, 'message': gettext('Could not decode one of the files. Please use UTF-8 Markdown.')},
                status=400,
            )
        except forms.ValidationError as error:
            return JsonResponse({'ok': False, 'message': error.message}, status=400)

        return JsonResponse({
            'ok': True,
            'message': ngettext(
                'Created %d problem from Markdown.',
                'Created %d problems from Markdown.',
                len(created),
            ) % len(created),
            'created': created,
            'redirect_url': reverse('admin:judge_problem_changelist'),
        })

    def render_change_form(self, request, context, add=False, change=False, form_url='', obj=None):
        use_embedded_problem_layout = not add or not self.is_advanced_add_mode(request, obj)
        context['show_embedded_advanced_toggle'] = add and not self.is_advanced_add_mode(request, obj)
        context['show_problem_layout_toggle'] = use_embedded_problem_layout
        context['use_embedded_problem_layout'] = use_embedded_problem_layout
        context['is_advanced_add'] = add and self.is_advanced_add_mode(request, obj)
        context['is_embedded_advanced_mode'] = add and not self.is_advanced_add_mode(request, obj) and (
            self.is_embedded_advanced_mode(request, obj)
        )
        if context['show_embedded_advanced_toggle']:
            context['simple_preset_defaults'] = json.dumps(self.get_simple_preset_defaults(request))
        return super().render_change_form(request, context, add, change, form_url, obj)

    def save_model(self, request, obj, form, change):
        if (
            not change and
            not self.is_advanced_add_mode(request, obj) and
            not self.is_embedded_advanced_mode(request, obj)
        ):
            self.apply_creation_defaults(request, obj)

        # `organizations` and `classes` will not appear in `cleaned_data` if user cannot edit them
        if form.changed_data:
            if 'organizations' in form.changed_data or 'classes' in form.changed_data:
                has_orgs = form.cleaned_data.get('organizations')
                has_classes = form.cleaned_data.get('classes')
                obj.is_organization_private = bool(has_orgs or has_classes)

        if form.cleaned_data.get('is_public') and not request.user.has_perm('judge.change_public_visibility'):
            if not obj.is_organization_private:
                raise PermissionDenied
            if not request.user.has_perm('judge.create_private_problem'):
                raise PermissionDenied

        super(ProblemAdmin, self).save_model(request, obj, form, change)
        if (
            change and
            form.changed_data and
            any(f in form.changed_data for f in ('is_public', 'organizations', 'classes', 'points', 'partial'))
        ):
            self._rescore(request, obj.id)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if (
            change or
            self.is_advanced_add_mode(request, form.instance) or
            self.is_embedded_advanced_mode(request, form.instance)
        ):
            return

        problem = form.instance
        default_type = self.get_default_type()
        default_languages = self.get_default_languages()

        if default_type is not None:
            problem.types.set([default_type])
        if default_languages:
            problem.allowed_languages.set(default_languages)
        if hasattr(request.user, 'profile'):
            problem.authors.set([request.user.profile])

    def construct_change_message(self, request, form, *args, **kwargs):
        if form.cleaned_data.get('change_message'):
            return form.cleaned_data['change_message']
        return super(ProblemAdmin, self).construct_change_message(request, form, *args, **kwargs)


class ProblemPointsVoteAdmin(admin.ModelAdmin):
    list_display = ('points', 'voter', 'linked_problem', 'vote_time')
    search_fields = ('voter__user__username', 'problem__code', 'problem__name')
    readonly_fields = ('voter', 'problem', 'vote_time')

    def get_queryset(self, request):
        return ProblemPointsVote.objects.filter(problem__in=Problem.get_editable_problems(request.user))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        if obj is None:
            return request.user.has_perm('judge.edit_own_problem')
        return obj.problem.is_editable_by(request.user)

    def lookup_allowed(self, key, value):
        return super().lookup_allowed(key, value) or key in ('problem__code',)

    @admin.display(description=_('problem'), ordering='problem__name')
    def linked_problem(self, obj):
        link = reverse('problem_detail', args=[obj.problem.code])
        return format_html('<a href="{0}">{1}</a>', link, obj.problem.name)
