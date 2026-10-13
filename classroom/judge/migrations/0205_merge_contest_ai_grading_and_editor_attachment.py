# Merge develop leaf (0204) with contest AI grading (0201_contest_ai_grading).

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0201_contest_ai_grading'),
        ('judge', '0204_editor_attachment'),
    ]

    operations = []
