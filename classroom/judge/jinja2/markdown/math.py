import re

import mistune

from judge.utils.mathoid import MathoidMathParser

mistune._pre_tags.append('latex')

# \[...\] may span blank lines; mistune splits those into paragraphs first, so
# escape turns \[ / \] into [ / ]. Rewrite to $$...$$ (and collapse blank lines
# inside) before block parsing. Skip fenced code so examples stay literal.
_FENCED_CODE = re.compile(r'```[\s\S]*?```')
_BRACKET_DISPLAY_MATH = re.compile(r'\\\[(.+?)\\\]', re.DOTALL)


def normalize_latex_display_math(text):
    if not text or '\\[' not in text:
        return text

    fences = []

    def stash_fence(m):
        fences.append(m.group(0))
        return '\x00F%d\x00' % (len(fences) - 1)

    def restore_fences(s):
        for i, block in enumerate(fences):
            s = s.replace('\x00F%d\x00' % i, block)
        return s

    def to_dollars(m):
        inner = m.group(1)
        # Single-line \[x\] is fine as-is. Multiline (esp. with blank lines) can be
        # split into paragraphs by mistune before math rules run — rewrite to $$.
        if '\n' not in inner and '\r' not in inner:
            return m.group(0)
        inner = re.sub(r'\n{2,}', '\n', inner.strip())
        return '$$\n%s\n$$' % inner

    stashed = _FENCED_CODE.sub(stash_fence, text)
    return restore_fences(_BRACKET_DISPLAY_MATH.sub(to_dollars, stashed))


class MathInlineGrammar(mistune.InlineGrammar):
    block_math = re.compile(r'^\$\$(.*?)\$\$|^\\\[(.*?)\\\]', re.DOTALL)
    math = re.compile(r'^~(.*?)~|^\\\((.*?)\\\)', re.DOTALL)
    text = re.compile(r'^[\s\S]+?(?=[\\<!\[_*`~$]|\\[\[(]|https?://| {2,}\n|$)')


class MathInlineLexer(mistune.InlineLexer):
    grammar_class = MathInlineGrammar

    def __init__(self, *args, **kwargs):
        self.default_rules = self.default_rules[:]
        self.inline_html_rules = self.default_rules
        # Before mistune's ``escape`` rule: otherwise ``\(`` / ``\[`` become ``(`` / ``[``
        # and MathJax (no-Mathoid path) never sees delimiters. ``~`` / ``$$`` were fine.
        escape_idx = self.default_rules.index('escape')
        self.default_rules.insert(escape_idx, 'math')
        self.default_rules.insert(escape_idx, 'block_math')
        super(MathInlineLexer, self).__init__(*args, **kwargs)

    def output_block_math(self, m):
        return self.renderer.block_math(m.group(1) or m.group(2))

    def output_math(self, m):
        return self.renderer.math(m.group(1) or m.group(2))

    def output_inline_html(self, m):
        tag = m.group(1)
        text = m.group(3)
        if self._parse_inline_html and text:
            if tag == 'a':
                self._in_link = True
                text = self.output(text)
                self._in_link = False
            else:
                text = self.output(text)
            extra = m.group(2) or ''
            html = '<%s%s>%s</%s>' % (tag, extra, text, tag)
        else:
            html = m.group(0)
        return self.renderer.inline_html(html)


class MathRenderer(mistune.Renderer):
    def __init__(self, *args, **kwargs):
        if kwargs.pop('math', False):
            self.mathoid = MathoidMathParser(kwargs.pop('math_engine', None) or 'svg')
        else:
            self.mathoid = None
        super(MathRenderer, self).__init__(*args, **kwargs)

    def block_math(self, math):
        if self.mathoid is None or not math:
            return r'\[%s\]' % mistune.escape(str(math))
        return self.mathoid.display_math(math)

    def math(self, math):
        if self.mathoid is None or not math:
            return r'\(%s\)' % mistune.escape(str(math))
        return self.mathoid.inline_math(math)
