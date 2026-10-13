"""Server-authoritative markdown fixtures + batch preview contract (RTE P2/P3)."""
import json
import os

from django.test import RequestFactory, SimpleTestCase, TestCase

from judge.jinja2.markdown import markdown
from judge.jinja2.reference import reference
from judge.utils.markdown_preview import render_preview_html
from judge.views.preview import ProblemMarkdownBatchPreviewView


FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__),
    '..', '..', '..', 'resources', 'tests', 'fixtures', 'rte_markdown.json',
)
PREVIEW_TEMPLATE = os.path.join(
    os.path.dirname(__file__),
    '..', '..', '..', 'templates', 'problem', 'preview.html',
)


def _load_cases():
    with open(os.path.abspath(FIXTURE_PATH), encoding='utf-8') as f:
        return json.load(f)['cases']


class RteMarkdownFixtureTest(SimpleTestCase):
    """Same Markdown fixtures as JS tests, rendered via mistune+bleach (problem style)."""

    def test_fixtures_via_markdown(self):
        for case in _load_cases():
            with self.subTest(case=case['id']):
                html = str(markdown(case['markdown'], 'problem'))
                for needle in case.get('expect_substrings') or []:
                    self.assertIn(needle, html, msg=f'{case["id"]}: missing {needle!r} in {html!r}')
                for needle in case.get('forbid_substrings') or []:
                    self.assertNotIn(needle, html, msg=f'{case["id"]}: forbidden {needle!r} in {html!r}')


class ProblemPreviewTemplateContractTest(SimpleTestCase):
    """create_v2 single preview posts to problem_preview → this template → markdown('problem')."""

    def test_preview_template_uses_problem_markdown_style(self):
        with open(os.path.abspath(PREVIEW_TEMPLATE), encoding='utf-8') as f:
            source = f.read()
        self.assertIn("markdown('problem'", source)
        self.assertIn('|reference', source)


class SharedPreviewHelperTest(SimpleTestCase):
    def test_helper_matches_problem_page_pipeline(self):
        for case in _load_cases():
            with self.subTest(case=case['id']):
                self.assertEqual(
                    render_preview_html(case['markdown'], 'problem'),
                    str(reference(markdown(case['markdown'], 'problem'))),
                )


class PreviewUserReferenceTest(TestCase):
    def test_user_token_becomes_profile_link(self):
        html = render_preview_html('See [user:nobodyhere]', 'problem')
        self.assertNotIn('[user:nobodyhere]', html)
        self.assertIn('nobodyhere', html)


class ProblemPreviewBatchContractTest(SimpleTestCase):
    """P3: one JSON POST renders many fragments with the same problem pipeline."""

    def test_batch_preview_returns_keyed_html(self):
        factory = RequestFactory()
        view = ProblemMarkdownBatchPreviewView.as_view()
        items = [
            {'key': 'stem', 'content': 'Hello **bold** and *italic*.'},
            {'key': 'opt-0', 'content': '> quoted option'},
            {'key': 'opt-1', 'content': '```\nint x = 1;\n```'},
        ]
        request = factory.post(
            '/widgets/preview/problem/batch',
            data=json.dumps({'items': items}),
            content_type='application/json',
        )
        response = view(request)
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content.decode('utf-8'))
        self.assertIn('items', body)
        self.assertEqual(len(body['items']), 3)

        by_key = {row['key']: row['html'] for row in body['items']}
        self.assertIn('<strong>bold</strong>', by_key['stem'])
        self.assertIn('<em>italic</em>', by_key['stem'])
        self.assertIn('quoted option', by_key['opt-0'])
        self.assertIn('blockquote', by_key['opt-0'].lower())
        self.assertIn('int x = 1', by_key['opt-1'])

        # Same HTML as the shared helper / single-fragment pipeline.
        for item in items:
            self.assertEqual(by_key[item['key']], render_preview_html(item['content'], 'problem'))

    def test_batch_rejects_non_list(self):
        factory = RequestFactory()
        view = ProblemMarkdownBatchPreviewView.as_view()
        request = factory.post(
            '/widgets/preview/problem/batch',
            data=json.dumps({'items': 'nope'}),
            content_type='application/json',
        )
        response = view(request)
        self.assertEqual(response.status_code, 400)

    def test_batch_fixtures_roundtrip(self):
        factory = RequestFactory()
        view = ProblemMarkdownBatchPreviewView.as_view()
        cases = _load_cases()
        payload = {
            'items': [{'key': c['id'], 'content': c['markdown']} for c in cases],
        }
        request = factory.post(
            '/widgets/preview/problem/batch',
            data=json.dumps(payload),
            content_type='application/json',
        )
        response = view(request)
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content.decode('utf-8'))
        by_key = {row['key']: row['html'] for row in body['items']}
        for case in cases:
            with self.subTest(case=case['id']):
                html = by_key[case['id']]
                for needle in case.get('expect_substrings') or []:
                    self.assertIn(needle, html)
                for needle in case.get('forbid_substrings') or []:
                    self.assertNotIn(needle, html)
