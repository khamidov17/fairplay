# Generated manually: .seb configs reuse submission_storage (OSS), same as file-upload.

from django.db import migrations, models

import judge.models.contest
import judge.models.seb
import judge.storage


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
                upload_to=judge.models.contest.seb_contest_config_upload_to,
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
                upload_to=judge.models.seb.seb_preset_config_upload_to,
                verbose_name='SEB config file (.seb)',
            ),
        ),
    ]
