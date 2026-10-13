import hashlib
import hmac

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from reversion import revisions

from judge.judgeapi import abort_submission, judge_submission
from judge.models.problem import Problem, SubmissionSourceAccess
from judge.models.profile import Profile
from judge.models.runtime import Language
from judge.utils.unicode import utf8bytes

__all__ = ['SUBMISSION_RESULT', 'Submission', 'SubmissionSource', 'SubmissionTestCase',
           'SubmissionManualGrade']

SUBMISSION_RESULT = (
    ('AC', _('Accepted')),
    ('WA', _('Wrong Answer')),
    ('TLE', _('Time Limit Exceeded')),
    ('MLE', _('Memory Limit Exceeded')),
    ('OLE', _('Output Limit Exceeded')),
    ('IR', _('Invalid Return')),
    ('RTE', _('Runtime Error')),
    ('CE', _('Compile Error')),
    ('IE', _('Internal Error')),
    ('SC', _('Short Circuited')),
    ('AB', _('Aborted')),
)


@revisions.register(follow=['test_cases'])
class Submission(models.Model):
    STATUS = (
        ('QU', _('Queued')),
        ('P', _('Processing')),
        ('G', _('Grading')),
        ('D', _('Completed')),
        ('IE', _('Internal Error')),
        ('CE', _('Compile Error')),
        ('AB', _('Aborted')),
    )
    IN_PROGRESS_GRADING_STATUS = ('QU', 'P', 'G')
    RESULT = SUBMISSION_RESULT
    USER_DISPLAY_CODES = {
        'AC': _('Accepted'),
        'WA': _('Wrong Answer'),
        'SC': _('Short Circuited'),
        'TLE': _('Time Limit Exceeded'),
        'MLE': _('Memory Limit Exceeded'),
        'OLE': _('Output Limit Exceeded'),
        'IR': _('Invalid Return'),
        'RTE': _('Runtime Error'),
        'CE': _('Compile Error'),
        'IE': _('Internal Error (judging server error)'),
        'QU': _('Queued'),
        'P': _('Processing'),
        'G': _('Grading'),
        'D': _('Completed'),
        'AB': _('Aborted'),
    }

    user = models.ForeignKey(Profile, verbose_name=_('user'), on_delete=models.CASCADE, db_index=False)
    problem = models.ForeignKey(Problem, verbose_name=_('problem'), on_delete=models.CASCADE, db_index=False)
    date = models.DateTimeField(verbose_name=_('submission time'), auto_now_add=True, db_index=True)
    time = models.FloatField(verbose_name=_('execution time'), null=True)
    memory = models.FloatField(verbose_name=_('memory usage'), null=True)
    points = models.FloatField(verbose_name=_('points granted'), null=True)
    language = models.ForeignKey(Language, verbose_name=_('submission language'),
                                 on_delete=models.CASCADE, db_index=False)
    status = models.CharField(verbose_name=_('status'), max_length=2, choices=STATUS, default='QU', db_index=True)
    result = models.CharField(verbose_name=_('result'), max_length=3, choices=SUBMISSION_RESULT,
                              default=None, null=True, blank=True)
    error = models.TextField(verbose_name=_('compile errors'), null=True, blank=True)
    current_testcase = models.IntegerField(default=0)
    batch = models.BooleanField(verbose_name=_('batched cases'), default=False)
    case_points = models.FloatField(verbose_name=_('test case points'), default=0)
    case_total = models.FloatField(verbose_name=_('test case total points'), default=0)
    ai_points = models.FloatField(
        verbose_name=_('AI rubric points'), null=True, blank=True,
        help_text=_('Supplementary AI rubric score, on the problem points scale, after the '
                    'test-case ratio clamp. Null until AI grading has run.'))
    judged_on = models.ForeignKey('Judge', verbose_name=_('judged on'), null=True, blank=True,
                                  on_delete=models.SET_NULL)
    judged_date = models.DateTimeField(verbose_name=_('submission judge time'), default=None, null=True)
    rejudged_date = models.DateTimeField(verbose_name=_('last rejudge date by admin'), null=True, blank=True)
    is_pretested = models.BooleanField(verbose_name=_('was ran on pretests only'), default=False)
    contest_object = models.ForeignKey('Contest', verbose_name=_('contest'), null=True, blank=True,
                                       on_delete=models.SET_NULL, related_name='+', db_index=False)
    locked_after = models.DateTimeField(verbose_name=_('submission lock'), null=True, blank=True)

    # ── Manual grading metadata (teacher-side grade page) ───────────────────
    # These three fields back the "评分与评语" (score + review comment) panel of
    # the grade workspace. They are deliberately NOT exposed on the student
    # submission view — they exist so a teacher can annotate a submission and
    # the grade list can show "Last graded · by X" state, mirroring the
    # grading.html reference design.
    review_note = models.TextField(
        verbose_name=_('review note'), blank=True, default='',
        help_text=_('Teacher comment written on the grade page while grading this submission.'),
    )
    reviewed_by = models.ForeignKey(
        'Profile', verbose_name=_('reviewed by'), null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
        help_text=_('The teacher who last saved a manual grade for this submission.'),
    )
    reviewed_at = models.DateTimeField(
        verbose_name=_('reviewed at'), null=True, blank=True,
        help_text=_('When the manual grade was last saved.'),
    )

    @cached_property
    def manual_grade_override(self):
        """The teacher's manual grade record for this submission, or None.

        OneToOne reverse accessor (``self.manual_grade``) raises
        ``ObjectDoesNotExist`` when no manual grade exists, or returns None when
        ``prefetch_related('manual_grade')`` was used. Both collapse to None here
        so callers can branch on ``is None`` uniformly.
        """
        try:
            return self.manual_grade
        except ObjectDoesNotExist:
            return None

    @classmethod
    def result_class_from_code(cls, result, case_points, case_total):
        if result == 'AC':
            if case_points == case_total:
                return 'AC'
            return '_AC'
        return result

    @property
    def result_class(self):
        # This exists to save all these conditionals from being executed (slowly) in each row.html template
        if self.status in ('IE', 'CE'):
            return self.status
        return Submission.result_class_from_code(self.result, self.case_points, self.case_total)

    @property
    def memory_bytes(self):
        return self.memory * 1024 if self.memory is not None else 0

    @property
    def short_status(self):
        return self.result or self.status

    @property
    def long_status(self):
        return Submission.USER_DISPLAY_CODES.get(self.short_status, '')

    @cached_property
    def is_locked(self):
        return self.locked_after is not None and self.locked_after < timezone.now()

    def judge(self, *args, rejudge=False, force_judge=False, rejudge_user=None, **kwargs):
        if force_judge or not self.is_locked:
            if rejudge:
                with revisions.create_revision(manage_manually=True):
                    if rejudge_user:
                        revisions.set_user(rejudge_user)
                    revisions.set_comment('Rejudged')
                    revisions.add_to_revision(self)

            # Route by question type: code goes to the judge bridge; auto-graded types (e.g.
            # multiple-choice) grade internally; manual types are left untouched. Importing here
            # avoids a circular import between models and the question-type registry.
            from judge.question_types import get_grader
            grader = get_grader(self.problem.submission_type)
            if grader.routes_to_bridge:
                judge_submission(self, *args, rejudge=rejudge, **kwargs)
            elif not grader.is_manual:
                from judge.submission_grading import grade_internally
                # Bridge path sets rejudged_date in judge_submission; mirror that
                # for internal graders (MCQ / short-answer / AI fill-blank) so
                # Grade-page status polling can tell a rejudge finished.
                if rejudge:
                    self.rejudged_date = timezone.now()
                grade_internally(self)

    judge.alters_data = True

    def abort(self):
        abort_submission(self)

    abort.alters_data = True

    def can_see_detail(self, user):
        if not user.is_authenticated:
            return False
        profile = user.profile
        source_visibility = self.problem.submission_source_visibility
        if self.problem.is_editable_by(user):
            return True
        elif user.has_perm('judge.view_all_submission'):
            return True
        elif self.user_id == profile.id:
            return True
        elif source_visibility == SubmissionSourceAccess.ALWAYS:
            return True
        elif source_visibility == SubmissionSourceAccess.SOLVED and \
                (self.problem.is_public or self.problem.testers.filter(id=profile.id).exists()) and \
                self.problem.is_solved_by(user):
            return True
        elif source_visibility == SubmissionSourceAccess.ONLY_OWN and \
                self.problem.testers.filter(id=profile.id).exists():
            return True

        contest = self.contest_object
        # If user is an author or curator of the contest the submission was made in, or they can see in-contest subs
        if contest is not None and (
            user.profile.id in contest.editor_ids or
            contest.view_contest_submissions.filter(id=user.profile.id).exists() or
            (contest.tester_see_submissions and user.profile.id in contest.tester_ids)
        ):
            return True

        return False

    def update_contest(self):
        try:
            contest = self.contest
        except AttributeError:
            return

        contest_problem = contest.problem
        contest.points = round(self.case_points / self.case_total * contest_problem.points
                               if self.case_total > 0 else 0, 3)
        if not contest_problem.partial and contest.points != contest_problem.points:
            contest.points = 0
        # A released manual override wins over the machine score, so a rejudge
        # (which recomputes machine points) must not wipe it. Until the teacher
        # releases it, the machine score stands (the ranking must not leak an
        # unreleased grade).
        manual_grade = self.manual_grade_override
        if manual_grade is not None and manual_grade.points is not None and manual_grade.released:
            contest.points = manual_grade.points
        contest.save()
        contest.participation.recompute_results()

    update_contest.alters_data = True

    @property
    def is_graded(self):
        return self.status not in ('QU', 'P', 'G')

    @cached_property
    def contest_key(self):
        if hasattr(self, 'contest'):
            return self.contest_object.key

    def __str__(self):
        return _('Submission %(id)d of %(problem)s by %(user)s') % {
            'id': self.id, 'problem': self.problem, 'user': self.user.user.username,
        }

    def get_absolute_url(self):
        return reverse('submission_status', args=(self.id,))

    @cached_property
    def contest_or_none(self):
        try:
            return self.contest
        except ObjectDoesNotExist:
            return None

    @cached_property
    def contest_results_hidden(self):
        """Whether this submission's results are currently masked from ordinary viewers.

        True only inside an exam-style contest (a format with ``has_hidden_results``) that has not been
        finalized, for a problem flagged hidden (``ContestProblem.results_hidden``). Viewer-independent;
        the per-viewer bypass lives in ``results_visible_to``. Checks are ordered to short-circuit
        cheaply for the common (non-contest / non-exam) case.
        """
        if self.contest_object_id is None:
            return False
        contest = self.contest_object
        # format_class is a class attribute lookup (no format instantiation / format_config needed).
        if not contest.format_class.has_hidden_results or contest.results_finalized:
            return False
        contest_submission = self.contest_or_none
        if contest_submission is None:
            return False
        return contest_submission.problem.results_hidden

    def results_visible_to(self, user):
        """Whether ``user`` may see this submission's verdict/score right now.

        Everyone sees results unless they are masked (``contest_results_hidden``); when masked, only
        contest editors / submission-viewers / staff see through — the owner does **not**.
        """
        if not self.contest_results_hidden:
            return True
        if not user.is_authenticated:
            return False
        if user.has_perm('judge.view_all_submission'):
            return True
        if self.problem.is_editable_by(user):
            return True
        profile = user.profile
        contest = self.contest_object
        if contest is not None and (
            profile.id in contest.editor_ids or
            contest.view_contest_submissions.filter(id=profile.id).exists() or
            (contest.tester_see_submissions and profile.id in contest.tester_ids)
        ):
            return True
        return False

    @classmethod
    def get_id_secret(cls, sub_id):
        return (hmac.new(utf8bytes(settings.EVENT_DAEMON_SUBMISSION_KEY), b'%d' % sub_id, hashlib.sha512)
                    .hexdigest()[:16] + '%08x' % sub_id)

    @cached_property
    def id_secret(self):
        return self.get_id_secret(self.id)

    class Meta:
        permissions = (
            ('abort_any_submission', _('Abort any submission')),
            ('rejudge_submission', _('Rejudge the submission')),
            ('rejudge_submission_lot', _('Rejudge a lot of submissions')),
            ('spam_submission', _('Submit without limit')),
            ('view_all_submission', _('View all submission')),
            ('resubmit_other', _("Resubmit others' submission")),
            ('lock_submission', _('Change lock status of submission')),
        )
        verbose_name = _('submission')
        verbose_name_plural = _('submissions')

        indexes = [
            # For problem submission rankings
            models.Index(fields=['problem', 'user', '-points', '-time']),

            # For contest problem submission rankings
            models.Index(fields=['contest_object', 'problem', 'user', '-points', '-time']),

            # For main submission list filtering by some combination of result and language
            models.Index(fields=['result', '-id']),
            models.Index(fields=['result', 'language', '-id']),
            models.Index(fields=['language', '-id']),

            # For filtered main submission list result charts
            models.Index(fields=['result', 'problem']),
            models.Index(fields=['language', 'problem', 'result']),

            # For problem submissions result chart
            models.Index(fields=['problem', 'result']),

            # For user_attempted_ids and own problem submissions result chart
            models.Index(fields=['user', 'problem', 'result']),

            # For user_completed_ids
            models.Index(fields=['user', 'result']),
        ]


class SubmissionSource(models.Model):
    submission = models.OneToOneField(Submission, on_delete=models.CASCADE, verbose_name=_('associated submission'),
                                      related_name='source')
    source = models.TextField(verbose_name=_('source code'), max_length=65536)

    def __str__(self):
        return _('Source of %(submission)s') % {'submission': self.submission}

    class Meta:
        verbose_name = _('submission source')
        verbose_name_plural = _('submission sources')


@revisions.register()
class SubmissionTestCase(models.Model):
    RESULT = SUBMISSION_RESULT

    submission = models.ForeignKey(Submission, verbose_name=_('associated submission'), db_index=False,
                                   related_name='test_cases', on_delete=models.CASCADE)
    case = models.IntegerField(verbose_name=_('test case ID'))
    status = models.CharField(max_length=3, verbose_name=_('status flag'), choices=SUBMISSION_RESULT)
    time = models.FloatField(verbose_name=_('execution time'), null=True)
    memory = models.FloatField(verbose_name=_('memory usage'), null=True)
    points = models.FloatField(verbose_name=_('points granted'), null=True)
    total = models.FloatField(verbose_name=_('points possible'), null=True)
    batch = models.IntegerField(verbose_name=_('batch number'), null=True)
    feedback = models.CharField(max_length=50, verbose_name=_('judging feedback'), blank=True)
    extended_feedback = models.TextField(verbose_name=_('extended judging feedback'), blank=True)
    output = models.TextField(verbose_name=_('program output'), blank=True)

    @property
    def long_status(self):
        return Submission.USER_DISPLAY_CODES.get(self.status, '')

    @property
    def result_class(self):
        if self.status in ('IE', 'CE'):
            return self.status
        return Submission.result_class_from_code(self.status, self.points, self.total)

    class Meta:
        unique_together = ('submission', 'case')
        verbose_name = _('submission test case')
        verbose_name_plural = _('submission test cases')


class SubmissionManualGrade(models.Model):
    """A teacher's manual score override for a single submission.

    This is the authoritative record written by the grade page's "Save grading"
    action. It is deliberately kept OUT of ``Submission.points`` /
    ``ContestSubmission.points`` (the machine/judge scores) so that:

    * the machine score survives untouched for display until the teacher
      "releases" this override (``released``), and
    * every consumer that needs the *actual* score (grade page, ranking,
      standings, exports) can prefer the manual score unconditionally.

    ``machine_points`` snapshots the machine score in the same scale the grade
    page displays (assignment-specified points for contest submissions, the
    problem's own scale otherwise) at the moment of first grading, so the
    student-facing workspace can keep showing the machine score while the
    override is unreleased.
    """
    submission = models.OneToOneField(Submission, verbose_name=_('submission'),
                                      related_name='manual_grade', on_delete=models.CASCADE)
    points = models.FloatField(verbose_name=_('manual score'), null=True, blank=True)
    machine_points = models.FloatField(verbose_name=_('machine score at grading time'), null=True, blank=True)
    released = models.BooleanField(verbose_name=_('released to students'), default=False)
    updated_at = models.DateTimeField(verbose_name=_('last updated'), auto_now=True)

    def __str__(self):
        return _('Manual grade for submission %(id)d') % {'id': self.submission_id}

    class Meta:
        verbose_name = _('submission manual grade')
        verbose_name_plural = _('submission manual grades')
