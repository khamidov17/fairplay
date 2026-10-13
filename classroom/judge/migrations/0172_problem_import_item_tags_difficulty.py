from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0171_problem_import_item_solution'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemimportitem',
            name='difficulty',
            field=models.CharField(blank=True, max_length=10),
        ),
        migrations.AddField(
            model_name='problemimportitem',
            name='tags',
            field=models.TextField(blank=True),
        ),
    ]
