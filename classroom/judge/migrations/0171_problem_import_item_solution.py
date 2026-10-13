from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0170_problem_import_item_test_data'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemimportitem',
            name='solution',
            field=models.TextField(blank=True),
        ),
    ]
