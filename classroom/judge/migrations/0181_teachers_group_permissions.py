from django.db import migrations


TEACHERS_GROUP = 'Teachers'
TEACHER_PERMISSION_CODENAMES = (
    'add_organization',
    'change_organization',
    'organization_admin',
    'add_class',
    'change_class',
    'delete_class',
    'create_private_problem',
    'edit_own_problem',
)


def attach_teacher_permissions(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    group, _ = Group.objects.get_or_create(name=TEACHERS_GROUP)
    perms = Permission.objects.filter(
        content_type__app_label='judge',
        codename__in=TEACHER_PERMISSION_CODENAMES,
    )
    group.permissions.add(*perms)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0180_admin_notification_teacher_verification'),
    ]

    operations = [
        migrations.RunPython(attach_teacher_permissions, noop_reverse),
    ]
