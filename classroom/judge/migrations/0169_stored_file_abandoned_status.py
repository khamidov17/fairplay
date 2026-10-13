from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0168_stored_file_record'),
    ]

    operations = [
        migrations.AlterField(
            model_name='storedfilerecord',
            name='status',
            field=models.CharField(choices=[('P', 'Pending mirror'), ('M', 'Mirrored'), ('F', 'Mirror failed'), ('D', 'Deleted from primary'), ('A', 'Abandoned (needs manual action)')], default='P', max_length=1, verbose_name='status'),
        ),
    ]
