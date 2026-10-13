from ansi2html import Ansi2HTMLConverter
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from judge.models import Language, Problem, Submission, SubmissionSource
from judge.subjects import has_feature
from judge.utils.sandbox_output import sandbox_hide_contest_for_new_run, submission_output_hidden_for
from judge.utils.views import TitleMixin

__all__ = ['IDEView', 'IDESubmitView', 'IDESubmissionStatus']

# IDE configuration constants
MINUTES_TO_SECONDS = 60
IDE_PROBLEM_CODE = 'idepractice'

# Rate limiting configuration
IDE_RATE_LIMIT_COUNT = 5  # runs per window
IDE_RATE_LIMIT_WINDOW = 1  # minutes

# Time/memory limits are NOT set here: the judge takes them from the
# `idepractice` problem's configuration, editable in the problem admin.
IDE_OUTPUT_LIMIT = 10240  # bytes (10 KB) shown to the user; display truncation only

_ansi_converter = Ansi2HTMLConverter(inline=True)


def _ansi_to_html(text):
    if not text:
        return ''
    return _ansi_converter.convert(text, full=False)


def _rate_limited_response():
    return JsonResponse({
        'error':
        f'Rate limit exceeded. Maximum {IDE_RATE_LIMIT_COUNT} runs per {IDE_RATE_LIMIT_WINDOW} minute(s).',
    }, status=429)


def _reserve_ide_run(cache_key):
    """Atomically count one run in the fixed window; False when over the limit."""
    timeout = IDE_RATE_LIMIT_WINDOW * MINUTES_TO_SECONDS
    for _attempt in range(2):
        cache.add(cache_key, 0, timeout=timeout)
        try:
            count = cache.incr(cache_key)
        except ValueError:
            # The key expired between add() and incr(): start a new window.
            continue
        return count <= IDE_RATE_LIMIT_COUNT
    return False


class IDEView(LoginRequiredMixin, TitleMixin, TemplateView):
    """
    Main IDE page with code editor, input editor, and output display.
    """
    template_name = 'ide/ide.html'
    title = 'IDE'

    def get(self, request, *args, **kwargs):
        # The IDE is part of the code pack: users whose courses are all non-code subjects do not get it.
        if not has_feature(request.user, 'ide'):
            raise Http404()
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        languages = Language.objects.filter(
            judges__online=True,
        ).distinct().order_by('name', 'key')
        context['languages'] = languages
        default_lang = self.request.profile.language
        if default_lang is None or not languages.filter(id=default_lang.id).exists():
            default_lang = languages.first()
        context['default_lang'] = default_lang
        # Used only to pick Monaco vs / vs-dark (IDE no longer loads Ace).
        context['ace_theme'] = self.request.profile.resolved_ace_theme
        context['rate_limit_count'] = IDE_RATE_LIMIT_COUNT
        context['rate_limit_window'] = IDE_RATE_LIMIT_WINDOW
        return context


class IDESubmitView(LoginRequiredMixin, View):
    """
    AJAX endpoint to submit IDE code for execution.
    """

    def post(self, request):
        if not has_feature(request.user, 'ide'):
            return JsonResponse({'error': 'The IDE is not available for your courses.'}, status=403)
        # Rate limiting check (fast path; the binding check is the atomic
        # reservation below, just before the run is created).
        cache_key = f'ide_run:{request.profile.id}'
        if (cache.get(cache_key) or 0) >= IDE_RATE_LIMIT_COUNT:
            return _rate_limited_response()

        # Parse request
        source_code = request.POST.get('source', '')
        language_id = request.POST.get('language')
        custom_input = request.POST.get('input', '')

        # Validation
        if not source_code or len(source_code) > 65536:
            return JsonResponse({
                'error': 'Source code must be between 1 and 65536 characters.',
            }, status=400)

        if len(custom_input) > 65536:
            return JsonResponse({
                'error': 'Input must be at most 65536 characters.',
            }, status=400)

        language = Language.objects.filter(
            id=language_id, judges__online=True,
        ).distinct().first()
        if language is None:
            return JsonResponse({
                'error': 'Invalid or unavailable language.',
            }, status=400)

        # Get IDE problem
        try:
            ide_problem = Problem.objects.get(code=IDE_PROBLEM_CODE)
        except Problem.DoesNotExist:
            return JsonResponse({
                'error': 'IDE feature not configured. Contact administrator.',
            }, status=500)

        # Check global submission limit (reuse existing spam check)
        if (not request.user.has_perm('judge.spam_submission') and
            Submission.objects.filter(user=request.profile, rejudged_date__isnull=True)
                              .exclude(status__in=['D', 'IE', 'CE', 'AB'])
                              .count() >= settings.DMOJ_SUBMISSION_LIMIT):
            return JsonResponse({
                'error': 'You have too many submissions in queue. Please wait.',
            }, status=429)

        # Reserve a slot atomically: concurrent requests each get their own
        # count from incr, so a burst cannot pass a stale read of the counter.
        if not _reserve_ide_run(cache_key):
            return _rate_limited_response()

        # Create submission. The "Hide IDE / Run Test output" decision is taken
        # now and stored on the run, so leaving the assignment later does not
        # reveal the output.
        with transaction.atomic():
            submission = Submission(
                user=request.profile,
                problem=ide_problem,
                language=language,
                sandbox_hidden_contest=sandbox_hide_contest_for_new_run(request.user),
            )
            submission.save()

            source = SubmissionSource(
                submission=submission,
                source=source_code,
            )
            source.save()
            submission.source = source

        # Store custom input in cache (accessible by judge callback)
        cache.set(f'ide_input:{submission.id}', custom_input, timeout=600)  # 10 minutes

        # Judge submission with custom limits
        submission.judge(force_judge=True, judge_id=None)

        return JsonResponse({
            'success': True,
            'submission_id': submission.id,
            'submission_url': reverse('ide_submission_status', args=[submission.id]),
        })


class IDESubmissionStatus(LoginRequiredMixin, View):
    """
    AJAX endpoint to check submission status and retrieve output.
    Returns submission progress and truncated output.
    """

    def get(self, request, submission):
        submission_obj = get_object_or_404(Submission, id=submission)

        # Security check: only allow viewing own IDE submissions
        if submission_obj.user_id != request.profile.id:
            return JsonResponse({'error': 'Access denied.'}, status=403)

        if submission_obj.problem.code != IDE_PROBLEM_CODE:
            return JsonResponse({'error': 'Not an IDE submission.'}, status=400)

        # Build response. Text fields that may carry ANSI escape codes from
        # compiler/interpreter output are converted to HTML here; the frontend
        # inserts them as-is. Keep truncation on pre-conversion text to avoid
        # slicing inside an HTML tag.
        hide_output = submission_output_hidden_for(submission_obj, request.user)
        response = {
            'status': submission_obj.status,
            'result': submission_obj.result,
            'error': _ansi_to_html(submission_obj.error),
            'time': submission_obj.time,
            'memory': submission_obj.memory,
        }

        # Add output if graded
        if submission_obj.is_graded:
            test_cases = submission_obj.test_cases.all()
            if test_cases:
                # For IDE, there should be exactly one test case
                test_case = test_cases[0]
                output = test_case.output or ''

                # Truncate to 10KB
                if len(output) > IDE_OUTPUT_LIMIT:
                    output = output[:IDE_OUTPUT_LIMIT] + '\n\n[Output truncated to 10KB]'

                if not hide_output:
                    response['output'] = _ansi_to_html(output)
                response['test_case_status'] = test_case.status
                response['test_case_time'] = test_case.time
                response['test_case_memory'] = test_case.memory
                response['feedback'] = _ansi_to_html(test_case.feedback)
                response['extended_feedback'] = _ansi_to_html(test_case.extended_feedback)

        if hide_output:
            response['output_hidden'] = True

        return JsonResponse(response)
