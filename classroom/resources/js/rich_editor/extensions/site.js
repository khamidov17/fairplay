import { mergeAttributes } from '@tiptap/core';
import Heading from '@tiptap/extension-heading';
import Image from '@tiptap/extension-image';
import Highlight from '@tiptap/extension-highlight';
import Paragraph from '@tiptap/extension-paragraph';

const LEVEL_TO_TAG = { 1: 'h3', 2: 'h4', 3: 'h5' };
const TAG_TO_LEVEL = { H1: 1, H2: 1, H3: 1, H4: 2, H5: 3, H6: 3 };

const ALIGN_WRAP_RE = /^<(div|p)\s+[^>]*style="[^"]*text-align:\s*(center|right|left|justify)[^"]*"[^>]*>([\s\S]*?)<\/\1>/i;

function wrapAlignedMarkdown(inner, align) {
  if (!align || align === 'left' || align === 'start') return inner;
  return `<div style="text-align:${align}">${inner}</div>`;
}

export const SiteParagraph = Paragraph.extend({
  parseHTML() {
    return [
      { tag: 'p' },
      {
        tag: 'div',
        getAttrs: (el) => {
          if (typeof el === 'string' || !el.style) return false;
          const align = el.style.textAlign;
          if (!align || align === 'left' || align === 'start') return false;
          return { textAlign: align };
        },
      },
    ];
  },
  markdownTokenName: 'alignedBlock',
  markdownTokenizer: {
    name: 'alignedBlock',
    level: 'block',
    start: (src) => src.search(/<(?:div|p)\s+[^>]*text-align\s*:/i),
    tokenize: (src, _tokens, helpers) => {
      const m = src.match(ALIGN_WRAP_RE);
      if (!m) return undefined;
      const inner = m[3].replace(/^\n+|\n+$/g, '');
      const blockInner = /\n/.test(inner) || /^#{1,6}\s/.test(inner);
      return {
        type: 'alignedBlock',
        raw: m[0],
        align: m[2].toLowerCase(),
        blockInner,
        tokens: blockInner
          ? (helpers.blockTokens ? helpers.blockTokens(inner) : [])
          : (helpers.inlineTokens ? helpers.inlineTokens(inner) : []),
      };
    },
  },
  parseMarkdown: (token, helpers) => {
    const align = token.align || 'left';
    if (token.blockInner) {
      const kids = helpers.parseChildren(token.tokens || []) || [];
      return kids.map((node) => {
        if (node.type === 'paragraph' || node.type === 'heading') {
          return { ...node, attrs: { ...(node.attrs || {}), textAlign: align } };
        }
        return node;
      });
    }
    return helpers.createNode(
      'paragraph',
      { textAlign: align },
      helpers.parseInline(token.tokens || []),
    );
  },
  renderMarkdown: (node, helpers) => {
    // Do not append \n\n here. The document renderer already joins blocks
    // with \n\n; a second suffix doubles blank lines on every round-trip.
    const content = Array.isArray(node.content) ? node.content : [];
    let inner;
    if (content.length === 0) {
      inner = '';
    } else {
      inner = helpers.renderChildren(content);
    }
    return wrapAlignedMarkdown(inner, node.attrs?.textAlign);
  },
});

export const SiteHeading = Heading.extend({
  addOptions() {
    return { ...this.parent?.(), levels: [1, 2, 3] };
  },
  parseHTML() {
    return [
      { tag: 'h1', attrs: { level: 1 } },
      { tag: 'h2', attrs: { level: 1 } },
      { tag: 'h3', attrs: { level: 1 } },
      { tag: 'h4', attrs: { level: 2 } },
      { tag: 'h5', attrs: { level: 3 } },
      { tag: 'h6', attrs: { level: 3 } },
    ];
  },
  renderMarkdown: (node, helpers) => wrapAlignedMarkdown(
    `${'#'.repeat(node.attrs.level || 1)} ${helpers.renderChildren(node)}`,
    node.attrs?.textAlign,
  ),
  renderHTML({ node, HTMLAttributes }) {
    const tag = LEVEL_TO_TAG[node.attrs.level] || 'h3';
    return [tag, mergeAttributes(this.options.HTMLAttributes, HTMLAttributes), 0];
  },
});

function applyImageBox(img, attrs) {
  img.style.width = attrs.width || '';
  img.style.height = attrs.height || '';
}

function persistImageBox(editor, getPos, img, { lockHeight }) {
  const pos = typeof getPos === 'function' ? getPos() : null;
  if (typeof pos !== 'number') return;
  const current = editor.state.doc.nodeAt(pos);
  if (!current) return;
  const width = img.style.width && img.style.width !== 'auto' ? img.style.width : null;
  const height = lockHeight && img.style.height && img.style.height !== 'auto'
    ? img.style.height
    : null;
  editor.chain().command(({ tr }) => {
    tr.setNodeMarkup(pos, undefined, { ...current.attrs, width, height });
    return true;
  }).run();
}

function bindImageResize(handle, { img, editor, getPos, mode }) {
  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const rect = img.getBoundingClientRect();
    const startW = rect.width;
    const startH = rect.height;
    const edge = handle.dataset.edge || 'e';
    const onMove = (ev) => {
      const dx = ev.clientX - startX;
      const signed = edge === 'w' || edge === 'sw' ? -dx : dx;
      const w = Math.max(48, Math.round(startW + signed));
      img.style.width = w + 'px';
      if (mode === 'stretch') {
        img.style.height = Math.round(startH) + 'px';
      } else {
        img.style.height = 'auto';
      }
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      persistImageBox(editor, getPos, img, { lockHeight: mode === 'stretch' });
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

function sizeFromEl(el, key) {
  const style = el.getAttribute('style') || '';
  const m = style.match(new RegExp(`(?:^|;)\\s*${key}\\s*:\\s*([^;]+)`, 'i'));
  let v = (el.style && el.style[key]) || el.getAttribute(key) || (m && m[1]) || '';
  v = String(v).trim();
  if (v === 'auto') return null;
  if (/^\d+$/.test(v)) v += 'px';
  return cssSize(v) || null;
}

function cssSize(value) {
  const s = String(value || '').trim();
  if (/^-?\d+(\.\d+)?(px|em|rem|%)?$/i.test(s)) {
    return /^\d+(\.\d+)?$/.test(s) ? `${s}px` : s;
  }
  return '';
}

function escapeAttr(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function escapeMdAlt(value) {
  return String(value == null ? '' : value).replace(/\\/g, '\\\\').replace(/]/g, '\\]');
}

export const SiteImage = Image.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      width: {
        default: null,
        parseHTML: (el) => sizeFromEl(el, 'width'),
        renderHTML: (attrs) => {
          const w = cssSize(attrs.width);
          return w ? { style: `width:${w};` } : {};
        },
      },
      height: {
        default: null,
        parseHTML: (el) => sizeFromEl(el, 'height'),
        renderHTML: (attrs) => {
          const h = cssSize(attrs.height);
          return h ? { style: `height:${h};` } : {};
        },
      },
    };
  },
  renderHTML({ node, HTMLAttributes }) {
    const styleParts = [];
    const w = cssSize(node.attrs.width);
    const h = cssSize(node.attrs.height);
    if (w) styleParts.push('width:' + w);
    if (h) styleParts.push('height:' + h);
    const attrs = mergeAttributes(this.options.HTMLAttributes, HTMLAttributes);
    if (styleParts.length) attrs.style = styleParts.join(';') + (attrs.style ? ';' + attrs.style : '');
    return ['img', attrs];
  },
  renderMarkdown: (node) => {
    const src = node.attrs?.src || '';
    const alt = node.attrs?.alt || '';
    const w = cssSize(node.attrs?.width);
    const h = cssSize(node.attrs?.height);
    if (w || h) {
      let style = '';
      if (w) style += `width:${w};`;
      if (h) style += `height:${h};`;
      const altAttr = alt ? ` alt="${escapeAttr(alt)}"` : '';
      return `<img src="${escapeAttr(src)}"${altAttr} style="${style}">`;
    }
    return `![${escapeMdAlt(alt)}](${src})`;
  },
  addNodeView() {
    return ({ node, editor, getPos }) => {
      const wrap = document.createElement('span');
      wrap.className = 'rte-image-wrap';
      const img = document.createElement('img');
      img.src = node.attrs.src || '';
      img.alt = node.attrs.alt || '';
      img.draggable = false;
      applyImageBox(img, node.attrs);

      const handles = [
        { edge: 'se', mode: 'scale', title: 'Scale proportionally' },
        { edge: 'sw', mode: 'scale', title: 'Scale proportionally' },
        { edge: 'e', mode: 'stretch', title: 'Stretch width' },
        { edge: 'w', mode: 'stretch', title: 'Stretch width' },
      ].map((spec) => {
        const handle = document.createElement('span');
        handle.className = 'img-resize-handle ' + spec.edge;
        handle.dataset.edge = spec.edge;
        handle.dataset.mode = spec.mode;
        handle.title = spec.title;
        bindImageResize(handle, { img, editor, getPos, mode: spec.mode });
        return handle;
      });

      wrap.appendChild(img);
      handles.forEach((h) => wrap.appendChild(h));
      return {
        dom: wrap,
        selectNode() { wrap.classList.add('is-selected'); },
        deselectNode() { wrap.classList.remove('is-selected'); },
        update(updated) {
          if (updated.type.name !== 'image') return false;
          img.src = updated.attrs.src || '';
          img.alt = updated.attrs.alt || '';
          applyImageBox(img, updated.attrs);
          return true;
        },
      };
    };
  },
});

export const SiteHighlight = Highlight.extend({
  parseMarkdown: (token) => ({
    type: 'text',
    text: token.text || '',
    marks: [{ type: 'highlight' }],
  }),
  renderMarkdown: (node, helpers) => `<mark>${helpers.renderChildren(node)}</mark>`,
  markdownTokenizer: {
    name: 'highlight',
    level: 'inline',
    start: (src) => {
      const a = src.indexOf('==');
      const b = src.indexOf('<mark');
      if (a < 0) return b;
      if (b < 0) return a;
      return Math.min(a, b);
    },
    tokenize: (src) => {
      let m = src.match(/^==([^=]+)==/);
      if (m) return { type: 'highlight', raw: m[0], text: m[1] };
      m = src.match(/^<mark\b[^>]*>([\s\S]*?)<\/mark>/);
      if (m) return { type: 'highlight', raw: m[0], text: m[1] };
      return undefined;
    },
  },
});

export { TAG_TO_LEVEL };
