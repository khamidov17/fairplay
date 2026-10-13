from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('judge', '0180_problem_import_item_editor_data'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemimportitem',
            name='data_load_error',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='problemimportitem',
            name='data_needs_review',
            field=models.BooleanField(default=False),
        ),
    ]
