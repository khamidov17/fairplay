import re
from operator import attrgetter

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.core.exceptions import PermissionDenied
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery, Value
from django.forms import Form, modelformset_factory
from django.http import Http404, HttpResponsePermanentRedirect, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.html import json_script
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify
from django.utils.translation import gettext as _, gettext_lazy, ngettext
from django.views.generic import DetailView, FormView, ListView, UpdateView, View
from django.views.generic.detail import SingleObjectMixin, SingleObjectTemplateResponseMixin
from reversion import revisions

from judge.forms import ClassAssignmentsForm, ClassSiteForm, EditOrganizationForm, OrganizationSiteForm
from judge.models import AdminNotification, Class, ClassMaterial, Contest, InstructorVerificationRequest, \
    Organization, OrganizationRequest, Profile
from judge.models.notification import ensure_instructors_group, user_in_instructors_group
from judge.subjects import prefix_labels
from judge.utils.admin_notify import (
    notify_class_join, notify_course_join, notify_instructor_verification,
)
from judge.utils.import_export import process_user_csv
from judge.utils.organization_membership import sync_class_members_to_organization
from judge.utils.ranker import ranker
from judge.utils.views import DiggPaginatorMixin, QueryStringSortMixin, TitleMixin, generic_message

__all__ = ['OrganizationList', 'OrganizationHome', 'OrganizationUsers', 'OrganizationMembershipChange',
           'JoinOrganization', 'LeaveOrganization', 'EditOrganization', 'CreateOrganization',
           'DeleteOrganization', 'RequestJoinOrganization',
           'OrganizationRequestDetail', 'OrganizationRequestView', 'OrganizationRequestLog',
           'InstructorJoinRequestsView', 'InstructorVerificationInboxView',
           'AdminNotificationInboxView', 'InstructorVerificationSubmitView',
           'KickUserWidgetView', 'ClassHome', 'RequestJoinClass',
           'OrganizationImportUsers', 'CreateClass', 'EditClass', 'DeleteClass',
           'ClassLinkAssignmentsView', 'ClassMaterialActionView', 'ClassMaterialDownloadView',
           'ClassImportMembers']


# Materials upload policy (extensions lowercase, without leading dot).
CLASS_MATERIAL_ALLOWED_EXTENSIONS = frozenset({
    'pdf', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'csv', 'txt', 'md',
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'zip', 'py', 'c', 'cpp', 'h', 'hpp',
    'java', 'js', 'ts', 'html', 'css', 'json',
})
CLASS_MATERIAL_MAX_BYTES = 25 * 1024 * 1024  # 25 MiB


def unique_organization_slug(base):
    slug = slugify(base)[:120] or 'course'
    candidate = slug
    n = 2
    while Organization.objects.filter(slug=candidate).exists():
        candidate = ('%s-%d' % (slug, n))[:128]
        n += 1
    return candidate


def unique_class_slug(organization, base):
    slug = slugify(base)[:120] or 'class'
    candidate = slug
    n = 2
    while Class.objects.filter(organization=organization, slug=candidate).exists():
        candidate = ('%s-%d' % (slug, n))[:128]
        n += 1
    return candidate


def plain_text_summary(text, max_len=160):
    """Strip light markdown/markup for class-card previews."""
    if not text:
        return ''
    cleaned = text.replace('\r\n', '\n').replace('\r', '\n')
    cleaned = re.sub(r'```.*?```', ' ', cleaned, flags=re.S)
    cleaned = re.sub(r'`([^`]*)`', r'\1', cleaned)
    cleaned = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', cleaned)
    cleaned = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', cleaned)
    cleaned = re.sub(r'[#>*_~]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + '…'
    return cleaned


def remove_profile_from_organization(profile, org):
    """Leave course and drop all class memberships under that course."""
    profile.organizations.remove(org)
    org.instructors.remove(profile)
    class_ids = list(org.classes.values_list('id', flat=True))
    if class_ids:
        profile.classes.remove(*class_ids)
        for klass in org.classes.filter(id__in=class_ids):
            klass.admins.remove(profile)
    cache.delete(make_template_fragment_key('org_member_count', (org.id,)))


def safe_redirect_url(request, candidate, fallback):
    if candidate and url_has_allowed_host_and_scheme(
        url=candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return fallback


def orgs_administered_by(profile):
    """Courses where profile is a course administrator (Organization.admins)."""
    if profile is None:
        return Organization.objects.none()
    return Organization.objects.filter(admins=profile)


def orgs_reviewable_by(profile):
    """Courses with join requests this profile may review (course admin or class instructor)."""
    if profile is None:
        return Organization.objects.none()
    # Reverse FK uses related_query_name 'class' in lookups, related_name 'classes' on instances.
    return Organization.objects.filter(
        Q(admins=profile) | Q(class__admins=profile),
    ).distinct()


def user_can_edit_all_organizations(user):
    return bool(user and user.is_authenticated and (
        user.is_superuser or user.has_perm('judge.edit_all_organization')
    ))


def profile_is_org_admin(profile, org):
    return bool(profile and org.admins.filter(id=profile.id).exists())


def profile_manages_organization(profile, org, user=None):
    """Course-level manage: course administrators (set at course create) or site editors.

    Class instructors do NOT manage the whole course — only their own class via
    profile_manages_class / profile_is_class_admin.
    """
    if user_can_edit_all_organizations(user):
        return True
    return profile_is_org_admin(profile, org)


def profile_is_site_teacher(profile, user=None):
    """Approved site-wide Teacher (Instructors group / create-course permission)."""
    user = user or (profile.user if profile is not None else None)
    if user is None or not user.is_authenticated:
        return False
    if user.has_perm('judge.add_organization'):
        return True
    return user_in_instructors_group(user)


def profile_is_org_member(profile, org):
    return bool(profile and org.members.filter(id=profile.id).exists())


def profile_can_view_course(profile, org, user=None):
    """See course content (classes list / shared materials context).

    Members, course admins, class instructors of this course, and site editors.
    Non-members may still open a limited join page separately.
    """
    if user_can_edit_all_organizations(user):
        return True
    if profile is None or org is None:
        return False
    if profile_is_org_admin(profile, org) or profile_is_org_member(profile, org):
        return True
    return Class.objects.filter(organization=org, admins=profile).exists()


def profile_is_course_teacher(profile, org):
    if profile is None or org is None:
        return False
    return org.profile_teaches_course(profile)


def profile_can_create_class(profile, org, user=None):
    """Course admins, or site teachers who are already course members."""
    if profile_manages_organization(profile, org, user):
        return True
    if profile is None or org is None:
        return False
    return profile_is_site_teacher(profile, user) and profile_is_org_member(profile, org)


def profile_is_class_admin(profile, class_obj):
    return bool(profile and class_obj.admins.filter(id=profile.id).exists())


def profile_manages_class(profile, class_obj, user=None):
    """Class admin, parent course admin, or site-wide org editor."""
    if user_can_edit_all_organizations(user):
        return True
    if profile is None:
        return False
    org = class_obj.organization
    return profile_is_org_admin(profile, org) or profile_is_class_admin(profile, class_obj)


def profile_has_pending_class_request(profile, class_obj):
    if profile is None:
        return False
    return class_obj.organization.requests.filter(
        user=profile, state='P', request_class=class_obj,
    ).exists()


def profile_can_access_class(profile, class_obj, user=None, *, allow_pending=True):
    """Who may open the class detail page (not necessarily edit).

    - Class/course managers and class members (students) as before.
    - Course teachers may open every class in that course (read-only unless they
      are also class instructors).
    """
    if profile_manages_class(profile, class_obj, user):
        return True
    if profile is None:
        return False
    if class_obj.members.filter(id=profile.id).exists():
        return True
    org = class_obj.organization
    if profile_is_course_teacher(profile, org):
        return True
    if allow_pending and profile_has_pending_class_request(profile, class_obj):
        return True
    return False


def profile_can_view_class_content(profile, class_obj, user=None):
    """Who may see materials / assignments / students list (view only).

    Same audience as profile_can_access_class for teachers-in-course and members;
    editing still requires profile_manages_class.
    """
    if profile_manages_class(profile, class_obj, user):
        return True
    if profile is None:
        return False
    if class_obj.members.filter(id=profile.id).exists():
        return True
    org = class_obj.organization
    return profile_is_course_teacher(profile, org)


def pending_requests_for_reviewer(profile, user=None, organization=None):
    """Pending join requests visible to this instructor/admin (optionally scoped to one course)."""
    qs = OrganizationRequest.objects.filter(state='P')
    if organization is not None:
        qs = qs.filter(organization=organization)
    if user_can_edit_all_organizations(user):
        return qs
    if profile is None:
        return OrganizationRequest.objects.none()
    org_admin_ids = set(Organization.objects.filter(admins=profile).values_list('id', flat=True))
    class_admin_ids = set(Class.objects.filter(admins=profile).values_list('id', flat=True))
    return qs.filter(
        Q(organization_id__in=org_admin_ids) | Q(request_class_id__in=class_admin_ids),
    )


def users_for_template(users, order):
    return ranker(users.filter(is_unlisted=False).order_by(order)
                  .select_related('user').defer('about', 'user_script', 'notes'))


def user_can_review_site_teacher_verifications(user):
    """Site-wide teacher applications: Django superusers."""
    if not user or not user.is_authenticated:
        return False
    return user.is_superuser


def instructor_verifications_for_reviewer(profile, user=None):
    """Verification requests this reviewer may see/act on.

    - Site admins: all teacher + class-instructor requests.
    - Course admins: class-instructor requests for courses they administer only.
    """
    qs = InstructorVerificationRequest.objects.select_related(
        'user__user', 'reviewed_by__user', 'organization', 'request_class',
    )
    if user_can_review_site_teacher_verifications(user):
        return qs
    if profile is None:
        return InstructorVerificationRequest.objects.none()
    admin_org_ids = set(Organization.objects.filter(admins=profile).values_list('id', flat=True))
    if not admin_org_ids:
        return InstructorVerificationRequest.objects.none()
    return qs.filter(
        kind=InstructorVerificationRequest.KIND_CLASS_INSTRUCTOR,
        organization_id__in=admin_org_ids,
    )


def user_can_review_instructor_verifications(user, profile=None):
    """Who may open the teacher/class-instructor review inbox."""
    if user_can_review_site_teacher_verifications(user):
        return True
    if not user or not user.is_authenticated:
        return False
    if profile is None:
        profile = getattr(user, 'profile', None)
    return bool(profile and Organization.objects.filter(admins=profile).exists())


def instructor_apply_catalog():
    """Courses and active classes for the Apply-as-instructor modal cascade selects."""
    catalog = []
    orgs = Organization.objects.prefetch_related('classes').order_by('short_name', 'name')
    for org in orgs:
        classes = []
        for klass in org.classes.all():
            if not klass.is_active:
                continue
            session = klass.offering_label or klass.name
            classes.append({
                'id': klass.id,
                'name': klass.name,
                'label': session,
            })
        classes.sort(key=lambda row: (row['label'] or '').lower())
        catalog.append({
            'id': org.id,
            'short_name': org.short_name or org.name,
            'name': org.name,
            'classes': classes,
        })
    return catalog


def course_inbox_chrome_context(request):
    """Shared Course tab chrome: unified Join requests badge (+ Apply as teacher)."""
    profile = request.profile if request.user.is_authenticated else None
    can_all = user_can_edit_all_organizations(request.user)
    reviewable = orgs_reviewable_by(profile) if profile is not None else Organization.objects.none()
    can_review_any = bool(profile is not None and (reviewable.exists() or can_all))
    pending_join_total = pending_requests_for_reviewer(
        profile, request.user,
    ).count() if can_review_any else 0
    can_review_instructors = user_can_review_instructor_verifications(request.user, profile)
    pending_instructor_total = (
        instructor_verifications_for_reviewer(profile, request.user).filter(state='P').count()
        if can_review_instructors else 0
    )
    can_review_inbox = can_review_any or can_review_instructors
    pending_review_total = pending_join_total + pending_instructor_total
    is_site_teacher = profile_is_site_teacher(profile, request.user)
    pending_teacher_apply = False
    if profile is not None and not is_site_teacher:
        pending_teacher_apply = InstructorVerificationRequest.objects.filter(
            user=profile, kind=InstructorVerificationRequest.KIND_TEACHER, state='P',
        ).exists()
    admin_email = settings.ADMINS[0][1] if settings.ADMINS else ''
    return {
        'can_review_any': can_review_inbox,
        'can_review_joins': can_review_any,
        'can_review_instructors': can_review_instructors,
        'pending_review_total': pending_review_total,
        'pending_join_total': pending_join_total,
        'pending_instructor_total': pending_instructor_total,
        'can_see_notifications': False,
        'unread_notification_total': 0,
        'can_add_course': request.user.is_authenticated and (
            request.user.is_superuser or request.user.has_perm('judge.add_organization')
        ),
        'is_site_teacher': is_site_teacher,
        'can_apply_teacher': request.user.is_authenticated and not is_site_teacher and not pending_teacher_apply,
        'pending_teacher_apply': pending_teacher_apply,
        'admin_email': admin_email or getattr(settings, 'SITE_ADMIN_EMAIL', '') or 'admin@example.com',
        'instructor_apply_catalog': instructor_apply_catalog() if request.user.is_authenticated else [],
    }


def grant_site_teacher_role(profile):
    """Add profile to Instructors group (+ staff if needed). Returns (granted_group, granted_staff)."""
    if profile is None:
        return False, False
    user = profile.user
    if user is None:
        return False, False
    group = ensure_instructors_group()
    already = user.groups.filter(id=group.id).exists()
    if not already:
        user.groups.add(group)
    granted_staff = False
    if not user.is_staff:
        user.is_staff = True
        user.save(update_fields=['is_staff'])
        granted_staff = True
    return (not already), granted_staff


def revoke_site_teacher_role(profile, *, excluding_verification=None):
    """Remove Instructors group / staff only when this approval recorded those grants."""
    if profile is None:
        return
    verification = excluding_verification
    user = profile.user
    if user is None:
        return

    if verification is None or verification.granted_instructors_group:
        others = InstructorVerificationRequest.objects.filter(
            user=profile, state='A', kind=InstructorVerificationRequest.KIND_TEACHER,
            granted_instructors_group=True,
        )
        if verification is not None:
            others = others.exclude(pk=verification.pk)
        if not others.exists():
            group = ensure_instructors_group()
            user.groups.remove(group)

    if verification is not None and verification.granted_staff:
        others = InstructorVerificationRequest.objects.filter(
            user=profile, state='A', granted_staff=True,
        ).exclude(pk=verification.pk)
        if not others.exists() and user.is_staff and not user.is_superuser:
            user.is_staff = False
            user.save(update_fields=['is_staff'])


def grant_class_instructor_role(profile, class_obj):
    """Make profile an instructor for one class (class admin + course member).

    Returns (granted_class_admin, granted_staff) describing what this call changed.
    Does NOT grant site-wide Instructors group (use grant_site_teacher_role for that),
    and never sets ``is_staff`` (CA-11): class rights come from ``Class.admins``, and
    staff status opens the Django admin. ``granted_staff`` is therefore always False.
    """
    if profile is None or class_obj is None:
        return False, False
    already_admin = class_obj.admins.filter(id=profile.id).exists()
    class_obj.admins.add(profile)
    class_obj.organization.members.add(profile)
    class_obj.members.add(profile)
    granted_class_admin = not already_admin
    return granted_class_admin, False


def promote_site_teacher_on_class_join(profile, class_obj):
    """No longer auto-grants class instructor.

    Site teachers who join a course can open all classes read-only; editing a
    class requires Apply as class instructor (or creating the class).
    Still ensures they appear on Organization.instructors.
    """
    if profile is None or class_obj is None:
        return False
    return maybe_add_course_instructor(profile, class_obj.organization)


def apply_approved_join_request(obj):
    """Grant course/class membership after an OrganizationRequest is approved.

    Site teachers who picked a class are added as class instructors (co-teaching).
    Students are added as class members only.
    """
    if obj.grants_organization or obj.request_class_id:
        obj.user.organizations.add(obj.organization)
        maybe_add_course_instructor(obj.user, obj.organization)
    if obj.request_class:
        if profile_is_site_teacher(obj.user, getattr(obj.user, 'user', None)):
            grant_class_instructor_role(obj.user, obj.request_class)
            maybe_add_course_instructor(obj.user, obj.organization)
        else:
            obj.user.classes.add(obj.request_class)
            promote_site_teacher_on_class_join(obj.user, obj.request_class)
    cache.delete(make_template_fragment_key('org_member_count', (obj.organization_id,)))


def maybe_add_course_instructor(profile, org):
    """Add site teachers to Organization.instructors when they join a course."""
    if profile is None or org is None:
        return False
    user = profile.user
    if not profile_is_site_teacher(profile, user):
        return False
    org.instructors.add(profile)
    return True


def revoke_class_instructor_role(profile, class_obj, *, excluding_verification=None):
    """Undo privileges this verification granted (keep course membership).

    - class.admins: removed when this approval granted it, or for legacy approvals
      that predate the flag (only if no other approved request still covers the class).
    - is_staff: cleared only when this approval recorded granted_staff and no other
      still-approved request retains that grant (never strips unrelated staff).
    """
    if profile is None or class_obj is None:
        return
    verification = excluding_verification

    remove_admin = False
    if verification is None:
        remove_admin = True
    elif verification.granted_class_admin:
        remove_admin = True
    else:
        # Legacy approval without the flag: remove only if nothing else still covers
        # this class as an approved instructor request.
        remove_admin = not InstructorVerificationRequest.objects.filter(
            user=profile, state='A', request_class=class_obj,
            kind=InstructorVerificationRequest.KIND_CLASS_INSTRUCTOR,
        ).exclude(pk=verification.pk).exists()
    if remove_admin:
        class_obj.admins.remove(profile)

    if verification is not None and verification.granted_staff:
        others = InstructorVerificationRequest.objects.filter(
            user=profile, state='A', granted_staff=True,
        ).exclude(pk=verification.pk)
        if not others.exists():
            user = profile.user
            if user is not None and user.is_staff and not user.is_superuser:
                user.is_staff = False
                user.save(update_fields=['is_staff'])


def apply_instructor_verification_action(verification, action, reviewer):
    """approve / reject / reopen teacher or class-instructor requests. Returns True if changed."""
    kind = verification.kind or InstructorVerificationRequest.KIND_CLASS_INSTRUCTOR

    if action == 'approve':
        if verification.state not in ('P', 'R'):
            return False
        if kind == InstructorVerificationRequest.KIND_TEACHER:
            granted_group, granted_staff = grant_site_teacher_role(verification.user)
            verification.state = 'A'
            verification.reviewed_by = reviewer
            verification.reviewed_time = timezone.now()
            verification.granted_instructors_group = granted_group
            verification.granted_staff = granted_staff
            verification.granted_class_admin = False
            verification.save()
        else:
            if verification.request_class_id is None:
                return False
            # CA-11: only approved site teachers can become class instructors.
            if not profile_is_site_teacher(verification.user):
                return False
            granted_class_admin, granted_staff = grant_class_instructor_role(
                verification.user, verification.request_class,
            )
            verification.state = 'A'
            verification.reviewed_by = reviewer
            verification.reviewed_time = timezone.now()
            verification.granted_class_admin = granted_class_admin
            verification.granted_staff = granted_staff
            verification.granted_instructors_group = False
            verification.save()
        AdminNotification.objects.filter(
            instructor_verification=verification, read_at__isnull=True,
        ).update(read_at=timezone.now())
        return True

    if action == 'reject':
        if verification.state not in ('P', 'A'):
            return False
        if verification.state == 'A':
            if kind == InstructorVerificationRequest.KIND_TEACHER:
                revoke_site_teacher_role(
                    verification.user, excluding_verification=verification,
                )
            else:
                revoke_class_instructor_role(
                    verification.user, verification.request_class,
                    excluding_verification=verification,
                )
        verification.state = 'R'
        verification.reviewed_by = reviewer
        verification.reviewed_time = timezone.now()
        verification.granted_class_admin = False
        verification.granted_staff = False
        verification.granted_instructors_group = False
        verification.save()
        AdminNotification.objects.filter(
            instructor_verification=verification, read_at__isnull=True,
        ).update(read_at=timezone.now())
        return True

    if action == 'reopen':
        if verification.state not in ('A', 'R'):
            return False
        if verification.state == 'A':
            if kind == InstructorVerificationRequest.KIND_TEACHER:
                revoke_site_teacher_role(
                    verification.user, excluding_verification=verification,
                )
            else:
                revoke_class_instructor_role(
                    verification.user, verification.request_class,
                    excluding_verification=verification,
                )
        verification.state = 'P'
        verification.reviewed_by = reviewer
        verification.reviewed_time = timezone.now()
        verification.granted_class_admin = False
        verification.granted_staff = False
        verification.granted_instructors_group = False
        verification.save()
        return True
    return False


def parse_class_description_meta(description):
    """Parse structured meta lines from a class description (Session/Location/…)."""
    meta = {
        'session_label': '',
        'section_label': '',
        'location_line': '',
        'schedule_line': '',
        'dates_line': '',
        'credits_line': '',
        'materials_line': '',
        'material_items': [],
        'desc_lines': [],
    }
    if not description:
        return meta
    in_materials = False
    for raw in description.splitlines():
        line = raw.strip().lstrip('#').strip()
        if not line:
            in_materials = False
            continue
        low = line.lower()
        if low.startswith('session:') or low.startswith('term:'):
            meta['session_label'] = line.split(':', 1)[1].strip() or line
            in_materials = False
        elif low.startswith('section:'):
            meta['section_label'] = line.split(':', 1)[1].strip() or line
            in_materials = False
        elif low.startswith('location:') or low.startswith('room:') or low.startswith('canvas:'):
            meta['location_line'] = line.split(':', 1)[1].strip() or line
            in_materials = False
        elif low.startswith('schedule:'):
            meta['schedule_line'] = line.split(':', 1)[1].strip() or line
            in_materials = False
        elif low.startswith('dates:'):
            # Prefer dedicated class-dates; also fill schedule when empty (card/hero).
            value = line.split(':', 1)[1].strip() or line
            meta['dates_line'] = value
            if not meta['schedule_line']:
                meta['schedule_line'] = value
            in_materials = False
        elif low.startswith('credits:'):
            meta['credits_line'] = line.split(':', 1)[1].strip() or line
            in_materials = False
        elif low.startswith('materials:') or low.startswith('content:'):
            value = line.split(':', 1)[1].strip()
            meta['materials_line'] = value or line
            if value:
                meta['material_items'].append(value)
            in_materials = True
        elif in_materials and (line.startswith('- ') or line.startswith('* ')):
            item = line[2:].strip()
            if item:
                meta['material_items'].append(item)
        else:
            in_materials = False
            meta['desc_lines'].append(line)
    return meta


def class_session_sort_key(cls, session_label=None):
    """Build a descending-friendly sort key (newest session first)."""
    year = 0
    if cls.academic_year:
        match = re.search(r'(\d{4})', cls.academic_year)
        if match:
            year = int(match.group(1))
    term = (cls.term_session or session_label or '').lower()
    session_num = 0
    sm = re.search(r'session\s*(\d+)', term)
    if sm:
        session_num = int(sm.group(1))
    if 'spring' in term:
        term_rank = 2
    elif 'fall' in term:
        term_rank = 1
    else:
        term_rank = 0
    start = cls.start_date.toordinal() if cls.start_date else 0
    return '%04d%d%02d%08d' % (year, term_rank, session_num, start)


def contest_assignment_status(contest):
    """Return (status_key, status_label, timing_label) for class assignment rows."""
    if not contest.started:
        return 'scheduled', _('Scheduled'), _('Opens %(time)s') % {
            'time': contest.start_time.strftime('%Y/%m/%d %H:%M:%S'),
        }
    if contest.ended:
        return 'released', _('Released'), _('Ended %(time)s') % {
            'time': contest.end_time.strftime('%Y/%m/%d %H:%M:%S'),
        }
    return 'open', _('Open'), _('Due %(time)s') % {
        'time': contest.end_time.strftime('%Y/%m/%d %H:%M:%S'),
    }


class OrganizationMixin(object):
    context_object_name = 'organization'
    model = Organization

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['logo_override_image'] = self.object.logo_override_image
        return context

    def dispatch(self, request, *args, **kwargs):
        try:
            return super(OrganizationMixin, self).dispatch(request, *args, **kwargs)
        except Http404:
            key = kwargs.get(self.slug_url_kwarg, None)
            if key:
                return generic_message(request, _('No such organization'),
                                       _('Could not find an organization with the key "%s".') % key)
            else:
                return generic_message(request, _('No such organization'),
                                       _('Could not find such organization.'))

    def can_edit_organization(self, org=None):
        """Can edit course settings: course administrator or site-wide editor."""
        if org is None:
            org = self.object
        if not self.request.user.is_authenticated:
            return False
        if user_can_edit_all_organizations(self.request.user):
            return True
        return profile_is_org_admin(self.request.profile, org)

    def can_create_class(self, org=None):
        """Course admins, or site teachers who are members of this course."""
        if org is None:
            org = self.object
        if not self.request.user.is_authenticated:
            return False
        return profile_can_create_class(self.request.profile, org, self.request.user)

    def can_manage_organization(self, org=None):
        """Course-level manage (same scope as course administrators, not class instructors)."""
        if org is None:
            org = self.object
        if not self.request.user.is_authenticated:
            return False
        return profile_manages_organization(self.request.profile, org, self.request.user)


class BaseOrganizationListView(OrganizationMixin, ListView):
    model = None
    context_object_name = None
    slug_url_kwarg = 'slug'

    def get_object(self):
        return get_object_or_404(Organization, id=self.kwargs.get('pk'))

    def get_context_data(self, **kwargs):
        return super().get_context_data(organization=self.object, **kwargs)

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return super().get(request, *args, **kwargs)


class OrganizationDetailView(OrganizationMixin, DetailView):
    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.slug != kwargs['slug']:
            return HttpResponsePermanentRedirect(reverse(
                request.resolver_match.url_name, args=(self.object.id, self.object.slug)))
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)


class OrganizationList(TitleMixin, ListView):
    model = Organization
    context_object_name = 'organizations'
    template_name = 'organization/list.html'
    title = gettext_lazy('Courses')

    def get_queryset(self):
        qs = Organization.objects.annotate(
            member_count=Count('member', distinct=True),
            class_count=Count('class', distinct=True),
            admin_count=Count('admins', distinct=True),
        )
        # Filtering / sorting is applied client-side for live updates; keep a stable default order.
        return qs.order_by('short_name', 'name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        admin_email = ''
        if settings.ADMINS:
            admin_email = settings.ADMINS[0][1]
        context['admin_email'] = admin_email or getattr(settings, 'SITE_ADMIN_EMAIL', '') or 'admin@example.com'
        context['filter_q'] = (self.request.GET.get('q') or '').strip()
        context['filter_joined'] = self.request.GET.get('joined') or ''
        context['filter_join_mode'] = self.request.GET.get('join_mode') or ''
        context['filter_sort'] = self.request.GET.get('sort') or 'short_name'
        profile = self.request.profile if self.request.user.is_authenticated else None
        pending_ids = set()
        joined_ids = set()
        pending_review_total = 0
        can_review_any = False
        org_pending_counts = {}
        manage_ids = set()
        can_all = user_can_edit_all_organizations(self.request.user)
        if profile is not None:
            joined_ids = set(profile.organizations.values_list('id', flat=True))
            pending_ids = set(
                OrganizationRequest.objects.filter(user=profile, state='P')
                .values_list('organization_id', flat=True),
            )
            reviewable = orgs_reviewable_by(profile)
            can_review_any = reviewable.exists() or can_all
            if can_review_any:
                pending_qs = pending_requests_for_reviewer(profile, self.request.user)
                pending_review_total = pending_qs.count()
                for row in pending_qs.values('organization_id').annotate(c=Count('id')):
                    org_pending_counts[row['organization_id']] = row['c']
            if can_all:
                manage_ids = set(Organization.objects.values_list('id', flat=True))
            else:
                # Course administrators only — class instructors open the course as members.
                manage_ids = set(orgs_administered_by(profile).values_list('id', flat=True))

        # Always ship the full course set; live filtering/sorting runs in the page JS.
        orgs = list(context['organizations'])
        editable_ids = set()
        if profile is not None:
            if can_all:
                editable_ids = {o.id for o in orgs}
            else:
                editable_ids = set(
                    Organization.admins.through.objects.filter(profile=profile)
                    .values_list('organization_id', flat=True),
                )
        # Prefetch instructors for card display (display names, not course admins).
        for org in orgs:
            org.user_joined = org.id in joined_ids
            org.user_pending = org.id in pending_ids and not org.user_joined
            org.review_pending_count = org_pending_counts.get(org.id, 0)
            org.can_edit = org.id in editable_ids or can_all
            org.can_manage = org.id in manage_ids
            names = org.instructor_name_list
            org.instructor_line = ', '.join(names) if names else _('TBA')
            org.instructor_count = len(names)
        classes_by_org = {}
        org_ids = [o.id for o in orgs]
        for cls in Class.objects.filter(organization_id__in=org_ids, is_active=True).order_by('name') \
                .only('id', 'name', 'organization_id'):
            classes_by_org.setdefault(cls.organization_id, []).append({'id': cls.id, 'name': cls.name})
        context['organizations'] = orgs
        context['join_classes_script'] = json_script(
            {str(oid): classes_by_org.get(oid, []) for oid in org_ids},
            'join-classes-json',
        )
        context.update(course_inbox_chrome_context(self.request))
        # Keep list-page review totals (already computed above) if chrome helper differs.
        context['can_review_any'] = can_review_any
        context['pending_review_total'] = pending_review_total
        return context


class OrganizationHome(OrganizationDetailView):
    template_name = 'organization/home.html'

    def get_context_data(self, **kwargs):
        context = super(OrganizationHome, self).get_context_data(**kwargs)
        context['title'] = self.object.name
        profile = self.request.profile if self.request.user.is_authenticated else None
        can_all = user_can_edit_all_organizations(self.request.user)
        context['can_edit'] = self.can_edit_organization()
        context['can_manage'] = self.can_manage_organization()
        context['can_create_class'] = self.can_create_class()
        context['can_view_course'] = profile_can_view_course(profile, self.object, self.request.user)
        # Subject registry (judge/subjects/): has_feature() in templates answers for this course.
        context['subject_course'] = self.object
        context['subject_is_teacher'] = bool(can_all or (
            profile is not None and self.object.profile_teaches_course(profile)))
        context['can_review_requests'] = not self.object.is_open and self.request.user.is_authenticated and (
            self.object.can_review_all_requests(self.request.profile) or
            self.object.can_review_class_requests(self.request.profile) or
            can_all
        )
        context['user_pending'] = False
        context['review_pending_count'] = 0
        if profile is not None:
            context['user_pending'] = (
                profile not in self.object and
                self.object.requests.filter(user=profile, state='P').exists()
            )
            if context['can_review_requests']:
                context['review_pending_count'] = pending_requests_for_reviewer(
                    profile, self.request.user, organization=self.object,
                ).count()

        context.update(course_inbox_chrome_context(self.request))
        # Per-course badge on the detail page link.
        context['org_pending_review_count'] = context['review_pending_count']
        context['can_request_from_list'] = bool(
            profile is not None and
            not context['can_manage'] and
            profile not in self.object and
            not context['user_pending'] and
            not self.object.is_open,
        )
        active_classes = self.object.classes.filter(is_active=True)

        classes = active_classes.prefetch_related('admins__user', 'members')
        if profile is not None:
            classes = classes.annotate(joined=Subquery(
                profile.classes.filter(id=OuterRef('id')).values('id'),
            )).order_by('-joined', 'name')
            pending_class_ids = set(
                self.object.requests.filter(user=profile, state='P', request_class__isnull=False)
                .values_list('request_class_id', flat=True),
            )
        else:
            classes = classes.annotate(joined=Value(0, output_field=IntegerField()))
            pending_class_ids = set()
        class_list = list(classes)
        instructors = {}
        session_options = {}
        for cls in class_list:
            cls.user_pending = cls.id in pending_class_ids and not cls.joined
            instructor_names = cls.instructor_name_list
            if not instructor_names:
                instructor_names = self.object.instructor_name_list
            if instructor_names:
                # Filter key is the primary instructor display name (first listed).
                cls.instructor_id = instructor_names[0]
                cls.instructor_name = ', '.join(instructor_names)
                for name in instructor_names:
                    instructors[name] = name
            else:
                cls.instructor_id = ''
                cls.instructor_name = _('TBA')

            meta = parse_class_description_meta(cls.description)
            session_label = cls.term_session or meta['session_label']
            section_label = meta['section_label']
            location_line = cls.location or meta['location_line']
            schedule_line = cls.schedule_label or meta['schedule_line']
            materials_line = meta['materials_line']
            desc_lines = meta['desc_lines']
            # Infer session only from distinct metadata-ish lines, never from class.name alone.
            if not session_label:
                for line in desc_lines:
                    low = line.lower()
                    if 'section' in low:
                        continue
                    if 'spring' in low or 'fall' in low or 'session' in low or '·' in line:
                        session_label = line
                        break
            cleaned = []
            name_l = (cls.name or '').lower()
            session_l = (session_label or '').lower()
            for line in desc_lines:
                low = line.lower().strip()
                if not low or low == name_l or low == session_l:
                    continue
                if 'section' in low and ('spring' in low or 'fall' in low or '·' in line or 'session' in low):
                    continue
                if session_l and session_l in low and len(low) <= len(session_l) + 24:
                    continue
                cleaned.append(line)
            summary = plain_text_summary(' '.join(cleaned[:3]))
            if session_label and summary.lower().startswith(session_label.lower()):
                summary = summary[len(session_label):].strip(' ·-–—')
            if not summary:
                summary = _('Assignments, materials and students are organized inside this class.')

            if not section_label:
                name = cls.name or ''
                section_label = name

            if cls.academic_year and session_label:
                cls.session_label = '%s · %s' % (cls.academic_year, session_label)
            else:
                cls.session_label = session_label or cls.academic_year
            cls.session_sort_key = class_session_sort_key(cls, session_label)
            cls.section_label = section_label
            cls.session_key = session_label or cls.academic_year or 'none'
            cls.location_line = location_line
            cls.schedule_line = schedule_line
            cls.description_summary = summary
            cls.card_title = cls.session_label or (cls.name or _('Untitled class'))
            mat_count = ClassMaterial.objects.filter(class_obj=cls, is_folder=False).count()
            if mat_count:
                cls.materials_line = ngettext('%d material', '%d materials', mat_count) % mat_count
            elif materials_line:
                cls.materials_line = materials_line
            elif summary and summary != _('Assignments, materials and students are organized inside this class.'):
                cls.materials_line = summary
            else:
                cls.materials_line = _('Assignments & materials inside')
            cls.member_count = cls.members.count()
            org_slots = self.object.slots
            cls.capacity_display = org_slots if org_slots else '—'
            cls.user_can_edit = bool(
                context['can_edit'] or
                (profile is not None and cls.admins.filter(id=profile.id).exists()),
            )
            # Site teachers in the course may open every class (read-only).
            # Students only open classes they joined; edit still needs class admin.
            teacher_viewer = bool(
                profile is not None and
                profile_is_site_teacher(profile, self.request.user) and
                profile_is_org_member(profile, self.object),
            )
            cls.user_can_open = bool(cls.user_can_edit or cls.joined or teacher_viewer)
            cls.review_pending_count = 0
            if cls.session_label:
                session_options[cls.session_label] = cls.session_sort_key
        context['instructor_apply_org_id'] = self.object.id
        if context['can_review_requests']:
            class_pending_map = {}
            for row in pending_requests_for_reviewer(
                profile, self.request.user, organization=self.object,
            ).filter(request_class__isnull=False).values('request_class_id').annotate(c=Count('id')):
                class_pending_map[row['request_class_id']] = row['c']
            for cls in class_list:
                cls.review_pending_count = class_pending_map.get(cls.id, 0)
        context['classes'] = class_list
        context['class_instructors'] = [
            {'id': str(tid), 'username': uname} for tid, uname in sorted(instructors.items(), key=lambda x: x[1])
        ]
        context['class_sessions'] = [
            label for label, _ in sorted(session_options.items(), key=lambda item: item[1], reverse=True)
        ]
        return context


class OrganizationUsers(QueryStringSortMixin, DiggPaginatorMixin, BaseOrganizationListView):
    template_name = 'organization/users.html'
    all_sorts = frozenset(('problem_count', 'rating', 'performance_points'))
    default_desc = all_sorts
    default_sort = '-performance_points'
    paginate_by = 100
    context_object_name = 'users'

    def get_queryset(self):
        return self.object.members.filter(is_unlisted=False).order_by(self.order).select_related('user') \
            .defer('about', 'user_script', 'notes')

    def get_context_data(self, **kwargs):
        context = super(OrganizationUsers, self).get_context_data(**kwargs)
        context['title'] = _('%s Members') % self.object.name
        context['users'] = ranker(context['users'])
        context['partial'] = True
        context['is_admin'] = self.can_edit_organization()
        context['kick_url'] = reverse('organization_user_kick', args=[self.object.id, self.object.slug])
        context['first_page_href'] = '.'
        context.update(self.get_sort_context())
        context.update(self.get_sort_paginate_context())
        return context


class OrganizationMembershipChange(LoginRequiredMixin, OrganizationMixin, SingleObjectMixin, View):
    def post(self, request, *args, **kwargs):
        org = self.get_object()
        response = self.handle(request, org, request.profile)
        if response is not None:
            return response
        return HttpResponseRedirect(org.get_absolute_url())

    def handle(self, request, org, profile):
        raise NotImplementedError()


class JoinOrganization(OrganizationMembershipChange):
    def handle(self, request, org, profile):
        if profile.organizations.filter(id=org.id).exists():
            return generic_message(request, _('Joining organization'), _('You are already in the organization.'))

        if not org.is_open:
            return generic_message(request, _('Joining organization'), _('This organization is not open.'))

        if org.slots is not None and org.members.count() >= org.slots:
            return generic_message(request, _('Joining organization'),
                                   _('This course has no remaining slots.'))

        max_orgs = settings.DMOJ_USER_MAX_ORGANIZATION_COUNT
        if profile.organizations.filter(is_open=True).count() >= max_orgs:
            return generic_message(
                request, _('Joining organization'),
                ngettext('You may not be part of more than {count} public organization.',
                         'You may not be part of more than {count} public organizations.',
                         max_orgs).format(count=max_orgs),
            )

        profile.organizations.add(org)
        profile.save()
        maybe_add_course_instructor(profile, org)
        cache.delete(make_template_fragment_key('org_member_count', (org.id,)))


class LeaveOrganization(OrganizationMembershipChange):
    def handle(self, request, org, profile):
        if not profile.organizations.filter(id=org.id).exists():
            return generic_message(request, _('Leaving organization'), _('You are not in "%s".') % org.short_name)
        remove_profile_from_organization(profile, org)


class OrganizationRequestForm(Form):
    class_ = forms.ModelChoiceField(Class.objects.all(), required=False)
    reason = forms.CharField(widget=forms.Textarea, required=False)

    def __init__(self, *args, class_required: bool, class_queryset, for_teacher: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.for_teacher = for_teacher
        has_classes = bool(class_queryset)
        if for_teacher:
            self.fields['class_'].required = False
            self.fields['reason'].required = False
            self.show_classes = has_classes
            self.fields['class_'].empty_label = _('None — course only (not co-teaching)')
            self.fields['reason'].widget.attrs['placeholder'] = _(
                'Optional. Leave class empty unless you will co-teach that section.',
            )
        else:
            self.fields['class_'].required = class_required or has_classes
            self.fields['reason'].required = True
            self.show_classes = class_required or has_classes
            self.fields['class_'].empty_label = _('Select a class…')
        self.fields['class_'].queryset = class_queryset
        self.fields['class_'].label_from_instance = attrgetter('name')
        self.fields['class_'].widget.attrs.update({'class': 'oj-select'})
        self.fields['reason'].widget.attrs.update({'rows': 5, 'class': 'oj-input'})


class RequestJoinOrganization(LoginRequiredMixin, SingleObjectMixin, FormView):
    model = Organization
    slug_field = 'key'
    slug_url_kwarg = 'key'
    template_name = 'organization/requests/request.html'
    form_class = OrganizationRequestForm

    def dispatch(self, request, *args, **kwargs):
        # The checks below need a profile, so send visitors to log in first (LoginRequiredMixin
        # only runs in super().dispatch).
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.object = self.get_object()
        profile = self.request.profile
        if profile_manages_organization(profile, self.object, request.user):
            return generic_message(self.request, _("Can't request to join %s") % self.object.name,
                                   _('You already manage this course and do not need to request membership.'))
        if profile in self.object:
            return generic_message(self.request, _("Can't request to join %s") % self.object.name,
                                   _('You are already a member of %s.') % self.object.name)
        if self.object.requests.filter(user=profile, state='P').exists():
            return generic_message(self.request, _("Can't request to join %s") % self.object.name,
                                   _('You already have a pending request to join %s.') % self.object.name)
        if self.object.is_open:
            raise Http404()
        is_teacher = profile_is_site_teacher(profile, request.user)
        if (not is_teacher and self.object.class_required and
                not self.object.classes.filter(is_active=True).exists()):
            return generic_message(self.request, _("Can't request to join %s") % self.object.name,
                                   _('This course requires a class, but no classes are available yet.'))
        return super(RequestJoinOrganization, self).dispatch(request, *args, **kwargs)

    def get_form_kwargs(self) -> dict:
        kwargs = super().get_form_kwargs()
        is_teacher = profile_is_site_teacher(self.request.profile, self.request.user)
        kwargs['for_teacher'] = is_teacher
        kwargs['class_required'] = False if is_teacher else self.object.class_required
        kwargs['class_queryset'] = self.object.classes.filter(is_active=True)
        return kwargs

    def get_context_data(self, **kwargs):
        context = super(RequestJoinOrganization, self).get_context_data(**kwargs)
        context['title'] = _('Request to join %s') % self.object.name
        return context

    def form_valid(self, form):
        request = OrganizationRequest()
        request.organization = self.get_object()
        request.user = self.request.profile
        reason = (form.cleaned_data.get('reason') or '').strip()
        if not reason and getattr(form, 'for_teacher', False):
            reason = _('Course instructor')
        request.reason = reason
        request.request_class = form.cleaned_data.get('class_')
        request.state = 'P'
        request.grants_organization = True
        request.save()
        notify_course_join(request)
        messages.success(self.request, _('Your join request has been submitted.'))
        next_url = self.request.POST.get('next') or self.request.GET.get('next')
        return HttpResponseRedirect(safe_redirect_url(
            self.request, next_url, request.organization.get_absolute_url(),
        ))

    def form_invalid(self, form):
        next_url = self.request.POST.get('next') or self.request.GET.get('next')
        if next_url:
            messages.error(self.request, _('Please complete the membership request.'))
            return HttpResponseRedirect(safe_redirect_url(
                self.request, next_url, self.object.get_absolute_url(),
            ))
        return super().form_invalid(form)


class OrganizationRequestDetail(LoginRequiredMixin, TitleMixin, DetailView):
    model = OrganizationRequest
    template_name = 'organization/requests/detail.html'
    title = gettext_lazy('Join request detail')
    pk_url_kwarg = 'rpk'

    def get_object(self, queryset=None):
        object = super(OrganizationRequestDetail, self).get_object(queryset)
        profile = self.request.profile
        if user_can_edit_all_organizations(self.request.user):
            return object
        if object.user_id != profile.id and not object.organization.admins.filter(id=profile.id).exists() and (
                not object.request_class or not object.request_class.admins.filter(id=profile.id).exists()):
            raise PermissionDenied()
        return object


OrganizationRequestFormSet = modelformset_factory(OrganizationRequest, extra=0, fields=('state',), can_delete=False)


class OrganizationRequestBaseView(LoginRequiredMixin, SingleObjectTemplateResponseMixin, SingleObjectMixin, View):
    model = Organization
    slug_field = 'key'
    slug_url_kwarg = 'key'
    tab = None

    def get_object(self, queryset=None):
        organization = super(OrganizationRequestBaseView, self).get_object(queryset)
        if user_can_edit_all_organizations(self.request.user):
            self.edit_all = True
        elif organization.can_review_all_requests(self.request.profile):
            self.edit_all = True
        elif organization.can_review_class_requests(self.request.profile):
            self.edit_all = False
        else:
            raise PermissionDenied()
        return organization

    def get_requests(self):
        queryset = self.object.requests.select_related('user__user').defer(
            'user__about', 'user__notes', 'user__user_script',
        )
        if not self.edit_all:
            queryset = queryset.filter(request_class__in=self.object.classes.filter(admins__id=self.request.profile.id))
        return queryset

    def get_context_data(self, **kwargs):
        context = super(OrganizationRequestBaseView, self).get_context_data(**kwargs)
        context['title'] = _('Managing join requests for %s') % self.object.name
        context['tab'] = self.tab
        return context


class OrganizationRequestView(OrganizationRequestBaseView):
    template_name = 'organization/requests/pending.html'
    tab = 'pending'

    def get_context_data(self, **kwargs):
        context = super(OrganizationRequestView, self).get_context_data(**kwargs)
        context['formset'] = self.formset
        return context

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.formset = OrganizationRequestFormSet(queryset=self.get_requests())
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def get_requests(self):
        return super().get_requests().filter(state='P')

    def post(self, request, *args, **kwargs):
        self.object = organization = self.get_object()
        self.formset = formset = OrganizationRequestFormSet(request.POST, request.FILES, queryset=self.get_requests())
        if formset.is_valid():
            if organization.slots is not None:
                deleted_set = set(formset.deleted_forms)
                to_approve = sum(form.cleaned_data['state'] == 'A' for form in formset.forms if form not in deleted_set)
                can_add = organization.slots - organization.members.count()
                if to_approve > can_add:
                    msg1 = ngettext('Your organization can only receive %d more member.',
                                    'Your organization can only receive %d more members.', can_add) % can_add
                    msg2 = ngettext('You cannot approve %d user.',
                                    'You cannot approve %d users.', to_approve) % to_approve
                    messages.error(request, msg1 + '\n' + msg2)
                    return self.render_to_response(self.get_context_data(object=organization))

            approved, rejected = 0, 0
            for obj in formset.save():
                if obj.state == 'A':
                    apply_approved_join_request(obj)
                    approved += 1
                elif obj.state == 'R':
                    rejected += 1
            messages.success(request,
                             ngettext('Approved %d user.', 'Approved %d users.', approved) % approved + '\n' +
                             ngettext('Rejected %d user.', 'Rejected %d users.', rejected) % rejected)
            cache.delete(make_template_fragment_key('org_member_count', (organization.id,)))
            return HttpResponseRedirect(request.get_full_path())
        return self.render_to_response(self.get_context_data(object=organization))

    put = post


class OrganizationRequestLog(OrganizationRequestBaseView):
    states = ('A', 'R')
    tab = 'log'
    template_name = 'organization/requests/log.html'

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super(OrganizationRequestLog, self).get_context_data(**kwargs)
        context['requests'] = self.get_requests().filter(state__in=self.states)
        return context


class InstructorJoinRequestsView(LoginRequiredMixin, TitleMixin, View):
    """Unified inbox: course/class joins + teacher / class-instructor applications."""

    template_name = 'organization/requests/inbox.html'
    title = gettext_lazy('Join requests')

    def _reviewable_queryset(self, profile):
        org_admin_ids = set(Organization.objects.filter(admins=profile).values_list('id', flat=True))
        class_admin_ids = set(Class.objects.filter(admins=profile).values_list('id', flat=True))
        return OrganizationRequest.objects.filter(
            Q(organization_id__in=org_admin_ids) | Q(request_class_id__in=class_admin_ids),
        ).select_related('user__user', 'organization', 'request_class').order_by('-time')

    def _can_review_all(self, request):
        return request.user.has_perm('judge.edit_all_organization') or request.user.is_superuser

    def _can_review_joins(self, request):
        if self._can_review_all(request):
            return True
        return orgs_reviewable_by(request.profile).exists()

    def _can_review_verifications(self, request):
        return user_can_review_instructor_verifications(request.user, request.profile)

    def _can_access_inbox(self, request):
        return self._can_review_joins(request) or self._can_review_verifications(request)

    def _join_queryset(self, request):
        if not self._can_review_joins(request):
            return OrganizationRequest.objects.none()
        qs = self._reviewable_queryset(request.profile)
        if self._can_review_all(request):
            qs = OrganizationRequest.objects.select_related(
                'user__user', 'organization', 'request_class',
            ).order_by('-time')
        return qs

    def _verification_queryset(self, request):
        if not self._can_review_verifications(request):
            return InstructorVerificationRequest.objects.none()
        return instructor_verifications_for_reviewer(request.profile, request.user).order_by('-time')

    @staticmethod
    def _join_row(obj):
        if obj.request_class_id:
            applied_kind = 'class'
            target = obj.request_class.name
        else:
            applied_kind = 'course'
            target = obj.organization.short_name or obj.organization.name
        return {
            'box_id': 'j:%d' % obj.id,
            'source': 'join',
            'applied_kind': applied_kind,
            'obj': obj,
            'user': obj.user,
            'time': obj.time,
            'reason': obj.reason,
            'state': obj.state,
            'organization': obj.organization,
            'request_class': obj.request_class,
            'target_label': target,
            'detail_url': reverse(
                'request_organization_detail',
                args=(obj.organization.id, obj.organization.slug, obj.id),
            ),
        }

    @staticmethod
    def _verification_row(obj):
        applied_kind = 'teacher' if obj.kind == InstructorVerificationRequest.KIND_TEACHER else 'class_instructor'
        return {
            'box_id': 'v:%d' % obj.id,
            'source': 'verification',
            'applied_kind': applied_kind,
            'obj': obj,
            'user': obj.user,
            'time': obj.time,
            'reason': obj.reason,
            'state': obj.state,
            'organization': obj.organization,
            'request_class': obj.request_class,
            'target_label': obj.target_label,
            'detail_url': '',
        }

    def get(self, request, *args, **kwargs):
        if not self._can_access_inbox(request):
            return generic_message(request, _('Access Denied'),
                                   _('You do not have permission to review join requests.'), status=403)

        status = request.GET.get('status') or 'P'
        if status not in ('P', 'A', 'R', 'all'):
            status = 'P'
        course_id = safe_int(request.GET.get('course'))
        class_id = safe_int(request.GET.get('class'))
        kind = request.GET.get('kind') or ''
        if kind not in ('', 'course', 'class', 'teacher', 'class_instructor'):
            kind = ''
        q = (request.GET.get('q') or '').strip()

        join_qs = self._join_queryset(request)
        ivr_qs = self._verification_queryset(request)

        if status != 'all':
            join_qs = join_qs.filter(state=status)
            ivr_qs = ivr_qs.filter(state=status)
        if course_id:
            join_qs = join_qs.filter(organization_id=course_id)
            ivr_qs = ivr_qs.filter(organization_id=course_id)
        if class_id:
            join_qs = join_qs.filter(request_class_id=class_id)
            ivr_qs = ivr_qs.filter(request_class_id=class_id)
        if kind == 'course':
            join_qs = join_qs.filter(request_class__isnull=True)
            ivr_qs = ivr_qs.none()
        elif kind == 'class':
            join_qs = join_qs.filter(request_class__isnull=False)
            ivr_qs = ivr_qs.none()
        elif kind == 'teacher':
            join_qs = join_qs.none()
            ivr_qs = ivr_qs.filter(kind=InstructorVerificationRequest.KIND_TEACHER)
        elif kind == 'class_instructor':
            join_qs = join_qs.none()
            ivr_qs = ivr_qs.filter(kind=InstructorVerificationRequest.KIND_CLASS_INSTRUCTOR)
        if q:
            join_qs = join_qs.filter(
                Q(user__user__username__icontains=q) |
                Q(reason__icontains=q) |
                Q(organization__name__icontains=q) |
                Q(organization__short_name__icontains=q),
            )
            ivr_qs = ivr_qs.filter(
                Q(user__user__username__icontains=q) |
                Q(reason__icontains=q) |
                Q(organization__name__icontains=q) |
                Q(organization__short_name__icontains=q),
            )

        rows = [self._join_row(obj) for obj in join_qs]
        rows.extend(self._verification_row(obj) for obj in ivr_qs)
        rows.sort(key=lambda row: row['time'] or timezone.now(), reverse=True)

        profile = request.profile
        can_all = self._can_review_all(request)
        if can_all:
            courses = Organization.objects.order_by('name')
            classes = Class.objects.select_related('organization').order_by('organization__name', 'name')
        else:
            # Course admins + class instructors (orgs_reviewable_by already covers admins).
            courses = orgs_reviewable_by(profile).order_by('name')
            classes = Class.objects.filter(
                Q(organization__admins=profile) | Q(admins=profile),
            ).select_related('organization').distinct().order_by('organization__name', 'name')
        if course_id:
            classes = classes.filter(organization_id=course_id)

        chrome = course_inbox_chrome_context(request)
        ctx = {
            'title': self.title,
            'requests': rows,
            'status': status,
            'course_id': course_id,
            'class_id': class_id,
            'kind': kind,
            'filter_q': q,
            'courses': courses,
            'classes': classes,
            'inbox_panel': 'joins',
        }
        ctx.update(chrome)
        ctx['can_review_any'] = True
        return self.render_to_response(ctx)

    def post(self, request, *args, **kwargs):
        if not self._can_access_inbox(request):
            return generic_message(request, _('Access Denied'),
                                   _('You do not have permission to review join requests.'), status=403)
        action = request.POST.get('action')
        join_ids = []
        ivr_ids = []
        for raw in request.POST.getlist('request_ids'):
            raw = (raw or '').strip()
            if raw.startswith('j:'):
                try:
                    join_ids.append(int(raw[2:]))
                except (TypeError, ValueError):
                    pass
            elif raw.startswith('v:'):
                try:
                    ivr_ids.append(int(raw[2:]))
                except (TypeError, ValueError):
                    pass
            else:
                # Backward-compatible bare join ids.
                try:
                    join_ids.append(int(raw))
                except (TypeError, ValueError):
                    pass

        approved = rejected = reopened = deleted = 0

        def grant_membership(obj):
            apply_approved_join_request(obj)

        def reviewer_manages_course(obj):
            return self._can_review_all(request) or profile_is_org_admin(request.profile, obj.organization)

        def revoke_membership(obj):
            # CA-8: only a course-level reviewer may take a user out of the whole
            # course. A class instructor's reject/reopen undoes the class part only.
            if obj.grants_organization and reviewer_manages_course(obj):
                remove_profile_from_organization(obj.user, obj.organization)
            elif obj.request_class:
                obj.user.classes.remove(obj.request_class)
                obj.request_class.admins.remove(obj.user)
            else:
                cache.delete(make_template_fragment_key('org_member_count', (obj.organization_id,)))
                return
            cache.delete(make_template_fragment_key('org_member_count', (obj.organization_id,)))

        join_qs = list(self._join_queryset(request).filter(id__in=join_ids))
        ivr_qs = list(self._verification_queryset(request).filter(id__in=ivr_ids))

        if action == 'approve':
            candidates = [obj for obj in join_qs if obj.state in ('P', 'R')]
            by_org = {}
            for obj in candidates:
                by_org.setdefault(obj.organization_id, []).append(obj)
            blocked = set()
            for org_id, reqs in by_org.items():
                org = reqs[0].organization
                if org.slots is None:
                    continue
                current_members = org.members.count()
                member_ids = set(org.members.values_list('id', flat=True))
                missing = sum(1 for o in reqs if o.user_id not in member_ids)
                can_add = org.slots - current_members
                if missing > can_add:
                    blocked.add(org_id)
                    messages.error(
                        request,
                        _('Cannot approve %(count)d request(s) for %(course)s: only %(slots)d slot(s) left.') % {
                            'count': len(reqs),
                            'course': org.name,
                            'slots': max(can_add, 0),
                        },
                    )
            for obj in candidates:
                if obj.organization_id in blocked:
                    continue
                obj.state = 'A'
                obj.save()
                grant_membership(obj)
                approved += 1
            for obj in ivr_qs:
                if apply_instructor_verification_action(obj, 'approve', request.profile):
                    approved += 1
        elif action == 'reject':
            for obj in join_qs:
                if obj.state not in ('P', 'A'):
                    continue
                if obj.state == 'A':
                    revoke_membership(obj)
                obj.state = 'R'
                obj.save()
                rejected += 1
            for obj in ivr_qs:
                if apply_instructor_verification_action(obj, 'reject', request.profile):
                    rejected += 1
        elif action == 'reopen':
            for obj in join_qs:
                if obj.state not in ('A', 'R'):
                    continue
                if obj.state == 'A':
                    revoke_membership(obj)
                obj.state = 'P'
                obj.save()
                reopened += 1
            for obj in ivr_qs:
                if apply_instructor_verification_action(obj, 'reopen', request.profile):
                    reopened += 1
        elif action == 'delete':
            for obj in join_qs:
                obj.delete()
                deleted += 1
            for obj in ivr_qs:
                obj.delete()
                deleted += 1
            if deleted:
                messages.success(
                    request,
                    ngettext('Deleted %d request.', 'Deleted %d requests.', deleted) % deleted,
                )
            return HttpResponseRedirect(request.get_full_path())
        else:
            messages.error(request, _('Unknown action.'))
            return HttpResponseRedirect(request.get_full_path())

        if approved:
            messages.success(request, ngettext('Approved %d request.', 'Approved %d requests.', approved) % approved)
        if rejected:
            messages.success(request, ngettext('Rejected %d request.', 'Rejected %d requests.', rejected) % rejected)
        if reopened:
            messages.success(
                request,
                ngettext('Reopened %d request as pending.', 'Reopened %d requests as pending.', reopened) % reopened,
            )
        if action in ('approve', 'reject', 'reopen') and not approved and not rejected and not reopened:
            messages.error(request, _('No eligible requests selected for that action.'))
        return HttpResponseRedirect(request.get_full_path())

    def render_to_response(self, context):
        from django.shortcuts import render
        return render(self.request, self.template_name, context)


def safe_int(value):
    try:
        return int(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


class CreateOrganization(LoginRequiredMixin, TitleMixin, FormView):
    template_name = 'organization/edit.html'
    form_class = OrganizationSiteForm
    title = gettext_lazy('Add course')

    def dispatch(self, request, *args, **kwargs):
        if not (request.user.is_superuser or request.user.has_perm('judge.add_organization')):
            return generic_message(request, _('Access Denied'),
                                   _('You do not have permission to create courses.'), status=403)
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if self.request.method == 'GET':
            creator = self.request.profile
            form.fields['admins'].initial = [creator.pk]
            form.fields['instructors'].initial = [creator.pk]
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form_mode'] = 'create'
        context['title'] = self.title
        context['subject_prefixes'] = prefix_labels()
        context.update(course_inbox_chrome_context(self.request))
        return context

    def form_valid(self, form):
        with revisions.create_revision(atomic=True):
            org = form.save(commit=False)
            org.slug = unique_organization_slug(org.short_name or org.name)
            org.about = org.about or ''
            org.save()
            form.save_m2m()
            # Creator is always course admin + instructor (even if cleared in the form).
            org.admins.add(self.request.profile)
            org.instructors.add(self.request.profile)
            revisions.set_comment(_('Created course from site'))
            revisions.set_user(self.request.user)
        messages.success(self.request, _('Course "%s" created successfully.') % org.name)
        return HttpResponseRedirect(org.get_absolute_url())


class EditOrganization(LoginRequiredMixin, TitleMixin, OrganizationMixin, UpdateView):
    template_name = 'organization/edit.html'
    model = Organization
    form_class = EditOrganizationForm

    def get_title(self):
        return _('Editing %s') % self.object.name

    def get_object(self, queryset=None):
        object = super(EditOrganization, self).get_object()
        if not self.can_edit_organization(object) and not self.request.user.has_perm('judge.edit_all_organization'):
            raise PermissionDenied()
        return object

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form_mode'] = 'edit'
        context.update(course_inbox_chrome_context(self.request))
        return context

    def form_valid(self, form):
        with revisions.create_revision(atomic=True):
            revisions.set_comment(_('Edited from site'))
            revisions.set_user(self.request.user)
            return super(EditOrganization, self).form_valid(form)

    def dispatch(self, request, *args, **kwargs):
        try:
            return super(EditOrganization, self).dispatch(request, *args, **kwargs)
        except PermissionDenied:
            return generic_message(request, _("Can't edit organization"),
                                   _('You are not allowed to edit this organization.'), status=403)


class DeleteOrganization(LoginRequiredMixin, OrganizationMixin, SingleObjectMixin, View):
    def post(self, request, *args, **kwargs):
        organization = self.get_object()
        if not self.can_edit_organization(organization) and not request.user.has_perm('judge.delete_organization'):
            return generic_message(request, _("Can't delete course"),
                                   _('You are not allowed to delete this course.'), status=403)
        name = organization.name
        with revisions.create_revision(atomic=True):
            revisions.set_comment(_('Deleted course from site'))
            revisions.set_user(request.user)
            organization.delete()
        messages.success(request, _('Course "%s" deleted.') % name)
        return HttpResponseRedirect(reverse('organization_list'))


class KickUserWidgetView(LoginRequiredMixin, OrganizationMixin, SingleObjectMixin, View):
    def post(self, request, *args, **kwargs):
        organization = self.get_object()
        if not self.can_edit_organization(organization):
            return generic_message(request, _("Can't edit organization"),
                                   _('You are not allowed to kick people from this organization.'), status=403)

        try:
            user = Profile.objects.get(id=request.POST.get('user', None))
        except Profile.DoesNotExist:
            return generic_message(request, _("Can't kick user"),
                                   _('The user you are trying to kick does not exist!'), status=400)

        if not organization.members.filter(id=user.id).exists():
            return generic_message(request, _("Can't kick user"),
                                   _('The user you are trying to kick is not in organization: %s') %
                                   organization.name, status=400)

        remove_profile_from_organization(user, organization)
        return HttpResponseRedirect(organization.get_users_url())


class ClassMixin(TitleMixin, SingleObjectTemplateResponseMixin, SingleObjectMixin):
    context_object_name = 'class'
    model = Class
    pk_url_kwarg = 'cpk'

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        org = self.object.organization
        if self.object.slug != kwargs['cslug'] or org.id != kwargs['pk'] or org.slug != kwargs['slug']:
            return HttpResponsePermanentRedirect(self.object.get_absolute_url())
        context = self.get_context_data()
        return self.render_to_response(context)


class ClassHome(QueryStringSortMixin, ClassMixin, DetailView):
    template_name = 'organization/class.html'
    all_sorts = frozenset(('problem_count', 'rating', 'performance_points'))
    default_desc = all_sorts
    default_sort = '-performance_points'

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        org = self.object.organization
        if self.object.slug != kwargs.get('cslug') or org.id != kwargs.get('pk') or org.slug != kwargs.get('slug'):
            return HttpResponsePermanentRedirect(self.object.get_absolute_url())
        profile = request.profile if request.user.is_authenticated else None
        if not profile_can_access_class(profile, self.object, request.user, allow_pending=True):
            if not request.user.is_authenticated:
                return generic_message(
                    request, _('Login required'),
                    _('Log in and join this course and class to view class details.'),
                    status=403,
                )
            if profile is not None and org.members.filter(id=profile.id).exists():
                return generic_message(
                    request, _('Access Denied'),
                    _('Join this class to view its details, materials, and members.'),
                    status=403,
                )
            return generic_message(
                request, _('Access Denied'),
                _('Join this course first, then request to join the class.'),
                status=403,
            )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.object.organization
        profile = self.request.profile if self.request.user.is_authenticated else None
        context['logo_override_image'] = org.logo_override_image
        context['organization'] = org
        can_edit = False
        joined = False
        user_pending = False
        in_org = False
        is_site_teacher = False
        can_apply_class_instructor = False
        pending_class_instructor = False
        if profile is not None:
            in_org = org.members.filter(id=profile.id).exists()
            joined = self.object.members.filter(id=profile.id).exists()
            user_pending = (
                not joined and
                profile_has_pending_class_request(profile, self.object)
            )
            can_edit = profile_manages_class(profile, self.object, self.request.user)
            is_site_teacher = profile_is_site_teacher(profile, self.request.user)
            pending_class_instructor = InstructorVerificationRequest.objects.filter(
                user=profile,
                request_class=self.object,
                kind=InstructorVerificationRequest.KIND_CLASS_INSTRUCTOR,
                state='P',
            ).exists()
            can_apply_class_instructor = bool(
                is_site_teacher and in_org and not can_edit and not pending_class_instructor,
            )
        can_view_content = profile_can_view_class_content(profile, self.object, self.request.user)
        context['can_edit'] = can_edit
        context['can_manage'] = can_edit or profile_manages_organization(profile, org, self.request.user)
        context['class_joined'] = joined
        context['class_pending'] = user_pending
        context['in_organization'] = in_org
        context['can_view_content'] = can_view_content
        context['is_site_teacher'] = is_site_teacher
        context['can_apply_class_instructor'] = can_apply_class_instructor
        context['pending_class_instructor'] = pending_class_instructor
        context['is_admin'] = False  # Don't allow kicking here
        context['instructor_apply_org_id'] = org.id
        context['instructor_apply_class_id'] = self.object.id

        if can_view_content:
            # Students tab: class members who are not class instructors/admins.
            student_qs = self.object.members.exclude(
                id__in=self.object.admins.values_list('id', flat=True),
            )
            ranked_users = list(users_for_template(student_qs, self.order))
        else:
            ranked_users = []
        context['users'] = ranked_users

        meta = parse_class_description_meta(self.object.description)
        obj = self.object
        context['academic_year'] = obj.academic_year or ''
        context['session_label'] = obj.term_session or meta['session_label']
        context['offering_label'] = obj.offering_label or context['session_label'] or _('Not set')
        context['section_label'] = meta['section_label'] or obj.name
        context['location_line'] = obj.location or meta['location_line']
        context['schedule_line'] = obj.schedule_label or meta['schedule_line']
        context['dates_line'] = obj.schedule_label or meta['dates_line'] or meta['schedule_line']
        context['credits_line'] = (
            ('%s' % obj.credits).rstrip('0').rstrip('.') if obj.credits is not None else meta['credits_line']
        )
        context['materials_line'] = meta['materials_line']
        # Free-form description body (legacy structured prefix lines still stripped).
        context['description_body'] = '\n'.join(meta['desc_lines']).strip() or obj.description
        # Do not leak Canvas/location URLs in the hero for pending/outsiders.
        hero_location = context['location_line'] if can_view_content else ''
        hero_bits = [b for b in (context['schedule_line'], hero_location) if b]
        context['hero_subtitle'] = ' · '.join(hero_bits)

        admins = list(self.object.admins.select_related('user').all())
        if not admins:
            admins = list(org.admins.select_related('user').all()[:5])
        context['class_admins'] = admins
        context['admin_names'] = ', '.join(a.user.username for a in admins) if admins else _('Not set')
        context['instructor_names'] = self.object.instructor_display or _('Not set')
        context['ta_names'] = self.object.ta_display or _('Not set')
        context['member_count'] = self.object.members.count() if can_view_content else None
        context['not_set'] = _('Not set')

        # Assignments = contests linked to this class (visible to user, or all for editors).
        assignments = []
        assignment_tags = set()
        if can_view_content:
            contest_qs = (
                Contest.objects.filter(classes=self.object)
                .prefetch_related('tags', 'problems')
                .order_by('-start_time')
            )
            for contest in contest_qs:
                if not (can_edit or contest.is_accessible_by(self.request.user)):
                    continue
                status_key, status_label, timing = contest_assignment_status(contest)
                tag_names = [t.name for t in contest.tags.all()[:6]]
                assignment_tags.update(tag_names)
                assignments.append({
                    'contest': contest,
                    'status_key': status_key,
                    'status_label': status_label,
                    'timing_label': timing,
                    'tag_names': tag_names,
                    'format_name': contest.format_name or 'default',
                    'summary': plain_text_summary(contest.summary or contest.description, max_len=180),
                    'problem_count': contest.problems.count(),
                })
        context['assignments'] = assignments
        context['assignment_tags'] = sorted(assignment_tags)

        folder_id = safe_int(self.request.GET.get('folder'))
        material_qs = ClassMaterial.objects.filter(class_obj=self.object).select_related(
            'uploaded_by__user', 'parent',
        )
        current_folder = None
        material_rows = []
        if can_view_content:
            if folder_id:
                current_folder = material_qs.filter(id=folder_id, is_folder=True).first()
            material_rows = list(
                material_qs.filter(parent=current_folder).order_by('-is_folder', 'name'),
            )
        context['current_folder'] = current_folder
        context['material_rows'] = material_rows
        context['material_count'] = len(material_rows)
        context['material_upload_url'] = reverse(
            'class_material_action',
            args=[org.id, org.slug, self.object.id, self.object.slug],
        )

        context.update(course_inbox_chrome_context(self.request))
        if can_edit and self.request.user.has_perm('judge.add_contest'):
            context['add_assignment_url'] = reverse('contest_create')
        else:
            context['add_assignment_url'] = ''

        context.update(self.get_sort_context())
        return context

    def get_title(self):
        return _('%(class)s - %(organization)s') % {
            'class': self.object.name,
            'organization': self.object.organization.name,
        }


class ClassRequestForm(Form):
    reason = forms.CharField(widget=forms.Textarea)


class RequestJoinClass(LoginRequiredMixin, ClassMixin, FormView):
    template_name = 'organization/requests/request.html'
    form_class = ClassRequestForm

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        self.object = self.get_object()
        org = self.object.organization
        profile = self.request.profile
        if (
            profile_manages_organization(profile, org, request.user) or
            self.object.admins.filter(id=profile.id).exists()
        ):
            return generic_message(self.request, _("Can't request to join %s") % self.object.name,
                                   _('You already manage this class and do not need to request membership.'))
        if self.object.members.filter(id=profile.id).exists():
            return generic_message(self.request, _("Can't request to join %s") % self.object.name,
                                   _('You are already a member of %s.') % self.object.name)
        if not org.members.filter(id=profile.id).exists():
            if org.is_open:
                messages.info(self.request, _('Join the course first, then request this class.'))
                return HttpResponseRedirect(org.get_absolute_url())
            return HttpResponseRedirect(reverse('request_organization', args=(org.id, org.slug)))
        if org.requests.filter(user=profile, state='P', request_class=self.object).exists():
            return generic_message(self.request, _("Can't request to join %s") % self.object.name,
                                   _('You already have a pending request to join %s.') % self.object.name)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = _('Request to join {name} in {organization}').format(
            name=self.object.name, organization=self.object.organization.name,
        )
        return context

    def form_valid(self, form):
        request = OrganizationRequest()
        request.organization = self.object.organization
        request.user = self.request.profile
        request.reason = form.cleaned_data['reason']
        request.request_class = self.object
        request.state = 'P'
        request.grants_organization = False
        request.save()
        notify_class_join(request)
        messages.success(self.request, _('Your class join request has been submitted.'))
        return HttpResponseRedirect(reverse('request_organization_detail', args=(
            request.organization.id, request.organization.slug, request.id,
        )))


def apply_class_member_csv(form, class_obj, organization):
    """Process optional member_csv on ClassSiteForm; link users to class + course."""
    csv_file = form.cleaned_data.get('member_csv')
    if not csv_file:
        return None
    if hasattr(csv_file, 'seek'):
        csv_file.seek(0)
    # CA-1: a class roster upload only creates new accounts and links existing
    # ones. It never rewrites an existing account's name, e-mail or active flag.
    result = process_user_csv(
        csv_file, organization=organization, target_class=class_obj,
        update_existing=False, activate=True,
    )
    return result


def _flash_class_csv_import_result(request, csv_result):
    """Surface create/link counts and any import errors after class create/edit."""
    if csv_result is None:
        return
    messages.info(
        request,
        _('CSV import: %(created)d created, %(updated)d updated, %(linked)d linked to class.') % {
            'created': csv_result.created,
            'updated': csv_result.updated,
            'linked': csv_result.class_linked,
        },
    )
    for err in csv_result.errors[:10]:
        messages.error(request, err)
    if len(csv_result.errors) > 10:
        messages.error(
            request,
            _('…and %(count)d more CSV import error(s).') % {
                'count': len(csv_result.errors) - 10,
            },
        )


class CreateClass(LoginRequiredMixin, OrganizationMixin, FormView):
    template_name = 'organization/create_class.html'
    form_class = ClassSiteForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['organization'] = self.object
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if self.request.method == 'GET':
            form.fields['admins'].initial = [self.request.profile.pk]
            form.fields['instructor_names'].initial = self.request.profile.display_name
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['organization'] = self.object
        context['title'] = _('Add class - %s') % self.object.name
        context['form_mode'] = 'create'
        context.update(course_inbox_chrome_context(self.request))
        return context

    def get_object(self, queryset=None):
        return get_object_or_404(Organization, id=self.kwargs['pk'])

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.object = self.get_object()
        if not self.can_create_class():
            return generic_message(request, _('Access Denied'),
                                   _('You do not have permission to create classes for this organization.'), status=403)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        new_class = form.save(commit=False)
        new_class.organization = self.object
        new_class.slug = unique_class_slug(self.object, new_class.name)
        if new_class.is_active is None:
            new_class.is_active = True
        new_class.save()
        form.save_m2m()
        new_class.admins.add(self.request.profile)
        csv_result = apply_class_member_csv(form, new_class, self.object)
        sync_class_members_to_organization(new_class)
        messages.success(self.request, _('Class "%s" created successfully.') % new_class.name)
        _flash_class_csv_import_result(self.request, csv_result)
        return HttpResponseRedirect(new_class.get_absolute_url())


class EditClass(LoginRequiredMixin, TitleMixin, FormView):
    template_name = 'organization/create_class.html'
    form_class = ClassSiteForm

    def get_object(self):
        return get_object_or_404(Class, id=self.kwargs['cpk'])

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.object = self.get_object()
        org = self.object.organization
        if not (org.admins.filter(id=request.profile.id).exists() or
                self.object.admins.filter(id=request.profile.id).exists() or
                request.user.has_perm('judge.edit_all_organization')):
            return generic_message(request, _('Access Denied'),
                                   _('You do not have permission to edit this class.'), status=403)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['instance'] = self.object
        kwargs['organization'] = self.object.organization
        return kwargs

    def get_title(self):
        return _('Edit class - %s') % self.object.name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['organization'] = self.object.organization
        context['form_mode'] = 'edit'
        context['class'] = self.object
        context.update(course_inbox_chrome_context(self.request))
        return context

    def form_valid(self, form):
        previous_admin_ids = set(self.object.admins.values_list('id', flat=True))
        form.save()
        # HeavySelect2 often omits admins on submit (empty POST → clears M2M). If the
        # class previously had administrators and the submit cleared them all, restore.
        submitted_admins = form.cleaned_data.get('admins')
        if previous_admin_ids and (submitted_admins is None or not submitted_admins.exists()):
            self.object.admins.add(*previous_admin_ids)
        # The editor who just saved must remain able to edit.
        self.object.admins.add(self.request.profile)
        csv_result = apply_class_member_csv(form, self.object, self.object.organization)
        sync_class_members_to_organization(self.object)
        messages.success(self.request, _('Class "%s" updated.') % self.object.name)
        _flash_class_csv_import_result(self.request, csv_result)
        return HttpResponseRedirect(self.object.get_absolute_url())


class ClassImportMembers(LoginRequiredMixin, View):
    """Import a Canvas/roster CSV into an existing class immediately (no Save class)."""

    def post(self, request, *args, **kwargs):
        class_obj = get_object_or_404(Class, id=kwargs['cpk'])
        org = class_obj.organization
        if not (org.admins.filter(id=request.profile.id).exists() or
                class_obj.admins.filter(id=request.profile.id).exists() or
                request.user.has_perm('judge.edit_all_organization')):
            return generic_message(request, _('Access Denied'),
                                   _('You do not have permission to import members for this class.'),
                                   status=403)

        csv_file = request.FILES.get('member_csv')
        if not csv_file:
            messages.error(request, _('Choose a CSV file to import.'))
            return HttpResponseRedirect(reverse('class_edit', args=[org.id, org.slug, class_obj.id, class_obj.slug]))

        # CA-1: only create new accounts and link existing ones (see apply_class_member_csv).
        result = process_user_csv(
            csv_file, organization=org, target_class=class_obj,
            update_existing=False, activate=True,
        )
        sync_class_members_to_organization(class_obj)
        _flash_class_csv_import_result(request, result)
        return HttpResponseRedirect(class_obj.get_absolute_url() + '?tab=info')


class DeleteClass(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        class_obj = get_object_or_404(Class, id=kwargs['cpk'])
        org = class_obj.organization
        if not (org.admins.filter(id=request.profile.id).exists() or
                class_obj.admins.filter(id=request.profile.id).exists() or
                user_can_edit_all_organizations(request.user)):
            # CA-2: the site-wide judge.delete_class perm (held by every site
            # teacher through the Instructors group) is not enough on its own.
            return generic_message(request, _("Can't delete class"),
                                   _('You are not allowed to delete this class.'), status=403)
        name = class_obj.name
        class_obj.delete()
        messages.success(request, _('Class "%s" deleted.') % name)
        return HttpResponseRedirect(org.get_absolute_url())


class ClassMaterialActionView(LoginRequiredMixin, View):
    """Upload / new-folder / delete materials directly on the class Materials tab."""

    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        class_obj = get_object_or_404(Class, id=kwargs['cpk'])
        org = class_obj.organization
        if class_obj.slug != kwargs.get('cslug') or org.id != kwargs.get('pk') or org.slug != kwargs.get('slug'):
            return HttpResponsePermanentRedirect(reverse(
                'class_material_action',
                args=[org.id, org.slug, class_obj.id, class_obj.slug],
            ))
        if not profile_manages_class(request.profile, class_obj, request.user):
            return generic_message(request, _('Access Denied'),
                                   _('You do not have permission to manage materials for this class.'), status=403)

        action = request.POST.get('action')
        folder_id = safe_int(request.POST.get('folder') or request.GET.get('folder'))
        parent = None
        if folder_id:
            parent = ClassMaterial.objects.filter(
                id=folder_id, class_obj=class_obj, is_folder=True,
            ).first()

        redirect_url = class_obj.get_absolute_url() + '?tab=materials'
        if parent:
            redirect_url += '&folder=%d' % parent.id

        if action == 'upload':
            files = request.FILES.getlist('files') or (
                [request.FILES['file']] if 'file' in request.FILES else []
            )
            if not files:
                messages.error(request, _('Choose at least one file to upload.'))
                return HttpResponseRedirect(redirect_url)
            created = 0
            for uploaded in files:
                name = (uploaded.name or _('Untitled'))[:255]
                ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
                if ext not in CLASS_MATERIAL_ALLOWED_EXTENSIONS:
                    messages.error(
                        request,
                        _('File "%(name)s" has a disallowed type. Allowed: %(types)s.') % {
                            'name': name,
                            'types': ', '.join(sorted(CLASS_MATERIAL_ALLOWED_EXTENSIONS)),
                        },
                    )
                    continue
                size = getattr(uploaded, 'size', None)
                if size is not None and size > CLASS_MATERIAL_MAX_BYTES:
                    messages.error(
                        request,
                        _('File "%(name)s" exceeds the %(limit)d MB upload limit.') % {
                            'name': name,
                            'limit': CLASS_MATERIAL_MAX_BYTES // (1024 * 1024),
                        },
                    )
                    continue
                ClassMaterial.objects.create(
                    class_obj=class_obj,
                    parent=parent,
                    name=name,
                    is_folder=False,
                    file=uploaded,
                    uploaded_by=request.profile,
                )
                created += 1
            if created:
                messages.success(
                    request,
                    ngettext('Uploaded %d file.', 'Uploaded %d files.', created) % created,
                )
            elif files:
                messages.error(request, _('No files were uploaded.'))
        elif action == 'mkdir':
            name = (request.POST.get('name') or '').strip()
            if not name:
                messages.error(request, _('Enter a folder name.'))
                return HttpResponseRedirect(redirect_url)
            ClassMaterial.objects.create(
                class_obj=class_obj,
                parent=parent,
                name=name[:255],
                is_folder=True,
                uploaded_by=request.profile,
            )
            messages.success(request, _('Folder "%s" created.') % name)
        elif action == 'delete':
            ids = []
            for raw in request.POST.getlist('material_ids'):
                try:
                    ids.append(int(raw))
                except (TypeError, ValueError):
                    continue
            # Scoped to this class only. Folder CASCADE + pre_delete removes nested
            # files from OSS (same store as file-upload submissions).
            qs = ClassMaterial.objects.filter(class_obj=class_obj, id__in=ids)
            count = qs.count()
            if count:
                qs.delete()
                messages.success(
                    request,
                    ngettext('Deleted %d item.', 'Deleted %d items.', count) % count,
                )
            else:
                messages.error(request, _('No materials selected.'))
        else:
            messages.error(request, _('Unknown action.'))
        return HttpResponseRedirect(redirect_url)


class ClassMaterialDownloadView(LoginRequiredMixin, View):
    """Stream class materials through Django (private OSS storage; no raw MEDIA/presigned URLs)."""

    http_method_names = ['get']

    def get(self, request, *args, **kwargs):
        from django.http import FileResponse, Http404, HttpResponse

        from judge.storage import StorageUnavailableError

        class_obj = get_object_or_404(Class, id=kwargs['cpk'])
        org = class_obj.organization
        if class_obj.slug != kwargs.get('cslug') or org.id != kwargs.get('pk') or org.slug != kwargs.get('slug'):
            return HttpResponsePermanentRedirect(reverse(
                'class_material_download',
                args=[org.id, org.slug, class_obj.id, class_obj.slug, kwargs['mid']],
            ))
        # Members / instructors of THIS class only — blocks cross-course downloads.
        if not profile_can_view_class_content(request.profile, class_obj, request.user):
            return generic_message(
                request, _('Access Denied'),
                _('You do not have permission to download materials for this class.'),
                status=403,
            )
        material = get_object_or_404(
            ClassMaterial, id=kwargs['mid'], class_obj=class_obj, is_folder=False,
        )
        if not material.file:
            raise Http404()
        try:
            handle = material.file.open('rb')
        except StorageUnavailableError:
            return HttpResponse(
                _('The file store is temporarily unavailable. Please try again later.'),
                status=503, content_type='text/plain',
            )
        except (OSError, ValueError, FileNotFoundError):
            raise Http404()
        # Force attachment + neutral type (uploaded HTML/SVG must never render inline).
        return FileResponse(
            handle, as_attachment=True, filename=material.name,
            content_type='application/octet-stream',
        )


class ClassLinkAssignmentsView(LoginRequiredMixin, TitleMixin, FormView):
    """Link contests to a class as Assignments (site UI, not Django admin)."""

    template_name = 'organization/link_assignments.html'
    form_class = ClassAssignmentsForm

    def get_object(self):
        return get_object_or_404(Class, id=self.kwargs['cpk'])

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.object = self.get_object()
        org = self.object.organization
        if self.object.slug != kwargs.get('cslug') or org.id != kwargs.get('pk') or org.slug != kwargs.get('slug'):
            return HttpResponsePermanentRedirect(reverse(
                'class_link_assignments',
                args=[org.id, org.slug, self.object.id, self.object.slug],
            ))
        if not (
            org.admins.filter(id=request.profile.id).exists() or
            self.object.admins.filter(id=request.profile.id).exists() or
            user_can_edit_all_organizations(request.user)
        ):
            return generic_message(request, _('Access Denied'),
                                   _('You do not have permission to manage assignments for this class.'), status=403)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['class_obj'] = self.object
        kwargs['user'] = self.request.user
        return kwargs

    def get_title(self):
        return _('Add from library - %s') % self.object.name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['organization'] = self.object.organization
        context['class'] = self.object
        context['title'] = self.get_title()
        context.update(course_inbox_chrome_context(self.request))
        return context

    def form_valid(self, form):
        selected = list(form.cleaned_data['contests'])
        selected_ids = {c.id for c in selected}
        current = list(Contest.objects.filter(classes=self.object))
        for contest in current:
            if contest.id not in selected_ids:
                contest.classes.remove(self.object)
        for contest in selected:
            contest.classes.add(self.object)
        messages.success(
            self.request,
            ngettext(
                'Updated assignments: %d contest linked.',
                'Updated assignments: %d contests linked.',
                len(selected),
            ) % len(selected),
        )
        return HttpResponseRedirect(self.object.get_absolute_url() + '?tab=assignments')


class OrganizationImportForm(forms.Form):
    csv_file = forms.FileField(label=gettext_lazy('CSV File'))
    target_class = forms.ModelChoiceField(
        queryset=Class.objects.none(),
        required=False,
        label=gettext_lazy('Target Class'),
        empty_label=gettext_lazy('-- No class (organization only) --'),
    )
    update_existing = forms.BooleanField(
        required=False,
        initial=True,
        label=gettext_lazy('Update existing users'),
    )
    activate = forms.BooleanField(required=False, initial=True, label=gettext_lazy('Activate users'))

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization:
            self.fields['target_class'].queryset = organization.classes.filter(is_active=True).order_by('name')


class OrganizationImportUsers(LoginRequiredMixin, OrganizationMixin, FormView):
    template_name = 'organization/import_users.html'
    form_class = OrganizationImportForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['organization'] = self.object
        context['title'] = _('Import Users - %s') % self.object.name
        context['can_edit'] = self.can_edit_organization()
        return context

    def get_object(self, queryset=None):
        return get_object_or_404(Organization, id=self.kwargs['pk'])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['organization'] = self.object
        return kwargs

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.object = self.get_object()
        if not self.can_edit_organization():
            return generic_message(request, _('Access Denied'),
                                   _('You do not have permission to import users for this organization.'), status=403)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        csv_file = form.cleaned_data['csv_file']
        update_existing = form.cleaned_data['update_existing']
        activate = form.cleaned_data['activate']
        target_class = form.cleaned_data.get('target_class')

        from judge.utils.import_export import process_user_csv
        # We need to read the file content, process_user_csv expects an open text file or iterable of lines
        # But we can also pass the InMemoryUploadedFile if we handle decoding inside.
        # The utils function I wrote: `decoded_file = csv_file.read().decode('utf-8-sig').splitlines()`
        # requires the file pointer to be at start.
        if hasattr(csv_file, 'seek'):
            csv_file.seek(0)

        result = process_user_csv(csv_file, organization=self.object, target_class=target_class,
                                  update_existing=update_existing, activate=activate)

        return self.render_to_response(self.get_context_data(form=form, result=result))


class InstructorVerificationForm(Form):
    """Site-wide teacher apply, or class-instructor apply (kind selects required fields)."""

    kind = forms.ChoiceField(
        choices=InstructorVerificationRequest.KIND_CHOICES,
        initial=InstructorVerificationRequest.KIND_TEACHER,
    )
    organization = forms.ModelChoiceField(
        queryset=Organization.objects.all(),
        label=gettext_lazy('Course'),
        required=False,
    )
    request_class = forms.ModelChoiceField(
        queryset=Class.objects.filter(is_active=True),
        label=gettext_lazy('Class / session'),
        required=False,
    )
    reason = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 4, 'class': 'oj-input'}),
        label=gettext_lazy('Reason'),
        max_length=2000,
    )

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get('kind') or InstructorVerificationRequest.KIND_TEACHER
        org = cleaned.get('organization')
        klass = cleaned.get('request_class')
        if kind == InstructorVerificationRequest.KIND_TEACHER:
            cleaned['organization'] = None
            cleaned['request_class'] = None
            return cleaned
        if org is None or klass is None:
            raise forms.ValidationError(_('Please choose a course and class/session.'))
        if klass.organization_id != org.id:
            raise forms.ValidationError(_('Selected class does not belong to that course.'))
        if not klass.is_active:
            raise forms.ValidationError(_('That class is not active.'))
        return cleaned


class InstructorVerificationSubmitView(LoginRequiredMixin, View):
    """POST-only: apply as site-wide teacher, or as instructor of one class."""

    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        form = InstructorVerificationForm(request.POST)
        next_url = safe_redirect_url(
            request,
            request.POST.get('next') or request.META.get('HTTP_REFERER'),
            reverse('organization_list'),
        )
        if not form.is_valid():
            messages.error(
                request,
                _('Please provide a short reason (and course/class for class-instructor requests).'),
            )
            return HttpResponseRedirect(next_url)

        profile = request.profile
        kind = form.cleaned_data['kind']
        org = form.cleaned_data.get('organization')
        klass = form.cleaned_data.get('request_class')

        if kind == InstructorVerificationRequest.KIND_TEACHER:
            if profile_is_site_teacher(profile, request.user):
                messages.info(request, _('You already have teacher access.'))
                return HttpResponseRedirect(next_url)
            if InstructorVerificationRequest.objects.filter(
                user=profile, kind=InstructorVerificationRequest.KIND_TEACHER, state='P',
            ).exists():
                messages.info(request, _('You already have a pending teacher application.'))
                return HttpResponseRedirect(next_url)
            verification = InstructorVerificationRequest.objects.create(
                user=profile,
                kind=InstructorVerificationRequest.KIND_TEACHER,
                organization=None,
                request_class=None,
                reason=form.cleaned_data['reason'].strip(),
                state='P',
            )
            notify_instructor_verification(verification)
            messages.success(
                request,
                _('Your teacher application was sent to site administrators. '
                  'Once approved you can create problems and courses.'),
            )
            return HttpResponseRedirect(next_url)

        # CA-11: the UI only offers this to site teachers; enforce it here too.
        if not profile_is_site_teacher(profile, request.user):
            messages.error(
                request,
                _('Only approved teachers can apply to instruct a class. Apply for teacher access first.'),
            )
            return HttpResponseRedirect(next_url)
        if profile_is_class_admin(profile, klass):
            messages.info(request, _('You already instruct this class.'))
            return HttpResponseRedirect(next_url)
        if profile_is_org_admin(profile, org):
            messages.info(
                request,
                _('You are already a course administrator for this course.'),
            )
            return HttpResponseRedirect(next_url)

        if InstructorVerificationRequest.objects.filter(
            user=profile, request_class=klass, state='P',
            kind=InstructorVerificationRequest.KIND_CLASS_INSTRUCTOR,
        ).exists():
            messages.info(
                request,
                _('You already have a pending instructor request for this class.'),
            )
            return HttpResponseRedirect(next_url)

        verification = InstructorVerificationRequest.objects.create(
            user=profile,
            kind=InstructorVerificationRequest.KIND_CLASS_INSTRUCTOR,
            organization=org,
            request_class=klass,
            reason=form.cleaned_data['reason'].strip(),
            state='P',
        )
        notify_instructor_verification(verification)
        messages.success(
            request,
            _('Your request to instruct %(target)s was sent to the course administrators.') % {
                'target': verification.target_label or klass.name,
            },
        )
        return HttpResponseRedirect(next_url)


class AdminNotificationInboxView(LoginRequiredMixin, View):
    """Legacy Messages inbox — redirects into unified Join requests."""

    def get(self, request, *args, **kwargs):
        return HttpResponseRedirect(reverse('organization_join_requests'))

    post = get


class InstructorVerificationInboxView(LoginRequiredMixin, View):
    """Legacy Teacher verification inbox — redirects into unified Join requests."""

    def get(self, request, *args, **kwargs):
        return HttpResponseRedirect(reverse('organization_join_requests'))

    post = get


# Backward-compatible aliases (teacher → instructor rename).
TeacherJoinRequestsView = InstructorJoinRequestsView
TeacherVerificationInboxView = InstructorVerificationInboxView
TeacherVerificationSubmitView = InstructorVerificationSubmitView
TeacherVerificationForm = InstructorVerificationForm
