from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0187_merge_class_offering_and_file_upload'),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='instructor_names',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Display names shown on course cards (comma-separated). Separate from course administrators.',
                max_length=512,
                verbose_name='instructors',
            ),
        ),
        migrations.AddField(
            model_name='class',
            name='instructor_names',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Instructor display names (comma-separated).',
                max_length=512,
                verbose_name='instructors',
            ),
        ),
        migrations.AddField(
            model_name='class',
            name='ta_names',
            field=models.CharField(
                blank=True,
                default='',
                help_text='TA display names (comma-separated).',
                max_length=512,
                verbose_name='teaching assistants',
            ),
        ),
    ]
