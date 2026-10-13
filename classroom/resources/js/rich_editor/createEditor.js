import { Editor } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import { Markdown } from '@tiptap/markdown';
import HardBreak from '@tiptap/extension-hard-break';
import { Table, TableRow, TableCell, TableHeader } from '@tiptap/extension-table';
import Link from '@tiptap/extension-link';
import Placeholder from '@tiptap/extension-placeholder';
import TextAlign from '@tiptap/extension-text-align';
import Underline from '@tiptap/extension-underline';
import katex from 'katex';
import { SiteInlineMath, SiteBlockMath } from './extensions/math.js';
import { SiteCodeBlock } from './extensions/codeBlock.js';
import { Blank, renumberBlankChips } from './extensions/blank.js';
import { Mention } from './extensions/mention.js';
import { SiteHeading, SiteParagraph, SiteImage, SiteHighlight } from './extensions/site.js';

/** Strip the empty-paragraph marker TipTap used to emit into Markdown. */
export function sanitizeEditorMarkdown(md) {
  return String(md || '')
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
    .replace(/^[ \t]*(?:&nbsp;|\u00a0)+[ \t]*$/gm, '')
    .replace(/&nbsp;/g, ' ')
    .replace(/\u00a0/g, ' ')
    // Accidental extra cell paragraphs serialize as a trailing <br> before `|`.
    .replace(/<br>(?=\s*\|)/g, '');
}

/** CommonMark collapses a single newline to a space. Teachers type Enter in
 * Markdown mode and expect the preview / rich view to keep that line break
 * (GFM "breaks"). Code fences, inline code, and math are left alone. */
export function markdownWithBreaks(md) {
  const src = sanitizeEditorMarkdown(md);
  const held = [];
  const parked = src.replace(
    /```[\s\S]*?```|`[^`\n]+`|\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|~[^~\n]+~|^(?:\|[^\n]*\|[ \t]*(?:\n|$))+/gm,
    (m) => {
      held.push(m);
      return `\0C${held.length - 1}\0`;
    },
  );
  const out = parked.replace(/([^\n])\n(?!\n)/g, '$1  \n');
  return out.replace(/\0C(\d+)\0/g, (_, i) => held[+i]);
}

const SiteHardBreak = HardBreak.extend({
  markdownTokenName: 'br',
  parseMarkdown: () => ({ type: 'hardBreak' }),
  renderMarkdown: () => '\n',
});

export function editorExtensions(opts = {}) {
  return [
    Markdown.configure({
      markedOptions: { gfm: true, breaks: true },
    }),
    StarterKit.configure({
      strike: false,
      heading: false,
      paragraph: false,
      link: false,
      underline: false,
      codeBlock: false,
      hardBreak: false,
    }),
    SiteHardBreak,
    SiteCodeBlock,
    SiteParagraph,
    SiteHeading,
    SiteHighlight.configure({ multicolor: false }),
    Underline,
    Link.configure({
      openOnClick: false,
      HTMLAttributes: { rel: 'noopener' },
    }),
    SiteImage.configure({ inline: true, allowBase64: false }),
    Table.configure({ resizable: false, HTMLAttributes: { class: 'table' } }),
    TableRow,
    TableHeader,
    TableCell,
    TextAlign.configure({ types: ['heading', 'paragraph'] }),
    Placeholder.configure({ placeholder: opts.placeholder || '' }),
    SiteInlineMath.configure({
      katexOptions: { throwOnError: false, displayMode: false },
      onClick: opts.onMathClick,
    }),
    SiteBlockMath.configure({
      katexOptions: { throwOnError: false, displayMode: true },
      onClick: opts.onMathClick,
    }),
    Blank,
    Mention,
  ];
}

export function createTiptapEditor({
  element, placeholder, editable = true, onUpdate, onSelectionUpdate, onMathClick, onMentionTrigger, onPaste,
}) {
  return new Editor({
    element,
    editable,
    content: '',
    contentType: 'markdown',
    extensions: editorExtensions({ placeholder, onMathClick }),
    editorProps: {
      attributes: {
        class: 'rich-editor-body',
        'data-placeholder': placeholder || '',
      },
      handleTextInput(view, from, to, text) {
        if (text === '@' && typeof onMentionTrigger === 'function') {
          const type = view.state.selection.$from.parent.type.name;
          if (type !== 'codeBlock' && type !== 'inlineMath' && type !== 'blockMath') {
            onMentionTrigger();
            return true;
          }
        }
        return false;
      },
      handlePaste(view, event) {
        return typeof onPaste === 'function' ? !!onPaste(event) : false;
      },
    },
    onUpdate,
    onSelectionUpdate,
  });
}

export function createHeadlessEditor() {
  return new Editor({
    editable: false,
    content: '',
    contentType: 'markdown',
    extensions: editorExtensions(),
  });
}

export function parseMarkdownToHtml(md) {
  const ed = createHeadlessEditor();
  ed.commands.setContent(markdownWithBreaks(md || ''), { contentType: 'markdown' });
  const html = fillKatex(ed.getHTML());
  ed.destroy();
  return html;
}

function fillKatex(html) {
  if (!html || typeof document === 'undefined') return html || '';
  const wrap = document.createElement('div');
  wrap.innerHTML = html;
  wrap.querySelectorAll('[data-latex]').forEach((el) => {
    const latex = el.getAttribute('data-latex') || '';
    const display = el.getAttribute('data-type') === 'block-math';
    try {
      el.innerHTML = katex.renderToString(latex, { throwOnError: false, displayMode: display });
    } catch (err) {
      el.textContent = latex;
    }
  });
  renumberBlankChips(wrap);
  return wrap.innerHTML;
}

export function serializeHtmlToMarkdown(html) {
  const ed = createHeadlessEditor();
  ed.commands.setContent(html || '');
  const md = sanitizeEditorMarkdown(ed.getMarkdown() || '');
  ed.destroy();
  return md;
}
