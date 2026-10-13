# Generated manually: .seb configs reuse submission_storage (OSS), same as file-upload.

from django.db import migrations, models

import judge.storage


# Safe Exam Browser support was removed later (0223_remove_seb_and_exam_lock); these upload paths are kept
# here, frozen, so the historical migration still loads. They only matter for a database that
# is migrated through this step.
def seb_contest_config_upload_to(instance, filename):
    return _seb_config_key('contest-%s' % (instance.pk or 0), filename)


def seb_preset_config_upload_to(instance, filename):
    return _seb_config_key('preset', filename)


def _seb_config_key(scope, original_name):
    import posixpath
    import re
    import uuid
    from django.utils import timezone
    now = timezone.now()
    ext = posixpath.splitext(original_name)[1].lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,10}', ext):
        ext = ''
    scope = re.sub(r'[^a-z0-9_-]', '', str(scope).lower())[:40] or 'misc'
    return 'seb_configs/%04d/%02d/%s/%s%s' % (now.year, now.month, scope, uuid.uuid4().hex, ext)


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0194_instructor_seb_permission'),
    ]

    operations = [
        migrations.AlterField(
            model_name='contest',
            name='seb_config_file',
            field=models.FileField(
                blank=True,
                help_text='The .seb file students download or launch via sebs:// to enter this exam.',
                max_length=255,
                null=True,
                storage=judge.storage.submission_storage,
                upload_to=seb_contest_config_upload_to,
                verbose_name='SEB config file (.seb)',
            ),
        ),
        migrations.AlterField(
            model_name='sebconfigpreset',
            name='config_file',
            field=models.FileField(
                help_text="Built in the SEB Configuration Tool. Set the Start URL to this site's home page so one file serves every exam.",
                max_length=255,
                storage=judge.storage.submission_storage,
                upload_to=seb_preset_config_upload_to,
                verbose_name='SEB config file (.seb)',
            ),
        ),
    ]
