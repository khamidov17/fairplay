"""Practice (non-assignment) teacher Grade workspace.

Assignment grading lives under ``contest/<key>/grade``. Practice submissions
have no contest, so they share the same ``contest/grade.html`` shell at
``/submissions/grade`` with a list scoped to practice rows the viewer can
edit.
"""
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.generic import TemplateView, View

from judge.models import Problem, Submission, SubmissionManualGrade
from judge.utils.contest_grade import (
    apply_manual_grade, build_grade_payload, can_open_practice_grade,
    grade_points_total, practice_grade_page, practice_grade_queryset,
)


def _practice_grade_problems(submissions, request):
    from judge.jinja2.markdown import markdown as render_markdown

    math_engine = getattr(getattr(request, 'profile', None), 'math_engine', None)
    if math_engine == 'auto':
        math_engine = getattr(settings, 'MATHOID_DEFAULT_TYPE', 'jax')
    seen = []
    seen_ids = set()
    for sub in submissions:
        if sub.problem_id in seen_ids:
            continue
        seen_ids.add(sub.problem_id)
        seen.append(sub.problem)
    seen.sort(key=lambda p: (p.name, p.code))
    return [
        {
            'code': problem.code,
            'name': problem.name,
            'letter': '',
            'points': problem.points,
            'description': problem.description or '',
            'description_html': str(render_markdown(
                problem.description or '', problem.markdown_style, math_engine)),
        }
        for problem in seen
    ]


class PracticeGradeAuthMixin(object):
    """401 anonymous (JSON) / login redirect (HTML); 404 if the user cannot grade practice."""

    html_login_redirect = False

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            if self.html_login_redirect:
                return redirect_to_login(request.get_full_path())
            return JsonResponse({'error': _('Not authenticated.')}, status=401)
        if not can_open_practice_grade(request.user):
            if self.html_login_redirect:
                raise Http404()
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        return super().dispatch(request, *args, **kwargs)


class PracticeGrade(PracticeGradeAuthMixin, TemplateView):
    template_name = 'contest/grade.html'
    html_login_redirect = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Bounded page: the desk inlines full source + test cases per row, so it
        # lists the newest N practice rows and pins the ?submission= deep-link
        # target when it falls outside that window.
        pinned_id = self.request.GET.get('submission')
        pinned_id = int(pinned_id) if pinned_id and pinned_id.isdigit() else None
        submissions, final_ids, truncated = practice_grade_page(self.request.user, pinned_id=pinned_id)
        context['contest'] = None
        context['grade_truncated'] = truncated
        context['grade_truncated_count'] = len(submissions)
        context['grade_submissions'] = submissions
        context['grade_problems'] = _practice_grade_problems(submissions, self.request)
        manual_grades = SubmissionManualGrade.objects.filter(
            submission__contest_object__isnull=True,
            submission__problem__in=Problem.get_editable_problems(self.request.user),
        )
        context['grade_released_count'] = manual_grades.filter(released=True).count()
        context['grade_unreleased_count'] = manual_grades.filter(released=False).count()
        context['grade_ai_batch'] = False
        context['grade_ai_batch_done'] = False
        context['grade_save_url'] = reverse('practice_grade_save')
        context['grade_release_url'] = reverse('practice_grade_release')
        context['grade_status_url'] = reverse('practice_grade_status')
        context['grade_ai_url'] = ''
        context['grade_home_url'] = reverse('all_submissions')
        context['grade_home_label'] = _('Practice Grade')
        context['grade_show_participation'] = False
        context['grade_submissions_json'] = build_grade_payload(submissions, final_ids)
        context['grade_problems_json'] = context['grade_problems']
        return context


class PracticeGradeSave(PracticeGradeAuthMixin, View):
    def get(self, request, *args, **kwargs):
        raise Http404()

    def post(self, request, *args, **kwargs):
        try:
            submission_id = int(request.POST.get('submission'))
            score = float(request.POST.get('score') or 0)
        except (TypeError, ValueError):
            return JsonResponse({'error': _('Invalid input.')}, status=400)
        submission = get_object_or_404(
            practice_grade_queryset(request.user), id=submission_id)
        note = request.POST.get('note')
        grade, score, total = apply_manual_grade(
            request.profile, submission, score, note=note)
        return JsonResponse({
            'ok': True,
            'submission': submission_id,
            'score': score,
            'total': total,
            'note': submission.review_note,
            'released': grade.released,
            'reviewed_by': submission.reviewed_by.user.username if submission.reviewed_by else None,
            'reviewed_at': (timezone.localtime(submission.reviewed_at)
                            .strftime('%Y-%m-%d %H:%M') if submission.reviewed_at else None),
        })


class PracticeGradeStatus(PracticeGradeAuthMixin, View):
    http_method_names = ['get']

    def get(self, request, *args, **kwargs):
        try:
            submission_id = int(request.GET.get('submission'))
        except (TypeError, ValueError):
            return JsonResponse({'error': _('Invalid input.')}, status=400)
        try:
            submission = practice_grade_queryset(request.user).filter(id=submission_id) \
                .select_related('language', 'reviewed_by__user') \
                .prefetch_related('test_cases').get()
        except Submission.DoesNotExist:
            return JsonResponse({'error': _('Submission not found.')}, status=404)
        total = grade_points_total(submission)
        manual_grade = submission.manual_grade_override
        if manual_grade is not None and manual_grade.points is not None:
            points = manual_grade.points
        else:
            points = submission.points
        return JsonResponse({
            'ok': True,
            'id': submission.id,
            'status': submission.status,
            'result': submission.result,
            'points': points,
            'total': total,
            'time': submission.time,
            'memory': submission.memory,
            'is_graded': submission.is_graded,
            'review_note': submission.review_note or '',
            'reviewed_by': submission.reviewed_by.user.username if submission.reviewed_by else None,
            'released': bool(manual_grade and manual_grade.released),
            'test_cases': [
                {
                    'case': tc.case,
                    'status': tc.status,
                    'time': tc.time,
                    'memory': tc.memory,
                    'points': tc.points,
                } for tc in submission.test_cases.all()
            ],
        })


class PracticeGradeRelease(PracticeGradeAuthMixin, View):
    def get(self, request, *args, **kwargs):
        raise Http404()

    def post(self, request, *args, **kwargs):
        updated = SubmissionManualGrade.objects.filter(
            submission__contest_object__isnull=True,
            submission__problem__in=Problem.get_editable_problems(request.user),
        ).update(released=True)
        return JsonResponse({'ok': True, 'released': True, 'count': updated})
