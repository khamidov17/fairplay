from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.encoding import smart_str
from django.utils.translation import gettext as _
from django.views.generic.list import BaseListView

from judge.jinja2.gravatar import gravatar
from judge.models import Class, Comment, Contest, Organization, Problem, Profile
from judge.models.notification import user_in_instructors_group
from judge.utils.user_search import (
    format_user_label, profile_choice_label, profile_person_name, profile_user_search_q,
)

# CA-4: user pickers need at least this many characters before they search.
USER_SEARCH_MIN_TERM_LENGTH = 2


def user_search_is_site_wide(user):
    """May this caller search every (listed) user and see real names?"""
    if not user.is_authenticated:
        return False
    return bool(
        user.is_superuser or user.is_staff or
        user.has_perm('judge.edit_all_organization') or
        user.has_perm('judge.add_organization') or
        user_in_instructors_group(user),
    )


def user_search_sees_real_names(user):
    """Real names are for teachers/staff and course or class managers, never students."""
    if user_search_is_site_wide(user):
        return True
    if not user.is_authenticated:
        return False
    profile = user.profile
    return (Organization.objects.filter(admins=profile).exists() or
            Class.objects.filter(admins=profile).exists())


def user_search_scope_q(user):
    """Profiles a non-site-wide caller may find: people sharing a course or class."""
    profile = user.profile
    org_ids = set(profile.organizations.values_list('id', flat=True))
    org_ids.update(Organization.objects.filter(admins=profile).values_list('id', flat=True))
    class_ids = set(profile.classes.values_list('id', flat=True))
    class_ids.update(Class.objects.filter(admins=profile).values_list('id', flat=True))
    return (Q(id=profile.id) | Q(organizations__in=org_ids) | Q(admin_of__in=org_ids) |
            Q(class__in=class_ids) | Q(class_admin_of__in=class_ids))


def user_search_term_too_short(term):
    return len((term or '').strip()) < USER_SEARCH_MIN_TERM_LENGTH


def _username_only_q(term):
    q = Q()
    for token in (term or '').split():
        q &= Q(user__username__icontains=token)
    return q


def _get_user_queryset(term, names=True):
    qs = Profile.objects
    if term.endswith(' '):
        exact = term.strip()
        exact_q = Q(user__username=exact)
        if names:
            exact_q |= Q(username_display_override=exact) | Q(user__first_name=exact) | Q(user__last_name=exact)
        qs = qs.filter(exact_q)
    else:
        qs = qs.filter(profile_user_search_q(term) if names else _username_only_q(term))
    return qs


def scoped_user_queryset(user, term):
    """CA-4: the Profile search every user picker shares (login, min length, scope, unlisted)."""
    if not user.is_authenticated or user_search_term_too_short(term):
        return Profile.objects.none()
    names = user_search_sees_real_names(user)
    qs = _get_user_queryset(term, names=names).filter(user__is_active=True)
    if not user.is_superuser:
        qs = qs.filter(Q(is_unlisted=False) | Q(id=user.profile.id))
    if not user_search_is_site_wide(user):
        qs = qs.filter(user_search_scope_q(user))
    return qs.distinct()


class Select2View(BaseListView):
    paginate_by = 20

    def get(self, request, *args, **kwargs):
        self.request = request
        self.term = kwargs.get('term', request.GET.get('term', ''))
        self.object_list = self.get_queryset()
        context = self.get_context_data()

        return JsonResponse({
            'results': [
                {
                    'text': smart_str(self.get_name(obj)),
                    'id': obj.pk,
                } for obj in context['object_list']],
            'more': context['page_obj'].has_next(),
        })

    def get_name(self, obj):
        return str(obj)


class UserSelect2View(Select2View):
    def get_queryset(self):
        return scoped_user_queryset(self.request.user, self.term).select_related('user').order_by('user__username')

    def get_name(self, obj):
        if user_search_sees_real_names(self.request.user):
            return profile_choice_label(obj)
        return obj.user.username


class AuthorFilterSelect2View(UserSelect2View):
    """Profile picker for problem-author filters. Students get no users."""

    def get(self, request, *args, **kwargs):
        from judge.views.problem import user_can_see_problem_author_metadata
        if not user_can_see_problem_author_metadata(request.user):
            return JsonResponse({'results': [], 'more': False})
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        from judge.views.problem import user_can_see_problem_author_metadata
        if not user_can_see_problem_author_metadata(self.request.user):
            return Profile.objects.none()
        return super().get_queryset()


class OrganizationSelect2View(Select2View):
    def get_queryset(self):
        queryset = Organization.objects.filter(name__icontains=self.term)
        user = self.request.user
        if not user.is_authenticated:
            return queryset.none()
        if user.has_perm('judge.edit_all_organization'):
            return queryset
        return queryset.filter(admins=user.profile)


class ClassSelect2View(Select2View):
    def get_queryset(self):
        queryset = Class.get_visible_classes(self.request.user).filter(name__icontains=self.term)
        # Filter by organization if provided
        org_ids = self.request.GET.get('organizations', '')
        if not org_ids:
            return queryset.none()
        org_id_list = [int(x) for x in org_ids.split(',') if x.isdigit()]
        if not org_id_list:
            return queryset.none()
        return queryset.filter(organization_id__in=org_id_list)


class ProblemSelect2View(Select2View):
    def get_queryset(self):
        return Problem.get_visible_problems(self.request.user) \
                      .filter(Q(code__icontains=self.term) | Q(name__icontains=self.term))


class ContestSelect2View(Select2View):
    def get_queryset(self):
        return Contest.get_visible_contests(self.request.user) \
                      .filter(Q(key__icontains=self.term) | Q(name__icontains=self.term))


class ProblemFilterSelect2View(Select2View):
    """Submission-list problem filter: id is problem code (querystring-friendly)."""

    def get_queryset(self):
        return Problem.get_visible_problems(self.request.user) \
                      .filter(Q(code__icontains=self.term) | Q(name__icontains=self.term)) \
                      .only('code', 'name').order_by('code')

    def get(self, request, *args, **kwargs):
        self.request = request
        self.term = kwargs.get('term', request.GET.get('term', ''))
        self.object_list = self.get_queryset()
        context = self.get_context_data()
        return JsonResponse({
            'results': [
                {
                    'text': '%s — %s' % (obj.code, obj.name),
                    'id': obj.code,
                } for obj in context['object_list']
            ],
            'more': context['page_obj'].has_next(),
        })


class ContestFilterSelect2View(Select2View):
    """Submission-list contest filter: id is contest key; includes practice-only option."""

    def get_queryset(self):
        return Contest.get_visible_contests(self.request.user) \
                      .filter(Q(key__icontains=self.term) | Q(name__icontains=self.term)) \
                      .only('key', 'name').order_by('key')

    def get(self, request, *args, **kwargs):
        self.request = request
        self.term = kwargs.get('term', request.GET.get('term', ''))
        self.object_list = self.get_queryset()
        context = self.get_context_data()
        results = []
        practice_label = str(_('Practice only (no contest)'))
        term_l = self.term.lower()
        if not self.term or term_l in practice_label.lower() or self.term in ('__none__', 'practice'):
            results.append({'id': '__none__', 'text': practice_label})
        results.extend({
            'text': '%s — %s' % (obj.key, obj.name),
            'id': obj.key,
        } for obj in context['object_list'])
        return JsonResponse({
            'results': results,
            'more': context['page_obj'].has_next(),
        })


class CommentSelect2View(Select2View):
    def get_queryset(self):
        return Comment.objects.filter(page__icontains=self.term)


class UserSearchSelect2View(BaseListView):
    paginate_by = 20

    def get_queryset(self):
        return scoped_user_queryset(self.request.user, self.term)

    def search_q(self):
        """Name-or-username match for teachers, username only for students (CA-4)."""
        if user_search_sees_real_names(self.request.user):
            return profile_user_search_q(self.term)
        return _username_only_q(self.term)

    def get(self, request, *args, **kwargs):
        self.request = request
        self.kwargs = kwargs
        self.term = kwargs.get('term', request.GET.get('term', ''))
        self.gravatar_size = request.GET.get('gravatar_size', 128)
        self.gravatar_default = request.GET.get('gravatar_default', None)

        # CA-4: login required, and no listing of everybody with an empty term.
        if not request.user.is_authenticated or user_search_term_too_short(self.term):
            return JsonResponse({'results': [], 'more': False})
        show_names = user_search_sees_real_names(request.user)

        self.object_list = self.get_queryset().values_list(
            'pk', 'user__username', 'user__email', 'display_rank',
            'username_display_override', 'user__first_name', 'user__last_name',
        )

        context = self.get_context_data()

        return JsonResponse({
            'results': [
                {
                    'text': format_user_label(
                        profile_person_name(first_name, last_name, username_override, username),
                        username,
                    ) if show_names else username,
                    'id': username,
                    'gravatar_url': gravatar(email, self.gravatar_size, self.gravatar_default),
                    'display_rank': display_rank,
                } for pk, username, email, display_rank, username_override, first_name, last_name
                in context['object_list']],
            'more': context['page_obj'].has_next(),
        })

    def get_name(self, obj):
        return str(obj)


class ContestUserSearchSelect2View(UserSearchSelect2View):
    def get_queryset(self):
        contest = get_object_or_404(Contest, key=self.kwargs['contest'])
        if not contest.is_accessible_by(self.request.user) or not contest.can_see_full_scoreboard(self.request.user):
            raise Http404()

        return Profile.objects.filter(contest_history__contest=contest).filter(
            self.search_q(),
        ).distinct()


class TicketUserSelect2View(UserSearchSelect2View):
    def get_queryset(self):
        return Profile.objects.filter(tickets__isnull=False).filter(
            self.search_q(),
        ).distinct()


class AssigneeSelect2View(UserSearchSelect2View):
    def get_queryset(self):
        return Profile.objects.filter(assigned_tickets__isnull=False).filter(
            self.search_q(),
        ).distinct()
