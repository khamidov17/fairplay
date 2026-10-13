from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.encoding import smart_str
from django.utils.translation import gettext as _
from django.views.generic.list import BaseListView

from judge.jinja2.gravatar import gravatar
from judge.models import Class, Comment, Contest, Organization, Problem, Profile
from judge.utils.user_search import (
    format_user_label, profile_choice_label, profile_person_name, profile_user_search_q,
)


def _get_user_queryset(term):
    qs = Profile.objects
    if term.endswith(' '):
        exact = term.strip()
        qs = qs.filter(
            Q(user__username=exact) |
            Q(username_display_override=exact) |
            Q(user__first_name=exact) |
            Q(user__last_name=exact),
        )
    else:
        qs = qs.filter(profile_user_search_q(term))
    return qs


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
        return _get_user_queryset(self.term).select_related('user').order_by('user__username')

    def get_name(self, obj):
        return profile_choice_label(obj)


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
        return _get_user_queryset(self.term).filter(is_unlisted=False)

    def get(self, request, *args, **kwargs):
        self.request = request
        self.kwargs = kwargs
        self.term = kwargs.get('term', request.GET.get('term', ''))
        self.gravatar_size = request.GET.get('gravatar_size', 128)
        self.gravatar_default = request.GET.get('gravatar_default', None)

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
                    ),
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
            profile_user_search_q(self.term),
        ).distinct()


class TicketUserSelect2View(UserSearchSelect2View):
    def get_queryset(self):
        return Profile.objects.filter(tickets__isnull=False).filter(
            profile_user_search_q(self.term),
        ).distinct()


class AssigneeSelect2View(UserSearchSelect2View):
    def get_queryset(self):
        return Profile.objects.filter(assigned_tickets__isnull=False).filter(
            profile_user_search_q(self.term),
        ).distinct()
