from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0213_contestproblem_display_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='contest',
            name='hide_sandbox_output',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Hide sandbox stdout from students. The verdict, compile errors, and '
                    'runtime exceptions remain visible. This does not close the IDE, lock '
                    'problems, or change Submit.'
                ),
                verbose_name='Hide IDE / Run Test output',
            ),
        ),
    ]
