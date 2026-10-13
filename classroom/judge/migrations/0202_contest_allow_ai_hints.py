from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0201_merge_organization_instructors_and_problemcodeconfig'),
    ]

    operations = [
        migrations.AddField(
            model_name='contest',
            name='allow_ai_hints',
            field=models.BooleanField(
                default=False,
                help_text="When this assignment is active, students can use AI Hint on every attached problem. Each problem's own AI Hint setting is unchanged outside this assignment.",
                verbose_name='allow AI hints',
            ),
        ),
    ]
