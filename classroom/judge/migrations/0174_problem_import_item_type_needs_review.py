from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0173_problem_import_image'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemimportitem',
            name='type_needs_review',
            field=models.BooleanField(default=False),
        ),
    ]
