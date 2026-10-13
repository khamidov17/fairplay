# Generated manually to join develop (drop requires_manual_review) with
# qixuan-ai-grading-ux (student AI grading + auto-grade delay Off=-1).

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0208_drop_requires_manual_review'),
        ('judge', '0209_auto_grade_delay_off_and_defaults'),
    ]

    operations = [
    ]
