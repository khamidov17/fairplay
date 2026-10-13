# Multiple-choice question models: per-problem config and its options.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0164_merge_20260626_1436'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProblemMCQConfig',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('multiple_select', models.BooleanField(default=False, help_text='If set, students may select more than one choice (checkboxes); otherwise exactly one (radio).', verbose_name='allow multiple answers')),
                ('shuffle_options', models.BooleanField(default=False, help_text='Present choices in a different, stable order for each student to deter copying.', verbose_name='shuffle options per student')),
                ('problem', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='mcq_config', to='judge.problem', verbose_name='problem')),
            ],
            options={
                'verbose_name': 'MCQ configuration',
                'verbose_name_plural': 'MCQ configurations',
            },
        ),
        migrations.CreateModel(
            name='ProblemMCQOption',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order', models.PositiveIntegerField(verbose_name='order')),
                ('text', models.TextField(verbose_name='choice text')),
                ('is_correct', models.BooleanField(default=False, verbose_name='is correct')),
                ('problem', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='mcq_options', to='judge.problem', verbose_name='problem')),
            ],
            options={
                'verbose_name': 'MCQ option',
                'verbose_name_plural': 'MCQ options',
                'ordering': ('order',),
                'unique_together': {('problem', 'order')},
            },
        ),
    ]
