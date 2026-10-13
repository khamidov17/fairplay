# Grant Instructors create_private_contest so verified teachers get
# Add Assignment and can open ContestCreate.
#
# develop already merged 0194_organization_instructors with 0200_problemcodeconfig
# (0201_merge_... -> 0202_contest_allow_ai_hints). Depend on that leaf instead of
# repeating the same merge, matching the !86 handling of a duplicate 0201 merge.

from django.db import migrations

INSTRUCTORS_GROUP = 'Instructors'
CREATE_ASSIGNMENT_PERMISSION = 'create_private_contest'


def attach_create_assignment_permission(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    group, _created = Group.objects.get_or_create(name=INSTRUCTORS_GROUP)
    perms = Permission.objects.filter(
        content_type__app_label='judge',
        codename=CREATE_ASSIGNMENT_PERMISSION,
    )
    group.permissions.add(*perms)


def detach_create_assignment_permission(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    group = Group.objects.filter(name=INSTRUCTORS_GROUP).first()
    if group is None:
        return
    perms = Permission.objects.filter(
        content_type__app_label='judge',
        codename=CREATE_ASSIGNMENT_PERMISSION,
    )
    group.permissions.remove(*perms)


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0202_contest_allow_ai_hints'),
    ]

    operations = [
        migrations.RunPython(
            attach_create_assignment_permission,
            detach_create_assignment_permission,
        ),
    ]
