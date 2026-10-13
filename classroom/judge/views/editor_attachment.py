"""Upload/download for files inserted into the rich-text editor.

Uploads go through Binghan's ``submission_storage`` (OSS primary + Box mirror).
Downloads stream through Django so Markdown can embed a stable site URL; the
object store itself is private (no presigned URLs).
"""
import os
import posixpath

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from judge.models import EditorAttachment
from judge.storage import StorageUnavailableError

MAX_EDITOR_ATTACHMENT_BYTES = 20 * 1024 * 1024
INLINE_IMAGE_TYPES = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
}


def _can_upload_editor_files(user):
    """Non-image files stay instructor-only (staff / problem / contest authors)."""
    if not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    return (
        user.has_perm('judge.create_private_problem') or
        user.has_perm('judge.edit_own_contest')
    )


def _can_upload_editor_attachment(user):
    # Signed-in users may insert images (short-answer students included).
    return user.is_authenticated


def is_inline_image_name(filename):
    ext = posixpath.splitext(filename or '')[1].lower()
    return ext in INLINE_IMAGE_TYPES


def _cache_incr(key, timeout):
    cache.add(key, 0, timeout=timeout)
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=timeout)
        return 1


def _cache_decr(key):
    try:
        cache.decr(key)
    except ValueError:
        pass


def _reserve_counter(key, timeout, limit):
    used = _cache_incr(key, timeout)
    if used > limit:
        _cache_decr(key)
        return False
    return True


def _reserve_student_upload_slots(user):
    """Reserve rate/daily slots. Refund the reservation if the caller never saves."""
    reserved = []
    rate = int(getattr(settings, 'DMOJ_EDITOR_ATTACHMENT_RATE_LIMIT', 0) or 0)
    window = int(getattr(settings, 'DMOJ_EDITOR_ATTACHMENT_RATE_WINDOW_SECONDS', 60) or 0)
    if rate > 0 and window > 0:
        key = 'editor_attach_rate:%s' % user.pk
        if not _reserve_counter(key, window, rate):
            return reserved, JsonResponse({
                'status': 429,
                'error': _('Too many uploads. Please wait and try again.'),
            }, status=429)
        reserved.append(key)

    limit = int(getattr(settings, 'DMOJ_EDITOR_ATTACHMENT_DAILY_LIMIT', 0) or 0)
    if limit > 0:
        day = timezone.now().strftime('%Y-%m-%d')
        key = 'editor_attach_day:%s:%s' % (user.pk, day)
        if not _reserve_counter(key, 26 * 3600, limit):
            for reserved_key in reserved:
                _cache_decr(reserved_key)
            return [], JsonResponse({
                'status': 429,
                'error': _('Daily upload limit reached.'),
            }, status=429)
        reserved.append(key)
    return reserved, None


@login_required
@require_POST
def editor_file_uploader(request):
    if not _can_upload_editor_attachment(request.user):
        return JsonResponse({
            'status': 403,
            'error': _('You do not have permission to upload files.'),
        }, status=403)

    upload = request.FILES.get('markdown-file-upload')
    if upload is None:
        return JsonResponse({'status': 400, 'error': _('No file uploaded.')}, status=400)
    if not upload.size:
        return JsonResponse({'status': 400, 'error': _('The file is empty.')}, status=400)
    if upload.size > MAX_EDITOR_ATTACHMENT_BYTES:
        return JsonResponse({
            'status': 400,
            'error': _('File is too large (maximum %d MB).') % (MAX_EDITOR_ATTACHMENT_BYTES // (1024 * 1024)),
        }, status=400)

    original_name = os.path.basename(upload.name or 'file')[:255] or 'file'
    is_image = is_inline_image_name(original_name)
    if not is_image and not _can_upload_editor_files(request.user):
        return JsonResponse({
            'status': 403,
            'error': _('You can only upload images.'),
        }, status=403)

    student_upload = not _can_upload_editor_files(request.user)
    reserved = []
    if student_upload:
        reserved, denied = _reserve_student_upload_slots(request.user)
        if denied is not None:
            return denied

    profile = getattr(request, 'profile', None)
    attachment = EditorAttachment(uploaded_by=profile, original_name=original_name, size=upload.size)
    try:
        attachment.file.save(original_name, upload, save=False)
        attachment.size = upload.size
        attachment.save()
    except StorageUnavailableError:
        for key in reserved:
            _cache_decr(key)
        return JsonResponse({
            'status': 503,
            'error': _('The file store is temporarily unavailable. Please try again later.'),
        }, status=503)
    except Exception:
        for key in reserved:
            _cache_decr(key)
        raise

    url = request.build_absolute_uri(reverse('editor_file_download', args=[attachment.uuid]))
    return JsonResponse({
        'status': 200,
        'name': original_name,
        'link': url,
        'is_image': is_image,
    })


def editor_file_download(request, file_uuid):
    """Stream an editor attachment. UUID URLs are unguessable; no login required
    so public problem statements can render inserted images/links."""
    attachment = get_object_or_404(EditorAttachment, uuid=file_uuid)
    if not attachment.file:
        raise Http404()
    try:
        handle = attachment.file.open('rb')
    except StorageUnavailableError:
        return HttpResponse(
            _('The file store is temporarily unavailable. Please try again later.'),
            status=503, content_type='text/plain',
        )
    except (OSError, ValueError, FileNotFoundError):
        raise Http404()

    ext = posixpath.splitext(attachment.original_name)[1].lower()
    content_type = INLINE_IMAGE_TYPES.get(ext)
    if content_type:
        return FileResponse(
            handle, as_attachment=False, filename=attachment.original_name,
            content_type=content_type,
        )
    # Force attachment + a neutral type: uploaded HTML/SVG must never render inline.
    return FileResponse(
        handle, as_attachment=True, filename=attachment.original_name,
        content_type='application/octet-stream',
    )
