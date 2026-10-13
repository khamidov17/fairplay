"""Base class and result type for the problem-type registry.

A ``QuestionType`` bundles everything that distinguishes one kind of problem: how it is graded,
how students answer it, and how it is authored. Registering one (see ``judge/question_types/
__init__.py``) is enough to make it appear in the "Add new problem" menu, dispatch the submit
page, and route grading — no other code needs to know the concrete types.
"""
from collections import namedtuple

# case_points/case_total feed Submission scoring; cases is a list of dicts describing
# SubmissionTestCase rows to create (keys: case, status, points, total, feedback).
GradingResult = namedtuple('GradingResult', 'case_points case_total result cases')


class QuestionType:
    submission_type = None          # one-char Problem.submission_type discriminator
    label = None                    # human label for the "Add new problem" menu
    routes_to_bridge = False        # True only for code (uses the judge server)
    is_manual = False               # True when a human grades it (e.g. file upload)
    order = 100                     # menu ordering

    # --- exam-style contest policy (see ExamContestFormat / Submission.contest_results_hidden) ----
    # These two are independent on purpose: a type may be hidden-but-best, or visible-but-latest.
    # They only take effect in a contest whose format has ``has_hidden_results`` (e.g. the Exam type).
    hide_results_in_contest = False     # mask verdict/score from students until the contest is finalized
    contest_score_uses_latest = False   # score the latest submission (vs the best) for this type

    # --- authoring / menu ---------------------------------------------------
    def can_create(self, user):
        """Whether ``user`` may create a problem of this type (gates the menu item)."""
        return False

    def create_url(self):
        """Named-route URL to create a new problem of this type."""
        raise NotImplementedError

    def edit_url(self, problem):
        """Named-route URL to edit an existing problem of this type."""
        raise NotImplementedError

    # --- student submission -------------------------------------------------
    def build_form(self, problem, profile, data=None, files=None, initial=None):
        """Return a bound/unbound form for the student submit page.

        ``files`` carries ``request.FILES`` for types that accept an upload (e.g. file upload);
        types without a file field ignore it.
        """
        raise NotImplementedError

    def store_answer(self, submission, cleaned_data):
        """Persist the student's answer for ``submission`` (e.g. into SubmissionSource)."""
        raise NotImplementedError

    # --- grading ------------------------------------------------------------
    def grade(self, submission):
        """Grade ``submission`` and return a ``GradingResult``. Not called for bridge/manual types."""
        raise NotImplementedError
