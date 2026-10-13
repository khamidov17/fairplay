# Short-answer problems get an explicit AI-grading toggle (matching code / fill-blank).
# The data migration turns the toggle off on every existing problem that currently
# has AI grading enabled. Historical submission scores (SubmissionTestCase rows,
# submission.ai_points) are left untouched.

from django.db import migrations, models


def disable_existing_problem_ai_grading(apps, schema_editor):
    ProblemCodeConfig = apps.get_model('judge', 'ProblemCodeConfig')
    ProblemFillBlankConfig = apps.get_model('judge', 'ProblemFillBlankConfig')
    ProblemShortAnswerConfig = apps.get_model('judge', 'ProblemShortAnswerConfig')
    ProblemCodeConfig.objects.filter(ai_enabled=True).update(ai_enabled=False)
    ProblemFillBlankConfig.objects.filter(grading_mode='ai').update(grading_mode='exact')
    ProblemShortAnswerConfig.objects.filter(ai_enabled=True).update(ai_enabled=False)


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0211_merge_20260821_0317'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemshortanswerconfig',
            name='ai_enabled',
            field=models.BooleanField(
                default=False,
                help_text='Ask the configured model to score the answer against the rubric. '
                          'When off, the submission is saved for manual grading and the LLM is not called.',
                verbose_name='enable AI grading',
            ),
        ),
        migrations.RunPython(
            disable_existing_problem_ai_grading,
            migrations.RunPython.noop,
        ),
    ]
