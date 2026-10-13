import json
import os
from operator import attrgetter, itemgetter

import pyotp
import webauthn
from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import RegexValidator
from django.db.models import Q
from django.forms import BooleanField, CharField, ChoiceField, Form, ModelForm, ModelMultipleChoiceField, \
    MultipleChoiceField
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy as _, ngettext_lazy

from judge.models import Class, Contest, Language, Organization, Problem, ProblemPointsVote, Profile, Submission, \
    WebAuthnCredential
from judge.utils.mail import validate_email_domain
from judge.utils.problem_create import generate_problem_code
from judge.utils.subscription import newsletter_id
from judge.utils.unique_ids import is_series_id
from judge.utils.user_search import apply_profile_choice_labels
from judge.widgets import (
    AceWidget, HeavySelect2MultipleWidget, MarkdownCheckboxSelectMultiple, MarkdownRadioSelect,
    MartorWidget, Select2MultipleWidget, Select2Widget, math_engine_for_profile,
)

TOTP_CODE_LENGTH = 6

two_factor_validators_by_length = {
    TOTP_CODE_LENGTH: {
        'regex_validator': RegexValidator(
            f'^[0-9]{{{TOTP_CODE_LENGTH}}}$',
            format_lazy(ngettext_lazy('Two-factor authentication tokens must be {count} decimal digit.',
                                      'Two-factor authentication tokens must be {count} decimal digits.',
                                      TOTP_CODE_LENGTH), count=TOTP_CODE_LENGTH),
        ),
        'verify': lambda code, profile: not profile.check_totp_code(code),
        'err': _('Invalid two-factor authentication token.'),
    },
    16: {
        'regex_validator': RegexValidator('^[A-Z0-9]{16}$', _('Scratch codes must be 16 Base32 characters.')),
        'verify': lambda code, profile: code not in json.loads(profile.scratch_codes),
        'err': _('Invalid scratch code.'),
    },
}


class ProfileForm(ModelForm):
    if newsletter_id is not None:
        newsletter = forms.BooleanField(label=_('Subscribe to contest updates'), initial=False, required=False)
    test_site = forms.BooleanField(label=_('Enable experimental features'), initial=False, required=False)

    class Meta:
        model = Profile
        fields = ['about', 'organizations', 'timezone', 'language', 'ace_theme', 'site_theme', 'user_script']
        widgets = {
            'timezone': Select2Widget(attrs={'style': 'width:200px'}),
            'language': Select2Widget(attrs={'style': 'width:200px'}),
            'ace_theme': Select2Widget(attrs={'style': 'width:200px'}),
            'site_theme': Select2Widget(attrs={'style': 'width:200px'}),
        }

        has_math_config = bool(settings.MATHOID_URL)
        if has_math_config:
            fields.append('math_engine')
            widgets['math_engine'] = Select2Widget(attrs={'style': 'width:200px'})

        widgets['about'] = MartorWidget(attrs={'data-markdownfy-url': reverse_lazy('profile_preview')})

    def clean_about(self):
        if 'about' in self.changed_data and not self.instance.has_any_solves:
            raise ValidationError(_('You must solve at least one problem before you can update your profile.'))
        return self.cleaned_data['about']

    def clean(self):
        organizations = self.cleaned_data.get('organizations') or []
        max_orgs = settings.DMOJ_USER_MAX_ORGANIZATION_COUNT

        if sum(org.is_open for org in organizations) > max_orgs:
            raise ValidationError(ngettext_lazy('You may not be part of more than {count} public organization.',
                                                'You may not be part of more than {count} public organizations.',
                                                max_orgs).format(count=max_orgs))

        return self.cleaned_data

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super(ProfileForm, self).__init__(*args, **kwargs)
        if not user.has_perm('judge.edit_all_organization'):
            self.fields['organizations'].queryset = Organization.objects.filter(
                Q(is_open=True) | Q(id__in=user.profile.organizations.all()),
            )
        if not self.fields['organizations'].queryset:
            self.fields.pop('organizations')
        self.fields['user_script'].widget = AceWidget(mode='javascript', theme=user.profile.resolved_ace_theme)


class EmailChangeForm(Form):
    password = CharField(widget=forms.PasswordInput())
    email = forms.EmailField()

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_email(self):
        if User.objects.filter(email=self.cleaned_data['email']).exists():
            raise ValidationError(_('This email address is already taken.'))
        validate_email_domain(self.cleaned_data['email'])
        return self.cleaned_data['email']

    def clean_password(self):
        if not self.user.check_password(self.cleaned_data['password']):
            raise ValidationError(_('Invalid password'))
        return self.cleaned_data['password']


class DownloadDataForm(Form):
    comment_download = BooleanField(required=False, label=_('Download comments?'))
    submission_download = BooleanField(required=False, label=_('Download submissions?'))
    submission_problem_glob = CharField(initial='*', label=_('Filter by problem code glob:'), max_length=100)
    submission_results = MultipleChoiceField(
        required=False,
        widget=Select2MultipleWidget(
            attrs={'style': 'width: 260px', 'data-placeholder': _('Leave empty to include all submissions')},
        ),
        choices=sorted(map(itemgetter(0, 0), Submission.RESULT)),
        label=_('Filter by result:'),
    )

    def clean(self):
        can_download = ('comment_download', 'submission_download')
        if not any(self.cleaned_data[v] for v in can_download):
            raise ValidationError(_('Please select at least one thing to download.'))
        return self.cleaned_data

    def clean_submission_problem_glob(self):
        if not self.cleaned_data['submission_download']:
            return '*'
        return self.cleaned_data['submission_problem_glob']

    def clean_submission_result(self):
        if not self.cleaned_data['submission_download']:
            return ()
        return self.cleaned_data['submission_result']


class ProblemSubmitForm(ModelForm):
    source = CharField(max_length=65536, widget=AceWidget(theme='twilight', no_ace_media=True))
    judge = ChoiceField(choices=(), widget=forms.HiddenInput(), required=False)

    def __init__(self, *args, judge_choices=(), **kwargs):
        super(ProblemSubmitForm, self).__init__(*args, **kwargs)
        self.fields['language'].empty_label = None
        self.fields['language'].label_from_instance = attrgetter('display_name')
        self.fields['language'].queryset = Language.objects.filter(judges__online=True).distinct()

        if judge_choices:
            self.fields['judge'].widget = Select2Widget(
                attrs={'style': 'width: 150px', 'data-placeholder': _('Any judge')},
            )
            self.fields['judge'].choices = judge_choices

    class Meta:
        model = Submission
        fields = ['language']


class MCQSubmitForm(Form):
    """Student-facing multiple-choice answer form.

    Choices are presented in this student's order (see ``ordered_options_for``) and posted back
    as stable option ids, so per-student shuffling never affects answer identity. Only id/text
    reach the template — never ``is_correct``.
    """

    def __init__(self, *args, problem=None, profile=None, **kwargs):
        super().__init__(*args, **kwargs)
        from judge.question_types.mcq import ordered_options_for

        try:
            config = problem.mcq_config
        except ObjectDoesNotExist:
            config = None
        self.multiple = bool(config and config.multiple_select)

        choices = [(str(o['id']), o['text']) for o in ordered_options_for(problem, profile)]
        widget_kwargs = {
            'markdown_style': problem.markdown_style,
            'math_engine': math_engine_for_profile(profile),
        }
        if self.multiple:
            self.fields['options'] = MultipleChoiceField(
                choices=choices, widget=MarkdownCheckboxSelectMultiple(**widget_kwargs),
                label='', required=True)
        else:
            self.fields['options'] = ChoiceField(
                choices=choices, widget=MarkdownRadioSelect(**widget_kwargs),
                label='', required=True)

    def clean(self):
        cleaned = super().clean()
        value = cleaned.get('options')
        if self.multiple:
            cleaned['selected'] = [int(v) for v in (value or [])]
        elif value:
            cleaned['selected'] = [int(value)]
        else:
            cleaned['selected'] = []
        return cleaned


class NoAutoCompleteCharField(forms.CharField):
    def widget_attrs(self, widget):
        attrs = super(NoAutoCompleteCharField, self).widget_attrs(widget)
        attrs['autocomplete'] = 'off'
        return attrs


class FillBlankSubmitForm(Form):
    """Student-facing fill-in-the-blank answer form.

    One text field per blank, named ``fb_<order>`` and labelled by the blank's label (or
    ``Blank N``). ``clean`` collects the answers in blank order into ``cleaned['answers']``. The
    accepted answers are never exposed to the template.
    """

    def __init__(self, *args, problem=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.blanks = list(problem.blanks.all())
        for index, blank in enumerate(self.blanks):
            # Distinct name prefix avoids browsers matching prior ``blank_N`` autofill history.
            field = NoAutoCompleteCharField(
                label=blank.label or (_('Blank %d') % (index + 1)),
                max_length=1000, required=True, strip=False,
            )
            # Extra browser hints: Chrome often ignores autocomplete=off alone.
            field.widget.attrs.update({
                'autocomplete': 'new-password',
                'autocapitalize': 'off',
                'autocorrect': 'off',
                'spellcheck': 'false',
                'data-lpignore': 'true',
                'data-1p-ignore': 'true',
                'readonly': 'readonly',
                'onfocus': "this.removeAttribute('readonly');",
            })
            self.fields['fb_%d' % blank.order] = field

    def clean(self):
        cleaned = super().clean()
        cleaned['answers'] = [cleaned.get('fb_%d' % blank.order, '') or '' for blank in self.blanks]
        return cleaned


class FileSubmitForm(Form):
    """Student-facing file-upload answer form.

    A single file field, validated server-side (extension + size) against the problem's
    ``ProblemFileConfig`` so an invalid upload never reaches storage. ``profile`` is accepted for
    a uniform ``build_form`` signature but unused here.
    """
    file = forms.FileField(label=_('Your file'), required=True)

    def __init__(self, *args, problem=None, profile=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.problem = problem

    def clean_file(self):
        upload = self.cleaned_data['file']
        try:
            config = self.problem.file_config
        except ObjectDoesNotExist:
            config = None
        if config is not None:
            allowed = config.allowed_extension_list()
            if allowed:
                ext = os.path.splitext(upload.name)[1].lower().lstrip('.')
                if ext not in allowed:
                    raise ValidationError(
                        _('File type "%(ext)s" is not allowed. Allowed types: %(allowed)s.'),
                        params={'ext': ext or _('(none)'), 'allowed': ', '.join(allowed)})
            if config.max_file_size and upload.size > config.max_file_size:
                raise ValidationError(
                    _('File is too large (%(size)d bytes); the limit is %(max)d bytes.'),
                    params={'size': upload.size, 'max': config.max_file_size})
        return upload


class ShortAnswerSubmitForm(Form):
    """Student-facing short-answer form: a single free-text response."""
    answer = NoAutoCompleteCharField(
        label='', required=True, strip=True,
        widget=forms.Textarea(attrs={'rows': 6, 'class': 'short-answer-input'}),
    )

    def __init__(self, *args, problem=None, **kwargs):
        super().__init__(*args, **kwargs)


class OrganizationSiteForm(ModelForm):
    """Front-end create/edit course form: no slug field (auto-generated), RichEditor description."""

    class Meta:
        model = Organization
        fields = ['short_name', 'name', 'about', 'is_open', 'class_required', 'slots',
                  'admins', 'instructors', 'logo_override_image']
        labels = {
            'short_name': _('Course code'),
            'name': _('Course name'),
            'about': _('Course description'),
            'is_open': _('Is open organization?'),
            'class_required': _('Class membership required'),
            'slots': _('Maximum size'),
            'admins': _('Course administrators'),
            'instructors': _('Course instructors'),
            'logo_override_image': _('Logo override image'),
        }
        help_texts = {
            'is_open': _('If checked, students can join without approval. Cannot combine with class required.'),
            'class_required': _('If checked, students must select a class when requesting membership. '
                                'Site teachers can request the course without picking a class.'),
            'slots': _('Optional. Maximum members for private courses.'),
            'admins': _('People who can edit this course and review join requests. '
                        'The creator is included by default and can add co-administrators.'),
            'instructors': _(
                'Teachers on this course. Defaults to the creator; site teachers are added '
                'automatically when their course join request is approved.',
            ),
        }
        widgets = {
            'about': forms.Textarea(attrs={
                'rows': 4,
                'class': 'rich-editor-source',
                'data-rich-editor': 'course-about',
            }),
            'slots': forms.NumberInput(attrs={'min': 1, 'placeholder': _('Optional')}),
            'admins': HeavySelect2MultipleWidget(data_view='profile_select2', attrs={'style': 'width: 100%'}),
            'instructors': HeavySelect2MultipleWidget(data_view='profile_select2', attrs={'style': 'width: 100%'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_profile_choice_labels(self)
        if 'slots' in self.fields:
            self.fields['slots'].required = False
        if 'logo_override_image' in self.fields:
            self.fields['logo_override_image'].required = False
        if 'admins' in self.fields:
            self.fields['admins'].required = False
        if 'instructors' in self.fields:
            self.fields['instructors'].required = False
        # RichEditor mounts a visual editor; the Django textarea stays hidden.
        # HTML5 "required" on that hidden control blocks submit before JS can
        # copy markdown back — validate emptiness in clean_about instead.
        if 'about' in self.fields:
            self.fields['about'].required = False

    def clean_about(self):
        about = (self.cleaned_data.get('about') or '').strip()
        if not about:
            raise ValidationError(_('This field is required.'))
        return about

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('class_required') and cleaned.get('is_open'):
            raise ValidationError(_('Class membership cannot be enforced when the course has open enrollment.'))
        return cleaned


class EditOrganizationForm(OrganizationSiteForm):
    """Edit course uses the same fields as create (admins + instructors)."""
    pass


def suggested_class_name(academic_year='', term_session='', instructor_names=''):
    """Build a class name from year, session, and instructor (e.g. 2027-2028 Fall Session Prof. Huang)."""
    parts = []
    year = (academic_year or '').strip()
    term = (term_session or '').strip()
    instructors = (instructor_names or '').strip()
    if year:
        parts.append(year)
    if term:
        parts.append(term)
    if instructors:
        parts.append(instructors)
    return ' '.join(parts)


def suggested_class_name_legacy(organization, academic_year='', term_session=''):
    """Previous auto name: course code + year + session (no instructor)."""
    parts = []
    if organization is not None:
        code = (getattr(organization, 'short_name', None) or getattr(organization, 'name', '') or '').strip()
        if code:
            parts.append(code)
    year = (academic_year or '').strip()
    term = (term_session or '').strip()
    if year:
        parts.append(year)
    if term:
        parts.append(term)
    return ' '.join(parts)


def uniquify_class_name(name, instance=None):
    """Append 2, 3, … when *name* is already taken."""
    candidate = name
    suffix = 2
    while True:
        qs = Class.objects.filter(name=candidate)
        if instance is not None and instance.pk:
            qs = qs.exclude(pk=instance.pk)
        if not qs.exists():
            return candidate
        candidate = '%s %d' % (name, suffix)
        suffix += 1


class ClassSiteForm(ModelForm):
    """Front-end create/edit class form aligned with the class-form demo modules."""

    TERM_SESSION_CHOICES = (
        ('', _('Select…')),
        ('Fall - Session 1', _('Fall - Session 1')),
        ('Fall - Session 2', _('Fall - Session 2')),
        ('Spring - Session 1', _('Spring - Session 1')),
        ('Spring - Session 2', _('Spring - Session 2')),
    )

    member_csv = forms.FileField(
        required=False,
        label=_('Import members CSV'),
        help_text=_('Optional on create: Canvas roster CSV is imported when you save. '
                    'On edit, use Import CSV to import immediately.'),
    )

    class Meta:
        model = Class
        fields = [
            'name', 'description',
            'academic_year', 'term_session', 'credits', 'location', 'start_date', 'end_date',
            'instructor_names', 'ta_names', 'admins', 'members', 'access_code', 'is_active',
        ]
        labels = {
            'name': _('Name'),
            'description': _('Description'),
            'academic_year': _('Academic year'),
            'term_session': _('Term and session'),
            'credits': _('Credits'),
            'location': _('Location or Canvas link'),
            'start_date': _('Class start'),
            'end_date': _('Class end'),
            'instructor_names': _('Instructors'),
            'ta_names': _('Teaching assistants'),
            'admins': _('Class administrators'),
            'members': _('Members'),
            'access_code': _('Class access code'),
            'is_active': _('Class is active'),
        }
        help_texts = {
            'name': _('Filled in from the academic year, session, and instructor. You can still edit it.'),
            'instructor_names': _('Required. Comma-separated names shown on the class page and course class cards.'),
            'ta_names': _('Comma-separated TA names shown on the class page.'),
            'admins': _('Optional. The creator is always added as a class administrator.'),
            'members': _('Search course members or users. Use Import CSV to bulk-add usernames.'),
            'access_code': _('Optional.'),
            'location': _('Room, building, or a Canvas / meeting URL.'),
        }
        widgets = {
            'description': forms.Textarea(attrs={
                'rows': 4,
                'class': 'rich-editor-source',
                'data-rich-editor': 'class-description',
            }),
            'academic_year': forms.Select(attrs={'class': 'oj-select'}),
            'term_session': forms.Select(attrs={'class': 'oj-select'}),
            'credits': forms.NumberInput(attrs={'class': 'oj-input', 'min': '0', 'step': '0.5'}),
            'location': forms.TextInput(attrs={'class': 'oj-input', 'placeholder': 'IB 204'}),
            'start_date': forms.DateInput(attrs={'class': 'oj-input', 'type': 'date'}),
            'end_date': forms.DateInput(attrs={'class': 'oj-input', 'type': 'date'}),
            'instructor_names': forms.TextInput(attrs={
                'class': 'oj-input',
                'placeholder': _('e.g. Alice Smith'),
            }),
            'ta_names': forms.TextInput(attrs={
                'class': 'oj-input',
                'placeholder': _('e.g. Carol Chen, Dan Wu'),
            }),
            'access_code': forms.TextInput(attrs={'class': 'oj-input', 'maxlength': '7'}),
            'name': forms.TextInput(attrs={
                'class': 'oj-input',
                'placeholder': _('Generated from year, session, and instructor'),
                'autocomplete': 'off',
            }),
            'admins': HeavySelect2MultipleWidget(data_view='profile_select2', attrs={'style': 'width: 100%'}),
            'members': HeavySelect2MultipleWidget(data_view='profile_select2', attrs={'style': 'width: 100%'}),
        }

    def __init__(self, *args, organization=None, **kwargs):
        self.organization = organization
        super().__init__(*args, **kwargs)
        apply_profile_choice_labels(self)
        year = timezone.now().year
        year_choices = [('', _('Select…'))]
        for start in range(year + 1, year - 4, -1):
            label = '%d-%d' % (start, start + 1)
            year_choices.append((label, label))
        self.fields['academic_year'].widget.choices = year_choices
        self.fields['term_session'].widget.choices = list(self.TERM_SESSION_CHOICES)
        self.fields['name'].required = False
        self.fields['instructor_names'].required = True
        for name in (
            'admins', 'members', 'access_code', 'description', 'academic_year', 'term_session',
            'credits', 'location', 'start_date', 'end_date', 'ta_names',
        ):
            if name in self.fields:
                self.fields[name].required = False

    def clean_instructor_names(self):
        value = (self.cleaned_data.get('instructor_names') or '').strip()
        if not value:
            raise ValidationError(_('This field is required.'))
        return value

    def clean_ta_names(self):
        return (self.cleaned_data.get('ta_names') or '').strip()

    def clean_name(self):
        return (self.cleaned_data.get('name') or '').strip()

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get('start_date')
        end = cleaned.get('end_date')
        if start and end and end < start:
            self.add_error('end_date', _('The end date must be after the start date.'))

        name = (cleaned.get('name') or '').strip()
        year = cleaned.get('academic_year')
        term = cleaned.get('term_session')
        instructors = cleaned.get('instructor_names')
        suggested = suggested_class_name(year, term, instructors)
        legacy = suggested_class_name_legacy(self.organization, year, term)
        generated = not name or name in {
            suggested,
            suggested_class_name(year, term, ''),
            legacy,
        }
        if generated:
            name = suggested
        if not name:
            self.add_error(
                'name',
                _('Select an academic year, session, and instructor, or enter a class name.'),
            )
            return cleaned
        if generated:
            name = uniquify_class_name(name, self.instance)
        else:
            qs = Class.objects.filter(name=name)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                self.add_error('name', _(
                    'A class with this name already exists. Class names must be unique across the site.',
                ))
                return cleaned
        cleaned['name'] = name
        return cleaned


class ClassAssignmentsForm(Form):
    """Link existing contests to a class as Assignments (no Django-admin dependency)."""

    contests = ModelMultipleChoiceField(
        queryset=Contest.objects.none(),
        required=False,
        label=_('Assignments'),
        help_text=_('Choose contests to show under this class’s Assignments tab. '
                    'Create or edit contest content from Contests; this only links them here.'),
        widget=HeavySelect2MultipleWidget(data_view='contest_select2', attrs={'style': 'width: 100%'}),
    )

    def __init__(self, *args, class_obj=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.class_obj = class_obj
        self.user = user
        # CA-3: only contests the user can edit may be linked. Contests that are
        # already linked to this class stay selectable so that saving the form
        # does not drop them (or reject the whole submit).
        self.fields['contests'].queryset = self.linkable_contests(user, class_obj).order_by('-start_time')
        if class_obj is not None and class_obj.pk:
            self.fields['contests'].initial = list(
                Contest.objects.filter(classes=class_obj).values_list('id', flat=True),
            )

    @staticmethod
    def editable_contests(user):
        if user is None or not user.is_authenticated:
            return Contest.objects.none()
        if user.has_perm('judge.edit_all_contest'):
            return Contest.objects.all()
        profile = user.profile
        return Contest.objects.filter(Q(authors=profile) | Q(curators=profile)).distinct()

    @classmethod
    def linkable_contests(cls, user, class_obj):
        editable_ids = cls.editable_contests(user).values('id')
        q = Q(id__in=editable_ids)
        if class_obj is not None and class_obj.pk:
            q |= Q(classes=class_obj)
        return Contest.objects.filter(q).distinct()

    def clean_contests(self):
        contests = self.cleaned_data.get('contests')
        if not contests:
            return contests
        already = set()
        if self.class_obj is not None and self.class_obj.pk:
            already = set(Contest.objects.filter(classes=self.class_obj).values_list('id', flat=True))
        for contest in contests:
            if contest.id not in already and (self.user is None or not contest.is_editable_by(self.user)):
                raise ValidationError(
                    _('You can only add assignments that you can edit: %(contest)s'),
                    params={'contest': contest.name},
                )
        return contests


class CustomAuthenticationForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super(CustomAuthenticationForm, self).__init__(*args, **kwargs)
        self.fields['username'].widget.attrs.update({'placeholder': _('Username')})
        self.fields['password'].widget.attrs.update({'placeholder': _('Password')})

        self.has_google_auth = self._has_social_auth('GOOGLE_OAUTH2')
        self.has_facebook_auth = self._has_social_auth('FACEBOOK')
        self.has_github_auth = self._has_social_auth('GITHUB_SECURE')

    def _has_social_auth(self, key):
        return (getattr(settings, 'SOCIAL_AUTH_%s_KEY' % key, None) and
                getattr(settings, 'SOCIAL_AUTH_%s_SECRET' % key, None))


class TOTPForm(Form):
    TOLERANCE = settings.DMOJ_TOTP_TOLERANCE_HALF_MINUTES

    totp_or_scratch_code = NoAutoCompleteCharField(required=False, widget=forms.TextInput(attrs={'autofocus': True}))

    def __init__(self, *args, **kwargs):
        self.profile = kwargs.pop('profile')
        super().__init__(*args, **kwargs)

    def clean(self):
        totp_or_scratch_code = self.cleaned_data.get('totp_or_scratch_code')
        try:
            validator = two_factor_validators_by_length[len(totp_or_scratch_code)]
        except KeyError:
            raise ValidationError(_('Invalid code length.'))
        validator['regex_validator'](totp_or_scratch_code)
        if validator['verify'](totp_or_scratch_code, self.profile):
            raise ValidationError(validator['err'])


class TOTPEnableForm(TOTPForm):
    def __init__(self, *args, **kwargs):
        self.totp_key = kwargs.pop('totp_key')
        super().__init__(*args, **kwargs)

    def clean(self):
        totp_validate = two_factor_validators_by_length[TOTP_CODE_LENGTH]
        code = self.cleaned_data.get('totp_or_scratch_code')
        totp_validate['regex_validator'](code)
        if not pyotp.TOTP(self.totp_key).verify(code, valid_window=settings.DMOJ_TOTP_TOLERANCE_HALF_MINUTES):
            raise ValidationError(totp_validate['err'])


class TwoFactorLoginForm(TOTPForm):
    webauthn_response = forms.CharField(widget=forms.HiddenInput(), required=False)

    def __init__(self, *args, **kwargs):
        self.webauthn_challenge = kwargs.pop('webauthn_challenge')
        self.webauthn_origin = kwargs.pop('webauthn_origin')
        super().__init__(*args, **kwargs)

    def clean(self):
        totp_or_scratch_code = self.cleaned_data.get('totp_or_scratch_code')
        if self.profile.is_webauthn_enabled and self.cleaned_data.get('webauthn_response'):
            if len(self.cleaned_data['webauthn_response']) > 65536:
                raise ValidationError(_('Invalid WebAuthn response.'))

            if not self.webauthn_challenge:
                raise ValidationError(_('No WebAuthn challenge issued.'))

            response = json.loads(self.cleaned_data['webauthn_response'])
            try:
                credential = self.profile.webauthn_credentials.get(cred_id=response.get('id', ''))
            except WebAuthnCredential.DoesNotExist:
                raise ValidationError(_('Invalid WebAuthn credential ID.'))

            user = credential.webauthn_user
            # Work around a useless check in the webauthn package.
            user.credential_id = credential.cred_id
            assertion = webauthn.WebAuthnAssertionResponse(
                webauthn_user=user,
                assertion_response=response.get('response'),
                challenge=self.webauthn_challenge,
                origin=self.webauthn_origin,
                uv_required=False,
            )

            try:
                sign_count = assertion.verify()
            except Exception as e:
                raise ValidationError(str(e))

            credential.counter = sign_count
            credential.save(update_fields=['counter'])
        elif totp_or_scratch_code:
            if self.profile.is_totp_enabled and self.profile.check_totp_code(totp_or_scratch_code):
                return
            elif self.profile.scratch_codes and totp_or_scratch_code in json.loads(self.profile.scratch_codes):
                scratch_codes = json.loads(self.profile.scratch_codes)
                scratch_codes.remove(totp_or_scratch_code)
                self.profile.scratch_codes = json.dumps(scratch_codes)
                self.profile.save(update_fields=['scratch_codes'])
                return
            elif self.profile.is_totp_enabled:
                raise ValidationError(_('Invalid two-factor authentication token or scratch code.'))
            else:
                raise ValidationError(_('Invalid scratch code.'))
        else:
            raise ValidationError(_('Must specify either totp_token or webauthn_response.'))


class ProblemCloneForm(Form):
    code = CharField(max_length=20, validators=[RegexValidator('^[a-z0-9]+$', _('Problem code must be ^[a-z0-9]+$'))])

    def clean_code(self):
        code = self.cleaned_data['code']
        if Problem.objects.filter(code=code).exists():
            if is_series_id(code, 'p'):
                return generate_problem_code()
            raise ValidationError(_('Problem code "%(code)s" already exists. Choose a different code.') % {
                'code': code,
            })
        return code


class ContestCloneForm(Form):
    key = CharField(max_length=20, validators=[RegexValidator('^[a-z0-9]+$', _('Contest id must be ^[a-z0-9]+$'))])

    def clean_key(self):
        key = self.cleaned_data['key']
        if Contest.objects.filter(key=key).exists():
            if is_series_id(key, 'c'):
                return Contest.generate_unique_key()
            raise ValidationError(_('Assignment ID "%(key)s" already exists. Choose a different ID.') % {
                'key': key,
            })
        return key


class ProblemPointsVoteForm(ModelForm):
    note = CharField(max_length=8192, required=False)

    class Meta:
        model = ProblemPointsVote
        fields = ['points', 'note']
