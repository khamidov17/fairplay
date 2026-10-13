# Generated manually for scoped teacher verification (course + class).

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0188_course_class_instructor_names'),
    ]

    operations = [
        migrations.AddField(
            model_name='teacherverificationrequest',
            name='organization',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='teacher_verification_requests',
                to='judge.organization',
                verbose_name='course',
            ),
        ),
        migrations.AddField(
            model_name='teacherverificationrequest',
            name='request_class',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='teacher_verification_requests',
                to='judge.class',
                verbose_name='class',
            ),
        ),
    ]
