from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0169_problem_import_item_allowed_languages'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemimportitem',
            name='test_data',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='problemimportitem',
            name='test_data_zip',
            field=models.FileField(blank=True, upload_to='problem_import_test_data/'),
        ),
    ]
