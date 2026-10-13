# Add AI-grading options to fill-in-the-blank config (additive, on top of the colleague's
# 0167_fill_blank_models). grading_mode selects exact vs AI scoring; ai_rubric guides the model.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0169_mcq_config_is_true_false'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemfillblankconfig',
            name='grading_mode',
            field=models.CharField(choices=[('exact', 'Exact match'), ('ai', 'AI grading')], default='exact', help_text='Exact match compares each blank to its accepted answers; AI grading asks the configured model to score the response against the rubric (falls back to exact match when no model is configured).', max_length=8, verbose_name='grading mode'),
        ),
        migrations.AddField(
            model_name='problemfillblankconfig',
            name='ai_rubric',
            field=models.TextField(blank=True, help_text='Grading guidance sent to the model (never shown to students).', verbose_name='AI rubric'),
        ),
    ]
