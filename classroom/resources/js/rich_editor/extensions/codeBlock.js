import { CodeBlockLowlight } from '@tiptap/extension-code-block-lowlight';
import { common, createLowlight } from 'lowlight';

const lowlight = createLowlight(common);

export const CODE_LANGUAGES = [
  { id: 'plaintext', label: 'Plain text' },
  { id: 'python', label: 'Python' },
  { id: 'c', label: 'C' },
  { id: 'cpp', label: 'C++' },
  { id: 'java', label: 'Java' },
  { id: 'javascript', label: 'JavaScript' },
  { id: 'typescript', label: 'TypeScript' },
  { id: 'html', label: 'HTML' },
  { id: 'css', label: 'CSS' },
  { id: 'json', label: 'JSON' },
  { id: 'bash', label: 'Bash' },
  { id: 'sql', label: 'SQL' },
  { id: 'markdown', label: 'Markdown' },
  { id: 'rust', label: 'Rust' },
  { id: 'go', label: 'Go' },
];

function languageOrPlain(value) {
  const id = String(value || '').trim();
  if (!id) return 'plaintext';
  if (CODE_LANGUAGES.some((item) => item.id === id)) return id;
  return id;
}

export const SiteCodeBlock = CodeBlockLowlight.extend({
  addOptions() {
    return {
      ...this.parent?.(),
      lowlight,
      defaultLanguage: 'plaintext',
      HTMLAttributes: { class: 'hljs' },
    };
  },

  addNodeView() {
    return ({ node, editor, getPos }) => {
      const wrap = document.createElement('div');
      wrap.className = 'code-block';
      const bar = document.createElement('div');
      bar.className = 'code-block-lang';
      const sel = document.createElement('select');
      sel.className = 'code-block-lang-input';
      sel.setAttribute('aria-label', 'Code language');
      CODE_LANGUAGES.forEach((item) => {
        const opt = document.createElement('option');
        opt.value = item.id;
        opt.textContent = item.label;
        sel.appendChild(opt);
      });
      const current = languageOrPlain(node.attrs.language);
      if (!CODE_LANGUAGES.some((item) => item.id === current)) {
        const extra = document.createElement('option');
        extra.value = current;
        extra.textContent = current;
        sel.appendChild(extra);
      }
      sel.value = current;
      sel.addEventListener('mousedown', (e) => e.stopPropagation());
      sel.addEventListener('change', () => {
        const pos = typeof getPos === 'function' ? getPos() : null;
        if (typeof pos !== 'number') return;
        editor.chain().focus().command(({ tr }) => {
          tr.setNodeMarkup(pos, undefined, { language: sel.value });
          return true;
        }).run();
      });
      bar.appendChild(sel);
      const pre = document.createElement('pre');
      const code = document.createElement('code');
      pre.appendChild(code);
      wrap.appendChild(bar);
      wrap.appendChild(pre);
      return {
        dom: wrap,
        contentDOM: code,
        update(updated) {
          if (updated.type.name !== 'codeBlock') return false;
          sel.value = languageOrPlain(updated.attrs.language);
          return true;
        },
      };
    };
  },
});
