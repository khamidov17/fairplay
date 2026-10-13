// Browser entry. `npm run build:editor` writes the gitignored IIFE at resources/rich_editor.js.
import { createTiptapEditor, createHeadlessEditor, parseMarkdownToHtml, serializeHtmlToMarkdown, markdownWithBreaks, sanitizeEditorMarkdown } from './createEditor.js';
import { renumberBlankChips } from './extensions/blank.js';
import { hideVirtualKeyboard, loadMathLive } from './mathField.js';
import { renderMathPalette } from './mathPalette.js';

const config = {
  fileUploadUrl: '',
  searchUsersUrl: '',
  csrfToken: '',
  onChange: null,
  messages: {
    fileUploadFailed: 'File upload failed.',
    fileUploadFailedAuth: 'File upload failed. Make sure you are signed in.',
  },
  fileInputId: 'rte-file',
  fileAccept: '',
};

const instances = new Map();

function t(msgid) {
  return (typeof gettext === 'function') ? gettext(msgid) : msgid;
}

function notifyChange() {
  if (typeof config.onChange === 'function') config.onChange();
}

function getRteButtons() {
  return {
    heading: '<select class="rte-heading" title="' + t('Text level') + '"><option value="p">' + t('Body') + '</option><option value="h3">' + t('Heading 1') + '</option><option value="h4">' + t('Heading 2') + '</option><option value="h5">' + t('Heading 3') + '</option></select>',
    sep: '<span class="sep"></span>',
    bold: '<button type="button" data-cmd="bold" title="' + t('Bold (Ctrl+B)') + '"><i class="fa fa-bold"></i></button>',
    italic: '<button type="button" data-cmd="italic" title="' + t('Italic (Ctrl+I)') + '"><i class="fa fa-italic"></i></button>',
    highlight: '<button type="button" data-cmd="highlight" class="hl-btn" title="' + t('Highlight') + '"><i class="fa fa-paint-brush"></i></button>',
    code: '<button type="button" data-cmd="code" title="' + t('Inline code') + '"><i class="fa fa-code"></i></button>',
    codeblock: '<button type="button" data-cmd="codeblock" title="' + t('Code block (Ctrl+Enter to leave)') + '"><i class="fa fa-file-code-o"></i></button>',
    math: '<button type="button" data-cmd="math" title="' + t('Math (Ctrl+Alt+D)') + '"><i class="fa fa-superscript"></i></button>',
    quote: '<button type="button" data-cmd="quote" title="' + t('Quote (Ctrl+Q)') + '"><i class="fa fa-quote-left"></i></button>',
    ul: '<button type="button" data-cmd="ul" title="' + t('Unordered list (Ctrl+U)') + '"><i class="fa fa-list-ul"></i></button>',
    ol: '<button type="button" data-cmd="ol" title="' + t('Ordered list (Ctrl+Shift+O)') + '"><i class="fa fa-list-ol"></i></button>',
    table: '<button type="button" data-cmd="table" title="' + t('Table') + '"><i class="fa fa-table"></i></button>',
    link: '<button type="button" data-cmd="link" title="' + t('Link (Ctrl+L)') + '"><i class="fa fa-link"></i></button>',
    file: '<button type="button" data-cmd="file" title="' + t('Upload an image or file') + '"' +
      (config.fileUploadUrl ? '' : ' style="display:none;"') + '><i class="fa fa-paperclip"></i></button>',
    mention: '<button type="button" data-cmd="mention" title="' + t('Mention user (Ctrl+M)') + '"><i class="fa fa-at"></i></button>',
    help: '<button type="button" data-cmd="help" title="' + t('Editor help') + '"><i class="fa fa-question-circle"></i></button>',
    alignLeft: '<button type="button" data-cmd="alignLeft" title="' + t('Align left') + '"><i class="fa fa-align-left"></i></button>',
    alignCenter: '<button type="button" data-cmd="alignCenter" title="' + t('Align center') + '"><i class="fa fa-align-center"></i></button>',
    alignRight: '<button type="button" data-cmd="alignRight" title="' + t('Align right') + '"><i class="fa fa-align-right"></i></button>',
    blank: '<button type="button" data-cmd="blank" class="blank-insert" title="' + t('Insert blank') + '" style="display:none;"><i class="fa fa-square-o"></i></button>',
  };
}

const RTE_TOOLBAR = [
  'heading', 'sep',
  'bold', 'highlight', 'italic', 'sep',
  'code', 'codeblock', 'math', 'quote', 'sep',
  'ul', 'ol', 'table', 'sep',
  'alignLeft', 'alignCenter', 'alignRight', 'sep',
  'link', 'file', 'mention', 'help', 'blank',
];

function richEditorTpl(name, opts) {
  opts = opts || {};
  const buttons = getRteButtons();
  const bar = RTE_TOOLBAR.map((k) => buttons[k] || '').join('');
  const placeholder = opts.placeholder || t('Enter content…');
  const mdPlaceholder = opts.mdPlaceholder || t('Enter Markdown…');
  const compact = opts.compact ? ' compact' : '';
  return `<div class="rich-editor mode-rich${compact}" data-editor="${name}">
      <div class="rich-editor-toolbar-row">
        <div class="editor-mode-tabs">
          <button type="button" class="mode-btn active" data-mode="rich">${t('Rich text')}</button>
          <button type="button" class="mode-btn" data-mode="markdown">Markdown</button>
        </div>
        <div class="rich-toolbar">${bar}</div>
      </div>
      <div class="rich-editor-panes">
        <div class="rich-editor-rich"><div class="rte-tiptap-mount"></div></div>
        <div class="rich-editor-markdown"><textarea placeholder="${mdPlaceholder}"></textarea></div>
      </div>
    </div>`;
}

function findRoot(name) {
  return document.querySelector('.rich-editor[data-editor="' + name + '"]');
}

function getInstance(name) {
  return instances.get(name) || null;
}

function getMarkdown(name) {
  const inst = getInstance(name);
  const root = findRoot(name);
  if (!inst || !root) return '';
  if (root.classList.contains('mode-markdown')) {
    return sanitizeEditorMarkdown((root.querySelector('textarea') || {}).value || '');
  }
  return sanitizeEditorMarkdown(inst.getMarkdown() || '');
}

function applyMarkdownContent(inst, md) {
  if (!inst || inst.isDestroyed) return;
  try {
    inst.commands.setContent(markdownWithBreaks(md || ''), { contentType: 'markdown' });
  } catch (err) {
    if (!inst.isDestroyed) {
      try { inst.commands.setContent(md || ''); } catch (e) { /* ignore */ }
    }
  }
}

function setMarkdown(name, md) {
  const inst = getInstance(name);
  const root = findRoot(name);
  if (!root) return;
  const ta = root.querySelector('textarea');
  if (ta) ta.value = sanitizeEditorMarkdown(md || '');
  // Hidden ProseMirror views break if we setContent while display/layout is off.
  if (inst && !root.classList.contains('mode-markdown')) {
    applyMarkdownContent(inst, md);
    renumberBlankChips(root);
  }
}

function editorHtml(name) {
  const inst = getInstance(name);
  if (inst) return inst.getHTML();
  return parseMarkdownToHtml(getMarkdown(name));
}

function flushMarkdown(root) {
  const name = root.getAttribute('data-editor');
  const inst = getInstance(name);
  const ta = root.querySelector('textarea');
  if (inst && ta && !root.classList.contains('mode-markdown')) {
    ta.value = sanitizeEditorMarkdown(inst.getMarkdown() || '');
  }
}

function applyAlign(inst, align) {
  inst.chain().focus().setTextAlign(align).run();
}

function applyOverlayCmd(root, cmd) {
  switch (cmd) {
    case 'table': openTablePicker(root); return true;
    case 'math': openMathPopover(root); return true;
    case 'link': openLinkPopover(root); return true;
    case 'mention': openMentionPopover(root); return true;
    case 'file': pickFile(root); return true;
    case 'help': openHelpModal(); return true;
    default: return false;
  }
}

function applyCmd(root, cmd) {
  const name = root.getAttribute('data-editor');
  const inst = getInstance(name);
  const isMd = root.classList.contains('mode-markdown');
  const ta = root.querySelector('textarea');
  if (applyOverlayCmd(root, cmd)) return;
  if (isMd && ta) {
    applyMarkdownShortcut(ta, cmd);
    ta.dispatchEvent(new Event('input'));
    notifyChange();
    return;
  }
  if (!inst) return;
  switch (cmd) {
    case 'bold': inst.chain().focus().toggleBold().run(); break;
    case 'italic': inst.chain().focus().toggleItalic().run(); break;
    case 'highlight': inst.chain().focus().toggleHighlight().run(); break;
    case 'code': inst.chain().focus().toggleCode().run(); break;
    case 'codeblock': inst.chain().focus().toggleCodeBlock().run(); break;
    case 'quote': inst.chain().focus().toggleBlockquote().run(); break;
    case 'ul': inst.chain().focus().toggleBulletList().run(); break;
    case 'ol': inst.chain().focus().toggleOrderedList().run(); break;
    case 'p': inst.chain().focus().setParagraph().run(); break;
    case 'h3': inst.chain().focus().toggleHeading({ level: 1 }).run(); break;
    case 'h4': inst.chain().focus().toggleHeading({ level: 2 }).run(); break;
    case 'h5': inst.chain().focus().toggleHeading({ level: 3 }).run(); break;
    case 'alignLeft': applyAlign(inst, 'left'); break;
    case 'alignCenter': applyAlign(inst, 'center'); break;
    case 'alignRight': applyAlign(inst, 'right'); break;
    case 'blank': inst.chain().focus().insertBlank().run(); break;
    default: break;
  }
  flushMarkdown(root);
  renumberBlankChips(root);
  notifyChange();
  updateToolbarState(root);
}

const MD_ALIGN_RE = /^<(div|p)\s+[^>]*style="[^"]*text-align:\s*(center|right|left|justify)[^"]*"[^>]*>([\s\S]*?)<\/\1>$/i;

function markdownLineSpan(ta) {
  const v = ta.value;
  let start = ta.selectionStart;
  let end = ta.selectionEnd;
  if (start === end) {
    while (start > 0 && v[start - 1] !== '\n') start -= 1;
    while (end < v.length && v[end] !== '\n') end += 1;
  }
  return { start, end, text: v.slice(start, end) };
}

function applyMarkdownAlign(ta, align) {
  const { start, end, text } = markdownLineSpan(ta);
  const lead = text.match(/^\s*/)?.[0] || '';
  const tail = text.match(/\s*$/)?.[0] || '';
  const body = text.slice(lead.length, text.length - tail.length);
  const wrapped = body.match(MD_ALIGN_RE);
  const inner = wrapped ? wrapped[3] : body;
  const next = (!align || align === 'left')
    ? inner
    : `<div style="text-align:${align}">${inner}</div>`;
  ta.value = vReplace(ta.value, start, end, lead + next + tail);
  ta.selectionStart = start + lead.length;
  ta.selectionEnd = start + lead.length + next.length;
}

function applyMarkdownHeading(ta, cmd) {
  const { start, end, text } = markdownLineSpan(ta);
  const lead = text.match(/^\s*/)?.[0] || '';
  const tail = text.match(/\s*$/)?.[0] || '';
  const body = text.slice(lead.length, text.length - tail.length);
  const stripped = body.replace(/^#{1,6}\s+/, '');
  let prefix = '';
  if (cmd === 'h3') prefix = '# ';
  else if (cmd === 'h4') prefix = '## ';
  else if (cmd === 'h5') prefix = '### ';
  const next = prefix + stripped;
  ta.value = vReplace(ta.value, start, end, lead + next + tail);
  ta.selectionStart = start + lead.length;
  ta.selectionEnd = start + lead.length + next.length;
}

function applyMarkdownLinePrefix(ta, prefix, re) {
  const { start, end, text } = markdownLineSpan(ta);
  const lines = text.split('\n');
  const targets = lines.filter((l) => l.trim());
  const allOn = targets.length > 0 && targets.every((l) => re.test(l));
  const next = lines.map((l) => {
    if (!l.trim()) return l;
    if (allOn) return l.replace(re, '');
    if (re.test(l)) return l;
    return prefix + l.replace(/^(#{1,6}\s+|>\s+|[-*+]\s+|\d+\.\s+)/, '');
  }).join('\n');
  ta.value = vReplace(ta.value, start, end, next);
  ta.selectionStart = start;
  ta.selectionEnd = start + next.length;
}

function vReplace(value, start, end, insert) {
  return value.slice(0, start) + insert + value.slice(end);
}

function applyMarkdownShortcut(ta, cmd) {
  const wrap = (left, right, fallback) => {
    const s = ta.selectionStart;
    const e = ta.selectionEnd;
    const sel = ta.value.slice(s, e) || fallback;
    const ins = left + sel + right;
    ta.value = ta.value.slice(0, s) + ins + ta.value.slice(e);
    ta.selectionStart = ta.selectionEnd = s + ins.length;
  };
  switch (cmd) {
    case 'bold': wrap('**', '**', 'bold'); break;
    case 'italic': wrap('*', '*', 'italic'); break;
    case 'highlight': wrap('<mark>', '</mark>', 'text'); break;
    case 'code': wrap('`', '`', 'code'); break;
    case 'codeblock': wrap('```\n', '\n```', 'code'); break;
    case 'quote': applyMarkdownLinePrefix(ta, '> ', /^>\s+/); break;
    case 'math': wrap('$$', '$$', 'a^2 + b^2 = c^2'); break;
    case 'blank': wrap('{{blank}}', '', ''); break;
    case 'ul': applyMarkdownLinePrefix(ta, '- ', /^[-*+]\s+/); break;
    case 'ol': applyMarkdownLinePrefix(ta, '1. ', /^\d+\.\s+/); break;
    case 'p':
    case 'h3':
    case 'h4':
    case 'h5':
      applyMarkdownHeading(ta, cmd);
      break;
    case 'alignLeft': applyMarkdownAlign(ta, 'left'); break;
    case 'alignCenter': applyMarkdownAlign(ta, 'center'); break;
    case 'alignRight': applyMarkdownAlign(ta, 'right'); break;
    default: break;
  }
}

function updateToolbarState(root) {
  if (!root || root.classList.contains('mode-markdown')) return;
  const inst = getInstance(root.getAttribute('data-editor'));
  if (!inst) return;
  const map = {
    bold: () => inst.isActive('bold'),
    italic: () => inst.isActive('italic'),
    highlight: () => inst.isActive('highlight'),
    code: () => inst.isActive('code'),
    codeblock: () => inst.isActive('codeBlock'),
    quote: () => inst.isActive('blockquote'),
    ul: () => inst.isActive('bulletList'),
    ol: () => inst.isActive('orderedList'),
    link: () => inst.isActive('link'),
  };
  root.querySelectorAll('.rich-toolbar button[data-cmd]').forEach((btn) => {
    const fn = map[btn.dataset.cmd];
    btn.classList.toggle('active', !!(fn && fn()));
  });
  const headSel = root.querySelector('.rte-heading');
  if (headSel) {
    if (inst.isActive('heading', { level: 1 })) headSel.value = 'h3';
    else if (inst.isActive('heading', { level: 2 })) headSel.value = 'h4';
    else if (inst.isActive('heading', { level: 3 })) headSel.value = 'h5';
    else headSel.value = 'p';
  }
}

function bindRoot(root) {
  if (root.dataset.bound) return;
  root.dataset.bound = '1';
  const name = root.getAttribute('data-editor');
  if (!root.querySelector('.rich-editor-panes')) {
    const panes = document.createElement('div');
    panes.className = 'rich-editor-panes';
    const rich = root.querySelector('.rich-editor-rich');
    const md = root.querySelector('.rich-editor-markdown');
    if (rich && md) {
      rich.parentNode.insertBefore(panes, rich);
      panes.appendChild(rich);
      panes.appendChild(md);
    }
  }
  let mount = root.querySelector('.rte-tiptap-mount');
  if (!mount) {
    const rich = root.querySelector('.rich-editor-rich') || root;
    const legacy = rich.querySelector('.rich-editor-body');
    if (legacy) legacy.remove();
    mount = document.createElement('div');
    mount.className = 'rte-tiptap-mount';
    rich.appendChild(mount);
  }
  const ta = root.querySelector('textarea');
  const placeholder = (mount && mount.getAttribute('data-placeholder'))
    || (root.querySelector('.rich-editor-body') && root.querySelector('.rich-editor-body').getAttribute('data-placeholder'))
    || t('Enter content…');

  const inst = createTiptapEditor({
    element: mount,
    placeholder,
    onMathClick: () => openMathPopover(root, true),
    onMentionTrigger: () => openMentionPopover(root),
    onPaste: (event) => handleImagePaste(root, event),
    onUpdate: () => {
      if (!root.classList.contains('mode-markdown')) {
        flushMarkdown(root);
        renumberBlankChips(root);
        notifyChange();
        updateToolbarState(root);
        syncTableCellChrome(root);
      }
    },
    onSelectionUpdate: () => syncTableCellChrome(root),
  });
  instances.set(name, inst);
  if (ta && ta.value) applyMarkdownContent(inst, ta.value);
  renumberBlankChips(root);

  root.querySelectorAll('.mode-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const toMd = btn.dataset.mode === 'markdown';
      const fromMd = root.classList.contains('mode-markdown');
      if (toMd === fromMd) return;
      if (root._rteModeRaf) {
        cancelAnimationFrame(root._rteModeRaf);
        root._rteModeRaf = 0;
      }
      if (toMd) {
        flushMarkdown(root);
        root.classList.add('mode-markdown');
        root.classList.remove('mode-rich');
        hideTableChrome();
        if (ta) ta.focus();
      } else {
        root.classList.remove('mode-markdown');
        root.classList.add('mode-rich');
        root._rteModeRaf = requestAnimationFrame(() => {
          root._rteModeRaf = 0;
          if (inst.isDestroyed || root.classList.contains('mode-markdown')) return;
          applyMarkdownContent(inst, (ta && ta.value) || '');
          try { inst.chain().focus('start', { scrollIntoView: false }).run(); } catch (e) { /* jsdom */ }
          renumberBlankChips(root);
          notifyChange();
        });
      }
      root.querySelectorAll('.mode-btn').forEach((b) => b.classList.toggle('active', b === btn));
      notifyChange();
    });
  });

  if (ta) {
    ta.addEventListener('input', () => notifyChange());
    ta.addEventListener('paste', (e) => {
      if (handleImagePaste(root, e)) e.preventDefault();
    });
  }

  root.querySelectorAll('.rich-toolbar button[data-cmd]').forEach((btn) => {
    btn.addEventListener('mousedown', (e) => {
      if (['link', 'mention', 'file', 'math', 'table', 'help'].indexOf(btn.dataset.cmd) !== -1) {
        e.preventDefault();
      }
    });
    btn.addEventListener('click', () => applyCmd(root, btn.dataset.cmd));
  });
  const headSel = root.querySelector('.rte-heading');
  if (headSel) headSel.addEventListener('change', () => applyCmd(root, headSel.value));

  root.addEventListener('keydown', (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (!mod) return;
    const key = (e.key || '').toLowerCase();
    let cmd = null;
    if (e.altKey && key === 'd') cmd = 'math';
    else if (e.altKey && key === '1') cmd = 'h3';
    else if (e.altKey && key === '2') cmd = 'h4';
    else if (e.altKey && key === '3') cmd = 'h5';
    else if (e.shiftKey && key === 'o') cmd = 'ol';
    else if (!e.altKey && !e.shiftKey) {
      if (key === 'b') cmd = 'bold';
      else if (key === 'i') cmd = 'italic';
      else if (key === 'l') cmd = 'link';
      else if (key === 'q') cmd = 'quote';
      else if (key === 'u') cmd = 'ul';
      else if (key === 'm') cmd = 'mention';
    }
    if (!cmd) return;
    e.preventDefault();
    applyCmd(root, cmd);
  });

  syncUploadButtons(root);
}

function syncUploadButtons(root) {
  const scope = root || document;
  (scope.matches && scope.matches('.rich-editor') ? [scope] : scope.querySelectorAll('.rich-editor')).forEach((el) => {
    const fileBtn = el.querySelector('[data-cmd="file"]');
    if (fileBtn) {
      fileBtn.style.display = config.fileUploadUrl ? '' : 'none';
      fileBtn.setAttribute(
        'title',
        config.fileAccept && config.fileAccept.indexOf('image/') === 0
          ? t('Upload an image')
          : t('Upload an image or file'),
      );
    }
  });
}

function mountRichEditors(scope) {
  (scope || document).querySelectorAll('[data-rich-mount]').forEach((m) => {
    const name = m.getAttribute('data-rich-mount');
    m.outerHTML = richEditorTpl(name, {
      compact: m.hasAttribute('data-compact'),
      placeholder: m.getAttribute('data-placeholder') || undefined,
    });
  });
}

function initEditors(scope) {
  (scope || document).querySelectorAll('.rich-editor').forEach(bindRoot);
}

/* ---- overlays ---- */
function helpBodyHtml() {
  return `
    <p>${t('Switch between Rich text and Markdown at any time. The toolbar works in both modes. Students see the same formatting as you.')}</p>
    <h3>${t('Rich text')}</h3>
    <ul>
      <li>${t('Select text, then use the toolbar for headings, bold, italic, highlight, inline code, quotes, lists, and alignment.')}</li>
      <li>${t('Press Enter for a new line. Leave a blank line when you want a new paragraph.')}</li>
      <li>${t('In a code block, press Ctrl+Enter to leave the block.')}</li>
    </ul>
    <h3>${t('Equations')}</h3>
    <ul>
      <li>${t('Click the formula button, then click a symbol. Dashed boxes are slots you can type into.')}</li>
      <li>${t('Click an existing formula in the document to edit it.')}</li>
      <li>${t('Turn on “Directly edit LaTeX” only if you want to type raw LaTeX.')}</li>
    </ul>
    <h3>${t('Tables, images, links')}</h3>
    <ul>
      <li>${t('Insert a table, then use the cell menu to add or delete rows and columns.')}</li>
      <li>${t('Use the paperclip to upload an image or file, or paste an image from the clipboard.')}</li>
      <li>${t('Use the link button for a web link, or @ to mention a user.')}</li>
    </ul>
    <h3>${t('Fill-in blanks')}</h3>
    <ul>
      <li>${t('On fill-in problems, click the blank button. Chips are numbered automatically.')}</li>
    </ul>
    <h3>${t('Keyboard shortcuts')}</h3>
    <table class="rte-help-table">
      <thead><tr><th>${t('Action')}</th><th>${t('Shortcut')}</th></tr></thead>
      <tbody>
        <tr><td>${t('Bold')}</td><td><code>Ctrl+B</code></td></tr>
        <tr><td>${t('Italic')}</td><td><code>Ctrl+I</code></td></tr>
        <tr><td>${t('Quote')}</td><td><code>Ctrl+Q</code></td></tr>
        <tr><td>${t('Unordered list')}</td><td><code>Ctrl+U</code></td></tr>
        <tr><td>${t('Ordered list')}</td><td><code>Ctrl+Shift+O</code></td></tr>
        <tr><td>${t('Link')}</td><td><code>Ctrl+L</code></td></tr>
        <tr><td>${t('Mention')}</td><td><code>Ctrl+M</code></td></tr>
        <tr><td>${t('Math')}</td><td><code>Ctrl+Alt+D</code></td></tr>
        <tr><td>${t('Close dialog')}</td><td><code>Esc</code></td></tr>
      </tbody>
    </table>
    <h3>Markdown</h3>
    <p>${t('If you prefer typing markup, switch to the Markdown tab. These are the site-specific bits:')}</p>
    <table class="rte-help-table">
      <thead><tr><th>${t('Style')}</th><th>Markdown</th></tr></thead>
      <tbody>
        <tr><td>${t('Bold')}</td><td><code>**text**</code></td></tr>
        <tr><td>${t('Italic')}</td><td><code>*text*</code></td></tr>
        <tr><td>${t('Highlight')}</td><td><code>&lt;mark&gt;text&lt;/mark&gt;</code></td></tr>
        <tr><td>${t('Inline code')}</td><td><code>\`code\`</code></td></tr>
        <tr><td>${t('Equation')}</td><td><code>$$a^2+b^2=c^2$$</code></td></tr>
        <tr><td>${t('Blank')}</td><td><code>{{blank}}</code></td></tr>
        <tr><td>${t('Link')}</td><td><code>[text](https://…)</code></td></tr>
        <tr><td>${t('Image')}</td><td><code>![alt](url)</code></td></tr>
        <tr><td>${t('Mention')}</td><td><code>[user:name]</code></td></tr>
        <tr><td>${t('Table')}</td><td><code>| Col 1 | Col 2 |</code></td></tr>
      </tbody>
    </table>
    <p>${t('Problem statements use Markdown. Full syntax:')} <a href="https://commonmark.org/help/" target="_blank" rel="noopener">CommonMark</a>.</p>
  `;
}

let overlays = null;
function ensureOverlays() {
  if (overlays && overlays.root && overlays.root.isConnected) return overlays;
  overlays = null;
  if (typeof document === 'undefined') return null;
  const root = document.createElement('div');
  root.id = 'rte-overlays';
  root.innerHTML = `
    <div id="rte-link-popover" class="rte-popover" hidden>
      <label class="rte-field"><span>${t('Text')}</span><input type="text" class="rte-link-text" placeholder="${t('Link text')}"></label>
      <label class="rte-field"><span>${t('URL')}</span><input type="url" class="rte-link-url" placeholder="https://"></label>
      <div class="rte-popover-actions"><button type="button" class="rte-btn-cancel">${t('Cancel')}</button><button type="button" class="rte-btn-ok">${t('Insert')}</button></div>
    </div>
    <div id="rte-mention-popover" class="rte-popover" hidden>
      <label class="rte-field"><span>${t('Name or username')}</span><input type="text" class="rte-mention-input" placeholder="${t('Search users…')}" autocomplete="off"></label>
      <ul class="rte-mention-results"></ul>
      <div class="rte-popover-actions"><button type="button" class="rte-btn-cancel">${t('Cancel')}</button><button type="button" class="rte-btn-ok">${t('Insert')}</button></div>
    </div>
    <div id="rte-math-popover" class="rte-math-modal" hidden>
      <div class="rte-math-dialog" role="dialog" aria-modal="true" aria-labelledby="rte-math-title">
        <div class="rte-help-head">
          <h2 id="rte-math-title">${t('Equation editor')}</h2>
          <button type="button" class="rte-help-close" aria-label="${t('Close')}">×</button>
        </div>
        <div class="rte-math-body">
          ${renderMathPalette()}
          <div class="rte-math-field-wrap" hidden></div>
          <textarea class="rte-math-input" rows="3" placeholder="a^2 + b^2 = c^2"></textarea>
          <label class="rte-math-latex-row">
            <input type="checkbox" class="rte-math-latex-check">
            ${t('Directly edit LaTeX')}
          </label>
        </div>
        <div class="rte-popover-actions">
          <button type="button" class="rte-btn-cancel">${t('Cancel')}</button>
          <button type="button" class="rte-btn-ok">${t('Insert')}</button>
        </div>
      </div>
    </div>
    <div id="rte-help-modal" class="rte-help-modal" hidden>
      <div class="rte-help-dialog" role="dialog" aria-modal="true" aria-labelledby="rte-help-title">
        <div class="rte-help-head">
          <h2 id="rte-help-title">${t('Editor help')}</h2>
          <button type="button" class="rte-help-close" aria-label="${t('Close')}">×</button>
        </div>
        <div class="rte-help-body">
          ${helpBodyHtml()}
        </div>
      </div>
    </div>
    <div id="rte-table-picker" class="rte-table-picker" hidden>
      <div class="rte-table-picker-grid"></div>
      <div class="rte-table-picker-label">3 × 3</div>
    </div>
    <div id="rte-table-cell-chrome" class="rte-table-cell-chrome" hidden>
      <button type="button" class="rte-table-cell-handle" title="${t('Table row and column')}"><i class="fa fa-ellipsis-v"></i></button>
    </div>
    <div id="rte-table-cell-menu" class="rte-table-cell-menu" hidden>
      <button type="button" data-table-act="row-above">${t('Insert row above')}</button>
      <button type="button" data-table-act="row-below">${t('Insert row below')}</button>
      <button type="button" data-table-act="col-left">${t('Insert column left')}</button>
      <button type="button" data-table-act="col-right">${t('Insert column right')}</button>
      <div class="rte-table-cell-menu-sep"></div>
      <button type="button" data-table-act="del-row">${t('Delete row')}</button>
      <button type="button" data-table-act="del-col">${t('Delete column')}</button>
      <button type="button" data-table-act="del-table">${t('Delete table')}</button>
    </div>`;
  document.body.appendChild(root);
  overlays = {
    root,
    link: root.querySelector('#rte-link-popover'),
    mention: root.querySelector('#rte-mention-popover'),
    math: root.querySelector('#rte-math-popover'),
    help: root.querySelector('#rte-help-modal'),
    tablePicker: root.querySelector('#rte-table-picker'),
    tableChrome: root.querySelector('#rte-table-cell-chrome'),
    tableMenu: root.querySelector('#rte-table-cell-menu'),
  };
  bindTableOverlays(overlays);
  overlays.link.querySelector('.rte-btn-cancel').addEventListener('click', closeOverlays);
  overlays.link.querySelector('.rte-btn-ok').addEventListener('click', confirmLink);
  overlays.mention.querySelector('.rte-btn-cancel').addEventListener('click', closeOverlays);
  overlays.mention.querySelector('.rte-btn-ok').addEventListener('click', () => confirmMention());
  overlays.math.querySelector('.rte-btn-cancel').addEventListener('click', closeOverlays);
  // mousedown: MathLive blur/keyboard-hide otherwise eats the click on Insert.
  overlays.math.querySelector('.rte-btn-ok').addEventListener('mousedown', (e) => {
    e.preventDefault();
    confirmMath();
  });
  overlays.math.querySelector('.rte-btn-ok').addEventListener('click', (e) => {
    e.preventDefault();
    confirmMath();
  });
  overlays.math.querySelector('.rte-help-close').addEventListener('click', closeOverlays);
  overlays.math.addEventListener('click', (e) => {
    if (e.target === overlays.math) closeOverlays();
  });
  overlays.math.querySelector('.rte-math-latex-check').addEventListener('change', (e) => {
    setMathLatexMode(!!e.target.checked);
  });
  bindMathPalette(overlays.math);
  overlays.math.querySelector('.rte-math-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      confirmMath();
    }
  });
  overlays.math.querySelector('.rte-math-input').addEventListener('input', () => {
    const mf = overlays.math.querySelector('math-field');
    if (mf && !overlays.math.querySelector('.rte-math-latex-check').checked) {
      mf.value = overlays.math.querySelector('.rte-math-input').value;
    }
  });
  overlays.help.addEventListener('click', (e) => {
    if (e.target === overlays.help) closeOverlays();
  });
  overlays.help.querySelector('.rte-help-close').addEventListener('click', closeOverlays);
  if (!overlayEscapeBound) {
    overlayEscapeBound = true;
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || !overlays) return;
      const open = (overlays.help && !overlays.help.hidden)
        || (overlays.math && !overlays.math.hidden)
        || (overlays.link && !overlays.link.hidden)
        || (overlays.mention && !overlays.mention.hidden)
        || (overlays.tablePicker && !overlays.tablePicker.hidden);
      if (!open) return;
      e.preventDefault();
      closeOverlays();
    });
  }
  const mi = overlays.mention.querySelector('.rte-mention-input');
  let mentionTimer = null;
  mi.addEventListener('input', () => {
    clearTimeout(mentionTimer);
    mentionTimer = setTimeout(() => searchUsers(mi.value), 200);
  });
  overlays.mention.querySelector('.rte-mention-results').addEventListener('click', (e) => {
    const li = e.target.closest('[data-username]');
    if (li) confirmMention(li.getAttribute('data-username'), li.getAttribute('data-label'));
  });
  return overlays;
}

let overlayRoot = null;
let savedInsertPos = null;
let overlayEscapeBound = false;
function hidePopovers() {
  const o = ensureOverlays();
  if (!o) return;
  o.link.hidden = true;
  o.mention.hidden = true;
  o.math.hidden = true;
  o.help.hidden = true;
  if (o.tablePicker) o.tablePicker.hidden = true;
  hideTableCellMenu();
}

function closeOverlays() {
  hidePopovers();
  hideVirtualKeyboard();
  overlayRoot = null;
  savedInsertPos = null;
}

function positionPopover(pop, root, cmd) {
  const btn = root.querySelector('.rich-toolbar button[data-cmd="' + cmd + '"]');
  const rect = (btn || root).getBoundingClientRect();
  pop.hidden = false;
  // position:fixed is viewport-relative. Adding scrollY/scrollX drops the
  // popover to the bottom of a scrolled create/edit page.
  pop.style.position = 'fixed';
  const gap = 6;
  const width = pop.offsetWidth || 280;
  const height = pop.offsetHeight || 0;
  let top = rect.bottom + gap;
  if (height && top + height > window.innerHeight - 8) {
    top = Math.max(8, rect.top - height - gap);
  }
  const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
  pop.style.top = top + 'px';
  pop.style.left = left + 'px';
}

const TABLE_PICKER_MAX = 10;
let tableChromeRoot = null;
let tableOverlayBound = false;

function fillEmptyTableHeaders(inst, cols) {
  if (!inst) return;
  const spots = [];
  inst.state.doc.descendants((node, pos) => {
    if (spots.length >= cols) return false;
    if (node.type.name === 'tableHeader' && !String(node.textContent || '').trim()) {
      spots.push(pos);
    }
    return undefined;
  });
  if (!spots.length) return;
  inst.chain().command(({ tr }) => {
    for (let i = spots.length - 1; i >= 0; i--) {
      const pos = spots[i];
      const header = tr.doc.nodeAt(pos);
      const para = header && header.firstChild;
      if (!para || !para.isTextblock) continue;
      const from = pos + 2;
      const to = from + para.content.size;
      tr.insertText(t('Col') + ' ' + (i + 1), from, to);
    }
    return true;
  }).run();
}

function buildPipeTableMd(rows, cols) {
  rows = Math.max(1, Math.min(TABLE_PICKER_MAX, rows | 0));
  cols = Math.max(1, Math.min(TABLE_PICKER_MAX, cols | 0));
  const header = [];
  const sep = [];
  for (let c = 0; c < cols; c++) {
    header.push(t('Col') + ' ' + (c + 1));
    sep.push('---');
  }
  const lines = ['| ' + header.join(' | ') + ' |', '| ' + sep.join(' | ') + ' |'];
  for (let r = 1; r < rows; r++) {
    lines.push('| ' + Array(cols).fill('').join(' | ') + ' |');
  }
  return lines.join('\n');
}

function ensureTablePickerGrid(grid) {
  if (!grid || grid.childElementCount) return;
  for (let r = 1; r <= TABLE_PICKER_MAX; r++) {
    for (let c = 1; c <= TABLE_PICKER_MAX; c++) {
      const cell = document.createElement('button');
      cell.type = 'button';
      cell.className = 'rte-table-picker-cell';
      cell.dataset.rows = String(r);
      cell.dataset.cols = String(c);
      grid.appendChild(cell);
    }
  }
}

function highlightTablePicker(rows, cols) {
  const o = ensureOverlays();
  if (!o || !o.tablePicker) return;
  o.tablePicker.querySelectorAll('.rte-table-picker-cell').forEach((cell) => {
    const r = Number(cell.dataset.rows);
    const c = Number(cell.dataset.cols);
    cell.classList.toggle('is-active', r <= rows && c <= cols);
  });
  const label = o.tablePicker.querySelector('.rte-table-picker-label');
  if (label) label.textContent = rows + ' × ' + cols;
}

function insertTableAt(root, rows, cols) {
  const inst = getInstance(root.getAttribute('data-editor'));
  if (root.classList.contains('mode-markdown')) {
    const ta = root.querySelector('textarea');
    if (!ta) return;
    const md = buildPipeTableMd(rows, cols);
    const s = ta.selectionStart;
    const pad = (s > 0 && ta.value[s - 1] !== '\n') ? '\n\n' : '';
    const text = pad + md + '\n\n';
    ta.value = ta.value.slice(0, s) + text + ta.value.slice(ta.selectionEnd);
    ta.selectionStart = ta.selectionEnd = s + text.length;
    ta.dispatchEvent(new Event('input'));
  } else if (inst) {
    inst.chain().focus().insertTable({ rows, cols, withHeaderRow: true }).run();
    fillEmptyTableHeaders(inst, cols);
    flushMarkdown(root);
    syncTableCellChrome(root);
  }
  notifyChange();
}

function openTablePicker(root) {
  const o = ensureOverlays();
  if (!o || !o.tablePicker) return;
  if (!o.tablePicker.hidden && overlayRoot === root) {
    closeOverlays();
    return;
  }
  hidePopovers();
  overlayRoot = root;
  ensureTablePickerGrid(o.tablePicker.querySelector('.rte-table-picker-grid'));
  highlightTablePicker(3, 3);
  positionPopover(o.tablePicker, root, 'table');
}

function hideTableCellMenu() {
  const o = overlays;
  if (!o || !o.tableMenu) return;
  o.tableMenu.hidden = true;
  if (o.tableChrome) o.tableChrome.classList.remove('menu-open');
}

function hideTableChrome() {
  const o = overlays;
  if (!o) return;
  hideTableCellMenu();
  if (o.tableChrome) o.tableChrome.hidden = true;
  document.querySelectorAll('.rte-table-cell-active').forEach((el) => {
    el.classList.remove('rte-table-cell-active');
  });
  tableChromeRoot = null;
}

function currentTableCellEl(inst) {
  if (!inst || inst.isDestroyed || !inst.isActive('table')) return null;
  const { view, state } = inst;
  const $from = state.selection.$from;
  for (let d = $from.depth; d > 0; d--) {
    const name = $from.node(d).type.name;
    if (name === 'tableCell' || name === 'tableHeader') {
      const node = view.nodeDOM($from.before(d));
      if (node && node.nodeType === 1) return node.closest('td, th') || node;
    }
  }
  const at = view.domAtPos(state.selection.from);
  const el = at.node && (at.node.nodeType === 1 ? at.node : at.node.parentElement);
  return el && el.closest('td, th');
}

function syncTableCellChrome(root) {
  const o = ensureOverlays();
  if (!o || !o.tableChrome) return;
  if (!root || root.classList.contains('mode-markdown')) {
    hideTableChrome();
    return;
  }
  const inst = getInstance(root.getAttribute('data-editor'));
  const cell = inst && currentTableCellEl(inst);
  if (!cell) {
    const active = document.activeElement;
    if (active && active.closest && active.closest('.rte-table-cell-chrome, .rte-table-cell-menu')) {
      return;
    }
    hideTableChrome();
    return;
  }
  const prev = document.querySelector('.rte-table-cell-active');
  if (prev && prev !== cell) hideTableCellMenu();
  document.querySelectorAll('.rte-table-cell-active').forEach((el) => {
    if (el !== cell) el.classList.remove('rte-table-cell-active');
  });
  cell.classList.add('rte-table-cell-active');
  tableChromeRoot = root;
  const rect = cell.getBoundingClientRect();
  o.tableChrome.hidden = false;
  o.tableChrome.style.top = Math.round(rect.top + 3) + 'px';
  o.tableChrome.style.left = Math.round(rect.right - 20) + 'px';
}

function runTableAction(act) {
  const root = tableChromeRoot || overlayRoot;
  if (!root) return;
  const inst = getInstance(root.getAttribute('data-editor'));
  if (!inst) return;
  const chain = inst.chain().focus();
  if (act === 'row-above') chain.addRowBefore().run();
  else if (act === 'row-below') chain.addRowAfter().run();
  else if (act === 'col-left') chain.addColumnBefore().run();
  else if (act === 'col-right') chain.addColumnAfter().run();
  else if (act === 'del-row') chain.deleteRow().run();
  else if (act === 'del-col') chain.deleteColumn().run();
  else if (act === 'del-table') chain.deleteTable().run();
  hideTableCellMenu();
  flushMarkdown(root);
  notifyChange();
  if (act === 'del-table' || !inst.isActive('table')) hideTableChrome();
  else syncTableCellChrome(root);
}

function bindTableOverlays(o) {
  ensureTablePickerGrid(o.tablePicker.querySelector('.rte-table-picker-grid'));
  o.tablePicker.addEventListener('mouseover', (e) => {
    const cell = e.target.closest('.rte-table-picker-cell');
    if (!cell) return;
    highlightTablePicker(Number(cell.dataset.rows), Number(cell.dataset.cols));
  });
  o.tablePicker.addEventListener('click', (e) => {
    const cell = e.target.closest('.rte-table-picker-cell');
    if (!cell || !overlayRoot) return;
    const root = overlayRoot;
    const rows = Number(cell.dataset.rows);
    const cols = Number(cell.dataset.cols);
    closeOverlays();
    insertTableAt(root, rows, cols);
  });
  o.tableChrome.querySelector('.rte-table-cell-handle').addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (!o.tableMenu.hidden) {
      hideTableCellMenu();
      return;
    }
    const inst = tableChromeRoot && getInstance(tableChromeRoot.getAttribute('data-editor'));
    o.tableMenu.querySelectorAll('[data-table-act]').forEach((btn) => {
      const act = btn.getAttribute('data-table-act');
      let ok = true;
      if (inst) {
        if (act === 'del-row') ok = inst.can().deleteRow();
        else if (act === 'del-col') ok = inst.can().deleteColumn();
        else if (act === 'del-table') ok = inst.can().deleteTable();
      }
      btn.disabled = !ok;
    });
    const handle = e.currentTarget.getBoundingClientRect();
    o.tableMenu.hidden = false;
    o.tableChrome.classList.add('menu-open');
    const width = o.tableMenu.offsetWidth || 200;
    o.tableMenu.style.top = Math.round(handle.bottom + 4) + 'px';
    o.tableMenu.style.left = Math.max(8, Math.round(handle.right - width)) + 'px';
  });
  o.tableMenu.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-table-act]');
    if (btn && !btn.disabled) runTableAction(btn.getAttribute('data-table-act'));
  });
  if (!tableOverlayBound) {
    tableOverlayBound = true;
    document.addEventListener('mousedown', (e) => {
      const target = e.target;
      if (!target.closest || target.closest('.rte-table-picker, [data-cmd="table"]')) return;
      if (overlays && overlays.tablePicker) overlays.tablePicker.hidden = true;
      if (target.closest('.rte-table-cell-chrome, .rte-table-cell-menu')) return;
      hideTableCellMenu();
    });
    window.addEventListener('scroll', () => {
      if (tableChromeRoot) syncTableCellChrome(tableChromeRoot);
    }, true);
    window.addEventListener('resize', () => {
      if (tableChromeRoot) syncTableCellChrome(tableChromeRoot);
    });
  }
}

function openLinkPopover(root) {
  const o = ensureOverlays();
  overlayRoot = root;
  o.mention.hidden = true;
  o.math.hidden = true;
  if (o.tablePicker) o.tablePicker.hidden = true;
  hideTableCellMenu();
  o.link.querySelector('.rte-link-text').value = '';
  o.link.querySelector('.rte-link-url').value = 'https://';
  positionPopover(o.link, root, 'link');
}

function confirmLink() {
  const o = ensureOverlays();
  if (!o || !overlayRoot) return;
  const root = overlayRoot;
  const text = (o.link.querySelector('.rte-link-text').value || '').trim() || t('Link text');
  const href = (o.link.querySelector('.rte-link-url').value || '').trim() || 'https://';
  const inst = getInstance(root.getAttribute('data-editor'));
  closeOverlays();
  if (root.classList.contains('mode-markdown')) {
    const ta = root.querySelector('textarea');
    const md = '[' + text + '](' + href + ')';
    const s = ta.selectionStart;
    ta.value = ta.value.slice(0, s) + md + ta.value.slice(ta.selectionEnd);
    ta.dispatchEvent(new Event('input'));
  } else if (inst) {
    inst.chain().focus().setLink({ href }).insertContent(text).run();
    flushMarkdown(root);
  }
  notifyChange();
}

function openMentionPopover(root) {
  const o = ensureOverlays();
  overlayRoot = root;
  o.link.hidden = true;
  o.math.hidden = true;
  if (o.tablePicker) o.tablePicker.hidden = true;
  hideTableCellMenu();
  o.mention.querySelector('.rte-mention-input').value = '';
  o.mention.querySelector('.rte-mention-results').innerHTML = '<li class="muted">' + t('Type a name or username') + '</li>';
  positionPopover(o.mention, root, 'mention');
  o.mention.querySelector('.rte-mention-input').focus();
}

function mentionSearchUrl(q) {
  const base = config.searchUsersUrl;
  const sep = base.indexOf('?') >= 0 ? '&' : '?';
  return base + sep + 'username=' + encodeURIComponent(q || '');
}

function searchUsers(q) {
  const o = ensureOverlays();
  if (!o || !config.searchUsersUrl) return;
  const list = o.mention.querySelector('.rte-mention-results');
  const query = String(q || '').trim();
  if (!query) {
    list.innerHTML = '<li class="muted">' + t('Type a name or username') + '</li>';
    return;
  }
  const url = mentionSearchUrl(query);
  fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
    .then((r) => r.json())
    .then((body) => {
      const rows = (body && body.data) || [];
      list.innerHTML = rows.length
        ? rows.map((u) => '<li data-username="' + escapeAttr(u.username) + '" data-label="' + escapeAttr(u.text || u.username) + '">' + escapeHtml(u.text || u.username) + '</li>').join('')
        : '<li class="muted">' + t('No match; you can still insert') + '</li>';
    })
    .catch(() => { list.innerHTML = '<li class="muted">' + t('Search failed') + '</li>'; });
}

function confirmMention(username, label) {
  const o = ensureOverlays();
  if (!o || !overlayRoot) return;
  const root = overlayRoot;
  const name = (username || o.mention.querySelector('.rte-mention-input').value || '').trim();
  if (!name) return;
  const inst = getInstance(root.getAttribute('data-editor'));
  closeOverlays();
  if (root.classList.contains('mode-markdown')) {
    const ta = root.querySelector('textarea');
    const token = '[user:' + name + ']';
    const s = ta.selectionStart;
    ta.value = ta.value.slice(0, s) + token + ta.value.slice(ta.selectionEnd);
    ta.dispatchEvent(new Event('input'));
  } else if (inst) {
    inst.chain().focus().insertMention({
      username: name,
      kind: 'user',
      label: label || name,
    }).run();
    flushMarkdown(root);
  }
  notifyChange();
}

function mathFieldEl() {
  const o = ensureOverlays();
  return o ? o.math.querySelector('math-field') : null;
}

function bindMathPalette(mathRoot) {
  if (!mathRoot || mathRoot.dataset.paletteBound) return;
  mathRoot.dataset.paletteBound = '1';
  mathRoot.addEventListener('click', (e) => {
    const tab = e.target.closest('[data-math-tab]');
    if (tab) {
      e.preventDefault();
      const id = tab.getAttribute('data-math-tab');
      mathRoot.querySelectorAll('.rte-math-tab').forEach((b) => b.classList.toggle('is-active', b === tab));
      mathRoot.querySelectorAll('[data-math-panel]').forEach((p) => {
        p.hidden = p.getAttribute('data-math-panel') !== id;
      });
      return;
    }
    const key = e.target.closest('[data-math-tex]');
    if (key) {
      e.preventDefault();
      insertMathSnippet(
        key.getAttribute('data-math-tex') || '',
        key.getAttribute('data-math-advanced') || '',
      );
    }
  });
}

function toMathLiveInsert(latex) {
  return String(latex || '')
    .replace(/\\vec\{\\placeholder\{\}\}/g, '\\overrightarrow{#0}')
    .replace(/\\(?:hat|widehat)\{\\placeholder\{\}\}/g, '\\overset{\\wedge}{#0}')
    .replace(/\\placeholder\{\}/g, '#0');
}

function insertMathSnippet(latex, advanced) {
  if (!latex) return;
  const o = ensureOverlays();
  if (!o) return;
  const ta = o.math.querySelector('.rte-math-input');
  const check = o.math.querySelector('.rte-math-latex-check');
  const mf = mathFieldEl();
  const forLatex = advanced || latex
    .replace(/\\placeholder\{\}/g, '')
    .replace(/#@|#\?|#0/g, '');
  if (check && check.checked || !mf) {
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    ta.value = ta.value.slice(0, start) + forLatex + ta.value.slice(end);
    ta.selectionStart = ta.selectionEnd = start + forLatex.length;
    ta.dispatchEvent(new Event('input'));
    ta.focus();
    return;
  }
  const live = toMathLiveInsert(latex);
  try {
    if (typeof mf.insert === 'function') {
      mf.insert(live, { focus: true, insertionMode: 'insert', selectionMode: 'placeholder' });
    } else if (typeof mf.executeCommand === 'function') {
      mf.executeCommand(['insert', live]);
    } else {
      mf.value = (mf.value || '') + forLatex;
    }
    try {
      if (typeof mf.executeCommand === 'function') mf.executeCommand('moveToNextPlaceholder');
    } catch (e) { /* optional */ }
    ta.value = (typeof mf.getValue === 'function' ? mf.getValue('latex') : mf.value) || ta.value;
    mf.focus();
  } catch (e) {
    ta.value = (ta.value || '') + forLatex;
  }
}

function setMathLatexMode(on) {
  const o = ensureOverlays();
  if (!o) return;
  const wrap = o.math.querySelector('.rte-math-field-wrap');
  const ta = o.math.querySelector('.rte-math-input');
  const mf = mathFieldEl();
  const check = o.math.querySelector('.rte-math-latex-check');
  if (check) check.checked = !!on;
  hideVirtualKeyboard();
  if (on) {
    if (mf) ta.value = mf.value || ta.value;
    if (wrap) wrap.hidden = true;
    ta.hidden = false;
    ta.focus();
  } else if (mf) {
    mf.value = ta.value;
    if (wrap) wrap.hidden = false;
    ta.hidden = true;
    try { mf.focus(); } catch (e) { /* jsdom */ }
  } else {
    ta.hidden = false;
  }
}

function normalizeMathLatex(raw) {
  let s = String(raw || '').trim();
  s = s.replace(/^\$\$([\s\S]*)\$\$$/, '$1').trim();
  s = s.replace(/^\$([\s\S]*)\$$/, '$1').trim();
  s = s.replace(/^\\\(([\s\S]*)\\\)$/, '$1').trim();
  s = s.replace(/^\\\[([\s\S]*)\\\]$/, '$1').trim();
  s = s.replace(/\\placeholder\[[^\]]*\]\{\}/g, '');
  s = s.replace(/\\placeholder\{\}/g, '');
  s = s.replace(/#\d/g, '');
  s = s.replace(/\{\{\s*\}\}/g, '');
  return s;
}

function currentMathLatex() {
  const o = ensureOverlays();
  if (!o) return '';
  const check = o.math.querySelector('.rte-math-latex-check');
  const ta = o.math.querySelector('.rte-math-input');
  const mf = mathFieldEl();
  if (check && check.checked) return (ta && ta.value) || '';
  if (mf) {
    let v = '';
    try {
      if (typeof mf.getValue === 'function') {
        v = mf.getValue('latex-without-placeholders') || mf.getValue('latex') || '';
      } else {
        v = mf.value || '';
      }
    } catch (e) { v = mf.value || ''; }
    if (v) return v;
  }
  return (ta && ta.value) || '';
}

function mountMathField(seed) {
  const o = ensureOverlays();
  if (!o) return Promise.resolve();
  const wrap = o.math.querySelector('.rte-math-field-wrap');
  const ta = o.math.querySelector('.rte-math-input');
  hideVirtualKeyboard();
  return loadMathLive().then((MFE) => {
    if (!MFE || !wrap) {
      ta.hidden = false;
      return;
    }
    let mf = wrap.querySelector('math-field');
    if (!mf) {
      mf = new MFE();
      mf.className = 'rte-math-field';
      mf.setAttribute('smart-fence', 'on');
      mf.mathVirtualKeyboardPolicy = 'manual';
      mf.addEventListener('input', () => {
        if (!o.math.querySelector('.rte-math-latex-check').checked) ta.value = mf.value;
      });
      wrap.appendChild(mf);
    }
    mf.mathVirtualKeyboardPolicy = 'manual';
    mf.value = seed;
    ta.value = seed;
    setMathLatexMode(false);
    hideVirtualKeyboard();
  });
}

function openMathPopover(root) {
  const o = ensureOverlays();
  overlayRoot = root;
  hidePopovers();
  hideVirtualKeyboard();
  hideTableCellMenu();
  const inst = getInstance(root.getAttribute('data-editor'));
  let seed = 'a^2 + b^2 = c^2';
  savedInsertPos = null;
  if (inst) {
    savedInsertPos = inst.state.selection.from;
    const node = inst.state.doc.nodeAt(inst.state.selection.from);
    if (node && (node.type.name === 'inlineMath' || node.type.name === 'blockMath') && node.attrs.latex) {
      seed = node.attrs.latex;
    }
  }
  const ta = o.math.querySelector('.rte-math-input');
  ta.value = seed;
  o.math.querySelector('.rte-math-latex-check').checked = false;
  o.math.hidden = false;
  mountMathField(seed).then(() => {
    if (o.math.hidden) return;
    if (!mathFieldEl()) ta.focus();
  });
}

function confirmMath() {
  const o = ensureOverlays();
  if (!o || !overlayRoot) return;
  const root = overlayRoot;
  const pos = savedInsertPos;
  const latex = normalizeMathLatex(currentMathLatex()) || 'a^2 + b^2 = c^2';
  const inst = getInstance(root.getAttribute('data-editor'));
  closeOverlays();
  if (root.classList.contains('mode-markdown')) {
    const ta = root.querySelector('textarea');
    if (!ta) return;
    const md = latex.includes('\n') ? ('$$\n' + latex + '\n$$') : ('$$' + latex + '$$');
    const s = ta.selectionStart;
    ta.value = ta.value.slice(0, s) + md + ta.value.slice(ta.selectionEnd);
    ta.dispatchEvent(new Event('input'));
    notifyChange();
    return;
  }
  if (!inst) return;
  const node = { type: 'inlineMath', attrs: { latex, delim: 'dollar' } };
  const existing = typeof pos === 'number' ? inst.state.doc.nodeAt(pos) : null;
  if (existing && (existing.type.name === 'inlineMath' || existing.type.name === 'blockMath')) {
    inst.chain().focus().command(({ tr }) => {
      tr.setNodeMarkup(pos, existing.type, { latex, delim: existing.attrs.delim || 'dollar' });
      return true;
    }).run();
  } else {
    const size = inst.state.doc.content.size;
    const at = (typeof pos === 'number' && pos >= 0 && pos <= size) ? pos : size;
    const ok = inst.chain().focus().insertContentAt(at, node).run();
    if (!ok) inst.chain().focus().insertContent(node).run();
  }
  flushMarkdown(root);
  notifyChange();
}

function openHelpModal() {
  const o = ensureOverlays();
  if (!o || !o.help) return;
  if (!o.help.hidden) {
    closeOverlays();
    return;
  }
  hidePopovers();
  o.help.hidden = false;
}

/* ---- upload ---- */
let pendingFileRoot = null;

function clipboardImageFiles(event) {
  const cd = event && (event.clipboardData || (event.originalEvent && event.originalEvent.clipboardData));
  if (!cd) return [];
  const out = [];
  const add = (file) => {
    if (file && file.type && file.type.indexOf('image/') === 0) out.push(file);
  };
  if (cd.files && cd.files.length) {
    Array.from(cd.files).forEach(add);
  }
  if (!out.length && cd.items && cd.items.length) {
    Array.from(cd.items).forEach((item) => {
      if (item && item.kind === 'file' && typeof item.getAsFile === 'function') add(item.getAsFile());
    });
  }
  return out;
}

function handleImagePaste(root, event) {
  const files = clipboardImageFiles(event);
  if (!files.length) return false;
  files.forEach((file) => uploadEditorFile(root, file));
  return true;
}

function ensureHiddenFileInput(id, accept, onChange) {
  let input = document.getElementById(id);
  if (!input) {
    input = document.createElement('input');
    input.type = 'file';
    input.id = id;
    input.style.display = 'none';
    document.body.appendChild(input);
  }
  if (accept) input.accept = accept;
  else input.removeAttribute('accept');
  input.onchange = onChange;
  return input;
}

function pickFile(root) {
  if (!config.fileUploadUrl) {
    alert(config.messages.fileUploadFailed);
    return;
  }
  pendingFileRoot = root;
  const input = ensureHiddenFileInput(config.fileInputId, config.fileAccept || '', onFilePicked);
  input.value = '';
  input.click();
}

function insertImageAt(root, url, alt) {
  const inst = getInstance(root.getAttribute('data-editor'));
  if (root.classList.contains('mode-markdown')) {
    const ta = root.querySelector('textarea');
    const md = '![' + (alt || '') + '](' + url + ')';
    const s = ta.selectionStart;
    ta.value = ta.value.slice(0, s) + md + ta.value.slice(ta.selectionEnd);
    ta.dispatchEvent(new Event('input'));
  } else if (inst) {
    inst.chain().focus().setImage({ src: url, alt: alt || '' }).run();
    flushMarkdown(root);
  }
  notifyChange();
}

function insertFileLinkAt(root, url, name) {
  const inst = getInstance(root.getAttribute('data-editor'));
  const label = String(name || 'file').replace(/[\r\n[\]]/g, ' ').trim() || 'file';
  if (root.classList.contains('mode-markdown')) {
    const ta = root.querySelector('textarea');
    const md = '[' + label + '](' + url + ')';
    const s = ta.selectionStart;
    ta.value = ta.value.slice(0, s) + md + ta.value.slice(ta.selectionEnd);
    ta.dispatchEvent(new Event('input'));
  } else if (inst) {
    inst.chain().focus().insertContent({
      type: 'text',
      text: label,
      marks: [{ type: 'link', attrs: { href: url } }],
    }).run();
    flushMarkdown(root);
  }
  notifyChange();
}

function uploadEditorFile(root, file) {
  if (!file || !root) return Promise.resolve();
  if (!config.fileUploadUrl) {
    alert(config.messages.fileUploadFailed);
    return Promise.resolve();
  }
  const fd = new FormData();
  fd.append('markdown-file-upload', file);
  return fetch(config.fileUploadUrl, {
    method: 'POST',
    headers: { 'X-CSRFToken': config.csrfToken },
    body: fd,
  })
    .then((r) => r.json().then((body) => ({ ok: r.ok, body })).catch(() => ({ ok: false, body: null })))
    .then(({ ok, body }) => {
      if (!ok || !body || body.status !== 200 || !body.link) {
        alert((body && body.error) || config.messages.fileUploadFailedAuth);
        return;
      }
      if (body.is_image) {
        insertImageAt(root, body.link, body.name || file.name || '');
        return;
      }
      insertFileLinkAt(root, body.link, body.name || file.name || '');
    })
    .catch(() => alert(config.messages.fileUploadFailed));
}

function onFilePicked(e) {
  const file = e.target.files && e.target.files[0];
  const root = pendingFileRoot;
  pendingFileRoot = null;
  uploadEditorFile(root, file);
}

function escapeAttr(s) {
  return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function configure(opts) {
  if (!opts) return;
  if (opts.fileUploadUrl != null) config.fileUploadUrl = opts.fileUploadUrl;
  if (opts.searchUsersUrl != null) config.searchUsersUrl = opts.searchUsersUrl;
  if (opts.csrfToken != null) config.csrfToken = opts.csrfToken;
  if (opts.onChange != null) config.onChange = opts.onChange;
  if (opts.fileInputId != null) config.fileInputId = opts.fileInputId;
  if (opts.fileAccept != null) config.fileAccept = opts.fileAccept;
  if (opts.messages) Object.assign(config.messages, opts.messages);
  if (typeof document !== 'undefined') syncUploadButtons(document);
}

function paintDisplay(el, md) {
  if (!el) return '';
  const html = parseMarkdownToHtml(md || '');
  el.innerHTML = html || '';
  el.classList.add('rte-display');
  return html;
}

function paintTargets(scope) {
  const root = scope || (typeof document === 'undefined' ? null : document);
  if (!root || !root.querySelectorAll) return;
  root.querySelectorAll('script.rte-md-source').forEach((script) => {
    const target = script.nextElementSibling;
    if (!target) return;
    let md = '';
    try { md = JSON.parse(script.textContent || '""'); } catch (e) { md = script.textContent || ''; }
    paintDisplay(target, md);
  });
}

const RichEditor = {
  configure,
  html: richEditorTpl,
  mount(root) {
    mountRichEditors(root);
    initEditors(root);
  },
  init(root) {
    initEditors(root);
  },
  getMarkdown,
  getHtml: editorHtml,
  setMarkdown,
  htmlToMd: serializeHtmlToMarkdown,
  mdToHtml: parseMarkdownToHtml,
  paintDisplay,
  paintTargets,
  onFilePicked,
  _createHeadless: createHeadlessEditor,
  _toMathLiveInsert: toMathLiveInsert,
  _instances: instances,
};

export default RichEditor;
