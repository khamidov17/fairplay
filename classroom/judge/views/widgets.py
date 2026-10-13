import json
import os
import uuid
from urllib.parse import urljoin

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, \
    HttpResponseNotFound, HttpResponseRedirect, JsonResponse
from django.utils.translation import gettext as _, ngettext
from django.views.decorators.http import require_POST

from judge.models import ContestParticipation, Profile, Submission

__all__ = ['rejudge_submission', 'bulk_rejudge_submissions', 'bulk_rescore_submissions']


def _parse_submission_ids(request):
    raw_ids = request.POST.getlist('ids')
    if len(raw_ids) == 1 and ',' in raw_ids[0]:
        raw_ids = [part.strip() for part in raw_ids[0].split(',') if part.strip()]
    ids = []
    for value in raw_ids:
        if not str(value).isdigit():
            return None
        ids.append(int(value))
    return ids


@login_required
@require_POST
def rejudge_submission(request):
    if 'id' not in request.POST or not request.POST['id'].isdigit():
        return HttpResponseBadRequest()

    try:
        submission = Submission.objects.get(id=request.POST['id'])
    except Submission.DoesNotExist:
        return HttpResponseBadRequest()

    if not submission.problem.is_subs_manageable_by(request.user):
        return HttpResponseForbidden()

    # Short-answer / AI fill-blank: plain rejudge only writes IR placeholders
    # ("AI grading is off…") because should_run_ai_now is false. Teachers
    # clicking Rejudge expect a fresh LLM pass — queue it on Celery so the
    # gunicorn worker is not blocked on the model call.
    from django.utils import timezone

    from judge.models import ProblemSubmissionType, SubmissionTestCase
    from judge.utils.contest_ai import problem_has_ai_grading

    st = submission.problem.submission_type
    if (st in (ProblemSubmissionType.SHORT_ANSWER, ProblemSubmissionType.FILL_BLANK) and
            problem_has_ai_grading(submission.problem)):
        # Mirror judge_submission's requeue so Grade-page polling sees
        # is_graded=False until the Celery task finishes.
        Submission.objects.filter(id=submission.id).update(
            status='QU', result=None, rejudged_date=timezone.now(),
            time=None, memory=None, points=None, case_points=0, error=None,
        )
        SubmissionTestCase.objects.filter(submission_id=submission.id).delete()
        from judge.tasks.contest import ai_rejudge_submission
        ai_rejudge_submission.delay(submission.id, request.user.id)
    else:
        submission.judge(rejudge=True, rejudge_user=request.user)

    redirect = request.POST.get('path', None)
    if redirect:
        return HttpResponseRedirect(redirect)

    # Grade-page / AJAX callers refresh status themselves; return JSON so the
    # UI can update without a blocking alert(responseText).
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or \
            'application/json' in (request.headers.get('accept') or ''):
        submission.refresh_from_db()
        return JsonResponse({
            'ok': True,
            'id': submission.id,
            'status': submission.status,
            'result': submission.result,
            'is_graded': submission.is_graded,
            'was_rejudged': submission.rejudged_date is not None,
            'queued': True,
        })
    return HttpResponse('success', content_type='text/plain')


@login_required
@require_POST
def bulk_rejudge_submissions(request):
    """Bulk rejudge selected submissions (mirrors Admin SubmissionAdmin.judge)."""
    if not request.user.has_perm('judge.rejudge_submission') or not request.user.has_perm('judge.edit_own_problem'):
        return JsonResponse({
            'ok': False,
            'processed': 0,
            'message': _('You do not have the permission to rejudge submissions.'),
        }, status=403)

    ids = _parse_submission_ids(request)
    if not ids:
        return JsonResponse({
            'ok': False,
            'processed': 0,
            'message': _('No submissions selected.'),
        }, status=400)

    queryset = Submission.objects.filter(id__in=ids).order_by('id')
    if not request.user.has_perm('judge.rejudge_submission_lot') and \
            queryset.count() > settings.DMOJ_SUBMISSIONS_REJUDGE_LIMIT:
        return JsonResponse({
            'ok': False,
            'processed': 0,
            'message': _('You do not have the permission to rejudge THAT many submissions.'),
        }, status=403)

    if not request.user.has_perm('judge.edit_all_problem'):
        profile_id = request.profile.id
        queryset = queryset.filter(
            Q(problem__authors__id=profile_id) | Q(problem__curators__id=profile_id),
        ).distinct()

    judged = len(queryset)
    for submission in queryset:
        submission.judge(rejudge=True, batch_rejudge=True, rejudge_user=request.user)

    message = ngettext(
        '%d submission was successfully scheduled for rejudging.',
        '%d submissions were successfully scheduled for rejudging.',
        judged,
    ) % judged
    return JsonResponse({'ok': True, 'processed': judged, 'message': message})


@login_required
@require_POST
def bulk_rescore_submissions(request):
    """Bulk rescore selected submissions (mirrors Admin recalculate_score)."""
    if not request.user.has_perm('judge.rejudge_submission'):
        return JsonResponse({
            'ok': False,
            'processed': 0,
            'message': _('You do not have the permission to rejudge submissions.'),
        }, status=403)

    ids = _parse_submission_ids(request)
    if not ids:
        return JsonResponse({
            'ok': False,
            'processed': 0,
            'message': _('No submissions selected.'),
        }, status=400)

    # Match SubmissionAdmin.get_queryset: limited staff may only touch
    # submissions for problems they author or curate (recalculate_score itself
    # only checks rejudge_submission; admin changelist queryset is already scoped).
    queryset = Submission.objects.filter(id__in=ids)
    if not request.user.has_perm('judge.edit_all_problem'):
        profile_id = request.profile.id
        queryset = queryset.filter(
            Q(problem__authors__id=profile_id) | Q(problem__curators__id=profile_id),
        ).distinct()

    if not request.user.has_perm('judge.rejudge_submission_lot') and \
            queryset.count() > settings.DMOJ_SUBMISSIONS_REJUDGE_LIMIT:
        return JsonResponse({
            'ok': False,
            'processed': 0,
            'message': _('You do not have the permission to rejudge THAT many submissions.'),
        }, status=403)

    submissions = list(queryset.defer(None).select_related(None).select_related('problem')
                       .only('points', 'case_points', 'case_total', 'problem__partial', 'problem__points'))
    for submission in submissions:
        submission.points = round(submission.case_points / submission.case_total * submission.problem.points
                                  if submission.case_total else 0, 1)
        if not submission.problem.partial and submission.points < submission.problem.points:
            submission.points = 0
        submission.save()
        submission.update_contest()

    user_ids = queryset.values_list('user_id', flat=True).distinct()
    for profile in Profile.objects.filter(id__in=user_ids):
        profile.calculate_points()
        cache.delete('user_complete:%d' % profile.id)
        cache.delete('user_attempted:%d' % profile.id)

    participation_ids = queryset.values_list('contest__participation_id', flat=True)
    for participation in ContestParticipation.objects.filter(id__in=participation_ids).prefetch_related('contest'):
        participation.recompute_results()

    processed = len(submissions)
    message = ngettext(
        '%d submission was successfully rescored.',
        '%d submissions were successfully rescored.',
        processed,
    ) % processed
    return JsonResponse({'ok': True, 'processed': processed, 'message': message})


def django_uploader(image):
    ext = os.path.splitext(image.name)[1]
    if ext not in settings.MARTOR_UPLOAD_SAFE_EXTS:
        ext = '.png'
    name = str(uuid.uuid4()) + ext
    default_storage.save(os.path.join(settings.MARTOR_UPLOAD_MEDIA_DIR, name), image)
    url_base = getattr(settings, 'MARTOR_UPLOAD_URL_PREFIX',
                       urljoin(settings.MEDIA_URL, settings.MARTOR_UPLOAD_MEDIA_DIR))
    if not url_base.endswith('/'):
        url_base += '/'
    return json.dumps({'status': 200, 'name': '', 'link': urljoin(url_base, name)})


@login_required
def martor_image_uploader(request):
    if not request.user.is_staff:
        return HttpResponseNotFound()
    if request.method != 'POST' or 'markdown-image-upload' not in request.FILES:
        return HttpResponseBadRequest('Invalid request')

    image = request.FILES['markdown-image-upload']
    data = django_uploader(image)
    return HttpResponse(data, content_type='application/json')
