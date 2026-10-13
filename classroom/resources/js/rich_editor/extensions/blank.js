import { Node, mergeAttributes } from '@tiptap/core';

export const Blank = Node.create({
  name: 'blank',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,
  draggable: false,

  parseHTML() {
    return [{ tag: 'span[data-type="blank"]' }];
  },

  renderHTML({ HTMLAttributes }) {
    return ['span', mergeAttributes(HTMLAttributes, { 'data-type': 'blank', class: 'rte-blank' })];
  },

  parseMarkdown: () => ({ type: 'blank' }),

  renderMarkdown: () => '{{blank}}',

  markdownTokenizer: {
    name: 'blank',
    level: 'inline',
    start: (src) => {
      const i = src.search(/\{\{\s*blank\s*\}\}/);
      return i;
    },
    tokenize: (src) => {
      const match = src.match(/^\{\{\s*blank\s*\}\}/);
      if (!match) return undefined;
      return { type: 'blank', raw: match[0] };
    },
  },

  addCommands() {
    return {
      insertBlank: () => ({ commands }) => commands.insertContent({ type: this.name }),
    };
  },

  addNodeView() {
    return () => {
      const el = document.createElement('span');
      el.className = 'rte-blank';
      el.setAttribute('data-type', 'blank');
      el.contentEditable = 'false';
      el.textContent = '(1)';
      return { dom: el };
    };
  },
});

export function renumberBlankChips(root) {
  if (!root) return;
  root.querySelectorAll('.rte-blank').forEach((el, i) => {
    el.textContent = '(' + (i + 1) + ')';
  });
}
