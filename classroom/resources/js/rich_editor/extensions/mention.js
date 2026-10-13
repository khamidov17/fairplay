import { Node, mergeAttributes } from '@tiptap/core';

function userPageHref(username) {
  return '/user/' + encodeURIComponent(username || '');
}

function applyMentionDom(span, link, node) {
  const username = node.attrs.username || '';
  span.className = 'rte-mention rating rate-none user';
  span.setAttribute('data-type', 'mention');
  span.setAttribute('data-username', username);
  span.setAttribute('data-kind', node.attrs.kind || 'user');
  link.href = userPageHref(username);
  link.textContent = username;
}

export const Mention = Node.create({
  name: 'mention',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,

  addAttributes() {
    return {
      username: { default: '' },
      kind: { default: 'user' },
      label: { default: null },
    };
  },

  parseHTML() {
    return [{
      tag: 'span[data-type="mention"]',
      getAttrs: (el) => ({
        username: el.getAttribute('data-username') || '',
        kind: el.getAttribute('data-kind') || 'user',
        label: el.getAttribute('data-label') || null,
      }),
    }];
  },

  renderHTML({ node, HTMLAttributes }) {
    const username = node.attrs.username || '';
    return ['span', mergeAttributes(HTMLAttributes, {
      'data-type': 'mention',
      'data-username': username,
      'data-kind': node.attrs.kind || 'user',
      class: 'rte-mention rating rate-none user',
    }), ['a', { href: userPageHref(username) }, username]];
  },

  addNodeView() {
    return ({ node }) => {
      const span = document.createElement('span');
      const link = document.createElement('a');
      link.addEventListener('click', (e) => e.preventDefault());
      span.appendChild(link);
      applyMentionDom(span, link, node);
      return {
        dom: span,
        update(updated) {
          if (updated.type.name !== 'mention') return false;
          applyMentionDom(span, link, updated);
          return true;
        },
      };
    };
  },

  parseMarkdown: (token) => ({
    type: 'mention',
    attrs: {
      username: token.username || '',
      kind: token.kind || 'user',
      label: token.username || '',
    },
  }),

  renderMarkdown: (node) => {
    const kind = node.attrs?.kind === 'ruser' ? 'ruser' : 'user';
    return `[${kind}:${node.attrs?.username || ''}]`;
  },

  markdownTokenizer: {
    name: 'mention',
    level: 'inline',
    start: (src) => src.search(/\[r?user:/),
    tokenize: (src) => {
      const match = src.match(/^\[(r?user):([^\]]+)\]/);
      if (!match) return undefined;
      return {
        type: 'mention',
        raw: match[0],
        kind: match[1],
        username: match[2],
      };
    },
  },

  addCommands() {
    return {
      insertMention: (attrs) => ({ commands }) => commands.insertContent({
        type: this.name,
        attrs: {
          username: attrs.username || '',
          kind: attrs.kind || 'user',
          label: attrs.label || attrs.username || '',
        },
      }),
    };
  },
});
