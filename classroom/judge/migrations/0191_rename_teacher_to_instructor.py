# Rename teacher → instructor for course verification models / auth group / notification kind.

from django.db import migrations


def rename_teachers_group_forward(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name='Teachers').update(name='Instructors')


def rename_teachers_group_reverse(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name='Instructors').update(name='Teachers')


def rename_kind_forward(apps, schema_editor):
    AdminNotification = apps.get_model('judge', 'AdminNotification')
    AdminNotification.objects.filter(kind='teacher_verify').update(kind='instructor_verify')


def rename_kind_reverse(apps, schema_editor):
    AdminNotification = apps.get_model('judge', 'AdminNotification')
    AdminNotification.objects.filter(kind='instructor_verify').update(kind='teacher_verify')


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0190_class_material_oss_storage'),
    ]

    operations = [
        migrations.RenameModel(
            old_name='TeacherVerificationRequest',
            new_name='InstructorVerificationRequest',
        ),
        migrations.RenameField(
            model_name='adminnotification',
            old_name='teacher_verification',
            new_name='instructor_verification',
        ),
        migrations.AlterModelOptions(
            name='instructorverificationrequest',
            options={
                'ordering': ('-time',),
                'verbose_name': 'instructor verification request',
                'verbose_name_plural': 'instructor verification requests',
            },
        ),
        migrations.RunPython(rename_kind_forward, rename_kind_reverse),
        migrations.RunPython(rename_teachers_group_forward, rename_teachers_group_reverse),
    ]
