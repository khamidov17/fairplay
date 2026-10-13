from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0168_problem_import_drafts'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemimportitem',
            name='allowed_languages',
            field=models.TextField(blank=True),
        ),
    ]
