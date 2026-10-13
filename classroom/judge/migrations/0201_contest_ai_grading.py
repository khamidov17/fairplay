# Merge qixuan Organization.instructors leaf with develop ProblemCodeConfig
# leaf, and add contest-level AI grading mode.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0194_organization_instructors'),
        ('judge', '0200_problemcodeconfig'),
    ]

    operations = [
        migrations.AddField(
            model_name='contest',
            name='ai_grading_mode',
            field=models.CharField(
                choices=[
                    ('off', 'Off'),
                    ('realtime', 'Realtime (grade each submit)'),
                    ('batch', 'Batch (latest submits after the contest)'),
                ],
                default='off',
                help_text='When AI-enabled problems in this assignment/exam are graded. '
                          'Always uses each student’s latest submission per problem. '
                          'Realtime grades a submit as it arrives (a newer submit cancels '
                          'one still queued). Batch waits for the Grade-page button, or '
                          'for the delay after the contest ends.',
                max_length=16,
                verbose_name='AI grading',
            ),
        ),
        migrations.AddField(
            model_name='contest',
            name='ai_grading_auto_delay_minutes',
            field=models.PositiveIntegerField(
                default=30,
                help_text='For batch mode: automatically grade latest submissions this many '
                          'minutes after the contest ends. 0 disables auto-run (manual button only).',
                verbose_name='batch AI delay after end (minutes)',
            ),
        ),
        migrations.AddField(
            model_name='contest',
            name='ai_grading_scheduled_task_id',
            field=models.CharField(
                blank=True, default='', max_length=255,
                verbose_name='scheduled batch AI task id',
            ),
        ),
        migrations.AddField(
            model_name='contest',
            name='ai_grading_batch_started_at',
            field=models.DateTimeField(
                blank=True, null=True, verbose_name='batch AI started',
            ),
        ),
        migrations.AddField(
            model_name='contest',
            name='ai_grading_batch_finished_at',
            field=models.DateTimeField(
                blank=True, null=True, verbose_name='batch AI finished',
            ),
        ),
    ]
