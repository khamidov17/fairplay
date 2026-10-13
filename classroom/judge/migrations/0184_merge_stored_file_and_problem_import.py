# Generated manually to merge the submission-file storage line with the problem-import migrations
# develop grew after 0178 (0179_problem_import_short_answer .. 0183_problem_import_config_review_signature).
# Both lines branch off 0178, so the graph had two leaves; the two lines touch disjoint models
# (StoredFileRecord vs ProblemImportItem), so no operations are needed to reconcile them.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0179_merge_stored_file'),
        ('judge', '0183_problem_import_config_review_signature'),
    ]

    operations = [
    ]
