from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('judge', '0179_problem_import_short_answer'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemimportitem',
            name='editor_data',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
