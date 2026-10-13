"""Student download and teacher inline-grading views for file-upload submissions.

Authoring lives on the unified create_v2 page (``judge/views/problem_create_v2.py``); this module
only covers what happens after a student submits. Uploaded files are graded manually by a teacher,
so grading goes through ``finalize_submission`` (the single source of truth), never a Django-admin
points edit.
"""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from judge.models import ProblemSubmissionType, Submission
from judge.storage import StorageUnavailableError
from judge.submission_grading import finalize_submission


def submission_file_download(request, submission):
    """Stream a submission's uploaded file through Django (private storage, no presigned URLs)."""
    sub = get_object_or_404(Submission, id=submission)
    if not sub.can_see_detail(request.user):
        raise PermissionDenied()
    upload = getattr(sub, 'upload', None)
    if upload is None:
        raise Http404()
    try:
        fh = upload.file.open('rb')
    except StorageUnavailableError:
        return HttpResponse(_('The file store is temporarily unavailable. Please try again later.'),
                            status=503, content_type='text/plain')
    # Force attachment + a neutral content-type: uploaded HTML/SVG must never render inline (stored XSS).
    return FileResponse(fh, as_attachment=True, filename=upload.original_name,
                        content_type='application/octet-stream')


@require_POST
def submission_file_grade(request, submission):
    """Teacher grading for a file-upload submission.

    The student result page no longer hosts this form; a dedicated grade page will
    call this endpoint later. The URL is kept so that page can reuse it.
    """
    sub = get_object_or_404(Submission.objects.select_related('problem'), id=submission)
    if not (request.user.is_authenticated and sub.problem.is_editable_by(request.user)):
        raise PermissionDenied()
    if sub.problem.submission_type != ProblemSubmissionType.FILE:
        raise Http404()

    max_points = sub.problem.points
    try:
        points = float(request.POST.get('points', ''))
    except (TypeError, ValueError):
        messages.error(request, _('Enter a valid number of points.'))
        return redirect('submission_status', sub.id)
    points = max(0.0, min(points, max_points))
    feedback = (request.POST.get('feedback') or '').strip() or None

    finalize_submission(
        sub, case_points=points, case_total=max_points,
        result='AC' if points >= max_points else 'WA',
        status='D', error=feedback,
    )
    messages.success(request, _('Graded submission #%d.') % sub.id)
    return redirect('submission_status', sub.id)
