# Merge the develop-side Organization.instructors/ProblemCodeConfig leaf
# with the grade_fix manual-grade leaf.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0201_merge_organization_instructors_and_problemcodeconfig'),
        ('judge', '0203_merge_20260817_1744'),
    ]

    operations = [
    ]
