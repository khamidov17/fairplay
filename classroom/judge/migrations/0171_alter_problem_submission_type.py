# Record the 'S' (short answer) choice added to Problem.submission_type. This is a choices-only
# change on a max_length=1 CharField, so it does not alter the database column; it only keeps the
# migration state in sync with the model (silences the "changes not yet reflected" warning).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0170_fill_blank_ai_fields'),
    ]

    operations = [
        migrations.AlterField(
            model_name='problem',
            name='submission_type',
            field=models.CharField(choices=[('C', 'Code'), ('M', 'Multiple choice'), ('B', 'Fill in the blank'), ('S', 'Short answer'), ('F', 'File upload')], db_index=True, default='C', help_text='How this problem is answered and graded. Code problems route to the judge; other types are graded internally.', max_length=1, verbose_name='submission type'),
        ),
    ]
