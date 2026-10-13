from django.db import migrations


def mark_existing_errors_for_review(apps, schema_editor):
    ProblemImportItem = apps.get_model('judge', 'ProblemImportItem')
    ProblemImportItem.objects.exclude(formatting_error='').update(type_needs_review=True)


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0174_problem_import_item_type_needs_review'),
    ]

    operations = [
        migrations.RunPython(mark_existing_errors_for_review, migrations.RunPython.noop),
    ]
