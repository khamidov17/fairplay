from django.db import migrations


class Migration(migrations.Migration):
    """Retire ``requires_manual_review``.

    The "keep AI-graded submissions pending until a teacher reviews them" flag
    was superseded by the Grade desk: teachers now open the desk directly
    instead of relying on a per-problem pending queue. The Create V2 editor
    stopped writing the field, so this drops the now-unread columns.
    """

    dependencies = [
        ('judge', '0207_merge_manual_grade_and_ai_grading'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='problemfillblankconfig',
            name='requires_manual_review',
        ),
        migrations.RemoveField(
            model_name='problemshortanswerconfig',
            name='requires_manual_review',
        ),
    ]
