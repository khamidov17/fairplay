import json
import logging
import re
from calendar import Calendar, SUNDAY
from collections import defaultdict, namedtuple
from datetime import date, datetime, time, timedelta
from functools import partial
from itertools import chain
from operator import attrgetter, itemgetter

import markdown
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.contrib.contenttypes.models import ContentType
from django.core import signing
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import BooleanField, Case, Count, F, FloatField, IntegerField, Max, Min, Q, Sum, Value, When
from django.db.models.expressions import CombinedExpression, Exists, OuterRef
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.defaultfilters import date as date_filter
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.utils.text import slugify
from django.utils.timezone import make_aware
from django.utils.translation import gettext as _, gettext_lazy
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import ListView, TemplateView, View
from django.views.generic.detail import BaseDetailView, DetailView, SingleObjectMixin
from django.views.generic.list import BaseListView
from icalendar import Calendar as ICalendar, Event
from markdown_katex import KatexExtension
from reversion import revisions

from judge import contest_format
from judge import event_poster as event
from judge.comments import CommentedDetailView
from judge.forms import ContestCloneForm
from judge.models import Contest, ContestMoss, ContestParticipation, ContestProblem, \
    ContestTag, Problem, ProblemSubmissionType, Profile, SEBConfigPreset, Submission, SubmissionManualGrade, \
    contest_problem_display_name
from judge.models.profile import Class, Organization
from judge.storage import StorageUnavailableError
from judge.tasks import run_moss
from judge.utils.celery import redirect_to_task_status
from judge.utils.opengraph import generate_opengraph
from judge.utils.problems import _get_result_data, assignment_problem_stats, participation_submit_progress, \
    user_completed_ids
from judge.utils.ranker import ranker
from judge.utils.seb import read_seb_launch_token, request_matches_seb, seb_launch_context, seb_launch_url
from judge.utils.stats import get_bar_chart, get_pie_chart
from judge.utils.user_search import profile_choice_label
from judge.utils.views import DiggPaginatorMixin, QueryStringSortMixin, SingleObjectFormView, TitleMixin, \
    generic_message

logger = logging.getLogger(__name__)

__all__ = ['ContestList', 'ContestListV2', 'ContestCreate', 'ContestDetail', 'ContestRanking', 'ContestJoin',
           'ContestLeave', 'ContestCalendar', 'ContestClone', 'ContestDelete', 'ContestBulkDelete',
           'ContestStats', 'ContestMossView',
           'ContestMossDelete', 'contest_ranking_ajax',
           'ContestParticipationList', 'ContestParticipationDisqualify', 'get_contest_ranking_list',
           'base_contest_ranking_list', 'ContestExportPDF', 'ContestExportScores', 'ContestExportSubmissions',
           'ContestExportJoinedScores',
           'ContestGrade', 'ContestGradeSave', 'ContestGradeRelease',
           'ContestBatchAIGrade', 'ContestSEBConfig', 'SEBLaunchConfig', 'SEBLaunchToken', 'SEBQuit',
           'ContestTagCreateView', 'ContestSEBFileView',
           'ContestFinalizeView', 'ContestUnfinalizeView']


def _find_contest(request, key, private_check=True):
    try:
        contest = Contest.objects.get(key=key)
        if private_check and not contest.is_accessible_by(request.user):
            raise ObjectDoesNotExist()
    except ObjectDoesNotExist:
        return generic_message(request, _('No such contest'),
                               _('Could not find a contest with the key "%s".') % key, status=404), False
    return contest, True


class ContestListMixin(object):
    def get_queryset(self):
        return Contest.get_visible_contests(self.request.user)


class ContestInSessionListRedirectMixin(object):
    """While joined, the Assignments nav must open the current assignment, not the list."""

    def dispatch(self, request, *args, **kwargs):
        participation = getattr(request, 'participation', None)
        if getattr(request, 'in_contest', False) and participation is not None:
            return HttpResponseRedirect(reverse('contest_view', args=(participation.contest.key,)))
        return super().dispatch(request, *args, **kwargs)


class ContestList(ContestInSessionListRedirectMixin, QueryStringSortMixin, DiggPaginatorMixin, TitleMixin,
                  ContestListMixin, ListView):
    model = Contest
    paginate_by = 20
    template_name = 'contest/list.html'
    title = gettext_lazy('Assignments')
    context_object_name = 'past_contests'
    all_sorts = frozenset(('name', 'user_count', 'start_time'))
    default_desc = frozenset(('name', 'user_count'))
    default_sort = '-start_time'

    @cached_property
    def _now(self):
        return timezone.now()

    def _get_queryset(self):
        queryset = super().get_queryset().prefetch_related(
            'tags',
            'organizations',
            'authors',
            'curators',
            'testers',
            'spectators',
            'classes',
        )

        profile = self.request.profile
        if not profile:
            return queryset

        return queryset.annotate(
            editor_or_tester=Exists(Contest.authors.through.objects.filter(contest=OuterRef('pk'), profile=profile)) |
            Exists(Contest.curators.through.objects.filter(contest=OuterRef('pk'), profile=profile)) |
            Exists(Contest.testers.through.objects.filter(contest=OuterRef('pk'), profile=profile)) |
            Exists(Contest.spectators.through.objects.filter(contest=OuterRef('pk'), profile=profile)),
            completed_contest=Exists(ContestParticipation.objects.filter(contest=OuterRef('pk'), user=profile,
                                                                         virtual=ContestParticipation.LIVE)),
        )

    def get_queryset(self):
        self.search_query = None
        queryset = self._get_queryset().order_by(self.order, 'key').filter(end_time__lt=self._now)
        if 'search' in self.request.GET:
            terms = [t.strip() for t in self.request.GET.getlist('search') if t.strip()]
            self.search_query = ' '.join(terms) or None
            for term in terms:
                queryset = queryset.filter(Q(key__icontains=term) | Q(name__icontains=term))
        return queryset

    def get_paginator(self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs):
        return super().get_paginator(queryset, per_page, orphans, allow_empty_first_page,
                                     count=self.get_queryset().values('id').count(), **kwargs)

    def get_context_data(self, **kwargs):
        context = super(ContestList, self).get_context_data(**kwargs)
        present, active, future = [], [], []
        finished = set()
        for contest in self._get_queryset().exclude(end_time__lt=self._now):
            if contest.start_time > self._now:
                future.append(contest)
            else:
                present.append(contest)

        if self.request.user.is_authenticated:
            for participation in (
                ContestParticipation.objects.filter(virtual=0, user=self.request.profile, contest_id__in=present)
                .select_related('contest')
                .prefetch_related('contest__authors', 'contest__curators', 'contest__testers', 'contest__spectators')
                .annotate(key=F('contest__key'))
            ):
                if participation.ended:
                    finished.add(participation.contest.key)
                else:
                    active.append(participation)
                    present.remove(participation.contest)

        active.sort(key=attrgetter('end_time', 'key'))
        present.sort(key=attrgetter('end_time', 'key'))
        future.sort(key=attrgetter('start_time'))
        context['active_participations'] = active
        context['current_contests'] = present
        context['future_contests'] = future
        context['finished_contests'] = finished
        context['now'] = self._now
        context['first_page_href'] = '.'
        context['page_suffix'] = '#past-contests'
        context['search_query'] = self.search_query
        context.update(self.get_sort_context())
        context.update(self.get_sort_paginate_context())
        return context


class ContestListV2(ContestInSessionListRedirectMixin, QueryStringSortMixin, DiggPaginatorMixin, TitleMixin,
                    ContestListMixin, ListView):
    """Redesigned contest list with filters, sorting, and layout options.

    All matching contests are rendered and filtering happens client-side (no
    pagination), so the filters update instantly without reloads.
    """
    model = Contest
    paginate_by = None
    template_name = 'contest/list-v2.html'
    title = gettext_lazy('Assignments')
    context_object_name = 'past_contests'
    all_sorts = frozenset(('name', 'user_count', 'start_time', 'end_time'))
    default_desc = frozenset(('name', 'user_count'))
    default_sort = '-start_time'
    # Section grouping modes for the list: by assignment status or by course.
    group_modes = frozenset(('status', 'course'))
    default_group = 'course'
    # Status filter for the list (single-select, applied client-side).
    time_filters = frozenset(('active', 'ongoing', 'upcoming', 'past'))

    @cached_property
    def _now(self):
        return timezone.now()

    @property
    def group(self):
        group = self.request.GET.get('group', self.default_group)
        return group if group in self.group_modes else self.default_group

    @property
    def time_filter(self):
        """Selected status filter ('' = all). Applied client-side; this is only
        used to render the initial dropdown selection."""
        tf = self.request.GET.get('time', '')
        return tf if tf in self.time_filters else ''

    def _get_queryset(self):
        queryset = super().get_queryset().prefetch_related(
            'tags',
            'organizations',
            'authors__user',
            'curators',
            'testers',
            'spectators',
            'classes',
        )

        profile = self.request.profile
        if not profile:
            return queryset

        return queryset.annotate(
            editor_or_tester=Exists(Contest.authors.through.objects.filter(contest=OuterRef('pk'), profile=profile)) |
            Exists(Contest.curators.through.objects.filter(contest=OuterRef('pk'), profile=profile)) |
            Exists(Contest.testers.through.objects.filter(contest=OuterRef('pk'), profile=profile)) |
            Exists(Contest.spectators.through.objects.filter(contest=OuterRef('pk'), profile=profile)),
            completed_contest=Exists(ContestParticipation.objects.filter(contest=OuterRef('pk'), user=profile,
                                                                         virtual=ContestParticipation.LIVE)),
        )

    def get_queryset(self):
        queryset = self._get_queryset().order_by(self.order, 'key').filter(end_time__lt=self._now)
        return queryset

    def _can_see_assignment_authors(self):
        """Teachers may filter assignments by contest author; students may not."""
        from judge.views.problem import user_can_see_problem_author_metadata
        return bool(user_can_see_problem_author_metadata(self.request.user))

    def get_context_data(self, **kwargs):
        context = super(ContestListV2, self).get_context_data(**kwargs)
        tf = self.time_filter
        finished = set()
        active = []

        # All contests are rendered; filtering is applied client-side.
        ongoing_future = self._get_queryset().exclude(end_time__lt=self._now)
        present_all, future_all = [], []
        for contest in ongoing_future:
            if contest.start_time > self._now:
                future_all.append(contest)
            else:
                present_all.append(contest)

        if self.request.user.is_authenticated:
            present_ids = [c.id for c in present_all]
            for participation in (
                ContestParticipation.objects.filter(virtual=0, user=self.request.profile, contest_id__in=present_ids)
                .select_related('contest')
                .prefetch_related('contest__authors', 'contest__curators', 'contest__testers', 'contest__spectators',
                                  'contest__organizations', 'contest__classes')
                .annotate(key=F('contest__key'))
            ):
                if participation.ended:
                    finished.add(participation.contest.key)
                else:
                    active.append(participation)
                    present_all.remove(participation.contest)

        active.sort(key=attrgetter('end_time', 'key'))
        present_all.sort(key=attrgetter('end_time', 'key'))
        future_all.sort(key=attrgetter('start_time'))

        # All sections are rendered server-side; the status filter is applied
        # client-side for instant switching.
        present = present_all
        future = future_all

        context['active_participations'] = active
        context['current_contests'] = present
        context['future_contests'] = future
        context['finished_contests'] = finished
        context['now'] = self._now
        context['first_page_href'] = '.'
        context['page_suffix'] = '#past-contests' if self.group == 'status' else ''
        context['search_terms'] = [t.strip() for t in self.request.GET.getlist('search') if t.strip()]
        context['search_query'] = ' '.join(context['search_terms'])

        # Additional context for v2
        # Filter options are scoped to the contests the user can actually see,
        # so private course/class/tag names never leak to unauthorized visitors
        # (the contest rows themselves are gated by get_visible_contests).
        visible_contests = self._get_queryset()
        context['organizations'] = Organization.objects.filter(contest__in=visible_contests).distinct()
        context['classes'] = (
            Class.objects.filter(contest__in=visible_contests)
            .select_related('organization')
            .distinct()
            .order_by('organization__name', 'name')
        )
        context['tags'] = ContestTag.objects.filter(contests__in=visible_contests).distinct()
        can_see_authors = self._can_see_assignment_authors()
        context['can_see_problem_authors'] = can_see_authors
        selected_authors = []
        if can_see_authors:
            context['contest_authors'] = (
                Profile.objects.filter(authored_contests__in=visible_contests)
                .select_related('user')
                .distinct()
                .order_by('user__username')
            )
            for raw in self.request.GET.getlist('author'):
                for part in str(raw).split(','):
                    part = part.strip()
                    if part.isdigit():
                        selected_authors.append(int(part))
            if selected_authors:
                author_map = {
                    profile.id: profile
                    for profile in Profile.objects.filter(pk__in=selected_authors).select_related('user')
                }
                context['selected_author_profiles'] = [
                    author_map[pk] for pk in selected_authors if pk in author_map
                ]
            else:
                context['selected_author_profiles'] = []
        else:
            context['contest_authors'] = Profile.objects.none()
            context['selected_author_profiles'] = []
        context['selected_authors'] = selected_authors
        context['total_count'] = self._total_count(active, present, future)
        context['sort_by'] = self.order
        context['group'] = self.group
        context['time_filter'] = tf
        if self.group == 'course':
            course_groups, contest_statuses = self._get_course_groups(context)
            context['course_groups'] = course_groups
            context['contest_statuses'] = contest_statuses

        context.update(self.get_sort_context())
        context.update(self.get_sort_paginate_context())
        return context

    def _total_count(self, active, present, future):
        """Number of assignments matching the (server-side) filters."""
        return len(active) + len(present) + len(future) + self.get_queryset().values('id').count()

    def _get_course_groups(self, context):
        """Group the displayed assignments by their first organization (course).

        Mirrors the mock's "By course" view: one section per course, with each
        row still showing its status badge. Assignments without an organization
        fall into a generic "General course" section.
        """
        groups = {}
        statuses = {}

        def add(contest, status):
            statuses[contest.key] = status
            orgs = list(contest.organizations.all())
            name = orgs[0].name if orgs else _('General course')
            groups.setdefault(name, []).append(contest)

        for participation in context['active_participations']:
            add(participation.contest, 'active')
        for contest in context['current_contests']:
            add(contest, 'ongoing')
        for contest in context['future_contests']:
            add(contest, 'upcoming')
        for contest in context['past_contests']:
            add(contest, 'past')

        field = self.order.lstrip('-')
        if field not in ('start_time', 'end_time', 'name', 'user_count'):
            field = 'start_time'
        for contests in groups.values():
            contests.sort(key=attrgetter(field), reverse=self.order.startswith('-'))
        return {name: groups[name] for name in sorted(groups)}, statuses


class _ContestKeyConflict(Exception):
    """Insert hit ``Contest.key`` uniqueness, not some other integrity error."""


# Bounded retries when reserving an auto key; a duplicate INSERT means another
# creator won the id and we should try the next free one.
_KEY_RESERVATION_ATTEMPTS = 5


class ContestCreate(LoginRequiredMixin, View):
    """Create or edit a contest with the split layout form.

    ``/contests/create/`` (add) and ``/contests/<key>/edit/`` (edit) share this
    view and the same template; only the title and pre-fill differ.

    Every field below maps 1:1 onto the ``Contest`` model — i.e. a column of
    the ``judge_contest`` table — so "registering" a field in one of the
    ``_*_fields`` tuples is all it takes for the frontend editor to read/write
    it, staying in sync with the Django admin's contest editor.
    """
    template_name = 'contest/create.html'

    # Scalar fields handled generically from the JSON payload.
    _bool_fields = (
        'is_visible', 'is_rated', 'use_clarifications', 'hide_problem_tags',
        'hide_problem_authors', 'allow_virtual_participation', 'allow_spectate', 'run_pretests_only',
        'rate_all', 'tester_see_submissions', 'tester_see_scoreboard',
        'show_short_display', 'is_private', 'is_organization_private',
        'limit_join_organizations', 'seb_required', 'allow_ai_hints',
        'allow_student_ai_grading', 'hide_sandbox_output',
    )
    _text_fields = (
        'name', 'description', 'access_code', 'problem_label_script',
        'og_image', 'logo_override_image', 'summary',
        'seb_browser_exam_keys', 'seb_config_keys', 'seb_quit_url',
    )
    _int_fields = ('points_precision', 'rating_floor', 'rating_ceiling', 'performance_ceiling_override')
    _m2m_fields = (
        'curators', 'testers', 'spectators', 'rate_exclude',
        'view_contest_scoreboard', 'view_contest_submissions', 'banned_users',
        'organizations', 'classes', 'join_organizations', 'tags', 'private_contestants',
    )

    def _restricted_fields(self, user):
        """Fields the current user may NOT write, mirroring the Django admin.

        ``ContestAdmin.get_readonly_fields`` gates sensitive fields behind their
        own model permissions (``contest_access_code``, ``contest_seb_requirement``,
        ``change_contest_visibility``, ``contest_rating``, ``lock_contest``,
        ``contest_problem_label``, ``override_performance_ceiling`` and
        ``create_private_contest``).

        The frontend editor must enforce the exact same rules server-side:
        otherwise any curator holding only ``judge.edit_own_contest`` could
        bypass every one of those permissions by POSTing the field directly
        (e.g. flip ``is_visible`` on a contest they merely curate, or arm SEB
        without the ``contest_seb_requirement`` permission).

        Returns a ``set`` of model field names that must be skipped when
        applying the payload — and hidden in the template, so the UI only ever
        shows controls the user is actually allowed to touch. Kept in lock-step
        with ``ContestAdmin.get_readonly_fields``: if that list changes, change
        this one too.
        """
        restricted = set()
        if not user.has_perm('judge.contest_rating'):
            restricted |= {'is_rated', 'rate_all', 'rate_exclude'}
        if not user.has_perm('judge.lock_contest'):
            restricted.add('locked_after')
        if not user.has_perm('judge.contest_access_code'):
            restricted.add('access_code')
        if not user.has_perm('judge.contest_seb_requirement'):
            restricted |= {'seb_required', 'seb_browser_exam_keys', 'seb_config_keys',
                           'seb_config_file', 'seb_quit_url', 'seb_preset'}
        if not user.has_perm('judge.create_private_contest'):
            restricted |= {'private_contestants', 'organizations', 'is_private', 'is_organization_private'}
            # is_visible is only gated when the user can neither create private
            # contests nor change visibility — same nesting as the admin.
            if not user.has_perm('judge.change_contest_visibility'):
                restricted.add('is_visible')
        if not user.has_perm('judge.contest_problem_label'):
            restricted.add('problem_label_script')
        if not user.has_perm('judge.override_performance_ceiling'):
            restricted.add('performance_ceiling_override')

        # Roles (authors / curators / testers / spectators) are managed by the
        # contest's authors (or full editors) only — a mere curator must not be
        # able to promote themselves or reassign the contest. On create the
        # creator becomes an author, so the roles stay editable.
        contest = getattr(self, 'contest', None)
        if not user.has_perm('judge.edit_all_contest'):
            if contest is None:
                pass  # create mode: the creator becomes an author
            elif not user.is_authenticated or user.profile.id not in contest.author_ids:
                restricted |= {'authors', 'curators', 'testers', 'spectators'}
        return restricted

    def dispatch(self, request, *args, **kwargs):
        """Route to create or edit mode and enforce the matching permission.

        Aligns with ContestAdmin:
        - Create (no ``contest`` kwarg) requires ``judge.add_contest``.
        - Edit requires ``is_changeable_by`` (``edit_own_contest`` plus
          ``is_editable_by``). Grade still uses ``is_editable_by`` alone.
        """
        self.contest = None
        if 'contest' in kwargs:
            self.contest = get_object_or_404(Contest, key=kwargs['contest'])
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not self.contest.is_changeable_by(request.user):
                raise PermissionDenied
        else:
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not Contest.can_be_added_by(request.user):
                raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        context = self.get_context_data()
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': _('Not authenticated.')}, status=401)
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            # A browser sending a non-JSON body (e.g. a native form POST after
            # the page's JS failed to attach its submit handler) lands here.
            # Log enough to diagnose it from the server side.
            logger.warning('Contest save rejected: expected JSON body, got content-type=%r body=%r',
                           request.content_type, request.body[:500])
            return JsonResponse({'error': _('Invalid request.')}, status=400)
        if self.contest is None:
            return self._create(request, payload)
        return self._update(request, payload)

    # ── payload → model helpers ─────────────────────────────────────────────

    @staticmethod
    def _parse_datetime(payload, field):
        """Parse an ISO-8601 payload value into an aware UTC datetime.

        The frontend always sends UTC strings ending in ``Z`` (from
        ``Date.toISOString()``); naive local strings are also accepted and
        interpreted as UTC to match ``settings.TIME_ZONE``. Returns ``None``
        when the field is blank. Raises ``ValidationError`` on bad input.
        """
        value = payload.get(field)
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            raise ValidationError(_('Invalid date format for "%(field)s".') % {'field': field})
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.utc)
        return dt

    @staticmethod
    def _parse_duration(payload):
        """Convert the frontend's hours/minutes inputs into a ``timedelta``.

        A blank or zero duration means "no time limit" (``time_limit`` = None).

        The values are bounded (0–1000 h / 0–59 min) and coerced defensively:
        a hostile or malformed payload must not be able to raise
        ``OverflowError`` inside ``timedelta`` (→ 500) or store a negative /
        absurd time limit.
        """
        if not (payload.get('duration_hours') or payload.get('duration_minutes')):
            return None
        try:
            hours = int(payload.get('duration_hours', 0) or 0)
            minutes = int(payload.get('duration_minutes', 0) or 0)
        except (TypeError, ValueError, OverflowError):
            # OverflowError: json.loads('1e309') yields float('inf') — int(inf)
            # overflows. A hostile body must 400 with a readable message,
            # never 500 the worker.
            raise ValidationError(_('Invalid duration.'))
        if not (hours or minutes):
            return None
        if not 0 <= hours <= 1000 or not 0 <= minutes <= 59:
            raise ValidationError(_('Duration must be between 0 and 1000 hours.'))
        return timedelta(hours=hours, minutes=minutes)

    def _apply_payload(self, contest, payload):
        """Copy every supported field from the payload onto the contest object.

        Only assigns attributes — it never calls ``save()``. Shared by the
        create and update flows so the two modes cannot drift apart. Raises
        ``ValidationError`` (→ HTTP 400) for invalid dates, bad format JSON or
        an unregistered ``format_name``.

        Three security-critical rules are enforced here:

        * **Granular permission gating** — fields the current user may not
          touch (per ``_restricted_fields``, mirroring the Django admin) are
          skipped entirely. This is the server-side backstop: even if the
          frontend were bypassed, a curator cannot widen visibility, arm SEB,
          mark a contest rated, lock it, or change label scripts / ceilings.
        * **Format whitelist** — ``format_name`` must name a registered contest
          format. Anything else would later blow up ``Contest.format_class``
          (KeyError) on every page that renders the contest, bricking it.
        * **Single source of truth for visibility** — when the payload carries
          the ``vis_mode`` seg control it derives the three underlying flags
          (``is_visible`` / ``is_private`` / ``is_organization_private``),
          instead of letting the UI and the backend disagree about state.

        Note: model-level validation (``Contest.clean()`` / field lengths /
        SEB keys / label script) is NOT run here — it happens via
        ``full_clean()`` in ``_create`` / ``_update`` right before ``save()``,
        so invalid input becomes a readable 400 instead of bricking the row.
        """
        restricted = self._restricted_fields(self.request.user)

        # ── List visibility: vis_mode seg control is authoritative ──────────
        # Maps the four-option radio onto the three underlying flags, touching
        # only flags the user may actually modify (is_visible is admin-gated
        # for users without change_contest_visibility).
        vis_mode = payload.get('vis_mode')
        vis_applied = vis_mode in ('hidden', 'public', 'org', 'private')
        if vis_applied:
            can_edit_visible = 'is_visible' not in restricted
            # Privacy flags (is_private / is_organization_private) need
            # create_private_contest. Without it a curator must never be able
            # to flip a shared contest private (locking out students) or public
            # (leaking a private contest) via the vis_mode seg control — the
            # flags are kept at their stored values instead.
            can_edit_private = 'is_private' not in restricted
            if vis_mode == 'hidden':
                if can_edit_visible:
                    contest.is_visible = False
                if can_edit_private:
                    contest.is_private = False
                    contest.is_organization_private = False
            elif vis_mode == 'public':
                if can_edit_visible:
                    contest.is_visible = True
                if can_edit_private:
                    contest.is_private = False
                    contest.is_organization_private = False
            elif vis_mode == 'org':
                if can_edit_visible:
                    contest.is_visible = True
                if can_edit_private:
                    contest.is_private = False
                    contest.is_organization_private = True
            elif vis_mode == 'private':
                if can_edit_visible:
                    contest.is_visible = True
                if can_edit_private:
                    contest.is_private = True
                    contest.is_organization_private = False

        # ── Plain scalars: booleans, text, and nullable integers ────────────
        for field in self._bool_fields:
            if field in restricted:
                # Permission-gated: keep whatever is already stored.
                continue
            if vis_applied and field in ('is_visible', 'is_private', 'is_organization_private'):
                # Already decided by the vis_mode seg control above.
                continue
            # The JS layer already converts checkboxes to real booleans.
            setattr(contest, field, bool(payload.get(field, False)))
        for field in self._text_fields:
            if field in restricted:
                continue
            setattr(contest, field, payload.get(field, ''))
        for field in self._int_fields:
            if field in restricted:
                continue
            value = payload.get(field)
            try:
                parsed = int(value) if value not in (None, '') else None
            except (TypeError, ValueError, OverflowError):
                # OverflowError guards float('inf') payloads (json.loads('1e309')).
                parsed = None
            if parsed is None:
                # Fall back to the model's default: points_precision is NOT NULL
                # (default 3), the rating bounds are nullable (default None).
                parsed = contest._meta.get_field(field).get_default()
            setattr(contest, field, parsed)

        # ── SEB: opt into the staff-published default configuration ─────────
        # The payload carries a boolean, never a preset id: an instructor may
        # take the site default but must not be able to point their contest at
        # an arbitrary (retired, unlisted, half-configured) preset row. Setting
        # the link here — before full_clean() — is what lets a brand-new
        # assignment be SEB-armed in a single save, since the preset satisfies
        # Contest.clean()'s "at least one key" requirement without any upload.
        if 'seb_preset' not in restricted and 'seb_use_default' in payload:
            if payload.get('seb_use_default'):
                # An existing link is kept as-is. Re-resolving on every save
                # would silently move the contest onto whatever preset happens
                # to be default now — changing its Config Key and locking out
                # anyone mid-exam, the exact failure SEBConfigPreset.clean()
                # refuses to allow when the preset itself is edited.
                if contest.seb_preset_id is None:
                    preset = SEBConfigPreset.get_default()
                    if preset is None:
                        raise ValidationError(
                            _('No default Safe Exam Browser configuration has been set up by the site '
                              'administrator yet.'))
                    contest.seb_preset = preset
                # Drop this contest's own file and hashes, so the preset actually
                # takes effect. Every SEB accessor prefers the contest's own
                # value, so leaving them would keep serving the old upload while
                # the editor hides the fields that explain why — the failure that
                # locks out macOS silently, since it authenticates with the
                # Config Key alone and Windows matches on the BEK first.
                # Judged against the STORED row, not the in-flight object: the
                # text loop above has already blanked the key fields from a
                # payload that omits them (the editor hides those inputs once
                # this box is ticked, and a hidden input is not submitted), so
                # the object no longer shows what is really overriding.
                stored = Contest.objects.filter(pk=contest.pk).first() if contest.pk else None
                overrides = [f for f in ('seb_config_file', 'seb_browser_exam_keys', 'seb_config_keys',
                                         'seb_quit_url') if stored and getattr(stored, f)]
                if overrides:
                    # Re-keying a contest students are sitting right now would 403
                    # every one of them, the same reason SEBConfigPreset.clean()
                    # refuses to let a live preset be edited.
                    now = timezone.now()
                    if stored.seb_required and stored.start_time is not None and \
                            stored.end_time is not None and stored.start_time <= now < stored.end_time:
                        raise ValidationError(
                            _('This exam is in progress. Switching it to the default configuration now '
                              'would change the keys its students are authenticating with and lock them '
                              'out. Wait until it ends, or leave it on its own configuration.'))
                    # The stored object is left in place; only the reference is
                    # cleared, so a mistaken switch is recoverable.
                    contest.seb_config_file = None
                    for field in ('seb_browser_exam_keys', 'seb_config_keys', 'seb_quit_url'):
                        setattr(contest, field, '')
            else:
                contest.seb_preset = None

        # ── Format: registered name + optional JSON config blob ─────────────
        # Validating against contest_format.formats (rather than the field's
        # choices) keeps this working even for formats added via the registry.
        format_name = payload.get('format_name', 'default')
        if format_name not in contest_format.formats:
            raise ValidationError(_('Invalid assignment format: "%(name)s".') % {'name': format_name})
        contest.format_name = format_name
        format_config = payload.get('format_config')
        if format_config is not None and str(format_config).strip():
            try:
                contest.format_config = json.loads(format_config)
            except ValueError:
                raise ValidationError(_('Invalid assignment format configuration JSON.'))
        elif format_config is not None:
            contest.format_config = None

        # Exam / Default reject any non-empty config. Switching from ICPC (etc.)
        # leaves leftover JSON in Advanced or in the DB when the frontend omits
        # the key — drop it so full_clean() does not 400 the save.
        if contest.format_name in self._FORMATS_WITHOUT_CONFIG and contest.format_config:
            contest.format_config = None

        # Homework create/edit: omitted visibility means hidden. Django admin
        # and the model field stay SCOREBOARD_VISIBLE (see Contest.scoreboard_visibility).
        scoreboard = payload.get('scoreboard_visibility', Contest.SCOREBOARD_HIDDEN)
        valid_scoreboards = {choice[0] for choice in Contest.SCOREBOARD_VISIBILITY}
        if scoreboard not in valid_scoreboards:
            raise ValidationError(_('Invalid scoreboard visibility.'))
        contest.scoreboard_visibility = scoreboard

        # ── Schedule: start/end are required and must be ordered; lock is optional ──
        start_time = self._parse_datetime(payload, 'start_time')
        end_time = self._parse_datetime(payload, 'end_time')
        if start_time is None or end_time is None:
            raise ValidationError(_('Start and end time are required.'))
        if start_time >= end_time:
            raise ValidationError(_('Start time must be before end time.'))
        contest.start_time = start_time
        contest.end_time = end_time
        if 'locked_after' not in restricted:
            # Locking submissions (preventing rejudge) needs judge.lock_contest.
            contest.locked_after = self._parse_datetime(payload, 'locked_after')

        # Duration: the frontend edits hours+minutes, the model stores a timedelta.
        contest.time_limit = self._parse_duration(payload)

        # Student AI grading checkbox (teachers always have AI via Grade page).
        # Keep legacy ai_grading_mode in sync for older code paths / admin.
        if 'allow_student_ai_grading' in payload:
            allow_student = bool(payload.get('allow_student_ai_grading'))
        else:
            # Backward-compatible JSON clients that still post ai_grading_mode.
            mode = payload.get('ai_grading_mode') or Contest.AI_GRADING_OFF
            if mode not in dict(Contest.AI_GRADING_MODE_CHOICES):
                raise ValidationError(_('Invalid AI grading mode.'))
            allow_student = mode == Contest.AI_GRADING_REALTIME
        contest.allow_student_ai_grading = allow_student
        contest.ai_grading_mode = (
            Contest.AI_GRADING_REALTIME if allow_student else Contest.AI_GRADING_OFF
        )
        try:
            delay = int(payload.get('ai_grading_auto_delay_minutes', 120))
        except (TypeError, ValueError):
            delay = 120
        # −1 = Off; 0 = at end time; positive = minutes after end.
        contest.ai_grading_auto_delay_minutes = max(-1, min(delay, 24 * 60))

        # ── Duration increase follows through to joined students ────────────
        # start_time / end_time / time_limit are stored exactly as submitted —
        # the server never rewrites the deadline. A duration *increase* still
        # needs a nudge on the participation rows though:
        # ``ContestParticipation.end_time`` is
        # ``min(real_start + time_limit, contest.end_time)`` anchored to the
        # immutable ``real_start``, so raising ``time_limit`` alone is invisible
        # to anyone whose ``real_start + new_limit`` is already in the past
        # (they still see "Your time is up!").
        #
        # The extension is still capped by ``end_time``: raising the duration
        # without also pushing the deadline out gives students nothing extra,
        # by design. The editor pre-fills the recomputed deadline into the
        # visible end-time field so the teacher sees it and can override it.
        existing = getattr(self, 'contest', None)
        old_row = None
        if existing is not None and existing.pk is not None:
            # IMPORTANT: the old values must come from the DATABASE, not from
            # ``contest`` — by this point ``_apply_payload`` has already written
            # the new duration/end_time onto it, and ``self.contest`` IS that
            # same object, so reading ``existing.time_limit`` would see the new.
            old_row = type(existing).objects.filter(pk=existing.pk).values('time_limit', 'end_time').first()
            if old_row:
                old_limit, old_end = old_row['time_limit'], old_row['end_time']
                new_limit = contest.time_limit
                if old_limit is not None and new_limit is not None and new_limit > old_limit \
                        and old_end is not None:
                    # Shift ``real_start`` later so each student's new deadline
                    # becomes ``max(old deadline, now) + increase`` — active
                    # students keep their start (their end just grows), finished
                    # students restart their window from now. Spectators have no
                    # personal window (their end is the contest's), so they are
                    # left untouched.
                    _now = timezone.now()
                    shifted = []
                    for participation in existing.users.exclude(
                            virtual=ContestParticipation.SPECTATE).only('id', 'real_start', 'virtual', 'user_id'):
                        if participation.real_start is None:
                            continue
                        old_deadline = min(participation.real_start + old_limit, old_end)
                        participation.real_start = max(old_deadline, _now) - old_limit
                        shifted.append(participation)
                    if shifted:
                        ContestParticipation.objects.bulk_update(shifted, ['real_start'])
                        self._restore_missing_sessions(shifted)
                elif old_limit is not None and new_limit is None:
                    # Removing the personal limit extends every joined student's
                    # window to "until the contest ends", so restore missing
                    # sessions the same way the increase path does.
                    self._restore_missing_sessions(
                        existing.users.exclude(virtual=ContestParticipation.SPECTATE)
                        .only('id', 'virtual', 'user_id'))
        return contest

    @staticmethod
    def _restore_missing_sessions(participations):
        """Re-point each participation's owner at it when they have no session.

        The middleware clears ``profile.current_contest`` as soon as a
        student's window ends (or the contest becomes temporarily
        inaccessible), and nothing ever sets it back — yet
        ``Problem.is_accessible_by()`` only admits contest problems while
        ``current_contest`` is set. So a teacher extending a contest's window
        (duration increase or limit removal) must also restore the session:
        otherwise the student can SEE the extended timer but gets
        "No such problem" 404s when clicking into any problem.

        Only profiles that are not currently inside ANY contest are touched
        (``current_contest_id is None``) — a student who moved on to another
        contest, or is currently spectating/re-joined elsewhere, is never
        hijacked. When a profile has several matching participations (live +
        virtual), the live (``virtual=0``) one wins. Restored sessions that
        turn out ended are harmless: the middleware re-clears them on the
        student's next request.
        """
        participations = list(participations)
        if not participations:
            return
        profiles = {p.id: p for p in Profile.objects.filter(
            id__in={p.user_id for p in participations})}
        restored = []
        seen = set()
        for participation in sorted(
                participations, key=lambda p: p.virtual != ContestParticipation.LIVE):
            profile = profiles.get(participation.user_id)
            if profile is None or profile.id in seen:
                continue
            seen.add(profile.id)
            if profile.current_contest_id is None:
                profile.current_contest = participation
                restored.append(profile)
        if restored:
            Profile.objects.bulk_update(restored, ['current_contest'])

    def _save_m2m(self, contest, payload):
        """Fully replace each submitted many-to-many field with the given ids.

        Only fields present in the payload are touched: Django's ``.set()`` is
        atomic, so an omitted field keeps its current rows and an empty list
        clears them.

        Permission-gated fields (e.g. ``organizations`` / ``private_contestants``
        without ``create_private_contest``, ``rate_exclude`` without
        ``contest_rating``) are skipped entirely — same rule as the admin.
        """
        restricted = self._restricted_fields(self.request.user)
        for field in self._m2m_fields:
            if field not in payload or field in restricted:
                continue
            values = payload[field]
            if isinstance(values, str):
                values = [values] if values else []
            try:
                ids = [int(v) for v in values if str(v)]
            except (TypeError, ValueError):
                raise ValidationError(_('Invalid value for "%(field)s".') % {'field': field})
            getattr(contest, field).set(ids)

        # Same org→class rule as ContestForm.clean: classes are a subset of
        # the selected organizations, and cannot be set without an organization.
        if 'classes' in payload or 'organizations' in payload:
            self._validate_contest_classes(contest)

    @staticmethod
    def _validate_contest_classes(contest):
        """Reject class ids that do not belong to the contest's organizations."""
        if not contest.classes.exists():
            return
        org_ids = set(contest.organizations.values_list('id', flat=True))
        if not org_ids:
            raise ValidationError(_('You must select organizations before selecting classes.'))
        if contest.classes.exclude(organization_id__in=org_ids).exists():
            raise ValidationError(_('Selected classes must belong to selected organizations.'))

    @staticmethod
    def _contest_problem_points(raw_points, problem):
        """Use the submitted score, or the library problem's own points."""
        if raw_points not in (None, ''):
            try:
                return int(raw_points)
            except (TypeError, ValueError):
                pass
        try:
            return int(problem.points)
        except (TypeError, ValueError):
            return 100

    def _sync_problems(self, contest, payload):
        """Replace the contest's problem list with the submitted selection.

        Accepts either a list of ``{id, points, partial, is_pretested,
        max_submissions, output_prefix, hide_results, display_name}`` dicts
        (what the frontend sends) or a plain list of ids (points default to
        the library problem's own score, partial on, not pretested, unlimited
        submissions, no display-name override). Called on create (initial add)
        and on edit (full replace,
        mirroring the frontend's problem library). A missing/absent
        ``problems`` key is a no-op, so existing problems are preserved unless
        explicitly edited; an explicit empty list clears them.

        Rows are **upserted, never deleted-and-recreated**: ContestSubmission
        rows point at ContestProblem with ``on_delete=CASCADE``, so the old
        delete-then-recreate approach cascade-deleted every ContestSubmission
        (and the ``contest_submission_delete`` signal then detached each
        ``Submission.contest_object``) on *any* edit — submissions silently
        vanished from the grade page / workspace / exports after a pure
        duration change. Only problems genuinely removed from the submitted
        list are deleted.

        **Security:** only problems the editor may see may be attached. The
        problem picker already filters to ``_visible_problems_for``; this check
        is the server-side backstop against a crafted request attaching another
        teacher's hidden problem, which the contest page would then display to
        students (cross-user information disclosure). Problems *already*
        attached to the contest stay allowed, so a curator can keep saving a
        contest that contains an admin-attached problem they cannot otherwise
        see.
        """
        problems = payload.get('problems')
        if problems is None:
            problems = payload.get('problem_ids')
        if problems is None:
            return
        if isinstance(problems, str):
            problems = [problems]

        user = self.request.user
        if user.has_perm('judge.edit_all_contest') or user.has_perm('judge.see_private_problem'):
            # Trusted editor: may attach anything (same trust as the admin picker).
            allowed_ids = None
        else:
            # Everyone else: visible problems ∪ problems already on this contest.
            allowed_ids = set(Problem.get_visible_problems(user).values_list('id', flat=True)) \
                | set(contest.contest_problems.values_list('problem_id', flat=True))

        # Parse the full submitted list first so a security rejection aborts
        # before any row is created/updated.
        parsed = []
        for item in problems:
            if isinstance(item, dict):
                pid = item.get('id')
                raw_points = item.get('points')
                raw_partial = item.get('partial')
                raw_pretested = item.get('is_pretested')
                raw_max_subs = item.get('max_submissions')
                raw_out_prefix = item.get('output_prefix')
                raw_hide_results = item.get('hide_results')
                raw_display_name = item['display_name'] if 'display_name' in item else None
                display_name_provided = 'display_name' in item
            else:
                pid = item
                raw_points = None
                raw_partial = raw_pretested = None
                raw_max_subs = None
                raw_out_prefix = raw_hide_results = None
                raw_display_name = None
                display_name_provided = False
            try:
                problem = Problem.objects.get(pk=int(pid))
            except (TypeError, ValueError, Problem.DoesNotExist):
                continue
            if allowed_ids is not None and problem.id not in allowed_ids:
                raise ValidationError(
                    _('You are not allowed to attach problem "%(code)s" to this assignment.') % {'code': problem.code})
            points = self._contest_problem_points(raw_points, problem)
            # Per-problem options (admin-parity). Explicit booleans win; when
            # the payload omits them (plain id list) the model defaults apply.
            partial = bool(raw_partial) if raw_partial is not None else True
            is_pretested = bool(raw_pretested) if raw_pretested is not None else False
            try:
                # Blank / 0 → unlimited (model validator rejects 0: min 1 or None).
                max_submissions = int(raw_max_subs) if raw_max_subs not in (None, '') else None
                if max_submissions is not None and max_submissions < 1:
                    max_submissions = None
            except (TypeError, ValueError):
                max_submissions = None
            # output_prefix_override: nullable int (blank → model default 0).
            try:
                output_prefix = int(raw_out_prefix) if raw_out_prefix not in (None, '') else None
            except (TypeError, ValueError):
                output_prefix = None
            # hide_results: three-state — None/'default' → unset, True/'hide' → hidden,
            # False/'show' → shown (defaults to the question type's policy).
            if raw_hide_results in (True, 'true', 'hide', 1, '1'):
                hide_results = True
            elif raw_hide_results in (False, 'false', 'show', 0, '0'):
                hide_results = False
            else:
                hide_results = None
            if display_name_provided:
                display_name = str(raw_display_name or '').strip()
                if len(display_name) > 100:
                    raise ValidationError(_('Problem display name must be at most 100 characters.'))
                # Same text as the library title is not an override: keep
                # display_name empty so later library edits show up. Only the
                # canonical problem.name is compared; a translated name is stored
                # as an override (intentional: the teacher typed that title).
                if display_name == problem.name:
                    display_name = ''
            else:
                display_name = None
            parsed.append((problem, points, partial, is_pretested,
                           max_submissions, output_prefix, hide_results, display_name))

        # Upsert (never delete-and-recreate). ContestSubmission.problem points
        # at ContestProblem with on_delete=CASCADE, so wiping the rows here
        # would cascade-delete every ContestSubmission and the
        # contest_submission_delete signal would then detach each
        # Submission.contest_object — silently erasing every prior submission
        # from the grade page / workspace / exports on any edit (even a pure
        # duration change). Only problems genuinely removed from the list may
        # be deleted; everything else is updated in place so the FK links stay.
        existing = {cp.problem_id: cp for cp in contest.contest_problems.all()}
        submitted_ids = {problem.id for problem, *_ in parsed}
        ContestProblem.objects.filter(contest=contest).exclude(problem_id__in=submitted_ids).delete()
        for order, (problem, points, partial, is_pretested,
                    max_submissions, output_prefix, hide_results, display_name) in enumerate(parsed, 1):
            cp = existing.get(problem.id)
            if cp is None:
                cp = ContestProblem(contest=contest, problem=problem)
            cp.points = points
            cp.partial = partial
            cp.is_pretested = is_pretested
            cp.max_submissions = max_submissions
            cp.output_prefix_override = output_prefix
            cp.hide_results = hide_results
            if display_name is not None:
                cp.display_name = display_name
            cp.order = order
            cp.save()

    # Formats whose validate() rejects any non-empty config dict. Keep in sync
    # with DefaultContestFormat / ExamContestFormat.
    _FORMATS_WITHOUT_CONFIG = frozenset(('default', 'exam'))

    @staticmethod
    def _scoring_snapshot(contest):
        """Inputs that change how existing submissions are scored / ranked."""
        problems = list(
            contest.contest_problems.order_by('order').values_list(
                'problem_id', 'points', 'partial', 'is_pretested', 'order',
            ),
        )
        config = contest.format_config
        if isinstance(config, dict):
            config = json.dumps(config, sort_keys=True)
        return (contest.format_name, config, problems)

    @staticmethod
    def _queue_rescore(contest_key):
        """Recalculate standings after commit — same path as Django admin."""
        from judge.tasks import rescore_contest
        transaction.on_commit(rescore_contest.s(contest_key).delay)

    # ── create / update ─────────────────────────────────────────────────────

    @staticmethod
    def _error_response(exception):
        """Map a save failure to a JSON error response.

        Validation errors (bad dates/JSON/user input) return their message
        with HTTP 400; integrity errors after the contest row is stored are
        reported as a related-data failure, not an ID conflict. Anything else
        is logged and reported generically so internal details never reach
        the browser.
        """
        if isinstance(exception, ValidationError):
            # Django's ValidationError can carry dicts / lists of messages
            # (e.g. from full_clean()); flatten them into one readable line.
            return JsonResponse({'error': ' '.join(exception.messages)}, status=400)
        if isinstance(exception, IntegrityError):
            logger.exception('Contest save integrity error: %s', exception)
            return JsonResponse({
                'error': _('Could not save assignment details (not an ID conflict). '
                           'Check the problem list and try again.'),
            }, status=400)
        logger.exception('Contest save failed: %s', exception)
        return JsonResponse({'error': _('Failed to save assignment.')}, status=500)

    def _create(self, request, payload):
        """Validate identity, insert a new ``judge_contest`` row, return redirect."""
        if not payload.get('name'):
            return JsonResponse({'error': _('Missing required field: %s') % 'name'}, status=400)

        submitted_key = (payload.get('key') or '').strip()
        if payload.get('key_auto') or not submitted_key:
            # Auto key: reserve the next free id atomically at save time, so two
            # simultaneous creators can never be handed the same key.
            key = None
        else:
            # Manually chosen key: validate it and reject duplicates outright.
            key = submitted_key
            if not re.match(r'^[a-z0-9]+$', key):
                return JsonResponse({'error': _('Assignment ID must contain only lowercase letters and numbers.')},
                                    status=400)
            if Contest.objects.filter(key=key).exists():
                return JsonResponse({
                    'error': _('Assignment ID "%(key)s" already exists. Choose a different ID.') % {'key': key},
                }, status=400)

        try:
            contest = self._save_new_contest(request, payload, key)
        except _ContestKeyConflict:
            return JsonResponse({
                'error': _('Assignment ID "%(key)s" already exists. '
                           'Choose a different ID.') % {'key': submitted_key or key},
            }, status=400)
        except Exception as e:
            return self._error_response(e)

        contest_id = contest.pk
        from judge.utils.assignment_schedule import reschedule_assignment_harvest
        from judge.utils.contest_ai import schedule_batch_ai_grade
        transaction.on_commit(lambda cid=contest_id: schedule_batch_ai_grade(cid))
        transaction.on_commit(lambda cid=contest_id: reschedule_assignment_harvest(cid))
        return self._action_response(payload, contest.key)

    def _save_new_contest(self, request, payload, key):
        """Insert one contest row and its related data.

        ``key=None`` requests an auto key, reserved atomically: the next free
        id is inserted inside the same transaction, and a concurrent insert
        that wins the id makes us retry with the next free id.
        ``IntegrityError`` on a *provided* key is a collision and is re-raised
        as ``_ContestKeyConflict``. Later integrity errors (problem list, M2M)
        propagate unchanged so they are not reported as ID issues.
        """
        for _attempt in range(_KEY_RESERVATION_ATTEMPTS):
            candidate = key or Contest.generate_unique_key()
            try:
                with transaction.atomic():
                    contest = Contest(key=candidate)
                    self._apply_payload(contest, payload)
                    contest.full_clean()
                    try:
                        contest.save()
                    except IntegrityError:
                        raise _ContestKeyConflict
                    self._save_m2m(contest, payload)
                    contest.authors.add(request.profile)
                    self._sync_problems(contest, payload)
                    self._log_assignment(request, contest, added=True)
            except _ContestKeyConflict:
                if key is None:
                    continue  # auto key raced; reserve the next free id
                raise
            return contest
        raise _ContestKeyConflict

    def _update(self, request, payload):
        """Update an existing contest row (and optionally its key) in place."""
        contest = self.contest
        try:
            with transaction.atomic():
                # Renaming the key (the URL slug) needs a uniqueness check that
                # excludes this very contest.
                new_key = payload.get('key') or contest.key
                if new_key != contest.key:
                    if not re.match(r'^[a-z0-9]+$', new_key):
                        return JsonResponse(
                            {'error': _('Assignment ID must contain only lowercase letters and numbers.')},
                            status=400,
                        )
                    if Contest.objects.filter(key=new_key).exclude(pk=contest.pk).exists():
                        return JsonResponse({
                            'error': _('Assignment ID "%(key)s" already exists. Choose a different ID.') % {
                                'key': new_key,
                            },
                        }, status=400)
                    contest.key = new_key

                before_score = self._scoring_snapshot(contest)
                self._apply_payload(contest, payload)
                # Same model-level validation as create: an invalid edit must
                # 400 with a readable message, not brick the contest.
                contest.full_clean()
                contest.save()
                self._save_m2m(contest, payload)
                # Authors are only editable here; create auto-adds the current user.
                if 'authors' in payload:
                    values = payload['authors']
                    if isinstance(values, str):
                        values = [values] if values else []
                    try:
                        contest.authors.set([int(v) for v in values if str(v)])
                    except (TypeError, ValueError):
                        raise ValidationError(_('Invalid value for "authors".'))
                self._sync_problems(contest, payload)
                contest_id = contest.pk
                contest_key = contest.key
                if self._scoring_snapshot(contest) != before_score:
                    self._queue_rescore(contest_key)
                self._log_assignment(request, contest, added=False)
            from judge.utils.assignment_schedule import reschedule_assignment_harvest
            from judge.utils.contest_ai import schedule_batch_ai_grade
            transaction.on_commit(lambda cid=contest_id: schedule_batch_ai_grade(cid))
            transaction.on_commit(lambda cid=contest_id: reschedule_assignment_harvest(cid))
            return self._action_response(payload, contest_key)
        except Exception as e:
            return self._error_response(e)

    @staticmethod
    def _log_assignment(request, contest, added):
        """Write a Django admin LogEntry so frontend saves show up in Log entries."""
        LogEntry.objects.log_action(
            user_id=request.user.pk,
            content_type_id=ContentType.objects.get_for_model(Contest).pk,
            object_id=contest.pk,
            object_repr=_('Assignment: %s') % contest.name,
            action_flag=ADDITION if added else CHANGE,
            change_message=_('Added assignment.') if added else _('Changed assignment.'),
        )

    @staticmethod
    def _action_response(payload, key, *, return_url=None, extra=None):
        """Build the post-save response from the ``action`` button clicked.

        ``save`` (default) → redirect to the contest page; ``continue`` → back
        to the editor for another round of edits; ``another`` → no redirect —
        the frontend clears the form and stays put to create the next contest.
        """
        action = payload.get('action') if isinstance(payload, dict) else None
        if action not in ('save', 'continue', 'another'):
            action = 'save'
        body = {'ok': True, 'action': action, 'key': key}
        if extra:
            body.update(extra)
        if action == 'another':
            return JsonResponse(body)
        if action == 'continue':
            body['redirect'] = reverse('contest_edit', args=[key])
        elif action == 'save' and return_url:
            body['redirect'] = return_url
        else:
            body['redirect'] = reverse('contest_view', args=[key])
        return JsonResponse(body)

    # ── template context ────────────────────────────────────────────────────

    def _visible_problems_for(self, user):
        """Problems the editor may attach to a contest (library + server check).

        Full editors (``edit_all_contest`` / ``see_private_problem``) may
        attach anything — the same trust as the Django admin's problem picker.
        Everyone else is limited to what they could already see via
        ``Problem.get_visible_problems``: public problems, their own
        authored/curated/tested problems, and the org-private problems of
        organizations they administer. This keeps a curator who merely holds
        ``edit_own_contest`` from attaching — and thereby leaking — a
        colleague's hidden problem through a contest that students can see.
        """
        if user.has_perm('judge.edit_all_contest') or user.has_perm('judge.see_private_problem'):
            return Problem.objects.all()
        return Problem.get_visible_problems(user)

    @staticmethod
    def _local_input(dt):
        """Format a stored UTC datetime in the viewer's own timezone.

        The editor stores UTC; ``timezone.localtime`` renders the value in the
        zone the middleware activated for this request (the profile timezone),
        so a teacher who typed 10:00 in their zone sees 10:00 again when
        reopening the form instead of the raw UTC value.
        """
        if not dt:
            return ''
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.utc)
        return timezone.localtime(dt).strftime('%Y-%m-%dT%H:%M')

    def _library_filter_context(self):
        """Filter metadata for the From-library panel (same controls as the workspace drawer)."""
        from judge.models import ProblemType
        from judge.question_types import all_types

        format_types = []
        seen_codes = set()
        for question_type in all_types():
            if question_type.submission_type == ProblemSubmissionType.MCQ:
                code, label = ProblemSubmissionType.MCQ, str(_('Choice question'))
            else:
                code, label = question_type.submission_type, str(question_type.label)
            if code in seen_codes:
                continue
            seen_codes.add(code)
            format_types.append({'code': code, 'label': label})
        reserved_labels = Problem.reserved_auto_type_labels()
        knowledge_types = [
            problem_type for problem_type in ProblemType.objects.order_by('full_name')
            if Problem.is_knowledge_problem_type(problem_type, reserved_labels=reserved_labels)
        ]
        from judge.views.problem import user_can_see_problem_author_metadata

        profile = getattr(self.request, 'profile', None)
        return {
            'lib_format_types': format_types,
            'lib_knowledge_types': knowledge_types,
            'lib_difficulty_options': [
                ('easy', str(_('Easy'))),
                ('medium', str(_('Medium'))),
                ('hard', str(_('Hard'))),
            ],
            'lib_completed_ids': user_completed_ids(profile) if profile is not None else set(),
            'can_see_problem_authors': user_can_see_problem_author_metadata(self.request.user),
        }

    def get_context_data(self):
        """Build the template context, pre-filling every field in edit mode.

        Add mode ships sensible defaults (e.g. virtual participation on,
        ``vis_mode`` = org / Course); edit mode overwrites them from the stored row.
        """
        contest = self.contest
        # Times are shown and entered in the viewer's own profile timezone (the
        # site's standard behaviour, set by TimezoneMiddleware); the stored value
        # is UTC and is echoed back in that same zone via ``_local_input``.
        user_tz_name = getattr(getattr(self.request, 'profile', None), 'timezone', '') \
            or settings.DEFAULT_USER_TIME_ZONE
        prefill = {
            'name': '', 'key': '', 'description': '', 'summary': '', 'og_image': '',
            'logo_override_image': '', 'access_code': '', 'problem_label_script': '',
            'format_config': '', 'format_name': 'exam',
            # New assignment form; admin create still defaults to visible.
            'scoreboard_visibility': Contest.SCOREBOARD_HIDDEN,
            'points_precision': 3, 'rating_floor': '', 'rating_ceiling': '',
            'performance_ceiling_override': '', 'start_time': '', 'end_time': '',
            'locked_after': '', 'duration_hours': 0, 'duration_minutes': 0,
            'seb_browser_exam_keys': '', 'seb_config_keys': '', 'seb_quit_url': '',
            'is_visible': False, 'is_rated': False, 'use_clarifications': True,
            'hide_problem_tags': False, 'hide_problem_authors': False,
            'allow_virtual_participation': True, 'allow_spectate': False, 'run_pretests_only': False,
            'rate_all': False, 'tester_see_submissions': False, 'tester_see_scoreboard': False,
            'show_short_display': False, 'is_private': False, 'is_organization_private': False,
            'limit_join_organizations': False, 'seb_required': False, 'seb_use_default': False,
            'allow_ai_hints': False,
            'allow_student_ai_grading': False,
            'hide_sandbox_output': False,
            'vis_mode': 'org' if self.request.user.has_perm('judge.create_private_contest') else 'hidden',
            'problems': [],
            'target_score': 100,
            'ai_grading_mode': 'off', 'ai_grading_auto_delay_minutes': 120,
        }
        for field in self._m2m_fields:
            prefill[field] = []
        prefill['authors'] = []
        # The preset this contest is actually linked to, which is not
        # necessarily the current site default (staff may have published a
        # newer one, or retired this one).
        linked_preset = None

        if contest is not None:
            total = int(contest.time_limit.total_seconds()) if contest.time_limit else 0
            # Privacy of prefill: sensitive fields the current user may not
            # edit (per _restricted_fields) are blanked so they never reach
            # the browser, even as hidden input values / JS state. The UI
            # already hides those controls; this is defense in depth against
            # a lower-privileged editor reading the page source.
            restricted = self._restricted_fields(self.request.user)
            prefill.update({
                'name': contest.name,
                'key': contest.key,
                'description': contest.description,
                'summary': contest.summary,
                'og_image': contest.og_image,
                'logo_override_image': contest.logo_override_image,
                'access_code': contest.access_code if 'access_code' not in restricted else '',
                'problem_label_script': (contest.problem_label_script
                                         if 'problem_label_script' not in restricted else ''),
                'format_config': json.dumps(contest.format_config, ensure_ascii=False) if contest.format_config else '',
                'format_name': contest.format_name,
                'ai_grading_mode': contest.ai_grading_mode,
                'ai_grading_auto_delay_minutes': contest.ai_grading_auto_delay_minutes,
                'allow_student_ai_grading': contest.allow_student_ai_grading,
                'scoreboard_visibility': contest.scoreboard_visibility,
                'points_precision': contest.points_precision,
                'rating_floor': contest.rating_floor if contest.rating_floor is not None else '',
                'rating_ceiling': contest.rating_ceiling if contest.rating_ceiling is not None else '',
                'performance_ceiling_override': (contest.performance_ceiling_override
                                                 if contest.performance_ceiling_override is not None else ''),
                'start_time': self._local_input(contest.start_time),
                'end_time': self._local_input(contest.end_time),
                'locked_after': self._local_input(contest.locked_after),
                'duration_hours': total // 3600 if total else 0,
                'duration_minutes': (total % 3600) // 60 if total else 0,
                'seb_browser_exam_keys': (contest.seb_browser_exam_keys
                                          if 'seb_browser_exam_keys' not in restricted else ''),
                'seb_config_keys': (contest.seb_config_keys
                                    if 'seb_config_keys' not in restricted else ''),
                'seb_quit_url': contest.seb_quit_url if 'seb_quit_url' not in restricted else '',
                'seb_use_default': (contest.seb_preset_id is not None
                                    if 'seb_preset' not in restricted else False),
            })
            if 'seb_preset' not in restricted:
                linked_preset = contest.seb_preset
            for field in self._bool_fields:
                prefill[field] = getattr(contest, field)
            # Profile M2M → (id, "Name (username)"); organization/class/tag M2M → (id, name).
            for field in self._m2m_fields:
                related = getattr(contest, field)
                if field in ('organizations', 'join_organizations', 'classes'):
                    prefill[field] = [(o.id, o.name) for o in related.all()]
                elif field == 'tags':
                    prefill[field] = [(t.id, t.name) for t in related.all()]
                else:
                    prefill[field] = [(p.id, profile_choice_label(p)) for p in related.select_related('user')]
            prefill['authors'] = [
                (p.id, profile_choice_label(p)) for p in contest.authors.select_related('user')
            ]
            # Problems currently attached to the contest, in display order.
            # Per-problem options (partial / pretested / max submissions / out
            # prefix / result visibility) ride along so the editor round-trips
            # them losslessly.
            prefill['problems'] = [
                {
                    'id': cp.problem_id,
                    'code': cp.problem.code,
                    'name': cp.problem.name,
                    'display_name': cp.display_name or '',
                    'points': cp.points,
                    'letter': str(idx + 1),
                    'partial': cp.partial,
                    'is_pretested': cp.is_pretested,
                    'max_submissions': cp.max_submissions if cp.max_submissions is not None else '',
                    'output_prefix': (cp.output_prefix_override
                                      if cp.output_prefix_override is not None else 10000),
                    'hide_results': cp.hide_results,  # three-state: None/True/False
                    # Library/filter code: C / B / S / F / single / multiple / truefalse.
                    'format': cp.problem.format_filter_code,
                }
                for idx, cp in enumerate(
                    contest.contest_problems.select_related('problem', 'problem__mcq_config').order_by('order'),
                )
            ]
            # Target score (pure UX, not a model field): backfill from the sum
            # of problem points so the editor's target input matches reality.
            prefill['target_score'] = sum(cp.points for cp in contest.contest_problems.all()) or 100

            # List visibility seg control: user-private > org-private > public > hidden.
            # Derived from the STORED FLAGS only — list contents (private_contestants /
            # organizations / classes) are scoping data, not mode selectors. Checking the
            # lists instead would resurrect 'private' every time an assignment that was
            # once private (leaving entries behind) is switched to org/public and reopened.
            if contest.is_private:
                prefill['vis_mode'] = 'private'
            elif contest.is_organization_private:
                prefill['vis_mode'] = 'org'
            elif contest.is_visible:
                prefill['vis_mode'] = 'public'
            else:
                prefill['vis_mode'] = 'hidden'
        # New assignment (create mode): leave `key` blank (the prefill default
        # is already ''). The id is auto-generated and atomically reserved at
        # save time, so pre-filling a "suggestion" here would mislead editors
        # into thinking that exact id is already theirs — two simultaneous
        # creators would see the same suggestion but land on different ids.

        ctx = {
            'title': _('Edit Assignment') if contest else _('Add Assignment'),
            'content_title': _('Edit Assignment') if contest else _('Add Assignment'),
            'now': timezone.now(),
            'organizations': Organization.objects.all(),
            'classes': Class.objects.all(),
            'tags': ContestTag.objects.all(),
            'save_url': reverse('contest_edit', args=[contest.key]) if contest else reverse('contest_create'),
            # Only problems this editor may attach — see _visible_problems_for.
            # select_related/prefetch_related feed the library columns (types /
            # difficulty / orgs / MCQ format) without N+1 queries on a large bank.
            'problems': (self._visible_problems_for(self.request.user)
                         .select_related('mcq_config')
                         .prefetch_related('types', 'organizations', 'authors__user')
                         .order_by('-date')),
            'contest': contest,
            'user_timezone': user_tz_name,
            'is_edit': contest is not None,
            # Fields hidden in the template AND skipped server-side, mirroring
            # ContestAdmin.get_readonly_fields (see _restricted_fields).
            'restricted_fields': self._restricted_fields(self.request.user),
            # Whether the user may scope a contest to a private audience at all
            # (drives which seg options the visibility control offers).
            'can_edit_private': self.request.user.has_perm('judge.create_private_contest'),
            # Whether real students have joined (spectators excluded — a
            # teacher previewing their own assignment must not count). Purely
            # advisory: the editor warns that shortening the duration or the
            # end time will end those students' windows, but never blocks it.
            'has_participants': bool(contest is not None and contest.users.exclude(
                virtual=ContestParticipation.SPECTATE).exists()),
            # The staff-published SEB configuration offered by the one-click
            # checkbox, or None when no administrator has set one up yet (the
            # template then explains why the checkbox is disabled).
            'seb_default_preset': SEBConfigPreset.get_default(),
            # What this contest already uses, so the card names the real
            # configuration rather than whichever one is default today.
            'seb_linked_preset': linked_preset,
            'prefill': prefill,
        }
        ctx.update(self._library_filter_context())
        return ctx


@method_decorator(ensure_csrf_cookie, name='dispatch')
class ContestTagCreateView(LoginRequiredMixin, View):
    """Create a ContestTag from the contest editor's tag input ("Add new").

    The tag field on ``Contest`` is a plain M2M to ``ContestTag``; the editor
    only lets teachers pick existing tags. This endpoint lets them type a new
    tag name, persist it (with an auto-assigned colour), and immediately select
    it — mirroring ``ProblemCreateV2ProblemTypeView`` for problem types.
    Duplicate names return the existing row (get-or-create) so preset pills
    stay instant even when homework/quiz/exam already exist.
    """

    # Rotating palette so consecutive tags get distinct colours automatically.
    _PALETTE = ('#2980B9', '#27AE60', '#E74C3C', '#8E44AD', '#F39C12')

    @staticmethod
    def _can_create(user):
        # Same population that may open the contest editor: full editors,
        # edit_own_contest holders, and anyone listed as author/curator of at
        # least one contest (curators may edit contests even without the
        # permission — see Contest.is_editable_by).
        if not user.is_authenticated:
            return False
        if (user.has_perm('judge.edit_all_contest')
                or user.has_perm('judge.edit_own_contest')
                or user.has_perm('judge.create_private_contest')):
            return True
        from judge.models import Contest as ContestModel
        return (ContestModel.authors.through.objects.filter(profile=user.profile).exists()
                or ContestModel.curators.through.objects.filter(profile=user.profile).exists())

    def post(self, request, *args, **kwargs):
        if not self._can_create(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({'error': _('Invalid request.')}, status=400)

        name = (payload.get('name') or '').strip()
        if not name:
            return JsonResponse({'error': _('Please enter a tag name.')}, status=400)
        if not re.match(r'^[a-z-]+$', name):
            return JsonResponse({'error': _('Lowercase letters and hyphens only.')}, status=400)
        if len(name) > 20:
            return JsonResponse({'error': _('Tag name must be at most 20 characters.')}, status=400)
        tag, _created = ContestTag.objects.get_or_create(
            name=name,
            defaults={'color': self._PALETTE[ContestTag.objects.count() % len(self._PALETTE)]},
        )
        return JsonResponse({'ok': True, 'tag': {'id': tag.id, 'name': tag.name, 'color': tag.color}})


@method_decorator(ensure_csrf_cookie, name='dispatch')
class ContestSEBFileView(LoginRequiredMixin, View):
    """Upload/replace a contest's ``.seb`` Safe Exam Browser config file.

    The admin-only ``seb_config_file`` FileField becomes editable from the
    contest editor via a multipart POST (the main editor save is JSON and
    cannot carry file uploads). Permission mirrors ``ContestCreate`` edit mode:
    the contest must be editable by the requesting user, and — since this
    writes one of the SEB fields — the user must hold
    ``judge.contest_seb_requirement`` just as ``_restricted_fields`` demands for
    every other SEB write path.

    GET streams the contest's current file back to the same audience. The bytes
    live in private object storage under a generated key, so there is no URL an
    editor could link to directly (``querystring_auth`` is off by design — see
    ``judge/storage/s3.py``); every read goes through Django.
    """

    def _editor_or_error(self, request, contest):
        if not request.user.is_authenticated:
            return JsonResponse({'error': _('Not authenticated.')}, status=401)
        if not contest.is_editable_by(request.user) or \
                not request.user.has_perm('judge.contest_seb_requirement'):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        return None

    def get(self, request, *args, **kwargs):
        contest = get_object_or_404(Contest, key=kwargs['contest'])
        denied = self._editor_or_error(request, contest)
        if denied is not None:
            return denied
        if not contest.seb_config_file:
            raise Http404()
        try:
            fh = contest.seb_config_file.open('rb')
        except StorageUnavailableError:
            return HttpResponse(_('The file store is temporarily unavailable. Please try again later.'),
                                status=503, content_type='text/plain')
        return FileResponse(fh, as_attachment=True, filename=f'{contest.key}.seb',
                            content_type='application/seb')

    def post(self, request, *args, **kwargs):
        contest = get_object_or_404(Contest, key=kwargs['contest'])
        denied = self._editor_or_error(request, contest)
        if denied is not None:
            return denied
        if 'seb_config_file' not in request.FILES:
            return JsonResponse({'error': _('No file uploaded.')}, status=400)
        upload = request.FILES['seb_config_file']
        if not upload.name.lower().endswith('.seb'):
            return JsonResponse({'error': _('Only .seb files are accepted.')}, status=400)
        previous = contest.seb_config_file.name if contest.seb_config_file else ''
        try:
            contest.seb_config_file = upload
            contest.save(update_fields=['seb_config_file'])
        except Exception:
            logger.exception('SEB config upload failed for contest %s', contest.key)
            return JsonResponse({'error': _('Failed to upload the file.')}, status=500)
        # Each upload gets a fresh generated key, so the replaced object would
        # otherwise linger in the bucket forever with nothing referencing it.
        # ContestClone copies the field VALUE, so a clone can still point at the
        # same object — deleting it then would take the clone's config with it.
        if previous and previous != contest.seb_config_file.name and \
                not Contest.objects.filter(seb_config_file=previous).exclude(pk=contest.pk).exists():
            try:
                contest.seb_config_file.storage.delete(previous)
            except Exception:
                logger.exception('could not remove replaced SEB config %s', previous)
        return JsonResponse({'ok': True, 'name': upload.name})


class ContestFinalizeView(LoginRequiredMixin, View):
    """Finalize (or un-finalize) an exam-style contest from the editor.

    Only full editors may trigger this (``judge.edit_all_contest``): finalizing
    reveals hidden problem results, rejudges latest submissions and recomputes
    the standings, so it must not be reachable by mere curators. The heavy work
    runs as a celery task via ``transaction.on_commit``, exactly like the admin.
    """

    def post(self, request, *args, **kwargs):
        contest = get_object_or_404(Contest, key=kwargs['contest'])
        if not request.user.is_authenticated:
            return JsonResponse({'error': _('Not authenticated.')}, status=401)
        if not contest.is_editable_by(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        if not request.user.has_perm('judge.edit_all_contest'):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        from judge.tasks import finalize_contest_results, unfinalize_contest_results
        task = finalize_contest_results if self.finalize else unfinalize_contest_results
        transaction.on_commit(task.s(contest.key).delay)
        return JsonResponse({'ok': True})

    @property
    def finalize(self):
        return True


class ContestUnfinalizeView(ContestFinalizeView):
    """Reverse of ``ContestFinalizeView``: re-hide results until finalize again."""

    @property
    def finalize(self):
        return False


class PrivateContestError(Exception):
    def __init__(self, name, is_private, is_organization_private, orgs, classes):
        self.name = name
        self.is_private = is_private
        self.is_organization_private = is_organization_private
        self.orgs = orgs
        self.classes = classes


class ContestMixin(object):
    context_object_name = 'contest'
    model = Contest
    slug_field = 'key'
    slug_url_kwarg = 'contest'

    @cached_property
    def is_editor(self):
        if not self.request.user.is_authenticated:
            return False
        return self.request.profile.id in self.object.editor_ids

    @cached_property
    def is_tester(self):
        if not self.request.user.is_authenticated:
            return False
        return self.request.profile.id in self.object.tester_ids

    @cached_property
    def is_spectator(self):
        if not self.request.user.is_authenticated:
            return False
        return self.request.profile.id in self.object.spectator_ids

    @cached_property
    def can_edit(self):
        return self.object.is_editable_by(self.request.user)

    @cached_property
    def can_change(self):
        return self.object.is_changeable_by(self.request.user)

    @cached_property
    def can_clone(self):
        return self.object.is_cloneable_by(self.request.user)

    def get_context_data(self, **kwargs):
        context = super(ContestMixin, self).get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            try:
                context['live_participation'] = (
                    self.request.profile.contest_history.get(
                        contest=self.object,
                        virtual=ContestParticipation.LIVE,
                    )
                )
            except ContestParticipation.DoesNotExist:
                context['live_participation'] = None
                context['has_joined'] = False
            else:
                context['has_joined'] = True
        else:
            context['live_participation'] = None
            context['has_joined'] = False

        context['now'] = timezone.now()
        context['is_editor'] = self.is_editor
        context['is_tester'] = self.is_tester
        context['is_spectator'] = self.is_spectator
        context['can_edit'] = self.can_edit
        context['can_change'] = self.can_change
        context['can_clone'] = self.can_clone
        context['is_org_admin'] = self.object.is_viewable_by_org_admin(self.request.user)

        if self.object.seb_required:
            context['seb_required'] = True
            context['seb_user_bypasses'] = self.object.user_bypasses_seb(self.request.user)
            context['seb_request_valid'] = request_matches_seb(
                self.request, self.object.seb_allowed_bek(), self.object.seb_allowed_config_keys())
            context['seb_launch'] = seb_launch_context(self.request, self.object)
        else:
            context['seb_required'] = False
            context['seb_user_bypasses'] = True
            context['seb_request_valid'] = True
            context['seb_launch'] = None

        if not self.object.og_image or not self.object.summary:
            metadata = generate_opengraph('generated-meta-contest:%d' % self.object.id,
                                          self.object.description, 'contest')
        context['meta_description'] = self.object.summary or metadata[0]
        context['og_image'] = self.object.og_image or metadata[1]
        context['has_moss_api_key'] = settings.MOSS_API_KEY is not None
        context['logo_override_image'] = self.object.logo_override_image
        if not context['logo_override_image'] and self.object.organizations.count() == 1:
            context['logo_override_image'] = self.object.organizations.first().logo_override_image

        return context

    def get_object(self, queryset=None):
        contest = super(ContestMixin, self).get_object(queryset)

        profile = self.request.profile
        if (profile is not None and
                ContestParticipation.objects.filter(id=profile.current_contest_id, contest_id=contest.id).exists()):
            return contest

        try:
            contest.access_check(self.request.user)
        except Contest.PrivateContest:
            raise PrivateContestError(contest.name, contest.is_private, contest.is_organization_private,
                                      contest.organizations.all(), contest.classes.all())
        except Contest.Inaccessible:
            raise Http404()
        else:
            return contest

    def dispatch(self, request, *args, **kwargs):
        try:
            return super(ContestMixin, self).dispatch(request, *args, **kwargs)
        except Http404:
            key = kwargs.get(self.slug_url_kwarg, None)
            if key:
                return generic_message(request, _('No such contest'),
                                       _('Could not find a contest with the key "%s".') % key)
            else:
                return generic_message(request, _('No such contest'),
                                       _('Could not find such contest.'))
        except PrivateContestError as e:
            return render(request, 'contest/private.html', {
                'error': e, 'title': _('Access to contest "%s" denied') % e.name,
            }, status=403)


class ContestDetail(ContestMixin, TitleMixin, CommentedDetailView):
    template_name = 'contest/contest.html'

    def get_comment_page(self):
        return 'c:%s' % self.object.key

    def get_title(self):
        return self.object.name

    def get_context_data(self, **kwargs):
        context = super(ContestDetail, self).get_context_data(**kwargs)
        problems = list(
            Problem.objects.filter(contests__contest=self.object)
            .order_by('contests__order').defer('description')
            .annotate(has_public_editorial=Case(
                When(solution__is_public=True, solution__publish_on__lte=timezone.now(), then=True),
                default=False,
                output_field=BooleanField(),
            ), contest_points=F('contests__points'), contest_partial=F('contests__partial'))
            .add_i18n_name(self.request.LANGUAGE_CODE),
        )
        stats = assignment_problem_stats(self.object)
        cp_by_pid = {
            cp.problem_id: cp
            for cp in self.object.contest_problems.select_related('problem')
        }
        lang = self.request.LANGUAGE_CODE
        for problem in problems:
            problem_stats = stats.get(problem.id, {'user_count': 0, 'ac_rate': 0.0})
            problem.user_count = problem_stats['user_count']
            problem.ac_rate = problem_stats['ac_rate']
            cp = cp_by_pid.get(problem.id)
            if cp is not None:
                problem.effective_name = contest_problem_display_name(cp, lang)
            else:
                problem.effective_name = problem.i18n_name or problem.name
        context['contest_problems'] = problems
        context['contest_problem_titles'] = {
            problem.id: problem.effective_name for problem in problems
        }
        context['metadata'] = {
            'has_public_editorials': any(
                problem.is_public and problem.has_public_editorial for problem in context['contest_problems']
            ),
        }
        context['metadata'].update(
            **self.object.contest_problems
            .annotate(
                partials_enabled=Case(
                    When(partial=True, problem__partial=True, then=Value(True)),
                    default=Value(False),
                    output_field=BooleanField(),
                ),
                pretests_enabled=Case(
                    When(is_pretested=True, contest__run_pretests_only=True, then=Value(True)),
                    default=Value(False),
                    output_field=BooleanField(),
                ),
            )
            .aggregate(
                has_partials=Sum('partials_enabled'),
                has_pretests=Sum('pretests_enabled'),
                has_submission_cap=Sum('max_submissions'),
                problem_count=Count('id'),
            ),
        )
        # Which problems the viewer has solved / attempted *within this contest*,
        # driving the green checkmark (same visual language as the Problem list
        # page). Results are withheld while an exam-style contest is still
        # masking verdicts (has_hidden_results && not finalized), so the check
        # cannot leak a hidden result before release.
        solved = set()
        attempted = set()
        profile = getattr(self.request, 'profile', None)
        if self.request.user.is_authenticated and profile is not None:
            contest_subs = Submission.objects.filter(contest_object=self.object, user=profile)
            attempted = set(contest_subs.values_list('problem_id', flat=True).distinct())
            if not (self.object.format_class.has_hidden_results and not self.object.results_finalized):
                solved = set(
                    contest_subs.filter(result='AC', case_points__gte=F('case_total'))
                    .values_list('problem_id', flat=True).distinct(),
                )
        context['contest_solved_problem_ids'] = solved
        context['contest_attempted_problem_ids'] = attempted
        context['enable_comments'] = settings.DMOJ_ENABLE_COMMENTS
        context['enable_social'] = settings.DMOJ_ENABLE_SOCIAL
        return context


class ContestKeyCheckView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        if request.GET.get('suggest'):
            key = Contest.generate_unique_key()
            return JsonResponse({'key': key, 'exists': False})

        key = request.GET.get('key', '').strip()
        if not key:
            return JsonResponse({'error': _('Missing assignment ID.')}, status=400)

        exists = Contest.objects.filter(key=key).exists()
        return JsonResponse({'exists': exists})


class ContestClone(ContestMixin, PermissionRequiredMixin, TitleMixin, SingleObjectFormView):
    title = gettext_lazy('Clone Contest')
    template_name = 'contest/clone.html'
    form_class = ContestCloneForm
    permission_required = 'judge.clone_contest'

    def dispatch(self, request, *args, **kwargs):
        # PermissionRequiredMixin checks has_permission() before View.setup(), so
        # bind URL kwargs early for contest-scoped permission checks below.
        self.kwargs = kwargs
        return super().dispatch(request, *args, **kwargs)

    def has_permission(self):
        return super().has_permission()

    def get_initial(self):
        initial = super().get_initial()
        initial['key'] = Contest.generate_unique_key()
        return initial

    def form_valid(self, form):
        contest = self.object

        tags = contest.tags.all()
        organizations = contest.organizations.all()
        private_contestants = contest.private_contestants.all()
        view_contest_scoreboard = contest.view_contest_scoreboard.all()
        contest_problems = list(contest.contest_problems.all())
        old_key = contest.key

        contest.pk = None
        contest.is_visible = False
        contest.user_count = 0
        contest.locked_after = None
        # A clone is a fresh, unrun contest: never carry over the finalize state, or its results would
        # be revealed (and folded into the scoreboard) before it has even started.
        contest.results_finalized = False
        contest.key = form.cleaned_data['key']
        with revisions.create_revision(atomic=True):
            contest.save()
            contest.tags.set(tags)
            contest.organizations.set(organizations)
            contest.private_contestants.set(private_contestants)
            contest.view_contest_scoreboard.set(view_contest_scoreboard)
            contest.authors.add(self.request.profile)

            for problem in contest_problems:
                problem.contest = contest
                problem.pk = None
            ContestProblem.objects.bulk_create(contest_problems)

            revisions.set_user(self.request.user)
            revisions.set_comment(_('Cloned contest from %s') % old_key)

        requested = (self.request.POST.get('key') or '').strip()
        if requested and requested != contest.key:
            messages.info(self.request, _('Saved with ID %(new)s because %(old)s was already in use.') % {
                'new': contest.key,
                'old': requested,
            })
        return HttpResponseRedirect(reverse('contest_edit', args=(contest.key,)))


class ContestDelete(LoginRequiredMixin, View):
    """POST-only frontend delete for one assignment.

    Matches Django admin contest deletion: the user must hold
    ``judge.delete_contest``, and the contest must be one they could edit
    (``edit_all_contest``, or author/curator). Admin also disables batch
    delete (``NoBatchDeleteMixin``), so this endpoint deletes a single key.
    """

    def post(self, request, *args, **kwargs):
        contest = get_object_or_404(Contest, key=kwargs.get('contest'))
        redirect_to = request.POST.get('next') or reverse('contest_list')
        if not url_has_allowed_host_and_scheme(
            url=redirect_to,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            redirect_to = reverse('contest_list')

        if not contest.is_deletable_by(request.user):
            messages.error(request, _('You are not allowed to delete this assignment.'))
            return HttpResponseRedirect(redirect_to)

        name = contest.name
        key = contest.key
        contest.delete()
        messages.success(
            request,
            _('Deleted assignment "%(name)s" (%(key)s).') % {'name': name, 'key': key},
        )
        return HttpResponseRedirect(redirect_to)


class ContestBulkDelete(LoginRequiredMixin, View):
    """POST-only frontend bulk delete for the assignment list.

    Django admin ContestAdmin still uses ``NoBatchDeleteMixin``. This endpoint
    is the list-page counterpart of ``ProblemBulkDelete``: it accepts one or
    more keys, and re-checks ``is_deletable_by`` on every contest rather than
    trusting the form.
    """

    def post(self, request, *args, **kwargs):
        redirect_to = request.POST.get('next') or reverse('contest_list')
        if not url_has_allowed_host_and_scheme(
            url=redirect_to,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            redirect_to = reverse('contest_list')

        keys = [k for k in request.POST.getlist('keys') if k]
        if not keys:
            messages.error(request, _('No assignments selected.'))
            return HttpResponseRedirect(redirect_to)

        deleted = denied = 0
        with transaction.atomic():
            for contest in Contest.objects.filter(key__in=keys):
                if contest.is_deletable_by(request.user):
                    contest.delete()
                    deleted += 1
                else:
                    denied += 1

        if deleted:
            messages.success(
                request,
                _('Deleted %(count)d assignment(s).') % {'count': deleted},
            )
        if denied:
            messages.error(
                request,
                _('Skipped %(count)d assignment(s) you are not allowed to delete.') % {'count': denied},
            )
        return HttpResponseRedirect(redirect_to)


class ContestAccessDenied(Exception):
    pass


class ContestSEBRequired(Exception):
    pass


class ContestAccessCodeForm(forms.Form):
    access_code = forms.CharField(max_length=255)

    def __init__(self, *args, **kwargs):
        super(ContestAccessCodeForm, self).__init__(*args, **kwargs)
        self.fields['access_code'].widget.attrs.update({'autocomplete': 'off'})


class ContestJoin(LoginRequiredMixin, ContestMixin, SingleObjectMixin, View):
    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        try:
            self._check_seb(request)
        except ContestSEBRequired:
            return self._seb_required_response(request)
        return self.ask_for_access_code()

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        try:
            return self.join_contest(request)
        except ContestSEBRequired:
            return self._seb_required_response(request)
        except ContestAccessDenied:
            if request.POST.get('access_code'):
                return self.ask_for_access_code(ContestAccessCodeForm(request.POST))
            else:
                return HttpResponseRedirect(request.path)

    def _check_seb(self, request):
        contest = self.object
        if not contest.seb_required:
            return
        if contest.user_bypasses_seb(request.user):
            return
        if not request_matches_seb(request, contest.seb_allowed_bek(), contest.seb_allowed_config_keys()):
            raise ContestSEBRequired()

    def _seb_required_response(self, request):
        contest = self.object
        return render(request, 'contest/seb_required.html', {
            'contest': contest,
            'seb_launch': seb_launch_context(request, contest),
            'title': _('Safe Exam Browser required for "%s"') % contest.name,
        }, status=403)

    def join_contest(self, request, access_code=None):
        contest = self.object

        if not contest.started and not (self.is_editor or self.is_tester):
            return generic_message(request, _('Contest not ongoing'),
                                   _('"%s" is not currently ongoing.') % contest.name)

        profile = request.profile

        if not request.user.is_superuser and contest.banned_users.filter(id=profile.id).exists():
            return generic_message(request, _('Banned from joining'),
                                   _('You have been declared persona non grata for this contest. '
                                     'You are permanently barred from joining this contest.'))

        self._check_seb(request)

        requires_access_code = (not self.can_edit and contest.access_code and access_code != contest.access_code)
        if contest.ended:
            # Check if virtual participation is allowed for this contest
            if not contest.allow_virtual_participation:
                return generic_message(
                    request,
                    _('Virtual participation disabled'),
                    _('Virtual participation is disabled for this contest.'),
                )

            if requires_access_code:
                raise ContestAccessDenied()

            while True:
                virtual_id = max((ContestParticipation.objects.filter(contest=contest, user=profile)
                                  .aggregate(virtual_id=Max('virtual'))['virtual_id'] or 0) + 1, 1)
                try:
                    participation = ContestParticipation.objects.create(
                        contest=contest, user=profile, virtual=virtual_id,
                        real_start=timezone.now(),
                    )
                # There is obviously a race condition here, so we keep trying until we win the race.
                except IntegrityError:
                    pass
                else:
                    break
        else:
            SPECTATE = ContestParticipation.SPECTATE
            LIVE = ContestParticipation.LIVE

            if contest.is_live_joinable_by(request.user):
                participation_type = LIVE
            elif contest.is_spectatable_by(request.user):
                participation_type = SPECTATE
            else:
                return generic_message(request, _('Cannot enter'),
                                       _('You are not able to join this contest.'))
            try:
                participation = ContestParticipation.objects.get(
                    contest=contest, user=profile, virtual=participation_type,
                )
            except ContestParticipation.DoesNotExist:
                if requires_access_code:
                    raise ContestAccessDenied()

                participation = ContestParticipation.objects.create(
                    contest=contest, user=profile, virtual=participation_type,
                    real_start=timezone.now(),
                )
            else:
                if requires_access_code:
                    raise ContestAccessDenied()

                if participation.ended:
                    participation = ContestParticipation.objects.get_or_create(
                        contest=contest, user=profile, virtual=SPECTATE,
                        defaults={'real_start': timezone.now()},
                    )[0]

        profile.current_contest = participation
        profile.save()
        contest._updating_stats_only = True
        contest.update_user_count()
        # With a personal time limit this student's deadline only exists once they have started,
        # so their auto-submit can only be scheduled here.
        if contest.time_limit is not None:
            from judge.utils.assignment_schedule import schedule_participation_harvest
            transaction.on_commit(lambda pid=participation.pk: schedule_participation_harvest(pid))
        return HttpResponseRedirect(reverse('problem_list'))

    def ask_for_access_code(self, form=None):
        contest = self.object
        wrong_code = False
        if form:
            if form.is_valid():
                if form.cleaned_data['access_code'] == contest.access_code:
                    return self.join_contest(self.request, form.cleaned_data['access_code'])
                wrong_code = True
        else:
            form = ContestAccessCodeForm()
        return render(self.request, 'contest/access_code.html', {
            'form': form, 'wrong_code': wrong_code,
            'contest': contest,
            'title': _('Enter access code for "%s"') % contest.name,
        })


class ContestLeave(LoginRequiredMixin, ContestMixin, SingleObjectMixin, View):
    def post(self, request, *args, **kwargs):
        contest = self.get_object()

        profile = request.profile
        if profile.current_contest is None or profile.current_contest.contest_id != contest.id:
            return generic_message(request, _('No such contest'),
                                   _('You are not in contest "%s".') % contest.key, 404)

        participation = profile.current_contest
        was_spectating = participation.spectate
        try:
            participation_submit_progress(participation, log_step='leave_post')
        except TypeError:
            participation_submit_progress(participation)
        # Leave does not cancel the participation: the same row, its timer and any leftover
        # drafts stay. When harvest is on, one Leave is one harvest; a later deadline sweep
        # still picks up drafts that this call missed or that the student wrote after coming
        # back. When harvest is off, drafts stay drafts.
        try:
            from judge.utils.assignment_harvest import assignment_harvest_enabled, harvest_participation
            if assignment_harvest_enabled():
                harvest_participation(participation)
        except Exception:
            logger.exception('Auto-submit on leave failed for participation %d', participation.pk)
        profile.remove_contest()
        if contest.seb_required and not was_spectating and not contest.user_bypasses_seb(request.user):
            return HttpResponseRedirect(contest.seb_effective_quit_url or reverse('seb_quit'))
        return HttpResponseRedirect(reverse('contest_view', args=(contest.key,)))


ContestDay = namedtuple('ContestDay', 'date is_pad is_today starts ends oneday')


class ContestCalendar(TitleMixin, ContestListMixin, TemplateView):
    firstweekday = SUNDAY
    template_name = 'contest/calendar.html'

    def get(self, request, *args, **kwargs):
        try:
            self.year = int(kwargs['year'])
            self.month = int(kwargs['month'])
        except (KeyError, ValueError):
            raise ImproperlyConfigured('ContestCalendar requires integer year and month')
        self.today = timezone.now().date()
        return self.render()

    def render(self):
        context = self.get_context_data()
        return self.render_to_response(context)

    def get_contest_data(self, start, end):
        end += timedelta(days=1)
        contests = self.get_queryset().filter(Q(start_time__gte=start, start_time__lt=end) |
                                              Q(end_time__gte=start, end_time__lt=end))
        starts, ends, oneday = (defaultdict(list) for i in range(3))
        for contest in contests:
            start_date = timezone.localtime(contest.start_time).date()
            end_date = timezone.localtime(contest.end_time - timedelta(seconds=1)).date()
            if start_date == end_date:
                oneday[start_date].append(contest)
            else:
                starts[start_date].append(contest)
                ends[end_date].append(contest)
        return starts, ends, oneday

    def get_table(self):
        calendar = Calendar(self.firstweekday).monthdatescalendar(self.year, self.month)
        starts, ends, oneday = self.get_contest_data(make_aware(datetime.combine(calendar[0][0], time.min)),
                                                     make_aware(datetime.combine(calendar[-1][-1], time.min)))
        return [[ContestDay(
            date=date, is_pad=date.month != self.month,
            is_today=date == self.today, starts=starts[date], ends=ends[date], oneday=oneday[date],
        ) for date in week] for week in calendar]

    def get_context_data(self, **kwargs):
        context = super(ContestCalendar, self).get_context_data(**kwargs)

        try:
            month = date(self.year, self.month, 1)
        except ValueError:
            raise Http404()
        else:
            context['title'] = _('Contests in %(month)s') % {'month': date_filter(month, _('F Y'))}

        dates = Contest.objects.aggregate(min=Min('start_time'), max=Max('end_time'))
        min_month = (self.today.year, self.today.month)
        if dates['min'] is not None:
            min_month = dates['min'].year, dates['min'].month
        max_month = (self.today.year, self.today.month)
        if dates['max'] is not None:
            max_month = max((dates['max'].year, dates['max'].month), (self.today.year, self.today.month))

        month = (self.year, self.month)
        if month < min_month or month > max_month:
            # 404 is valid because it merely declares the lack of existence, without any reason
            raise Http404()

        context['now'] = timezone.now()
        context['calendar'] = self.get_table()
        context['curr_month'] = date(self.year, self.month, 1)

        if month > min_month:
            context['prev_month'] = date(self.year - (self.month == 1), 12 if self.month == 1 else self.month - 1, 1)
        else:
            context['prev_month'] = None

        if month < max_month:
            context['next_month'] = date(self.year + (self.month == 12), 1 if self.month == 12 else self.month + 1, 1)
        else:
            context['next_month'] = None
        return context


class ContestICal(TitleMixin, ContestListMixin, BaseListView):
    def generate_ical(self):
        cal = ICalendar()
        cal.add('prodid', '-//DMOJ//NONSGML Contests Calendar//')
        cal.add('version', '2.0')

        now = timezone.now().astimezone(timezone.utc)
        domain = self.request.get_host()
        for contest in self.get_queryset():
            event = Event()
            event.add('uid', f'contest-{contest.key}@{domain}')
            event.add('summary', contest.name)
            event.add('location', self.request.build_absolute_uri(contest.get_absolute_url()))
            event.add('dtstart', contest.start_time.astimezone(timezone.utc))
            event.add('dtend', contest.end_time.astimezone(timezone.utc))
            event.add('dtstamp', now)
            cal.add_component(event)
        return cal.to_ical()

    def render_to_response(self, context, **kwargs):
        return HttpResponse(self.generate_ical(), content_type='text/calendar')


class ContestStats(TitleMixin, ContestMixin, DetailView):
    template_name = 'contest/stats.html'

    def get_title(self):
        return _('%s Statistics') % self.object.name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if not (self.object.ended or self.can_edit):
            raise Http404()

        queryset = Submission.objects.filter(contest_object=self.object)

        ac_count = Count(Case(When(result='AC', then=Value(1)), output_field=IntegerField()))
        ac_rate = CombinedExpression(ac_count / Count('problem'), '*', Value(100.0), output_field=FloatField())

        status_count_queryset = list(
            queryset.values('problem__code', 'result').annotate(count=Count('result'))
                    .values_list('problem__code', 'result', 'count'),
        )
        labels, codes = [], []
        contest_problems = list(
            self.object.contest_problems.order_by('order').select_related('problem'),
        )
        lang = self.request.LANGUAGE_CODE
        order_to_name = {
            cp.order: contest_problem_display_name(cp, lang) for cp in contest_problems
        }
        if contest_problems:
            labels = [order_to_name[cp.order] for cp in contest_problems]
            codes = [cp.problem.code for cp in contest_problems]
        num_problems = len(labels)
        status_counts = [[] for i in range(num_problems)]
        for problem_code, result, count in status_count_queryset:
            if problem_code in codes:
                status_counts[codes.index(problem_code)].append((result, count))

        result_data = defaultdict(partial(list, [0] * num_problems))
        for i in range(num_problems):
            for category in _get_result_data(defaultdict(int, status_counts[i]))['categories']:
                result_data[category['code']][i] = category['count']

        stats = {
            'problem_status_count': {
                'labels': labels,
                'datasets': [
                    {
                        'label': name,
                        'backgroundColor': settings.DMOJ_STATS_SUBMISSION_RESULT_COLORS[name],
                        'data': data,
                    }
                    for name, data in result_data.items()
                ],
            },
            'problem_ac_rate': get_bar_chart(
                [
                    (order_to_name.get(order, ''), rate)
                    for order, rate in queryset.values('contest__problem__order')
                    .annotate(ac_rate=ac_rate)
                    .order_by('contest__problem__order')
                    .values_list('contest__problem__order', 'ac_rate')
                ],
            ),
            'language_count': get_pie_chart(
                queryset.values('language__name').annotate(count=Count('language__name'))
                        .filter(count__gt=0).order_by('-count').values_list('language__name', 'count'),
            ),
            'language_ac_rate': get_bar_chart(
                queryset.values('language__name').annotate(ac_rate=ac_rate)
                        .filter(ac_rate__gt=0).values_list('language__name', 'ac_rate'),
            ),
        }

        context['stats'] = mark_safe(json.dumps(stats).replace('</', '<\\/'))

        return context


ContestRankingProfile = namedtuple(
    'ContestRankingProfile',
    'id user css_class username points cumtime tiebreaker organization participation '
    'participation_rating problem_cells result_cell display_name',
)

BestSolutionData = namedtuple('BestSolutionData', 'code points time state is_pretested')


class ContestGrade(TitleMixin, ContestMixin, DetailView):
    """Teacher-side grading workspace for a contest — mirrors the grading.html reference."""
    template_name = 'contest/grade.html'

    def get_title(self):
        return _('%s Grade') % self.object.name

    def dispatch(self, request, *args, **kwargs):
        # Grading is an *edit* action, not a view action, so we gate on
        # `is_editable_by` and NOT on `ContestMixin.get_object()`'s
        # `access_check`. Using `get_object()` here would (a) 404 a hidden
        # contest for non-viewers *before* the edit check, and (b) raise an
        # uncaught `PrivateContestError` (→500) for a private contest. Fetching
        # the contest directly gives every authenticated non-editor a uniform,
        # existence-hiding 404.
        if not request.user.is_authenticated:
            return redirect_to_login(self.request.get_full_path())
        self.object = get_object_or_404(Contest, key=kwargs['contest'])
        if not self.object.is_editable_by(request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        contest = self.object
        from judge.utils.contest_grade import (
            build_grade_payload, contest_grade_queryset, final_submission_ids_for_contest,
        )
        submissions = contest_grade_queryset(contest)
        context['grade_submissions'] = submissions
        # Problem library for the grade page: code → display name + statement,
        # so the detail pane can render the problem description ("题面").
        # ``description_html`` is rendered with the site's markdown pipeline
        # (escaped + sanitized server-side) so the grade page can inject it
        # directly instead of escaping raw markdown source.
        from judge.jinja2.markdown import markdown as render_markdown
        math_engine = getattr(getattr(self.request, 'profile', None), 'math_engine', None)
        if math_engine == 'auto':
            math_engine = getattr(settings, 'MATHOID_DEFAULT_TYPE', 'jax')
        problems = list(contest.contest_problems.order_by('order').select_related('problem'))
        context['grade_problems'] = [
            {
                'code': cp.problem.code,
                'name': contest_problem_display_name(cp, self.request.LANGUAGE_CODE),
                'letter': chr(65 + idx) if len(problems) <= 26 else '',
                'points': cp.points,
                'description': cp.problem.description or '',
                'description_html': str(render_markdown(
                    cp.problem.description or '', cp.problem.markdown_style, math_engine)),
            }
            for idx, cp in enumerate(problems)
        ]
        # Release state for the "Release All" toolbar button: how many manual
        # grades exist and how many are still unreleased (students keep seeing
        # their machine score until the teacher releases the override).
        manual_grades = SubmissionManualGrade.objects.filter(submission__contest_object=contest)
        context['grade_released_count'] = manual_grades.filter(released=True).count()
        context['grade_unreleased_count'] = manual_grades.filter(released=False).count()
        # Teachers can always run AI grading when the contest has AI-enabled problems.
        from judge.utils.contest_ai import problem_has_ai_grading
        context['grade_ai_batch'] = any(
            problem_has_ai_grading(cp.problem)
            for cp in contest.contest_problems.select_related(
                'problem', 'problem__code_config', 'problem__fill_blank_config',
            )
        )
        context['grade_ai_batch_done'] = bool(contest.ai_grading_batch_finished_at)
        context['grade_save_url'] = reverse('contest_grade_save', args=[contest.key])
        context['grade_release_url'] = reverse('contest_grade_release', args=[contest.key])
        context['grade_status_url'] = reverse('contest_grade_status', args=[contest.key])
        context['grade_ai_url'] = reverse('contest_grade_ai', args=[contest.key])
        context['grade_home_url'] = reverse('contest_view', args=[contest.key])
        context['grade_home_label'] = contest.name
        context['grade_show_participation'] = True

        payload = build_grade_payload(submissions, final_submission_ids_for_contest(contest))
        # Pass Python objects; the template serializes with |tojson (htmlsafe JSON,
        # escapes `<`/`>`/`&`/`'`) so student-controlled source can never break out
        # of the <script> block via `</script>`.
        context['grade_submissions_json'] = payload
        context['grade_problems_json'] = context['grade_problems']
        return context


class ContestGradeSave(ContestMixin, DetailView):
    """Save a manual score for a submission (teacher-side)."""

    def dispatch(self, request, *args, **kwargs):
        # Same reasoning as `ContestGrade.dispatch`: grading is an edit action,
        # so we check `is_editable_by` directly instead of `access_check` (which
        # would give non-viewers a 404 HTML page rather than the 403 JSON the
        # frontend expects, and 500 on private contests). Status contract for the
        # JSON endpoint: 401 anonymous, 403 authenticated non-editor, 200 editor.
        if not request.user.is_authenticated:
            return JsonResponse({'error': _('Not authenticated.')}, status=401)
        self.object = get_object_or_404(Contest, key=kwargs['contest'])
        if not self.object.is_editable_by(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        raise Http404()

    def post(self, request, *args, **kwargs):
        try:
            submission_id = int(request.POST.get('submission'))
            score = float(request.POST.get('score') or 0)
        except (TypeError, ValueError):
            return JsonResponse({'error': _('Invalid input.')}, status=400)
        try:
            submission = Submission.objects.get(
                id=submission_id, contest_object=self.object,
            )
        except Submission.DoesNotExist:
            return JsonResponse({'error': _('Submission not found.')}, status=404)

        from judge.utils.contest_grade import apply_manual_grade
        note = request.POST.get('note')
        grade, score, total = apply_manual_grade(
            self.request.profile, submission, score, note=note)

        return JsonResponse({
            'ok': True,
            'submission': submission_id,
            'score': score,
            'total': total,
            'note': submission.review_note,
            'released': grade.released,
            'reviewed_by': submission.reviewed_by.user.username if submission.reviewed_by else None,
            'reviewed_at': (timezone.localtime(submission.reviewed_at)
                            .strftime('%Y-%m-%d %H:%M') if submission.reviewed_at else None),
        })


class ContestGradeStatus(ContestMixin, DetailView):
    """JSON status for one grade-page submission (teacher-side).

    The grade page polls this endpoint after queueing a rejudge so the
    teacher sees the fresh verdict / score without a manual page reload.
    Read-only, same authorization contract as ``ContestGradeSave`` (401
    anonymous, 403 authenticated non-editor, 404 unknown submission).
    """

    http_method_names = ['get']

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': _('Not authenticated.')}, status=401)
        self.object = get_object_or_404(Contest, key=kwargs['contest'])
        if not self.object.is_editable_by(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        try:
            submission_id = int(request.GET.get('submission'))
        except (TypeError, ValueError):
            return JsonResponse({'error': _('Invalid input.')}, status=400)
        try:
            submission = Submission.objects.filter(contest_object=self.object, id=submission_id) \
                .select_related('language', 'reviewed_by__user') \
                .prefetch_related('test_cases').get()
        except Submission.DoesNotExist:
            return JsonResponse({'error': _('Submission not found.')}, status=404)
        contest_submission = submission.contest_or_none
        from judge.utils.contest_grade import grade_points_total
        total = grade_points_total(submission)
        # Mirror the grade payload's point scale (contest-scaled when part of
        # the assignment) so a polled rejudge result agrees with the row shown.
        # A manual override always wins (actual score), including for standalone
        # submissions where it is not folded into a ContestSubmission row.
        manual_grade = submission.manual_grade_override
        if manual_grade is not None and manual_grade.points is not None:
            points = manual_grade.points
        elif contest_submission is not None:
            points = contest_submission.points
        else:
            points = submission.points
        return JsonResponse({
            'ok': True,
            'id': submission.id,
            'status': submission.status,
            'result': submission.result,
            'points': points,
            'total': total,
            'time': submission.time,
            'memory': submission.memory,
            'is_graded': submission.is_graded,
            'was_rejudged': submission.rejudged_date is not None,
            'rejudged_date': submission.rejudged_date.isoformat() if submission.rejudged_date else None,
            'judged_date': submission.judged_date.isoformat() if submission.judged_date else None,
            'review_note': submission.review_note or '',
            'reviewed_by': submission.reviewed_by.user.username if submission.reviewed_by else None,
            'released': bool(manual_grade and manual_grade.released),
            'test_cases': [
                {
                    'case': tc.case,
                    'status': tc.status,
                    'time': tc.time,
                    'memory': tc.memory,
                    'points': tc.points,
                    'total': tc.total,
                    'batch': tc.batch,
                    'feedback': tc.feedback or '',
                    'extended_feedback': tc.extended_feedback or '',
                } for tc in submission.test_cases.all()
            ],
        })


class ContestGradeRelease(ContestMixin, DetailView):
    """Release all manual grades for a contest (teacher-side).

    The grade page's "Release All" button: it flips ``released=True`` on every
    manual grade (``SubmissionManualGrade``) belonging to this contest. Until a
    submission's override is released, the student keeps seeing their machine
    score; after it, the student-visible score switches to the manual grade.
    (This is per-submission and independent of ``Contest.results_finalized``,
    which remains the exam-format hidden-results flag.)
    The status contract matches ``ContestGradeSave``: 401 anonymous, 403
    authenticated non-editor, 200 editor.
    """

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': _('Not authenticated.')}, status=401)
        self.object = get_object_or_404(Contest, key=kwargs['contest'])
        if not self.object.is_editable_by(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        raise Http404()

    def post(self, request, *args, **kwargs):
        updated = SubmissionManualGrade.objects.filter(
            submission__contest_object=self.object,
        ).update(released=True)
        from judge.utils.contest_grade import sync_released_manual_grades
        sync_released_manual_grades(self.object)
        return JsonResponse({'ok': True, 'released': True, 'count': updated})


class ContestBatchAIGrade(View):
    """Start (or re-run) batch AI grading of latest submissions."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': _('Not authenticated.')}, status=401)
        self.object = get_object_or_404(Contest, key=kwargs['contest'])
        if not self.object.is_editable_by(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        contest = self.object
        started = contest.ai_grading_batch_started_at
        finished = contest.ai_grading_batch_finished_at
        return JsonResponse({
            'ok': True,
            'mode': contest.ai_grading_mode,
            'started_at': started.isoformat() if started else None,
            'finished_at': finished.isoformat() if finished else None,
        })

    def post(self, request, *args, **kwargs):
        contest = self.object
        from judge.tasks.contest import batch_ai_grade
        transaction.on_commit(lambda key=contest.key: batch_ai_grade.delay(key, auto=False))
        return JsonResponse({'ok': True, 'queued': True})


def make_contest_ranking_profile(contest, participation, contest_problems, scoring_map=None):
    def display_user_problem(contest_problem):
        # When the contest format is changed, `format_data` might be invalid.
        # This will cause `display_user_problem` to error, so we display '???' instead.
        cell_url = None
        if scoring_map is not None:
            sub = scoring_map.get((participation.id, contest_problem.id))
            if sub is not None:
                cell_url = '%s?submission=%d' % (
                    reverse('contest_grade', args=[contest.key]), sub.id)
        try:
            return contest.format.display_user_problem(
                participation, contest_problem, cell_url=cell_url)
        except (KeyError, TypeError, ValueError):
            return mark_safe('<td>???</td>')

    user = participation.user
    return ContestRankingProfile(
        id=user.id,
        user=user.user,
        css_class=user.css_class,
        username=user.username,
        points=participation.score,
        cumtime=participation.cumtime,
        tiebreaker=participation.tiebreaker,
        organization=user.organization,
        participation_rating=participation.rating.rating if hasattr(participation, 'rating') else None,
        problem_cells=[display_user_problem(contest_problem) for contest_problem in contest_problems],
        result_cell=contest.format.display_participation_result(participation),
        participation=participation,
        display_name=user.display_name,
    )


def base_contest_ranking_list(contest, problems, queryset, scoring_map_factory=None):
    # Materialize first so the Grade deep-link map can be scoped to exactly the
    # participations this page renders (see `scoring_submissions_for_contest`);
    # a per-user page must not scan every submission in the contest.
    participations = list(
        queryset.select_related('user__user', 'rating').defer('user__about', 'user__organizations__about'))
    scoring_map = scoring_map_factory([p.id for p in participations]) if scoring_map_factory is not None else None
    return [make_contest_ranking_profile(contest, participation, problems, scoring_map=scoring_map)
            for participation in participations]


def contest_ranking_list(contest, problems, scoring_map_factory=None):
    return base_contest_ranking_list(contest, problems, contest.users.filter(virtual=0)
                                     .prefetch_related('user__organizations')
                                     .annotate(submission_cnt=Count('submission'))
                                     .order_by('is_disqualified', '-score', 'cumtime', 'tiebreaker', '-submission_cnt'),
                                     scoring_map_factory=scoring_map_factory)


def get_contest_ranking_list(request, contest, participation=None, ranking_list=contest_ranking_list,
                             show_current_virtual=True, ranker=ranker):
    problems = list(contest.contest_problems.select_related('problem').defer('problem__description').order_by('order'))

    scoring_map_factory = None
    if (request is not None and getattr(request, 'user', None) is not None
            and request.user.is_authenticated and contest.is_editable_by(request.user)):
        from judge.utils.contest_grade import scoring_submissions_for_contest

        def make_scoring_map(participation_ids):
            return scoring_submissions_for_contest(contest, participation_ids=participation_ids)

        scoring_map_factory = make_scoring_map

    users = ranker(ranking_list(contest, problems, scoring_map_factory=scoring_map_factory),
                   key=attrgetter('points', 'cumtime', 'tiebreaker'))

    if show_current_virtual:
        if participation is None and request.user.is_authenticated:
            participation = request.profile.current_contest
            if participation is None or participation.contest_id != contest.id:
                participation = None
        if participation is not None and participation.virtual:
            virtual_map = scoring_map_factory([participation.id]) if scoring_map_factory is not None else None
            users = chain([('-', make_contest_ranking_profile(
                contest, participation, problems, scoring_map=virtual_map))], users)
    return users, problems


def contest_ranking_ajax(request, contest, participation=None):
    contest, exists = _find_contest(request, contest)
    if not exists:
        return HttpResponseBadRequest('Invalid contest', content_type='text/plain')

    if not contest.can_see_full_scoreboard(request.user):
        raise Http404()

    users, problems = get_contest_ranking_list(request, contest, participation)
    return render(request, 'contest/ranking-table.html', {
        'users': users,
        'problems': problems,
        'contest': contest,
        'has_rating': contest.ratings.exists(),
    })


class ContestRankingBase(ContestMixin, TitleMixin, DetailView):
    template_name = 'contest/ranking.html'
    tab = None

    def get_title(self):
        raise NotImplementedError()

    def get_content_title(self):
        return self.object.name

    def get_ranking_list(self):
        raise NotImplementedError()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if not self.object.can_see_own_scoreboard(self.request.user):
            raise Http404()

        users, problems = self.get_ranking_list()
        context['users'] = users
        context['problems'] = problems
        context['last_msg'] = event.last()
        context['tab'] = self.tab
        return context


class ContestRanking(ContestRankingBase):
    tab = 'ranking'

    def get_title(self):
        return _('%s Rankings') % self.object.name

    def get_ranking_list(self):
        if not self.object.can_see_full_scoreboard(self.request.user):
            queryset = self.object.users.filter(user=self.request.profile, virtual=ContestParticipation.LIVE)
            return get_contest_ranking_list(
                self.request, self.object,
                ranking_list=partial(base_contest_ranking_list, queryset=queryset),
                ranker=lambda users, key: ((_('???'), user) for user in users),
            )

        return get_contest_ranking_list(self.request, self.object)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['has_rating'] = self.object.ratings.exists()
        return context


class ContestParticipationList(LoginRequiredMixin, ContestRankingBase):
    tab = 'participation'

    def get_title(self):
        if self.profile == self.request.profile:
            return _('Your participation in %(contest)s') % {'contest': self.object.name}
        return _("%(user)s's participation in %(contest)s") % {
            'user': self.profile.username, 'contest': self.object.name,
        }

    def get_ranking_list(self):
        if not self.object.can_see_full_scoreboard(self.request.user) and self.profile != self.request.profile:
            raise Http404()

        queryset = self.object.users.filter(user=self.profile, virtual__gte=0).order_by('-virtual')
        live_link = format_html('<a href="{2}#!{1}">{0}</a>', _('Live'), self.profile.username,
                                reverse('contest_ranking', args=[self.object.key]))

        return get_contest_ranking_list(
            self.request, self.object, show_current_virtual=False,
            ranking_list=partial(base_contest_ranking_list, queryset=queryset),
            ranker=lambda users, key: ((user.participation.virtual or live_link, user) for user in users))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['has_rating'] = False
        context['now'] = timezone.now()
        context['rank_header'] = _('Participation')
        return context

    def get(self, request, *args, **kwargs):
        if 'user' in kwargs:
            self.profile = get_object_or_404(Profile, user__username=kwargs['user'])
        else:
            self.profile = self.request.profile
        return super().get(request, *args, **kwargs)


class ContestParticipationDisqualify(ContestMixin, SingleObjectMixin, View):
    def get_object(self, queryset=None):
        contest = super().get_object(queryset)
        if not contest.is_editable_by(self.request.user):
            raise Http404()
        return contest

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()

        try:
            participation = self.object.users.get(pk=request.POST.get('participation'))
        except ObjectDoesNotExist:
            pass
        else:
            participation.set_disqualified(not participation.is_disqualified)
        return HttpResponseRedirect(reverse('contest_ranking', args=(self.object.key,)))


class ContestMossMixin(ContestMixin, PermissionRequiredMixin):
    permission_required = 'judge.moss_contest'

    def get_object(self, queryset=None):
        contest = super().get_object(queryset)
        if settings.MOSS_API_KEY is None or not contest.is_editable_by(self.request.user):
            raise Http404()
        return contest


class ContestMossView(ContestMossMixin, TitleMixin, DetailView):
    template_name = 'contest/moss.html'

    def get_title(self):
        return _('%s MOSS Results') % self.object.name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        moss_problems = []
        lang = self.request.LANGUAGE_CODE
        for cp in self.object.contest_problems.order_by('order').select_related('problem'):
            problem = cp.problem
            problem.effective_name = contest_problem_display_name(cp, lang)
            moss_problems.append(problem)
        languages = list(map(itemgetter(0), ContestMoss.LANG_MAPPING))

        results = ContestMoss.objects.filter(contest=self.object)
        moss_results = defaultdict(list)
        for result in results:
            moss_results[result.problem].append(result)

        for result_list in moss_results.values():
            result_list.sort(key=lambda x: languages.index(x.language))

        context['languages'] = languages
        context['has_results'] = results.exists()
        context['moss_results'] = [(problem, moss_results[problem]) for problem in moss_problems]

        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        status = run_moss.delay(self.object.key)
        return redirect_to_task_status(
            status, message=_('Running MOSS for %s...') % (self.object.name,),
            redirect=reverse('contest_moss', args=(self.object.key,)),
        )


class ContestMossDelete(ContestMossMixin, SingleObjectMixin, View):
    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        ContestMoss.objects.filter(contest=self.object).delete()
        return HttpResponseRedirect(reverse('contest_moss', args=(self.object.key,)))


class ContestTagDetailAjax(DetailView):
    model = ContestTag
    slug_field = slug_url_kwarg = 'name'
    context_object_name = 'tag'
    template_name = 'contest/tag-ajax.html'


class ContestTagDetail(TitleMixin, ContestTagDetailAjax):
    template_name = 'contest/tag.html'

    def get_title(self):
        return _('Contest tag: %s') % self.object.name


class ContestExportPDF(ContestMixin, View):

    def post(self, request, *args, **kwargs):
        try:
            contest_key = kwargs.get('contest')

            contest, exists = _find_contest(request, contest_key, private_check=True)
            if not exists:
                return contest

            contest_problems = contest.contest_problems.order_by('order').select_related('problem')

            totalscore = 0
            for page_num, cp in enumerate(contest_problems, 1):
                problem = cp.problem

                totalscore += problem.points

            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <!-- KaTeX CSS -->
                <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
                <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
                <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"
                    onload="renderMathInElement(document.body);"></script>
                <script>
                    document.addEventListener("DOMContentLoaded", function() {{
                        renderMathInElement(document.body, {{
                            delimiters: [
                                {{left: "$$", right: "$$", display: true}},
                                {{left: "$", right: "$", display: false}},
                                {{left: "~~", right: "~~", display: true}},
                                {{left: "~", right: "~", display: false}},
                            ],
                            throwOnError: false
                        }});
                    }});
                </script>
                <meta charset="UTF-8">
                <title>{contest.name}</title>
                <style>
                    @media print {{
                        @page {{
                            margin: 20mm 25mm;
                            size: A4;
                        }}
                        @page :first {{
                            margin-top: 15mm;
                        }}
                        body {{
                            margin: 0;
                            padding: 0;
                            color: #000 !important;
                            background: #fff !important;
                            font-size: 11pt;
                            line-height: 1.4;
                        }}
                        .page {{
                            break-inside: avoid;
                            page-break-after: always;
                        }}
                        .last-page {{
                            page-break-after: auto;
                        }}
                        .no-print {{
                            display: none !important;
                        }}
                        .cover-controls {{
                            display: none !important;
                        }}
                        pre {{
                            page-break-inside: avoid !important;
                            break-inside: avoid !important;
                        }}
                        table {{
                            page-break-inside: avoid !important;
                            break-inside: avoid !important;
                        }}
                        ul, ol {{
                            page-break-inside: avoid !important;
                        }}
                    }}
                    body {{
                        font-family: 'Times New Roman', 'SimSun', serif;
                        line-height: 1.4;
                        color: #000;
                        margin: 0;
                        padding: 0;
                        font-size: 11pt;
                        background: #fff;
                        -webkit-print-color-adjust: exact;
                        print-color-adjust: exact;
                    }}
                    .problem-description {{
                        font-size: 11pt;
                        line-height: 1.5;
                        margin-bottom: 5mm;
                        font-family: inherit;
                    }}
                    .problem-description h1,
                    .problem-description h2,
                    .problem-description h3,
                    .problem-description h4,
                    .problem-description h5,
                    .problem-description h6 {{
                        margin-top: 15px;
                        margin-bottom: 10px;
                        font-weight: bold;
                        line-height: 1.2;
                    }}
                    .problem-description h1 {{
                        font-size: 1.8em;
                        border-bottom: 2px solid #ccc;
                        padding-bottom: 5px;
                    }}
                    .problem-description h2 {{
                        font-size: 1.5em;
                        border-bottom: 1px solid #eee;
                        padding-bottom: 3px;
                    }}
                    .problem-description h3 {{
                        font-size: 1.3em;
                    }}
                    .problem-description h4 {{
                        font-size: 1.1em;
                    }}
                    .problem-description p {{
                        margin: 10px 0;
                        line-height: 1.5;
                    }}
                    .problem-description ul,
                    .problem-description ol {{
                        margin: 10px 0;
                        padding-left: 30px;
                    }}
                    .problem-description li {{
                        margin: 5px 0;
                    }}
                    .problem-description code {{
                        font-family: 'Courier New', 'Consolas', monospace;
                        background-color: #f5f5f5;
                        padding: 2px 4px;
                        border-radius: 3px;
                        font-size: 0.9em;
                    }}
                    .problem-description pre {{
                        font-family: 'Courier New', 'Consolas', monospace;
                        background-color: #f8f8f8;
                        border: 1px solid #ddd;
                        border-radius: 4px;
                        padding: 10px;
                        overflow: auto;
                        margin: 15px 0;
                        font-size: 0.9em;
                        line-height: 1.3;
                    }}
                    .problem-description pre code {{
                        background-color: transparent;
                        padding: 0;
                        border-radius: 0;
                    }}
                    .problem-description table {{
                        border-collapse: collapse;
                        border-spacing: 0;
                        width: 100%;
                        margin: 15px 0;
                        font-size: 0.95em;
                    }}
                    .problem-description table th,
                    .problem-description table td {{
                        border: 1px solid #ddd;
                        padding: 8px 12px;
                        text-align: left;
                    }}
                    .problem-description table th {{
                        background-color: #f5f5f5;
                        font-weight: bold;
                    }}
                    .problem-description blockquote {{
                        border-left: 4px solid #ddd;
                        padding-left: 15px;
                        margin: 15px 0;
                        color: #666;
                    }}
                    .problem-description a {{
                        color: #0066cc;
                        text-decoration: underline;
                    }}
                    .problem-description hr {{
                        border: 0;
                        border-top: 1px solid #ddd;
                        margin: 20px 0;
                    }}
                    .problem-description strong {{
                        font-weight: bold;
                    }}
                    .problem-description em {{
                        font-style: italic;
                    }}
                    .cover-page {{
                        height: 277mm;
                        display: flex;
                        flex-direction: column;
                        justify-content: center;
                        align-items: center;
                        text-align: center;
                        padding: 20mm;
                        box-sizing: border-box;
                        border-bottom: 2px solid #000;
                    }}
                    .contest-title {{
                        font-size: 24pt;
                        font-weight: bold;
                        margin-bottom: 15mm;
                        text-decoration: underline;
                        text-underline-offset: 5px;
                    }}
                    .contest-info {{
                        font-size: 14pt;
                        margin-bottom: 20mm;
                        line-height: 2;
                    }}
                    .instructions {{
                        font-size: 10pt;
                        margin-top: 25mm;
                        padding: 5mm;
                        border: 1px solid #000;
                        width: 80%;
                        text-align: left;
                    }}
                    .instructions h3 {{
                        margin-top: 0;
                        text-align: center;
                    }}
                    .instructions ul {{
                        padding-left: 20px;
                        margin: 10px 0;
                    }}
                    .page {{
                        min-height: 257mm;
                        padding: 15mm 25mm;
                        box-sizing: border-box;
                        position: relative;
                        border-bottom: 1px solid #eee;
                    }}
                    .page-header {{
                        border-bottom: 2px solid #000;
                        padding-bottom: 5mm;
                        margin-bottom: 8mm;
                        display: flex;
                        justify-content: space-between;
                        align-items: flex-end;
                    }}
                    .contest-name {{
                        font-size: 12pt;
                        font-weight: bold;
                    }}
                    .problem-info {{
                        text-align: right;
                        font-size: 10pt;
                    }}
                    .problem-points {{
                        font-size: 14pt;
                        font-weight: bold;
                        color: #d00;
                    }}
                    .problem-content {{
                        margin-bottom: 10mm;
                    }}
                    .problem-title {{
                        font-size: 16pt;
                        font-weight: bold;
                        margin-bottom: 5mm;
                        padding-bottom: 3mm;
                        border-bottom: 1px solid #666;
                    }}
                    .problem-meta {{
                        font-size: 10pt;
                        color: #666;
                        margin-bottom: 8mm;
                        display: flex;
                        gap: 15mm;
                    }}
                    .problem-description {{
                        font-size: 11pt;
                        line-height: 1.5;
                        margin-bottom: 5mm;
                        white-space: pre-wrap;
                        font-family: inherit;
                    }}
                    .answer-area {{
                        margin-top: 15mm;
                        padding-top: 8mm;
                        border-top: 1px dashed #999;
                    }}
                    .answer-header {{
                        font-size: 12pt;
                        font-weight: bold;
                        margin-bottom: 5mm;
                        color: #006;
                    }}
                    .answer-box {{
                        min-height: 120mm;
                        border: 2px solid #ccc;
                        padding: 5mm;
                        margin-bottom: 3mm;
                        background: linear-gradient(to bottom, transparent 95%, #f0f0f0 95%);
                        background-size: 100% 25px;
                        line-height: 25px;
                    }}
                    .page-footer {{
                        position: absolute;
                        bottom: 15mm;
                        left: 25mm;
                        right: 25mm;
                        font-size: 9pt;
                        color: #666;
                        display: flex;
                        justify-content: space-between;
                        padding-top: 3mm;
                        border-top: 1px solid #ccc;
                    }}
                    .page-number {{
                        text-align: center;
                        flex-grow: 1;
                    }}
                    .student-info {{
                        position: absolute;
                        bottom: 5mm;
                        left: 25mm;
                        right: 25mm;
                        font-size: 9pt;
                        display: flex;
                        justify-content: space-between;
                    }}
                    .info-field {{
                        width: 40mm;
                        border-bottom: 1px solid #999;
                        text-align: center;
                        padding-bottom: 2px;
                    }}
                    .cover-controls {{
                        position: fixed;
                        top: 20px;
                        right: 20px;
                        z-index: 1000;
                        background: white;
                        padding: 15px;
                        border: 2px solid #007bff;
                        border-radius: 5px;
                        box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                    }}
                    .cover-controls h4 {{
                        margin-top: 0;
                        margin-bottom: 10px;
                        color: #007bff;
                    }}
                    .print-btn {{
                        background-color: #007bff;
                        color: white;
                        border: none;
                        padding: 8px 15px;
                        border-radius: 4px;
                        cursor: pointer;
                        font-size: 12px;
                        display: block;
                        width: 100%;
                        margin-bottom: 5px;
                    }}
                    .print-btn:hover {{
                        background-color: #0056b3;
                    }}
                    .text-center {{ text-align: center; }}
                    .text-right {{ text-align: right; }}
                    .bold {{ font-weight: bold; }}
                    .italic {{ font-style: italic; }}
                    .problem-description .math,
                    .problem-description .katex {{
                        font-size: 1.1em;
                        text-align: center;
                        margin: 10px 0;
                    }}
                    .problem-description .katex-display {{
                        overflow: auto hidden;
                        padding: 5px 0;
                    }}
                    .problem-description img {{
                        max-width: 100%;
                        height: auto;
                        display: block;
                        margin: 15px auto;
                    }}
                    .problem-description .task-list-item {{
                        list-style-type: none;
                        margin-left: -20px;
                    }}
                    .problem-description .task-list-item-checkbox {{
                        margin-right: 8px;
                    }}
                </style>
            </head>
            <body>
                <!-- 封面页 -->
                <div class="page cover-page">
                    <div class="contest-info">
                        <div>
                            <span class="bold">Start Time: </span>
                            {contest.start_time}
                        </div>
                        <div>
                            <span class="bold">End Time: </span>
                            {contest.end_time}
                        </div>
                        <div>
                            <span class="bold">Total Problems: </span>
                            {contest_problems.count()}
                        </div>
                        <div style="margin-top: 10mm;">
                            <span class="bold">Student Name: </span>
                            ____________________
                        </div>
                        <div>
                            <span class="bold">Student NetID: </span>
                            ____________________
                        </div>
                    </div>
                    <div class="instructions">
                        <ul>
                            <li>Total Score: {totalscore}</li>
                            <li>Pages: {contest_problems.count() + 1}</li>
                            {contest.description}
                        </ul>
                        <div class="text-right italic">—— {timezone.now().strftime('%m/%d/%Y')} ——</div>
                    </div>
                </div>
            """

            for page_num, cp in enumerate(contest_problems, 1):
                problem = cp.problem
                points = problem.points

                if problem.description:
                    description = problem.description

                description_html = mark_safe(markdown.markdown(
                    re.sub(r'~([^~]+?)~', r'$\1$', description),
                    extensions=[
                        'markdown.extensions.extra',
                        'markdown.extensions.codehilite',
                        'markdown.extensions.tables',
                        'markdown.extensions.toc',
                        'markdown.extensions.nl2br',
                        'markdown.extensions.sane_lists',
                        KatexExtension(),
                    ],
                    output_format='html5',
                ))

                html_content += f"""
                <!-- Problem {page_num} -->
                <div class="page {'last-page' if page_num == contest_problems.count() else ''}">
                    <!-- 页眉 -->
                    <div class="page-header">
                        <div class="problem-info">
                            Problem {page_num} / {contest_problems.count()} <br>
                            <span class="problem-points">{points} - points</span>
                        </div>
                    </div>
                    <!-- 题目内容 -->
                    <div class="problem-content">
                        <div class="problem-title">
                            Problem {page_num}: {contest_problem_display_name(cp)}
                        </div>
                        <div class="problem-description">
                            {description_html}
                        </div>
                    </div>
                    <!-- 页脚 -->
                    <div class="page-footer">
                        <div class="text-left">Problem {page_num}</div>
                        <div class="text-right">Points: {points}</div>
                    </div>
                </div>
                """

                html_content += f"""
                <!-- Problem {page_num} -->
                <div class="page {'last-page' if page_num == contest_problems.count() else ''}">
                    <!-- 页眉 -->
                    <div class="page-header">
                        <div class="problem-info">
                            Problem {page_num} / {contest_problems.count()} <br>
                            <span class="problem-points">{points} - points</span>
                        </div>
                    </div>
                    <!-- 答题区域 -->
                    <div class="answer-area">
                        <div class="answer-header">
                            Answer Area (write down your answer in this area)
                        </div>
                        <div class="answer-box">
                            <!-- 答题格子线 - 通过CSS背景实现 -->
                        </div>
                    </div>
                    <!-- 页脚 -->
                    <div class="page-footer">
                        <div class="text-left">Problem {page_num}</div>
                        <div class="text-right">Points: {points}</div>
                    </div>
                    <!-- 学生信息填写区 -->
                    <div class="student-info">
                        <div class="info-field">Student Name: ________________</div>
                        <div class="info-field">Student NetID: ________________</div>
                        <div class="info-field">Score For this Problem: ________________</div>
                    </div>
                </div>
                """

            html_content += """
            <script>
                function printFromPage(startPage) {
                    const style = document.createElement('style');
                    style.innerHTML = `
                        @media print {
                            .cover-page {
                                display: none !important;
                            }
                            body > .page:nth-child(-n+${startPage}) {
                                display: none !important;
                            }
                        }
                    `;
                    document.head.appendChild(style);
                    window.print();
                    setTimeout(() => style.remove(), 100);
                }
            </script>
            </body>
            </html>
            """

            # from django.http import HttpResponse
            # response = HttpResponse(
            #     html_content,
            #     content_type='text/html; charset=utf-8',
            # )
            # response['Content-Disposition'] = f'attachment; filename="contest_{contest.key}.html"'

            # return response

            import pypandoc
            import tempfile

            with tempfile.NamedTemporaryFile(mode='w', suffix='.pdf', delete=False, encoding='utf-8') as f:
                pdf_path = f.name

            try:
                pypandoc.convert_text(
                    html_content,
                    'pdf',
                    format='html',
                    outputfile=pdf_path,
                    extra_args=[
                        '--pdf-engine=lualatex',
                        '--mathjax',

                        '-V', 'geometry:margin=1in',
                        '-V', 'fontsize=11pt',
                        '-V', 'linestretch=1.4',
                        '-V', 'classoption=oneside',

                        '--standalone',
                        '--self-contained',
                        '--wrap=preserve',
                        '--highlight-style=pygments',

                        '-f', 'html+tex_math_dollars+tex_math_single_backslash',
                    ],
                )

                with open(pdf_path, 'rb') as f:
                    pdf_content = f.read()

                response = HttpResponse(pdf_content, content_type='application/pdf')
                response['Content-Disposition'] = f'attachment; filename="contest_{contest_key}.pdf"'
                return response

            except Exception as error:
                logger.exception('PDF generation failed for contest %s', contest_key)
                return JsonResponse({
                    'success': False,
                    'error': str(error),
                    'message': _('Unable to export this contest to PDF.'),
                }, status=500)

            finally:
                import os
                if os.path.exists(pdf_path):
                    os.unlink(pdf_path)

        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': str(e),
                'message': 'Error exporting contest.',
            }, status=500)


class ContestExportWord(ContestMixin, View):

    def post(self, request, *args, **kwargs):
        try:
            contest_key = kwargs.get('contest')

            contest, exists = _find_contest(request, contest_key, private_check=True)
            if not exists:
                return contest

            contest_problems = contest.contest_problems.order_by('order').select_related('problem')

            totalscore = 0
            for page_num, cp in enumerate(contest_problems, 1):
                problem = cp.problem

                totalscore += problem.points

            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <!-- KaTeX CSS -->
                <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
                <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
                <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"
                    onload="renderMathInElement(document.body);"></script>
                <script>
                    document.addEventListener("DOMContentLoaded", function() {{
                        renderMathInElement(document.body, {{
                            delimiters: [
                                {{left: "$$", right: "$$", display: true}},
                                {{left: "$", right: "$", display: false}},
                                {{left: "~~", right: "~~", display: true}},
                                {{left: "~", right: "~", display: false}},
                            ],
                            throwOnError: false
                        }});
                    }});
                </script>
                <meta charset="UTF-8">
                <title>{contest.name}</title>
                <style>
                    @media print {{
                        @page {{
                            margin: 20mm 25mm;
                            size: A4;
                        }}
                        @page :first {{
                            margin-top: 15mm;
                        }}
                        body {{
                            margin: 0;
                            padding: 0;
                            color: #000 !important;
                            background: #fff !important;
                            font-size: 11pt;
                            line-height: 1.4;
                        }}
                        .page {{
                            break-inside: avoid;
                            page-break-after: always;
                        }}
                        .last-page {{
                            page-break-after: auto;
                        }}
                        .no-print {{
                            display: none !important;
                        }}
                        .cover-controls {{
                            display: none !important;
                        }}
                        pre {{
                            page-break-inside: avoid !important;
                            break-inside: avoid !important;
                        }}
                        table {{
                            page-break-inside: avoid !important;
                            break-inside: avoid !important;
                        }}
                        ul, ol {{
                            page-break-inside: avoid !important;
                        }}
                    }}
                    body {{
                        font-family: 'Times New Roman', 'SimSun', serif;
                        line-height: 1.4;
                        color: #000;
                        margin: 0;
                        padding: 0;
                        font-size: 11pt;
                        background: #fff;
                        -webkit-print-color-adjust: exact;
                        print-color-adjust: exact;
                    }}
                    .problem-description {{
                        font-size: 11pt;
                        line-height: 1.5;
                        margin-bottom: 5mm;
                        font-family: inherit;
                    }}
                    .problem-description h1,
                    .problem-description h2,
                    .problem-description h3,
                    .problem-description h4,
                    .problem-description h5,
                    .problem-description h6 {{
                        margin-top: 15px;
                        margin-bottom: 10px;
                        font-weight: bold;
                        line-height: 1.2;
                    }}
                    .problem-description h1 {{
                        font-size: 1.8em;
                        border-bottom: 2px solid #ccc;
                        padding-bottom: 5px;
                    }}
                    .problem-description h2 {{
                        font-size: 1.5em;
                        border-bottom: 1px solid #eee;
                        padding-bottom: 3px;
                    }}
                    .problem-description h3 {{
                        font-size: 1.3em;
                    }}
                    .problem-description h4 {{
                        font-size: 1.1em;
                    }}
                    .problem-description p {{
                        margin: 10px 0;
                        line-height: 1.5;
                    }}
                    .problem-description ul,
                    .problem-description ol {{
                        margin: 10px 0;
                        padding-left: 30px;
                    }}
                    .problem-description li {{
                        margin: 5px 0;
                    }}
                    .problem-description code {{
                        font-family: 'Courier New', 'Consolas', monospace;
                        background-color: #f5f5f5;
                        padding: 2px 4px;
                        border-radius: 3px;
                        font-size: 0.9em;
                    }}
                    .problem-description pre {{
                        font-family: 'Courier New', 'Consolas', monospace;
                        background-color: #f8f8f8;
                        border: 1px solid #ddd;
                        border-radius: 4px;
                        padding: 10px;
                        overflow: auto;
                        margin: 15px 0;
                        font-size: 0.9em;
                        line-height: 1.3;
                    }}
                    .problem-description pre code {{
                        background-color: transparent;
                        padding: 0;
                        border-radius: 0;
                    }}
                    .problem-description table {{
                        border-collapse: collapse;
                        border-spacing: 0;
                        width: 100%;
                        margin: 15px 0;
                        font-size: 0.95em;
                    }}
                    .problem-description table th,
                    .problem-description table td {{
                        border: 1px solid #ddd;
                        padding: 8px 12px;
                        text-align: left;
                    }}
                    .problem-description table th {{
                        background-color: #f5f5f5;
                        font-weight: bold;
                    }}
                    .problem-description blockquote {{
                        border-left: 4px solid #ddd;
                        padding-left: 15px;
                        margin: 15px 0;
                        color: #666;
                    }}
                    .problem-description a {{
                        color: #0066cc;
                        text-decoration: underline;
                    }}
                    .problem-description hr {{
                        border: 0;
                        border-top: 1px solid #ddd;
                        margin: 20px 0;
                    }}
                    .problem-description strong {{
                        font-weight: bold;
                    }}
                    .problem-description em {{
                        font-style: italic;
                    }}
                    .cover-page {{
                        height: 277mm;
                        display: flex;
                        flex-direction: column;
                        justify-content: center;
                        align-items: center;
                        text-align: center;
                        padding: 20mm;
                        box-sizing: border-box;
                        border-bottom: 2px solid #000;
                    }}
                    .contest-title {{
                        font-size: 24pt;
                        font-weight: bold;
                        margin-bottom: 15mm;
                        text-decoration: underline;
                        text-underline-offset: 5px;
                    }}
                    .contest-info {{
                        font-size: 14pt;
                        margin-bottom: 20mm;
                        line-height: 2;
                    }}
                    .instructions {{
                        font-size: 10pt;
                        margin-top: 25mm;
                        padding: 5mm;
                        border: 1px solid #000;
                        width: 80%;
                        text-align: left;
                    }}
                    .instructions h3 {{
                        margin-top: 0;
                        text-align: center;
                    }}
                    .instructions ul {{
                        padding-left: 20px;
                        margin: 10px 0;
                    }}
                    .page {{
                        min-height: 257mm;
                        padding: 15mm 25mm;
                        box-sizing: border-box;
                        position: relative;
                        border-bottom: 1px solid #eee;
                    }}
                    .page-header {{
                        border-bottom: 2px solid #000;
                        padding-bottom: 5mm;
                        margin-bottom: 8mm;
                        display: flex;
                        justify-content: space-between;
                        align-items: flex-end;
                    }}
                    .contest-name {{
                        font-size: 12pt;
                        font-weight: bold;
                    }}
                    .problem-info {{
                        text-align: right;
                        font-size: 10pt;
                    }}
                    .problem-points {{
                        font-size: 14pt;
                        font-weight: bold;
                        color: #d00;
                    }}
                    .problem-content {{
                        margin-bottom: 10mm;
                    }}
                    .problem-title {{
                        font-size: 16pt;
                        font-weight: bold;
                        margin-bottom: 5mm;
                        padding-bottom: 3mm;
                        border-bottom: 1px solid #666;
                    }}
                    .problem-meta {{
                        font-size: 10pt;
                        color: #666;
                        margin-bottom: 8mm;
                        display: flex;
                        gap: 15mm;
                    }}
                    .problem-description {{
                        font-size: 11pt;
                        line-height: 1.5;
                        margin-bottom: 5mm;
                        white-space: pre-wrap;
                        font-family: inherit;
                    }}
                    .answer-area {{
                        margin-top: 15mm;
                        padding-top: 8mm;
                        border-top: 1px dashed #999;
                    }}
                    .answer-header {{
                        font-size: 12pt;
                        font-weight: bold;
                        margin-bottom: 5mm;
                        color: #006;
                    }}
                    .answer-box {{
                        min-height: 120mm;
                        border: 2px solid #ccc;
                        padding: 5mm;
                        margin-bottom: 3mm;
                        background: linear-gradient(to bottom, transparent 95%, #f0f0f0 95%);
                        background-size: 100% 25px;
                        line-height: 25px;
                    }}
                    .page-footer {{
                        position: absolute;
                        bottom: 15mm;
                        left: 25mm;
                        right: 25mm;
                        font-size: 9pt;
                        color: #666;
                        display: flex;
                        justify-content: space-between;
                        padding-top: 3mm;
                        border-top: 1px solid #ccc;
                    }}
                    .page-number {{
                        text-align: center;
                        flex-grow: 1;
                    }}
                    .student-info {{
                        position: absolute;
                        bottom: 5mm;
                        left: 25mm;
                        right: 25mm;
                        font-size: 9pt;
                        display: flex;
                        justify-content: space-between;
                    }}
                    .info-field {{
                        width: 40mm;
                        border-bottom: 1px solid #999;
                        text-align: center;
                        padding-bottom: 2px;
                    }}
                    .cover-controls {{
                        position: fixed;
                        top: 20px;
                        right: 20px;
                        z-index: 1000;
                        background: white;
                        padding: 15px;
                        border: 2px solid #007bff;
                        border-radius: 5px;
                        box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                    }}
                    .cover-controls h4 {{
                        margin-top: 0;
                        margin-bottom: 10px;
                        color: #007bff;
                    }}
                    .print-btn {{
                        background-color: #007bff;
                        color: white;
                        border: none;
                        padding: 8px 15px;
                        border-radius: 4px;
                        cursor: pointer;
                        font-size: 12px;
                        display: block;
                        width: 100%;
                        margin-bottom: 5px;
                    }}
                    .print-btn:hover {{
                        background-color: #0056b3;
                    }}
                    .text-center {{ text-align: center; }}
                    .text-right {{ text-align: right; }}
                    .bold {{ font-weight: bold; }}
                    .italic {{ font-style: italic; }}
                    .problem-description .math,
                    .problem-description .katex {{
                        font-size: 1.1em;
                        text-align: center;
                        margin: 10px 0;
                    }}
                    .problem-description .katex-display {{
                        overflow: auto hidden;
                        padding: 5px 0;
                    }}
                    .problem-description img {{
                        max-width: 100%;
                        height: auto;
                        display: block;
                        margin: 15px auto;
                    }}
                    .problem-description .task-list-item {{
                        list-style-type: none;
                        margin-left: -20px;
                    }}
                    .problem-description .task-list-item-checkbox {{
                        margin-right: 8px;
                    }}
                </style>
            </head>
            <body>
                <!-- 封面页 -->
                <div class="page cover-page">
                    <div class="contest-info">
                        <div>
                            <span class="bold">Start Time: </span>
                            {contest.start_time}
                        </div>
                        <div>
                            <span class="bold">End Time: </span>
                            {contest.end_time}
                        </div>
                        <div>
                            <span class="bold">Total Problems: </span>
                            {contest_problems.count()}
                        </div>
                        <div style="margin-top: 10mm;">
                            <span class="bold">Student Name: </span>
                            ____________________
                        </div>
                        <div>
                            <span class="bold">Student NetID: </span>
                            ____________________
                        </div>
                    </div>
                    <div class="instructions">
                        <ul>
                            <li>Total Score: {totalscore}</li>
                            <li>Pages: {contest_problems.count() + 1}</li>
                            {contest.description}
                        </ul>
                        <div class="text-right italic">—— {timezone.now().strftime('%m/%d/%Y')} ——</div>
                    </div>
                </div>
            """

            for page_num, cp in enumerate(contest_problems, 1):
                problem = cp.problem
                points = problem.points

                if problem.description:
                    description = problem.description

                description_html = mark_safe(markdown.markdown(
                    re.sub(r'~([^~]+?)~', r'$\1$', description),
                    extensions=[
                        'markdown.extensions.extra',
                        'markdown.extensions.codehilite',
                        'markdown.extensions.tables',
                        'markdown.extensions.toc',
                        'markdown.extensions.nl2br',
                        'markdown.extensions.sane_lists',
                        KatexExtension(),
                    ],
                    output_format='html5',
                ))

                html_content += f"""
                <!-- Problem {page_num} -->
                <div class="page {'last-page' if page_num == contest_problems.count() else ''}">
                    <!-- 页眉 -->
                    <div class="page-header">
                        <div class="problem-info">
                            Problem {page_num} / {contest_problems.count()} <br>
                            <span class="problem-points">{points} - points</span>
                        </div>
                    </div>
                    <!-- 题目内容 -->
                    <div class="problem-content">
                        <div class="problem-title">
                            Problem {page_num}: {contest_problem_display_name(cp)}
                        </div>
                        <div class="problem-description">
                            {description_html}
                        </div>
                    </div>
                    <!-- 页脚 -->
                    <div class="page-footer">
                        <div class="text-left">Problem {page_num}</div>
                        <div class="text-right">Points: {points}</div>
                    </div>
                </div>
                """

                html_content += f"""
                <!-- Problem {page_num} -->
                <div class="page {'last-page' if page_num == contest_problems.count() else ''}">
                    <!-- 页眉 -->
                    <div class="page-header">
                        <div class="problem-info">
                            Problem {page_num} / {contest_problems.count()} <br>
                            <span class="problem-points">{points} - points</span>
                        </div>
                    </div>
                    <!-- 答题区域 -->
                    <div class="answer-area">
                        <div class="answer-header">
                            Answer Area (write down your answer in this area)
                        </div>
                        <div class="answer-box">
                            <!-- 答题格子线 - 通过CSS背景实现 -->
                        </div>
                    </div>
                    <!-- 页脚 -->
                    <div class="page-footer">
                        <div class="text-left">Problem {page_num}</div>
                        <div class="text-right">Points: {points}</div>
                    </div>
                    <!-- 学生信息填写区 -->
                    <div class="student-info">
                        <div class="info-field">Student Name: ________________</div>
                        <div class="info-field">Student NetID: ________________</div>
                        <div class="info-field">Score For this Problem: ________________</div>
                    </div>
                </div>
                """

            html_content += """
            <script>
                function printFromPage(startPage) {
                    const style = document.createElement('style');
                    style.innerHTML = `
                        @media print {
                            .cover-page {
                                display: none !important;
                            }
                            body > .page:nth-child(-n+${startPage}) {
                                display: none !important;
                            }
                        }
                    `;
                    document.head.appendChild(style);
                    window.print();
                    setTimeout(() => style.remove(), 100);
                }
            </script>
            </body>
            </html>
            """

            import pypandoc
            import tempfile

            with tempfile.NamedTemporaryFile(mode='w', suffix='.docx', delete=False, encoding='utf-8') as f:
                docx_path = f.name

            try:
                pypandoc.convert_text(
                    html_content,
                    'docx',
                    format='html',
                    outputfile=docx_path,
                    extra_args=[
                        '--standalone',
                        '--self-contained',
                        '--wrap=preserve',
                        '--highlight-style=pygments',

                        '-f', 'html+tex_math_dollars+tex_math_single_backslash',

                        '--columns=80',
                    ],
                )

                with open(docx_path, 'rb') as f:
                    docx_content = f.read()

                response = HttpResponse(
                    docx_content,
                    content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                )
                response['Content-Disposition'] = f'attachment; filename="contest_{contest_key}.docx"'
                return response

            except Exception as error:
                logger.exception('Word generation failed for contest %s', contest_key)
                return JsonResponse({
                    'success': False,
                    'error': str(error),
                    'message': _('Unable to export this contest to Word.'),
                }, status=500)

            finally:
                import os
                if os.path.exists(docx_path):
                    os.unlink(docx_path)

        except Exception as e:
            return JsonResponse({
                'success': False,
                'error': str(e),
                'message': 'Error exporting contest.',
            }, status=500)


class ContestExportScores(ContestMixin, PermissionRequiredMixin, BaseDetailView):
    permission_required = 'judge.edit_own_contest'

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.object.is_editable_by(request.user):
            return self.handle_no_permission()

        from judge.utils.import_export import export_contest_scores_csv
        # Defaults to match management command behavior, can be expanded with query params if needed
        csv_content = export_contest_scores_csv(self.object, include_disqualified=False, include_virtual=False)

        response = HttpResponse(csv_content, content_type='text/csv; charset=utf-8')
        filename = f'{self.object.key}-scores.csv'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class ContestExportJoinedScores(ContestMixin, PermissionRequiredMixin, BaseDetailView):
    permission_required = 'judge.edit_own_contest'
    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.object.is_editable_by(request.user):
            return self.handle_no_permission()

        csv_file = request.FILES.get('grade_csv')
        if not csv_file:
            messages.error(request, _('Choose a CSV file to upload.'))
            return HttpResponseRedirect(reverse('contest_view', args=[self.object.key]))

        from judge.utils.import_export import export_contest_scores_joined_csv
        result = export_contest_scores_joined_csv(
            self.object, csv_file, score_column=request.POST.get('score_column'),
        )
        if result.errors:
            for error in result.errors:
                messages.error(request, error)
            return HttpResponseRedirect(reverse('contest_view', args=[self.object.key]))

        response = HttpResponse(result.csv_content, content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="{}-joined-scores.csv"'.format(self.object.key)
        export_token = request.POST.get('export_token', '')
        if re.fullmatch(r'[A-Za-z0-9-]{8,128}', export_token):
            response.set_cookie(
                'contest_score_export', export_token,
                max_age=60, path='/', samesite='Lax', secure=request.is_secure(),
            )
        return response


class ContestExportSubmissions(ContestMixin, PermissionRequiredMixin, BaseDetailView):
    permission_required = 'judge.edit_own_contest'

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.object.is_editable_by(request.user):
            return self.handle_no_permission()

        from judge.utils.import_export import export_contest_submissions_zip
        # Defaults to match management command behavior
        zip_bytes = export_contest_submissions_zip(self.object, include_disqualified=False,
                                                   include_virtual=False, latest_only=True)

        response = HttpResponse(zip_bytes, content_type='application/zip')
        filename = f'{self.object.key}-submissions.zip'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class ContestSEBConfig(LoginRequiredMixin, ContestMixin, BaseDetailView):
    """The "Download config file" button: same bytes, but for a real browser.

    SEB itself cannot use this URL — see :class:`SEBLaunchConfig`.
    """

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return _stream_seb_config(self.object)


def _stream_seb_config(contest):
    """Stream ``contest``'s effective ``.seb`` file, or 404/503."""
    # The file may be the contest's own upload or the one inherited from its
    # preset; either way it is served under the contest's own name.
    config_file = contest.seb_effective_config_file
    if not contest.seb_required or not config_file:
        raise Http404()
    try:
        fh = config_file.open('rb')
    except StorageUnavailableError:
        return HttpResponse(_('The file store is temporarily unavailable. Please try again later.'),
                            status=503, content_type='text/plain')
    return FileResponse(
        fh,
        as_attachment=True,
        filename=f'{contest.key}.seb',
        content_type='application/seb',
    )


class SEBLaunchConfig(View):
    """Serve a contest's ``.seb`` to the SEB client itself, authorized by a token.

    Deliberately has neither ``LoginRequiredMixin`` nor ``ContestMixin``. SEB
    requests this URL before it has a browser session, so any redirect to the
    login page would come back as HTML; SEB then abandons the configuration,
    opens the page in a temporary window still running its *local* settings, and
    never enters lockdown — the small, quittable window students reported.
    ``ContestMixin`` is skipped for the same reason: its ``access_check`` renders
    a 403 page for an anonymous visitor to a private contest.

    The signed token minted by :func:`sign_seb_launch_token` is the
    authorization. Nothing is weakened by serving the file here: entering the
    exam still requires a SEB client whose per-URL key hash matches, which
    ``ContestMiddleware`` and ``ContestJoin`` both check.
    """

    def get(self, request, *args, **kwargs):
        try:
            data = read_seb_launch_token(kwargs['token'])
        except signing.SignatureExpired:
            return self.stale(request, _('This launch link has expired.'))
        except signing.BadSignature:
            return self.stale(request, _('This launch link is not valid.'))
        contest = get_object_or_404(Contest, pk=data.get('c'))
        response = _stream_seb_config(contest)
        # A cached .seb would let a stale configuration start a later exam.
        response['Cache-Control'] = 'no-store'
        return response

    def stale(self, request, reason):
        # Must render, never redirect: a redirect puts SEB back on the
        # "configuration resource needs authentication" path this view exists to
        # avoid, leaving it parked in an unconfigured window.
        return render(request, 'contest/seb_launch_expired.html', {
            'reason': reason,
            'title': _('Launch link expired'),
        }, status=400)


class SEBLaunchToken(LoginRequiredMixin, ContestMixin, SingleObjectMixin, View):
    """Mint a fresh ``sebs://`` launch URL for the "Launch in SEB" button.

    The token embedded at page render expires; a student who opened the exam
    page well before the start time would otherwise click a dead link. The
    button re-mints here on click and falls back to the rendered href if this
    request fails.
    """

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.object.seb_required or not self.object.seb_effective_config_file:
            raise Http404()
        return JsonResponse({'url': seb_launch_url(request, self.object, request.profile)})


class SEBPresetConfig(LoginRequiredMixin, View):
    """Stream a preset's ``.seb`` so an editor can inspect what they are adopting.

    Presets live in private object storage like every other uploaded file, so
    the editor page cannot link to ``config_file.url``. Gated on the same
    permission that reveals the SEB controls at all.
    """

    def get(self, request, *args, **kwargs):
        if not request.user.has_perm('judge.contest_seb_requirement'):
            raise PermissionDenied()
        preset = get_object_or_404(SEBConfigPreset, pk=kwargs['pk'])
        if not preset.config_file:
            raise Http404()
        try:
            fh = preset.config_file.open('rb')
        except StorageUnavailableError:
            return HttpResponse(_('The file store is temporarily unavailable. Please try again later.'),
                                status=503, content_type='text/plain')
        # slugify() of a fully non-ASCII name is empty, which would offer the
        # browser a bare ".seb" dotfile.
        return FileResponse(fh, as_attachment=True,
                            filename='%s.seb' % (slugify(preset.name) or 'seb-config'),
                            content_type='application/seb')


class SEBQuit(TitleMixin, TemplateView):
    template_name = 'contest/seb_quit.html'

    def get_title(self):
        return _('Exam finished')
