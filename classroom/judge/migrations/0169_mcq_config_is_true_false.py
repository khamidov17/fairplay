# Mark MCQ configs that represent true/false questions so the list can label them distinctly.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0168_short_answer_models'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemmcqconfig',
            name='is_true_false',
            field=models.BooleanField(default=False, help_text='Marks this MCQ as a true/false question so it is labelled accordingly.', verbose_name='true/false question'),
        ),
    ]
