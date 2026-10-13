from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0167_fill_blank_models'),
    ]

    operations = [
        migrations.CreateModel(
            name='StoredFileRecord',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('path', models.CharField(max_length=255, unique=True, verbose_name='storage path')),
                ('sha256', models.CharField(max_length=64, verbose_name='SHA-256')),
                ('size', models.PositiveBigIntegerField(verbose_name='size (bytes)')),
                ('status', models.CharField(choices=[('P', 'Pending mirror'), ('M', 'Mirrored'), ('F', 'Mirror failed'), ('D', 'Deleted from primary')], default='P', max_length=1, verbose_name='status')),
                ('created_on', models.DateTimeField(auto_now_add=True, verbose_name='created on')),
                ('mirrored_at', models.DateTimeField(blank=True, null=True, verbose_name='mirrored at')),
                ('attempts', models.PositiveIntegerField(default=0, verbose_name='mirror attempts')),
                ('last_error', models.TextField(blank=True, verbose_name='last error')),
            ],
            options={
                'verbose_name': 'stored file record',
                'verbose_name_plural': 'stored file records',
                'indexes': [models.Index(fields=['status', 'created_on'], name='judge_store_status_1eb083_idx')],
            },
        ),
    ]
