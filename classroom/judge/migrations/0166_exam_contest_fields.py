from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0165_mcq_config_models'),
    ]

    operations = [
        migrations.AddField(
            model_name='contest',
            name='results_finalized',
            field=models.BooleanField(default=False, help_text='Set by the "Finalize results" action when an exam-style contest ends: it reveals hidden problem results to students and folds them into the scoreboard. Leave unset while the contest is running.', verbose_name='results finalized'),
        ),
        migrations.AddField(
            model_name='contestproblem',
            name='hide_results',
            field=models.BooleanField(blank=True, default=None, help_text="Whether to hide this problem's results from students until the contest is finalized. Leave unset to use the default for the question type (hidden for multiple-choice, shown for code). Only applies to exam-style contests.", null=True, verbose_name='hide results in contest'),
        ),
        migrations.AlterField(
            model_name='contest',
            name='format_name',
            field=models.CharField(choices=[('atcoder', 'AtCoder'), ('default', 'Default'), ('ecoo', 'ECOO'), ('exam', 'Exam'), ('icpc', 'ICPC'), ('ioi', 'IOI (pre-2016)'), ('ioi16', 'IOI')], default='default', help_text='The contest format module to use.', max_length=32, verbose_name='contest format'),
        ),
    ]
