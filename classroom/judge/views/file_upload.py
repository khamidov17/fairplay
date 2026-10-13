"""Student download view for file-upload submissions.

Authoring lives on the unified create_v2 page (``judge/views/problem_create_v2.py``); this module
only covers what happens after a student submits. Uploaded files are graded manually by a teacher
on the Grade desk (``judge/views/grade.py`` for practice, ``ContestGrade*`` for assignments), which
respects assignment editing rights and Release All. The old per-submission ``grade-file`` endpoint
was removed: it was authorized by problem ownership and bypassed Release All.
"""
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _

from judge.models import Submission
from judge.storage import StorageUnavailableError


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
