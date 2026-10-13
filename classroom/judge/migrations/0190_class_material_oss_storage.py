# Generated manually: class materials reuse submission_storage (OSS), same as file-upload.

from django.db import migrations, models

import judge.models.profile
import judge.storage


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0189_teacher_verification_scoped_class'),
    ]

    operations = [
        migrations.AlterField(
            model_name='classmaterial',
            name='file',
            field=models.FileField(
                blank=True,
                max_length=255,
                null=True,
                storage=judge.storage.submission_storage,
                upload_to=judge.models.profile.class_material_upload_to,
                verbose_name='file',
            ),
        ),
    ]
