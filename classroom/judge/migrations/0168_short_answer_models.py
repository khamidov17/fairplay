# Short-answer question model: per-problem reference answer + AI rubric.
# Chained after 0167_fill_blank_models (the colleague's fill-in-the-blank models) so both the
# fill-blank and short-answer tables live on a single linear migration history.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0167_fill_blank_models'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProblemShortAnswerConfig',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('reference_answer', models.TextField(blank=True, help_text='Model answer the response is compared against (never shown to students).', verbose_name='reference answer')),
                ('ai_rubric', models.TextField(blank=True, help_text='Grading guidance sent to the model (never shown to students).', verbose_name='AI rubric')),
                ('problem', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='short_answer_config', to='judge.problem', verbose_name='problem')),
            ],
            options={
                'verbose_name': 'short-answer configuration',
                'verbose_name_plural': 'short-answer configurations',
            },
        ),
    ]
