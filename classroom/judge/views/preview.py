import json

from django.http import HttpResponseBadRequest, JsonResponse
from django.views.generic.base import ContextMixin, TemplateResponseMixin, View

from judge.utils.markdown_preview import render_preview_html


class MarkdownPreviewView(TemplateResponseMixin, ContextMixin, View):
    def post(self, request, *args, **kwargs):
        try:
            self.preview_data = data = request.POST['content']
        except KeyError:
            return HttpResponseBadRequest('No preview data specified.')

        return self.render_to_response(self.get_context_data(
            preview_data=data,
        ))


class ProblemMarkdownPreviewView(MarkdownPreviewView):
    template_name = 'problem/preview.html'


class BlogMarkdownPreviewView(MarkdownPreviewView):
    template_name = 'blog/preview.html'


class ContestMarkdownPreviewView(MarkdownPreviewView):
    template_name = 'contest/preview.html'


class CommentMarkdownPreviewView(MarkdownPreviewView):
    template_name = 'comments/preview.html'


class FlatPageMarkdownPreviewView(MarkdownPreviewView):
    template_name = 'flatpage-preview.html'


class ProfileMarkdownPreviewView(MarkdownPreviewView):
    template_name = 'user/preview.html'


class OrganizationMarkdownPreviewView(MarkdownPreviewView):
    template_name = 'organization/preview.html'


class SolutionMarkdownPreviewView(MarkdownPreviewView):
    template_name = 'solution-preview.html'


class LicenseMarkdownPreviewView(MarkdownPreviewView):
    template_name = 'license-preview.html'


class TicketMarkdownPreviewView(MarkdownPreviewView):
    template_name = 'ticket/preview.html'


class DefaultMarkdownPreviewView(MarkdownPreviewView):
    template_name = 'default-preview.html'


class MarkdownBatchPreviewView(View):
    """JSON batch preview for multi-fragment editors (create_v2 RichEditor).

    POST body::
        {"items": [{"key": "stem", "content": "..."}, ...]}

    Response::
        {"items": [{"key": "stem", "html": "<p>...</p>"}, ...]}

    Each fragment is rendered with the same ``markdown(style)|reference``
    pipeline as the corresponding single preview / problem page.
    """

    style = 'default'
    max_items = 40
    max_content_len = 100000

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        except (UnicodeDecodeError, json.JSONDecodeError):
            return HttpResponseBadRequest('Invalid JSON.')

        items = payload.get('items')
        if not isinstance(items, list):
            return HttpResponseBadRequest('items must be a list.')
        if len(items) > self.max_items:
            return HttpResponseBadRequest('too many items.')

        out = []
        for item in items:
            if not isinstance(item, dict):
                return HttpResponseBadRequest('invalid item.')
            key = item.get('key', '')
            content = item.get('content', '')
            if content is None:
                content = ''
            content = str(content)
            if len(content) > self.max_content_len:
                return HttpResponseBadRequest('content too large.')
            out.append({
                'key': str(key),
                'html': render_preview_html(content, self.style),
            })
        return JsonResponse({'items': out})


class ProblemMarkdownBatchPreviewView(MarkdownBatchPreviewView):
    style = 'problem'
