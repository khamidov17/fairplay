from functools import cached_property

from django.core.exceptions import ValidationError
from django.utils.html import format_html
from django.utils.translation import gettext as _, gettext_lazy

from judge.contest_format.default import DefaultContestFormat
from judge.contest_format.registry import register_contest_format


@register_contest_format('exam')
class ExamContestFormat(DefaultContestFormat):
    """Exam-style contest: per-problem result hiding + latest-vs-best scoring.

    Problems flagged hidden (``ContestProblem.results_hidden`` — multiple-choice by default) have their
    results masked from students and excluded from the running scoreboard until the contest is
    *finalized* (``Contest.results_finalized``); their scoreboard cell shows only a "submitted" marker.
    Scoring is per the question type: latest submission for ``contest_score_uses_latest`` problems
    (so blind guessing can't farm a correct answer), best submission otherwise. The "Finalize results"
    contest action reveals everything and folds the hidden problems into the standings.
    """
    name = gettext_lazy('Exam')
    has_hidden_results = True

    @classmethod
    def validate(cls, config):
        if config is not None and (not isinstance(config, dict) or config):
            raise ValidationError(_('Exam format does not use extra configuration. '
                                    'Leave the assignment format configuration empty.'))

    @cached_property
    def _problem_policy(self):
        """(hidden problem ids, latest-scored problem ids) for this contest, by ContestProblem id."""
        hidden, latest = set(), set()
        for contest_problem in self.contest.contest_problems.select_related('problem'):
            if contest_problem.results_hidden:
                hidden.add(contest_problem.id)
            if contest_problem.score_uses_latest:
                latest.add(contest_problem.id)
        return hidden, latest

    def scoring_uses_latest(self, contest_problem):
        return contest_problem.score_uses_latest

    def update_participation(self, participation):
        hidden_ids, latest_ids = self._problem_policy
        finalized = self.contest.results_finalized

        # One pass over the participant's contest submissions: track best points and the latest
        # (date, points) per problem so we can score either way without extra queries.
        best, latest = {}, {}
        for sub in participation.submissions.values('problem_id', 'points', 'submission__date'):
            pid, pts, date = sub['problem_id'], sub['points'], sub['submission__date']
            if pid not in best or pts > best[pid]:
                best[pid] = pts
            if pid not in latest or date > latest[pid][0]:
                latest[pid] = (date, pts)

        cumtime = 0
        score = 0
        format_data = {}
        for pid, (last_date, last_pts) in latest.items():
            points = last_pts if pid in latest_ids else best[pid]
            dt = (last_date - participation.start).total_seconds()

            if pid in hidden_ids and not finalized:
                # Hidden while the contest runs: record only that an attempt was made — no points,
                # no contribution to the visible total (so the standings can't leak correctness).
                format_data[str(pid)] = {'time': dt, 'points': 0, 'hidden': True}
                continue

            if points:
                cumtime += dt
            format_data[str(pid)] = {'time': dt, 'points': points}
            score += points

        participation.cumtime = max(cumtime, 0)
        participation.score = round(score, self.contest.points_precision)
        participation.tiebreaker = 0
        participation.format_data = format_data
        participation.save()

    def display_user_problem(self, participation, contest_problem, cell_url=None):
        format_data = (participation.format_data or {}).get(str(contest_problem.id))
        if format_data and format_data.get('hidden'):
            url = self.problem_cell_url(participation, contest_problem, cell_url)
            if url:
                return format_html(
                    '<td class="submitted-hidden"><a href="{url}" title="{title}">'
                    '<i class="fa fa-check"></i></a></td>',
                    url=url,
                    title=_('Submitted'),
                )
            return format_html(
                '<td class="submitted-hidden" title="{title}"><i class="fa fa-check"></i></td>',
                title=_('Submitted'),
            )
        return super().display_user_problem(participation, contest_problem, cell_url=cell_url)

    def get_short_form_display(self):
        yield _('The latest submission is used for hidden (e.g. multiple-choice) problems; '
                'the maximum-score submission is used for the others.')
        yield _('Results of hidden problems are masked and excluded from the scoreboard until the '
                'contest is finalized, which reveals them and folds them into the standings.')
        yield _('Ties will be broken by the sum of the last submission time on problems with a '
                'non-zero score.')
