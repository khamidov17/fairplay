"""Attach Easy/Medium/Hard to problems that have no difficulty type.

Uses the same classifier as the add-problem 🤖 AI button (LLM when configured,
length heuristic otherwise). Reads the local problem table so private/org
problems are included — this is not a scrape of the public /problems/ page.

Works on production ``main`` images (view._classify) and on this branch
(classify_problem_difficulty).
"""
from django.core.management.base import BaseCommand
from django.db.models import Prefetch

from judge.models import Problem, ProblemType


def problems_missing_difficulty(codes=None):
    tagged_ids = Problem.objects.filter(
        types__in=ProblemType.objects.filter(Problem.difficulty_type_q()),
    ).values_list('id', flat=True)
    qs = Problem.objects.exclude(id__in=tagged_ids).order_by('id')
    if codes:
        qs = qs.filter(code__in=codes)
    return qs.prefetch_related(Prefetch('types'))


def apply_difficulty(problem, level):
    from judge.views.problem_create_v2 import ProblemCreateV2View

    ProblemCreateV2View()._attach_types(problem, {'difficulty': level})
    problem.__dict__.pop('difficulty', None)
    problem.__dict__.pop('difficulty_label', None)


def classify_text(description):
    """Same classifier as the add-problem 🤖 AI button."""
    from judge.views import problem_create_v2 as v2

    fn = getattr(v2, 'classify_problem_difficulty', None)
    if fn is not None:
        return fn(description)
    return v2.ProblemCreateV2AIDifficultyView()._classify(description)


class Command(BaseCommand):
    help = 'Tag problems that have no Easy/Medium/Hard difficulty using the in-site AI classifier.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write difficulty types. Default is a dry-run that only prints the plan.',
        )
        parser.add_argument('--limit', type=int, default=0, help='Max problems to process (0 = all).')
        parser.add_argument('--sleep', type=float, default=0.4, help='Seconds to wait between LLM calls.')
        parser.add_argument('--code', action='append', dest='codes', default=[],
                            help='Only these problem codes (repeatable).')

    def handle(self, *args, **options):
        import time

        qs = problems_missing_difficulty(options['codes'] or None)
        total = qs.count()
        if options['limit']:
            qs = qs[:options['limit']]
        problems = list(qs)
        self.stdout.write('Problems missing difficulty: %d (processing %d)' % (total, len(problems)))
        if not problems:
            return

        apply = options['apply']
        if not apply:
            for problem in problems:
                self.stdout.write('  missing %s  %s' % (
                    problem.code, (problem.name or '')[:80],
                ))
            self.stdout.write('Dry-run only. Re-run with --apply to classify and write tags.')
            return

        tagged = skipped = failed = 0
        for problem in problems:
            description = (problem.description or '').strip()
            if not description:
                skipped += 1
                self.stdout.write('  skip %s (empty description)' % problem.code)
                continue
            try:
                level = classify_text(description)
            except Exception as exc:
                failed += 1
                self.stderr.write('  fail %s: %s' % (problem.code, exc))
                continue
            apply_difficulty(problem, level)
            tagged += 1
            self.stdout.write('  %s -> %s' % (problem.code, level))
            if options['sleep']:
                time.sleep(options['sleep'])

        self.stdout.write(self.style.SUCCESS(
            'Tagged %d, skipped %d, failed %d' % (tagged, skipped, failed),
        ))
