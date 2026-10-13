# Generated manually for Organization.instructors M2M.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0193_instructor_verification_kind'),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='instructors',
            field=models.ManyToManyField(
                blank=True,
                help_text='Teachers associated with this course (auto-added when a site teacher joins).',
                related_name='instructor_of',
                to='judge.profile',
                verbose_name='course instructors',
            ),
        ),
    ]
