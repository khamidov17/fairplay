from django.db import migrations

OLD_NAMES = ('DMOJ: Modern Online Judge', 'DMOJ')
NEW_NAME = 'FairPlay Classroom'


def rename_site(apps, schema_editor):
    # Only the untouched DMOJ default is renamed; a name an admin chose is kept.
    Site = apps.get_model('sites', 'Site')
    Site.objects.filter(name__in=OLD_NAMES).update(name=NEW_NAME)


def restore_site(apps, schema_editor):
    Site = apps.get_model('sites', 'Site')
    Site.objects.filter(name=NEW_NAME).update(name=OLD_NAMES[0])


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0225_default_time_zone_shanghai'),
        ('sites', '0002_alter_domain_unique'),
    ]

    operations = [
        migrations.RunPython(rename_site, restore_site),
    ]
