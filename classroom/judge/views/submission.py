from collections import namedtuple
from datetime import timedelta
from itertools import groupby
from operator import attrgetter

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist, PermissionDenied
from django.db.models import Prefetch, Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _, gettext_lazy
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView

from judge import ai_hints, event_poster as event
from judge.comments import SubmissionCommentMixin
from judge.highlight_code import highlight_code
from judge.models import Contest, ContestProblem, Language, Problem, ProblemTranslation, Profile, Submission
from judge.models.contest import contest_problem_display_name
from judge.models.problem import SubmissionSourceAccess
from judge.utils.contest_grade import attach_student_display_score, grade_desk_url
from judge.utils.infinite_paginator import InfinitePaginationMixin
from judge.utils.lazy import memo_lazy
from judge.utils.problems import get_result_data, user_completed_ids, user_editable_ids, user_tester_ids
from judge.utils.raw_sql import join_sql_subquery, use_straight_join
from judge.utils.result_visibility import results_hidden_q, solved_source_visibility_applies, visible_results
from judge.utils.safe_json import script_json
from judge.utils.sandbox_output import submission_output_hidden_for
from judge.utils.views import DiggPaginatorMixin, TitleMixin, generic_message
from judge.views.ide import IDE_PROBLEM_CODE


def _problem_display_name(problem, language_code):
    """Translated problem title; reuse list-page prefetch when present.

    Matches ``Problem.translated_name`` so list and detail stay consistent,
    but avoids an extra query when ``_trans`` / ``_i18n_name`` was prefetched.
    """
    if getattr(problem, '_i18n_name', None) is not None:
        return problem.i18n_name
    trans = getattr(problem, '_trans', None)
    if trans is not None:
        return trans[0].name if trans else problem.name
    return problem.translated_name(language_code)


def _submission_contest_problem_name(submission, language_code):
    """Assignment-local title when the submission belongs to a contest."""
    if not getattr(submission, 'contest_object_id', None):
        return _problem_display_name(submission.problem, language_code)
    try:
        cp = ContestProblem.objects.select_related('problem').get(
            contest_id=submission.contest_object_id, problem_id=submission.problem_id,
        )
    except ContestProblem.DoesNotExist:
        return _problem_display_name(submission.problem, language_code)
    return contest_problem_display_name(cp, language_code)


def attach_contest_problem_display_names(submissions, language_code):
    """Set ``effective_problem_name`` on each submission from ContestProblem."""
    submissions = list(submissions)
    pairs = {
        (s.contest_object_id, s.problem_id)
        for s in submissions if getattr(s, 'contest_object_id', None)
    }
    name_map = {}
    if pairs:
        contest_ids = {cid for cid, _ in pairs}
        problem_ids = {pid for _, pid in pairs}
        for cp in ContestProblem.objects.filter(
            contest_id__in=contest_ids, problem_id__in=problem_ids,
        ).select_related('problem'):
            name_map[(cp.contest_id, cp.problem_id)] = contest_problem_display_name(cp, language_code)
    for submission in submissions:
        key = (submission.contest_object_id, submission.problem_id)
        if submission.contest_object_id and key in name_map:
            submission.effective_problem_name = name_map[key]
        else:
            submission.effective_problem_name = _problem_display_name(submission.problem, language_code)
    return submissions


def submission_related(queryset):
    return queryset.select_related('user__user', 'problem', 'language') \
        .only('id', 'user__user__username', 'user__display_rank', 'user__rating', 'problem__name',
              'problem__code', 'problem__is_public', 'problem__submission_type', 'problem__points',
              'language__short_name', 'language__key', 'date', 'time', 'memory',
              'points', 'result', 'status', 'case_points', 'case_total', 'current_testcase', 'contest_object',
              'locked_after', 'problem__submission_source_visibility_mode', 'user__username_display_override') \
        .prefetch_related('contest_object__authors', 'contest_object__curators',
                          'manual_grade', 'contest__problem')


class SubmissionPermissionDenied(PermissionDenied):
    def __init__(self, submission):
        self.submission = submission


class SubmissionMixin(object):
    model = Submission
    context_object_name = 'submission'
    pk_url_kwarg = 'submission'


class SubmissionDetailBase(LoginRequiredMixin, TitleMixin, SubmissionMixin, DetailView):
    def get_queryset(self):
        return super().get_queryset().select_related('problem', 'contest__problem') \
            .prefetch_related('manual_grade')

    def get_object(self, queryset=None):
        submission = super(SubmissionDetailBase, self).get_object(queryset)
        if not submission.can_see_detail(self.request.user):
            raise SubmissionPermissionDenied(submission)
        return submission

    def get(self, request, *args, **kwargs):
        try:
            return super().get(request, *args, **kwargs)
        except SubmissionPermissionDenied as e:
            return self.no_permission(e.submission)

    def no_permission(self, submission):
        # get_object() raises before assigning self.object, so use the
        # submission passed from the except handler — not self.object.
        self.object = submission
        problem = submission.problem
        # Only hint "solve it to view" where solving actually grants access; otherwise the
        # two different answers would tell a student whether their hidden exam answer is right.
        if problem.is_accessible_by(self.request.user) and \
                problem.submission_source_visibility == SubmissionSourceAccess.SOLVED and \
                solved_source_visibility_applies(problem):

            message = escape(_('Permission denied. Solve %(problem)s in order to view it.')) % {
                'problem': format_html('<a href="{0}">{1}</a>',
                                       reverse('problem_detail', args=[problem.code]),
                                       _submission_contest_problem_name(submission, self.request.LANGUAGE_CODE)),
            }
            return generic_message(self.request, _("Can't access submission"), mark_safe(message), status=403)
        else:
            return generic_message(self.request, _("Can't access submission"), _('Permission denied.'), status=403)

    def get_title(self):
        submission = self.object
        return _('Submission of %(problem)s by %(user)s') % {
            'problem': _submission_contest_problem_name(submission, self.request.LANGUAGE_CODE),
            'user': submission.user.display_name,
        }

    @cached_property
    def can_open_problem(self):
        # An exam question is closed to its students outside the running exam: no link that 404s.
        return self.object.problem.is_accessible_by(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['can_open_problem'] = self.can_open_problem
        return context

    def get_content_title(self):
        submission = self.object
        problem_name = _submission_contest_problem_name(submission, self.request.LANGUAGE_CODE)
        return mark_safe(escape(_('Submission of %(problem)s by %(user)s')) % {
            'problem': format_html('<a href="{0}">{1}</a>',
                                   reverse('problem_detail', args=[submission.problem.code]),
                                   problem_name) if self.can_open_problem else escape(problem_name),
            'user': format_html('<a href="{0}">{1}</a>',
                                reverse('user_page', args=[submission.user.user.username]),
                                submission.user.display_name),
        })


class SubmissionSource(SubmissionDetailBase):
    template_name = 'submission/source.html'

    def get_queryset(self):
        return super().get_queryset().select_related('source')

    def get_context_data(self, **kwargs):
        context = super(SubmissionSource, self).get_context_data(**kwargs)
        submission = self.object
        context['raw_source'] = submission.source.source.rstrip('\n')
        context['highlighted_source'] = highlight_code(submission.source.source, submission.language.pygments)
        return context


def make_batch(batch, cases):
    result = {'id': batch, 'cases': cases}
    if batch:
        result['points'] = min(map(attrgetter('points'), cases))
        result['total'] = max(map(attrgetter('total'), cases))
    return result


TestCase = namedtuple('TestCase', 'id status batch num_combined')


def get_statuses(batch, cases):
    cases = [TestCase(id=case.id, status=case.status, batch=batch, num_combined=1) for case in cases]
    if batch:
        # Get the first non-AC case if it exists.
        return [next((case for case in cases if case.status != 'AC'), cases[0])]
    else:
        return cases


def combine_statuses(status_cases, submission):
    ret = []
    # If the submission is not graded and the final case is a batch,
    # we don't actually know if it is completed or not, so just remove it.
    if not submission.is_graded and len(status_cases) > 0 and status_cases[-1].batch is not None:
        status_cases.pop()

    for key, group in groupby(status_cases, key=attrgetter('status')):
        group = list(group)
        if len(group) > 10:
            # Grab the first case's id so the user can jump to that case, and combine the rest.
            ret.append(TestCase(id=group[0].id, status=key, batch=None, num_combined=len(group)))
        else:
            ret.extend(group)
    return ret


def group_test_cases(cases):
    result = []
    status = []
    buf = []
    max_execution_time = 0.0
    last = None
    for case in cases:
        # Supplementary AI rubric rows (batch=-1, see question_types.code.AI_GRADE_BATCH) are
        # teacher-only feedback; exclude them from the student-facing status aggregation.
        if case.batch == -1:
            continue
        if case.time:
            max_execution_time = max(max_execution_time, case.time)
        if case.batch != last and buf:
            result.append(make_batch(last, buf))
            status.extend(get_statuses(last, buf))
            buf = []
        buf.append(case)
        last = case.batch
    if buf:
        result.append(make_batch(last, buf))
        status.extend(get_statuses(last, buf))
    return result, status, max_execution_time


class SubmissionStatus(SubmissionCommentMixin, SubmissionDetailBase):
    template_name = 'submission/status.html'
    # If a submission stays Queued this long with no judge pickup, finalize as Aborted
    # so the student sees a terminal result and can resubmit. No background requeue —
    # page GETs must not spawn judge threads (race / duplicate grading).
    QU_TIMEOUT_SECONDS = 40

    def get_context_data(self, **kwargs):
        context = super(SubmissionStatus, self).get_context_data(**kwargs)
        submission = self.object
        context['last_msg'] = event.last()

        self._finalize_queued_timeout(submission)

        context['batches'], statuses, context['max_execution_time'] = group_test_cases(submission.test_cases.all())
        # "Hide IDE / Run Test output": never print a run's stdout on this page while
        # the assignment that hid it is running (see judge.utils.sandbox_output).
        context['sandbox_output_hidden'] = submission_output_hidden_for(submission, self.request.user)
        context['statuses'] = combine_statuses(statuses, submission)

        context['time_limit'] = submission.problem.time_limit
        try:
            lang_limit = submission.problem.language_limits.get(language=submission.language)
        except ObjectDoesNotExist:
            pass
        else:
            context['time_limit'] = lang_limit.time_limit

        # Get the first failed test case ID for download button
        context['first_failed_case_id'] = None
        first_failed = (
            submission.test_cases
            .exclude(batch=-1)
            .exclude(status='AC')
            .exclude(status='SC')
            .order_by('case')
            .first()
        )
        if first_failed:
            context['first_failed_case_id'] = first_failed.case

        # AI hints context: the assignment this submission belongs to decides,
        # else the problem flag. Never offered while the verdict is hidden from
        # this viewer: whether the button appears would reveal AC / not AC.
        context['ai_hints_possible'] = (
            ai_hints.hints_enabled_for(submission.problem, submission.contest_object, self.request.user)
            and submission.results_visible_to(self.request.user)
            and ai_hints.is_configured()
        )
        context['ai_hints_enabled'] = (
            context['ai_hints_possible']
            and submission.status == 'D'
            and submission.result != 'AC'
        )
        if context['ai_hints_possible'] and self.request.user.is_authenticated:
            context['ai_hints_remaining'] = ai_hints.get_remaining(self.request.profile.id)
        else:
            context['ai_hints_remaining'] = 0

        from judge.question_types.code import AI_GRADE_BATCH
        from judge.utils.contest_ai import (
            AI_BATCH_PENDING, ai_grade_context, contest_awaits_student_ai, problem_has_ai_grading,
            student_can_see_code_ai,
        )
        context['ai_batch_pending'] = (submission.error or '') == AI_BATCH_PENDING
        show_code_ai = student_can_see_code_ai(self.request.user, submission)
        context['ai_rubric_cases'] = list(
            submission.test_cases.filter(batch=AI_GRADE_BATCH).order_by('case'),
        ) if show_code_ai else []
        context.update(ai_grade_context(self.request.user, submission))
        context['can_trigger_ai_grade'] = context['can_trigger']
        context['problem_has_ai_grading'] = problem_has_ai_grading(submission.problem)
        context['ai_awaiting_manual'] = (
            contest_awaits_student_ai(submission)
            and submission.result == 'IR'
            and context['can_trigger_ai_grade']
        )

        context['enable_comments'] = settings.DMOJ_ENABLE_COMMENTS
        if settings.DMOJ_ENABLE_COMMENTS:
            context.update(self.get_comment_context())

        attach_student_display_score(submission)
        context['grade_desk_url'] = grade_desk_url(submission, self.request.user)
        from judge.views.problem import workspace_review_note_html
        context['review_note_html'] = (
            '' if not submission.results_visible_to(self.request.user)
            else workspace_review_note_html(submission)
        )

        return context

    def post(self, request, *args, **kwargs):
        try:
            self.object = self.get_object()
        except SubmissionPermissionDenied as e:
            return self.no_permission(e.submission)

        response, form = self.handle_comment_post(request)
        if response is not None:
            return response

        context = self.get_context_data(object=self.object)
        context['comment_form'] = form
        return self.render_to_response(context)

    def _finalize_queued_timeout(self, submission):
        """If still Queued past QU_TIMEOUT_SECONDS, mark Aborted (timeout judgment).

        Uses a conditional UPDATE so concurrent status-page loads only win once;
        no daemon threads / force_judge from the request path.
        """
        if submission.status != 'QU' or submission.judged_date is not None:
            return
        # Only bridge-judged (code) submissions can be stuck waiting for a judge.
        # Manual types (file upload) sit at QU on purpose until a teacher grades
        # them; viewing their status page must never change their state.
        from judge.question_types import get_grader
        try:
            if not get_grader(submission.problem.submission_type).routes_to_bridge:
                return
        except NotImplementedError:
            return
        cutoff = timezone.now() - timedelta(seconds=self.QU_TIMEOUT_SECONDS)
        if submission.date > cutoff:
            return
        updated = Submission.objects.filter(
            id=submission.id,
            status='QU',
            judged_date__isnull=True,
            date__lte=cutoff,
        ).update(status='AB', result='AB', points=0)
        if not updated:
            return
        submission.refresh_from_db(fields=['status', 'result', 'points'])
        submission.update_contest()
        event.post('sub_%s' % submission.id_secret, {
            'type': 'grading-end',
            'result': 'AB',
            'points': 0,
            'total': float(submission.problem.points or 0),
        })


class SubmissionTestCaseQuery(SubmissionStatus):
    template_name = 'submission/status-testcases.html'

    def get(self, request, *args, **kwargs):
        if 'id' not in request.GET or not request.GET['id'].isdigit():
            return HttpResponseBadRequest()
        self.kwargs[self.pk_url_kwarg] = kwargs[self.pk_url_kwarg] = int(request.GET['id'])
        return super(SubmissionTestCaseQuery, self).get(request, *args, **kwargs)


class SubmissionAIHint(LoginRequiredMixin, SubmissionMixin, DetailView):
    """POST /submission/<id>/ai-hint — returns JSON AI hint."""

    def post(self, request, *args, **kwargs):
        submission = self.get_object()

        # Gate 1: the assignment this submission was made in decides, else the
        # problem flag (does not mutate Problem.ai_hints_enabled).
        if not ai_hints.hints_enabled_for(submission.problem, submission.contest_object, request.user):
            return JsonResponse({'error': 'AI hints are not enabled for this problem.'}, status=403)

        # Gate 1b: a hint is built from the verdict and failing cases, so it must
        # not be answered while that verdict is hidden from this user.
        if not submission.results_visible_to(request.user):
            return JsonResponse({'error': 'AI hints are not available while results are hidden.'}, status=403)

        # Gate 2: backend configured
        if not ai_hints.is_configured():
            return JsonResponse({'error': 'AI hints are not configured on this server.'}, status=503)

        # Gate 3: submission belongs to this user (or staff can bypass)
        if submission.user_id != request.profile.id and not request.user.has_perm('judge.change_submission'):
            return JsonResponse({'error': 'You may only get hints for your own submissions.'}, status=403)

        # Gate 4: quota — atomically reserve a slot up front; refunded if the call fails
        if not ai_hints.try_reserve(request.profile.id):
            return JsonResponse({'error': ai_hints.quota_exhausted_message()}, status=429)

        from judge.models.ai_hints import AIHintLog

        log = AIHintLog(
            user_id=request.profile.id,
            submission=submission,
            problem=submission.problem,
            submission_result=submission.result,
            submission_date=submission.date,
        )
        try:
            result = ai_hints.get_hint(submission)
            log.success = True
            log.hint_text = result.hint_text
            log.prompt_tokens = result.prompt_tokens
            log.completion_tokens = result.completion_tokens
            log.total_tokens = result.total_tokens
            log.model_name = result.model_name
            log.response_time_ms = result.response_time_ms
        except Exception as e:
            ai_hints.refund(request.profile.id)
            log.success = False
            log.error_message = str(e)[:2000]
            log.save()
            # Never leak backend internals (exception types, endpoints) to the student — log the
            # detail server-side and return a stable, friendly message instead.
            import logging
            logging.getLogger(__name__).warning('AI hint failed for submission %d: %s', submission.id, e)
            return JsonResponse(
                {'error': 'Could not reach the AI service. Please try again later.'}, status=503)

        log.save()
        return JsonResponse({'hint': result.hint_text, 'remaining': ai_hints.get_remaining(request.profile.id)})


class SubmissionAIGrade(LoginRequiredMixin, SubmissionMixin, DetailView):
    """POST /submission/<id>/ai-grade — student/teacher trigger, latest submit only."""

    def post(self, request, *args, **kwargs):
        from django.utils.translation import gettext as _

        from judge import llm
        from judge.ai_grading_quota import (
            is_teacher_viewer,
            rate_limit_remaining_seconds,
            rate_window_seconds,
            try_reserve,
        )
        from judge.utils.contest_ai import ai_grade_context, run_ai_for_submission

        submission = self.get_object()
        # Only someone who may see the submission at all (owner, its editors)
        # can trigger AI scoring on it; ``ai_grade_context`` then scopes the
        # teacher path to editors of that assignment/problem.
        if not submission.can_see_detail(request.user):
            return JsonResponse({'error': _('Permission denied.')}, status=403)
        ctx = ai_grade_context(request.user, submission)
        if not ctx['can_trigger']:
            if ctx.get('quota_disabled'):
                return JsonResponse({'error': _('AI grading is not available for students yet.')}, status=403)
            if ctx.get('rate_limited'):
                return JsonResponse({
                    'error': _('Too many AI grading requests for this problem. Try again in %(seconds)s seconds.') % {
                        'seconds': ctx.get('rate_limit_seconds', rate_window_seconds()),
                    },
                }, status=429)
            return JsonResponse(
                {'error': _('You can only request AI grading on your latest submission.')},
                status=403,
            )
        if not llm.is_configured():
            return JsonResponse({'error': _('AI grading is not configured on this server.')}, status=503)

        reserved = True
        if not is_teacher_viewer(request.user, submission):
            reserved, err = try_reserve(submission.user_id, submission.problem_id)
            if not reserved:
                if err and err.startswith('rate_limited'):
                    return JsonResponse({
                        'error': _('Please try again later. Try again in %(seconds)s seconds.') % {
                            'seconds': rate_limit_remaining_seconds(submission.user_id, submission.problem_id),
                        },
                    }, status=429)
                if err == 'quota_disabled':
                    return JsonResponse({'error': _('AI grading is not available for students yet.')}, status=403)
                return JsonResponse({'error': _('AI grading quota exhausted.')}, status=429)

        try:
            status = run_ai_for_submission(submission, manual_trigger=True)
        except Exception:
            import logging
            from judge.ai_grading_quota import refund
            if not is_teacher_viewer(request.user, submission):
                refund(submission.user_id)
            logging.getLogger(__name__).exception(
                'Manual AI grade failed for submission %d', submission.id,
            )
            return JsonResponse(
                {'error': _('Could not reach the AI service. Please try again later.')},
                status=503,
            )
        rate_remaining = None
        if not is_teacher_viewer(request.user, submission):
            from judge.ai_grading_quota import rate_limit_remaining_count
            rate_remaining = rate_limit_remaining_count(submission.user_id, submission.problem_id)
        return JsonResponse({'ok': True, 'status': status, 'rate_remaining': rate_remaining})


class SubmissionSourceRaw(SubmissionSource):
    def get(self, request, *args, **kwargs):
        submission = self.get_object()
        return HttpResponse(submission.source.source, content_type='text/plain')


@require_POST
def abort_submission(request, submission):
    submission = get_object_or_404(Submission, id=int(submission))
    if (not request.user.has_perm('judge.abort_any_submission') and
       (submission.rejudged_date is not None or request.profile != submission.user)):
        raise PermissionDenied()
    submission.abort()
    return HttpResponseRedirect(reverse('submission_status', args=(submission.id,)))


def filter_submissions_by_visible_problems(queryset, user):
    join_sql_subquery(
        queryset,
        subquery=str(Problem.get_visible_problems(user).only('id').query),
        params=[],
        join_fields=[('problem_id', 'id')],
        alias='visible_problems',
        related_model=Problem,
    )


class SubmissionsListBase(DiggPaginatorMixin, TitleMixin, ListView):
    model = Submission
    paginate_by = 50
    show_problem = True
    title = gettext_lazy('All submissions')
    content_title = gettext_lazy('All submissions')
    tab = 'all_submissions_list'
    template_name = 'submission/list.html'
    context_object_name = 'submissions'
    first_page_href = None
    filter_problems = ()
    filter_users = ()
    filter_contests = ()

    def get_result_data(self):
        result = self._get_result_data()
        for category in result['categories']:
            category['name'] = _(category['name'])
        return result

    def _get_result_data(self, queryset=None):
        if queryset is None:
            queryset = self.get_queryset()
        # Counts per result must not include verdicts hidden from this viewer.
        return get_result_data(visible_results(queryset, self.request.user).order_by())

    def access_check(self, request):
        pass

    @cached_property
    def in_contest(self):
        return self.request.user.is_authenticated and self.request.profile.current_contest is not None

    @cached_property
    def contest(self):
        return self.request.profile.current_contest.contest

    def _get_queryset(self):
        queryset = Submission.objects.all()
        use_straight_join(queryset)
        queryset = submission_related(queryset.order_by('-id'))
        if self.show_problem:
            queryset = queryset.prefetch_related(Prefetch('problem__translations',
                                                          queryset=ProblemTranslation.objects.filter(
                                                              language=self.request.LANGUAGE_CODE), to_attr='_trans'))
        if self.in_contest:
            queryset = queryset.filter(contest_object=self.contest)
            if not self.contest.can_see_full_scoreboard(self.request.user):
                queryset = queryset.filter(user=self.request.profile)
        else:
            queryset = queryset.select_related('contest_object').defer('contest_object__description')

            if not self.request.user.has_perm('judge.see_private_contest'):
                # Show submissions for any contest you can edit or where you can see submissions
                contest_queryset = Contest.objects.filter(
                    Q(authors=self.request.profile) |
                    Q(curators=self.request.profile) |
                    Q(tester_see_submissions=True, testers=self.request.profile) |
                    Q(view_contest_submissions=self.request.profile) |
                    Q(scoreboard_visibility=Contest.SCOREBOARD_VISIBLE) |
                    Q(end_time__lt=timezone.now(), scoreboard_visibility__in=(
                        Contest.SCOREBOARD_AFTER_PARTICIPATION,
                        Contest.SCOREBOARD_AFTER_CONTEST,
                    )),
                ).distinct()

                queryset = queryset.filter(
                    Q(user=self.request.profile) |
                    Q(contest_object__in=contest_queryset) |
                    Q(contest_object__isnull=True),
                )

        if self.selected_languages:
            # MariaDB can't optimize this subquery for some insane, unknown reason,
            # so we are forcing an eager evaluation to get the IDs right here.
            # Otherwise, with multiple language filters, MariaDB refuses to use an index
            # (or runs the subquery for every submission, which is even more horrifying to think about).
            queryset = queryset.filter(language__in=list(
                Language.objects.filter(key__in=self.selected_languages).values_list('id', flat=True)))
        if self.selected_statuses:
            # Filtering by result is only allowed over verdicts this viewer may see; otherwise
            # ``?status=AC`` would reveal a hidden exam answer's verdict.
            queryset = visible_results(queryset, self.request.user).filter(result__in=self.selected_statuses)

        if self.filter_problems:
            queryset = queryset.filter(problem__code__in=self.filter_problems)
        if self.filter_users:
            queryset = queryset.filter(user__user__username__in=self.filter_users)
        if self.filter_contests:
            include_practice = '__none__' in self.filter_contests
            contest_keys = [key for key in self.filter_contests if key != '__none__']
            contest_q = Q()
            if include_practice:
                contest_q |= Q(contest_object__isnull=True)
            if contest_keys:
                contest_q |= Q(contest_object__key__in=contest_keys)
            queryset = queryset.filter(contest_q)

        # Filter out IDE practice submissions except when viewing the idepractice problem page
        if not (hasattr(self, 'problem') and self.problem.code == IDE_PROBLEM_CODE):
            queryset = queryset.exclude(problem__code=IDE_PROBLEM_CODE)

        return queryset

    def get_queryset(self):
        queryset = self._get_queryset()
        if not self.in_contest:
            filter_submissions_by_visible_problems(queryset, self.request.user)

        return queryset

    def get_my_submissions_page(self):
        return None

    def get_all_submissions_page(self):
        return reverse('all_submissions')

    def get_searchable_status_codes(self):
        hidden_codes = ['SC']
        if not self.request.user.is_superuser and not self.request.user.is_staff:
            hidden_codes += ['IE']
        return [(key, value) for key, value in Submission.RESULT if key not in hidden_codes]

    def _filter_problem_options(self):
        if not self.filter_problems:
            return []
        problems = {
            p.code: '%s — %s' % (p.code, p.name)
            for p in Problem.objects.filter(code__in=self.filter_problems).only('code', 'name')
        }
        return [(code, problems.get(code, code)) for code in self.filter_problems]

    def _filter_contest_options(self):
        if not self.filter_contests:
            return []
        contests = {
            c.key: '%s — %s' % (c.key, c.name)
            for c in Contest.objects.filter(key__in=self.filter_contests).only('key', 'name')
        }
        options = []
        for key in self.filter_contests:
            if key == '__none__':
                options.append((key, _('Practice only (no contest)')))
            else:
                options.append((key, contests.get(key, key)))
        return options

    def get_context_data(self, **kwargs):
        context = super(SubmissionsListBase, self).get_context_data(**kwargs)
        authenticated = self.request.user.is_authenticated
        context['dynamic_update'] = False
        context['dynamic_contest_id'] = self.in_contest and self.contest.id
        context['show_problem'] = self.show_problem

        profile = self.request.profile
        context['completed_problem_ids'] = memo_lazy(lambda: user_completed_ids(profile), set) if authenticated else []
        context['editable_problem_ids'] = memo_lazy(lambda: user_editable_ids(profile), set) if authenticated else []
        context['tester_problem_ids'] = memo_lazy(lambda: user_tester_ids(profile), set) if authenticated else []

        context['all_languages'] = Language.objects.all().values_list('key', 'name')
        context['selected_languages'] = self.selected_languages

        context['all_statuses'] = self.get_searchable_status_codes()
        context['selected_statuses'] = self.selected_statuses

        context['filter_problems'] = self.filter_problems
        context['filter_users'] = self.filter_users
        context['filter_contests'] = self.filter_contests
        context['filter_problem_options'] = self._filter_problem_options()
        context['filter_contest_options'] = self._filter_contest_options()
        # Hide filters already implied by the URL scope (per-problem / per-user / in-contest).
        context['show_problem_filter'] = self.show_problem
        context['show_user_filter'] = not hasattr(self, 'profile')
        context['show_contest_filter'] = not self.in_contest
        context['scope_contest'] = self.contest if self.in_contest else None

        context['results_json'] = script_json(self.get_result_data())
        context['results_colors_json'] = script_json(settings.DMOJ_STATS_SUBMISSION_RESULT_COLORS)

        context['page_suffix'] = suffix = ('?' + self.request.GET.urlencode()) if self.request.GET else ''
        context['first_page_href'] = (self.first_page_href or '.') + suffix
        context['my_submissions_link'] = self.get_my_submissions_page()
        context['all_submissions_link'] = self.get_all_submissions_page()
        context['tab'] = self.tab
        submissions = context.get('submissions')
        if submissions is not None:
            attach_contest_problem_display_names(submissions, self.request.LANGUAGE_CODE)
            for submission in submissions:
                attach_student_display_score(submission)
        return context

    def get(self, request, *args, **kwargs):
        check = self.access_check(request)
        if check is not None:
            return check

        self.selected_languages = set(request.GET.getlist('language'))
        self.selected_statuses = set(request.GET.getlist('status'))
        self.filter_problems = tuple(p for p in request.GET.getlist('problem') if p.strip())
        self.filter_users = tuple(u for u in request.GET.getlist('user') if u.strip())
        self.filter_contests = tuple(c for c in request.GET.getlist('contest') if c.strip())

        if 'results' in request.GET:
            return JsonResponse(self.get_result_data())

        return super(SubmissionsListBase, self).get(request, *args, **kwargs)


class UserMixin(object):
    def get(self, request, *args, **kwargs):
        if 'user' not in kwargs:
            raise ImproperlyConfigured('Must pass a user')
        self.profile = get_object_or_404(Profile, user__username=kwargs['user'])
        self.username = kwargs['user']
        return super(UserMixin, self).get(request, *args, **kwargs)


class ConditionalUserTabMixin(object):
    @cached_property
    def is_own(self):
        return self.request.user.is_authenticated and self.request.profile == self.profile

    def get_context_data(self, **kwargs):
        context = super(ConditionalUserTabMixin, self).get_context_data(**kwargs)
        if self.is_own:
            context['tab'] = 'my_submissions_tab'
        else:
            context['tab'] = 'user_submissions_tab'
            context['tab_username'] = self.profile.display_name
        return context


class AllUserSubmissions(ConditionalUserTabMixin, UserMixin, SubmissionsListBase):
    def get_queryset(self):
        return super(AllUserSubmissions, self).get_queryset().filter(user_id=self.profile.id)

    def get_title(self):
        if self.is_own:
            return _('All my submissions')
        return _('All submissions by %s') % self.profile.display_name

    def get_content_title(self):
        if self.is_own:
            return _('All my submissions')
        return mark_safe(escape(_('All submissions by %s')) % (
            format_html('<a href="{1}">{0}</a>', self.profile.display_name,
                        reverse('user_page', args=[self.username])),
        ))

    def get_my_submissions_page(self):
        if self.request.user.is_authenticated:
            return reverse('all_user_submissions', kwargs={'user': self.request.user.username})

    def get_context_data(self, **kwargs):
        context = super(AllUserSubmissions, self).get_context_data(**kwargs)
        context['dynamic_update'] = context['page_obj'].number == 1
        context['dynamic_user_id'] = self.profile.id
        context['last_msg'] = event.last()
        return context


class ProblemSubmissionsBase(SubmissionsListBase):
    show_problem = False
    dynamic_update = True
    check_contest_in_access_check = True

    def get_queryset(self):
        if self.in_contest and not self.contest.contest_problems.filter(problem_id=self.problem.id).exists():
            raise Http404()
        return super(ProblemSubmissionsBase, self)._get_queryset().filter(problem_id=self.problem.id)

    def get_title(self):
        return _('All submissions for %s') % self.problem_name

    def get_content_title(self):
        return mark_safe(escape(_('All submissions for %s')) % (
            format_html('<a href="{1}">{0}</a>', self.problem_name,
                        reverse('problem_detail', args=[self.problem.code])),
        ))

    def access_check_contest(self, request):
        if self.in_contest and not self.contest.can_see_own_scoreboard(request.user):
            raise Http404()

    def access_check(self, request):
        # FIXME: This should be rolled into the `is_accessible_by` check when implementing #1509
        if self.in_contest and request.user.is_authenticated and request.profile.id in self.contest.editor_ids:
            return

        if not self.problem.is_accessible_by(request.user):
            raise Http404()

        if self.check_contest_in_access_check:
            self.access_check_contest(request)

    def get(self, request, *args, **kwargs):
        if 'problem' not in kwargs:
            raise ImproperlyConfigured('Must pass a problem')
        self.problem = get_object_or_404(Problem, code=kwargs['problem'])
        self.problem_name = self.problem.translated_name(self.request.LANGUAGE_CODE)
        if self.in_contest:
            try:
                cp = ContestProblem.objects.select_related('problem').get(
                    contest=self.contest, problem=self.problem,
                )
                self.problem_name = contest_problem_display_name(cp, self.request.LANGUAGE_CODE)
            except ContestProblem.DoesNotExist:
                pass
        return super(ProblemSubmissionsBase, self).get(request, *args, **kwargs)

    def get_all_submissions_page(self):
        return reverse('chronological_submissions', kwargs={'problem': self.problem.code})

    def get_context_data(self, **kwargs):
        context = super(ProblemSubmissionsBase, self).get_context_data(**kwargs)
        if self.dynamic_update:
            context['dynamic_update'] = context['page_obj'].number == 1
            context['dynamic_problem_id'] = self.problem.id
            context['last_msg'] = event.last()
        context['best_submissions_link'] = reverse('ranked_submissions', kwargs={'problem': self.problem.code})
        return context


class ProblemSubmissions(ProblemSubmissionsBase):
    def get_my_submissions_page(self):
        if self.request.user.is_authenticated:
            return reverse('user_submissions', kwargs={'problem': self.problem.code,
                                                       'user': self.request.user.username})


class UserProblemSubmissions(ConditionalUserTabMixin, UserMixin, ProblemSubmissions):
    check_contest_in_access_check = False

    def access_check(self, request):
        super(UserProblemSubmissions, self).access_check(request)

        if not self.is_own:
            self.access_check_contest(request)

    def get_queryset(self):
        return super(UserProblemSubmissions, self).get_queryset().filter(user_id=self.profile.id)

    def get_title(self):
        if self.is_own:
            return _('My submissions for %(problem)s') % {'problem': self.problem_name}
        return _("%(user)s's submissions for %(problem)s") % {
            'user': self.profile.display_name, 'problem': self.problem_name,
        }

    def get_content_title(self):
        if self.request.user.is_authenticated and self.request.profile == self.profile:
            return mark_safe(escape(_('My submissions for %(problem)s')) % {
                'problem': format_html('<a href="{1}">{0}</a>', self.problem_name,
                                       reverse('problem_detail', args=[self.problem.code])),
            })
        return mark_safe(escape(_("%(user)s's submissions for %(problem)s")) % {
            'user': format_html('<a href="{1}">{0}</a>', self.profile.display_name,
                                reverse('user_page', args=[self.username])),
            'problem': format_html('<a href="{1}">{0}</a>', self.problem_name,
                                   reverse('problem_detail', args=[self.problem.code])),
        })

    def get_context_data(self, **kwargs):
        context = super(UserProblemSubmissions, self).get_context_data(**kwargs)
        context['dynamic_user_id'] = self.profile.id
        return context


def single_submission(request):
    request.no_profile_update = True
    if 'id' not in request.GET or not request.GET['id'].isdigit():
        return HttpResponseBadRequest()
    try:
        show_problem = int(request.GET.get('show_problem', '1'))
    except ValueError:
        return HttpResponseBadRequest()

    authenticated = request.user.is_authenticated
    submission = get_object_or_404(submission_related(Submission.objects.all()), id=int(request.GET['id']))
    if not submission.problem.is_accessible_by(request.user):
        raise Http404()

    attach_student_display_score(submission)
    return render(request, 'submission/row.html', {
        'submission': submission,
        'completed_problem_ids': user_completed_ids(request.profile) if authenticated else [],
        'editable_problem_ids': user_editable_ids(request.profile) if authenticated else [],
        'tester_problem_ids': user_tester_ids(request.profile) if authenticated else [],
        'show_problem': show_problem,
        'problem_name': show_problem and _submission_contest_problem_name(
            submission, request.LANGUAGE_CODE,
        ),
        'profile_id': request.profile.id if authenticated else 0,
    })


class AllSubmissions(InfinitePaginationMixin, SubmissionsListBase):
    stats_update_interval = 3600

    @property
    def use_infinite_pagination(self):
        return not self.in_contest

    def get_my_submissions_page(self):
        if self.request.user.is_authenticated:
            return reverse('all_user_submissions', kwargs={'user': self.request.user.username})

    def get_context_data(self, **kwargs):
        context = super(AllSubmissions, self).get_context_data(**kwargs)
        context['dynamic_update'] = context['page_obj'].number == 1
        context['last_msg'] = event.last()
        context['stats_update_interval'] = self.stats_update_interval
        return context

    def _get_result_data(self, queryset=None):
        if (queryset is not None or self.in_contest or self.selected_languages or self.selected_statuses or
                self.filter_problems or self.filter_users or self.filter_contests):
            return super(AllSubmissions, self)._get_result_data(queryset)

        key = 'global_submission_result_data'
        result = cache.get(key)
        if result:
            return result
        # Cached for every viewer: leave out every currently hidden verdict.
        result = get_result_data(Submission.objects.exclude(results_hidden_q()).order_by())
        cache.set(key, result, self.stats_update_interval)
        return result


class ForceContestMixin(object):
    @property
    def in_contest(self):
        return True

    @property
    def contest(self):
        return self._contest

    def access_check(self, request):
        super(ForceContestMixin, self).access_check(request)

        # The assignment's own editors may look before it starts (and at drafts), as on the
        # assignment page itself; the problem table links them here.
        if not request.user.has_perm('judge.see_private_contest') and \
                not self.contest.is_editable_by(request.user):
            if not self.contest.is_visible:
                raise Http404()
            if self.contest.start_time is not None and self.contest.start_time > timezone.now():
                raise Http404()
        # A private or course-only assignment's lists (and its name in the title) are only for
        # those who may open the assignment itself.
        if not self.contest.is_accessible_by(request.user):
            raise Http404()

    def _site_lists_closed(self, problem):
        """Would this problem's site-wide submission lists be a 404 for the viewer? They are while
        the viewer sits an assignment without this problem, or cannot open the problem at all."""
        user = self.request.user
        if not problem.is_accessible_by(user):
            return True
        current = self.request.profile.current_contest if user.is_authenticated else None
        return current is not None and current.contest_id != self.contest.id and \
            not current.contest.contest_problems.filter(problem_id=problem.id).exists()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        problem = getattr(self, 'problem', None)
        if problem is not None and self._site_lists_closed(problem):
            # Keep the tabs inside this assignment instead of linking to lists that 404.
            context['all_submissions_link'] = None
            context['best_submissions_link'] = reverse('contest_ranked_submissions',
                                                       args=[self.contest.key, problem.code])
            user = self.request.user
            context['my_submissions_link'] = reverse(
                'contest_user_submissions', args=[self.contest.key, user.username, problem.code],
            ) if user.is_authenticated and self.contest.users.filter(user_id=self.request.profile.id).exists() \
                else None
        return context

    def get_problem_number(self, problem):
        return self.contest.contest_problems.select_related('problem').get(problem=problem).order

    def get(self, request, *args, **kwargs):
        if 'contest' not in kwargs:
            raise ImproperlyConfigured('Must pass a contest')
        self._contest = get_object_or_404(Contest, key=kwargs['contest'])
        return super(ForceContestMixin, self).get(request, *args, **kwargs)


class UserAllContestSubmissions(ForceContestMixin, AllUserSubmissions):
    def get_title(self):
        if self.is_own:
            return _('My submissions in %(contest)s') % {'contest': self.contest.name}
        return _("%(user)s's submissions in %(contest)s") % {
            'user': self.profile.display_name,
            'contest': self.contest.name,
        }

    def access_check(self, request):
        super().access_check(request)
        if not self.contest.users.filter(user_id=self.profile.id).exists():
            raise Http404()
        if not self.is_own and not self.contest.can_see_full_scoreboard(self.request.user):
            raise Http404()

    def get_content_title(self):
        if self.is_own:
            return mark_safe(escape(_('My submissions in %(contest)s')) % {
                'contest': format_html('<a href="{1}">{0}</a>', self.contest.name,
                                       reverse('contest_view', args=[self.contest.key])),
            })
        return mark_safe(escape(_("%(user)s's submissions in %(contest)s")) % {
            'user': format_html('<a href="{1}">{0}</a>', self.profile.display_name,
                                reverse('user_page', args=[self.username])),
            'contest': format_html('<a href="{1}">{0}</a>', self.contest.name,
                                   reverse('contest_view', args=[self.contest.key])),
        })

    def get_queryset(self):
        queryset = super().get_queryset()
        # FIXME: fix this line of code when #1509 is implemented
        if not self.request.user.is_authenticated or self.request.profile.id not in self.contest.editor_ids:
            filter_submissions_by_visible_problems(queryset, self.request.user)
        return queryset


class UserContestSubmissions(ForceContestMixin, UserProblemSubmissions):
    def get_title(self):
        if self.problem.is_accessible_by(self.request.user):
            return _("{user}'s submissions for {problem} in {contest}").format(
                user=self.profile.display_name,
                problem=self.problem_name,
                contest=self.contest.name,
            )
        return _("{user}'s submissions for problem {number} in {contest}").format(
            user=self.profile.display_name,
            number=self.get_problem_number(self.problem),
            contest=self.contest.name,
        )

    def access_check(self, request):
        super(UserContestSubmissions, self).access_check(request)
        if not self.contest.users.filter(user_id=self.profile.id).exists():
            raise Http404()

    def get_content_title(self):
        if self.problem.is_accessible_by(self.request.user):
            return mark_safe(escape(_("{user}'s submissions for {problem} in {contest}")).format(
                user=format_html('<a href="{1}">{0}</a>', self.profile.display_name,
                                 reverse('user_page', args=[self.username])),
                problem=format_html('<a href="{1}">{0}</a>', self.problem_name,
                                    reverse('problem_detail', args=[self.problem.code])),
                contest=format_html('<a href="{1}">{0}</a>', self.contest.name,
                                    reverse('contest_view', args=[self.contest.key])),
            ))
        return mark_safe(escape(_("{user}'s submissions for problem {number} in {contest}")).format(
            user=format_html('<a href="{1}">{0}</a>', self.profile.display_name,
                             reverse('user_page', args=[self.username])),
            number=self.get_problem_number(self.problem),
            contest=format_html('<a href="{1}">{0}</a>', self.contest.name,
                                reverse('contest_view', args=[self.contest.key])),
        ))
