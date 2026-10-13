# Merge qixuan Organization.instructors leaf with develop ProblemCodeConfig leaf.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0194_organization_instructors'),
        ('judge', '0200_problemcodeconfig'),
    ]

    operations = [
    ]
