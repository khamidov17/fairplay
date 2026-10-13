# Generated manually to merge the submission-file storage migrations with the
# short-answer / problem-import branches already merged into develop.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0169_stored_file_abandoned_status'),
        ('judge', '0178_merge_short_answer_and_problem_import'),
    ]

    operations = [
    ]
