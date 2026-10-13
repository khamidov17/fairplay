import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0172_problem_import_item_tags_difficulty'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProblemImportImage',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('filename', models.CharField(max_length=255)),
                ('content', models.BinaryField()),
                ('item', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name='images', to='judge.problemimportitem')),
            ],
            options={
                'verbose_name': 'problem import image',
                'verbose_name_plural': 'problem import images',
            },
        ),
    ]
