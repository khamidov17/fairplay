"""File-upload question type: front-end authored, student uploads one file, teacher grades manually.

``is_manual`` makes ``Submission.judge()`` a no-op for these submissions (they wait at ``QU`` until a
teacher grades them from the submission page via ``finalize_submission``); ``grade()`` is never called.
"""
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from judge.models import ProblemSubmissionType
from judge.question_types.base import QuestionType
from judge.question_types.registry import register


@register
class FileUploadQuestionType(QuestionType):
    submission_type = ProblemSubmissionType.FILE
    label = _('File upload')
    order = 30

    routes_to_bridge = False
    is_manual = True

    # In an exam-style contest, hide the (manually assigned) result from students until the contest
    # is finalized — so an ungraded upload shows as "submitted" rather than 0 on the scoreboard —
    # and score the student's latest upload (their final answer). Mirrors multiple-choice.
    hide_results_in_contest = True
    contest_score_uses_latest = True

    # --- authoring / menu ---------------------------------------------------
    def can_create(self, user):
        return user.has_perm('judge.create_private_problem')

    # File-upload problems are authored/edited through the unified create_v2 page.
    def create_url(self):
        return reverse('problem_create_v2')

    def edit_url(self, problem):
        return reverse('problem_edit_v2', args=(problem.code,))

    # --- student submission -------------------------------------------------
    def build_form(self, problem, profile, data=None, files=None, initial=None):
        from judge.forms import FileSubmitForm
        return FileSubmitForm(data, files, problem=problem, profile=profile, initial=initial)

    def store_answer(self, submission, cleaned_data):
        from judge.models import SubmissionFile
        upload = cleaned_data['file']
        # submission is already saved, so submission_id is available for the storage key.
        SubmissionFile.objects.create(
            submission=submission,
            file=upload,
            original_name=upload.name[:255],
            size=upload.size,
        )

    # grade() intentionally unimplemented: manual types are graded by a human, not the registry.
