"""Front-end create + edit flow for fill-in-the-blank problems.

Mirrors ``judge/views/mcq.py``: a single view drives both creating and editing, reusing the same
problem perms (``judge.create_private_problem`` to create, ``Problem.is_editable_by`` to edit) and
``apply_simple_problem_defaults`` so teachers only ever see the prompt, blanks, and settings.
"""
from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.forms import inlineformset_factory
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.translation import gettext as _
from django.views.generic import View

from judge.models import Problem, ProblemBlank, ProblemFillBlankConfig, ProblemSubmissionType
from judge.utils.problem_create import apply_simple_problem_defaults, default_problem_type
from judge.widgets import HeavySelect2MultipleWidget, MartorWidget


class ProblemFillBlankForm(forms.ModelForm):
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
            'partial': _('Allow partial credit (score each blank independently)'),
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


class FillBlankConfigForm(forms.ModelForm):
    class Meta:
        model = ProblemFillBlankConfig
        fields = ['case_sensitive', 'trim_whitespace']


class BlankForm(forms.ModelForm):
    class Meta:
        model = ProblemBlank
        fields = ['label', 'answers', 'order']
        widgets = {
            'order': forms.HiddenInput(attrs={'class': 'blank-order'}),
            'label': forms.TextInput(attrs={'class': 'blank-label'}),
            'answers': forms.Textarea(attrs={'rows': 2, 'class': 'blank-answers'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Answers are optional at the field level so that clearing an existing blank's answers is a
        # valid edit (it removes the blank in _save_blanks) rather than a required-field error, and
        # so a trailing empty row can just be ignored. validate_blanks still enforces >= 1 non-empty
        # blank overall.
        self.fields['answers'].required = False


def make_blank_formset(extra):
    return inlineformset_factory(
        Problem, ProblemBlank, form=BlankForm, extra=extra, can_delete=True)


class ProblemFillBlankEditView(LoginRequiredMixin, View):
    template_name = 'problem/fill_blank_edit.html'

    def get_problem(self):
        """Return the problem being edited, or None in create mode. Enforces permissions."""
        code = self.kwargs.get('problem')
        if code is None:
            if not self.request.user.has_perm('judge.create_private_problem'):
                raise Http404()
            return None
        problem = get_object_or_404(Problem, code=code)
        if not (self.request.user.is_superuser or problem.is_editable_by(self.request.user)):
            raise Http404()
        if problem.submission_type != ProblemSubmissionType.FILL_BLANK:
            raise Http404()
        return problem

    def build_forms(self, problem, data=None):
        creating = problem is None
        config = None if creating else _get_or_none(problem)
        # Default a brand-new problem to the creator's organizations (field stays editable).
        initial = None
        if creating and self.request.user.is_authenticated:
            initial = {'organizations': list(self.request.profile.organizations.all())}
        problem_form = ProblemFillBlankForm(data, instance=problem, initial=initial, user=self.request.user)
        config_form = FillBlankConfigForm(data, instance=config)
        # On edit, start with no blank trailing row; the "Add blank" button adds rows on demand.
        BlankFormSet = make_blank_formset(extra=2 if creating else 0)
        formset = BlankFormSet(data, instance=problem if problem is not None else Problem(), prefix='blanks')
        return problem_form, config_form, formset

    def get(self, request, *args, **kwargs):
        problem = self.get_problem()
        problem_form, config_form, formset = self.build_forms(problem)
        return self.render(request, problem, problem_form, config_form, formset)

    def post(self, request, *args, **kwargs):
        problem = self.get_problem()
        creating = problem is None
        problem_form, config_form, formset = self.build_forms(problem, data=request.POST)

        forms_valid = problem_form.is_valid() & config_form.is_valid() & formset.is_valid()
        blank_error = self.validate_blanks(formset) if forms_valid else None

        if forms_valid and blank_error is None:
            with transaction.atomic():
                problem = problem_form.save(commit=False)
                if creating:
                    problem.submission_type = ProblemSubmissionType.FILL_BLANK
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

                self._save_blanks(formset, problem)

            if creating:
                return redirect('problem_detail', problem.code)
            messages.success(request, _('Saved “%s”.') % problem.name)
            return redirect('problem_fillblank_edit', problem.code)

        return self.render(request, problem, problem_form, config_form, formset, blank_error=blank_error)

    def validate_blanks(self, formset):
        rows = [f for f in formset.forms
                if f.cleaned_data and not f.cleaned_data.get('DELETE')
                and (f.cleaned_data.get('answers') or '').strip()]
        if not rows:
            return _('Add at least one blank with accepted answers.')
        return None

    def _save_blanks(self, formset, problem):
        # Re-sequence order from the (drag-maintained) hidden order field, then save.
        formset.instance = problem
        formset.save(commit=False)
        for obj in formset.deleted_objects:
            obj.delete()

        live_forms = [f for f in formset.forms if f.cleaned_data and not f.cleaned_data.get('DELETE')]
        kept = [f for f in live_forms if (f.cleaned_data.get('answers') or '').strip()]
        # An existing blank whose answers were cleared is treated as removed: delete it so a stale
        # record with the old accepted answers can't linger and keep affecting grading. A brand-new
        # empty row (no pk) is simply ignored.
        for f in live_forms:
            if f not in kept and f.instance.pk:
                f.instance.delete()

        kept.sort(key=lambda f: f.cleaned_data.get('order') or 0)
        for index, f in enumerate(kept):
            blank = f.instance
            blank.problem = problem
            blank.order = index
            blank.save()

    def render(self, request, problem, problem_form, config_form, formset, blank_error=None):
        page_title = _('New fill-in-the-blank question') if problem is None else _('Edit %s') % problem.name
        return render(request, self.template_name, {
            'problem': problem,
            'creating': problem is None,
            'form': problem_form,
            'config_form': config_form,
            'formset': formset,
            'blank_error': blank_error,
            'title': page_title,
            'content_title': page_title,
        })


def _get_or_none(problem):
    try:
        return problem.fill_blank_config
    except ProblemFillBlankConfig.DoesNotExist:
        return None
