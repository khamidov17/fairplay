from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0176_limit_problem_import_review_to_unknown_types'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemimportitem',
            name='name_needs_review',
            field=models.BooleanField(default=False),
        ),
    ]
