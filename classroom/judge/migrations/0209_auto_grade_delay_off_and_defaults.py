from django.db import migrations, models


def forwards_zero_to_off(apps, schema_editor):
    """Legacy 0 meant “manual only / no auto-run”; map to −1 (Off)."""
    Contest = apps.get_model('judge', 'Contest')
    Contest.objects.filter(ai_grading_auto_delay_minutes=0).update(ai_grading_auto_delay_minutes=-1)


def backwards_off_to_zero(apps, schema_editor):
    Contest = apps.get_model('judge', 'Contest')
    Contest.objects.filter(ai_grading_auto_delay_minutes__lt=0).update(ai_grading_auto_delay_minutes=0)


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0208_contest_allow_student_ai_grading'),
    ]

    operations = [
        # Delay semantics move here only (not also in 0208): signed int so −1 = Off,
        # 0 = at end time, default 120 minutes.
        migrations.AlterField(
            model_name='contest',
            name='ai_grading_auto_delay_minutes',
            field=models.IntegerField(
                default=120,
                help_text=(
                    'Minutes after the contest ends to re-grade each student’s latest '
                    'objective submissions (multiple choice, true/false, exact fill-blank). '
                    'Does not call the LLM. −1 = Off (no auto-run). 0 = run at end time. '
                    'Teachers can still use Batch grading on the Grade page anytime.'
                ),
                verbose_name='auto-grade delay after end (minutes)',
            ),
        ),
        migrations.AlterField(
            model_name='contest',
            name='allow_ai_hints',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'When this assignment is active, students can use AI Hint on every attached '
                    "problem. Each problem's own AI Hint setting is unchanged outside this assignment."
                ),
                verbose_name='allow student use AI hints',
            ),
        ),
        migrations.RunPython(forwards_zero_to_off, backwards_off_to_zero),
    ]
