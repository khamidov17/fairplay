from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0196_move_seb_configs_to_oss'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemmcqconfig',
            name='scoring_mode',
            field=models.CharField(
                choices=[('proportional', 'Proportional credit'), ('underselect', 'Under-select credit')],
                default='proportional',
                help_text='How a multiple-select question awards partial credit. Ignored for single-answer questions.',
                max_length=16,
                verbose_name='scoring method',
            ),
        ),
        migrations.AddField(
            model_name='problemmcqconfig',
            name='underselect_points',
            field=models.FloatField(
                blank=True,
                default=None,
                help_text='In under-select mode, points awarded for each correctly selected option. '
                          'Leave empty to split the question points evenly among correct options.',
                null=True,
                verbose_name='points per correct choice',
            ),
        ),
        migrations.AddField(
            model_name='problemblank',
            name='points',
            field=models.FloatField(
                default=1,
                help_text='Points awarded when this blank is correct. Used when the problem allows partial credit.',
                verbose_name='points',
            ),
        ),
    ]
