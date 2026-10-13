from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0167_fill_blank_models'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProblemImportDraft',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='problem_import_drafts', to='judge.profile')),
            ],
            options={
                'ordering': ('-updated_at',),
                'verbose_name': 'problem import draft',
                'verbose_name_plural': 'problem import drafts',
            },
        ),
        migrations.CreateModel(
            name='ProblemImportItem',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('row_number', models.PositiveIntegerField()),
                ('name', models.CharField(max_length=200)),
                ('submission_type', models.CharField(choices=[('C', 'Code'), ('M', 'Multiple choice'), ('B', 'Fill in the blank')], max_length=1)),
                ('description', models.TextField(blank=True)),
                ('answer', models.TextField(blank=True)),
                ('source_label', models.CharField(blank=True, max_length=255)),
                ('options', models.TextField(blank=True)),
                ('correct_options', models.TextField(blank=True)),
                ('blanks', models.TextField(blank=True)),
                ('formatting_error', models.TextField(blank=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_problem', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='judge.problem')),
                ('draft', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='judge.problemimportdraft')),
            ],
            options={
                'ordering': ('row_number', 'pk'),
                'verbose_name': 'problem import item',
                'verbose_name_plural': 'problem import items',
            },
        ),
    ]
