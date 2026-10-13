import django.db.models.deletion
from django.db import migrations, models

import judge.models.question
import judge.storage


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0179_merge_stored_file'),
    ]

    operations = [
        migrations.CreateModel(
            name='SubmissionFile',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.FileField(max_length=255, storage=judge.storage.submission_storage, upload_to=judge.models.question.submission_file_path, verbose_name='uploaded file')),
                ('original_name', models.CharField(max_length=255, verbose_name='original filename')),
                ('size', models.PositiveBigIntegerField(default=0, verbose_name='file size')),
                ('uploaded_on', models.DateTimeField(auto_now_add=True, verbose_name='uploaded on')),
                ('submission', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='upload', to='judge.submission', verbose_name='submission')),
            ],
            options={
                'verbose_name': 'submission file',
                'verbose_name_plural': 'submission files',
            },
        ),
        migrations.CreateModel(
            name='ProblemFileConfig',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('allowed_extensions', models.CharField(blank=True, help_text='Comma-separated extensions without dots, e.g. "pdf,zip,docx". Leave blank to allow any extension.', max_length=255, verbose_name='allowed file extensions')),
                ('max_file_size', models.PositiveBigIntegerField(default=20971520, help_text='Reject uploads larger than this many bytes.', verbose_name='maximum file size (bytes)')),
                ('instructions', models.TextField(blank=True, help_text='Optional guidance shown to students on the submit page.', verbose_name='submission instructions')),
                ('problem', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='file_config', to='judge.problem', verbose_name='problem')),
            ],
            options={
                'verbose_name': 'file-upload configuration',
                'verbose_name_plural': 'file-upload configurations',
            },
        ),
    ]
