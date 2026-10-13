# Let instructors arm Safe Exam Browser on their own assignments.
#
# Without judge.contest_seb_requirement the whole SEB card is hidden from them
# (ContestCreate._restricted_fields / ContestAdmin.get_readonly_fields), so they
# could not even opt into the site default configuration.

from django.db import migrations

INSTRUCTORS_GROUP = 'Instructors'
SEB_PERMISSION = 'contest_seb_requirement'


def attach_seb_permission(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    group, _created = Group.objects.get_or_create(name=INSTRUCTORS_GROUP)
    perms = Permission.objects.filter(content_type__app_label='judge', codename=SEB_PERMISSION)
    group.permissions.add(*perms)


def detach_seb_permission(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    group = Group.objects.filter(name=INSTRUCTORS_GROUP).first()
    if group is None:
        return
    perms = Permission.objects.filter(content_type__app_label='judge', codename=SEB_PERMISSION)
    group.permissions.remove(*perms)


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0193_seb_config_preset'),
    ]

    operations = [
        migrations.RunPython(attach_seb_permission, detach_seb_permission),
    ]
