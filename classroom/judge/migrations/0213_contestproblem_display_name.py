from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0212_short_answer_ai_enabled_and_disable_existing'),
    ]

    operations = [
        migrations.AddField(
            model_name='contestproblem',
            name='display_name',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='display name'),
        ),
    ]
