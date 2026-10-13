# Merge courses instructor-assignment leaf with assignment allow_ai_hints leaf.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0201_instructor_create_assignment'),
        ('judge', '0202_contest_allow_ai_hints'),
    ]

    operations = [
    ]
