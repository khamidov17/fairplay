"""Choice widgets that render option labels through the site Markdown pipeline.

Student MCQ options are authored as Markdown (including fenced code), but Django's
``RadioSelect`` / ``CheckboxSelectMultiple`` treat labels as plain text. These
widgets convert each label with the same renderer as the problem statement.
"""
from django import forms
from django.conf import settings
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from judge.jinja2.markdown import markdown as render_markdown


def math_engine_for_profile(profile):
    engine = getattr(profile, 'math_engine', None) if profile is not None else None
    if not engine:
        engine = getattr(settings, 'MATHOID_DEFAULT_TYPE', 'jax')
    if engine == 'auto':
        engine = getattr(settings, 'MATHOID_DEFAULT_TYPE', 'jax')
    if engine == 'auto':
        engine = 'jax'
    return engine


class MarkdownChoiceMixin:
    """Render each choice label as sanitized problem Markdown."""

    option_inherits_attrs = False

    def __init__(self, *args, markdown_style='problem', math_engine=None, **kwargs):
        self.markdown_style = markdown_style
        self.math_engine = math_engine
        super().__init__(*args, **kwargs)

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        html = str(render_markdown(label or '', self.markdown_style, self.math_engine))
        option['label'] = format_html(
            '<span class="mcq-option-body content-description">{}</span>',
            mark_safe(html),
        )
        return option


class MarkdownRadioSelect(MarkdownChoiceMixin, forms.RadioSelect):
    pass


class MarkdownCheckboxSelectMultiple(MarkdownChoiceMixin, forms.CheckboxSelectMultiple):
    pass
