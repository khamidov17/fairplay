from django.core.management.base import BaseCommand, CommandError
from django.db.migrations.loader import MigrationLoader


class Command(BaseCommand):
    help = ('checks that every app has a single migration leaf, so that migrate does not abort with '
            '"Conflicting migrations detected"')

    def add_arguments(self, parser):
        parser.add_argument('app_label', nargs='*', help='app labels to check; defaults to every app')

    def handle(self, *args, **options):
        # connection=None loads the graph from disk only. This check must not need a database:
        # it runs in CI before the test database exists, and its whole job is to catch a bad
        # graph before anything tries to apply it.
        loader = MigrationLoader(None, ignore_no_migrations=True)
        conflicts = loader.detect_conflicts()

        app_labels = set(options['app_label'])
        if app_labels:
            conflicts = {app: names for app, names in conflicts.items() if app in app_labels}

        if conflicts:
            raise CommandError(
                'Conflicting migrations detected; multiple leaf nodes in the migration graph:\n' +
                '\n'.join(
                    '  %s: %s' % (app, ', '.join(sorted(names)))
                    for app, names in sorted(conflicts.items())
                ) +
                '\nTwo branches added migrations off the same parent. Merge develop into your branch and run '
                "'python manage.py makemigrations --merge', then rename the result to "
                'NNNN_merge_<line_a>_and_<line_b>.py.',
            )

        for app_label in sorted(app_labels or loader.migrated_apps):
            leaf = loader.graph.leaf_nodes(app_label)
            self.stdout.write('%s: single leaf %s' % (app_label, leaf[0][1] if leaf else '(none)'))
