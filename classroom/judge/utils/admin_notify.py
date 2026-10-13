from django.urls import reverse
from django.utils.translation import gettext as _

from judge.models import AdminNotification, Profile


def site_administrator_profiles():
    """Active Django superusers (the original site-admin flag, not a custom group)."""
    return Profile.objects.filter(user__is_active=True, user__is_superuser=True).distinct()


def notify_profiles(recipients, *, kind, title, body='', actor=None, link='',
                    organization_request=None, instructor_verification=None,
                    teacher_verification=None):
    """Create AdminNotification rows for each recipient (skips actor)."""
    if instructor_verification is None:
        instructor_verification = teacher_verification
    if recipients is None:
        return 0
    if hasattr(recipients, 'all'):
        recipients = list(recipients.all())
    else:
        recipients = list(recipients)
    created = 0
    actor_id = getattr(actor, 'id', None)
    seen = set()
    for profile in recipients:
        if profile is None or profile.id in seen:
            continue
        if actor_id and profile.id == actor_id:
            continue
        seen.add(profile.id)
        AdminNotification.objects.create(
            recipient=profile,
            actor=actor,
            kind=kind,
            title=title,
            body=body or '',
            link=link or '',
            organization_request=organization_request,
            instructor_verification=instructor_verification,
        )
        created += 1
    return created


def notify_course_join(org_request):
    org = org_request.organization
    actor = org_request.user
    recipients = org.admins.all()
    title = _('Course join request: %(course)s') % {'course': org.short_name or org.name}
    body = _('%(user)s requested to join %(course)s.') % {
        'user': actor.username,
        'course': org.name,
    }
    if org_request.request_class_id:
        body += ' ' + _('Preferred class: %(klass)s.') % {'klass': org_request.request_class.name}
    if org_request.reason:
        body += '\n' + org_request.reason
    link = reverse('organization_join_requests') + '?status=P&course=%d' % org.id
    return notify_profiles(
        recipients,
        kind=AdminNotification.KIND_COURSE_JOIN,
        title=title,
        body=body,
        actor=actor,
        link=link,
        organization_request=org_request,
    )


def notify_class_join(org_request):
    klass = org_request.request_class
    org = org_request.organization
    actor = org_request.user
    recipients = list(klass.admins.all()) if klass is not None else []
    if not recipients:
        recipients = list(org.admins.all())
    title = _('Class join request: %(klass)s') % {
        'klass': klass.name if klass else org.name,
    }
    body = _('%(user)s requested to join class %(klass)s in %(course)s.') % {
        'user': actor.username,
        'klass': klass.name if klass else '—',
        'course': org.short_name or org.name,
    }
    if org_request.reason:
        body += '\n' + org_request.reason
    link = reverse('organization_join_requests') + '?status=P'
    if org is not None:
        link += '&course=%d' % org.id
    if klass is not None:
        link += '&class=%d' % klass.id
    return notify_profiles(
        recipients,
        kind=AdminNotification.KIND_CLASS_JOIN,
        title=title,
        body=body,
        actor=actor,
        link=link,
        organization_request=org_request,
    )


def notify_instructor_verification(verification):
    actor = verification.user
    if getattr(verification, 'kind', None) == verification.KIND_TEACHER:
        title = _('Teacher application: %(user)s') % {'user': actor.username}
        body = _('%(user)s applied for site-wide teacher access (create problems and courses).') % {
            'user': actor.username,
        }
        recipients = site_administrator_profiles()
    else:
        target = verification.target_label or _('a class')
        title = _('Class instructor request: %(user)s → %(target)s') % {
            'user': actor.username, 'target': target,
        }
        body = _('%(user)s applied to instruct %(target)s.') % {
            'user': actor.username, 'target': target,
        }
        # Course administrators review class-instructor applications for their course.
        recipients = []
        if verification.organization_id:
            recipients = list(verification.organization.admins.all())
        if not recipients:
            recipients = list(site_administrator_profiles())
    if verification.reason:
        body += '\n' + verification.reason
    link = reverse('organization_join_requests') + '?status=P'
    return notify_profiles(
        recipients,
        kind=AdminNotification.KIND_INSTRUCTOR_VERIFY,
        title=title,
        body=body,
        actor=actor,
        link=link,
        instructor_verification=verification,
    )


notify_teacher_verification = notify_instructor_verification


def unread_notification_count(profile):
    if profile is None:
        return 0
    return AdminNotification.objects.filter(recipient=profile, read_at__isnull=True).count()
