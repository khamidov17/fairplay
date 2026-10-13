# Generated manually to merge the file-upload models onto the storage / problem-import line that
# 0184 reconciled. 0180_file_upload_models branched off 0179_merge_stored_file before develop's
# problem-import migrations were merged in, so the graph had two leaves; the two lines touch
# disjoint models (SubmissionFile / ProblemFileConfig vs ProblemImportItem), so no operations
# are needed to reconcile them.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0180_file_upload_models'),
        ('judge', '0184_merge_stored_file_and_problem_import'),
    ]

    operations = [
    ]
