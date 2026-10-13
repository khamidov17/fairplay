# Files attached from the rich-text editor reuse submission_storage (OSS).

import uuid

from django.db import migrations, models
import django.db.models.deletion

import judge.models.editor_attachment
import judge.storage


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0203_merge_instructor_create_assignment_and_contest_allow_ai_hints'),
    ]

    operations = [
        migrations.CreateModel(
            name='EditorAttachment',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name='uuid')),
                ('original_name', models.CharField(max_length=255, verbose_name='original filename')),
                ('file', models.FileField(
                    max_length=255,
                    storage=judge.storage.submission_storage,
                    upload_to=judge.models.editor_attachment.editor_attachment_upload_to,
                    verbose_name='file',
                )),
                ('size', models.PositiveBigIntegerField(default=0, verbose_name='file size')),
                ('created', models.DateTimeField(auto_now_add=True, verbose_name='created')),
                ('uploaded_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='editor_attachments',
                    to='judge.profile',
                    verbose_name='uploaded by',
                )),
            ],
            options={
                'verbose_name': 'editor attachment',
                'verbose_name_plural': 'editor attachments',
            },
        ),
    ]
