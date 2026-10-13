import logging

from django.db import migrations, models

import judge.models.question
import judge.storage

logger = logging.getLogger('judge.migrations')

LEGACY_PREFIX = 'problem_import_test_data/'


def move_legacy_zips(apps, schema_editor):
    """Move draft test-data ZIPs out of public MEDIA into the private store (AUTH-03).

    ZIPs of items whose problem already exists are not needed any more and are deleted.
    """
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    from judge.storage import generate_problem_import_key, submission_storage

    ProblemImportItem = apps.get_model('judge', 'ProblemImportItem')
    for item in ProblemImportItem.objects.exclude(test_data_zip='').iterator():
        old_name = item.test_data_zip.name
        if not old_name.startswith(LEGACY_PREFIX):
            continue
        new_name = ''
        try:
            if item.created_problem_id is None and default_storage.exists(old_name):
                with default_storage.open(old_name, 'rb') as f:
                    content = f.read()
                new_name = submission_storage().save(
                    generate_problem_import_key(item.draft_id, old_name), ContentFile(content))
            ProblemImportItem.objects.filter(pk=item.pk).update(test_data_zip=new_name)
            if default_storage.exists(old_name):
                default_storage.delete(old_name)
        except Exception:
            logger.exception('Could not move import test data %s', old_name)


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0219_contest_allow_spectate'),
    ]

    operations = [
        migrations.AlterField(
            model_name='problemimportitem',
            name='test_data_zip',
            field=models.FileField(
                blank=True,
                max_length=255,
                storage=judge.storage.submission_storage,
                upload_to=judge.models.question.problem_import_test_data_upload_to,
            ),
        ),
        migrations.RunPython(move_legacy_zips, migrations.RunPython.noop),
    ]
