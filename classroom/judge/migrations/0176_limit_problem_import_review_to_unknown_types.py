from django.db import migrations


def keep_only_unknown_types_marked_for_review(apps, schema_editor):
    ProblemImportItem = apps.get_model('judge', 'ProblemImportItem')
    ProblemImportItem.objects.update(type_needs_review=False)
    ProblemImportItem.objects.filter(formatting_error__startswith='Question type "').update(type_needs_review=True)


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0175_mark_problem_import_errors_for_review'),
    ]

    operations = [
        migrations.RunPython(keep_only_unknown_types_marked_for_review, migrations.RunPython.noop),
    ]
