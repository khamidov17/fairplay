"""Front-end create + edit flow for multiple-choice problems.

A single view drives both creating a brand-new MCQ problem (no admin round-trip) and editing an
existing one. Permissions reuse the existing problem perms: ``judge.create_private_problem`` to
create, ``Problem.is_editable_by`` to edit. Code-centric required fields are auto-filled via
``apply_simple_problem_defaults`` so teachers only ever see the prompt, choices, and settings.
"""
import re
import zipfile
from xml.etree import ElementTree as ET

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.forms import inlineformset_factory
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext as _
from django.views.generic import View

from judge.models import Class, Language, Organization, Problem, ProblemMCQConfig, ProblemMCQOption, \
    ProblemSubmissionType
from judge.question_types.mcq import selected_option_ids
from judge.utils.problem_create import ScopedProblemFormMixin, apply_simple_problem_defaults, default_problem_type
from judge.utils.simple_xlsx import build_xlsx_workbook, read_xlsx_rows
from judge.widgets import HeavySelect2MultipleWidget, MartorWidget


class ProblemMCQForm(ScopedProblemFormMixin, forms.ModelForm):
    class Meta:
        model = Problem
        fields = ['name', 'description', 'points', 'partial', 'is_public', 'organizations', 'classes']
        widgets = {
            'description': MartorWidget(attrs={'data-markdownfy-url': reverse_lazy('problem_preview')}),
            'organizations': HeavySelect2MultipleWidget(data_view='organization_select2'),
            'classes': HeavySelect2MultipleWidget(data_view='class_select2'),
        }
        labels = {
            'description': _('Question prompt'),
            'partial': _('Allow partial credit (multiple-answer questions)'),
        }

    class Media:
        # Dependent select2: filters the class dropdown by the chosen organization(s). The script
        # falls back from django.jQuery to the global jQuery, so it works outside the admin too.
        js = ('admin_class_filter.js',)

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields['organizations'].required = False
        self.fields['classes'].required = False
        self.scope_problem_fields()

    def clean(self):
        # Mirror admin ProblemForm.clean: classes must belong to the selected organizations.
        cleaned_data = super().clean()
        organizations = cleaned_data.get('organizations')
        classes = cleaned_data.get('classes')
        if classes and organizations:
            org_ids = set(organizations.values_list('id', flat=True))
            if classes.exclude(organization_id__in=org_ids).exists():
                raise forms.ValidationError(_('Selected classes must belong to selected organizations.'))
        elif classes and not organizations:
            raise forms.ValidationError(_('You must select organizations before selecting classes.'))

        # Mirror admin ProblemAdmin.save_model: without change_public_visibility, a problem may
        # only be made public if it is organization-private and the user can create private problems.
        if cleaned_data.get('is_public') and self.user is not None \
                and not self.user.has_perm('judge.change_public_visibility'):
            org_private = bool(organizations or classes)
            if not org_private:
                self.add_error('is_public', _('You do not have permission to make a problem '
                                              'public unless it is private to an organization.'))
            elif not self.user.has_perm('judge.create_private_problem'):
                self.add_error('is_public', _('You do not have permission to make this problem public.'))
        return cleaned_data


class MCQConfigForm(forms.ModelForm):
    class Meta:
        model = ProblemMCQConfig
        fields = ['multiple_select', 'shuffle_options']


def make_option_formset(extra):
    return inlineformset_factory(
        Problem, ProblemMCQOption,
        fields=['text', 'is_correct', 'order'], extra=extra, can_delete=True,
        widgets={
            'order': forms.HiddenInput(attrs={'class': 'option-order'}),
            'text': forms.Textarea(attrs={'rows': 2, 'class': 'option-text'}),
        },
    )


class ProblemMCQEditView(LoginRequiredMixin, View):
    template_name = 'problem/mcq_edit.html'

    def _split_multi_value(self, value):
        if value is None:
            return []
        return [item.strip() for item in re.split(r'[\n,;|]+', str(value)) if item.strip()]

    def _resolve_tokens(self, queryset, tokens, label, resolvers):
        resolved = []
        seen = set()
        for token in tokens:
            matches = []
            for resolver in resolvers:
                matches.extend(resolver(queryset, token))
            unique_matches = []
            local_seen = set()
            for match in matches:
                if match.pk not in local_seen:
                    unique_matches.append(match)
                    local_seen.add(match.pk)
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

    def _resolve_organizations(self, raw_value):
        tokens = self._split_multi_value(raw_value)
        if not tokens:
            return []
        queryset = self._visible_template_organizations()
        return self._resolve_tokens(
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

    def _resolve_classes(self, raw_value):
        tokens = self._split_multi_value(raw_value)
        if not tokens:
            return []
        queryset = Class.get_visible_classes(self.request.user)
        return self._resolve_tokens(
            queryset,
            tokens,
            _('Class'),
            (
                lambda qs, token: list(qs.filter(slug=token)),
                lambda qs, token: list(qs.filter(name=token)),
                lambda qs, token: list(qs.filter(pk=int(token))) if token.isdigit() else [],
            ),
        )

    def _parse_bool(self, value):
        return str(value).strip().lower() in ('1', 'true', 'yes', 'y', 'on')

    def _parse_float(self, value, field_name, row_number):
        raw = str(value).strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            raise forms.ValidationError(_('Row %(row)d has invalid %(field)s.') % {
                'row': row_number,
                'field': field_name,
            })

    def _resolve_languages(self, raw_value):
        tokens = self._split_multi_value(raw_value)
        if not tokens:
            return []
        queryset = Language.objects.all()
        return self._resolve_tokens(
            queryset,
            tokens,
            _('Language'),
            (
                lambda qs, token: list(qs.filter(key=token)),
                lambda qs, token: list(qs.filter(short_name=token)),
                lambda qs, token: list(qs.filter(common_name=token)),
                lambda qs, token: list(qs.filter(name=token)),
                lambda qs, token: list(qs.filter(pk=int(token))) if token.isdigit() else [],
            ),
        )

    def _mcq_excel_headers(self):
        return [
            'name', 'description', 'points', 'partial', 'is_public',
            'organizations', 'classes', 'multiple_select', 'shuffle_options',
            'options', 'correct_options',
        ]

    def _visible_template_organizations(self):
        user = self.request.user
        if not user.is_authenticated or not user.has_perm('judge.create_private_problem'):
            return Organization.objects.none()
        if user.has_perm('judge.edit_all_organization'):
            return Organization.objects.order_by('name')
        return Organization.objects.filter(admins=user.profile).order_by('name')

    def _visible_template_classes(self):
        user = self.request.user
        if not user.is_authenticated or not user.has_perm('judge.create_private_problem'):
            return Class.objects.none()
        return Class.get_visible_classes(user).order_by('name')

    def _mcq_template_rows(self):
        org = self._visible_template_organizations().first()
        klass = self._visible_template_classes().first()
        return [
            self._mcq_excel_headers(),
            [
                'Sample MCQ',
                'What is 2 + 2?',
                '5',
                'false',
                'false',
                org.slug if org else '',
                klass.slug if klass else '',
                'false',
                'false',
                '3 | 4 | 5',
                '2',
            ],
        ]

    def _mcq_reference_rows(self):
        org_names = ' | '.join(
            self._visible_template_organizations().values_list('slug', flat=True)[:5],
        ) or 'org-slug'
        class_names = ' | '.join(
            self._visible_template_classes().values_list('slug', flat=True)[:5],
        ) or 'class-slug'
        return [
            ['field', 'required', 'how_to_fill', 'example'],
            ['name', 'yes', 'Question name.', 'Binary Search Quiz'],
            ['description', 'yes', 'Question prompt in markdown/plain text.', 'Which answer is correct?'],
            ['points', 'no', 'Numeric points. Blank uses the default form value.', '10'],
            ['partial', 'no', 'true/false. Usually useful for multiple-answer questions.', 'true'],
            ['is_public', 'no', 'true/false.', 'false'],
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
            ['multiple_select', 'no', 'true/false for allowing multiple correct answers.', 'true'],
            ['shuffle_options', 'no', 'true/false for per-student shuffle.', 'false'],
            ['options', 'yes', 'Separate choices with "|" or "," or new lines.', 'A | B | C'],
            ['correct_options', 'yes', '1-based option indexes separated with "|" or ",".', '2 | 3'],
        ]

    def download_excel_template(self, request):
        if not request.user.has_perm('judge.create_private_problem'):
            raise Http404()
        payload = build_xlsx_workbook([
            ('Problems', self._mcq_template_rows()),
            ('Reference', self._mcq_reference_rows()),
        ])
        response = HttpResponse(
            payload,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = 'attachment; filename="mcq_batch_upload_template.xlsx"'
        return response

    def batch_create_excel(self, request):
        if not request.user.has_perm('judge.create_private_problem'):
            raise Http404()
        uploaded_file = request.FILES.get('excel_file')
        if uploaded_file is None:
            return JsonResponse({'ok': False, 'message': _('Please choose an Excel file.')}, status=400)
        try:
            rows = read_xlsx_rows(uploaded_file)
        except zipfile.BadZipFile:
            return JsonResponse({'ok': False, 'message': _('The Excel file is invalid.')}, status=400)
        except (ET.ParseError, KeyError):
            return JsonResponse({'ok': False, 'message': _('The Excel file is invalid.')}, status=400)
        except ValueError as error:
            return JsonResponse({'ok': False, 'message': _(str(error))}, status=400)

        if not rows:
            return JsonResponse({'ok': False, 'message': _('The Excel file is empty.')}, status=400)

        header_row = [str(cell).strip().lower() for cell in rows[0]]
        required_headers = self._mcq_excel_headers()
        missing_headers = [header for header in required_headers if header not in header_row]
        if missing_headers:
            return JsonResponse({
                'ok': False,
                'message': _('Missing required columns: %(columns)s.') % {'columns': ', '.join(missing_headers)},
            }, status=400)
        header_map = {header: header_row.index(header) for header in required_headers}

        created = []
        try:
            with transaction.atomic():
                for row_number, row in enumerate(rows[1:], start=2):
                    problem = self._create_problem_from_excel_row(row, row_number, header_map)
                    if problem is None:
                        continue
                    created.append({
                        'id': problem.pk,
                        'code': problem.code,
                        'name': problem.name,
                        'edit_url': reverse('problem_mcq_edit', args=[problem.code]),
                    })
        except forms.ValidationError as error:
            return JsonResponse({'ok': False, 'message': error.message}, status=400)

        if not created:
            return JsonResponse(
                {'ok': False, 'message': _('The Excel file does not contain any problem rows.')},
                status=400,
            )

        return JsonResponse({
            'ok': True,
            'message': _(
                'Created %(count)d multiple-choice problems from Excel.',
            ) % {'count': len(created)},
            'created': created,
            'redirect_url': reverse('problem_list'),
        })

    def _create_problem_from_excel_row(self, row, row_number, header_map):
        values = {}
        for key, index in header_map.items():
            values[key] = row[index].strip() if index < len(row) and row[index] is not None else ''
        if not any(values.values()):
            return None
        if not values['name']:
            raise forms.ValidationError(_('Row %(row)d is missing "name".') % {'row': row_number})
        if not values['description']:
            raise forms.ValidationError(_('Row %(row)d is missing "description".') % {'row': row_number})

        option_texts = self._split_multi_value(values['options'])
        if len(option_texts) < 2:
            raise forms.ValidationError(_('Row %(row)d must contain at least two options.') % {'row': row_number})

        correct_indexes = []
        for token in self._split_multi_value(values['correct_options']):
            if not token.isdigit():
                raise forms.ValidationError(_('Row %(row)d has invalid correct option index "%(value)s".') % {
                    'row': row_number,
                    'value': token,
                })
            index = int(token)
            if index < 1 or index > len(option_texts):
                raise forms.ValidationError(_('Row %(row)d correct option index %(value)s is out of range.') % {
                    'row': row_number,
                    'value': index,
                })
            if index not in correct_indexes:
                correct_indexes.append(index)
        if not correct_indexes:
            raise forms.ValidationError(_('Row %(row)d must mark at least one correct option.') % {'row': row_number})

        multiple_select = self._parse_bool(values['multiple_select'])
        if not multiple_select and len(correct_indexes) > 1:
            raise forms.ValidationError(
                _('Row %(row)d is single-answer but marks multiple correct options.') % {'row': row_number},
            )

        organizations = self._resolve_organizations(values['organizations'])
        classes = self._resolve_classes(values['classes'])
        if classes and not organizations:
            raise forms.ValidationError(_('Row %(row)d selects classes without organizations.') % {'row': row_number})
        if classes:
            org_ids = {org.pk for org in organizations}
            if any(item.organization_id not in org_ids for item in classes):
                raise forms.ValidationError(
                    _('Row %(row)d selects classes outside the chosen organizations.') % {'row': row_number},
                )

        is_public = self._parse_bool(values['is_public'])
        if is_public and not self.request.user.has_perm('judge.change_public_visibility'):
            if not organizations and not classes:
                raise forms.ValidationError(
                    _('Row %(row)d cannot make a problem public without organizations.') % {'row': row_number},
                )
        points = self._parse_float(values['points'], _('points'), row_number)

        with transaction.atomic():
            problem = Problem(
                name=values['name'],
                description=values['description'],
                submission_type=ProblemSubmissionType.MCQ,
                points=10 if points is None else points,
                partial=self._parse_bool(values['partial']),
                is_public=is_public,
            )
            apply_simple_problem_defaults(problem)
            problem.save()
            problem.organizations.set(organizations)
            problem.classes.set(classes)
            if organizations or classes:
                problem.is_organization_private = True
                problem.save(update_fields=['is_organization_private'])
            if self.request.user.is_authenticated:
                problem.authors.add(self.request.user.profile)
            problem.types.add(default_problem_type())

            ProblemMCQConfig.objects.create(
                problem=problem,
                multiple_select=multiple_select,
                shuffle_options=self._parse_bool(values['shuffle_options']),
            )
            problem.partial = self._parse_bool(values['partial'])
            if points is not None:
                problem.points = points
            problem.save(update_fields=['partial', 'points', 'is_public', 'is_organization_private'])
            for index, text in enumerate(option_texts):
                ProblemMCQOption.objects.create(
                    problem=problem,
                    order=index,
                    text=text,
                    is_correct=(index + 1) in correct_indexes,
                )
            return problem

    def get_problem(self):
        """Return the problem being edited, or None in create mode. Enforces permissions."""
        code = self.kwargs.get('problem')
        if code is None:
            if not self.request.user.has_perm('judge.create_private_problem'):
                raise Http404()
            return None
        problem = get_object_or_404(Problem, code=code)
        # Deliberately NOT applying the is_manually_managed 404 from ProblemManagerMixin: that
        # guard is a code-judge concept and irrelevant to MCQ.
        if not (self.request.user.is_superuser or problem.is_editable_by(self.request.user)):
            raise Http404()
        if problem.submission_type != ProblemSubmissionType.MCQ:
            raise Http404()
        return problem

    def build_forms(self, problem, data=None):
        creating = problem is None
        config = None if creating else _get_or_none(problem)
        # Default a brand-new MCQ to the creator's organizations (preserves the old auto-scope
        # behaviour when the teacher leaves the selector untouched); the field stays editable.
        initial = None
        if creating and self.request.user.is_authenticated:
            initial = {'organizations': list(self.request.profile.organizations.all())}
        problem_form = ProblemMCQForm(data, instance=problem, initial=initial, user=self.request.user)
        config_form = MCQConfigForm(data, instance=config)
        # On edit, start with no blank trailing row; the "Add choice" button adds rows on demand.
        OptionFormSet = make_option_formset(extra=4 if creating else 0)
        formset = OptionFormSet(data, instance=problem if problem is not None else Problem(), prefix='options')
        return problem_form, config_form, formset

    def get(self, request, *args, **kwargs):
        if self.kwargs.get('problem') is None and request.GET.get('download_excel_template') == '1':
            return self.download_excel_template(request)
        problem = self.get_problem()
        problem_form, config_form, formset = self.build_forms(problem)
        return self.render(request, problem, problem_form, config_form, formset)

    def post(self, request, *args, **kwargs):
        if self.kwargs.get('problem') is None and request.FILES.get('excel_file') is not None:
            return self.batch_create_excel(request)
        problem = self.get_problem()
        creating = problem is None
        problem_form, config_form, formset = self.build_forms(problem, data=request.POST)

        forms_valid = problem_form.is_valid() & config_form.is_valid() & formset.is_valid()
        option_error = self.validate_options(formset, config_form) if forms_valid else None

        if forms_valid and option_error is None:
            with transaction.atomic():
                problem = problem_form.save(commit=False)
                if creating:
                    problem.submission_type = ProblemSubmissionType.MCQ
                    apply_simple_problem_defaults(problem)
                problem.save()
                problem_form.save_m2m()
                if creating:
                    problem.types.add(default_problem_type())
                    problem.authors.add(request.profile)
                # Visibility is inherited from the org selector: org-private iff orgs are chosen.
                problem.is_organization_private = problem.organizations.exists()
                problem.save(update_fields=['is_organization_private'])

                config = config_form.save(commit=False)
                config.problem = problem
                config.save()

                self._save_options(formset, problem)

            if creating:
                # Land the teacher on the new problem itself.
                return redirect('problem_detail', problem.code)
            messages.success(request, _('Saved “%s”.') % problem.name)
            return redirect('problem_mcq_edit', problem.code)

        return self.render(request, problem, problem_form, config_form, formset, option_error=option_error)

    def validate_options(self, formset, config_form):
        rows = [f for f in formset.forms
                if f.cleaned_data and not f.cleaned_data.get('DELETE') and f.cleaned_data.get('text')]
        if len(rows) < 2:
            return _('Add at least two choices.')
        correct = [f for f in rows if f.cleaned_data.get('is_correct')]
        if not correct:
            return _('Mark at least one choice as correct.')
        multiple = config_form.cleaned_data.get('multiple_select')
        if not multiple and len(correct) > 1:
            return _('Single-answer questions must have exactly one correct choice.')
        deleted = [f.instance for f in formset.forms
                   if f.instance.pk and f.cleaned_data and f.cleaned_data.get('DELETE')]
        if deleted:
            # Stored answers reference options by id; deleting a chosen one breaks them.
            used = selected_option_ids(formset.instance)
            blocked = [o for o in deleted if o.pk in used]
            if blocked:
                return _('Choice "%(text)s" was already selected in student submissions and cannot be '
                         'deleted. Edit its text or correctness instead.') % {'text': blocked[0].text[:60]}
        return None

    def _save_options(self, formset, problem):
        # Re-sequence order from the (drag-maintained) hidden order field, then save.
        formset.instance = problem
        formset.save(commit=False)
        for obj in formset.deleted_objects:
            obj.delete()
        kept = [f for f in formset.forms
                if f.cleaned_data and not f.cleaned_data.get('DELETE') and f.cleaned_data.get('text')]
        kept.sort(key=lambda f: f.cleaned_data.get('order') or 0)
        for index, f in enumerate(kept):
            option = f.instance
            option.problem = problem
            option.order = index
            option.save()

    def render(self, request, problem, problem_form, config_form, formset, option_error=None):
        page_title = _('New multiple-choice question') if problem is None else _('Edit %s') % problem.name
        return render(request, self.template_name, {
            'problem': problem,
            'creating': problem is None,
            'form': problem_form,
            'config_form': config_form,
            'formset': formset,
            'option_error': option_error,
            'title': page_title,
            'content_title': page_title,
        })


def _get_or_none(problem):
    try:
        return problem.mcq_config
    except ProblemMCQConfig.DoesNotExist:
        return None
