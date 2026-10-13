from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('judge', '0181_problem_import_data_review'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemimportitem',
            name='config_needs_review',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='problemimportitem',
            name='unrecognized_configurations',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
