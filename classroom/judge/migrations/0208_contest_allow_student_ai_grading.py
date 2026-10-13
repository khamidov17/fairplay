from django.db import migrations, models


def forwards_allow_student_from_mode(apps, schema_editor):
    Contest = apps.get_model('judge', 'Contest')
    Contest.objects.filter(ai_grading_mode='realtime').update(allow_student_ai_grading=True)


def backwards_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0207_merge_manual_grade_and_ai_grading'),
    ]

    operations = [
        migrations.AddField(
            model_name='contest',
            name='allow_student_ai_grading',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'When enabled, students can request AI grading on the submission '
                    'page (subject to quota). Teachers can always run AI grading from '
                    'the Grade page or submission page.'
                ),
                verbose_name='allow student use AI grading',
            ),
        ),
        migrations.AlterField(
            model_name='contest',
            name='ai_grading_mode',
            field=models.CharField(
                choices=[
                    ('off', 'Off'),
                    ('realtime', 'On demand — student requests AI grading'),
                    ('batch', 'Batch — latest submits after the contest'),
                ],
                default='off',
                help_text=(
                    'Legacy field kept for older assignments. Prefer '
                    '“allow student AI grading”. Synced from that checkbox on save: '
                    'on → on-demand, off → off. Teachers can always run AI from the '
                    'Grade page regardless of this value.'
                ),
                max_length=16,
                verbose_name='AI grading (legacy mode)',
            ),
        ),
        migrations.RunPython(forwards_allow_student_from_mode, backwards_noop),
    ]
