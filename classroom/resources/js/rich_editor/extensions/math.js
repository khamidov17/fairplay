import { InlineMath as BaseInlineMath, BlockMath as BaseBlockMath } from '@tiptap/extension-mathematics';

function mathToken(type, raw, latex, delim) {
  return { type, raw, latex: String(latex || '').trim(), delim };
}

/** One-line $$math$$ stays on a single line so mode-switch does not grow fences. */
export function serializeDollarMath(latex) {
  const src = String(latex || '').trim();
  if (!src) return '$$$$';
  if (src.includes('\n')) return `$$\n${src}\n$$`;
  return `$$${src}$$`;
}

export const SiteInlineMath = BaseInlineMath.extend({
  name: 'inlineMath',
  addAttributes() {
    return {
      latex: {
        default: '',
        parseHTML: (el) => el.getAttribute('data-latex') || el.getAttribute('data-tex') || '',
        renderHTML: (attrs) => ({ 'data-latex': attrs.latex, 'data-delim': attrs.delim || 'dollar' }),
      },
      delim: {
        default: 'dollar',
        parseHTML: (el) => el.getAttribute('data-delim') || 'dollar',
        renderHTML: () => ({}),
      },
    };
  },
  parseMarkdown: (token) => ({
    type: 'inlineMath',
    attrs: { latex: token.latex || '', delim: token.delim || 'dollar' },
  }),
  renderMarkdown: (node) => {
    const latex = node.attrs?.latex || '';
    const delim = node.attrs?.delim || 'dollar';
    if (delim === 'tilde') return `~${latex}~`;
    if (delim === 'paren') return `\\(${latex}\\)`;
    return serializeDollarMath(latex);
  },
  markdownTokenizer: {
    name: 'inlineMath',
    level: 'inline',
    start: (src) => {
      const idxs = [
        src.search(/~[^~\n]+~/),
        src.indexOf('\\('),
        src.search(/\$\$[^$\n]+\$\$/),
      ].filter((i) => i >= 0);
      return idxs.length ? Math.min(...idxs) : -1;
    },
    tokenize: (src) => {
      let m = src.match(/^~([^~\n]+?)~/);
      if (m) return mathToken('inlineMath', m[0], m[1], 'tilde');
      m = src.match(/^\\\(([\s\S]+?)\\\)/);
      if (m) return mathToken('inlineMath', m[0], m[1], 'paren');
      m = src.match(/^\$\$([^$\n]+?)\$\$/);
      if (m) return mathToken('inlineMath', m[0], m[1], 'dollar');
      return undefined;
    },
  },
});

export const SiteBlockMath = BaseBlockMath.extend({
  name: 'blockMath',
  parseMarkdown: (token) => ({
    type: 'blockMath',
    attrs: { latex: token.latex || '', delim: token.delim || 'dollar' },
  }),
  addAttributes() {
    return {
      latex: {
        default: '',
        parseHTML: (el) => el.getAttribute('data-latex') || el.getAttribute('data-tex') || '',
        renderHTML: (attrs) => ({ 'data-latex': attrs.latex }),
      },
      delim: {
        default: 'dollar',
        parseHTML: (el) => el.getAttribute('data-delim') || 'dollar',
        renderHTML: () => ({}),
      },
    };
  },
  renderMarkdown: (node) => {
    const latex = node.attrs?.latex || '';
    if (node.attrs?.delim === 'bracket') return `\\[${latex}\\]`;
    return serializeDollarMath(latex);
  },
  markdownTokenizer: {
    name: 'blockMath',
    level: 'block',
    start: (src) => {
      // Only claim display fences ($$ + newline). Inline $$latex$$ must stay inline.
      const bracket = src.indexOf('\\[');
      let fence = -1;
      if (src.startsWith('$$\n')) fence = 0;
      else {
        const mid = src.search(/\n\$\$\n/);
        if (mid >= 0) fence = mid + 1;
      }
      if (fence < 0) return bracket;
      if (bracket < 0) return fence;
      return Math.min(fence, bracket);
    },
    tokenize: (src) => {
      let m = src.match(/^\\\[([\s\S]+?)\\\]/);
      if (m) return mathToken('blockMath', m[0], m[1], 'bracket');
      m = src.match(/^\$\$\n([\s\S]+?)\n\$\$/);
      if (m) return mathToken('blockMath', m[0], m[1], 'dollar');
      return undefined;
    },
  },
});
