# Generated manually to merge the programming-label alter onto develop's file-upload
# merge line. After merging develop into feature/problem-pages-redesign the graph had
# two leaves:
#   - 0184_alter_submission_type_programming_label (this branch: Code -> Programming)
#   - 0185_merge_file_upload (develop: file-upload models merge)
# Those lines only touch disjoint concerns (choice labels vs SubmissionFile /
# ProblemFileConfig), so no schema operations are needed — this empty merge
# migration just reunites the heads (same pattern as 0185_merge_file_upload).

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0184_alter_submission_type_programming_label'),
        ('judge', '0185_merge_file_upload'),
    ]

    operations = [
    ]
