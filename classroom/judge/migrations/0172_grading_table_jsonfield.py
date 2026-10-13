from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('judge', '0171_alter_problem_submission_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemfillblankconfig',
            name='grading_table',
            field=models.JSONField(
                blank=True, default=None, null=True,
                verbose_name='grading table',
                help_text=(
                    'Sub-question breakdown: a list of {"name", "max_points", '
                    '"strictness": "strict|normal|lenient"} objects. '
                    'When set, the LLM grades each sub-question independently; '
                    'otherwise the answer is graded as a single whole.'
                ),
            ),
        ),
        migrations.AddField(
            model_name='problemshortanswerconfig',
            name='grading_table',
            field=models.JSONField(
                blank=True, default=None, null=True,
                verbose_name='grading table',
                help_text=(
                    'Sub-question breakdown: a list of {"name", "max_points", '
                    '"strictness": "strict|normal|lenient"} objects. '
                    'When set, the LLM grades each sub-question independently; '
                    'otherwise the answer is graded as a single whole.'
                ),
            ),
        ),
    ]
