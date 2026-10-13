# Fold leftover Instructor / Teachers / Teacher auth groups into Instructors
# and attach the canonical instructor permission set.

from django.db import migrations


CANONICAL = 'Instructors'
ALIASES = ('Instructor', 'Teachers', 'Teacher')
PERMISSION_CODENAMES = (
    'add_organization',
    'change_organization',
    'organization_admin',
    'add_class',
    'change_class',
    'delete_class',
    'create_private_problem',
    'edit_own_problem',
    'create_private_contest',
    'contest_seb_requirement',
)


def merge_instructor_groups(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    group, _created = Group.objects.get_or_create(name=CANONICAL)
    for alias in ALIASES:
        extras = Group.objects.filter(name=alias).exclude(pk=group.pk)
        for extra in extras:
            for user in extra.user_set.all():
                group.user_set.add(user)
            for perm in extra.permissions.all():
                group.permissions.add(perm)
            extra.delete()

    perms = Permission.objects.filter(
        content_type__app_label='judge',
        codename__in=PERMISSION_CODENAMES,
    )
    group.permissions.set(perms)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0205_merge_contest_ai_grading_and_editor_attachment'),
    ]

    operations = [
        migrations.RunPython(merge_instructor_groups, noop_reverse),
    ]
