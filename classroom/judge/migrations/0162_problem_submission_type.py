# Multi-question-type foundation: the submission_type discriminator and the sentinel
# "ANSWER" language used by internally-graded (non-code) submissions.

from django.db import migrations, models


def create_answer_language(apps, schema_editor):
    Language = apps.get_model('judge', 'Language')
    Language.objects.get_or_create(
        key='ANSWER',
        defaults={'name': 'Answer', 'short_name': 'ANS', 'common_name': 'Answer',
                  'ace': '', 'pygments': 'text', 'extension': 'txt'},
    )


def delete_answer_language(apps, schema_editor):
    Language = apps.get_model('judge', 'Language')
    Language.objects.filter(key='ANSWER').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0161_aihintlog'),
    ]

    operations = [
        migrations.AddField(
            model_name='problem',
            name='submission_type',
            field=models.CharField(choices=[('C', 'Code'), ('M', 'Multiple choice'), ('B', 'Fill in the blank'), ('F', 'File upload')], db_index=True, default='C', help_text='How this problem is answered and graded. Code problems route to the judge; other types are graded internally.', max_length=1, verbose_name='submission type'),
        ),
        migrations.RunPython(create_answer_language, delete_answer_language),
    ]
