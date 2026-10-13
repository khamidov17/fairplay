import uuid

from django.db import models
from django.db.models import CASCADE
from django.utils.translation import gettext_lazy as _

from judge.models.problem import Problem
from judge.storage import generate_submission_key, submission_storage

__all__ = [
    'ProblemMCQOption', 'ProblemMCQConfig', 'ProblemBlank', 'ProblemFillBlankConfig',
    'ProblemShortAnswerConfig', 'ProblemCodeConfig', 'ProblemImportDraft', 'ProblemImportItem',
    'ProblemImportImage', 'SubmissionFile', 'ProblemFileConfig',
]


class ProblemImportDraft(models.Model):
    """A server-side Excel import preview owned by one teacher."""
    owner = models.ForeignKey('judge.Profile', related_name='problem_import_drafts', on_delete=CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-updated_at',)
        verbose_name = _('problem import draft')
        verbose_name_plural = _('problem import drafts')


class ProblemImportItem(models.Model):
    """One editable row in an Excel import preview."""
    TYPE_CHOICES = (
        ('C', _('Programming')),
        ('M', _('Multiple choice')),
        ('B', _('Fill in the blank')),
        ('S', _('Short answer')),
    )

    draft = models.ForeignKey(ProblemImportDraft, related_name='items', on_delete=CASCADE)
    row_number = models.PositiveIntegerField()
    name = models.CharField(max_length=200)
    submission_type = models.CharField(max_length=1, choices=TYPE_CHOICES)
    description = models.TextField(blank=True)
    answer = models.TextField(blank=True)
    solution = models.TextField(blank=True)
    tags = models.TextField(blank=True)
    difficulty = models.CharField(max_length=10, blank=True)
    allowed_languages = models.TextField(blank=True)
    test_data = models.TextField(blank=True)
    test_data_zip = models.FileField(upload_to='problem_import_test_data/', blank=True)
    source_label = models.CharField(max_length=255, blank=True)
    options = models.TextField(blank=True)
    correct_options = models.TextField(blank=True)
    blanks = models.TextField(blank=True)
    rubric = models.TextField(blank=True)
    grading_table = models.TextField(blank=True)
    is_true_false = models.BooleanField(default=False)
    # Preserve the complete unified-editor payload while the import is still a draft.
    editor_data = models.JSONField(default=dict, blank=True)
    data_needs_review = models.BooleanField(default=False)
    data_load_error = models.TextField(blank=True)
    config_needs_review = models.BooleanField(default=False)
    unrecognized_configurations = models.JSONField(default=list, blank=True)
    config_review_signature = models.CharField(max_length=64, blank=True)
    formatting_error = models.TextField(blank=True)
    type_needs_review = models.BooleanField(default=False)
    name_needs_review = models.BooleanField(default=False)
    created_problem = models.ForeignKey(Problem, null=True, blank=True, related_name='+', on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('row_number', 'pk')
        verbose_name = _('problem import item')
        verbose_name_plural = _('problem import items')


class ProblemImportImage(models.Model):
    """A DOCX figure kept with its editable import draft until problem creation."""
    item = models.ForeignKey(ProblemImportItem, related_name='images', on_delete=CASCADE)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    filename = models.CharField(max_length=255)
    content = models.BinaryField()

    class Meta:
        verbose_name = _('problem import image')
        verbose_name_plural = _('problem import images')


class ProblemMCQOption(models.Model):
    """A single multiple-choice option. The MCQ analog of a ProblemTestCase.

    ``is_correct`` is author-only and must never be serialized into anything served to students.
    """
    problem = models.ForeignKey(Problem, verbose_name=_('problem'), related_name='mcq_options', on_delete=CASCADE)
    order = models.PositiveIntegerField(verbose_name=_('order'))
    text = models.TextField(verbose_name=_('choice text'))
    is_correct = models.BooleanField(verbose_name=_('is correct'), default=False)

    def __str__(self):
        return f'{self.problem.code} #{self.order}'

    class Meta:
        ordering = ('order',)
        unique_together = ('problem', 'order')
        verbose_name = _('MCQ option')
        verbose_name_plural = _('MCQ options')


class ProblemMCQConfig(models.Model):
    """Per-problem multiple-choice settings."""
    SCORE_PROPORTIONAL = 'proportional'
    SCORE_UNDERSELECT = 'underselect'
    SCORE_MODE_CHOICES = (
        (SCORE_PROPORTIONAL, _('Proportional credit')),
        (SCORE_UNDERSELECT, _('Under-select credit')),
    )

    problem = models.OneToOneField(Problem, verbose_name=_('problem'), related_name='mcq_config', on_delete=CASCADE)
    multiple_select = models.BooleanField(
        verbose_name=_('allow multiple answers'), default=False,
        help_text=_('If set, students may select more than one choice (checkboxes); otherwise exactly one (radio).'))
    is_true_false = models.BooleanField(
        verbose_name=_('true/false question'), default=False,
        help_text=_('Marks this MCQ as a true/false question so it is labelled accordingly.'))
    shuffle_options = models.BooleanField(
        verbose_name=_('shuffle options per student'), default=False,
        help_text=_('Present choices in a different, stable order for each student to deter copying.'))
    scoring_mode = models.CharField(
        verbose_name=_('scoring method'), max_length=16, choices=SCORE_MODE_CHOICES,
        default=SCORE_PROPORTIONAL,
        help_text=_('How a multiple-select question awards partial credit. Ignored for single-answer questions.'))
    underselect_points = models.FloatField(
        verbose_name=_('points per correct choice'), null=True, blank=True, default=None,
        help_text=_('In under-select mode, points awarded for each correctly selected option. '
                    'Leave empty to split the question points evenly among correct options.'))

    def __str__(self):
        return _('MCQ config for %s') % self.problem.code

    class Meta:
        verbose_name = _('MCQ configuration')
        verbose_name_plural = _('MCQ configurations')


# --- Fill-in-the-blank ------------------------------------------------------
GRADE_EXACT = 'exact'
GRADE_AI = 'ai'
GRADE_MODE_CHOICES = (
    (GRADE_EXACT, _('Exact match')),
    (GRADE_AI, _('AI grading')),
)


class ProblemBlank(models.Model):
    """A single blank in a fill-in-the-blank problem. The fill-blank analog of a ProblemMCQOption.

    ``answers`` holds the accepted correct answers, one alternative per line; a student's answer is
    correct if (after normalization per ProblemFillBlankConfig) it matches any line. The accepted
    answers are author-only and must never be serialized into anything served to students.
    """
    problem = models.ForeignKey(Problem, verbose_name=_('problem'), related_name='blanks', on_delete=CASCADE)
    order = models.PositiveIntegerField(verbose_name=_('order'))
    answers = models.TextField(
        verbose_name=_('accepted answers'),
        help_text=_('One accepted answer per line; any of them counts as correct.'))
    points = models.FloatField(
        verbose_name=_('points'), default=1,
        help_text=_('Points awarded when this blank is correct. Used when the problem allows partial credit.'))
    label = models.CharField(
        verbose_name=_('label'), max_length=100, blank=True,
        help_text=_('Optional label shown next to the input; defaults to "Blank N".'))

    def __str__(self):
        return f'{self.problem.code} #{self.order}'

    class Meta:
        ordering = ('order',)
        unique_together = ('problem', 'order')
        verbose_name = _('fill-in-the-blank blank')
        verbose_name_plural = _('fill-in-the-blank blanks')


class ProblemFillBlankConfig(models.Model):
    """Per-problem fill-in-the-blank settings."""
    problem = models.OneToOneField(Problem, verbose_name=_('problem'), related_name='fill_blank_config',
                                   on_delete=CASCADE)
    case_sensitive = models.BooleanField(
        verbose_name=_('case sensitive'), default=False,
        help_text=_('If set, answers must match the exact letter case; otherwise case is ignored.'))
    trim_whitespace = models.BooleanField(
        verbose_name=_('trim surrounding whitespace'), default=True,
        help_text=_('If set, leading and trailing whitespace in the student answer is ignored.'))
    grading_mode = models.CharField(
        verbose_name=_('grading mode'), max_length=8, choices=GRADE_MODE_CHOICES, default=GRADE_EXACT,
        help_text=_('Exact match compares each blank to its accepted answers; AI grading asks the '
                    'configured model to score the response against the rubric (falls back to exact '
                    'match when no model is configured).'))
    ai_rubric = models.TextField(
        verbose_name=_('AI rubric'), blank=True,
        help_text=_('Grading guidance sent to the model (never shown to students).'))
    grading_table = models.JSONField(
        verbose_name=_('grading table'), blank=True, null=True, default=None,
        help_text=_('Sub-question breakdown: a list of {"name", "max_points", '
                    '"strictness": "strict|normal|lenient"} objects. '
                    'When set, the LLM grades each sub-question independently; '
                    'otherwise the answer is graded as a single whole.'))

    def __str__(self):
        return _('Fill-in-the-blank config for %s') % self.problem.code

    class Meta:
        verbose_name = _('fill-in-the-blank configuration')
        verbose_name_plural = _('fill-in-the-blank configurations')


def submission_file_path(instance, filename):
    """upload_to callback: the storage key is generated from the submission id, never the raw
    filename (the original name is kept on the model). ``submission_id`` is set before save."""
    return generate_submission_key(instance.submission_id, filename)


class SubmissionFile(models.Model):
    """The binary a student uploaded as their answer for a file-upload submission.

    One file per submission to start; the file itself lives in ``submission_storage`` (S3 primary
    + async Box mirror), never on the judge filesystem. ``original_name`` is what the student is
    offered on download; ``file.name`` is the generated storage key.
    """
    submission = models.OneToOneField('Submission', verbose_name=_('submission'), related_name='upload',
                                      on_delete=CASCADE)
    file = models.FileField(verbose_name=_('uploaded file'), storage=submission_storage,
                            upload_to=submission_file_path, max_length=255)
    original_name = models.CharField(verbose_name=_('original filename'), max_length=255)
    size = models.PositiveBigIntegerField(verbose_name=_('file size'), default=0)
    uploaded_on = models.DateTimeField(verbose_name=_('uploaded on'), auto_now_add=True)

    def __str__(self):
        return _('Upload for submission %d') % self.submission_id

    class Meta:
        verbose_name = _('submission file')
        verbose_name_plural = _('submission files')


class ProblemFileConfig(models.Model):
    """Per-problem file-upload settings (accepted extensions / size cap / instructions)."""
    problem = models.OneToOneField(Problem, verbose_name=_('problem'), related_name='file_config',
                                   on_delete=CASCADE)
    allowed_extensions = models.CharField(
        verbose_name=_('allowed file extensions'), max_length=255, blank=True,
        help_text=_('Comma-separated extensions without dots, e.g. "pdf,zip,docx". '
                    'Leave blank to allow any extension.'))
    max_file_size = models.PositiveBigIntegerField(
        verbose_name=_('maximum file size (bytes)'), default=20 * 1024 * 1024,
        help_text=_('Reject uploads larger than this many bytes.'))
    instructions = models.TextField(
        verbose_name=_('submission instructions'), blank=True,
        help_text=_('Optional guidance shown to students on the submit page.'))

    def allowed_extension_list(self):
        return [e.strip().lower().lstrip('.') for e in self.allowed_extensions.split(',') if e.strip()]

    def __str__(self):
        return _('File-upload config for %s') % self.problem.code

    class Meta:
        verbose_name = _('file-upload configuration')
        verbose_name_plural = _('file-upload configurations')


# --- Short answer -----------------------------------------------------------
class ProblemShortAnswerConfig(models.Model):
    """Per-problem short-answer settings. Graded by the LLM against a reference answer + rubric."""
    problem = models.OneToOneField(Problem, verbose_name=_('problem'), related_name='short_answer_config',
                                   on_delete=CASCADE)
    ai_enabled = models.BooleanField(
        verbose_name=_('enable AI grading'), default=False,
        help_text=_('Ask the configured model to score the answer against the rubric. '
                    'When off, the submission is saved for manual grading and the LLM is not called.'))
    reference_answer = models.TextField(
        verbose_name=_('reference answer'), blank=True,
        help_text=_('Model answer the response is compared against (never shown to students).'))
    ai_rubric = models.TextField(
        verbose_name=_('AI rubric'), blank=True,
        help_text=_('Grading guidance sent to the model (never shown to students).'))
    grading_table = models.JSONField(
        verbose_name=_('grading table'), blank=True, null=True, default=None,
        help_text=_('Sub-question breakdown: a list of {"name", "max_points", '
                    '"strictness": "strict|normal|lenient"} objects. '
                    'When set, the LLM grades each sub-question independently; '
                    'otherwise the answer is graded as a single whole.'))

    def __str__(self):
        return _('Short-answer config for %s') % self.problem.code

    class Meta:
        verbose_name = _('short-answer configuration')
        verbose_name_plural = _('short-answer configurations')


# --- Code (supplementary AI rubric) -----------------------------------------
class ProblemCodeConfig(models.Model):
    """Per-problem supplementary AI grading for code (programming) problems.

    Test-case judging (the judge bridge) remains the source of truth for AC/WA; when
    ``ai_enabled`` is set, the configured model *additionally* scores the submitted source against
    the rubric / grading table after the judge finishes.  That rubric score is stored as extra
    ``SubmissionTestCase`` rows (batch=-1). Students see them in realtime assignments and after
    batch grading in exams; the AC/WA verdict is never changed.
    """
    problem = models.OneToOneField(Problem, verbose_name=_('problem'), related_name='code_config',
                                   on_delete=CASCADE)
    ai_enabled = models.BooleanField(
        verbose_name=_('enable AI rubric scoring'), default=False,
        help_text=_('After the judge grades the test cases, also ask the configured model to score '
                    'the submitted code against the rubric below.'))
    ai_rubric = models.TextField(
        verbose_name=_('AI rubric'), blank=True,
        help_text=_('Grading guidance sent to the model (never shown to students).'))
    grading_table = models.JSONField(
        verbose_name=_('grading table'), blank=True, null=True, default=None,
        help_text=_('Sub-question breakdown: a list of {"name", "max_points", '
                    '"strictness": "strict|normal|lenient"} objects. '
                    'When set, the LLM grades each sub-question independently; '
                    'otherwise the code is graded as a single whole.'))

    def __str__(self):
        return _('Code AI config for %s') % self.problem.code

    class Meta:
        verbose_name = _('code AI configuration')
        verbose_name_plural = _('code AI configurations')
