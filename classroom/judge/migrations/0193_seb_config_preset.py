# Staff-published SEB configurations instructors can adopt with one checkbox.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0192_instructor_verification_granted_flags'),
    ]

    operations = [
        migrations.CreateModel(
            name='SEBConfigPreset',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text='Shown to instructors when they pick this configuration, e.g. "Standard exam lockdown".', max_length=100, unique=True, verbose_name='name')),
                ('description', models.TextField(blank=True, default='', help_text='What this configuration locks down. Shown to instructors under the name.', verbose_name='description')),
                ('config_file', models.FileField(help_text="Built in the SEB Configuration Tool. Set the Start URL to this site's home page so one file serves every exam.", upload_to='seb_configs/presets/', verbose_name='SEB config file (.seb)')),
                ('browser_exam_keys', models.TextField(blank=True, default='', help_text='One 64-char hex hash per line, copied from the SEB Configuration Tool — one per supported platform/version. Lines may include trailing comments after the hash.', verbose_name='Browser Exam Key hashes')),
                ('config_keys', models.TextField(blank=True, default='', help_text='One 64-char hex hash per line. Required for macOS clients (and any newer build that sends only the Config Key).', verbose_name='Config Key hashes')),
                ('quit_url', models.URLField(blank=True, default='', help_text='Must match the Quit URL set inside the .seb file. Students land here after their final submission so SEB auto-closes. Leave blank to disable.', verbose_name='quit URL')),
                ('is_default', models.BooleanField(default=False, help_text='The configuration instructors get from the "use the default configuration" checkbox. Marking one default un-marks any other.', verbose_name='site default')),
                ('is_active', models.BooleanField(default=True, help_text='Uncheck to retire this configuration. Contests already using it keep working; it just stops being offered as the default.', verbose_name='active')),
                ('updated_on', models.DateTimeField(auto_now=True, verbose_name='last updated')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='judge.profile', verbose_name='last updated by')),
            ],
            options={
                'verbose_name': 'SEB config preset',
                'verbose_name_plural': 'SEB config presets',
                'ordering': ['-is_default', 'name'],
            },
        ),
        migrations.AddField(
            model_name='contest',
            name='seb_preset',
            field=models.ForeignKey(blank=True, help_text='Inherit the .seb file, key hashes and quit URL from a staff-published configuration. Anything filled in above overrides the preset for this contest.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='contests', to='judge.sebconfigpreset', verbose_name='SEB config preset'),
        ),
    ]
