# Merge the grade_fix branch's allow_ai_hints leaf with the develop-side
# Organization.instructors / manual-grade leaf.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0202_contest_allow_ai_hints'),
        ('judge', '0204_merge_organization_instructors_and_manual_grade'),
    ]

    operations = [
    ]
