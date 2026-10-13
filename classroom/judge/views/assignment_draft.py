"""Save and clear a student's server-side draft for one assignment problem.

The workspace calls this while the student types, so that what they have written survives a closed
tab, a dead laptop or a different machine — and so the deadline sweep has something to submit for
students who never pressed Submit. Every rejection here is a draft that will *not* be
auto-submitted, so the guards mirror the ones on the submit path rather than inventing their own.
"""
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from django.views.generic import View

from judge.forms import FileSubmitForm
from judge.models import AssignmentDraft, AssignmentDraftFile, Language, Problem, ProblemSubmissionType
from judge.utils.assignment_draft import DraftContentError, normalize_draft_content
from judge.utils.assignment_harvest import user_bypasses_assignment_rules

logger = logging.getLogger('judge.assignment_draft')


class AssignmentDraftView(LoginRequiredMixin, View):
    """POST saves the current answer as this participation's draft; DELETE drops it."""

    http_method_names = ['post', 'delete']

    def _error(self, message, status):
        return JsonResponse({'error': message}, status=status)

    def _resolve(self):
        """Return ``(participation, problem)``, or an error response if drafts do not apply.

        Authorization is the participation itself: the caller can only ever reach their own
        ``current_contest``, and being joined is what grants access to the assignment's problems.
        """
        participation = self.request.profile.current_contest
        if participation is None or participation.contest.key != self.kwargs['contest']:
            return self._error(_('Join the assignment before saving answers.'), 403)
        contest = participation.contest
        # Spectators and staff who bypass assignment rules are not being graded, so a draft of
        # theirs would only give auto-submit work that must not be submitted.
        if participation.spectate or user_bypasses_assignment_rules(self.request.user, contest):
            return self._error(_('Answers are not saved in this mode.'), 403)
        if participation.ended:
            # The window is closed and auto-submit has already run or is about to: accepting a
            # write now would either be lost or resurrect a draft that was just harvested.
            return self._error(_('This assignment is closed.'), 403)

        problem = get_object_or_404(Problem, code=self.kwargs['problem'])
        if not contest.contest_problems.filter(problem_id=problem.id).exists():
            return self._error(_('This problem is not part of this assignment.'), 404)
        return participation, problem

    def _save_file_draft(self, participation, problem, upload):
        """Replace this participation's saved file for ``problem`` with ``upload``.

        The previous file has to survive a failed upload: a student who picks the wrong file and
        loses the connection mid-transfer would otherwise be left with nothing to auto-submit. So
        the bytes are written first and the row is only re-pointed once they are safely stored,
        and the row is updated in place rather than deleted and recreated, so there is never a
        moment where the draft exists with no file.
        """
        # The same extension and size rules the submit path enforces: a draft that could not be
        # submitted by hand must not become a submission at the deadline either.
        form = FileSubmitForm(None, {'file': upload}, problem=problem)
        if not form.is_valid():
            return self._error(' '.join(form.errors.get('file') or [_('That file cannot be saved.')]), 400)
        upload = form.cleaned_data['file']

        draft, _created = AssignmentDraft.objects.get_or_create(participation=participation, problem=problem)
        stored = AssignmentDraftFile.objects.filter(draft=draft).first()
        superseded = stored.file.name if stored is not None and stored.file else ''
        if stored is None:
            stored = AssignmentDraftFile(draft=draft)

        stored.file.save(upload.name, upload, save=False)
        stored.original_name = upload.name[:255]
        stored.size = upload.size
        stored.save()

        # A new upload is a new answer, so a draft already collected becomes collectable again.
        draft.content = ''
        draft.harvested_at = None
        draft.save(update_fields=['content', 'harvested_at', 'updated_on'])

        if superseded and superseded != stored.file.name:
            # Only now is the old object unreferenced. Losing this delete costs storage, not
            # correctness, so it must not fail the save the student is waiting on.
            try:
                stored.file.storage.delete(superseded)
            except Exception:
                logger.exception('Could not drop superseded draft file %s', superseded)

        return JsonResponse({
            'saved': True,
            'updated_on': draft.updated_on.isoformat(),
            'file': {'name': stored.original_name, 'size': stored.size},
        })

    def post(self, request, *args, **kwargs):
        resolved = self._resolve()
        if isinstance(resolved, JsonResponse):
            return resolved
        participation, problem = resolved

        if problem.submission_type == ProblemSubmissionType.FILE:
            upload = request.FILES.get('file')
            if upload is None:
                return self._error(_('Choose a file to save.'), 400)
            return self._save_file_draft(participation, problem, upload)

        try:
            content = normalize_draft_content(problem, request.POST.get('content', ''))
        except DraftContentError as error:
            return self._error(str(error), 400)

        language = None
        language_id = request.POST.get('language')
        if language_id:
            language = Language.objects.filter(id=language_id).first()
            if language is None or not problem.allowed_languages.filter(id=language.id).exists():
                return self._error(_('That language is not allowed for this problem.'), 400)

        draft, _created = AssignmentDraft.objects.update_or_create(
            participation=participation, problem=problem,
            # An edit after a harvest is a new answer, so it becomes harvestable again.
            defaults={'content': content, 'language': language, 'harvested_at': None},
        )
        return JsonResponse({'saved': True, 'updated_on': draft.updated_on.isoformat()})

    def delete(self, request, *args, **kwargs):
        resolved = self._resolve()
        if isinstance(resolved, JsonResponse):
            return resolved
        participation, problem = resolved
        AssignmentDraft.objects.filter(participation=participation, problem=problem).delete()
        return JsonResponse({'saved': False})
