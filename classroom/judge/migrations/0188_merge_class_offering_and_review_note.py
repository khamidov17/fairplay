# Merge migration: converges the two 0187 branches onto a single leaf.
#
# origin/develop's class-offering × file-upload merge
# (0187_merge_class_offering_and_file_upload) and this branch's already-shipped
# submission review-note migration (0187_submission_review_note_...) both fork
# off 0186. A migration graph must have exactly one leaf, so this empty
# migration depends on both — it changes nothing in the database and is safe to
# apply on any environment regardless of which branch was deployed first.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0187_merge_class_offering_and_file_upload'),
        ('judge', '0187_submission_review_note_submission_reviewed_at_and_more'),
    ]

    operations = [
    ]
