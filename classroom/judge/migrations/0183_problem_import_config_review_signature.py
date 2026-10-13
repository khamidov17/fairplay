from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('judge', '0182_problem_import_unrecognized_configurations'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemimportitem',
            name='config_review_signature',
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
