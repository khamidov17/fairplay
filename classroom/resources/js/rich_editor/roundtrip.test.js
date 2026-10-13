import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import RichEditor from './index.js';

function mountTwo(doc) {
  doc.body.innerHTML = `
    <div data-rich-mount="stem"></div>
    <div data-rich-mount="opt-a" data-compact></div>
  `;
  RichEditor.mount(doc);
}

function mdHas(out, ...needles) {
  needles.forEach((n) => expect(out).toContain(n));
}

function pipeLine(md) {
  return String(md).split('\n').find((l) => l.includes('|')) || '';
}

function pipeCols(md) {
  const line = pipeLine(md);
  return line.split('|').length - 2;
}

function pipeRows(md) {
  return (String(md).match(/^\|/gm) || []).length;
}

describe('RichEditor facade', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    RichEditor._instances.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    RichEditor.configure({ fileUploadUrl: '', searchUsersUrl: '', fileAccept: '' });
    RichEditor._instances.forEach((ed) => ed.destroy && ed.destroy());
    RichEditor._instances.clear();
    document.body.innerHTML = '';
  });

  it('mounts and exposes getMarkdown / setMarkdown', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    RichEditor.setMarkdown('stem', '**hello**');
    expect(RichEditor.getMarkdown('stem')).toContain('hello');
    expect(RichEditor.getMarkdown('stem')).toMatch(/\*\*hello\*\*|__hello__/);
  });

  it('keeps two instances independent', () => {
    mountTwo(document);
    RichEditor.setMarkdown('stem', 'stem text');
    RichEditor.setMarkdown('opt-a', 'option text');
    expect(RichEditor.getMarkdown('stem')).toContain('stem text');
    expect(RichEditor.getMarkdown('opt-a')).toContain('option text');
    expect(RichEditor.getMarkdown('stem')).not.toContain('option text');
  });

  it('round-trips bold, fence, table, and image url', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const src = [
      'A **bold** word',
      '',
      '```python',
      'print(1)',
      '```',
      '',
      '| A | B |',
      '| --- | --- |',
      '| 1 | 2 |',
      '',
      '![diagram](https://example.com/x.png)',
    ].join('\n');
    RichEditor.setMarkdown('stem', src);
    const out = RichEditor.getMarkdown('stem');
    mdHas(out, 'bold', 'print(1)', 'https://example.com/x.png');
    expect(out).toMatch(/```/);
    expect(out).toMatch(/\|/);
  });

  it('mdToHtml numbers blank chips like the editor', () => {
    const html = RichEditor.mdToHtml('A {{blank}} and {{blank}} end');
    expect(html).toMatch(/rte-blank/);
    expect(html).toContain('(1)');
    expect(html).toContain('(2)');
    expect(html).not.toMatch(/pv-blankinput|<input/i);
  });

  it('mdToHtml paints dollar math with KaTeX', () => {
    const html = RichEditor.mdToHtml('Area is $$a^2+b^2$$.');
    expect(html).toMatch(/katex/);
    expect(html).toMatch(/data-latex/);
  });

  it('keeps single newlines as line breaks in preview HTML', () => {
    const html = RichEditor.mdToHtml('请说明进程\nhhhh\ngggg');
    expect(html).toMatch(/进程[\s\S]*<br[\s\S]*hhhh[\s\S]*<br[\s\S]*gggg/i);
  });

  it('keeps single newlines after a blank-line paragraph split', () => {
    const src = [
      '请说明什么是进程及其通常包含的基本资源；进一步解释线程的含义，并比较线程与进程的主要区别',
      '',
      'hhhh',
      'hh',
      'h',
      'hhhh',
      'hhhh',
      'hhhh',
    ].join('\n');
    const html = RichEditor.mdToHtml(src);
    expect(html, html).not.toMatch(/hhhh hh/);
    expect(html, html).toMatch(/hhhh[\s\S]*<br[\s\S]*hh/i);
  });

  it('does not emit or keep &nbsp; for empty paragraphs', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    RichEditor.setMarkdown('stem', 'hello\n\n\n\n\n\nworld');
    const out = RichEditor.getMarkdown('stem');
    expect(out).not.toContain('&nbsp;');
    expect(out).not.toContain('\u00a0');
    expect(RichEditor.mdToHtml('a\n\n&nbsp;\n\nb')).not.toContain('&nbsp;');
  });

  it('escapes quotes in resized image markdown', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const inst = RichEditor._instances.get('stem');
    inst.chain().focus().setImage({
      src: 'https://example.com/x.png',
      alt: 'foo" onerror="alert(1)',
      width: '120px',
    }).run();
    const md = RichEditor.getMarkdown('stem');
    const wrap = document.createElement('div');
    wrap.innerHTML = md;
    const img = wrap.querySelector('img');
    expect(img).toBeTruthy();
    expect(img.getAttribute('onerror')).toBeNull();
    expect(img.getAttribute('alt')).toBe('foo" onerror="alert(1)');
    expect(md).toContain('&quot;');
  });

  it('preserves site math delimiters', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const src = 'Inline ~x^2~ and $$ E=mc^2 $$ and \\(a+b\\) and\n$$\n\\int x\n$$\n';
    RichEditor.setMarkdown('stem', src);
    const out = RichEditor.getMarkdown('stem');
    expect(out).toMatch(/~x\^2~|\$\$ ?x\^2/);
    expect(out).toMatch(/E=mc\^2/);
    expect(out).toMatch(/a\+b|\\\(a\+b\\\)/);
    expect(out).toMatch(/\\int x/);
  });

  it('serializes blanks in document order and after delete', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    RichEditor.setMarkdown('stem', 'A {{blank}} and {{blank}} end');
    let out = RichEditor.getMarkdown('stem');
    const first = out.match(/\{\{\s*blank\s*\}\}/g) || [];
    expect(first.length).toBe(2);

    const inst = RichEditor._instances.get('stem');
    let seen = 0;
    let pos = null;
    inst.state.doc.descendants((node, p) => {
      if (node.type.name === 'blank') {
        seen += 1;
        if (seen === 1) pos = p;
      }
    });
    expect(pos).not.toBeNull();
    inst.chain().focus().deleteRange({ from: pos, to: pos + 1 }).run();
    out = RichEditor.getMarkdown('stem');
    const second = out.match(/\{\{\s*blank\s*\}\}/g) || [];
    expect(second.length).toBe(1);
  });

  it('preserves mention, mark, sized image, and heading marks', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const src = [
      '# Title',
      '',
      'See [user:alice] and <mark>hi</mark>',
      '',
      '<img src="https://example.com/y.png" alt="fig" style="width:120px;">',
    ].join('\n');
    RichEditor.setMarkdown('stem', src);
    const out = RichEditor.getMarkdown('stem');
    expect(out).toMatch(/^#\s+Title/m);
    expect(out).toContain('[user:alice]');
    expect(out).toMatch(/<mark>hi<\/mark>|==hi==/);
    expect(out).toContain('https://example.com/y.png');
  });

  it('serializes paragraph alignment as an HTML wrapper', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const inst = RichEditor._instances.get('stem');
    inst.chain().focus().setContent('centered line', { contentType: 'markdown' }).setTextAlign('center').run();
    expect(RichEditor.getMarkdown('stem')).toContain('text-align:center');
  });

  it('keeps alignment across markdown load and mode switches', async () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const src = '<div style="text-align:center">**keep**</div>';
    RichEditor.setMarkdown('stem', src);
    expect(RichEditor.getMarkdown('stem')).toContain('text-align:center');
    expect(RichEditor.getMarkdown('stem')).toMatch(/keep/);
    const root = document.querySelector('[data-editor="stem"]');
    root.querySelector('[data-mode="markdown"]').click();
    expect(RichEditor.getMarkdown('stem')).toContain('text-align:center');
    root.querySelector('[data-mode="rich"]').click();
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
    expect(RichEditor.getMarkdown('stem')).toContain('text-align:center');
    expect(RichEditor.getMarkdown('stem')).toMatch(/\*\*keep\*\*|keep/);
  });

  it('applies alignment from the toolbar in markdown mode', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const root = document.querySelector('[data-editor="stem"]');
    root.querySelector('[data-mode="markdown"]').click();
    const ta = root.querySelector('.rich-editor-markdown textarea');
    ta.value = 'middle';
    ta.selectionStart = ta.selectionEnd = 0;
    root.querySelector('[data-cmd="alignCenter"]').click();
    expect(ta.value).toContain('text-align:center');
    expect(ta.value).toContain('middle');
    root.querySelector('[data-cmd="alignLeft"]').click();
    expect(ta.value).not.toContain('text-align:');
    expect(ta.value).toContain('middle');
  });

  it('applies headings and list prefixes from the toolbar in markdown mode', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const root = document.querySelector('[data-editor="stem"]');
    root.querySelector('[data-mode="markdown"]').click();
    const ta = root.querySelector('.rich-editor-markdown textarea');
    ta.value = 'Title';
    ta.selectionStart = ta.selectionEnd = 0;
    const head = root.querySelector('.rte-heading');
    head.value = 'h3';
    head.dispatchEvent(new Event('change'));
    expect(ta.value).toMatch(/^# Title/);
    head.value = 'h5';
    head.dispatchEvent(new Event('change'));
    expect(ta.value).toMatch(/^### Title/);
    head.value = 'p';
    head.dispatchEvent(new Event('change'));
    expect(ta.value.trim()).toBe('Title');
    ta.value = 'item';
    ta.selectionStart = ta.selectionEnd = 0;
    root.querySelector('[data-cmd="ul"]').click();
    expect(ta.value).toMatch(/^- item/);
    root.querySelector('[data-cmd="ul"]').click();
    expect(ta.value.trim()).toBe('item');
    root.querySelector('[data-cmd="ol"]').click();
    expect(ta.value).toMatch(/^1\. item/);
    root.querySelector('[data-cmd="quote"]').click();
    expect(ta.value).toMatch(/^> item/);
  });

  it('paints stored markdown with the same mdToHtml path', () => {
    document.body.innerHTML = '<script type="application/json" class="rte-md-source">"**hi** $$a^2$$"</script><div class="paint-target"></div>';
    RichEditor.paintTargets();
    const el = document.querySelector('.paint-target');
    expect(el.classList.contains('rte-display')).toBe(true);
    expect(el.innerHTML).toMatch(/hi/);
    expect(el.innerHTML).toMatch(/katex|data-latex/);
  });

  it('mode switch does not drop markdown', async () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    RichEditor.setMarkdown('stem', '**keep**');
    const root = document.querySelector('[data-editor="stem"]');
    root.querySelector('[data-mode="markdown"]').click();
    expect(root.classList.contains('mode-markdown')).toBe(true);
    expect(RichEditor.getMarkdown('stem')).toMatch(/keep/);
    root.querySelector('[data-mode="rich"]').click();
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
    expect(RichEditor.getMarkdown('stem')).toMatch(/keep/);
  });

  it('survives many rich/markdown toggles without losing list+bold', async () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const src = '1. **dd**\n2. one\n';
    RichEditor.setMarkdown('stem', src);
    const root = document.querySelector('[data-editor="stem"]');
    for (let i = 0; i < 6; i++) {
      root.querySelector('[data-mode="markdown"]').click();
      root.querySelector('textarea').value = src;
      root.querySelector('[data-mode="rich"]').click();
      await Promise.resolve();
      await new Promise((r) => setTimeout(r, 0));
    }
    const out = RichEditor.getMarkdown('stem');
    expect(out).toMatch(/dd/);
    expect(out).toMatch(/one/);
    expect(root.classList.contains('mode-markdown')).toBe(false);
    expect(root.querySelector('.ProseMirror')).toBeTruthy();
  });

  it('inserts latex from the math popover', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    RichEditor.setMarkdown('stem', 'before ');
    const root = document.querySelector('[data-editor="stem"]');
    root.querySelector('[data-cmd="math"]').click();
    const input = document.querySelector('.rte-math-input');
    expect(input).toBeTruthy();
    expect(document.querySelector('#rte-math-popover').hidden).toBe(false);
    expect(document.querySelector('.rte-math-latex-check')).toBeTruthy();
    expect(document.querySelector('.rte-math-tab.is-active')).toBeTruthy();
    expect(document.querySelectorAll('.rte-math-key').length).toBeGreaterThan(8);
    input.value = 'E=mc^2';
    document.querySelector('#rte-math-popover .rte-btn-ok').click();
    const md = RichEditor.getMarkdown('stem');
    expect(md).toMatch(/\$\$E=mc\^2\$\$/);
    expect(md).not.toMatch(/\$\$\s*\n/);
  });

  it('inserts vector and hat as editable MathLive boxes', () => {
    expect(RichEditor._toMathLiveInsert('\\vec{\\placeholder{}}')).toBe('\\overrightarrow{#0}');
    expect(RichEditor._toMathLiveInsert('\\hat{\\placeholder{}}')).toBe('\\overset{\\wedge}{#0}');
    expect(RichEditor._toMathLiveInsert('\\overline{\\placeholder{}}')).toBe('\\overline{#0}');
    expect(RichEditor._toMathLiveInsert('\\frac{\\placeholder{}}{\\placeholder{}}')).toBe('\\frac{#0}{#0}');
  });

  it('inserts a palette symbol into the LaTeX box', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    document.querySelector('[data-cmd="math"]').click();
    const input = document.querySelector('.rte-math-input');
    input.value = '';
    document.querySelector('[data-math-tex="\\\\pi"]').click();
    expect(input.value).toContain('\\pi');
    document.querySelector('#rte-math-popover .rte-btn-ok').click();
    expect(RichEditor.getMarkdown('stem')).toMatch(/\\pi/);
  });

  it('ships the Canvas RCE Basic board', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    document.querySelector('[data-cmd="math"]').click();
    const tex = [...document.querySelectorAll('[data-math-panel="basic"] [data-math-tex]')]
      .map((el) => el.getAttribute('data-math-tex'));
    expect(tex).toContain('\\binom{\\placeholder{}}{\\placeholder{}}');
    expect(tex).toContain('\\therefore');
    expect(tex).toContain('\\mathbb{N}');
    expect(tex).toContain('\\vec{\\placeholder{}}');
    expect(tex.length).toBeGreaterThan(30);
    const face = (title) => document.querySelector(`[data-math-panel="basic"] [title="${title}"]`).innerHTML;
    expect(face('Subscript')).toMatch(/katex/);
    expect(face('Superscript')).toMatch(/katex/);
    expect(face('Binomial Coefficient')).toMatch(/katex/);
    expect(face('Subscript')).not.toBe(face('Superscript'));
    expect(face('Definite Integral')).not.toBe(face('Indefinite Integral'));
    expect(face('Vector (Notation)')).toMatch(/katex/);
    expect(face('Fraction')).toMatch(/katex/);
    expect(face('Bar')).toMatch(/katex/);
    expect(face('Hat')).toMatch(/katex/);
  });

  it('toggles the math dialog to a LaTeX textarea', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    document.querySelector('[data-cmd="math"]').click();
    const check = document.querySelector('.rte-math-latex-check');
    const input = document.querySelector('.rte-math-input');
    check.checked = true;
    check.dispatchEvent(new Event('change', { bubbles: true }));
    expect(input.hidden).toBe(false);
    input.value = 'x^{2}';
    document.querySelector('#rte-math-popover .rte-btn-ok').click();
    expect(RichEditor.getMarkdown('stem')).toMatch(/\$\$x\^\{2\}\$\$/);
  });

  it('keeps one-line math on one line across mode switches', async () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    RichEditor.setMarkdown('stem', '$$a^2 + b^2 = c^2$$');
    const root = document.querySelector('[data-editor="stem"]');
    root.querySelector('[data-mode="markdown"]').click();
    root.querySelector('[data-mode="rich"]').click();
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
    const md = RichEditor.getMarkdown('stem');
    expect(md).toMatch(/\$\$a\^2 \+ b\^2 = c\^2\$\$/);
    expect(md.replace(/\$\$a\^2 \+ b\^2 = c\^2\$\$/g, '')).not.toMatch(/\$\$/);
  });

  it('keeps fenced code language and can change it', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    RichEditor.setMarkdown('stem', '```python\nprint(1)\n```\n');
    expect(RichEditor.getMarkdown('stem')).toMatch(/```python/);
    const inst = RichEditor._instances.get('stem');
    inst.chain().focus().updateAttributes('codeBlock', { language: 'cpp' }).run();
    expect(RichEditor.getMarkdown('stem')).toMatch(/```cpp/);
    expect(document.querySelector('.code-block-lang-input')).toBeTruthy();
  });

  it('shows a file button that covers images and hides the old image button', () => {
    RichEditor.configure({ fileUploadUrl: '/upload-file' });
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const fileBtn = document.querySelector('[data-cmd="file"]');
    expect(fileBtn).toBeTruthy();
    expect(fileBtn.style.display).not.toBe('none');
    expect(fileBtn.getAttribute('title')).toMatch(/image/i);
    expect(document.querySelector('[data-cmd="image"]')).toBeNull();
  });

  it('opens the help dialog in rich text and markdown', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const root = document.querySelector('[data-editor="stem"]');
    root.querySelector('[data-cmd="help"]').click();
    const modal = document.querySelector('#rte-help-modal');
    expect(modal).toBeTruthy();
    expect(modal.hidden).toBe(false);
    expect(modal.querySelector('.rte-help-dialog')).toBeTruthy();
    expect(modal.textContent).toContain('Switch between Rich text and Markdown');
    expect(modal.textContent).toContain('{{blank}}');
    modal.querySelector('.rte-help-close').click();
    expect(modal.hidden).toBe(true);
    root.querySelector('[data-mode="markdown"]').click();
    root.querySelector('[data-cmd="help"]').click();
    expect(document.querySelector('#rte-help-modal').hidden).toBe(false);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(document.querySelector('#rte-help-modal').hidden).toBe(true);
  });

  it('inserts a markdown link from the toolbar in markdown mode', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const root = document.querySelector('[data-editor="stem"]');
    root.querySelector('[data-mode="markdown"]').click();
    root.querySelector('[data-cmd="link"]').click();
    const pop = document.querySelector('#rte-link-popover');
    expect(pop.hidden).toBe(false);
    pop.querySelector('.rte-link-text').value = 'Docs';
    pop.querySelector('.rte-link-url').value = 'https://example.com';
    pop.querySelector('.rte-btn-ok').click();
    expect(root.querySelector('textarea').value).toContain('[Docs](https://example.com)');
  });

  it('restricts the paperclip to images when fileAccept is image/*', () => {
    RichEditor.configure({ fileUploadUrl: '/upload-file', fileAccept: 'image/*' });
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const fileBtn = document.querySelector('[data-cmd="file"]');
    expect(fileBtn.getAttribute('title')).toBe('Upload an image');
    fileBtn.click();
    expect(document.getElementById('rte-file').accept).toBe('image/*');
  });

  it('pastes a clipboard image through the file upload endpoint', async () => {
    RichEditor.configure({ fileUploadUrl: '/upload-file', csrfToken: 'csrf' });
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: 200, link: '/widgets/files/x', is_image: true, name: 'shot.png' }),
    });
    vi.stubGlobal('fetch', fetchMock);
    const file = new File([new Uint8Array([137, 80, 78, 71])], 'shot.png', { type: 'image/png' });
    const mdBtn = document.querySelector('[data-mode="markdown"]');
    mdBtn.click();
    const ta = document.querySelector('textarea');
    const event = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', {
      value: {
        files: [file],
        items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }],
      },
    });
    ta.dispatchEvent(event);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe('/upload-file');
    expect(opts.method).toBe('POST');
    expect(opts.body.get('markdown-file-upload')).toBe(file);
    await vi.waitFor(() => expect(ta.value).toContain('/widgets/files/x'));
    vi.unstubAllGlobals();
  });

  it('renders mentions as profile links and keeps [user:] in markdown', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    RichEditor.setMarkdown('stem', 'See [user:alice]');
    const chip = document.querySelector('.rte-mention');
    expect(chip).toBeTruthy();
    expect(chip.textContent).toBe('alice');
    expect(chip.querySelector('a')?.getAttribute('href')).toBe('/user/alice');
    expect(RichEditor.getMarkdown('stem')).toContain('[user:alice]');
  });

  it('serializes a resized image as an HTML img with width', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const inst = RichEditor._instances.get('stem');
    inst.chain().focus().setImage({ src: 'https://example.com/x.png', alt: 'fig', width: '240px' }).run();
    expect(RichEditor.getMarkdown('stem')).toContain('width:240px');
    const wrap = document.querySelector('.rte-image-wrap');
    expect(wrap).toBeTruthy();
    expect(wrap.querySelector('.img-resize-handle.se')).toBeTruthy();
    expect(wrap.querySelector('.img-resize-handle.sw')).toBeTruthy();
    expect(wrap.querySelector('.img-resize-handle.e')).toBeTruthy();
    expect(wrap.querySelector('.img-resize-handle.w')).toBeTruthy();
  });

  it('searches mentions with the username query param', async () => {
    const urls = [];
    const orig = globalThis.fetch;
    globalThis.fetch = (url) => {
      urls.push(String(url));
      return Promise.resolve({
        json: () => Promise.resolve({ status: 200, data: [{ username: 'alice', text: 'Alice (alice)' }] }),
      });
    };
    RichEditor.configure({ searchUsersUrl: '/martor/search-user/' });
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    document.querySelector('[data-cmd="mention"]').click();
    const input = document.querySelector('.rte-mention-input');
    input.value = 'alice';
    input.dispatchEvent(new Event('input'));
    await new Promise((r) => setTimeout(r, 250));
    expect(urls.some((u) => u.includes('username=alice'))).toBe(true);
    document.querySelector('#rte-mention-popover .rte-mention-results [data-username="alice"]').click();
    expect(RichEditor.getMarkdown('stem')).toContain('[user:alice]');
    globalThis.fetch = orig;
  });

  it('does not accumulate blank lines across rich/markdown switches', async () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const src = 'alpha\n\nbeta\n\n1. one\n2. two\n';
    RichEditor.setMarkdown('stem', src);
    const first = RichEditor.getMarkdown('stem');
    const root = document.querySelector('[data-editor="stem"]');
    for (let i = 0; i < 5; i++) {
      root.querySelector('[data-mode="markdown"]').click();
      root.querySelector('[data-mode="rich"]').click();
      await Promise.resolve();
      await new Promise((r) => setTimeout(r, 0));
    }
    const last = RichEditor.getMarkdown('stem');
    const extraBlanks = (s) => (s.match(/\n{3,}/g) || []).length;
    expect(extraBlanks(last)).toBe(extraBlanks(first));
    expect((last.match(/alpha/g) || []).length).toBe(1);
    expect((last.match(/beta/g) || []).length).toBe(1);
  });

  it('positions popovers in viewport coordinates', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const root = document.querySelector('[data-editor="stem"]');
    const btn = root.querySelector('[data-cmd="link"]');
    btn.getBoundingClientRect = () => ({
      top: 80, right: 200, bottom: 108, left: 160, width: 40, height: 28, x: 160, y: 80,
    });
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 900 });
    Object.defineProperty(window, 'scrollX', { configurable: true, value: 40 });
    btn.click();
    const pop = document.querySelector('#rte-link-popover');
    expect(pop.hidden).toBe(false);
    expect(pop.style.position).toBe('fixed');
    expect(parseFloat(pop.style.top)).toBeCloseTo(114, 0);
    expect(parseFloat(pop.style.left)).toBeCloseTo(160, 0);
    expect(parseFloat(pop.style.top)).toBeLessThan(200);
  });

  it('serializes an ordered list as one list, not split items', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    RichEditor.setMarkdown('stem', 'lead\n\n1. alpha\n2. beta\n3. gamma\n');
    const out = RichEditor.getMarkdown('stem');
    const items = out.match(/^\d+\.\s+\w+/gm) || [];
    expect(items.length).toBeGreaterThanOrEqual(3);
    expect(out).not.toMatch(/alpha\n\n\d+\.\s+beta/);
  });

  it('opens a row×col picker and inserts that table', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    document.querySelector('[data-cmd="table"]').click();
    const picker = document.querySelector('#rte-table-picker');
    expect(picker).toBeTruthy();
    expect(picker.hidden).toBe(false);
    expect(picker.querySelectorAll('.rte-table-picker-cell').length).toBe(100);
    picker.querySelector('[data-rows="2"][data-cols="3"]').click();
    expect(picker.hidden).toBe(true);
    const inst = RichEditor._instances.get('stem');
    inst.state.doc.descendants((node) => {
      if (node.type.name === 'tableHeader') {
        expect(node.childCount).toBe(1);
      }
    });
    const md = RichEditor.getMarkdown('stem');
    expect(pipeCols(md)).toBe(3);
    expect(pipeRows(md)).toBe(3);
    expect(md).toMatch(/Col 1/);
    expect(md).toMatch(/Col 2/);
    expect(md).toMatch(/Col 3/);
    expect(md).not.toMatch(/<br>/);
  });

  it('inserts the same Col N placeholders in markdown mode', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const root = document.querySelector('[data-editor="stem"]');
    root.querySelector('[data-mode="markdown"]').click();
    root.querySelector('[data-cmd="table"]').click();
    document.querySelector('[data-rows="2"][data-cols="3"]').click();
    const md = root.querySelector('textarea').value;
    expect(md).toMatch(/Col 1/);
    expect(md).toMatch(/Col 3/);
    expect(pipeCols(md)).toBe(3);
    expect(md).not.toMatch(/<br>/);
  });

  it('adds and deletes table rows and columns from the cell menu', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    RichEditor.setMarkdown('stem', '| A | B |\n| --- | --- |\n| 1 | 2 |\n');
    const inst = RichEditor._instances.get('stem');
    const focusBodyCell = () => {
      let cellPos = null;
      inst.state.doc.descendants((node, pos) => {
        if (cellPos == null && node.type.name === 'tableCell') {
          cellPos = pos + 1;
          return false;
        }
      });
      inst.chain().focus().setTextSelection(cellPos).run();
    };
    focusBodyCell();
    const chrome = document.querySelector('#rte-table-cell-chrome');
    expect(chrome).toBeTruthy();
    expect(chrome.hidden).toBe(false);
    expect(pipeCols(RichEditor.getMarkdown('stem'))).toBe(2);
    document.querySelector('.rte-table-cell-handle').click();
    document.querySelector('[data-table-act="col-right"]').click();
    expect(pipeCols(RichEditor.getMarkdown('stem'))).toBe(3);
    focusBodyCell();
    document.querySelector('.rte-table-cell-handle').click();
    document.querySelector('[data-table-act="row-below"]').click();
    const bodyRows = pipeRows(RichEditor.getMarkdown('stem'));
    expect(bodyRows).toBeGreaterThanOrEqual(4);
    focusBodyCell();
    document.querySelector('.rte-table-cell-handle').click();
    document.querySelector('[data-table-act="del-col"]').click();
    expect(pipeCols(RichEditor.getMarkdown('stem'))).toBe(2);
    focusBodyCell();
    document.querySelector('.rte-table-cell-handle').click();
    document.querySelector('[data-table-act="del-row"]').click();
    expect(pipeRows(RichEditor.getMarkdown('stem'))).toBeLessThan(bodyRows);
  });

  it('blank-insert button stays hidden until shown by the page', () => {
    document.body.innerHTML = '<div data-rich-mount="stem"></div>';
    RichEditor.mount(document);
    const btn = document.querySelector('.blank-insert');
    expect(btn).toBeTruthy();
    expect(btn.style.display).toBe('none');
  });
});
