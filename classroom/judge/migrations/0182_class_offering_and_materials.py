import re
from datetime import datetime

from django.db import migrations, models
import django.db.models.deletion

import judge.models.profile


SESSION_RE = re.compile(r'^(session|term)\s*:\s*(.+)$', re.I)
LOCATION_RE = re.compile(r'^(location|room|canvas)\s*:\s*(.+)$', re.I)
SCHEDULE_RE = re.compile(r'^(schedule|dates)\s*:\s*(.+)$', re.I)
CREDITS_RE = re.compile(r'^credits?\s*:\s*(.+)$', re.I)
MATERIALS_RE = re.compile(r'^(materials|content)\s*:\s*(.*)$', re.I)
DATE_RE = re.compile(r'(20\d{2}-\d{2}-\d{2})')


def _parse_date(text):
    match = DATE_RE.search(text or '')
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%Y-%m-%d').date()
    except ValueError:
        return None


def forwards_fill_from_description(apps, schema_editor):
    Class = apps.get_model('judge', 'Class')
    ClassMaterial = apps.get_model('judge', 'ClassMaterial')
    for cls in Class.objects.all().iterator():
        desc = cls.description or ''
        if not desc.strip():
            continue
        in_materials = False
        material_items = []
        changed = False
        for raw in desc.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
            line = raw.strip()
            if not line:
                in_materials = False
                continue
            m = SESSION_RE.match(line)
            if m:
                if not cls.term_session:
                    cls.term_session = m.group(2).strip()[:64]
                    changed = True
                in_materials = False
                continue
            m = LOCATION_RE.match(line)
            if m:
                if not cls.location:
                    cls.location = m.group(2).strip()[:255]
                    changed = True
                in_materials = False
                continue
            m = CREDITS_RE.match(line)
            if m:
                if cls.credits is None:
                    try:
                        cls.credits = m.group(1).strip().split()[0]
                        changed = True
                    except Exception:
                        pass
                in_materials = False
                continue
            m = SCHEDULE_RE.match(line)
            if m:
                value = m.group(2).strip()
                if not cls.start_date:
                    cls.start_date = _parse_date(value)
                    changed = bool(cls.start_date) or changed
                if not cls.end_date:
                    dates = DATE_RE.findall(value)
                    if len(dates) >= 2:
                        try:
                            cls.end_date = datetime.strptime(dates[1], '%Y-%m-%d').date()
                            changed = True
                        except ValueError:
                            pass
                in_materials = False
                continue
            m = MATERIALS_RE.match(line)
            if m:
                in_materials = True
                value = m.group(2).strip()
                if value:
                    material_items.append(value)
                continue
            if in_materials and (line.startswith('- ') or line.startswith('* ')):
                material_items.append(line[2:].strip())
                continue
            in_materials = False
        if changed:
            cls.save()
        for name in material_items:
            if not name:
                continue
            ClassMaterial.objects.get_or_create(
                class_obj_id=cls.id,
                parent=None,
                name=name[:255],
                is_folder=False,
                defaults={},
            )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0181_teachers_group_permissions'),
    ]

    operations = [
        migrations.AddField(
            model_name='class',
            name='academic_year',
            field=models.CharField(blank=True, default='', max_length=16, verbose_name='academic year'),
        ),
        migrations.AddField(
            model_name='class',
            name='term_session',
            field=models.CharField(blank=True, default='', max_length=64, verbose_name='term and session'),
        ),
        migrations.AddField(
            model_name='class',
            name='credits',
            field=models.DecimalField(blank=True, decimal_places=1, max_digits=4, null=True, verbose_name='credits'),
        ),
        migrations.AddField(
            model_name='class',
            name='location',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='location or Canvas link'),
        ),
        migrations.AddField(
            model_name='class',
            name='start_date',
            field=models.DateField(blank=True, null=True, verbose_name='class start'),
        ),
        migrations.AddField(
            model_name='class',
            name='end_date',
            field=models.DateField(blank=True, null=True, verbose_name='class end'),
        ),
        migrations.CreateModel(
            name='ClassMaterial',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255, verbose_name='name')),
                ('is_folder', models.BooleanField(default=False, verbose_name='is folder')),
                ('file', models.FileField(
                    blank=True, max_length=512, null=True,
                    upload_to=judge.models.profile.class_material_upload_to, verbose_name='file',
                )),
                ('created', models.DateTimeField(auto_now_add=True, verbose_name='created')),
                ('modified', models.DateTimeField(auto_now=True, verbose_name='modified')),
                ('class_obj', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='materials',
                    to='judge.class', verbose_name='class',
                )),
                ('parent', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='children', to='judge.classmaterial', verbose_name='parent folder',
                )),
                ('uploaded_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='uploaded_class_materials', to='judge.profile', verbose_name='uploaded by',
                )),
            ],
            options={
                'verbose_name': 'class material',
                'verbose_name_plural': 'class materials',
                'ordering': ['-is_folder', 'name'],
            },
        ),
        migrations.RunPython(forwards_fill_from_description, noop_reverse),
    ]
