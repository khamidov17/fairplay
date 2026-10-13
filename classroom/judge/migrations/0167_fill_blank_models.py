import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0166_exam_contest_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProblemFillBlankConfig',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('case_sensitive', models.BooleanField(default=False, help_text='If set, answers must match the exact letter case; otherwise case is ignored.', verbose_name='case sensitive')),
                ('trim_whitespace', models.BooleanField(default=True, help_text='If set, leading and trailing whitespace in the student answer is ignored.', verbose_name='trim surrounding whitespace')),
                ('problem', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='fill_blank_config', to='judge.problem', verbose_name='problem')),
            ],
            options={
                'verbose_name': 'fill-in-the-blank configuration',
                'verbose_name_plural': 'fill-in-the-blank configurations',
            },
        ),
        migrations.CreateModel(
            name='ProblemBlank',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order', models.PositiveIntegerField(verbose_name='order')),
                ('answers', models.TextField(help_text='One accepted answer per line; any of them counts as correct.', verbose_name='accepted answers')),
                ('label', models.CharField(blank=True, help_text='Optional label shown next to the input; defaults to "Blank N".', max_length=100, verbose_name='label')),
                ('problem', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='blanks', to='judge.problem', verbose_name='problem')),
            ],
            options={
                'verbose_name': 'fill-in-the-blank blank',
                'verbose_name_plural': 'fill-in-the-blank blanks',
                'ordering': ('order',),
                'unique_together': {('problem', 'order')},
            },
        ),
    ]
