# Generated manually for site-wide teacher vs class-instructor verification.

from django.db import migrations, models


def classify_existing_requests(apps, schema_editor):
    Request = apps.get_model('judge', 'InstructorVerificationRequest')
    Request.objects.filter(request_class__isnull=True).update(kind='teacher')
    Request.objects.filter(request_class__isnull=False).update(kind='class_instructor')


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0192_instructor_verification_granted_flags'),
    ]

    operations = [
        migrations.AddField(
            model_name='instructorverificationrequest',
            name='kind',
            field=models.CharField(
                choices=[
                    ('teacher', 'Teacher (site-wide)'),
                    ('class_instructor', 'Class instructor'),
                ],
                db_index=True,
                default='class_instructor',
                max_length=32,
                verbose_name='kind',
            ),
        ),
        migrations.AddField(
            model_name='instructorverificationrequest',
            name='granted_instructors_group',
            field=models.BooleanField(
                default=False,
                help_text='True if approval added the user to the site-wide Instructors group.',
                verbose_name='granted Instructors group',
            ),
        ),
        migrations.RunPython(classify_existing_requests, noop_reverse),
    ]
