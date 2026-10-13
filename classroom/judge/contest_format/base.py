from abc import ABCMeta, abstractmethod


class abstractclassmethod(classmethod):
    __isabstractmethod__ = True

    def __init__(self, callable):
        callable.__isabstractmethod__ = True
        super(abstractclassmethod, self).__init__(callable)


class BaseContestFormat(metaclass=ABCMeta):
    # Formats are referenced as classes in templates (e.g. {{ contest.format_class.has_hidden_results }}).
    # A class is callable, so Django's template engine would try to instantiate it (which needs
    # (contest, config)) and silently fall back to string_if_invalid. Opt out of that auto-call.
    do_not_call_in_templates = True

    # When True, this format hides results for problems flagged hidden until the contest is finalized
    # (see ExamContestFormat and Submission.contest_results_hidden). Formats default to no hiding.
    has_hidden_results = False

    @abstractmethod
    def __init__(self, contest, config):
        self.config = config
        self.contest = contest

    @property
    @abstractmethod
    def name(self):
        """
        Name of this contest format. Should be invoked with gettext_lazy.

        :return: str
        """
        raise NotImplementedError()

    @abstractclassmethod
    def validate(cls, config):
        """
        Validates the contest format configuration.

        :param config: A dictionary containing the configuration for this contest format.
        :return: None
        :raises: ValidationError
        """
        raise NotImplementedError()

    @abstractmethod
    def update_participation(self, participation):
        """
        Updates a ContestParticipation object's score, cumtime, and format_data fields based on this contest format.
        Implementations should call ContestParticipation.save().

        :param participation: A ContestParticipation object.
        :return: None
        """
        raise NotImplementedError()

    @abstractmethod
    def display_user_problem(self, participation, contest_problem, cell_url=None):
        """
        Returns the HTML fragment to show a user's performance on an individual problem. This is expected to use
        information from the format_data field instead of computing it from scratch.

        :param participation: The ContestParticipation object linking the user to the contest.
        :param contest_problem: The ContestProblem object representing the problem in question.
        :param cell_url: Optional href override (editors land on the Grade page).
            ``None`` uses the per-user contest submission list; ``''`` renders the
            cell with no href.
        :return: An HTML fragment, marked as safe for Jinja2.
        """
        raise NotImplementedError()

    @abstractmethod
    def display_participation_result(self, participation):
        """
        Returns the HTML fragment to show a user's performance on the whole contest. This is expected to use
        information from the format_data field instead of computing it from scratch.

        :param participation: The ContestParticipation object.
        :return: An HTML fragment, marked as safe for Jinja2.
        """
        raise NotImplementedError()

    @abstractmethod
    def get_problem_breakdown(self, participation, contest_problems):
        """
        Returns a machine-readable breakdown for the user's performance on every problem.

        :param participation: The ContestParticipation object.
        :param contest_problems: The list of ContestProblem objects to display performance for.
        :return: A list of dictionaries, whose content is to be determined by the contest system.
        """
        raise NotImplementedError()

    @abstractmethod
    def get_label_for_problem(self, index):
        """
        Returns the problem label for a given zero-indexed index.

        :param index: The zero-indexed problem index.
        :return: A string, the problem label.
        """
        raise NotImplementedError()

    @abstractmethod
    def get_short_form_display(self):
        """
        Returns a generator of Markdown strings to display the contest format's settings in short form.

        :return: A generator, where each item is an individual line.
        """
        raise NotImplementedError()

    def scoring_uses_latest(self, contest_problem):
        """Whether this format scores the latest live submission for ``contest_problem``.

        Default (and ICPC / AtCoder / IOI) use the highest-scoring submission. Exam
        format follows the question type (``ContestProblem.score_uses_latest``); ECOO
        always uses the latest. Grade-page "final version" and batch AI grading share
        this rule so they mark/score the same row the standings would.
        """
        return False

    @classmethod
    def best_solution_state(cls, points, total):
        if not points:
            return 'failed-score'
        if points == total:
            return 'full-score'
        return 'partial-score'
