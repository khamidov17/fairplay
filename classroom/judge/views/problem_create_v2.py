"""Unified front-end "Add problem" page (LeetCode-style redesign).

A single standalone page (see templates/problem/create_v2.html) lets an authorized user author a
problem of several question types. It reuses the existing backend primitives:

* Code problems  -> Problem(submission_type=CODE) + ProblemData / ProblemTestCase written to
  ``problem_data_storage`` and compiled into init.yml (so test cases can be entered at add time).
* Single/Multiple -> Problem(submission_type=MCQ) + ProblemMCQConfig + ProblemMCQOption
  (mirrors judge/views/mcq.py).

Other question types from the design (fill-in-blank, true/false, short answer, compound, other)
have no backend grader yet; the page shows them but this view refuses to save them
(``NOT_IMPLEMENTED_TYPES``) until their models/graders land.

The page talks to this view with JSON POSTs for most saves. Programming problems that carry a
ZIP of test data use ``multipart/form-data`` (field ``payload`` = JSON metadata, field
``testcase_zip`` = the archive) so large packages share the same upload path and size limits as
the classic admin test_data page. Responses are JSON ``{ok, redirect}`` or ``{error}``.
"""
import json
import os
import re
from datetime import datetime
from zipfile import BadZipfile, ZipFile

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.decorators import method_decorator
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import View
from reversion import revisions

from judge.models import (Class, Language, LanguageLimit, License, Organization, Problem, ProblemBlank,
                          ProblemClarification, ProblemCodeConfig, ProblemFileConfig, ProblemFillBlankConfig,
                          ProblemGroup, ProblemHarness, ProblemMCQConfig, ProblemMCQOption, ProblemShortAnswerConfig,
                          ProblemSubmissionType, ProblemTemplate, ProblemTranslation, ProblemType, Profile,
                          Solution, SubmissionSourceAccess)
from judge.models.problem_data import CHECKERS
from judge.models.question import GRADE_AI, GRADE_EXACT
from judge.question_types.mcq import sync_mcq_options
from judge.utils.problem_create import (apply_simple_problem_defaults, default_problem_group, default_problem_type,
                                        generate_problem_code, log_problem_admin_entry,
                                        problem_admin_changed_fields, problem_admin_snapshot)
from judge.utils.safe_json import script_json
from judge.utils.unique_ids import wants_auto_series
from judge.utils.user_search import profile_choice_payload, profile_choice_payloads

# Types the redesigned page offers but the backend cannot grade/store yet.
NOT_IMPLEMENTED_TYPES = {'compound', 'other'}


class ProblemAlreadySaved(Exception):
    """The submitted code belongs to a problem this user already authored."""

    def __init__(self, problem):
        self.problem = problem
        super().__init__(problem.code)


# Difficulty ProblemType.full_name defaults when attaching the standard easy/medium/hard row.
_DIFFICULTY_FULL_NAMES = {
    'easy': 'Easy',
    'medium': 'Medium',
    'hard': 'Hard',
}

# Controlled vocabulary of algorithm / solution tags the AI tag suggester may propose.
# The AI must pick ONLY from these (plus existing knowledge tags in the DB); it may not invent
# new labels or echo answer-format / difficulty chips.  Kept deliberately short and conventional
# so the tag space stays uniform across problems instead of drifting with each model response.
_KNOWLEDGE_TAG_VOCABULARY = (
    'dp', 'dfs', 'bfs', 'greedy', 'math', 'graph', 'tree', 'string', 'sorting', 'binary search',
    'two pointers', 'hash map', 'stack', 'queue', 'heap', 'recursion', 'backtracking',
    'divide and conquer', 'dynamic programming', 'number theory', 'combinatorics', 'geometry',
    'polynomial', 'linear algebra', 'probability', 'simulation', 'prefix sum', 'bit manipulation',
    'shortest path', 'minimum spanning tree', 'topological sort', 'union find', 'segment tree',
    'sliding window', 'linked list', 'array', 'matrix', 'binary tree', 'search', 'intervals',
)


def _problem_type_code(text):
    code = re.sub(r'[^\w\-]+', '-', (text or '').strip().lower())[:20].strip('-')
    return code or 'type'


def _reserved_auto_labels():
    """Format/difficulty auto-chip labels that must not be stored as knowledge tags."""
    return Problem.reserved_auto_type_labels()


def _knowledge_type_labels():
    """Display labels of existing knowledge ProblemTypes (format/difficulty chips excluded).

    This is the authoritative candidate set for AI tag suggestions: answer-format rows
    (``programming``, ``fill_in_blank``, ``short-answer``, difficulty levels, translated chip
    labels) are filtered out so the model can never return a question-type tag.
    """
    reserved = _reserved_auto_labels()
    labels = []
    for t in ProblemType.objects.order_by('full_name', 'name').values('name', 'full_name'):
        ptype = ProblemType(name=t['name'], full_name=t['full_name'])
        if Problem.is_knowledge_problem_type(ptype, reserved_labels=reserved):
            labels.append(t['full_name'] or t['name'])
    return labels


def _tag_suggestion_candidates():
    """Merge DB knowledge tags with the controlled vocabulary (deduped, ordered)."""
    existing = _knowledge_type_labels()
    seen = set()
    out = []
    for label in list(existing) + list(_KNOWLEDGE_TAG_VOCABULARY):
        key = (label or '').casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(label)
    return out


def _normalize_tag_label(label):
    """Return the canonical display form of a suggested tag, or None if not acceptable.

    Rejects answer-format / difficulty chips (``fill_in_blank``, ``Programming``, ``Easy`` …),
    arbitrary new labels outside the vocabulary, and anything longer than 40 chars — the model
    may only choose from the curated candidate set, keeping the tag space uniform.
    """
    text = (label or '').strip().strip('.').strip()
    if not text or len(text) > 100:
        return None
    for candidate in _tag_suggestion_candidates():
        if text.casefold() == candidate.casefold():
            return candidate
    return None


def _can_create_any(user):
    """The page is visible to anyone who can create at least one supported problem type."""
    return user.has_perm('judge.create_private_problem') or (user.is_staff and user.has_perm('judge.add_problem'))


def assignable_organizations(user, problem=None):
    """Courses ``user`` may attach a problem to: the ones they teach, plus any it is already in.

    "Teach" means course admin, course instructor or admin of a class in the course
    (``Organization.profile_teaches_course``); ``edit_all_organization`` allows every course.
    """
    if not user.is_authenticated or not user.has_perm('judge.create_private_problem'):
        return Organization.objects.none()
    if user.has_perm('judge.edit_all_organization'):
        return Organization.objects.all()
    profile = user.profile
    q = Q(admins=profile) | Q(instructors=profile) | Q(**{'class__admins': profile})
    if problem is not None and problem.pk is not None:
        q |= Q(id__in=problem.organizations.values('id'))
    return Organization.objects.filter(q).distinct()


def _normalized_text(value):
    return str(value or '').replace('\r\n', '\n').replace('\r', '\n').strip()


def _can_create_code(user):
    """Programming problems: course instructors (create_private_problem) or site add_problem."""
    return _can_create_any(user)


@method_decorator(ensure_csrf_cookie, name='dispatch')
class ProblemCreateV2View(LoginRequiredMixin, View):
    template_name = 'problem/create_v2.html'

    # ------------------------------------------------------------------ GET
    def get(self, request, *args, **kwargs):
        if not _can_create_any(request.user):
            raise Http404()
        context = self.get_context(request)
        edit_code = kwargs.get('problem')
        if edit_code:
            try:
                problem = self._get_editable_problem(request, edit_code)
            except PermissionError:
                raise Http404()
            if problem is None:
                raise Http404()
            context['title'] = _('Edit problem')
            context['edit_code'] = problem.code
            context['edit_name'] = problem.name
            context['organizations'] = list(
                assignable_organizations(request.user, problem).order_by('name').values('id', 'name'))
            context['edit_data_json'] = script_json(self._build_edit_data(problem))
            context['test_data_url'] = reverse('problem_data', args=[problem.code])
            context['edit_detail_url'] = reverse('problem_detail', args=[problem.code])
            if (
                request.user.has_perm('judge.clone_problem')
                or problem.is_viewable_by_org_admin(request.user)
            ):
                context['can_clone_problem'] = True
                context['clone_url'] = reverse('problem_clone', args=[problem.code])
        else:
            context['suggested_code'] = generate_problem_code()
        return render(request, self.template_name, context)

    def get_context(self, request):
        user = request.user
        profile = request.profile
        # Only courses the user teaches are offered (and accepted on save, see _payload_organizations).
        orgs = list(assignable_organizations(user).order_by('name').values('id', 'name'))
        can_edit_organizations = user.has_perm('judge.create_private_problem')
        return {
            'title': _('Add problem'),
            'organizations': orgs,
            'groups': list(ProblemGroup.objects.order_by('full_name', 'name').values('id', 'full_name', 'name')),
            'languages': list(Language.objects.order_by('name', 'key').values('id', 'key', 'name')),
            'languages_json': script_json([
                {'key': lang['key'], 'name': lang['name']}
                for lang in Language.objects.order_by('name', 'key').values('key', 'name')
            ]),
            'problem_types': list(ProblemType.objects.order_by('full_name', 'name').values('id', 'full_name', 'name')),
            'problem_types_json': script_json([
                {'name': t['name'], 'full': t['full_name'] or t['name']}
                for t in ProblemType.objects.order_by('full_name', 'name').values('name', 'full_name')
            ]),
            'can_create_code': _can_create_code(user),
            'can_create_mcq': user.has_perm('judge.create_private_problem'),
            'can_create_file': user.has_perm('judge.create_private_problem'),
            'can_import_excel_problems': user.has_perm('judge.create_private_problem'),
            # Match ProblemAdmin.get_readonly_fields on main: is_public is editable with
            # create_private_problem or change_public_visibility; save still requires an
            # organization unless the user has change_public_visibility.
            'can_change_public': (
                can_edit_organizations or user.has_perm('judge.change_public_visibility')
            ),
            'can_change_manually_managed': user.has_perm('judge.change_manually_managed'),
            'can_full_markup': user.has_perm('judge.problem_full_markup'),
            'default_group_id': default_problem_group().id,
            'suggested_code': '',
            'save_url': reverse('problem_create_v2'),
            'ai_difficulty_url': reverse('problem_create_v2_ai_difficulty'),
            'create_problem_type_url': reverse('problem_create_v2_problem_type'),
            'create_problem_group_url': reverse('problem_create_v2_problem_group'),
            'create_organization_url': reverse('problem_create_v2_organization'),
            'can_create_organization': can_edit_organizations,
            'harness_languages_json': script_json([
                {'id': lang.id, 'key': lang.key, 'name': lang.name, 'ace': lang.ace}
                for lang in Language.objects.filter(
                    common_name__in=['Java', 'Python'],
                ).order_by('common_name', 'key')
            ]),
            'ai_problem_type_url': reverse('problem_create_v2_ai_problem_type'),
            'ai_decompose_url': reverse('problem_create_v2_ai_decompose'),
            'problem_list_url': reverse('problem_list'),
            'problem_preview_url': reverse('problem_preview'),
            'problem_preview_batch_url': reverse('problem_preview_batch'),
            'image_upload_url': reverse('martor_image_uploader'),
            'file_upload_url': reverse('editor_file_uploader'),
            'search_users_url': reverse('martor_search_user'),
            'profile_select2_url': reverse('profile_select2'),
            'class_select2_url': reverse('class_select2'),
            # Every *_json value goes into <script> with |safe: script_json escapes <>& and U+2028/9.
            'current_profile_json': script_json(profile_choice_payload(profile)),
            'licenses': list(License.objects.order_by('name').values('id', 'key', 'name', 'display')),
            'ui_languages': list(settings.LANGUAGES),
            'edit_code': '',
            'edit_data_json': 'null',
            'test_data_url': '',
            'edit_detail_url': '',
            'can_clone_problem': False,
            'clone_url': '',
            'import_format_url': '',
            'import_create_url': '',
            'import_data_needs_review': False,
            'import_data_load_error': '',
            'import_config_needs_review': False,
            'import_unrecognized_configurations': [],
            'checkers': [{'value': value, 'label': str(label)} for value, label in CHECKERS],
            'checkers_json': script_json([
                {'value': value, 'label': str(label)} for value, label in CHECKERS
            ]),
        }

    # ------------------------------------------------------------------ POST
    def _parse_save_request(self, request):
        """Return ``(payload_dict, uploaded_zip_or_None)`` from JSON or multipart save posts."""
        content_type = (request.content_type or '').split(';')[0].strip().lower()
        if content_type == 'multipart/form-data':
            raw = request.POST.get('payload')
            if raw is None:
                raise ValueError(_('Invalid request.'))
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                raise ValueError(_('Invalid request.'))
            return payload, request.FILES.get('testcase_zip')
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise ValueError(_('Invalid request.'))
        return payload, None

    def post(self, request, *args, **kwargs):
        if not _can_create_any(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        try:
            payload, zip_upload = self._parse_save_request(request)
        except ValueError as e:
            return JsonResponse({'error': str(e)}, status=400)

        qtype = payload.get('type')
        name = (payload.get('name') or '').strip()
        if not name:
            return JsonResponse({'error': _('Please enter a problem name.')}, status=400)
        if qtype in NOT_IMPLEMENTED_TYPES:
            return JsonResponse({'error': _('This question type is under development and cannot be saved yet.')},
                                status=400)

        # When ``edit_code`` is present we update the existing problem in place (keeping its code/URL)
        # instead of creating a new one; this powers "Save and continue editing".
        # Prefer body ``edit_code``; fall back to the URL kwarg used by problem_edit_v2.
        existing = None
        edit_code = (payload.get('edit_code') or kwargs.get('problem') or '').strip()
        if edit_code:
            payload['edit_code'] = edit_code
            try:
                existing = self._get_editable_problem(request, edit_code)
            except PermissionError as e:
                return JsonResponse({'error': str(e)}, status=403)
            if existing is None:
                return JsonResponse({'error': _('Problem not found.')}, status=404)

        if existing is not None:
            try:
                self._check_full_markup_text(request.user, payload, existing)
            except PermissionError as e:
                return JsonResponse({'error': str(e)}, status=403)

        before = problem_admin_snapshot(existing) if existing is not None else None
        try:
            # Mirror classic VersionAdmin: store an optional edit-reason comment on the revision.
            with revisions.create_revision(atomic=True):
                if qtype == 'programming':
                    problem = self._create_code(request, payload, name, problem=existing,
                                                zip_upload=zip_upload)
                elif qtype in ('single', 'multiple'):
                    problem = self._create_mcq(request, payload, name, multiple=(qtype == 'multiple'),
                                               problem=existing)
                elif qtype == 'truefalse':
                    problem = self._create_truefalse(request, payload, name, problem=existing)
                elif qtype == 'blank':
                    problem = self._create_fill_blank(request, payload, name, problem=existing)
                elif qtype == 'short':
                    problem = self._create_short_answer(request, payload, name, problem=existing)
                elif qtype == 'file':
                    problem = self._create_file_upload(request, payload, name, problem=existing)
                else:
                    return JsonResponse({'error': _('Unknown question type.')}, status=400)
                revisions.set_user(request.user)
                reason = (payload.get('edit_reason') or '').strip()
                if reason:
                    revisions.set_comment(reason)
                elif existing:
                    revisions.set_comment(_('Edited from create_v2'))
                else:
                    revisions.set_comment(_('Created from create_v2'))
        except ProblemAlreadySaved as e:
            problem = e.problem
            return JsonResponse({
                'ok': True,
                'already_saved': True,
                'id': problem.id,
                'code': problem.code,
                'name': problem.name,
                'points': problem.points,
                'redirect': reverse('problem_edit_v2', args=[problem.code]),
                'message': _('This problem code is already saved. Opening the editor.'),
            })
        except PermissionError as e:
            return JsonResponse({'error': str(e)}, status=403)
        except ValueError as e:
            return JsonResponse({'error': str(e)}, status=400)
        except BadZipfile:
            return JsonResponse({'error': _('Your zip file is invalid!')}, status=400)

        # Same django.contrib.admin LogEntry path as classic ProblemAdmin saves.
        log_problem_admin_entry(
            request, problem,
            created=existing is None,
            changed_fields=None if existing is None else problem_admin_changed_fields(before, problem),
            message=(payload.get('edit_reason') or '').strip(),
        )

        action = payload.get('action') or 'save'
        if action == 'another':
            # Land on a fresh add-problem page.
            redirect = reverse('problem_create_v2')
        elif action == 'continue':
            # Reopen this problem on the add page for further edits.
            redirect = reverse('problem_edit_v2', args=[problem.code])
        else:
            redirect = reverse('problem_detail', args=[problem.code])
        body = {
            'ok': True,
            'id': problem.id,
            'code': problem.code,
            'name': problem.name,
            'format': problem.format_filter_code,
            'points': problem.points,
            'redirect': redirect,
        }
        submitted_code = (payload.get('code') or '').strip()
        if existing is None and submitted_code and submitted_code != problem.code:
            body['id_changed'] = True
            body['id_changed_from'] = submitted_code
            body['message'] = _('Saved with code %(new)s because %(old)s was already in use.') % {
                'new': problem.code,
                'old': submitted_code,
            }
        return JsonResponse(body)

    # ------------------------------------------------------------------ edit support
    def _get_editable_problem(self, request, code):
        problem = Problem.objects.filter(code=code).first()
        if problem is None:
            return None
        profile = request.profile
        can_edit = problem.is_editable_by(request.user) or (
            profile is not None and problem.authors.filter(id=profile.id).exists())
        if not can_edit:
            raise PermissionError(_('You do not have permission to edit this problem.'))
        return problem

    def _check_full_markup_text(self, user, payload, problem):
        """Mirror ProblemAdmin: without ``problem_full_markup`` the statement and the translations of a
        full-markup problem are read-only, because full markup is rendered without sanitizing."""
        if not problem.is_full_markup or user.has_perm('judge.problem_full_markup'):
            return
        message = _('You do not have permission to edit the statement of a full markup problem.')
        if _normalized_text(payload.get('description')) != _normalized_text(problem.description):
            raise PermissionError(message)
        if 'translations' in payload:
            submitted = {}
            for row in payload.get('translations') or []:
                if not isinstance(row, dict):
                    raise PermissionError(message)
                name = _normalized_text(row.get('name'))
                description = _normalized_text(row.get('description'))
                if not name and not description:
                    continue
                submitted[_normalized_text(row.get('language'))] = (name or problem.name, description)
            current = {
                translation.language: (_normalized_text(translation.name) or problem.name,
                                       _normalized_text(translation.description))
                for translation in problem.translations.all()
            }
            if submitted != current:
                raise PermissionError(message)

    def _clear_type_children(self, problem, *, clear_cases=True, keep_mcq_options=False):
        """Remove every question-type-specific child before repopulating on edit (also handles a
        type switch, e.g. single -> programming).

        ``clear_cases`` is False when editing a code problem without a ``testcases`` payload so
        cases managed on the embedded test_data page are not wiped accidentally.
        ``keep_mcq_options`` is True when an MCQ / true-false stays an MCQ: its options are then
        updated in place (``sync_mcq_options``) so stored answers keep their option ids.
        """
        if not keep_mcq_options:
            ProblemMCQOption.objects.filter(problem=problem).delete()
        ProblemMCQConfig.objects.filter(problem=problem).delete()
        ProblemBlank.objects.filter(problem=problem).delete()
        ProblemFillBlankConfig.objects.filter(problem=problem).delete()
        ProblemShortAnswerConfig.objects.filter(problem=problem).delete()
        ProblemCodeConfig.objects.filter(problem=problem).delete()
        # Only the per-problem upload settings go; SubmissionFile rows are student data and are
        # deliberately left alone.
        ProblemFileConfig.objects.filter(problem=problem).delete()
        if clear_cases:
            problem.cases.all().delete()
        # Called after submission_type is updated; drop harness when leaving programming.
        if problem.submission_type != ProblemSubmissionType.CODE:
            ProblemHarness.objects.filter(problem=problem).delete()

    def _build_edit_data(self, problem):
        """Serialize an existing problem into the shape the add-page JS expects (see collectData)."""
        st = problem.submission_type
        data = {
            'name': problem.name,
            'organizations': list(problem.organizations.values_list('id', flat=True)),
            'group_id': problem.group_id,
            'points': problem.points,
            'is_public': problem.is_public,
            'partial': problem.partial,
            'description': problem.description or '',
            'ai': False,
            'rubric': '',
        }

        # Knowledge chips only — difficulty / format / auto type labels are rebuilt by the UI.
        reserved = _reserved_auto_labels()
        type_tags = []
        seen_labels = set()
        for ptype in problem.types.all():
            if not Problem.is_knowledge_problem_type(ptype, reserved_labels=reserved):
                continue
            label = ptype.full_name or ptype.name
            key = (label or '').casefold()
            if not key or key in seen_labels:
                continue
            seen_labels.add(key)
            type_tags.append(label)
        data['problemTypes'] = type_tags
        data['difficulty'] = problem.difficulty or 'medium'

        if st == ProblemSubmissionType.CODE:
            data['type'] = 'programming'
            data['time_limit'] = problem.time_limit
            data['memory_limit'] = round((problem.memory_limit or 0) / 1024, 2)  # KB -> MB
            data['languages'] = list(problem.allowed_languages.values_list('key', flat=True))
            data['testcases'] = self._read_testcases(problem)
            code_config = getattr(problem, 'code_config', None)
            data['ai'] = bool(code_config and code_config.ai_enabled)
            data['rubric'] = (code_config.ai_rubric if code_config else '') or ''
            data['gradingTable'] = code_config.grading_table if code_config else None
            try:
                data['checker'] = problem.data_files.checker or ''
                data['zip_files'] = self._zip_member_names(problem.data_files.zipfile)
            except Exception:
                data['checker'] = ''
                data['zip_files'] = []
            harness = ProblemHarness.objects.filter(problem=problem).select_related('language').first()
            if harness is not None:
                data['harness'] = {
                    'language_id': harness.language_id,
                    'language': harness.language.key,
                    'harness_code': harness.harness_code or '',
                    'skip_precompile': harness.skip_precompile,
                    'run_student_main': harness.run_student_main,
                }
            else:
                data['harness'] = None
        elif st == ProblemSubmissionType.MCQ:
            config = getattr(problem, 'mcq_config', None)
            options = list(problem.mcq_options.order_by('order').values('id', 'text', 'is_correct'))
            if config is not None and config.is_true_false:
                data['type'] = 'truefalse'
                correct_true = next((o for o in options if o['is_correct']), None)
                data['tfAnswer'] = 'true' if (correct_true and correct_true is options[0]) else 'false'
            else:
                data['type'] = 'multiple' if (config and config.multiple_select) else 'single'
                data['multiple'] = bool(config and config.multiple_select)
                # ``id`` lets the editor send the option back so a save updates it in place.
                data['options'] = [{'id': o['id'], 'text': o['text'], 'correct': o['is_correct']} for o in options]
                if config and config.multiple_select:
                    data['mcqScoring'] = config.scoring_mode or ProblemMCQConfig.SCORE_PROPORTIONAL
                    data['mcqUnderselectPoints'] = config.underselect_points
        elif st == ProblemSubmissionType.FILL_BLANK:
            data['type'] = 'blank'
            config = getattr(problem, 'fill_blank_config', None)
            data['caseSensitive'] = bool(config and config.case_sensitive)
            data['trimWhitespace'] = bool(config.trim_whitespace) if config else True
            data['ai'] = bool(config and config.grading_mode == GRADE_AI)
            data['rubric'] = (config.ai_rubric if config else '') or ''
            data['gradingTable'] = config.grading_table if config else None
            # ProblemBlank stores accepted answers one-per-line and the create page now edits them
            # the same way (one accepted answer per line in a textarea), so pass them through as-is.
            data['blanks'] = [
                {'answers': blank.answers, 'points': blank.points}
                for blank in problem.blanks.order_by('order')
            ]
        elif st == ProblemSubmissionType.SHORT_ANSWER:
            data['type'] = 'short'
            config = getattr(problem, 'short_answer_config', None)
            data['shortAnswer'] = (config.reference_answer if config else '') or ''
            data['rubric'] = (config.ai_rubric if config else '') or ''
            data['ai'] = bool(config and config.ai_enabled)
            data['gradingTable'] = config.grading_table if config else None
        elif st == ProblemSubmissionType.FILE:
            data['type'] = 'file'
            config = getattr(problem, 'file_config', None)
            data['fileExtensions'] = (config.allowed_extensions if config else '') or ''
            # The model stores bytes; the page edits MB. Keep 3 decimals so a sub-MB limit set via
            # the API survives an edit round-trip instead of rounding down to zero.
            data['fileMaxSizeMb'] = round((config.max_file_size or 0) / (1024 * 1024), 3) if config else 20
            data['fileInstructions'] = (config.instructions if config else '') or ''
        else:
            data['type'] = 'single'

        data['authors'] = profile_choice_payloads(problem.authors)
        data['curators'] = profile_choice_payloads(problem.curators)
        data['testers'] = profile_choice_payloads(problem.testers)
        data['banned_users'] = profile_choice_payloads(problem.banned_users)
        data['classes'] = list(problem.classes.values('id', 'name'))
        data['license_id'] = problem.license_id or ''
        data['is_manually_managed'] = problem.is_manually_managed
        data['date'] = timezone.localtime(problem.date).strftime('%Y-%m-%d %H:%M') if problem.date else ''
        data['submission_source_visibility_mode'] = problem.submission_source_visibility_mode
        data['is_full_markup'] = problem.is_full_markup
        data['view_test_cases'] = problem.view_test_cases
        data['view_tester'] = problem.view_tester
        data['ai_hints_enabled'] = problem.ai_hints_enabled
        data['og_image'] = problem.og_image or ''
        data['summary'] = problem.summary or ''
        data['short_circuit'] = problem.short_circuit
        try:
            sol = problem.solution
        except Solution.DoesNotExist:
            sol = None
        if sol:
            data['solution'] = {
                'is_public': sol.is_public,
                'publish_on': timezone.localtime(sol.publish_on).strftime('%Y-%m-%d %H:%M') if sol.publish_on else '',
                'content': sol.content or '',
                'authors': profile_choice_payloads(sol.authors),
            }
        else:
            data['solution'] = {'is_public': False, 'publish_on': '', 'content': '', 'authors': []}
        data['translations'] = list(
            problem.translations.values('language', 'name', 'description'))
        data['language_limits'] = [
            {
                'language': lim.language.key,
                'time_limit': lim.time_limit,
                'memory_limit': lim.memory_limit,  # KB (same as classic admin)
            }
            for lim in problem.language_limits.select_related('language').order_by('language__key')
        ]
        data['templates'] = [
            {
                'language': tmpl.language.key,
                'code': tmpl.code or '',
            }
            for tmpl in problem.templates.select_related('language').order_by('language__key')
        ]
        data['clarifications'] = [
            {
                'id': clar.id,
                'description': clar.description or '',
                'date': timezone.localtime(clar.date).strftime('%Y-%m-%d %H:%M') if clar.date else '',
            }
            for clar in ProblemClarification.objects.filter(problem=problem).order_by('-date', '-id')
        ]
        return data

    def _read_testcases(self, problem):
        from judge.models import ProblemData, problem_data_storage

        zip_file = None
        zip_names = set()
        try:
            data = problem.data_files
        except ProblemData.DoesNotExist:
            data = None
        if data and data.zipfile:
            try:
                zip_file = ZipFile(data.zipfile.path, 'r')
                zip_names = set(zip_file.namelist())
            except (BadZipfile, OSError, ValueError):
                zip_file = None
                zip_names = set()

        def _read_loose(name):
            if not name:
                return ''
            try:
                with problem_data_storage.open('%s/%s' % (problem.code, name)) as fh:
                    return fh.read().decode('utf-8', 'replace')
            except Exception:
                return ''

        def _read_zip(name):
            if not name or not zip_file:
                return ''
            candidates = [name]
            if name not in zip_names:
                candidates.extend(n for n in zip_names if n.endswith('/' + name))
            for candidate in candidates:
                if candidate not in zip_names:
                    continue
                try:
                    return zip_file.read(candidate).decode('utf-8', 'replace')
                except Exception:
                    return ''
            return ''

        def _read(name):
            if not name:
                return ''
            loose_path = '%s/%s' % (problem.code, name)
            if problem_data_storage.exists(loose_path):
                return _read_loose(name)
            return _read_zip(name)

        try:
            cases = []
            for case in problem.cases.order_by('order'):
                # create-v2 only edits Normal cases; Batch start/end belong on test_data.
                if case.type in ('S', 'E'):
                    continue
                cases.append({
                    'input': _read(case.input_file),
                    'output': _read(case.output_file),
                    'input_file': case.input_file or '',
                    'output_file': case.output_file or '',
                    'points': case.points,
                    'pretest': case.is_pretest,
                    'checker': case.checker or '',
                })
            return cases
        finally:
            if zip_file is not None:
                zip_file.close()

    # ------------------------------------------------------------------ helpers
    def _num(self, value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _positive_or_none(self, value):
        """Parse a score input; empty / missing / non-positive means “use the even split”."""
        if value is None or value == '':
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _iter_blank_payload(self, payload):
        """Yield ``(answers_text, points)`` from create-v2 ``blanks`` (strings or objects)."""
        for item in payload.get('blanks') or []:
            if isinstance(item, dict):
                answers = item.get('answers') or item.get('text') or ''
                points = self._num(item.get('points'), 1)
            else:
                answers = item
                points = 1
            if isinstance(answers, (list, tuple)):
                answers = '\n'.join(str(part) for part in answers if part is not None and str(part).strip())
            elif not isinstance(answers, str):
                answers = str(answers) if answers is not None else ''
            answers = answers.strip()
            if not answers:
                continue
            yield answers, points if points > 0 else 1

    def _resolve_group(self, payload):
        gid = payload.get('group_id')
        if gid:
            group = ProblemGroup.objects.filter(pk=gid).first()
            if group:
                return group
        return default_problem_group()

    def _payload_organizations(self, payload, user, problem=None):
        """The courses named in ``payload``, all of which ``user`` must be allowed to assign."""
        ids = set()
        for value in payload.get('organizations') or []:
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                raise ValueError(_('Invalid course.'))
        organizations = list(assignable_organizations(user, problem).filter(pk__in=ids))
        if len(organizations) != len(ids):
            raise PermissionError(_('You can only add problems to courses you teach.'))
        return organizations

    def _resolve_public(self, user, is_public, payload, problem=None):
        """Mirror the admin/MCQ visibility rules; returns the effective is_public or raises."""
        if not is_public:
            return False
        if user.has_perm('judge.change_public_visibility'):
            return True
        # Decide on the courses that will really be attached (unknown ids do not count), like the admin
        # does with the saved ``is_organization_private``.
        if user.has_perm('judge.create_private_problem'):
            org_private = bool(self._payload_organizations(payload, user, problem))
        else:
            org_private = problem is not None and problem.pk is not None and problem.organizations.exists()
        if not org_private:
            raise PermissionError(_('You do not have permission to make a problem public unless it is '
                                    'private to an organization.'))
        if not user.has_perm('judge.create_private_problem'):
            raise PermissionError(_('You do not have permission to make this problem public.'))
        return True

    def _resolve_problem_type(self, label):
        """Match an existing ProblemType by name/full_name, or create one (AI / free-text tags)."""
        label = (label or '').strip()
        if not label:
            return None
        existing = ProblemType.objects.filter(Q(full_name=label) | Q(name=label)).first()
        if existing is None:
            existing = ProblemType.objects.filter(Q(full_name__iexact=label) | Q(name__iexact=label)).first()
        if existing is not None:
            return existing

        code = _problem_type_code(label)[:20]
        if ProblemType.objects.filter(name=code).exists():
            # Same slug, different display name — keep the code unique with a numeric suffix.
            base = code[:16]
            for i in range(2, 100):
                candidate = ('%s-%d' % (base, i))[:20]
                if not ProblemType.objects.filter(name=candidate).exists():
                    code = candidate
                    break
            else:
                return None

        return ProblemType.objects.create(name=code, full_name=label[:100])

    def _attach_types(self, problem, payload, is_create=False):
        """Persist knowledge tags + standard difficulty; drop auto type/difficulty chips.

        Distinguishes omitted vs explicit ``problemTypes`` so partial/legacy
        payloads that only send ``difficulty`` do not wipe knowledge tags:

        - key missing on edit → keep existing knowledge types; only update difficulty
        - key missing on create → reject; a new problem has no tags to keep
        - key present → replace knowledge types from payload; an empty list is rejected
        - ``difficulty`` omitted/invalid → keep any existing easy/medium/hard row
        """
        reserved = _reserved_auto_labels()
        resolved = []
        seen = set()

        if 'problemTypes' not in payload and is_create:
            raise ValueError(_('Please select a problem tag.'))

        if 'problemTypes' in payload:
            labels = [n for n in (payload.get('problemTypes') or []) if n and str(n).strip()]
            for label in labels:
                text = str(label).strip()
                if text.casefold() in reserved:
                    continue
                ptype = self._resolve_problem_type(text)
                if ptype is None or ptype.pk in seen:
                    continue
                if not Problem.is_knowledge_problem_type(ptype, reserved_labels=reserved):
                    continue
                seen.add(ptype.pk)
                resolved.append(ptype)
            # The editor always sends this key. Difficulty is stored separately,
            # so a payload with no knowledge tag must not save.
            if not resolved:
                raise ValueError(_('Please select a problem tag.'))
        else:
            for ptype in problem.types.all():
                if ptype.pk in seen:
                    continue
                if not Problem.is_knowledge_problem_type(ptype, reserved_labels=reserved):
                    continue
                seen.add(ptype.pk)
                resolved.append(ptype)

        diff_name = Problem.canonical_difficulty_name(payload.get('difficulty'))
        if diff_name in Problem.DIFFICULTY_TYPE_NAMES:
            diff_type = ProblemType.objects.filter(name__iexact=diff_name).first()
            if diff_type is None:
                diff_type = ProblemType.objects.filter(
                    full_name__iexact=_DIFFICULTY_FULL_NAMES[diff_name],
                ).first()
            if diff_type is None:
                diff_type, _created = ProblemType.objects.get_or_create(
                    name=diff_name,
                    defaults={'full_name': _DIFFICULTY_FULL_NAMES[diff_name]},
                )
            if diff_type.pk not in seen:
                resolved.append(diff_type)
                seen.add(diff_type.pk)
        else:
            # Older clients omit difficulty — keep any existing easy/medium/hard row.
            existing_diff = next(
                (ptype for ptype in problem.types.all() if Problem.is_difficulty_problem_type(ptype)),
                None,
            )
            if existing_diff is not None and existing_diff.pk not in seen:
                resolved.append(existing_diff)

        if resolved:
            problem.types.set(resolved)
        elif not problem.types.exists():
            problem.types.add(default_problem_type())

    def _attach_orgs(self, problem, payload, user):
        # Match ProblemAdmin.get_readonly_fields: organizations is read-only without
        # create_private_problem. Users who can edit the field may assign only courses they teach
        # (plus the ones the problem is already in).
        if not user.has_perm('judge.create_private_problem'):
            return
        problem.organizations.set(self._payload_organizations(payload, user, problem))

    def _parse_local_dt(self, value):
        text = (value or '').strip()
        if not text:
            return None
        dt = parse_datetime(text.replace(' ', 'T', 1) if 'T' not in text and ' ' in text else text)
        if dt is None:
            for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return dt

    def _profile_ids(self, raw):
        ids = []
        for item in raw or []:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                continue
        return ids

    def _apply_advanced(self, problem, payload, request, *, is_create=False):
        """Persist Advanced-section fields from create_v2 JSON payload."""
        user = request.user

        # Mirror admin get_readonly_fields: only privileged users may toggle these.
        if user.has_perm('judge.change_manually_managed'):
            problem.is_manually_managed = bool(payload.get('is_manually_managed'))
        elif is_create:
            problem.is_manually_managed = False
        elif bool(payload.get('is_manually_managed')) != bool(problem.is_manually_managed):
            raise PermissionError(_('You do not have permission to change the manually managed flag.'))

        if user.has_perm('judge.problem_full_markup'):
            problem.is_full_markup = bool(payload.get('is_full_markup'))
        elif is_create:
            problem.is_full_markup = False
        elif bool(payload.get('is_full_markup')) != bool(problem.is_full_markup):
            raise PermissionError(_('You do not have permission to change the full markup flag.'))

        # Remaining Advanced flags have no separate Django perms in classic admin
        # advanced mode; any user who can edit the problem may set them.
        problem.view_test_cases = bool(payload.get('view_test_cases'))
        problem.view_tester = bool(payload.get('view_tester'))
        problem.ai_hints_enabled = bool(payload.get('ai_hints_enabled'))
        problem.short_circuit = bool(payload.get('short_circuit'))
        problem.og_image = (payload.get('og_image') or '')[:150]
        problem.summary = (payload.get('summary') or '')[:300]

        mode = payload.get('submission_source_visibility_mode') or SubmissionSourceAccess.FOLLOW
        valid_modes = {c[0] for c in Problem.SUBMISSION_SOURCE_ACCESS}
        if mode not in valid_modes:
            mode = SubmissionSourceAccess.FOLLOW
        problem.submission_source_visibility_mode = mode

        date_val = self._parse_local_dt(payload.get('date'))
        if date_val is not None:
            problem.date = date_val

        license_id = payload.get('license_id')
        if license_id in ('', None):
            problem.license = None
        else:
            try:
                problem.license = License.objects.get(pk=int(license_id))
            except (TypeError, ValueError, License.DoesNotExist):
                problem.license = None

        problem.save()

        # M2M: only sync when the client sent the key (or on create). Omitting on
        # edit must not wipe / rewrite membership.
        if is_create or 'authors' in payload:
            author_ids = self._profile_ids(payload.get('authors'))
            if is_create:
                # New problems default to the creator; do not force-add on edit
                # (curators/admins must be able to edit without becoming authors).
                if request.profile and request.profile.id not in author_ids:
                    author_ids.append(request.profile.id)
                if not author_ids and request.profile:
                    author_ids = [request.profile.id]
            elif not author_ids:
                raise ValueError(_('Please select at least one author.'))
            problem.authors.set(Profile.objects.filter(pk__in=author_ids))
        if is_create or 'curators' in payload:
            problem.curators.set(Profile.objects.filter(pk__in=self._profile_ids(payload.get('curators'))))
        if is_create or 'testers' in payload:
            problem.testers.set(Profile.objects.filter(pk__in=self._profile_ids(payload.get('testers'))))
        if is_create or 'banned_users' in payload:
            problem.banned_users.set(Profile.objects.filter(pk__in=self._profile_ids(payload.get('banned_users'))))

        if user.has_perm('judge.create_private_problem') and (is_create or 'classes' in payload):
            org_ids = list(problem.organizations.values_list('id', flat=True))
            class_ids = self._profile_ids(payload.get('classes'))  # reuse int parser
            class_qs = Class.get_visible_classes(request.user).filter(pk__in=class_ids)
            if org_ids:
                class_qs = class_qs.filter(organization_id__in=org_ids)
            else:
                class_qs = class_qs.none()
            problem.classes.set(class_qs)

        # Only sync when the client explicitly sent these keys. Omitting them
        # on a partial edit must not wipe existing solution / translations.
        if 'solution' in payload:
            sol = payload.get('solution') or {}
            sol_content = (sol.get('content') or '').strip()
            sol_publish = self._parse_local_dt(sol.get('publish_on'))
            if sol_content or sol.get('is_public') or sol_publish or sol.get('authors'):
                if sol_publish is None:
                    sol_publish = timezone.now()
                solution, _created = Solution.objects.update_or_create(
                    problem=problem,
                    defaults={
                        'is_public': bool(sol.get('is_public')),
                        'publish_on': sol_publish,
                        'content': sol_content,
                    },
                )
                solution.authors.set(Profile.objects.filter(pk__in=self._profile_ids(sol.get('authors'))))
            elif hasattr(problem, 'solution'):
                try:
                    problem.solution.delete()
                except Solution.DoesNotExist:
                    pass

        if 'translations' in payload:
            keep_langs = set()
            supported = dict(settings.LANGUAGES)
            for row in payload.get('translations') or []:
                lang = (row.get('language') or '').strip()
                name = (row.get('name') or '').strip()
                description = (row.get('description') or '').strip()
                if not name and not description:
                    continue
                if not lang:
                    raise ValueError(_('Each translation must specify a language.'))
                if lang not in supported:
                    raise ValueError(
                        _('Unsupported translation language: %(lang)s') % {'lang': lang},
                    )
                keep_langs.add(lang)
                ProblemTranslation.objects.update_or_create(
                    problem=problem, language=lang,
                    defaults={'name': name or problem.name, 'description': description or ''},
                )
            problem.translations.exclude(language__in=keep_langs).delete()

        # Same omit-safe rule as solution/translations: only sync when the client sent the key.
        if 'language_limits' in payload:
            self._sync_language_limits(problem, payload.get('language_limits') or [])
        if 'templates' in payload:
            self._sync_templates(problem, payload.get('templates') or [])
        if 'clarifications' in payload:
            self._sync_clarifications(problem, payload.get('clarifications') or [])

    def _sync_language_limits(self, problem, rows):
        """Replace LanguageLimit rows (classic admin LanguageLimitInline)."""
        keep_lang_ids = set()
        seen_keys = set()
        for row in rows:
            if row.get('delete'):
                continue
            lang_key = (row.get('language') or '').strip()
            if not lang_key:
                continue
            if lang_key in seen_keys:
                raise ValueError(_('Duplicate language limit for %(lang)s.') % {'lang': lang_key})
            seen_keys.add(lang_key)
            lang = Language.objects.filter(key=lang_key).first()
            if lang is None:
                raise ValueError(_('Unknown language: %(lang)s') % {'lang': lang_key})
            try:
                time_limit = float(row.get('time_limit'))
                memory_limit = int(float(row.get('memory_limit')))
            except (TypeError, ValueError):
                raise ValueError(_('Invalid language limit values for %(lang)s.') % {'lang': lang_key})
            if not (settings.DMOJ_PROBLEM_MIN_TIME_LIMIT <= time_limit <= settings.DMOJ_PROBLEM_MAX_TIME_LIMIT):
                raise ValueError(_('Time limit out of range for %(lang)s.') % {'lang': lang_key})
            if not (settings.DMOJ_PROBLEM_MIN_MEMORY_LIMIT <= memory_limit <= settings.DMOJ_PROBLEM_MAX_MEMORY_LIMIT):
                raise ValueError(_('Memory limit out of range for %(lang)s.') % {'lang': lang_key})
            LanguageLimit.objects.update_or_create(
                problem=problem, language=lang,
                defaults={'time_limit': time_limit, 'memory_limit': memory_limit},
            )
            keep_lang_ids.add(lang.id)
        problem.language_limits.exclude(language_id__in=keep_lang_ids).delete()

    def _sync_templates(self, problem, rows):
        """Replace ProblemTemplate rows (classic admin ProblemTemplateInline)."""
        keep_lang_ids = set()
        seen_keys = set()
        for row in rows:
            if row.get('delete'):
                continue
            lang_key = (row.get('language') or '').strip()
            code = row.get('code')
            if code is None:
                code = ''
            else:
                code = str(code)
            if not lang_key:
                continue
            # Empty template rows are ignored (same as leaving an unused inline blank).
            if not code.strip():
                continue
            if lang_key in seen_keys:
                raise ValueError(_('Duplicate problem template for %(lang)s.') % {'lang': lang_key})
            seen_keys.add(lang_key)
            lang = Language.objects.filter(key=lang_key).first()
            if lang is None:
                raise ValueError(_('Unknown language: %(lang)s') % {'lang': lang_key})
            ProblemTemplate.objects.update_or_create(
                problem=problem, language=lang,
                defaults={'code': code},
            )
            keep_lang_ids.add(lang.id)
        problem.templates.exclude(language_id__in=keep_lang_ids).delete()

    def _sync_clarifications(self, problem, rows):
        """Replace ProblemClarification rows (classic admin ProblemClarificationInline)."""
        keep_ids = set()
        for row in rows:
            if row.get('delete'):
                continue
            description = (row.get('description') or '').strip()
            if not description:
                continue
            clar_id = row.get('id')
            if clar_id not in (None, ''):
                try:
                    clar_id = int(clar_id)
                except (TypeError, ValueError):
                    clar_id = None
            if clar_id is not None:
                updated = ProblemClarification.objects.filter(problem=problem, pk=clar_id).update(
                    description=description,
                )
                if updated:
                    keep_ids.add(clar_id)
                    continue
            obj = ProblemClarification.objects.create(problem=problem, description=description)
            keep_ids.add(obj.id)
        ProblemClarification.objects.filter(problem=problem).exclude(id__in=keep_ids).delete()

    # ------------------------------------------------------------------ code problem
    @staticmethod
    def _zip_basename(path):
        path = (path or '').replace('\\', '/')
        return path.rsplit('/', 1)[-1] if path else ''

    @staticmethod
    def _zip_member_names(zipfile_field):
        """Non-directory members of an uploaded ProblemData zip, as classic valid_files."""
        if not zipfile_field:
            return []
        try:
            with ZipFile(zipfile_field.path, 'r') as zf:
                return [
                    name.replace('\\', '/')
                    for name in zf.namelist()
                    if name and not name.endswith('/')
                ]
        except (BadZipfile, OSError, ValueError):
            return []

    def _compile_problem_init(self, problem, files=None):
        """Write init.yml after cases and harness are in the DB (classic test_data order)."""
        from judge.models import ProblemData
        from judge.utils.problem_data import ProblemDataCompiler

        data, _created = ProblemData.objects.get_or_create(problem=problem)
        problem.__dict__.pop('data_files', None)
        if files is None:
            files = self._zip_member_names(data.zipfile)
        ProblemDataCompiler.generate(problem, data, problem.cases.order_by('order'), files)

    def _preferred_create_code(self, payload, request):
        """Resolve the prefilled/submitted code for a new problem.

        Does not write to the database. Raises ``ProblemAlreadySaved`` when the
        code belongs to a problem this user already authored (retry after a
        successful-but-errored first save). Auto-suggested ``p######`` values
        that another user took are replaced with the next free code.
        """
        code = (payload.get('code') or '').strip()
        if not code:
            return None
        if not re.match(r'^[a-z0-9]+$', code):
            raise ValueError(_('Problem code must contain only lowercase letters and numbers.'))
        existing = Problem.objects.filter(code=code).first()
        if existing is None:
            return code
        if request.profile and existing.authors.filter(id=request.profile.id).exists():
            raise ProblemAlreadySaved(existing)
        if wants_auto_series(payload.get('code_auto'), code, 'p'):
            return generate_problem_code()
        raise ValueError(_('Problem code "%(code)s" already exists. Choose a different code.') % {'code': code})

    def _init_problem_defaults(self, problem, payload, request, is_create):
        preferred = self._preferred_create_code(payload, request) if is_create else None
        apply_simple_problem_defaults(problem, preferred_code=preferred)

    def _create_code(self, request, payload, name, problem=None, zip_upload=None):
        user = request.user
        if not _can_create_code(user):
            raise PermissionError(_('You do not have permission to create code problems.'))

        # Inline test cases: sync only when the client sends ``testcases`` (or a ZIP upload).
        # Omitting the key must not wipe cases; empty list on edit clears Normal cases written here.
        # A multipart ``testcase_zip`` uses the same archive path as classic test_data.
        sync_cases = 'testcases' in payload or zip_upload is not None
        cleaned_cases = None
        if sync_cases:
            testcases = payload.get('testcases') or []
            cleaned_cases = [
                tc for tc in testcases
                if (tc.get('input') or tc.get('output') or tc.get('input_file') or tc.get('output_file'))
            ]

        with transaction.atomic():
            is_create = problem is None
            if is_create:
                problem = Problem(submission_type=ProblemSubmissionType.CODE)
            else:
                problem.submission_type = ProblemSubmissionType.CODE
                self._clear_type_children(problem, clear_cases=sync_cases)
            problem.name = name
            problem.description = payload.get('description') or ''
            problem.points = self._num(payload.get('points'), 1)
            problem.partial = bool(payload.get('partial'))
            problem.time_limit = self._num(payload.get('time_limit'), 1)
            problem.memory_limit = int(self._num(payload.get('memory_limit'), 256) * 1024)  # MB -> KB
            problem.group = self._resolve_group(payload)
            self._init_problem_defaults(problem, payload, request, is_create)
            problem.is_public = self._resolve_public(user, bool(payload.get('is_public')), payload, problem=problem)
            problem.save()

            self._attach_types(problem, payload, is_create=is_create)
            self._attach_languages(problem, payload)
            self._attach_orgs(problem, payload, user)
            problem.is_organization_private = problem.organizations.exists()
            problem.save(update_fields=['is_organization_private'])
            self._apply_advanced(problem, payload, request, is_create=is_create)

            # Supplementary AI rubric scoring config (test-case judging is unchanged).
            self._save_code_config(problem, payload)

            # Same order as classic /test_data: persist cases and harness first,
            # then compile init.yml so the judge snapshot includes both.
            checker = payload.get('checker') or ''
            files_for_init = None
            if zip_upload is not None:
                files_for_init = self._save_testcases_from_zip(
                    problem, zip_upload,
                    meta_cases=cleaned_cases if cleaned_cases is not None else [],
                    checker=checker,
                )
            elif sync_cases and payload.get('zip_mode'):
                files_for_init = self._save_testcase_matches(
                    problem, cleaned_cases or [], checker=checker,
                )
            elif sync_cases and (cleaned_cases or payload.get('edit_code')):
                files_for_init = self._save_testcases(
                    problem, cleaned_cases or [], checker=checker,
                )
            if 'harness' in payload:
                self._save_harness(problem, payload)
            if files_for_init is not None or 'harness' in payload:
                self._compile_problem_init(problem, files=files_for_init)
        # Always ping judges after the row exists (first create used to notify
        # inside atomic(), so Judge.problems never picked up the new code).
        from judge.utils.problem_data import notify_judges_of_problem_data
        notify_judges_of_problem_data(problem.code)
        return problem

    def _save_code_config(self, problem, payload):
        """Persist the supplementary AI rubric config for a code problem.

        The AI score is informational (stored as extra test-case rows); it never changes the
        judge's AC/WA verdict, so an absent or disabled config is a no-op.
        """
        ai_enabled = bool(payload.get('ai'))
        rubric = (payload.get('rubric') or '').strip()
        grading_table = payload.get('gradingTable') or None

        config = getattr(problem, 'code_config', None)
        if not ai_enabled and config is None:
            return
        if config is None:
            ProblemCodeConfig.objects.create(
                problem=problem, ai_enabled=True,
                ai_rubric=rubric, grading_table=grading_table,
            )
        else:
            config.ai_enabled = ai_enabled
            config.ai_rubric = rubric
            config.grading_table = grading_table
            config.save(update_fields=['ai_enabled', 'ai_rubric', 'grading_table'])

    def _attach_languages(self, problem, payload):
        # No selection (or an all-invalid selection) means "all languages allowed" so the problem
        # is never left unsubmittable; DMOJ treats an empty allowed_languages set as no languages.
        keys = [k for k in (payload.get('languages') or []) if k]
        qs = Language.objects.filter(key__in=keys) if keys else Language.objects.none()
        if not qs.exists():
            qs = Language.objects.all()
        problem.allowed_languages.set(qs)

    def _clear_problem_data_files(self, problem):
        """Drop leftover zip/archive and old loose case files before rewriting from create-v2."""
        from judge.models import ProblemData, problem_data_storage

        try:
            data = problem.data_files
        except ProblemData.DoesNotExist:
            data = None
        if data and data.zipfile:
            # ProblemData.save() deletes the previous file when zipfile changes.
            data.zipfile = None
            data.save()

        root = getattr(problem_data_storage, 'location', None) or ''
        problem_dir = os.path.join(root, problem.code) if root else ''
        if problem_dir and os.path.isdir(problem_dir):
            for name in os.listdir(problem_dir):
                lower = name.lower()
                if (lower.endswith('.in') or lower.endswith('.out') or lower.endswith('.ans') or
                        lower.endswith('.in.txt') or lower.endswith('.out.txt') or lower.endswith('.ans.txt')):
                    try:
                        problem_data_storage.delete('%s/%s' % (problem.code, name))
                    except Exception:
                        pass

    def _apply_checker(self, data, checker):
        checker_value = (checker or '')[:10]
        if checker_value and checker_value not in {c[0] for c in CHECKERS}:
            checker_value = ''
        data.checker = checker_value

    def _save_testcases_from_zip(self, problem, uploaded_zip, meta_cases=None, checker=''):
        """Store the archive as classic test_data does; cases reference zip members the user picked.

        The zip is not re-paired by filename conventions. ``meta_cases`` supplies
        ``input_file`` / ``output_file`` / points / pretest for each Normal case.
        """
        from judge.models import ProblemData

        if not uploaded_zip:
            raise ValueError(_('Your zip file is invalid!'))
        zip_name = self._zip_basename(getattr(uploaded_zip, 'name', '') or '') or 'data.zip'
        if not zip_name.lower().endswith('.zip'):
            raise ValueError(_("Zip files must end in '.zip'"))

        try:
            with ZipFile(uploaded_zip, 'r') as zf:
                namelist = [name.replace('\\', '/') for name in zf.namelist() if name and not name.endswith('/')]
        except BadZipfile:
            raise
        except Exception:
            raise ValueError(_('Your zip file is invalid!'))
        finally:
            if hasattr(uploaded_zip, 'seek'):
                uploaded_zip.seek(0)

        valid = set(namelist)
        meta_cases = meta_cases or []
        self._write_zip_matched_cases(problem, meta_cases, valid)
        self._clear_problem_data_files(problem)

        data, _created = ProblemData.objects.get_or_create(problem=problem)
        self._apply_checker(data, checker)
        data.zipfile.save(zip_name, uploaded_zip, save=False)
        data.save()
        problem.__dict__.pop('data_files', None)
        return namelist

    def _save_testcase_matches(self, problem, testcases, checker=''):
        """Keep the existing zip and update which members each case uses (classic test_data)."""
        from judge.models import ProblemData

        try:
            data = problem.data_files
        except ProblemData.DoesNotExist:
            data = None
        if data is None or not data.zipfile:
            raise ValueError(_('Upload a zip of test files before matching input and output.'))

        namelist = self._zip_member_names(data.zipfile)
        self._write_zip_matched_cases(problem, testcases, set(namelist))
        self._apply_checker(data, checker)
        data.save()
        problem.__dict__.pop('data_files', None)
        return namelist

    def _write_zip_matched_cases(self, problem, testcases, valid_names):
        from judge.models import ProblemTestCase

        prepared = []
        for tc in testcases or []:
            in_name = (tc.get('input_file') or '').replace('\\', '/').strip()
            out_name = (tc.get('output_file') or '').replace('\\', '/').strip()
            if in_name and in_name not in valid_names:
                raise ValueError(_('Input file "%s" is not in the zip.') % in_name)
            if out_name and out_name not in valid_names:
                raise ValueError(_('Output file "%s" is not in the zip.') % out_name)
            prepared.append((in_name, out_name, tc))

        problem.cases.filter(type='C').delete()
        for order, (in_name, out_name, tc) in enumerate(prepared, start=1):
            ProblemTestCase.objects.create(
                dataset=problem, order=order, type='C',
                input_file=in_name, output_file=out_name,
                points=int(self._num(tc.get('points'), 0)),
                is_pretest=bool(tc.get('pretest')),
                checker=(tc.get('checker') or '')[:10],
            )

    def _save_harness(self, problem, payload):
        """Persist at most one harness, matching classic test_data formset max_num=1."""
        harness = payload.get('harness')
        ProblemHarness.objects.filter(problem=problem).delete()
        if not harness or harness.get('delete'):
            return
        code = (harness.get('harness_code') or '').strip()
        if not code:
            return
        lang = None
        language_id = harness.get('language_id')
        language_key = (harness.get('language') or '').strip()
        qs = Language.objects.filter(common_name__in=['Java', 'Python'])
        if language_id not in (None, ''):
            try:
                lang = qs.filter(pk=int(language_id)).first()
            except (TypeError, ValueError):
                lang = None
        if lang is None and language_key:
            lang = qs.filter(key=language_key).first()
        if lang is None:
            raise ValueError(_('Please select a harness language.'))
        ProblemHarness.objects.create(
            problem=problem,
            language=lang,
            harness_code=code,
            skip_precompile=bool(harness.get('skip_precompile')),
            run_student_main=bool(harness.get('run_student_main')),
        )

    def _save_testcases(self, problem, testcases, checker=''):
        """Persist inline cases as loose files + a data.zip so the classic test_data page stays in sync.

        Each case may carry optional ``input_file`` / ``output_file`` names (from a ZIP import).
        Contents are always written from ``input`` / ``output``. A fresh ``data.zip`` is built from
        those files so the old editor's ``valid_files`` (ZIP namelist) matches.
        """
        import io
        from zipfile import ZIP_DEFLATED

        from django.core.files.base import ContentFile as DjangoContentFile

        from judge.models import ProblemData, ProblemTestCase, problem_data_storage

        self._clear_problem_data_files(problem)
        problem.cases.filter(type='C').delete()

        valid_files = []
        used_names = set()

        def _unique_name(preferred, fallback):
            name = (preferred or '').strip() or fallback
            name = os.path.basename(name.replace('\\', '/')) or fallback
            if name not in used_names:
                used_names.add(name)
                return name
            stem, ext = os.path.splitext(name)
            n = 2
            while True:
                candidate = '%s_%d%s' % (stem, n, ext)
                if candidate not in used_names:
                    used_names.add(candidate)
                    return candidate
                n += 1

        for order, tc in enumerate(testcases, start=1):
            in_name = _unique_name(tc.get('input_file'), '%02d.in' % order)
            out_name = _unique_name(tc.get('output_file'), '%02d.out' % order)
            problem_data_storage.save('%s/%s' % (problem.code, in_name),
                                      ContentFile((tc.get('input') or '').encode('utf-8')))
            problem_data_storage.save('%s/%s' % (problem.code, out_name),
                                      ContentFile((tc.get('output') or '').encode('utf-8')))
            valid_files += [in_name, out_name]
            ProblemTestCase.objects.create(
                dataset=problem, order=order, type='C',
                input_file=in_name, output_file=out_name,
                points=int(self._num(tc.get('points'), 0)),
                is_pretest=bool(tc.get('pretest')),
                checker=(tc.get('checker') or '')[:10],
            )

        data, _created = ProblemData.objects.get_or_create(problem=problem)
        self._apply_checker(data, checker)

        # Rebuild a zip from the current loose files so classic test_data sees matching names.
        if valid_files:
            buf = io.BytesIO()
            with ZipFile(buf, 'w', compression=ZIP_DEFLATED) as zf:
                for name in valid_files:
                    with problem_data_storage.open('%s/%s' % (problem.code, name)) as fh:
                        zf.writestr(name, fh.read())
            buf.seek(0)
            # Assigning a new FileField value deletes the previous zip via ProblemData.save().
            data.zipfile.save('data.zip', DjangoContentFile(buf.read()), save=False)
        elif data.zipfile:
            data.zipfile = None

        data.save()
        # Drop cached reverse OneToOne so later ``problem.data_files`` sees this write
        # (same request often already touched data_files before _save_testcases).
        problem.__dict__.pop('data_files', None)
        return valid_files

    # ------------------------------------------------------------------ mcq problem
    def _create_mcq(self, request, payload, name, multiple, problem=None):
        user = request.user
        if not user.has_perm('judge.create_private_problem'):
            raise PermissionError(_('You do not have permission to create multiple-choice problems.'))

        options = [o for o in (payload.get('options') or []) if (o.get('text') or '').strip()]
        if len(options) < 2:
            raise ValueError(_('Please add at least two choices.'))
        correct = [o for o in options if o.get('correct')]
        if not correct:
            raise ValueError(_('Please mark at least one correct choice.'))
        if not multiple and len(correct) > 1:
            raise ValueError(_('Single-answer questions must have exactly one correct choice.'))

        with transaction.atomic():
            is_create = problem is None
            if is_create:
                problem = Problem(submission_type=ProblemSubmissionType.MCQ)
            else:
                was_mcq = problem.submission_type == ProblemSubmissionType.MCQ
                problem.submission_type = ProblemSubmissionType.MCQ
                self._clear_type_children(problem, keep_mcq_options=was_mcq)
            problem.name = name
            problem.description = payload.get('description') or ''
            problem.points = self._num(payload.get('points'), 1)
            problem.partial = True if multiple else False
            problem.group = self._resolve_group(payload)
            self._init_problem_defaults(problem, payload, request, is_create)
            problem.is_public = self._resolve_public(user, bool(payload.get('is_public')), payload, problem=problem)
            problem.save()

            self._attach_types(problem, payload, is_create=is_create)
            self._attach_orgs(problem, payload, user)
            problem.is_organization_private = problem.organizations.exists()
            problem.save(update_fields=['is_organization_private'])
            self._apply_advanced(problem, payload, request, is_create=is_create)

            scoring_mode = ProblemMCQConfig.SCORE_PROPORTIONAL
            underselect_points = None
            if multiple:
                raw_mode = payload.get('mcqScoring') or scoring_mode
                if raw_mode in (ProblemMCQConfig.SCORE_PROPORTIONAL, ProblemMCQConfig.SCORE_UNDERSELECT):
                    scoring_mode = raw_mode
                underselect_points = self._positive_or_none(payload.get('mcqUnderselectPoints'))
                if scoring_mode != ProblemMCQConfig.SCORE_UNDERSELECT:
                    underselect_points = None
            ProblemMCQConfig.objects.create(
                problem=problem, multiple_select=multiple, shuffle_options=bool(payload.get('shuffle')),
                scoring_mode=scoring_mode, underselect_points=underselect_points)
            sync_mcq_options(problem, [
                {'id': option.get('id'), 'text': (option.get('text') or '').strip(),
                 'is_correct': bool(option.get('correct'))}
                for option in options
            ])
        return problem

    # ------------------------------------------------------------------ true / false
    def _create_truefalse(self, request, payload, name, problem=None):
        """True/false is stored as a two-option single-choice MCQ (正确 / 错误)."""
        user = request.user
        if not user.has_perm('judge.create_private_problem'):
            raise PermissionError(_('You do not have permission to create this problem type.'))
        answer = (payload.get('tfAnswer') or 'true').strip().lower()
        if answer not in ('true', 'false'):
            raise ValueError(_('Please choose the correct answer.'))

        with transaction.atomic():
            is_create = problem is None
            if is_create:
                problem = Problem(submission_type=ProblemSubmissionType.MCQ)
            else:
                was_mcq = problem.submission_type == ProblemSubmissionType.MCQ
                problem.submission_type = ProblemSubmissionType.MCQ
                self._clear_type_children(problem, keep_mcq_options=was_mcq)
            problem.name = name
            problem.description = payload.get('description') or ''
            problem.points = self._num(payload.get('points'), 1)
            problem.partial = False
            problem.group = self._resolve_group(payload)
            self._init_problem_defaults(problem, payload, request, is_create)
            problem.is_public = self._resolve_public(user, bool(payload.get('is_public')), payload, problem=problem)
            problem.save()

            self._attach_types(problem, payload, is_create=is_create)
            self._attach_orgs(problem, payload, user)
            problem.is_organization_private = problem.organizations.exists()
            problem.save(update_fields=['is_organization_private'])
            self._apply_advanced(problem, payload, request, is_create=is_create)

            ProblemMCQConfig.objects.create(problem=problem, multiple_select=False, shuffle_options=False,
                                            is_true_false=True)
            # Matched by position (True = 0, False = 1), so an edit keeps both option ids.
            sync_mcq_options(problem, [
                {'text': _('True'), 'is_correct': answer == 'true'},
                {'text': _('False'), 'is_correct': answer == 'false'},
            ])
        return problem

    # ------------------------------------------------------------------ fill in the blank
    def _create_fill_blank(self, request, payload, name, problem=None):
        user = request.user
        if not user.has_perm('judge.create_private_problem'):
            raise PermissionError(_('You do not have permission to create this problem type.'))
        blanks = list(self._iter_blank_payload(payload))
        if not blanks:
            raise ValueError(_('Please provide at least one blank answer.'))

        with transaction.atomic():
            is_create = problem is None
            if is_create:
                problem = Problem(submission_type=ProblemSubmissionType.FILL_BLANK)
            else:
                problem.submission_type = ProblemSubmissionType.FILL_BLANK
                self._clear_type_children(problem)
            problem.name = name
            problem.description = payload.get('description') or ''
            problem.points = self._num(payload.get('points'), 1)
            problem.partial = bool(payload.get('partial'))
            problem.group = self._resolve_group(payload)
            self._init_problem_defaults(problem, payload, request, is_create)
            problem.is_public = self._resolve_public(user, bool(payload.get('is_public')), payload, problem=problem)
            problem.save()

            self._attach_types(problem, payload, is_create=is_create)
            self._attach_orgs(problem, payload, user)
            problem.is_organization_private = problem.organizations.exists()
            problem.save(update_fields=['is_organization_private'])
            self._apply_advanced(problem, payload, request, is_create=is_create)

            ProblemFillBlankConfig.objects.create(
                problem=problem,
                case_sensitive=bool(payload.get('caseSensitive')),
                trim_whitespace=bool(payload.get('trimWhitespace', True)),
                grading_mode=GRADE_AI if bool(payload.get('ai')) else GRADE_EXACT,
                ai_rubric=(payload.get('rubric') or '').strip(),
                grading_table=payload.get('gradingTable') or None,
            )
            # The create page enters accepted answers one-per-line per blank; ProblemBlank stores
            # them the same way (the grader splits on newlines). Per-blank points apply when
            # problem.partial is on.
            for index, (answers, points) in enumerate(blanks):
                normalized = '\n'.join(line.strip() for line in answers.splitlines() if line.strip())
                ProblemBlank.objects.create(
                    problem=problem, order=index, answers=normalized, points=points)
        return problem

    # ------------------------------------------------------------------ short answer
    def _create_short_answer(self, request, payload, name, problem=None):
        user = request.user
        if not user.has_perm('judge.create_private_problem'):
            raise PermissionError(_('You do not have permission to create this problem type.'))

        with transaction.atomic():
            is_create = problem is None
            if is_create:
                problem = Problem(submission_type=ProblemSubmissionType.SHORT_ANSWER)
            else:
                problem.submission_type = ProblemSubmissionType.SHORT_ANSWER
                self._clear_type_children(problem)
            problem.name = name
            problem.description = payload.get('description') or ''
            problem.points = self._num(payload.get('points'), 1)
            # AI grading returns a 0-100 percentage, so partial credit must be on for it to count.
            problem.partial = True
            problem.group = self._resolve_group(payload)
            self._init_problem_defaults(problem, payload, request, is_create)
            problem.is_public = self._resolve_public(user, bool(payload.get('is_public')), payload, problem=problem)
            problem.save()

            self._attach_types(problem, payload, is_create=is_create)
            self._attach_orgs(problem, payload, user)
            problem.is_organization_private = problem.organizations.exists()
            problem.save(update_fields=['is_organization_private'])
            self._apply_advanced(problem, payload, request, is_create=is_create)

            ProblemShortAnswerConfig.objects.create(
                problem=problem,
                reference_answer=(payload.get('shortAnswer') or '').strip(),
                ai_enabled=bool(payload.get('ai')),
                ai_rubric=(payload.get('rubric') or '').strip(),
                grading_table=payload.get('gradingTable') or None,
            )
        return problem

    @staticmethod
    def _normalise_extensions(raw):
        """``".PDF, .zip"`` -> ``"pdf,zip"``.

        ``ProblemFileConfig.allowed_extension_list`` expects lowercase, dot-less entries, so store
        them that way rather than round-tripping whatever the author typed.
        """
        seen, out = set(), []
        for part in (raw or '').split(','):
            ext = part.strip().lstrip('.').lower()
            if ext and ext not in seen:
                seen.add(ext)
                out.append(ext)
        return ','.join(out)

    def _create_file_upload(self, request, payload, name, problem=None):
        user = request.user
        if not user.has_perm('judge.create_private_problem'):
            raise PermissionError(_('You do not have permission to create this problem type.'))

        # The page edits whole MB; the model stores bytes. Guard on the byte count so a fractional
        # value cannot truncate to 0 — ProblemFileConfig treats 0 as "no limit".
        max_file_size = int(self._num(payload.get('fileMaxSizeMb'), 20) * 1024 * 1024)
        if max_file_size <= 0:
            raise ValueError(_('Maximum file size must be greater than zero.'))

        with transaction.atomic():
            is_create = problem is None
            if is_create:
                problem = Problem(submission_type=ProblemSubmissionType.FILE)
            else:
                problem.submission_type = ProblemSubmissionType.FILE
                self._clear_type_children(problem)
            problem.name = name
            problem.description = payload.get('description') or ''
            problem.points = self._num(payload.get('points'), 1)
            # A teacher grades these by hand and may award anything in [0, points], so partial
            # credit must be on for the awarded score to reach Profile.points and the standings.
            problem.partial = True
            problem.group = self._resolve_group(payload)
            self._init_problem_defaults(problem, payload, request, is_create)
            problem.is_public = self._resolve_public(user, bool(payload.get('is_public')), payload, problem=problem)
            problem.save()

            self._attach_types(problem, payload, is_create=is_create)
            self._attach_orgs(problem, payload, user)
            problem.is_organization_private = problem.organizations.exists()
            problem.save(update_fields=['is_organization_private'])
            self._apply_advanced(problem, payload, request, is_create=is_create)

            ProblemFileConfig.objects.create(
                problem=problem,
                allowed_extensions=self._normalise_extensions(payload.get('fileExtensions')),
                max_file_size=max_file_size,
                instructions=(payload.get('fileInstructions') or '').strip(),
            )
        return problem


@method_decorator(ensure_csrf_cookie, name='dispatch')
class ProblemCreateV2AIDifficultyView(LoginRequiredMixin, View):
    """Suggest Easy/Medium/Hard from problem text; uses LLM when configured, else a heuristic."""

    def post(self, request, *args, **kwargs):
        if not _can_create_any(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({'error': _('Invalid request.')}, status=400)

        description = (payload.get('description') or '').strip()
        if not description:
            return JsonResponse({'error': _('Please enter problem content first.')}, status=400)

        try:
            level = classify_problem_difficulty(description)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('AI difficulty classification failed')
            return JsonResponse({'error': _('Could not reach the AI service. '
                                            'Please check your connection and try again.')}, status=503)
        return JsonResponse({'level': level, 'label': {'easy': 'Easy', 'medium': 'Medium', 'hard': 'Hard'}[level]})


def classify_problem_difficulty(description):
    """Return ``easy`` / ``medium`` / ``hard`` from problem text.

    Uses the configured LLM when available (same prompt as the add-problem 🤖 AI
    button). A configured-but-unreachable model raises; callers that want a
    silent fallback should catch that. When no LLM is configured, a length
    heuristic is used.
    """
    from judge import llm

    description = (description or '').strip()
    if llm.is_configured():
        messages = [
            {'role': 'system', 'content': (
                'You classify educational/problem difficulty. Reply with exactly one word: '
                'easy, medium, or hard. No other text.'
            )},
            {'role': 'user', 'content': description[:4000]},
        ]
        text = llm.chat(messages, max_tokens=16).text.strip().lower()
        for level in ('easy', 'medium', 'hard'):
            if level in text:
                return level

    compact = description.replace(' ', '')
    if len(compact) < 80:
        return 'easy'
    if len(compact) < 240:
        return 'medium'
    return 'hard'


@method_decorator(ensure_csrf_cookie, name='dispatch')
class ProblemCreateV2ProblemTypeView(LoginRequiredMixin, View):
    """Create a ProblemType from the add-problem page (code and/or display name)."""

    def post(self, request, *args, **kwargs):
        if not _can_create_any(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({'error': _('Invalid request.')}, status=400)

        name = (payload.get('name') or '').strip()
        full_name = (payload.get('full_name') or '').strip()
        if not name and not full_name:
            return JsonResponse({'error': _('Please enter a category ID or name.')}, status=400)
        if not name:
            name = _problem_type_code(full_name)
        if not full_name:
            full_name = name
        if len(name) > 20:
            return JsonResponse({'error': _('Category ID must be at most 20 characters.')}, status=400)
        if len(full_name) > 100:
            return JsonResponse({'error': _('Category name must be at most 100 characters.')}, status=400)
        if ProblemType.objects.filter(name=name).exists():
            return JsonResponse({'error': _('This category ID already exists.')}, status=400)

        ptype = ProblemType.objects.create(name=name, full_name=full_name)
        return JsonResponse({'ok': True, 'type': {'name': ptype.name, 'full': ptype.full_name}})


@method_decorator(ensure_csrf_cookie, name='dispatch')
class ProblemCreateV2ProblemGroupView(LoginRequiredMixin, View):
    """Create a ProblemGroup from the add-problem page (accepts a display name)."""

    def post(self, request, *args, **kwargs):
        if not _can_create_any(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({'error': _('Invalid request.')}, status=400)

        full_name = (payload.get('full_name') or '').strip()
        name = (payload.get('name') or '').strip()
        if not full_name and not name:
            return JsonResponse({'error': _('Please enter a group name.')}, status=400)
        if not full_name:
            full_name = name
        if not name:
            name = _problem_type_code(full_name)
        if len(name) > 20:
            return JsonResponse({'error': _('Group ID must be at most 20 characters.')}, status=400)
        if len(full_name) > 100:
            return JsonResponse({'error': _('Group name must be at most 100 characters.')}, status=400)
        if ProblemGroup.objects.filter(name=name).exists():
            # Keep the code unique by suffixing a short number when it collides.
            base = name[:16]
            for i in range(2, 100):
                candidate = '%s-%d' % (base, i)
                if not ProblemGroup.objects.filter(name=candidate).exists():
                    name = candidate
                    break
            else:
                return JsonResponse({'error': _('This group already exists.')}, status=400)

        group = ProblemGroup.objects.create(name=name, full_name=full_name)
        return JsonResponse({'ok': True, 'group': {'id': group.id, 'full': group.full_name or group.name}})


@method_decorator(ensure_csrf_cookie, name='dispatch')
class ProblemCreateV2OrganizationView(LoginRequiredMixin, View):
    """Create an Organization from the add-problem page and add the current user as admin/member.

    Only users who can create private (organization) problems may make new organizations here; the
    creator is added both as an administrator and a member so it appears in their scoped list.
    """

    def post(self, request, *args, **kwargs):
        if not request.user.has_perm('judge.create_private_problem'):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({'error': _('Invalid request.')}, status=400)

        name = (payload.get('name') or '').strip()
        if not name:
            return JsonResponse({'error': _('Please enter an organization name.')}, status=400)
        if len(name) > 128:
            return JsonResponse({'error': _('Organization name must be at most 128 characters.')}, status=400)

        slug = slugify(name)[:128] or 'org'
        short_name = name[:20]
        with transaction.atomic():
            org = Organization.objects.create(name=name, slug=slug, short_name=short_name, about='')
            profile = request.profile
            if profile:
                org.admins.add(profile)
                profile.organizations.add(org)
        return JsonResponse({'ok': True, 'organization': {'id': org.id, 'name': org.name}})


@method_decorator(ensure_csrf_cookie, name='dispatch')
class ProblemCreateV2AIProblemTypeView(LoginRequiredMixin, View):
    """Suggest problem_type tags from problem text; uses LLM when configured."""

    def post(self, request, *args, **kwargs):
        if not _can_create_any(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({'error': _('Invalid request.')}, status=400)

        description = (payload.get('description') or '').strip()
        if not description:
            return JsonResponse({'error': _('Please enter problem content first.')}, status=400)

        try:
            suggestions = self._suggest(description)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('AI tag suggestion failed')
            return JsonResponse({'error': _('Could not reach the AI service. '
                                            'Please check your connection and try again.')}, status=503)
        return JsonResponse({'suggestions': suggestions})

    def _suggest(self, description):
        from judge import llm

        # Candidate set = existing knowledge tags + the controlled algorithm vocabulary.
        # Answer-format / difficulty chips (fill_in_blank, Programming, Easy, …) are excluded
        # here AND re-filtered after parsing, so a format tag can never be suggested.
        candidates = _tag_suggestion_candidates()

        # Only call the LLM when configured; a configured-but-unreachable model raises (surfaced
        # as 503) instead of silently falling back and pretending the AI produced the tags.
        if llm.is_configured():
            messages = [
                {'role': 'system', 'content': (
                    'You classify competitive-programming / educational problems by the '
                    'algorithm or solution technique they exercise (e.g. dynamic programming, '
                    'depth-first search, polynomial math).\n'
                    'Rules:\n'
                    '- Reply with 1-3 labels, comma-separated, chosen ONLY from the provided '
                    'candidate list.\n'
                    '- NEVER invent labels that are not in the candidate list.\n'
                    '- NEVER use question-format tags (programming, fill-in-the-blank, '
                    'multiple-choice, short answer, true/false) or difficulty tags '
                    '(easy, medium, hard) or anything unrelated to the solution method.\n'
                    '- If nothing fits well, reply with a single most-relevant candidate '
                    'instead of inventing new ones.\n'
                    'Candidate labels: ' + ', '.join(candidates)
                )},
                {'role': 'user', 'content': description[:4000]},
            ]
            text = llm.chat(messages, max_tokens=64).text.strip()
            picked = []
            seen = set()
            for part in text.replace('\n', ',').split(','):
                label = _normalize_tag_label(part)
                if label is None:
                    continue
                key = label.casefold()
                if key in seen:
                    continue
                seen.add(key)
                # Prefer the DB knowledge tag row when the label already exists; otherwise
                # the frontend creates the ProblemType row from the vocabulary label on save.
                existing = next((t for t in _knowledge_type_labels()
                                 if t.casefold() == label.casefold()), None)
                picked.append({
                    'name': _problem_type_code(existing or label),
                    'full': existing or label,
                    'existing': existing is not None,
                })
                if len(picked) >= 3:
                    break
            if picked:
                return picked

        # No LLM configured (or the model returned no usable label): fall back to substring
        # matching against knowledge tags only.
        compact = description.lower()
        picked = []
        for label in _knowledge_type_labels():
            if label.lower() in compact:
                picked.append({'name': _problem_type_code(label), 'full': label, 'existing': True})
            if len(picked) >= 3:
                break
        if picked:
            return picked
        # Last resort: a generic tag is preferable to inventing a question-type tag.
        return [{'name': 'general', 'full': 'General', 'existing': False}]


@method_decorator(ensure_csrf_cookie, name='dispatch')
class ProblemCreateV2AIDecomposeView(LoginRequiredMixin, View):
    """Use the LLM to decompose a problem description into independent sub-questions
    for a grading table.  Each sub-question should test a distinct, scorable concept.

    Uses a dedicated lightweight model (profile ``decompose``) so grading throughput
    is not affected."""

    def post(self, request, *args, **kwargs):
        if not _can_create_any(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({'error': _('Invalid request.')}, status=400)

        description = (payload.get('description') or '').strip()
        total_points = payload.get('total_points') or 1.0
        if not description:
            return JsonResponse({'error': _('Please enter problem content first.')}, status=400)

        try:
            sub_questions = self._decompose(description, float(total_points))
        except Exception:
            import logging
            logging.getLogger(__name__).exception('AI decompose failed')
            return JsonResponse({'error': _('The AI service is temporarily unavailable. '
                                            'Please try again later.')}, status=503)
        return JsonResponse({'sub_questions': sub_questions})

    def _decompose(self, description, total_points):
        from judge import llm
        import logging
        _log = logging.getLogger(__name__)

        if not llm.is_configured():
            _log.warning('Decompose called but LLM is not configured')
            raise RuntimeError('LLM not configured')

        try:
            messages = [
                {'role': 'system', 'content': (
                    'You decompose educational problems into independent sub-questions '
                    'for grading. Each sub-question must test a distinct conceptual '
                    'understanding or skill that can be scored independently. '
                    'Do NOT create sub-questions for trivial aspects like "expression '
                    'precision" or "writing style". Sub-questions should have no strong '
                    'dependencies on each other — a student could answer one correctly '
                    'and another incorrectly based on distinct knowledge gaps.\n\n'
                    'Reply with ONLY valid JSON:\n'
                    '{"sub_questions": ['
                    '  {"name": "short label (≤30 chars)", "max_points": float}, ...]}\n'
                    '- Use Chinese or English matching the problem language.\n'
                    '- Each max_points must be a positive multiple of 0.5.\n'
                    '- The max_points values MUST sum exactly to the total points.\n'
                    '- When there are no clear sub-questions, return exactly one item '
                    '  with max_points equal to the total points.'
                )},
                {'role': 'user', 'content': (
                    f'Problem description:\n{description[:2000]}\n\n'
                    f'Total points: {total_points}\n\n'
                    f'Break this problem into 1-5 independent sub-questions. '
                    f'Use 0.5-point increments only.'
                )},
            ]
            text = llm.chat(messages, profile='decompose', max_tokens=1024).text.strip()

            # Strip markdown fences if present
            if text.startswith('```'):
                lines = text.split('\n')
                text = '\n'.join(lines[1:]) if len(lines) > 1 else text
                if text.endswith('```'):
                    text = text.rsplit('```', 1)[0]

            parsed = json.loads(text)
            items = parsed.get('sub_questions', [])
            if not isinstance(items, list) or not items:
                return []
            named = [
                {'name': str(item.get('name', ''))[:30],
                 'max_points': float(item.get('max_points', 0))}
                for item in items if item.get('name')
            ]
            if not named:
                return []
            from judge.utils.calibration import redistribute_half_points
            snapped = redistribute_half_points(
                [row['max_points'] for row in named], total_points,
            )
            return [
                {'name': row['name'], 'max_points': pts}
                for row, pts in zip(named, snapped)
                if pts > 0
            ]
        except Exception as e:
            _log.exception('Decomposition failed')
            raise RuntimeError('Decomposition failed') from e
