"""Multiple-choice question type: front-end authored, internally graded, optionally shuffled."""
import json
import random

from django.core.exceptions import ObjectDoesNotExist
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from judge.models import ProblemMCQConfig, ProblemSubmissionType
from judge.question_types.base import GradingResult, QuestionType
from judge.question_types.registry import register


def ordered_options_for(problem, profile):
    """Return option dicts (id/order/text only — never ``is_correct``) in this student's order.

    When ``shuffle_options`` is set, the order is a deterministic per-student permutation seeded
    by (profile id, problem id), so it is stable across reloads and the resubmission view while
    differing between students. Otherwise the author's ``order`` is used.
    """
    options = list(problem.mcq_options.values('id', 'order', 'text'))
    options.sort(key=lambda o: o['order'])
    config = _config(problem)
    if config is not None and config.shuffle_options and profile is not None:
        random.Random('%d:%d' % (profile.id, problem.id)).shuffle(options)
    return options


def _config(problem):
    try:
        return problem.mcq_config
    except ObjectDoesNotExist:
        return None


@register
class MCQQuestionType(QuestionType):
    submission_type = ProblemSubmissionType.MCQ
    label = _('Multiple choice')
    order = 20

    # In an exam-style contest, hide results during the contest (so students can't guess until
    # correct) and grade by the latest answer (so blind guessing across attempts can't farm a hit).
    hide_results_in_contest = True
    contest_score_uses_latest = True

    # --- authoring / menu ---------------------------------------------------
    def can_create(self, user):
        return user.has_perm('judge.create_private_problem')

    def create_url(self):
        return reverse('problem_mcq_create')

    def edit_url(self, problem):
        return reverse('problem_mcq_edit', args=(problem.code,))

    # --- student submission -------------------------------------------------
    def build_form(self, problem, profile, data=None, files=None, initial=None):
        from judge.forms import MCQSubmitForm
        return MCQSubmitForm(data, problem=problem, profile=profile, initial=initial)

    def store_answer(self, submission, cleaned_data):
        from judge.models import SubmissionSource
        selected = sorted(int(x) for x in cleaned_data['selected'])
        # Snapshot option text at submit time so Result stays readable after options are edited.
        options = {o.id: o for o in submission.problem.mcq_options.all()}
        choices = []
        for opt_id in selected:
            opt = options.get(opt_id)
            if opt is None:
                choices.append({'id': opt_id, 'order': None, 'text': ''})
            else:
                choices.append({'id': opt.id, 'order': opt.order, 'text': opt.text})
        payload = json.dumps({'selected': selected, 'choices': choices})
        source, _created = SubmissionSource.objects.update_or_create(
            submission=submission, defaults={'source': payload})
        submission.source = source  # prime the reverse relation for immediate grading

    # --- grading ------------------------------------------------------------
    def grade(self, submission):
        problem = submission.problem
        options = list(problem.mcq_options.all())
        correct_ids = {o.id for o in options if o.is_correct}
        valid_ids = {o.id for o in options}

        try:
            selected = set(json.loads(submission.source.source).get('selected', []))
        except (ValueError, TypeError, ObjectDoesNotExist):
            selected = set()
        selected &= valid_ids

        config = _config(problem)
        multiple = config.multiple_select if config else False

        is_correct = bool(correct_ids) and selected == correct_ids

        # Always emit granular case points for multiple-select; the all-or-nothing gating is applied
        # downstream by problem.partial (finalize_submission) and contest_problem.partial
        # (update_contest), exactly like code problems. This keeps the contest-level partial setting
        # able to override the problem-level one.
        if multiple and correct_ids:
            case_points, case_total = self._multiple_case_score(
                selected, correct_ids, config, problem.points)
        else:
            case_points = 1 if is_correct else 0
            case_total = 1

        result = 'AC' if is_correct else 'WA'
        cases = [{'case': 1, 'status': result, 'points': case_points, 'total': case_total}]
        return GradingResult(case_points=case_points, case_total=case_total, result=result, cases=cases)

    @staticmethod
    def _multiple_case_score(selected, correct_ids, config, problem_points):
        """Raw case_points / case_total for a multiple-select answer.

        *proportional* (default): +1 per correct choice, −1 per wrong choice, clamped to
        ``[0, #correct]``. Scaled later by problem / contest points.

        *underselect*: each correctly selected option earns ``underselect_points`` (or an
        even split of the question if that is empty). Any incorrect option scores 0 for
        the whole question. A fully correct set still uses the 1/1 path via ``is_correct``.
        """
        mode = (config.scoring_mode if config else None) or ProblemMCQConfig.SCORE_PROPORTIONAL
        n_correct = len(correct_ids)
        n_right = len(selected & correct_ids)
        n_wrong = len(selected - correct_ids)
        if selected == correct_ids:
            return 1, 1

        if mode == ProblemMCQConfig.SCORE_UNDERSELECT:
            if n_wrong:
                return 0, 1
            per = getattr(config, 'underselect_points', None) if config is not None else None
            try:
                per = float(per) if per is not None else None
            except (TypeError, ValueError):
                per = None
            if per is None or per <= 0:
                return n_right, n_correct
            total = float(problem_points) if problem_points else 0
            if total <= 0:
                return n_right * per, n_correct * per if n_correct else 1
            earned = min(n_right * per, total)
            return earned, total

        # Proportional: +1 correct, −1 wrong, floor at 0.
        return max(0, n_right - n_wrong), n_correct
