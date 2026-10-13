from django.db import migrations


def add_edit_own_contest(apps, schema_editor):
    # Instructors could create assignments (add_contest) but not open them again to edit
    # (edit_own_contest). Give the Instructors group that permission.
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    group = Group.objects.filter(name='Instructors').first()
    perm = Permission.objects.filter(content_type__app_label='judge', codename='edit_own_contest').first()
    if group is not None and perm is not None:
        group.permissions.add(perm)


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0223_remove_seb_and_exam_lock'),
    ]

    operations = [
        migrations.RunPython(add_edit_own_contest, migrations.RunPython.noop),
    ]
