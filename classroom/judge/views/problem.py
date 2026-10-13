import json
import logging
import os
import re
import shutil
from datetime import timedelta
from operator import itemgetter
from random import randrange
from statistics import mean, median

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import BooleanField, Case, CharField, Count, F, FilteredRelation, Max, Prefetch, Q, When
from django.db.models.functions import Coalesce
from django.db.utils import ProgrammingError
from django.http import Http404, HttpResponse, HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.loader import get_template, render_to_string
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.functional import cached_property
from django.utils.html import escape, format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.generic import DetailView, ListView, View
from django.views.generic.detail import SingleObjectMixin
from reversion import revisions

from judge import event_poster as event
from judge.comments import CommentedDetailView, SubmissionCommentMixin
from judge.forms import ProblemCloneForm, ProblemPointsVoteForm, ProblemSubmitForm
from judge.models import (
    AssignmentDraft,
    Contest,
    ContestProblem,
    ContestSubmission,
    Judge,
    Language,
    LanguageLimit,
    Organization,
    Problem,
    ProblemBlank,
    ProblemData,
    ProblemFileConfig,
    ProblemFillBlankConfig,
    ProblemGroup,
    ProblemHarness,
    ProblemMCQConfig,
    ProblemMCQOption,
    ProblemPointsVote,
    ProblemShortAnswerConfig,
    ProblemTemplate,
    ProblemTestCase,
    ProblemTranslation,
    ProblemType,
    Profile,
    RuntimeVersion,
    Solution,
    Submission,
    contest_problem_display_name,
    problem_data_storage,
)
from judge.utils.assets import ace_url
from judge.utils.assignment_harvest import (
    assignment_harvest_enabled, participation_is_harvestable, user_bypasses_assignment_rules,
)
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.opengraph import generate_opengraph
from judge.utils.pdfoid import PDF_RENDERING_ENABLED, render_pdf
from judge.utils.problem_create import generate_problem_code
from judge.utils.problems import (
    assignment_problem_stats,
    contest_attempted_ids,
    contest_completed_ids,
    hot_problems,
    log_workspace_progress,
    participation_submit_progress,
    user_attempted_ids,
    user_completed_ids,
    user_editable_ids,
    user_tester_ids,
)
from judge.utils.result_visibility import (contest_problem_hides_results, problem_in_active_hidden_exam,
                                           results_hidden_q)
from judge.utils.strings import safe_float_or_none, safe_int_or_none
from judge.utils.submission_create import create_submission
from judge.utils.tickets import own_ticket_filter
from judge.utils.views import QueryStringSortMixin, SingleObjectFormView, TitleMixin, add_file_response, generic_message

recjk = re.compile(
    r'[\u2E80-\u2E99\u2E9B-\u2EF3\u2F00-\u2FD5\u3005\u3007\u3021-\u3029\u3038-\u303A\u303B\u3400-\u4DB5'
    r'\u4E00-\u9FC3\uF900-\uFA2D\uFA30-\uFA6A\uFA70-\uFAD9\U00020000-\U0002A6D6\U0002F800-\U0002FA1D]',
)


def get_contest_problem(problem, profile):
    if profile is None or profile.current_contest is None:
        return None
    try:
        return problem.contests.get(contest_id=profile.current_contest.contest_id)
    except ObjectDoesNotExist:
        return None


def get_contest_submission_count(problem, profile, virtual):
    return (
        profile.current_contest.submissions.exclude(submission__status__in=['IE'])
        .filter(problem__problem=problem, participation__virtual=virtual)
        .count()
    )


def user_can_see_problem_author_metadata(user):
    """Teachers/editors may see problem-author filters and columns; students may not."""
    if not user.is_authenticated:
        return False
    return (
        user.has_perm('judge.edit_own_problem')
        or user.has_perm('judge.see_private_problem')
        or user.has_perm('judge.edit_all_problem')
    )


def workspace_ai_grade_fields(request, submission):
    """On-demand AI grading button state for the Workspace / submit JSON."""
    from judge.utils.contest_ai import ai_grade_context, problem_has_ai_grading

    contest = None
    try:
        current = getattr(request.profile, 'current_contest', None)
        if current is not None:
            contest = current.contest
    except Exception:
        contest = None
    try:
        ctx = ai_grade_context(request.user, submission, contest=contest)
    except TypeError:
        # Older runtimes (this host's image) only accept (user, submission).
        ctx = ai_grade_context(request.user, submission)
    fields = {
        'can_trigger_ai_grade': bool(ctx.get('can_trigger')),
        'ai_grade_remaining': ctx.get('remaining', 0),
        'problem_has_ai_grading': problem_has_ai_grading(submission.problem),
        # Students see Feedback text only; teachers still see numeric AI scores.
        'show_ai_scores': bool(ctx.get('is_teacher')),
    }
    if fields['can_trigger_ai_grade']:
        fields['ai_grade_url'] = reverse('submission_ai_grade', args=[submission.id])
    return fields


def workspace_display_points(submission):
    """Score shown to students in Workspace, the submission list, and status.

    Gated on ``ContestSubmission`` + Release, not ``request.in_contest``. See
    ``judge.utils.contest_grade.student_display_points``.
    """
    from judge.utils.contest_grade import student_display_points
    return student_display_points(submission)


def workspace_display_points_total(submission):
    """Max score shown next to the student-visible numerator."""
    from judge.utils.contest_grade import student_display_points_total
    return student_display_points_total(submission)


def workspace_review_note_html(submission):
    """Sanitized teacher-feedback (评语) HTML, or '' when not shown.

    The note is only revealed to the student once the teacher's manual override
    has been released; it is rendered through the site markdown pipeline (bleach
    sanitized) so it can be injected safely from either the server-rendered
    template or the workspace's JSON status endpoint.
    """
    manual_grade = submission.manual_grade_override
    if manual_grade is None or not manual_grade.released or not submission.review_note:
        return ''
    from judge.jinja2.markdown import markdown as render_markdown
    return str(render_markdown(submission.review_note, 'default'))


def workspace_manual_released(submission):
    """Whether this submission's manual override has been released to the student."""
    from judge.utils.contest_grade import student_display_released
    return student_display_released(submission)


def course_filter_organizations(user):
    """Courses shown in course filters: the user's memberships (and orgs they admin).

    Users who can see every organization-private problem get the full catalog so they
    can still filter by a course they are not enrolled in.
    """
    qs = Organization.objects.order_by('name')
    if not getattr(user, 'is_authenticated', False):
        return []
    if (
        user.has_perm('judge.see_private_problem')
        or user.has_perm('judge.see_organization_problem')
        or (user.has_perm('judge.edit_own_problem') and user.has_perm('judge.edit_all_problem'))
    ):
        return list(qs)
    profile = user.profile
    member_ids = profile.organizations.values_list('id', flat=True)
    admin_ids = Organization.admins.through.objects.filter(profile=profile).values('organization_id')
    return list(qs.filter(Q(id__in=member_ids) | Q(id__in=admin_ids)).distinct())


def get_workspace_file_upload(submission):
    """Download metadata for a file-upload submission, or None."""
    if getattr(getattr(submission, 'problem', None), 'submission_type', None) != 'F':
        return None
    try:
        upload = submission.upload
    except ObjectDoesNotExist:
        return None
    if not upload:
        return None
    return {
        'url': reverse('submission_file_download', args=(submission.id,)),
        'name': upload.original_name,
        'size': upload.size,
    }


def get_workspace_answer_display(submission):
    """Human-readable answer rows for non-code Result panels (MCQ / fill-blank / short answer)."""
    from judge.question_types import get_grader

    grader = get_grader(submission.problem.submission_type)
    if grader.routes_to_bridge:
        return []
    try:
        raw = submission.source.source
    except ObjectDoesNotExist:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return [{'label': '', 'value': raw}] if raw else []

    if submission.problem.submission_type == 'M':
        options = {
            opt.id: opt
            for opt in submission.problem.mcq_options.all()
        }
        rows = []
        # Prefer submit-time snapshot (survives option recreate on edit).
        snapshot = payload.get('choices') if isinstance(payload, dict) else None
        if snapshot:
            for choice in snapshot:
                text = (choice.get('text') or '').strip()
                order = choice.get('order')
                opt_id = choice.get('id')
                live = options.get(int(opt_id)) if opt_id is not None and str(opt_id).isdigit() else None
                if not text and live is not None:
                    text = live.text
                    order = live.order
                if order is None and live is not None:
                    order = live.order
                letter = chr(ord('A') + order) if isinstance(order, int) else ''
                prefix = ('%s. ' % letter) if letter else ''
                if text:
                    rows.append({'label': '', 'value': '%s%s' % (prefix, text)})
                else:
                    rows.append({'label': '', 'value': _('(option no longer available)')})
            return rows or [{'label': '', 'value': _('(no option selected)')}]

        selected = payload.get('selected') or [] if isinstance(payload, dict) else []
        for opt_id in selected:
            opt = options.get(int(opt_id)) if str(opt_id).isdigit() or isinstance(opt_id, int) else None
            if opt is None:
                # Stale id after options were rewritten — do not show bare database ids.
                rows.append({'label': '', 'value': _('(option no longer available)')})
            else:
                letter = chr(ord('A') + opt.order) if opt.order is not None else ''
                label = ('%s. ' % letter) if letter else ''
                rows.append({'label': '', 'value': '%s%s' % (label, opt.text)})
        return rows or [{'label': '', 'value': _('(no option selected)')}]

    if submission.problem.submission_type == 'B':
        answers = payload.get('answers') or []
        blanks = list(submission.problem.blanks.order_by('order'))
        rows = []
        for idx, blank in enumerate(blanks):
            value = answers[idx] if idx < len(answers) else ''
            rows.append({
                'label': _('Blank %(n)d') % {'n': idx + 1},
                'value': value if value != '' else _('(empty)'),
            })
        if not rows and answers:
            for idx, value in enumerate(answers):
                rows.append({
                    'label': _('Blank %(n)d') % {'n': idx + 1},
                    'value': value if value != '' else _('(empty)'),
                })
        return rows

    if submission.problem.submission_type == 'S':
        text = payload.get('text', '')
        if text == '':
            return [{'label': '', 'value': _('(empty)')}]
        html = ''
        try:
            from judge.utils.markdown_preview import render_preview_html
            # Student text: always the sanitized default style, never the problem's staff or
            # full-markup style (that would let an answer inject HTML/JS into the grade desk).
            html = render_preview_html(text, 'default')
        except Exception:
            logging.getLogger('judge.problem').exception(
                'Failed to render short-answer markdown for submission %s', submission.id,
            )
        return [{'label': '', 'value': text, 'html': html}]

    return [{'label': '', 'value': raw}] if raw else []


class ProblemMixin(object):
    model = Problem
    slug_url_kwarg = 'problem'
    slug_field = 'code'

    def get_object(self, queryset=None):
        problem = super(ProblemMixin, self).get_object(queryset)
        if not problem.is_accessible_by(self.request.user):
            raise Http404()
        return problem

    def no_such_problem(self):
        code = self.kwargs.get(self.slug_url_kwarg, None)
        return generic_message(
            self.request,
            _('No such problem'),
            _('Could not find a problem with the code "%s".') % code,
            status=404,
        )

    def get(self, request, *args, **kwargs):
        try:
            return super(ProblemMixin, self).get(request, *args, **kwargs)
        except Http404:
            return self.no_such_problem()


class SolvedProblemMixin(object):
    def get_completed_problems(self):
        if self.in_contest:
            return contest_completed_ids(self.profile.current_contest)
        else:
            return user_completed_ids(self.profile) if self.profile is not None else ()

    def get_attempted_problems(self):
        if self.in_contest:
            return contest_attempted_ids(self.profile.current_contest)
        else:
            return user_attempted_ids(self.profile) if self.profile is not None else ()

    @cached_property
    def in_contest(self):
        return self.profile is not None and self.profile.current_contest is not None

    @cached_property
    def contest(self):
        return self.request.profile.current_contest.contest

    @cached_property
    def profile(self):
        if not self.request.user.is_authenticated:
            return None
        return self.request.profile


class ProblemSolution(
    SolvedProblemMixin,
    ProblemMixin,
    TitleMixin,
    CommentedDetailView,
):
    context_object_name = 'problem'
    template_name = 'problem/editorial.html'

    def get_title(self):
        return _('Editorial for {0}').format(self.object.name)

    def get_content_title(self):
        return mark_safe(
            escape(_('Editorial for {0}')).format(
                format_html(
                    '<a href="{1}">{0}</a>',
                    self.object.name,
                    reverse('problem_detail', args=[self.object.code]),
                ),
            ),
        )

    def get_context_data(self, **kwargs):
        context = super(ProblemSolution, self).get_context_data(**kwargs)

        solution = get_object_or_404(Solution, problem=self.object)

        if not solution.is_accessible_by(self.request.user) or self.request.in_contest:
            raise Http404()
        context['solution'] = solution
        context['has_solved_problem'] = self.object.id in self.get_completed_problems()
        context['enable_comments'] = settings.DMOJ_ENABLE_COMMENTS
        return context

    def get_comment_page(self):
        return 's:' + self.object.code

    def no_such_problem(self):
        code = self.kwargs.get(self.slug_url_kwarg, None)
        return generic_message(
            self.request,
            _('No such editorial'),
            _('Could not find an editorial with the code "%s".') % code,
            status=404,
        )


class ProblemDetail(ProblemMixin, SolvedProblemMixin, CommentedDetailView):
    context_object_name = 'problem'
    template_name = 'problem/workspace.html'

    def get_template_names(self):
        if self.request.GET.get('legacy') == '1':
            return ['problem/problem.html']
        return [self.template_name]

    def get_comment_page(self):
        return 'p:%s' % self.object.code

    def get(self, request, *args, **kwargs):
        try:
            return super(ProblemMixin, self).get(request, *args, **kwargs)
        except Http404:
            return self.no_such_problem()

    def _apply_assignment_editor_preview(self, context, user):
        """Drawer for Add/Edit Assignment preview links.

        The editor opens ``/problem/<code>/?assignment_preview=1&preview_codes=…``
        so the teacher sees the in-progress assignment set (answered/unanswered
        filter only), not the practice library.
        """
        if self.request.GET.get('assignment_preview') != '1':
            return
        raw_codes = self.request.GET.get('preview_codes', '')
        codes = [c.strip() for c in raw_codes.split(',') if c.strip()]
        if not codes:
            return
        names = {}
        raw_names = self.request.GET.get('preview_names', '')
        if raw_names:
            try:
                parsed = json.loads(raw_names)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                names = {str(key): str(value).strip()[:100] for key, value in parsed.items() if value}
        # The editor already chose these problems. Do not slice the practice
        # bank (``get_visible_problems()[:250]``) — that drops later codes.
        found = {
            problem.code: problem
            for problem in (
                Problem.objects.filter(code__in=codes)
                .select_related('mcq_config')
                .prefetch_related('types', 'organizations')
            )
        }
        can_attach_any = (
            user.is_authenticated
            and (
                user.has_perm('judge.edit_all_contest')
                or user.has_perm('judge.see_private_problem')
            )
        )
        ordered = []
        for index, code in enumerate(codes, 1):
            problem = found.get(code)
            if problem is None and code == self.object.code:
                problem = self.object
            if problem is None:
                continue
            if (
                problem.id != self.object.id
                and not can_attach_any
                and not problem.is_accessible_by(user)
            ):
                continue
            problem.order = index
            problem.effective_name = names.get(code) or problem.name
            ordered.append(problem)
        if not ordered:
            return
        context['workspace_assignment_preview'] = True
        context['workspace_in_contest'] = False
        context['workspace_mode'] = 'assignment'
        context['workspace_problems'] = ordered
        context['workspace_context_id'] = ''
        context['workspace_current_order'] = next(
            (item.order for item in ordered if item.code == self.object.code),
            None,
        )
        query = self.request.GET.copy()
        query.pop('legacy', None)
        query.pop('submission', None)
        context['workspace_preview_query'] = query.urlencode()
        preview_key = (self.request.GET.get('preview_key') or '').strip()
        contest = Contest.objects.filter(key=preview_key).first() if preview_key else None
        if contest is not None and contest.is_changeable_by(user):
            context['workspace_context_name'] = contest.name
            context['workspace_return_url'] = reverse('contest_edit', args=[contest.key])
        else:
            context['workspace_context_name'] = _('Assignment preview')
            context['workspace_return_url'] = (
                reverse('contest_create') if user.has_perm('judge.add_contest') else reverse('problem_list')
            )
        override = names.get(self.object.code)
        if override:
            context['title'] = override

    def _add_workspace_draft_context(self, context, participation, contest_problem):
        """Tell the workspace where to save drafts, and hand it the draft already on the server.

        The server draft wins over the browser's local copy on load: it is the one auto-submit
        will act on, and it is what the student sees after switching devices.
        """
        if contest_problem is None or participation.spectate or participation.ended:
            return
        contest = participation.contest
        if user_bypasses_assignment_rules(self.request.user, contest):
            return
        context['workspace_draft_url'] = reverse('assignment_draft', args=[contest.key, self.object.code])
        draft = AssignmentDraft.objects.filter(participation=participation, problem=self.object).first()
        if draft is None or draft.harvested_at is not None:
            return
        if draft.content:
            context['workspace_server_draft'] = {'content': draft.content, 'language': draft.language_id}
        upload = getattr(draft, 'upload', None)
        if upload is not None:
            # A file input cannot be prefilled, so without this the page looks exactly like one
            # where nothing was ever uploaded — and a student who cannot see their saved file has
            # no reason to believe it will be submitted.
            context['workspace_draft_upload'] = {'name': upload.original_name, 'size': upload.size}

    def get_context_data(self, **kwargs):
        context = super(ProblemDetail, self).get_context_data(**kwargs)
        context['workspace_draft_url'] = None
        context['workspace_server_draft'] = None
        context['workspace_draft_upload'] = None
        user = self.request.user
        authed = user.is_authenticated
        can_see_authors = user_can_see_problem_author_metadata(user)
        context['can_see_problem_authors'] = can_see_authors
        context['has_submissions'] = (
            authed and Submission.objects.filter(user=user.profile, problem=self.object).exists()
        )
        contest_problem = (
            None
            if not authed or user.profile.current_contest is None
            else get_contest_problem(self.object, user.profile)
        )
        context['contest_problem'] = contest_problem
        context['workspace_assignment_preview'] = False
        context['workspace_preview_query'] = ''
        # Drawer list follows the active participation, not whether *this* problem
        # happens to be in the assignment. After Join, the solving page always
        # shows the assignment problem set.
        current_participation = user.profile.current_contest if authed else None
        workspace_in_contest = current_participation is not None
        context['workspace_in_contest'] = workspace_in_contest
        # Whether the submit area may promise auto-submit. Asking the harvest itself keeps the
        # promise from outliving the behaviour for spectators, disqualified students and staff.
        context['workspace_autosubmit'] = (
            workspace_in_contest and participation_is_harvestable(current_participation)
            and assignment_harvest_enabled()
        )
        if workspace_in_contest:
            contest = current_participation.contest
            if contest.format_class.has_hidden_results:
                context['workspace_mode'] = 'exam'
            else:
                context['workspace_mode'] = 'assignment'
            context['workspace_context_id'] = contest.key
            contest_prefetch = ['problem__types', 'problem__organizations']
            if can_see_authors:
                contest_prefetch.append('problem__authors__user')
            context['workspace_problems'] = list(ContestProblem.objects.filter(
                contest=contest,
            ).select_related('problem', 'problem__mcq_config').prefetch_related(
                *contest_prefetch,
            ).order_by('order'))
            lang = self.request.LANGUAGE_CODE
            for cp in context['workspace_problems']:
                cp.effective_name = contest_problem_display_name(cp, lang)
            context['workspace_context_name'] = contest.name
            context['workspace_return_url'] = reverse('contest_view', args=[contest.key])
            context['workspace_current_order'] = contest_problem.order if contest_problem else None
            self._add_workspace_draft_context(context, current_participation, contest_problem)
        else:
            context['workspace_mode'] = 'practice'
            context['workspace_context_id'] = ''
            practice_prefetch = ['types', 'organizations']
            if can_see_authors:
                practice_prefetch.append('authors__user')
            context['workspace_problems'] = (
                Problem.get_visible_problems(user)
                .select_related('mcq_config')
                .prefetch_related(*practice_prefetch)
                .order_by('code')[:250]
            )
            context['workspace_context_name'] = _('Problem list')
            context['workspace_return_url'] = reverse('problem_list')
            context['workspace_current_order'] = None
        self._apply_assignment_editor_preview(context, user)
        if contest_problem:
            clarifications = self.object.clarifications
            context['has_clarifications'] = clarifications.count() > 0
            context['clarifications'] = clarifications.order_by('-date')
            context['submission_limit'] = contest_problem.max_submissions
            if contest_problem.max_submissions:
                context['submissions_left'] = max(
                    contest_problem.max_submissions
                    - get_contest_submission_count(self.object, user.profile, user.profile.current_contest.virtual),
                    0,
                )

        context['available_judges'] = Judge.objects.filter(online=True, problems=self.object)
        context['show_languages'] = self.object.allowed_languages.count() != Language.objects.count()
        context['has_pdf_render'] = PDF_RENDERING_ENABLED
        context['completed_problem_ids'] = self.get_completed_problems()
        context['attempted_problems'] = self.get_attempted_problems()
        if authed:
            context['editable_problem_ids'] = user_editable_ids(user.profile)
            context['tester_problem_ids'] = user_tester_ids(user.profile)
        else:
            context['editable_problem_ids'] = set()
            context['tester_problem_ids'] = set()

        can_edit = self.object.is_editable_by(user)
        context['can_edit_problem'] = can_edit
        context['can_clone_problem'] = (
            user.is_authenticated
            and (
                user.has_perm('judge.clone_problem')
                or self.object.is_viewable_by_org_admin(user)
            )
        )
        if user.is_authenticated:
            tickets = self.object.tickets
            if not can_edit:
                tickets = tickets.filter(own_ticket_filter(user.profile.id))
            context['has_tickets'] = tickets.exists()
            context['num_open_tickets'] = tickets.filter(is_open=True).values('id').distinct().count()

        try:
            context['editorial'] = Solution.objects.get(problem=self.object)
        except ObjectDoesNotExist:
            pass
        try:
            translation = self.object.translations.get(language=self.request.LANGUAGE_CODE)
        except ProblemTranslation.DoesNotExist:
            context['title'] = self.object.name
            context['language'] = settings.LANGUAGE_CODE
            context['description'] = self.object.description
            context['translated'] = False
        else:
            context['title'] = translation.name
            context['language'] = self.request.LANGUAGE_CODE
            context['description'] = translation.description
            context['translated'] = True

        if contest_problem:
            context['title'] = contest_problem_display_name(
                contest_problem, self.request.LANGUAGE_CODE,
            )
        if context.get('workspace_assignment_preview'):
            for item in context.get('workspace_problems') or []:
                if getattr(item, 'code', None) == self.object.code and getattr(item, 'effective_name', None):
                    context['title'] = item.effective_name
                    break

        if not self.object.og_image or not self.object.summary:
            metadata = generate_opengraph(
                'generated-meta-problem:%s:%d' % (context['language'], self.object.id),
                context['description'],
                'problem',
            )
        context['meta_description'] = self.object.summary or metadata[0]
        context['og_image'] = self.object.og_image or metadata[1]
        context['enable_comments'] = settings.DMOJ_ENABLE_COMMENTS

        context['vote_perm'] = self.object.vote_permission_for_user(user)
        if context['vote_perm'].can_vote():
            try:
                context['vote'] = ProblemPointsVote.objects.get(voter=user.profile, problem=self.object)
            except ObjectDoesNotExist:
                context['vote'] = None
        else:
            context['vote'] = None

        if contest_problem:
            cp_qs = ContestProblem.objects.filter(
                contest=contest_problem.contest,
            ).select_related('problem')
            context['next_contest_problem'] = cp_qs.filter(order__gt=contest_problem.order).order_by('order').first()
            context['prev_contest_problem'] = cp_qs.filter(order__lt=contest_problem.order).order_by('-order').first()
            lang = self.request.LANGUAGE_CODE
            for nav in (context['next_contest_problem'], context['prev_contest_problem']):
                if nav is not None:
                    nav.effective_name = contest_problem_display_name(nav, lang)

        context['submission_type'] = self.object.submission_type
        from judge.models import ProblemSubmissionType
        from judge.question_types import all_types, get_grader
        workspace_grader = get_grader(self.object.submission_type)
        context['workspace_is_code'] = workspace_grader.routes_to_bridge
        context['workspace_is_file'] = self.object.submission_type == ProblemSubmissionType.FILE
        context['workspace_is_mcq'] = self.object.submission_type == ProblemSubmissionType.MCQ
        try:
            context['workspace_file_config'] = (
                self.object.file_config if context['workspace_is_file'] else None
            )
        except ObjectDoesNotExist:
            context['workspace_file_config'] = None
        context['workspace_submission_type_label'] = str(workspace_grader.label)
        # Match problem-list redesign filters: one option per submission_type (MCQ = choice question).
        workspace_format_options = []
        from judge.subjects import filter_question_types
        for grader in filter_question_types(user, all_types()):
            if grader.submission_type == ProblemSubmissionType.MCQ:
                workspace_format_options.append({
                    'code': ProblemSubmissionType.MCQ, 'label': str(_('Choice question')),
                })
            else:
                workspace_format_options.append({
                    'code': grader.submission_type, 'label': str(grader.label),
                })
        context['workspace_format_options'] = workspace_format_options
        context['workspace_difficulty_options'] = [
            {'code': name, 'label': Problem.difficulty_display(name)}
            for name in Problem.DIFFICULTY_TYPE_NAMES
        ]
        context['workspace_organizations'] = course_filter_organizations(user)
        reserved_kp = Problem.reserved_auto_type_labels()
        context['workspace_knowledge_types'] = [
            t for t in ProblemType.objects.order_by('full_name')
            if Problem.is_knowledge_problem_type(t, reserved_labels=reserved_kp)
        ]
        context['workspace_attempt_counts'] = {}
        context['workspace_best_points'] = {}
        context['workspace_exam_hide_scores'] = False
        if authed:
            if workspace_in_contest:
                contest = current_participation.contest
                context['workspace_exam_hide_scores'] = (
                    contest.format_class.has_hidden_results and not contest.results_finalized
                )
                drawer_problem_ids = [cp.problem_id for cp in context['workspace_problems']]
                # Exam scoreboard uses latest for hidden types; assignment (and exam code) uses best.
                latest_ids = set()
                if contest.format_class.has_hidden_results:
                    latest_ids = {
                        cp.problem_id for cp in context['workspace_problems'] if cp.score_uses_latest
                    }
                if drawer_problem_ids:
                    counts, best, latest = {}, {}, {}
                    for row in (
                        ContestSubmission.objects.filter(
                            participation=self.request.participation,
                            problem__problem_id__in=drawer_problem_ids,
                        )
                        .values('problem__problem_id', 'points', 'submission__date', 'id')
                        .order_by('submission__date', 'id')
                    ):
                        pid = row['problem__problem_id']
                        counts[pid] = counts.get(pid, 0) + 1
                        pts = row['points']
                        if pid not in best or pts > best[pid]:
                            best[pid] = pts
                        latest[pid] = pts
                    for pid, n in counts.items():
                        context['workspace_attempt_counts'][pid] = n
                        context['workspace_best_points'][pid] = (
                            latest[pid] if pid in latest_ids else best[pid]
                        )
            else:
                drawer_problem_ids = [p.id for p in context['workspace_problems']]
                if drawer_problem_ids:
                    for row in (
                        Submission.objects.filter(user=user.profile, problem_id__in=drawer_problem_ids)
                        .values('problem_id')
                        .annotate(c=Count('id'), p=Max('points'))
                    ):
                        context['workspace_attempt_counts'][row['problem_id']] = row['c']
                        context['workspace_best_points'][row['problem_id']] = row['p']
        # Assignment workspace always shows the participation clock: Duration when
        # the contest has time_limit, otherwise Validity. Both are time_remaining.
        context['workspace_show_timer'] = bool(workspace_in_contest)
        context['workspace_timer_seconds'] = 0
        context['workspace_timer_window_seconds'] = 0
        context['workspace_timer_is_duration'] = False
        context['workspace_timer_remaining'] = None
        if workspace_in_contest:
            context['workspace_timer_is_duration'] = current_participation.contest.time_limit is not None
            start = current_participation.start
            end = current_participation.end_time
            if start is not None and end is not None:
                context['workspace_timer_window_seconds'] = max(0, int((end - start).total_seconds()))
            remaining = current_participation.time_remaining
            context['workspace_timer_remaining'] = remaining
            context['workspace_timer_seconds'] = (
                max(0, int(remaining.total_seconds())) if remaining is not None else 0
            )
        context['workspace_participation_id'] = (
            current_participation.pk if workspace_in_contest else ''
        )
        context['workspace_manual_submit_notice'] = bool(
            workspace_in_contest
            and not current_participation.spectate
            and not assignment_harvest_enabled(),
        )
        context['workspace_submission'] = None
        context['workspace_enable_comments'] = False
        selected_submission_id = self.request.GET.get('submission')
        if authed and selected_submission_id and selected_submission_id.isdigit():
            selected_qs = Submission.objects.select_related(
                'source', 'upload', 'problem', 'contest_object', 'contest__problem',
            ).prefetch_related('problem__mcq_options', 'problem__blanks', 'manual_grade').filter(
                id=int(selected_submission_id), problem=self.object,
            )
            # Assignment/exam: only allow loading submissions from the active contest.
            if workspace_in_contest:
                selected_qs = selected_qs.filter(contest_object=current_participation.contest)
            selected_submission = selected_qs.first()
            if selected_submission is not None and selected_submission.can_see_detail(user):
                selected_source = None
                if selected_submission.user_id == self.request.profile.id:
                    try:
                        selected_source = selected_submission.source.source
                    except ObjectDoesNotExist:
                        pass
                results_hidden = not selected_submission.results_visible_to(user)
                display_points = workspace_display_points(selected_submission)
                context['workspace_submission'] = {
                    'id': selected_submission.id,
                    'result': None if results_hidden else selected_submission.result,
                    'status': selected_submission.status,
                    'result_class': None if results_hidden else selected_submission.result_class,
                    'long_status': None if results_hidden else selected_submission.long_status,
                    'points': None if results_hidden else display_points,
                    'points_total': workspace_display_points_total(selected_submission),
                    'time': None if results_hidden else selected_submission.time,
                    'memory': None if results_hidden else selected_submission.memory,
                    'source': selected_source,
                    'answer_display': (
                        get_workspace_answer_display(selected_submission)
                        if selected_source is not None else []
                    ),
                    'file_upload': get_workspace_file_upload(selected_submission),
                    'results_hidden': results_hidden,
                    'is_graded': selected_submission.is_graded,
                    # Score and 评语 stay masked with the exam result until finalize.
                    'released': False if results_hidden else workspace_manual_released(selected_submission),
                    'review_note_html': '' if results_hidden else workspace_review_note_html(selected_submission),
                    'id_secret': selected_submission.id_secret,
                    'status_url': reverse(
                        'problem_workspace_submission_status',
                        args=(self.object.code, selected_submission.id),
                    ),
                    **({} if results_hidden else workspace_ai_grade_fields(self.request, selected_submission)),
                }
                # Result-tab comments = submission page (sub:ID), gated by DMOJ_ENABLE_COMMENTS + CommentLock.
                if settings.DMOJ_ENABLE_COMMENTS:
                    comment_handler = SubmissionCommentMixin()
                    comment_handler.object = selected_submission
                    comment_handler.request = self.request
                    comment_handler.comment_use_rich_editor = True
                    comment_ctx = comment_handler.get_comment_context()
                    context['workspace_enable_comments'] = True
                    context['comment_use_rich_editor'] = True
                    context['has_comments'] = comment_ctx['has_comments']
                    context['comment_lock'] = comment_ctx['comment_lock']
                    context['comment_list'] = comment_ctx['comment_list']
                    context['vote_hide_threshold'] = comment_ctx['vote_hide_threshold']
                    context['reply_cutoff'] = comment_ctx['reply_cutoff']
                    context['comment_form'] = comment_ctx['comment_form']
                    context['is_new_user'] = comment_ctx.get('is_new_user', False)
                    context['comment_form_action'] = '%s?submission=%d' % (
                        reverse('problem_detail', args=[self.object.code]),
                        selected_submission.id,
                    )
                context['last_msg'] = event.last()
        if authed:
            workspace_subs_qs = Submission.objects.filter(user=self.request.profile, problem=self.object)
            if workspace_in_contest:
                workspace_subs_qs = workspace_subs_qs.filter(contest_object=current_participation.contest)
            workspace_subs = list(
                workspace_subs_qs
                .select_related('language', 'contest_object', 'contest__problem')
                .prefetch_related('manual_grade')
                .order_by('-date')[:20],
            )
            from judge.utils.contest_grade import attach_student_display_score
            for submission in workspace_subs:
                attach_student_display_score(submission)
            context['workspace_submissions'] = workspace_subs
        else:
            context['workspace_submissions'] = []
        from judge import ai_hints as ai_hints_mod
        hint_contest = (
            current_participation.contest
            if contest_problem is not None and current_participation is not None
            else None
        )
        # Not offered while this problem's verdicts are hidden from the user: the
        # hint panel unlocking only after a non-AC result would reveal the verdict.
        context['ai_hints_possible'] = bool(
            ai_hints_mod.hints_enabled_for(self.object, hint_contest, user)
            and not ai_hints_mod.results_hidden_in_contest_problem(
                contest_problem if hint_contest is not None else None, user)
            and ai_hints_mod.is_configured(),
        )
        context['ai_hints_remaining'] = (
            ai_hints_mod.get_remaining(self.request.profile.id)
            if authed and context['ai_hints_possible'] else 0
        )
        if self.request.user.is_authenticated:
            from judge.question_types import get_grader
            grader = get_grader(self.object.submission_type)
            if grader.routes_to_bridge:
                form = ProblemSubmitForm(
                    initial={'language': self.request.profile.language},
                    instance=Submission(user=self.request.profile, problem=self.object),
                )
                if self.object.is_editable_by(self.request.user):
                    form.fields['judge'].choices = tuple(
                        Judge.objects.filter(online=True, problems=self.object).values_list('name', 'name'),
                    )
                form.fields['language'].queryset = self.object.usable_languages.order_by(
                    'name', 'key').prefetch_related(
                    Prefetch('runtimeversion_set', RuntimeVersion.objects.order_by('priority')),
                )
                form.fields['source'].widget.theme = self.request.profile.resolved_ace_theme
                # Set ACE mode to user's language only if it's in the allowed languages
                # Otherwise use the first allowed language
                usable_languages = form.fields['language'].queryset
                if self.request.profile.language and self.request.profile.language in usable_languages:
                    selected_language = self.request.profile.language
                else:
                    selected_language = usable_languages.first()
                if selected_language:
                    form.fields['source'].widget.mode = selected_language.ace
                    form.initial['language'] = selected_language
                templates = ProblemTemplate.starter_map(self.object, usable_languages)
                context['workspace_language_templates'] = templates
                if selected_language:
                    form.initial['source'] = templates.get(str(selected_language.id), '')
                context['form'] = form
                context['ACE_URL'] = ace_url()
            else:
                # Non-code (e.g. MCQ): render the type-specific answer form inline, not the Ace editor.
                # The inline form posts to the dedicated submit page (submit_url).
                context['form'] = grader.build_form(self.object, self.request.profile)
                context['submit_url'] = reverse('problem_submit', args=[self.object.code])

        return context

    def post(self, request, *args, **kwargs):
        """Post submission comments from the Result tab.

        Aligns with SubmissionStatus + SubmissionCommentMixin (page sub:ID).
        Falls back to problem-page comments (CommentedDetailView) otherwise.
        """
        self.object = self.get_object()
        sid = request.GET.get('submission') or request.POST.get('workspace_comment_submission')
        if (
            settings.DMOJ_ENABLE_COMMENTS
            and sid
            and str(sid).isdigit()
            and 'body' in request.POST
        ):
            submission_qs = Submission.objects.filter(id=int(sid), problem=self.object)
            contest_problem = (
                get_contest_problem(self.object, request.profile)
                if request.user.is_authenticated else None
            )
            if contest_problem:
                submission_qs = submission_qs.filter(contest_object=contest_problem.contest)
            submission = submission_qs.first()
            if submission is not None and submission.can_see_detail(request.user):
                handler = SubmissionCommentMixin()
                handler.object = submission
                handler.request = request
                response, form = handler.handle_comment_post(request)
                if response is not None:
                    if isinstance(response, HttpResponseRedirect):
                        return HttpResponseRedirect(
                            '%s?submission=%d' % (
                                reverse('problem_detail', args=[self.object.code]),
                                submission.id,
                            ),
                        )
                    return response
                context = self.get_context_data(object=self.object)
                context['comment_form'] = form
                context['workspace_enable_comments'] = True
                return self.render_to_response(context)
        return super(ProblemDetail, self).post(request, *args, **kwargs)


class ProblemVote(ProblemMixin, DetailView):
    context_object_name = 'problem'
    template_name = 'problem/vote-ajax.html'

    def get_context_data(self, **kwargs):
        if not self.object.vote_permission_for_user(self.request.user).can_vote():
            raise Http404()

        context = super().get_context_data(**kwargs)

        try:
            context['vote'] = ProblemPointsVote.objects.get(voter=self.request.profile, problem=self.object)
        except ObjectDoesNotExist:
            context['vote'] = None

        context['max_possible_vote'] = settings.DMOJ_PROBLEM_MAX_USER_POINTS_VOTE
        context['min_possible_vote'] = settings.DMOJ_PROBLEM_MIN_USER_POINTS_VOTE
        return context

    def post(self, request, *args, **kwargs):
        problem = self.get_object()
        if not problem.vote_permission_for_user(request.user).can_vote():
            return JsonResponse({'message': _('Not allowed to vote on this problem.')}, status=403)

        form = ProblemPointsVoteForm(request.POST)
        if not form.is_valid():
            return JsonResponse(form.errors, status=400)

        with transaction.atomic():
            # Delete any pre-existing votes.
            ProblemPointsVote.objects.filter(voter=request.profile, problem=problem).delete()
            vote = form.save(commit=False)
            vote.voter = request.profile
            vote.problem = problem
            vote.save()

        return JsonResponse({'points': vote.points})


class DeleteProblemVote(ProblemMixin, SingleObjectMixin, View):
    http_method_names = [
        'options',
        'post',
    ]  # This disables GET requests, even though ProblemMixin.get exists.

    def post(self, request, *args, **kwargs):
        problem = self.get_object()
        if not problem.vote_permission_for_user(request.user).can_vote():
            return JsonResponse(
                {'message': _('Not allowed to delete votes on this problem.')},
                status=403,
            )

        ProblemPointsVote.objects.filter(voter=request.profile, problem=problem).delete()
        return JsonResponse({'message': _('success')})


class ProblemVoteStats(ProblemMixin, DetailView):
    context_object_name = 'problem'
    template_name = 'problem/vote-stats-ajax.html'

    def get_context_data(self, **kwargs):
        if not self.object.vote_permission_for_user(self.request.user).can_view():
            raise Http404()

        context = super().get_context_data(**kwargs)

        votes = list(self.object.problem_points_votes.order_by('points').values_list('points', flat=True))
        context['votes'] = votes

        if votes:
            context['mean'] = mean(votes)
            context['median'] = median(votes)

        context['max_possible_vote'] = settings.DMOJ_PROBLEM_MAX_USER_POINTS_VOTE
        context['min_possible_vote'] = settings.DMOJ_PROBLEM_MIN_USER_POINTS_VOTE
        return context


class LatexError(Exception):
    pass


class ProblemPdfView(ProblemMixin, SingleObjectMixin, View):
    logger = logging.getLogger('judge.problem.pdf')
    languages = set(map(itemgetter(0), settings.LANGUAGES))

    def get(self, request, *args, **kwargs):
        if not PDF_RENDERING_ENABLED:
            raise Http404()

        language = kwargs.get('language', self.request.LANGUAGE_CODE)
        if language not in self.languages:
            raise Http404()

        problem = self.get_object()
        pdf_basename = '%s.%s.pdf' % (problem.code, language)

        def render_problem_pdf():
            self.logger.info('Rendering PDF in %s: %s', language, problem.code)

            with translation.override(language):
                try:
                    trans = problem.translations.get(language=language)
                except ProblemTranslation.DoesNotExist:
                    trans = None

                problem_name = trans.name if trans else problem.name
                return render_pdf(
                    html=get_template('problem/raw.html')
                    .render(
                        {
                            'problem': problem,
                            'problem_name': problem_name,
                            'description': (trans.description if trans else problem.description),
                            'url': request.build_absolute_uri(),
                        },
                    )
                    .replace('"//', '"https://')
                    .replace("'//", "'https://"),
                    title=problem_name,
                )

        response = HttpResponse()
        response['Content-Type'] = 'application/pdf'
        response['Content-Disposition'] = f'inline; filename={pdf_basename}'

        if settings.DMOJ_PDF_PROBLEM_CACHE:
            pdf_filename = os.path.join(settings.DMOJ_PDF_PROBLEM_CACHE, pdf_basename)
            if not os.path.exists(pdf_filename):
                with open(pdf_filename, 'wb') as f:
                    f.write(render_problem_pdf())

            if settings.DMOJ_PDF_PROBLEM_INTERNAL:
                url_path = f'{settings.DMOJ_PDF_PROBLEM_INTERNAL}/{pdf_basename}'
            else:
                url_path = None

            add_file_response(request, response, url_path, pdf_filename)
        else:
            response.content = render_problem_pdf()

        return response


class ProblemList(QueryStringSortMixin, TitleMixin, SolvedProblemMixin, ListView):
    model = Problem
    title = gettext_lazy('Problems')
    context_object_name = 'problems'
    template_name = 'problem/list.html'
    paginate_by = 20
    # Tags picker: show the most-used knowledge points first, then the rest behind
    # "Show more". Keep this small enough that the default chip row stays scannable.
    KNOWLEDGE_PICKER_HOT_COUNT = 12
    sql_sort = frozenset(('points', 'ac_rate', 'user_count', 'code', 'date'))
    manual_sort = frozenset(('name', 'solved', 'type', 'editorial'))
    all_sorts = sql_sort | manual_sort
    default_desc = frozenset(('points', 'ac_rate', 'user_count', 'date'))
    default_sort = 'code'

    def get_paginator(self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs):
        paginator = DiggPaginator(
            queryset,
            per_page,
            body=6,
            padding=2,
            orphans=orphans,
            count=queryset.values('pk').count() if not self.in_contest else None,
            allow_empty_first_page=allow_empty_first_page,
            **kwargs,
        )
        if not self.in_contest:
            queryset = queryset.add_i18n_name(self.request.LANGUAGE_CODE)
            sort_key = self.order.lstrip('-')
            if sort_key in self.sql_sort:
                queryset = queryset.order_by(self.order, 'id')
            elif sort_key == 'name':
                queryset = queryset.order_by(self.order.replace('name', 'i18n_name'), 'id')
            elif sort_key == 'editorial':
                queryset = queryset.order_by(self.order.replace('editorial', 'has_public_editorial'), 'id')
            elif sort_key == 'solved':
                if self.request.user.is_authenticated:
                    profile = self.request.profile
                    solved = user_completed_ids(profile)
                    attempted = user_attempted_ids(profile)

                    def _solved_sort_order(problem):
                        if problem.id in solved:
                            return 1
                        if problem.id in attempted:
                            return 0
                        return -1

                    queryset = list(queryset)
                    queryset.sort(key=_solved_sort_order, reverse=self.order.startswith('-'))
            elif sort_key == 'type':
                if self.show_types:
                    queryset = list(queryset)
                    queryset.sort(
                        key=lambda problem: (problem.types_list[0] if problem.types_list else ''),
                        reverse=self.order.startswith('-'),
                    )
            paginator.object_list = queryset
        return paginator

    @cached_property
    def profile(self):
        if not self.request.user.is_authenticated:
            return None
        return self.request.profile

    def get_title(self):
        if self.in_contest:
            return _('%(assignment)s — Problems') % {'assignment': self.contest.name}
        return super().get_title()

    def get_content_title(self):
        if self.in_contest:
            return self.contest.name
        return super().get_content_title()

    def get_contest_queryset(self):
        participation = self.profile.current_contest
        contest = participation.contest
        stats = assignment_problem_stats(contest, self.request.user)

        queryset = (
            contest.contest_problems.select_related('problem')
            .defer('problem__description')
            .annotate(
                i18n_translation=FilteredRelation(
                    'problem__translations',
                    condition=Q(problem__translations__language=self.request.LANGUAGE_CODE),
                ),
            )
            .annotate(
                i18n_name=Coalesce(
                    F('i18n_translation__name'),
                    F('problem__name'),
                    output_field=CharField(),
                ),
            )
            .order_by('order')
        )

        result = []
        for cp in queryset:
            try:
                user_problem_cell = contest.format.display_user_problem(participation, cp)
            except (KeyError, TypeError, ValueError):
                user_problem_cell = mark_safe('<td>???</td>')

            problem_stats = stats.get(cp.problem_id, {'user_count': 0, 'ac_rate': 0.0})
            effective_name = contest_problem_display_name(cp, self.request.LANGUAGE_CODE)
            result.append({
                'id': cp.problem_id,
                'code': cp.problem.code,
                'name': cp.problem.name,
                'i18n_name': effective_name,
                'effective_name': effective_name,
                'points': cp.points,
                'partial': cp.partial,
                'user_count': problem_stats['user_count'],
                'ac_rate': problem_stats['ac_rate'],
                'user_problem_cell': user_problem_cell,
                'submission_type': cp.problem.submission_type,
            })

        return result

    @staticmethod
    def apply_full_text(queryset, query):
        if recjk.search(query):
            # MariaDB can't tokenize CJK properly, fallback to LIKE '%term%' for each term.
            for term in query.split():
                queryset = queryset.filter(
                    Q(code__icontains=term) | Q(name__icontains=term) | Q(description__icontains=term),
                )
            return queryset
        return queryset.search(query, queryset.BOOLEAN).extra(order_by=['-relevance'])

    def _problem_type_counts(self):
        """Map ProblemType id → distinct visible-problem count for picker badges."""
        base_visible = Problem.objects.filter(self.get_visible_problems_filter())
        return {
            row['types']: row['count']
            for row in base_visible.values('types').annotate(count=Count('pk', distinct=True))
            if row['types'] is not None
        }

    def get_visible_problems_filter(self):
        user = self.request.user
        # Users who may see private problems (or edit everything) get the full set on the front-end
        # list too — mirroring Problem.get_visible_problems and the Django admin. Without this an
        # admin who did not author a private problem could not find it here (nor filter by its
        # group / type), even though it shows up in the admin site.
        if user.is_authenticated and (
            user.has_perm('judge.see_private_problem')
            or (user.has_perm('judge.edit_own_problem') and user.has_perm('judge.edit_all_problem'))
        ):
            return Q()
        filter = Q(is_public=True)
        if not self.request.user.has_perm('judge.see_organization_problem'):
            org_filter = Q(is_organization_private=False)
            if self.profile is not None:
                # If classes are set, check class membership; otherwise check org membership
                org_filter |= Q(is_organization_private=True, classes__isnull=True,
                                organizations__in=self.profile.organizations.all())
                org_filter |= Q(is_organization_private=True,
                                classes__in=self.profile.classes.all())
            filter &= org_filter
        if self.profile is not None:
            filter = Problem.q_add_author_curator_tester(filter, self.profile)
            # Org admins can see all org-private problems
            if self.request.user.has_perm('judge.edit_own_problem'):
                filter |= Problem.organization_filter_q(
                    Organization.admins.through.objects.filter(profile=self.profile).values('organization_id'),
                )
        return filter

    def get_course_filter_organizations(self):
        """Courses shown in the list filter: the user's memberships (and orgs they admin).

        Users who can see every organization-private problem get the full catalog so they
        can still filter by a course they are not enrolled in.
        """
        user = self.request.user
        qs = Organization.objects.order_by('name')
        if not user.is_authenticated:
            return []
        if (
            user.has_perm('judge.see_private_problem')
            or user.has_perm('judge.see_organization_problem')
            or (user.has_perm('judge.edit_own_problem') and user.has_perm('judge.edit_all_problem'))
        ):
            return list(qs)
        profile = user.profile
        member_ids = profile.organizations.values_list('id', flat=True)
        admin_ids = Organization.admins.through.objects.filter(profile=profile).values('organization_id')
        return list(qs.filter(Q(id__in=member_ids) | Q(id__in=admin_ids)).distinct())

    def get_format_options(self):
        """Answer-format filter options as ``(code, label, Q)`` tuples.

        MCQ is one ``submission_type`` covering single choice, multiple choice, and true/false;
        the list filter exposes it as a single "Choice question" option.
        """
        from judge.models import ProblemSubmissionType
        from judge.question_types import all_types
        from judge.subjects import filter_question_types
        options = []
        # Subject registry: e.g. no "Programming" filter for users whose courses are all non-code subjects.
        for t in filter_question_types(self.request.user, all_types()):
            if t.submission_type == ProblemSubmissionType.MCQ:
                options.append((ProblemSubmissionType.MCQ, str(_('Choice question')),
                                Q(submission_type=ProblemSubmissionType.MCQ)))
            else:
                options.append((t.submission_type, str(t.label), Q(submission_type=t.submission_type)))
        return options

    def get_normal_queryset(self):
        filter = self.get_visible_problems_filter()
        queryset = (Problem.objects.filter(filter).select_related('group', 'mcq_config')
                    .defer('description', 'summary'))
        if self.profile is not None and self.hide_solved:
            # A hidden exam verdict must not decide whether the problem disappears (G-1).
            queryset = queryset.exclude(
                id__in=Submission.objects.filter(
                    user=self.profile,
                    result='AC',
                    case_points__gte=F('case_total'),
                ).exclude(results_hidden_q()).values_list('problem_id', flat=True),
            )
        if self.profile is not None and self.problem_status:
            completed = self.get_completed_problems()
            if self.problem_status == 'solved':
                queryset = queryset.filter(id__in=completed)
            elif self.problem_status == 'unsolved':
                queryset = queryset.exclude(id__in=completed)
        if self.show_types:
            queryset = queryset.prefetch_related('types')
        queryset = queryset.annotate(
            has_public_editorial=Case(
                When(
                    solution__is_public=True,
                    solution__publish_on__lte=timezone.now(),
                    then=True,
                ),
                default=False,
                output_field=BooleanField(),
            ),
        )
        if self.has_public_editorial:
            queryset = queryset.filter(has_public_editorial=True)
        if self.category is not None:
            queryset = queryset.filter(group__id=self.category)
        if self.selected_organizations:
            # Multi-select within Course is OR. No course selected keeps the
            # visible set unchanged (public problems included).
            queryset = queryset.filter(organizations__in=self.selected_organizations)
        if self.selected_types:
            # Within a filter dimension multi-select is OR; dimensions combine with AND.
            # Difficulty and tags/knowledge points both use OR within their selected ids.
            difficulty_ids = set(
                ProblemType.objects.filter(Problem.difficulty_type_q()).values_list('id', flat=True),
            )
            selected_difficulty = [t for t in self.selected_types if t in difficulty_ids]
            selected_other = [t for t in self.selected_types if t not in difficulty_ids]
            if selected_other:
                queryset = queryset.filter(types__in=selected_other)
            if selected_difficulty:
                queryset = queryset.filter(types__in=selected_difficulty)
        if self.selected_formats:
            qmap = {code: q for code, label, q in self.get_format_options()}
            fmt_q = Q()
            for code in self.selected_formats:
                if code in qmap:
                    fmt_q |= qmap[code]
            if fmt_q:
                queryset = queryset.filter(fmt_q)
        self.search_terms = [t.strip() for t in self.request.GET.getlist('search') if t.strip()]
        self.search_query = ' '.join(self.search_terms) or None
        for term in self.search_terms:
            if settings.ENABLE_FTS and self.full_text:
                queryset = self.apply_full_text(queryset, term)
                continue
            text_filter = (
                Q(code__icontains=term)
                | Q(name__icontains=term)
                | Q(
                    translations__name__icontains=term,
                    translations__language=self.request.LANGUAGE_CODE,
                )
            )
            if self.search_full:
                text_filter |= Q(description__icontains=term)
            queryset = queryset.filter(text_filter)
        self.prepoint_queryset = queryset
        if self.point_start is not None:
            queryset = queryset.filter(points__gte=self.point_start)
        if self.point_end is not None:
            queryset = queryset.filter(points__lte=self.point_end)
        if self.ac_min is not None:
            queryset = queryset.filter(ac_rate__gte=self.ac_min)
        if self.ac_max is not None:
            queryset = queryset.filter(ac_rate__lte=self.ac_max)
        if self.selected_authors:
            queryset = queryset.filter(authors__in=self.selected_authors)
        queryset = queryset.distinct()
        if user_can_see_problem_author_metadata(self.request.user):
            queryset = queryset.prefetch_related('authors__user')
        return queryset

    def get_queryset(self):
        if self.in_contest:
            return self.get_contest_queryset()
        else:
            return self.get_normal_queryset()

    def get_context_data(self, **kwargs):
        context = super(ProblemList, self).get_context_data(**kwargs)
        context['hide_solved'] = 0 if self.in_contest else int(self.hide_solved)
        context['show_types'] = 0 if self.in_contest else int(self.show_types)
        context['has_public_editorial'] = 0 if self.in_contest else int(self.has_public_editorial)
        context['full_text'] = 0 if self.in_contest else int(self.full_text)
        context['category'] = self.category
        context['categories'] = ProblemGroup.objects.all()
        context['organizations'] = self.get_course_filter_organizations()
        context['selected_organizations'] = self.selected_organizations
        can_see_authors = user_can_see_problem_author_metadata(self.request.user)
        context['can_see_problem_authors'] = can_see_authors
        context['selected_authors'] = self.selected_authors if can_see_authors else []
        if can_see_authors and self.selected_authors:
            author_map = {
                profile.id: profile
                for profile in Profile.objects.filter(pk__in=self.selected_authors).select_related('user')
            }
            context['selected_author_profiles'] = [
                author_map[pk] for pk in self.selected_authors if pk in author_map
            ]
        else:
            context['selected_author_profiles'] = []
        if self.show_types:
            # Tags (knowledge points) come from the ProblemType taxonomy.
            all_problem_types = list(ProblemType.objects.all())
            context['problem_types'] = all_problem_types
            context['selected_types'] = self.selected_types
            # Difficulty is a dedicated dimension surfaced from the difficulty-named ProblemTypes,
            # ordered easiest -> hardest.
            difficulty_order = {name: i for i, name in enumerate(Problem.DIFFICULTY_TYPE_NAMES)}
            difficulty_types = sorted(
                (t for t in all_problem_types if Problem.is_difficulty_problem_type(t)),
                key=lambda t: difficulty_order.get(
                    Problem.canonical_difficulty_name(t.name) or Problem.canonical_difficulty_name(t.full_name),
                    len(difficulty_order),
                ),
            )
            # Knowledge points: exclude format/difficulty rows and leftover auto-chip ProblemTypes.
            reserved_labels = Problem.reserved_auto_type_labels()
            knowledge_types = [
                t for t in all_problem_types
                if Problem.is_knowledge_problem_type(t, reserved_labels=reserved_labels)
            ]
            # Badge counts + default order: highest frequency first so the collapsed
            # picker can show hot tags without listing the whole taxonomy.
            type_counts = self._problem_type_counts()
            knowledge_types.sort(
                key=lambda t: (-type_counts.get(t.id, 0), (t.full_name or t.name or '').casefold()),
            )
            context['type_counts'] = type_counts
            context['knowledge_hot_count'] = self.KNOWLEDGE_PICKER_HOT_COUNT
            context['difficulty_types'] = difficulty_types
            context['knowledge_types'] = knowledge_types
            difficulty_ids = {t.id for t in difficulty_types}
            knowledge_ids = {t.id for t in knowledge_types}
            context['selected_difficulty_types'] = [i for i in self.selected_types if i in difficulty_ids]
            context['selected_knowledge_types'] = [
                i for i in self.selected_types if i in knowledge_ids
            ]
            # The answer format is driven by the authoritative submission_type discriminator
            # (MCQ is one "Choice question" option); offer the question types the
            # system actually supports end-to-end.
            context['format_types'] = [{'code': code, 'label': label}
                                       for code, label, q in self.get_format_options()]
            context['selected_formats'] = self.selected_formats
        context['has_fts'] = settings.ENABLE_FTS
        context['search_terms'] = getattr(self, 'search_terms', None) or []
        context['search_query'] = self.search_query
        context['completed_problem_ids'] = self.get_completed_problems()
        context['attempted_problems'] = self.get_attempted_problems()

        # Editing/deleting controls: expose which problems on this page the current user may
        # manage, so the list can render per-row edit/delete and bulk delete. Each id is
        # re-checked server-side on delete, so this is purely for showing the right controls.
        user = self.request.user
        can_manage = user.is_authenticated and user.has_perm('judge.edit_own_problem')
        editable_ids = set()
        if can_manage and not self.in_contest:
            page_problems = context.get('problems') or []
            if user.has_perm('judge.edit_all_problem'):
                editable_ids = {p.id for p in page_problems}
            else:
                editable_ids = {p.id for p in page_problems if p.is_editable_by(user)}
        context['can_manage_problems'] = can_manage and not self.in_contest
        context['editable_problem_ids'] = editable_ids
        # Clone entry on the list (detail page already has it). Org admins without the
        # global perm can still open /clone/ via ProblemClone.has_permission for problems
        # they admin; surface the icon for the common permission case.
        context['can_clone_problems'] = (
            user.is_authenticated
            and not self.in_contest
            and user.has_perm('judge.clone_problem')
        )

        from judge.question_types import creatable_types
        from judge.subjects import filter_question_types
        context['creatable_problem_types'] = filter_question_types(
            self.request.user, creatable_types(self.request.user))
        context['can_import_excel_problems'] = self.request.user.has_perm('judge.create_private_problem')
        context['ac_min'] = self.ac_min if self.ac_min is not None else ''
        context['ac_max'] = self.ac_max if self.ac_max is not None else ''
        context['problem_status'] = self.problem_status
        context['search_full'] = int(self.search_full)

        context.update(self.get_sort_paginate_context())
        if not self.in_contest:
            context.update(self.get_sort_context())

            sort_labels = {
                'code': _('Problem code'),
                'name': _('Problem'),
                'type': _('Problem type'),
                'ac_rate': _('AC %'),
                'points': _('Points'),
                'date': _('Date added'),
                'solved': _('Status'),
                'user_count': _('Users'),
                'editorial': _('Editorial'),
            }
            context['current_sort_label'] = sort_labels.get(self.order.lstrip('-'), '')
            context['current_sort_desc'] = self.order.startswith('-')

            # Progress ring: solved vs total within the current filtered scope.
            completed = set(context['completed_problem_ids'])
            context['progress_total'] = context['page_obj'].paginator.count
            context['progress_solved'] = (
                self.object_list.filter(id__in=completed).count() if completed else 0
            )

            context['hot_problems'] = hot_problems(timedelta(days=1), settings.DMOJ_PROBLEM_HOT_PROBLEM_COUNT)
            context['point_start'], context['point_end'], context['point_values'] = self.get_noui_slider_points()
        else:
            context['hot_problems'] = None
            context['point_start'], context['point_end'], context['point_values'] = (
                0,
                0,
                {},
            )
            context['hide_contest_scoreboard'] = self.contest.scoreboard_visibility in (
                self.contest.SCOREBOARD_AFTER_CONTEST,
                self.contest.SCOREBOARD_AFTER_PARTICIPATION,
                self.contest.SCOREBOARD_HIDDEN,
            )
            context['title_url'] = reverse('contest_view', args=[self.contest.key])
            context['title_crumb'] = _('Problems')
            context['contest'] = self.contest
        return context

    def get_noui_slider_points(self):
        points = sorted(self.prepoint_queryset.values_list('points', flat=True).distinct())
        if not points:
            return 0, 0, {}
        if len(points) == 1:
            return (
                points[0] - 1,
                points[0] + 1,
                {
                    'min': points[0] - 1,
                    '50%': points[0],
                    'max': points[0] + 1,
                },
            )

        start, end = points[0], points[-1]
        if self.point_start is not None:
            start = self.point_start
        if self.point_end is not None:
            end = self.point_end
        points_map = {0.0: 'min', 1.0: 'max'}
        size = len(points) - 1
        return (
            start,
            end,
            {points_map.get(i / size, '%.2f%%' % (100 * i / size,)): j for i, j in enumerate(points)},
        )

    def GET_with_session(self, request, key):
        if not request.GET:
            return request.session.get(key, False)
        return request.GET.get(key, None) == '1'

    def setup_problem_list(self, request):
        self.hide_solved = self.GET_with_session(request, 'hide_solved')
        # The redesigned problem list always renders the problem type column/filter.
        self.show_types = True
        self.full_text = self.GET_with_session(request, 'full_text')
        # Search the statement by default; honor an explicit un-check once the form is submitted.
        if 'filtered' in request.GET:
            self.search_full = request.GET.get('search_full') == '1'
        else:
            self.search_full = True
        self.has_public_editorial = self.GET_with_session(request, 'has_public_editorial')

        self.search_query = None
        self.search_terms = []
        self.category = None
        self.selected_types = []
        self.selected_formats = []
        self.selected_organizations = []
        self.selected_authors = []

        # This actually copies into the instance dictionary...
        self.all_sorts = set(self.all_sorts)
        if not self.show_types:
            self.all_sorts.discard('type')

        self.category = safe_int_or_none(request.GET.get('category'))
        if 'type' in request.GET:
            try:
                self.selected_types = list(map(int, request.GET.getlist('type')))
            except ValueError:
                pass
        if 'format' in request.GET:
            valid_formats = {code for code, label, q in self.get_format_options()}
            self.selected_formats = [c for c in request.GET.getlist('format') if c in valid_formats]
        # UI label is "Course"; accept both `course` and legacy `organization`.
        # Non-integer values (the old `public` sentinel) are ignored so an
        # unselected course still includes public problems.
        if 'course' in request.GET or 'organization' in request.GET:
            raw_courses = request.GET.getlist('course') or request.GET.getlist('organization')
            for raw in raw_courses:
                try:
                    self.selected_organizations.append(int(raw))
                except (TypeError, ValueError):
                    pass
        if user_can_see_problem_author_metadata(request.user) and 'author' in request.GET:
            for raw in request.GET.getlist('author'):
                try:
                    self.selected_authors.append(int(raw))
                except (TypeError, ValueError):
                    pass

        self.point_start = safe_float_or_none(request.GET.get('point_start'))
        self.point_end = safe_float_or_none(request.GET.get('point_end'))

        self.ac_min = safe_float_or_none(request.GET.get('ac_min'))
        self.ac_max = safe_float_or_none(request.GET.get('ac_max'))
        self.problem_status = request.GET.get('status') or ''
        if self.problem_status not in ('solved', 'unsolved'):
            self.problem_status = ''

    def get(self, request, *args, **kwargs):
        self.setup_problem_list(request)

        try:
            return super(ProblemList, self).get(request, *args, **kwargs)
        except ProgrammingError as e:
            return generic_message(request, 'FTS syntax error', e.args[1], status=400)

    def post(self, request, *args, **kwargs):
        to_update = ('hide_solved', 'show_types', 'has_public_editorial', 'full_text')
        for key in to_update:
            if key in request.GET:
                val = request.GET.get(key) == '1'
                request.session[key] = val
            else:
                request.session.pop(key, None)
        return HttpResponseRedirect(request.get_full_path())


class ProblemBulkDelete(LoginRequiredMixin, View):
    """POST-only endpoint to delete one or more problems from the problem list.

    The per-row delete submits a single ``codes`` value; the bulk toolbar submits several.
    Every problem is re-checked with ``is_editable_by`` here, so a user can only ever delete
    problems they are allowed to edit, regardless of what the form posted.
    """

    def post(self, request, *args, **kwargs):
        redirect_to = request.POST.get('next') or reverse('problem_list')
        if not url_has_allowed_host_and_scheme(
            url=redirect_to,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            redirect_to = reverse('problem_list')
        codes = [c for c in request.POST.getlist('codes') if c]
        accept = request.headers.get('Accept') or ''
        wants_json = (
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or ('application/json' in accept and 'text/html' not in accept)
        )
        if not codes:
            if wants_json:
                return JsonResponse({
                    'ok': False,
                    'deleted': 0,
                    'denied': 0,
                    'list_url': reverse('problem_list'),
                    'error': _('No problems selected.'),
                }, status=400)
            messages.error(request, _('No problems selected.'))
            return HttpResponseRedirect(redirect_to)

        deleted = denied = 0
        with transaction.atomic():
            for problem in Problem.objects.filter(code__in=codes):
                if problem.is_editable_by(request.user):
                    problem.delete()
                    deleted += 1
                else:
                    denied += 1

        if wants_json:
            if denied and not deleted:
                error = _('Skipped %(count)d problem(s) you are not allowed to delete.') % {'count': denied}
            else:
                error = ''
            return JsonResponse({
                'ok': bool(deleted),
                'deleted': deleted,
                'denied': denied,
                'list_url': reverse('problem_list'),
                'error': error,
            }, status=200 if deleted else 403)

        if deleted:
            messages.success(request, _('Deleted %(count)d problem(s).') % {'count': deleted})
        if denied:
            messages.error(
                request,
                _('Skipped %(count)d problem(s) you are not allowed to delete.') % {'count': denied},
            )
        return HttpResponseRedirect(redirect_to)


class LanguageTemplateAjax(View):
    def get(self, request, *args, **kwargs):
        try:
            language = get_object_or_404(Language, id=int(request.GET.get('id', 0)))
        except ValueError:
            raise Http404()

        template = ProblemTemplate.starter_code(language, request.GET.get('problem_id'))
        resp = HttpResponse(template, content_type='text/plain')
        resp['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp['Pragma'] = 'no-cache'
        return resp


class ProblemCodeCheckView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        if not (
            request.user.has_perm('judge.clone_problem')
            or request.user.has_perm('judge.create_private_problem')
            or request.user.has_perm('judge.add_problem')
            or request.user.is_superuser
        ):
            return JsonResponse({'error': _('Forbidden')}, status=403)

        if request.GET.get('suggest'):
            code = generate_problem_code()
            return JsonResponse({'code': code, 'exists': False})

        code = request.GET.get('code', '').strip()
        if not code:
            return JsonResponse({'error': _('Missing code')}, status=400)

        exists = Problem.objects.filter(code=code).exists()
        return JsonResponse({'exists': exists})


class RandomProblem(ProblemList):
    def get(self, request, *args, **kwargs):
        self.setup_problem_list(request)
        if self.in_contest:
            raise Http404()

        queryset = self.get_normal_queryset()
        count = queryset.count()
        if not count:
            return HttpResponseRedirect(
                '%s%s%s'
                % (
                    reverse('problem_list'),
                    request.META['QUERY_STRING'] and '?',
                    request.META['QUERY_STRING'],
                ),
            )
        return HttpResponseRedirect(queryset[randrange(count)].get_absolute_url())


user_logger = logging.getLogger('judge.user')


class ProblemSubmit(LoginRequiredMixin, ProblemMixin, TitleMixin, SingleObjectFormView):
    template_name = 'problem/submit.html'
    form_class = ProblemSubmitForm

    @cached_property
    def grader(self):
        from judge.question_types import get_grader
        return get_grader(self.object.submission_type)

    @cached_property
    def is_code(self):
        # Code submissions use the Ace/language form and the judge bridge; other question types
        # render a type-specific form and are graded internally.
        return self.grader.routes_to_bridge

    @cached_property
    def contest_problem(self):
        if self.request.profile.current_contest is None:
            return None
        return get_contest_problem(self.object, self.request.profile)

    @cached_property
    def remaining_submission_count(self):
        max_subs = self.contest_problem and self.contest_problem.max_submissions
        # None or 0 → unlimited (0 is rejected by the model validator; treat as unset).
        if not max_subs:
            return None
        # When an IE submission is rejudged into a non-IE status, it will count towards the
        # submission limit. We max with 0 to ensure that `remaining_submission_count` returns
        # a non-negative integer, which is required for future checks in this view.
        return max(
            0,
            max_subs
            - get_contest_submission_count(
                self.object,
                self.request.profile,
                self.request.profile.current_contest.virtual,
            ),
        )

    @cached_property
    def default_language(self):
        # If the old submission exists, use its language, otherwise use the user's default language.
        if self.old_submission is not None:
            return self.old_submission.language
        return self.request.profile.language

    def _submit_problem_name(self):
        if self.contest_problem is not None:
            return contest_problem_display_name(self.contest_problem, self.request.LANGUAGE_CODE)
        return self.object.translated_name(self.request.LANGUAGE_CODE)

    def get_content_title(self):
        return mark_safe(
            escape(_('Submit to %s'))
            % format_html(
                '<a href="{0}">{1}</a>',
                reverse('problem_detail', args=[self.object.code]),
                self._submit_problem_name(),
            ),
        )

    def get_title(self):
        return _('Submit to %s') % self._submit_problem_name()

    def get_initial(self):
        if self.is_code:
            initial = {'language': self.default_language}
            if self.old_submission is not None:
                initial['source'] = self.old_submission.source.source
            return initial
        # Non-code: prefill the previous answer when resubmitting.
        initial = {}
        if self.old_submission is not None and self.object.submission_type == 'M':
            try:
                selected = json.loads(self.old_submission.source.source).get('selected', [])
            except (ValueError, TypeError, ObjectDoesNotExist):
                selected = []
            try:
                multiple = self.object.mcq_config.multiple_select
            except ObjectDoesNotExist:
                multiple = False
            if multiple:
                initial['options'] = [str(s) for s in selected]
            elif selected:
                initial['options'] = str(selected[0])
        elif self.old_submission is not None and self.object.submission_type == 'B':
            try:
                answers = json.loads(self.old_submission.source.source).get('answers', [])
            except (ValueError, TypeError, ObjectDoesNotExist):
                answers = []
            for blank, answer in zip(self.object.blanks.all(), answers):
                initial['fb_%d' % blank.order] = answer
        return initial

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['instance'] = Submission(user=self.request.profile, problem=self.object)

        if self.object.is_editable_by(self.request.user):
            kwargs['judge_choices'] = tuple(
                Judge.objects.filter(online=True, problems=self.object).values_list('name', 'name'),
            )
        else:
            kwargs['judge_choices'] = ()

        return kwargs

    def get_form(self, form_class=None):
        if not self.is_code:
            posting = self.request.method in ('POST', 'PUT')
            data = self.request.POST if posting else None
            files = self.request.FILES if posting else None
            return self.grader.build_form(self.object, self.request.profile,
                                          data=data, files=files, initial=self.get_initial())

        form = super().get_form(form_class)

        form.fields['language'].queryset = self.object.usable_languages.order_by('name', 'key').prefetch_related(
            Prefetch('runtimeversion_set', RuntimeVersion.objects.order_by('priority')),
        )

        form_data = getattr(form, 'cleaned_data', form.initial)
        if 'language' in form_data:
            form.fields['source'].widget.mode = form_data['language'].ace
        form.fields['source'].widget.theme = self.request.profile.resolved_ace_theme

        return form

    def get_success_url(self):
        if self.request.POST.get('workspace') == '1':
            return '%s?submission=%d' % (
                reverse('problem_detail', args=(self.object.code,)), self.new_submission.id,
            )
        return reverse('submission_status', args=(self.new_submission.id,))

    def _wants_workspace_json(self):
        return (
            self.request.POST.get('workspace') == '1'
            and self.request.headers.get('X-Workspace-Submit') == '1'
        )

    def _workspace_json_error(self, message, status=400, **extra):
        payload = {'error': message}
        payload.update(extra)
        return JsonResponse(payload, status=status)

    def form_invalid(self, form):
        if self._wants_workspace_json():
            message = _('Please fix the errors below.')
            for _field, errors in form.errors.items():
                if errors:
                    message = str(errors[0])
                    break
            # Empty code / blank required fields surface as field errors — keep them explicit.
            if self.is_code and not (form.data.get('source') or '').strip():
                message = _('Source code cannot be empty.')
            elif not self.is_code:
                non_empty = bool(self.request.FILES)
                for key, value in form.data.items():
                    if key in ('csrfmiddlewaretoken', 'workspace', 'parent'):
                        continue
                    if isinstance(value, list):
                        if any(str(v).strip() for v in value):
                            non_empty = True
                            break
                    elif str(value).strip():
                        non_empty = True
                        break
                if not non_empty:
                    message = _('Answer cannot be empty.')
            return self._workspace_json_error(message, status=400, errors=form.errors.get_json_data())
        return super().form_invalid(form)

    def _workspace_success_response(self, form):
        if self._wants_workspace_json():
            submission = self.new_submission
            hidden = not submission.results_visible_to(self.request.user)
            try:
                source = submission.source.source
            except ObjectDoesNotExist:
                source = None
            payload = {
                'submission_id': submission.id,
                'status': submission.status,
                'result': None if hidden else submission.result,
                'results_hidden': hidden,
                'is_graded': submission.is_graded,
                'points': None if hidden else workspace_display_points(submission),
                'points_total': workspace_display_points_total(submission),
                'source': source,
                'answer_display': get_workspace_answer_display(submission) if source is not None else [],
                'file_upload': get_workspace_file_upload(submission),
                'status_url': reverse('problem_workspace_submission_status', args=(self.object.code, submission.id)),
            }
            if not hidden:
                try:
                    payload.update(workspace_ai_grade_fields(self.request, submission))
                except Exception:
                    logging.getLogger('judge.problem').exception(
                        'Workspace AI-grade payload failed after submit %s', submission.id,
                    )
            participation = getattr(self.request, 'participation', None)
            if participation is None and self.request.user.is_authenticated:
                participation = self.request.profile.current_contest
            if participation is not None:
                payload['leave_progress'] = participation_submit_progress(
                    participation,
                    log_step='json_submit',
                    problem=self.object.code,
                    submission_id=submission.id,
                )
            return JsonResponse(payload)
        return super().form_valid(form)

    def _in_flight_code_submissions(self):
        """Queued/running judge-bridge submissions for this user on this problem only."""
        return Submission.objects.filter(
            user=self.request.profile,
            problem=self.object,
            rejudged_date__isnull=True,
        ).exclude(status__in=['D', 'IE', 'CE', 'AB']).count()

    def _submission_blocked_response(self):
        """Pre-submit guards shared by every question type. Returns a response if blocked."""
        # A non-public problem that belongs to an assignment may be READ by
        # anyone who can access the assignment, but submissions must happen
        # inside the student's own live window — never as a stray regular
        # submission outside a contest. Maintainers (authors/editors who can
        # edit the problem) are exempt so they can still test-submit from the
        # problem page outside a contest context.
        if self.contest_problem is None and not self.object.is_public \
                and not self.object.is_editable_by(self.request.user):
            from judge.models import ContestProblem
            if ContestProblem.objects.filter(problem_id=self.object.id).exists():
                return generic_message(
                    self.request,
                    _('Join the assignment to submit'),
                    _('This problem belongs to an assignment. Enter the assignment to submit a solution.'),
                )
        # A problem used in a running or upcoming exam whose results are hidden must not be
        # submitted anywhere its verdict is shown at once (public or not): not as practice, and
        # not inside another assignment that shows results, e.g. a virtual join of last term's
        # quiz or a homework running at the same time (G-7). It would work as an answer checker
        # for the exam. Submits whose results are masked too (inside such an exam) are fine.
        # Editors may still test it.
        if not contest_problem_hides_results(self.contest_problem) \
                and not self.object.is_editable_by(self.request.user) \
                and problem_in_active_hidden_exam(self.object):
            title = _('Submissions are closed during the exam')
            message = _('This problem is used in an exam that is running or has not started yet. '
                        'Submit your answer inside the exam.')
            if self._wants_workspace_json():
                return self._workspace_json_error(message, status=403)
            return generic_message(self.request, title, message, status=403)
        # Judge-bridge spam cap: code problems only, scoped to this problem.
        if (
            self.is_code
            and not self.request.user.has_perm('judge.spam_submission')
            and self._in_flight_code_submissions() >= settings.DMOJ_SUBMISSION_LIMIT
        ):
            if self._wants_workspace_json():
                return self._workspace_json_error(_('You submitted too many submissions.'), status=429)
            return HttpResponse(
                format_html('<h1>{0}</h1>', _('You submitted too many submissions.')),
                status=429,
            )
        if not self.request.user.is_superuser and self.object.banned_users.filter(id=self.request.profile.id).exists():
            if self._wants_workspace_json():
                return self._workspace_json_error(
                    _('You are permanently barred from submitting to this problem.'),
                    status=403,
                )
            return generic_message(
                self.request,
                _('Banned from submitting'),
                _(
                    'You have been declared persona non grata for this problem. '
                    'You are permanently barred from submitting to this problem.',
                ),
            )
        # Must check for zero and not None. None means infinite submissions remaining.
        if self.remaining_submission_count == 0:
            if self._wants_workspace_json():
                return self._workspace_json_error(
                    _('You have exceeded the submission limit for this problem.'),
                    status=429,
                )
            return generic_message(
                self.request,
                _('Too many submissions'),
                _('You have exceeded the submission limit for this problem.'),
            )
        return None

    def _create_submission(self, language, answer, judge_id=None):
        """Hand the validated answer to the shared, request-independent submission path."""
        contest_problem = self.contest_problem
        # Use the contest object from current_contest.contest because we already use it
        # in profile.update_contest().
        participation = self.request.profile.current_contest if contest_problem is not None else None
        submission = create_submission(
            self.request.profile, self.object,
            language=language, answer=answer,
            participation=participation, contest_problem=contest_problem,
            judge_id=judge_id,
        )
        if participation is not None:
            # Only a *successful* submit clears the draft, which is what makes a surviving draft
            # mean "never submitted, or edited since" — the exact set auto-submit has to collect.
            AssignmentDraft.objects.filter(participation=participation, problem=self.object).delete()
        return submission

    def form_valid(self, form):
        blocked = self._submission_blocked_response()
        if blocked is not None:
            return blocked
        try:
            if self.is_code:
                return self._code_form_valid(form)
            return self._noncode_form_valid(form)
        except IntegrityError:
            if self._wants_workspace_json():
                logging.getLogger('judge.problem').exception('Workspace submit failed')
                return self._workspace_json_error(_('Submit failed.'), status=500)
            raise

    def _code_form_valid(self, form):
        if not self.object.allowed_languages.filter(id=form.cleaned_data['language'].id).exists():
            raise PermissionDenied()

        # Check if problem uses harness grading - only allow languages with harnesses
        harness_languages = set(self.object.harnesses.values_list('language__key', flat=True))
        if harness_languages:
            language_key = form.cleaned_data['language'].key
            if language_key not in harness_languages:
                harness_language_names = list(
                    self.object.harnesses.select_related('language').values_list('language__name', flat=True),
                )
                return generic_message(
                    self.request,
                    _('Language not supported'),
                    _('This problem only accepts submissions in: %s') % ', '.join(harness_language_names),
                )

        self.new_submission = self._create_submission(
            form.cleaned_data['language'], form.cleaned_data['source'], judge_id=form.cleaned_data['judge'],
        )

        return self._workspace_success_response(form)

    def _noncode_form_valid(self, form):
        self.new_submission = self._create_submission(Language.get_answer_language(), form.cleaned_data)

        return self._workspace_success_response(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['submission_type'] = self.object.submission_type
        context['submission_limit'] = self.contest_problem and self.contest_problem.max_submissions
        context['submissions_left'] = self.remaining_submission_count
        if self.is_code:
            context['langs'] = Language.objects.all()
            context['no_judges'] = not context['form'].fields['language'].queryset
            context['ACE_URL'] = ace_url()
            context['default_lang'] = self.default_language
        return context

    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except Http404:
            # Is this really necessary? This entire post() method could be removed if we don't log this.
            user_logger.info(
                'Naughty user %s wants to submit to %s without permission',
                request.user.username,
                kwargs.get(self.slug_url_kwarg),
            )
            return HttpResponseForbidden(format_html('<h1>{0}</h1>', _('Do you want me to ban you?')))

    def dispatch(self, request, *args, **kwargs):
        submission_id = kwargs.get('submission')
        if submission_id is not None:
            self.old_submission = get_object_or_404(
                Submission.objects.select_related('source', 'language'),
                id=submission_id,
            )
            if not request.user.has_perm('judge.resubmit_other') and self.old_submission.user != request.profile:
                raise PermissionDenied()
        else:
            self.old_submission = None

        return super().dispatch(request, *args, **kwargs)


class ProblemClone(ProblemMixin, PermissionRequiredMixin, TitleMixin, SingleObjectFormView):
    title = gettext_lazy('Clone Problem')
    template_name = 'problem/clone.html'
    form_class = ProblemCloneForm
    permission_required = 'judge.clone_problem'

    def has_permission(self):
        if super().has_permission():
            return True
        if not self.request.user.is_authenticated:
            return False
        problem = self.get_object()
        return problem.is_viewable_by_org_admin(self.request.user)

    def get_initial(self):
        initial = super().get_initial()
        initial['code'] = generate_problem_code()
        return initial

    def form_valid(self, form):
        problem = self.object

        # Store references to ManyToMany relationships
        languages = problem.allowed_languages.all()
        organizations = problem.organizations.all()
        classes = problem.classes.all()
        types = problem.types.all()
        old_code = problem.code
        new_code = form.cleaned_data['code']

        # Store references to related objects BEFORE cloning problem
        old_problem_id = problem.id
        test_cases = list(ProblemTestCase.objects.filter(dataset_id=old_problem_id).order_by('order'))
        templates = list(ProblemTemplate.objects.filter(problem_id=old_problem_id))
        harnesses = list(ProblemHarness.objects.filter(problem_id=old_problem_id))
        # LanguageLimit has a FK to Problem — clone rows; never .set() (that would move them).
        language_limits = list(LanguageLimit.objects.filter(problem_id=old_problem_id))
        mcq_options = list(ProblemMCQOption.objects.filter(problem_id=old_problem_id).order_by('order'))
        blanks = list(ProblemBlank.objects.filter(problem_id=old_problem_id).order_by('order'))
        try:
            mcq_config = ProblemMCQConfig.objects.get(problem_id=old_problem_id)
        except ProblemMCQConfig.DoesNotExist:
            mcq_config = None
        try:
            fill_blank_config = ProblemFillBlankConfig.objects.get(problem_id=old_problem_id)
        except ProblemFillBlankConfig.DoesNotExist:
            fill_blank_config = None
        try:
            short_answer_config = ProblemShortAnswerConfig.objects.get(problem_id=old_problem_id)
        except ProblemShortAnswerConfig.DoesNotExist:
            short_answer_config = None
        try:
            file_config = ProblemFileConfig.objects.get(problem_id=old_problem_id)
        except ProblemFileConfig.DoesNotExist:
            file_config = None

        # Check if ProblemData exists for original problem
        try:
            original_problem_data = ProblemData.objects.get(problem_id=old_problem_id)
            has_problem_data = True
        except ProblemData.DoesNotExist:
            original_problem_data = None
            has_problem_data = False

        # Clone the problem
        problem.pk = None
        problem.is_public = False
        problem.ac_rate = 0
        problem.user_count = 0
        problem.code = new_code

        with revisions.create_revision(atomic=True):
            # Save new problem
            problem.save()
            problem.authors.add(self.request.profile)
            problem.allowed_languages.set(languages)
            problem.organizations.set(organizations)
            problem.classes.set(classes)
            problem.types.set(types)

            self._clone_language_limits(language_limits, problem)

            # Clone ProblemData if it exists
            if has_problem_data:
                self._clone_problem_data(original_problem_data, problem, old_code, new_code)

            # Clone ProblemTestCase instances
            self._clone_test_cases(test_cases, problem)

            # Clone ProblemTemplate instances
            self._clone_templates(templates, problem)

            # Clone ProblemHarness instances
            self._clone_harnesses(harnesses, problem)

            self._clone_question_type_data(
                problem, mcq_config, mcq_options, fill_blank_config, blanks, short_answer_config,
                file_config,
            )

            # Copy init.yml from original problem
            self._copy_init_yml(old_code, new_code)

            # Set revision metadata
            revisions.set_user(self.request.user)
            revisions.set_comment(_('Cloned problem from %s') % old_code)

        requested = (self.request.POST.get('code') or '').strip()
        if requested and requested != problem.code:
            messages.info(self.request, _('Saved with code %(new)s because %(old)s was already in use.') % {
                'new': problem.code,
                'old': requested,
            })
        return HttpResponseRedirect(reverse('problem_edit_v2', args=(problem.code,)))

    def _clone_problem_data(self, original_data, new_problem, old_code, new_code):
        """
        Clone ProblemData instance and copy associated files.

        Args:
            original_data: Original ProblemData instance
            new_problem: Newly created Problem instance
            old_code: Original problem code
            new_code: New problem code
        """
        logger = logging.getLogger('judge.problem')
        has_file_errors = False

        # Create new ProblemData instance
        new_data = ProblemData()
        new_data.problem = new_problem
        new_data.output_prefix = original_data.output_prefix
        new_data.output_limit = original_data.output_limit
        new_data.feedback = ''  # Reset feedback for new problem
        new_data.checker = original_data.checker
        new_data.unicode = original_data.unicode
        new_data.nobigmath = original_data.nobigmath
        new_data.checker_args = original_data.checker_args

        # Handle zipfile copying
        if original_data.zipfile:
            try:
                # Read original zipfile
                original_zipfile_path = original_data.zipfile.path
                if os.path.exists(original_zipfile_path):
                    with open(original_zipfile_path, 'rb') as f:
                        file_content = f.read()

                    # Create new zipfile with new problem code in path
                    original_filename = os.path.basename(original_data.zipfile.name)
                    new_data.zipfile.save(original_filename, ContentFile(file_content), save=False)
            except (IOError, OSError):
                logger.warning('Failed to copy zipfile for problem %s: %s', new_code, exc_info=True)
                has_file_errors = True

        # Handle generator file copying
        if original_data.generator:
            try:
                # Read original generator
                original_generator_path = original_data.generator.path
                if os.path.exists(original_generator_path):
                    with open(original_generator_path, 'rb') as f:
                        file_content = f.read()

                    # Create new generator with new problem code in path
                    original_filename = os.path.basename(original_data.generator.name)
                    new_data.generator.save(original_filename, ContentFile(file_content), save=False)
            except (IOError, OSError):
                logger.warning('Failed to copy generator for problem %s: %s', new_code, exc_info=True)
                has_file_errors = True

        # Save the new ProblemData instance
        new_data.save()

        # Copy individual test case files from old directory to new directory
        old_dir = problem_data_storage.path(old_code)
        new_dir = problem_data_storage.path(new_code)

        if os.path.exists(old_dir) and os.path.isdir(old_dir):
            try:
                # Create new directory if it doesn't exist
                os.makedirs(new_dir, exist_ok=True)

                # Copy all files except zipfile and generator (already handled)
                # and init.yml (will be copied separately)
                for filename in os.listdir(old_dir):
                    if filename == 'init.yml':
                        continue  # Skip init.yml, it will be copied separately

                    old_file_path = os.path.join(old_dir, filename)
                    new_file_path = os.path.join(new_dir, filename)

                    # Only copy regular files, not directories
                    if os.path.isfile(old_file_path):
                        # Skip if this is the zipfile or generator we already copied
                        if original_data.zipfile and filename == os.path.basename(
                            original_data.zipfile.name,
                        ):
                            continue
                        if original_data.generator and filename == os.path.basename(
                            original_data.generator.name,
                        ):
                            continue

                        shutil.copy2(old_file_path, new_file_path)
            except (IOError, OSError):
                logger.warning('Failed to copy data directory for problem %s: %s', new_code, exc_info=True)
                has_file_errors = True

        # Show warning message to user if there were any file errors
        if has_file_errors:
            messages.warning(
                self.request,
                _(
                    'Some test data files could not be copied. Please verify files in problem data.',
                ),
            )

    def _clone_test_cases(self, test_cases, new_problem):
        """
        Clone all ProblemTestCase instances for the new problem.

        Args:
            test_cases: List of original ProblemTestCase instances
            new_problem: Newly created Problem instance
        """
        new_test_cases = []

        for original_case in test_cases:
            new_case = ProblemTestCase()
            new_case.dataset = new_problem
            new_case.order = original_case.order
            new_case.type = original_case.type
            new_case.input_file = original_case.input_file
            new_case.output_file = original_case.output_file
            new_case.generator_args = original_case.generator_args
            new_case.points = original_case.points
            new_case.is_pretest = original_case.is_pretest
            new_case.output_prefix = original_case.output_prefix
            new_case.output_limit = original_case.output_limit
            new_case.checker = original_case.checker
            new_case.checker_args = original_case.checker_args
            new_case.batch_dependencies = original_case.batch_dependencies

            new_test_cases.append(new_case)

        # Bulk create for efficiency
        if new_test_cases:
            ProblemTestCase.objects.bulk_create(new_test_cases)

    def _clone_templates(self, templates, new_problem):
        """
        Clone all ProblemTemplate instances for the new problem.

        Args:
            templates: List of original ProblemTemplate instances
            new_problem: Newly created Problem instance
        """
        new_templates = []

        for original_template in templates:
            new_template = ProblemTemplate()
            new_template.problem = new_problem
            new_template.language = original_template.language
            new_template.code = original_template.code

            new_templates.append(new_template)

        # Bulk create for efficiency
        if new_templates:
            ProblemTemplate.objects.bulk_create(new_templates)

    def _clone_language_limits(self, language_limits, new_problem):
        """Deep-copy LanguageLimit rows so the source problem keeps its FK rows."""
        new_limits = []
        for original in language_limits:
            new_limits.append(LanguageLimit(
                problem=new_problem,
                language=original.language,
                time_limit=original.time_limit,
                memory_limit=original.memory_limit,
            ))
        if new_limits:
            LanguageLimit.objects.bulk_create(new_limits)

    def _clone_question_type_data(
        self, new_problem, mcq_config, mcq_options, fill_blank_config, blanks, short_answer_config,
        file_config=None,
    ):
        """Deep-copy MCQ / fill-blank / short-answer / file-upload authoring data onto the clone."""
        if mcq_config is not None:
            ProblemMCQConfig.objects.create(
                problem=new_problem,
                multiple_select=mcq_config.multiple_select,
                is_true_false=mcq_config.is_true_false,
                shuffle_options=mcq_config.shuffle_options,
            )
        if mcq_options:
            ProblemMCQOption.objects.bulk_create([
                ProblemMCQOption(
                    problem=new_problem,
                    order=opt.order,
                    text=opt.text,
                    is_correct=opt.is_correct,
                )
                for opt in mcq_options
            ])
        if fill_blank_config is not None:
            ProblemFillBlankConfig.objects.create(
                problem=new_problem,
                case_sensitive=fill_blank_config.case_sensitive,
                trim_whitespace=fill_blank_config.trim_whitespace,
                grading_mode=fill_blank_config.grading_mode,
                ai_rubric=fill_blank_config.ai_rubric,
                grading_table=fill_blank_config.grading_table,
            )
        if blanks:
            ProblemBlank.objects.bulk_create([
                ProblemBlank(
                    problem=new_problem,
                    order=blank.order,
                    answers=blank.answers,
                    label=blank.label,
                )
                for blank in blanks
            ])
        if short_answer_config is not None:
            ProblemShortAnswerConfig.objects.create(
                problem=new_problem,
                reference_answer=short_answer_config.reference_answer,
                ai_enabled=short_answer_config.ai_enabled,
                ai_rubric=short_answer_config.ai_rubric,
                grading_table=short_answer_config.grading_table,
            )
        # Same OneToOne fields create_v2 persists in `_create_file_upload` (MR !39/!46).
        if file_config is not None:
            ProblemFileConfig.objects.create(
                problem=new_problem,
                allowed_extensions=file_config.allowed_extensions,
                max_file_size=file_config.max_file_size,
                instructions=file_config.instructions,
            )

    def _clone_harnesses(self, harnesses, new_problem):
        """
        Clone all ProblemHarness instances for the new problem.

        Args:
            harnesses: List of original ProblemHarness instances
            new_problem: Newly created Problem instance
        """
        new_harnesses = []

        for original in harnesses:
            new_harness = ProblemHarness()
            new_harness.problem = new_problem
            new_harness.language = original.language
            new_harness.harness_code = original.harness_code
            new_harness.skip_precompile = original.skip_precompile
            new_harness.run_student_main = original.run_student_main

            new_harnesses.append(new_harness)

        # Bulk create for efficiency
        if new_harnesses:
            ProblemHarness.objects.bulk_create(new_harnesses)

    def _copy_init_yml(self, old_code, new_code):
        """
        Copy init.yml from the original problem to the cloned problem.

        Args:
            old_code: Original problem code
            new_code: New problem code
        """
        logger = logging.getLogger('judge.problem')

        try:
            old_init_path = problem_data_storage.path(os.path.join(old_code, 'init.yml'))
            new_init_path = problem_data_storage.path(os.path.join(new_code, 'init.yml'))

            if os.path.exists(old_init_path):
                shutil.copy2(old_init_path, new_init_path)
        except (IOError, OSError):
            logger.warning('Failed to copy init.yml for %s', new_code, exc_info=True)
            messages.warning(
                self.request,
                _(
                    'Failed to copy init.yml. You may need to regenerate it manually.',
                ),
            )


class ProblemWorkspaceRun(LoginRequiredMixin, ProblemMixin, SingleObjectMixin, View):
    """Run code in the existing isolated IDE sandbox, never against this problem."""

    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        from judge.question_types import get_grader
        if not get_grader(self.object.submission_type).routes_to_bridge:
            return JsonResponse({'error': _('Run is available only for programming problems.')}, status=400)
        if self.object.banned_users.filter(id=request.profile.id).exists() and not request.user.is_superuser:
            return JsonResponse({'error': _('You are banned from submitting to this problem.')}, status=403)
        if not self.object.usable_languages.filter(id=request.POST.get('language')).exists():
            return JsonResponse({'error': _('This language is not allowed for this problem.')}, status=400)
        if request.profile.current_contest and not get_contest_problem(self.object, request.profile):
            return JsonResponse({'error': _('This problem is not in your active assignment.')}, status=403)
        # Reuse the site IDE endpoint as-is: same rate limits, source/input
        # caps, output truncation, and bridge protocol. Submissions are filed
        # under the internal `idepractice` sandbox problem (see IDE_PROBLEM_CODE)
        # so they never attach ContestSubmission or touch this problem's tests.
        from judge.views.ide import IDESubmitView
        return IDESubmitView().post(request)


class WorkspaceProgressTrace(LoginRequiredMixin, View):
    """Beacon for what the browser received/applied for the progress bar and Leave dialog."""

    http_method_names = ['post']
    ALLOWED_STEPS = frozenset(('client_leave_dialog',))
    ALLOWED_FIELDS = frozenset((
        'href', 'mode', 'problem', 'source', 'submission_id', 'has_leave_progress',
        'bar_label', 'bar_value', 'bar_max',
        'leave_submitted', 'leave_unsubmitted', 'leave_total',
        'leave_submitted_names', 'leave_unsubmitted_names',
        'payload_submitted', 'payload_unsubmitted', 'payload_total',
    ))

    def post(self, request, *args, **kwargs):
        if len(request.body) > 8192:
            return JsonResponse({'ok': False}, status=400)
        try:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({'ok': False}, status=400)
        if not isinstance(payload, dict):
            return JsonResponse({'ok': False}, status=400)
        step = payload.get('step')
        if step not in self.ALLOWED_STEPS:
            return JsonResponse({'ok': False}, status=400)
        extra = {key: payload[key] for key in self.ALLOWED_FIELDS if key in payload}
        participation = getattr(request, 'participation', None)
        if participation is None and request.user.is_authenticated:
            participation = request.profile.current_contest
        log_workspace_progress(step, participation, user=request.user.username, **extra)
        return JsonResponse({'ok': True})


class ProblemWorkspaceSubmissionStatus(LoginRequiredMixin, ProblemMixin, SingleObjectMixin, View):
    """Status JSON for the current user's formal Workspace submission."""

    http_method_names = ['get']

    def get(self, request, *args, **kwargs):
        problem = self.get_object()
        submission = get_object_or_404(
            Submission.objects.select_related(
                'source', 'upload', 'problem', 'contest_object', 'contest__problem', 'user__user',
            ).prefetch_related('problem__mcq_options', 'problem__blanks'),
            id=kwargs['submission'], problem=problem,
        )
        if not submission.can_see_detail(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        contest_problem = get_contest_problem(problem, request.profile)
        if contest_problem and submission.contest_object_id != contest_problem.contest_id:
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        hidden = not submission.results_visible_to(request.user)
        source = None
        if submission.user_id == request.profile.id:
            try:
                source = submission.source.source
            except ObjectDoesNotExist:
                pass
        answer_display = get_workspace_answer_display(submission) if source is not None else []
        comments_html = ''
        if settings.DMOJ_ENABLE_COMMENTS:
            try:
                comment_handler = SubmissionCommentMixin()
                comment_handler.object = submission
                comment_handler.request = request
                comment_handler.comment_use_rich_editor = True
                comment_ctx = comment_handler.get_comment_context()
                comment_ctx.update({
                    'request': request,
                    'ENABLE_COMMENTS': True,
                    'comment_use_rich_editor': True,
                    'comment_form_action': '%s?submission=%d' % (
                        reverse('problem_detail', args=[problem.code]),
                        submission.id,
                    ),
                })
                comments_html = render_to_string('comments/list.html', comment_ctx, request=request)
            except Exception:
                logging.getLogger('judge.problem').exception(
                    'Failed to render workspace comments for submission %s', submission.id,
                )
                comments_html = ''
        return JsonResponse({
            'id': submission.id,
            'status': submission.status,
            'result': None if hidden else submission.result,
            'result_class': None if hidden else submission.result_class,
            'points': None if hidden else workspace_display_points(submission),
            'points_total': workspace_display_points_total(submission),
            'time': None if hidden else submission.time,
            'memory': None if hidden else submission.memory,
            'results_hidden': hidden,
            'is_graded': submission.is_graded,
            'source': source,
            'answer_display': answer_display,
            'file_upload': get_workspace_file_upload(submission),
            'comments_html': comments_html,
            'released': False if hidden else workspace_manual_released(submission),
            'review_note_html': '' if hidden else workspace_review_note_html(submission),
            'id_secret': submission.id_secret,
            'status_url': reverse(
                'problem_workspace_submission_status',
                args=(problem.code, submission.id),
            ),
            **({} if hidden else workspace_ai_grade_fields(request, submission)),
        })


class ProblemWorkspaceSubmissionsList(LoginRequiredMixin, ProblemMixin, SingleObjectMixin, View):
    """JSON submission rows for the Workspace inline submissions panel."""

    http_method_names = ['get']
    VALID_SCOPES = frozenset({'my', 'all', 'best'})

    def get(self, request, *args, **kwargs):
        from judge.views.submission import ProblemSubmissionsBase

        problem = self.get_object()
        scope = request.GET.get('scope', 'my')
        if scope not in self.VALID_SCOPES:
            return JsonResponse({'error': _('Invalid scope.')}, status=400)

        contest_problem = get_contest_problem(problem, request.profile)
        # Assignment/exam UI only offers "My"; reject All/Best to match the panel.
        if contest_problem and scope in ('all', 'best'):
            return JsonResponse({'error': _('Permission denied.')}, status=403)

        if scope in ('all', 'best'):
            checker = ProblemSubmissionsBase()
            checker.request = request
            checker.problem = problem
            checker.problem_name = problem.translated_name(request.LANGUAGE_CODE)
            try:
                checker.access_check(request)
            except PermissionDenied:
                return JsonResponse({'error': _('Permission denied.')}, status=403)
            except Http404:
                return JsonResponse({'error': _('Permission denied.')}, status=403)

        if scope == 'my':
            my_qs = Submission.objects.filter(user=request.profile, problem=problem)
            if contest_problem:
                my_qs = my_qs.filter(contest_object=contest_problem.contest)
            submissions = (
                my_qs
                .select_related('language', 'contest_object', 'contest__problem')
                .prefetch_related('manual_grade')
                .order_by('-date')[:50]
            )
        elif scope == 'all':
            checker.selected_languages = set()
            checker.selected_statuses = set()
            submissions = checker.get_queryset().select_related(
                'language', 'contest_object', 'contest__problem',
            ).prefetch_related('manual_grade').order_by('-id')[:50]
        else:
            checker.selected_languages = set()
            checker.selected_statuses = set()
            submissions = self._best_submissions(
                checker.get_queryset().select_related(
                    'language', 'contest_object', 'contest__problem',
                ).prefetch_related('manual_grade'),
            )

        profile = request.profile
        completed = user_completed_ids(profile)
        editable = user_editable_ids(profile)
        testers = user_tester_ids(profile)
        rows = []
        for submission in submissions:
            if not submission.can_see_detail(request.user):
                continue
            # The row template renders the gated score (machine until the manual
            # override is released), so attach the same display points the initial
            # page load computes for its workspace submissions.
            from judge.utils.contest_grade import attach_student_display_score
            attach_student_display_score(submission)
            html = render_to_string(
                'problem/workspace-submission-row.html',
                {
                    'submission': submission,
                    'profile_id': profile.id,
                    'completed_problem_ids': completed,
                    'editable_problem_ids': editable,
                    'tester_problem_ids': testers,
                    'request': request,
                },
                request=request,
            )
            rows.append({
                'id': submission.id,
                'html': html,
                'status_url': reverse(
                    'problem_workspace_submission_status',
                    args=(problem.code, submission.id),
                ),
                'url': '%s?submission=%d' % (
                    reverse('problem_detail', args=[problem.code]), submission.id,
                ),
            })

        return JsonResponse({'scope': scope, 'rows': rows})

    @staticmethod
    def _best_submissions(queryset):
        seen_users = set()
        rows = []
        for submission in queryset.filter(points__gt=0, user__is_unlisted=False).order_by('-points', 'time'):
            if submission.user_id in seen_users:
                continue
            seen_users.add(submission.user_id)
            rows.append(submission)
            if len(rows) >= 50:
                break
        return rows
