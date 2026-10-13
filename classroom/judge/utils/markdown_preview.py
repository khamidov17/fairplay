"""Shared server-side Markdown preview helpers.

Used by the single-fragment preview templates (Martor / classic forms) and by the
batch JSON endpoint consumed by create_v2 RichEditor. Keeping one entrypoint
avoids drift between “preview pane” and “problem page” rendering.
"""
from judge.jinja2.markdown import markdown
from judge.jinja2.reference import reference


def render_preview_html(content, style='problem'):
    """Return the same HTML the problem page would show: markdown then reference."""
    return str(reference(markdown(content or '', style)))
