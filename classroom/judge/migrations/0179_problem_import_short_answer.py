from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0178_merge_short_answer_and_problem_import'),
    ]

    operations = [
        migrations.AlterField(
            model_name='problemimportitem',
            name='submission_type',
            field=models.CharField(
                choices=[
                    ('C', 'Code'), ('M', 'Multiple choice'), ('B', 'Fill in the blank'), ('S', 'Short answer'),
                ],
                max_length=1,
            ),
        ),
        migrations.AddField(
            model_name='problemimportitem', name='grading_table', field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='problemimportitem', name='is_true_false', field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='problemimportitem', name='rubric', field=models.TextField(blank=True),
        ),
    ]
