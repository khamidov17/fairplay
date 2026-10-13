(function () {
  'use strict';
  var app = document.getElementById('workspace');
  if (!app) return;
  var $ = function (selector) { return document.querySelector(selector); };
  var $$ = function (selector) { return Array.prototype.slice.call(document.querySelectorAll(selector)); };
  var body = $('#ws-body'), drawer = $('#ws-drawer'), backdrop = $('#drawer-backdrop');
  var source = $('#workspace-source'), mount = $('#monaco-editor'), editor = null, runTimer = null;
  var layoutKey = 'dkuoj-ws-layout', splitKey = 'dkuoj-ws-split-', ideSplitKey = 'dkuoj-ws-ide-split', page = 0;
  var ACE_TO_MONACO = {
    c_cpp: 'cpp', cpp: 'cpp', c: 'c', csharp: 'csharp', golang: 'go', go: 'go',
    java: 'java', javascript: 'javascript', python: 'python', rust: 'rust',
    ruby: 'ruby', php: 'php', kotlin: 'kotlin', swift: 'swift', typescript: 'typescript',
    pascal: 'pascal', haskell: 'haskell', lua: 'lua', perl: 'perl', r: 'r', scala: 'scala',
  };
  var darkQuery = window.matchMedia('(prefers-color-scheme: dark)');

  function showDrawer(show) {
    drawer.classList.toggle('open', show);
    backdrop.classList.toggle('open', show);
    var overview = $('#btn-overview');
    overview.setAttribute('aria-expanded', String(show));
    overview.classList.toggle('is-active', show);
    if (show) renderDrawer();
  }
  function layout(mode) { body.classList.toggle('layout-lr', mode === 'lr'); body.classList.toggle('layout-tb', mode === 'tb'); $('#layout-mode').textContent = mode === 'lr' ? gettext('Left-right') : gettext('Top-bottom'); localStorage.setItem(layoutKey, mode); applySplit(); }
  function applySplit() { var mode = body.classList.contains('layout-tb') ? 'tb' : 'lr'; var percent = localStorage.getItem(splitKey + mode) || '50'; body.style.setProperty('--workspace-split', percent + '%'); }
  function applyIdeSplit(frac) {
    var value = Math.max(0.28, Math.min(0.78, Number(frac) || 0.62));
    app.style.setProperty('--ide-editor-frac', value.toFixed(4));
    localStorage.setItem(ideSplitKey, String(value));
    if (editor && editor.layout) editor.layout();
  }
  function monacoTheme() { return darkQuery.matches ? 'vs-dark' : 'vs'; }
  function syncMonacoTheme() {
    if (editor && window.monaco) window.monaco.editor.setTheme(monacoTheme());
  }
  function activateTab(name) { $$('[data-tab]').forEach(function (el) { el.classList.toggle('active', el.dataset.tab === name); }); $$('.ws-tab-panel').forEach(function (el) { el.classList.toggle('active', el.id === 'panel-' + name); }); }
  function activateBottom(name) { $$('[data-bottom-tab]').forEach(function (el) { el.classList.toggle('active', el.dataset.bottomTab === name); }); ['cases', 'result'].forEach(function (key) { var panel = $('#bottom-' + key); if (panel) panel.hidden = key !== name; }); }
  function sourceValue() { return editor ? editor.getValue() : source.value; }
  function setSource(value) { if (editor) editor.setValue(value); else source.value = value; }
  function languageSelect() { return $('#id_language'); }
  function parseLanguageTemplates() {
    var el = $('#workspace-language-templates');
    if (!el) return {};
    try { return JSON.parse(el.textContent || '{}') || {}; } catch (err) { return {}; }
  }
  var languageTemplates = parseLanguageTemplates();
  function templateForLanguage(langId) {
    if (langId == null || langId === '') return '';
    var code = languageTemplates[String(langId)];
    return code == null ? '' : code;
  }
  function applyLanguageTemplate() {
    var select = languageSelect();
    setSource(templateForLanguage(select && select.value));
  }
  // Drafts only for assignment/exam (join → leave). Practice keeps no answer drafts.
  function draftsEnabled() {
    var mode = app.dataset.mode || 'practice';
    return (mode === 'assignment' || mode === 'exam') && !!(app.dataset.contextId || '');
  }
  function draftContextPrefix() {
    var mode = app.dataset.mode || 'practice';
    var ctx = app.dataset.contextId || '_';
    return 'dkuoj-ws-draft-' + mode + '-' + ctx + '-' + app.dataset.problem;
  }
  function draftKey() { var language = languageSelect(); return draftContextPrefix() + '-' + (language ? language.value : 'answer'); }
  function setDraftStatus(text, kind) {
    var status = $('#draft-status');
    if (!status) return;
    status.textContent = text || '';
    status.classList.toggle('is-error', kind === 'error');
  }
  function showAutosaveHint(visible) {
    var hint = $('#ws-autosave-hint');
    if (hint) hint.hidden = !visible;
  }
  // Server-side drafts. localStorage keeps the answer through a reload, but the server cannot read
  // it, so the deadline sweep would have nothing to submit for a student who never pressed Submit.
  // The answer is therefore mirrored to the server, which is also what makes it follow the student
  // to another device. Present only in assignments where this participation is actually graded.
  var serverDraftUrl = app.dataset.draftUrl || '';
  var serverDraftTimer = null;
  var serverDraftRetryTimer = null;
  var pendingServerDraft = null;
  // 'idle' | 'saving' | 'saved' | 'error' — local-only copy must not hide a failed sync.
  var serverDraftSync = 'idle';
  var SERVER_DRAFT_RETRY_MS = [2000, 5000];
  function serverDraftsEnabled() { return !!serverDraftUrl; }
  function serverDraftBody(payload) {
    var data = new FormData();
    data.append('csrfmiddlewaretoken', csrfToken());
    data.append('content', payload.content == null ? '' : payload.content);
    if (payload.language) data.append('language', payload.language);
    return data;
  }
  function cancelServerDraftRetry() {
    if (serverDraftRetryTimer) { clearTimeout(serverDraftRetryTimer); serverDraftRetryTimer = null; }
  }
  function cancelPendingServerDraft() {
    pendingServerDraft = null;
    if (serverDraftTimer) { clearTimeout(serverDraftTimer); serverDraftTimer = null; }
    cancelServerDraftRetry();
  }
  function setLocalDraftStatus() {
    if (serverDraftSync === 'error' || serverDraftSync === 'saving') return;
    setDraftStatus(gettext('Saved on this device only'), 'local');
  }
  function markServerDraftSaved() {
    serverDraftSync = 'saved';
    setDraftStatus(gettext('Answer saved'), 'saved');
    showAutosaveHint(true);
  }
  function markServerDraftFailed(payload, attempt) {
    if (attempt < SERVER_DRAFT_RETRY_MS.length) {
      serverDraftSync = 'error';
      setDraftStatus(gettext('Not synced to the server. Retrying…'), 'error');
      showAutosaveHint(false);
      serverDraftRetryTimer = setTimeout(function () {
        serverDraftRetryTimer = null;
        sendServerDraft(payload, attempt + 1);
      }, SERVER_DRAFT_RETRY_MS[attempt]);
      return;
    }
    serverDraftSync = 'error';
    setDraftStatus(gettext('Not synced to the server'), 'error');
    showAutosaveHint(false);
  }
  function sendServerDraft(payload, attempt) {
    if (!serverDraftsEnabled()) return Promise.resolve();
    attempt = attempt || 0;
    cancelServerDraftRetry();
    serverDraftSync = 'saving';
    return fetch(serverDraftUrl, { method: 'POST', credentials: 'same-origin', body: serverDraftBody(payload) })
      .then(function (response) {
        if (response.ok) markServerDraftSaved();
        else markServerDraftFailed(payload, attempt);
      })
      .catch(function () { markServerDraftFailed(payload, attempt); });
  }
  // Typing fires constantly, so text answers are coalesced; a discrete choice is sent at once.
  function queueServerDraft(content, language) {
    if (!serverDraftsEnabled()) return;
    pendingServerDraft = { content: content, language: language };
    if (serverDraftTimer) clearTimeout(serverDraftTimer);
    cancelServerDraftRetry();
    serverDraftTimer = setTimeout(function () {
      serverDraftTimer = null;
      var payload = pendingServerDraft;
      pendingServerDraft = null;
      if (payload) sendServerDraft(payload);
    }, 1500);
  }
  function saveServerDraftNow(content, language) {
    if (!serverDraftsEnabled()) return;
    cancelPendingServerDraft();
    sendServerDraft({ content: content, language: language });
  }
  function flushServerDraft(options) {
    // pagehide: sendBeacon is still delivered, an in-flight fetch may not be.
    // Timer expiry keeps the page open, so use fetch and let retries update the status.
    if (!serverDraftsEnabled() || !pendingServerDraft) return Promise.resolve();
    var payload = pendingServerDraft;
    cancelPendingServerDraft();
    var useBeacon = !(options && options.beacon === false);
    if (useBeacon && navigator.sendBeacon && navigator.sendBeacon(serverDraftUrl, serverDraftBody(payload))) {
      markServerDraftSaved();
      return Promise.resolve();
    }
    return sendServerDraft(payload);
  }
  // Leave waits for this so the harvest in ContestLeave sees the last keystrokes.
  window.dkuojFlushServerDraft = function () {
    return flushServerDraft({ beacon: false });
  };
  function serverDraft() {
    var el = $('#workspace-server-draft');
    if (!el) return null;
    var payload;
    try { payload = JSON.parse(el.textContent || '{}'); } catch (err) { return null; }
    return payload && payload.content != null ? payload : null;
  }
  // The stored content mirrors the submitted answer's shape, one JSON object per question type.
  function serverDraftAnswer() {
    var draft = serverDraft();
    if (!draft) return null;
    try { return JSON.parse(draft.content || '{}'); } catch (err) { return null; }
  }
  window.addEventListener('pagehide', flushServerDraft);

  // A file answer cannot ride along in the text draft, so it gets its own upload. It is sent the
  // moment the student picks the file rather than debounced: there are no keystrokes to coalesce,
  // and the whole point is to cover the student who chooses a file and never presses Submit.
  function setFileDraftLine(text) {
    var line = $('#ws-file-draft');
    if (!line) return;
    line.textContent = text || '';
    line.hidden = !text;
  }
  function uploadFileDraft(file) {
    if (!serverDraftsEnabled() || !file) return;
    var data = new FormData();
    data.append('csrfmiddlewaretoken', csrfToken());
    data.append('file', file);
    setFileDraftLine(gettext('Saving your file…'));
    fetch(serverDraftUrl, { method: 'POST', credentials: 'same-origin', body: data })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (payload) {
          if (!response.ok || !payload.file) {
            // Naming the reason matters here: the usual causes are a rejected extension or an
            // oversized file, both of which the student can act on.
            throw new Error(payload.error || gettext('Your file was not saved.'));
          }
          setFileDraftLine(interpolate(
            gettext('Saved: %(name)s. If you do not submit it yourself, this file is submitted ' +
                    'for you at the deadline.'),
            {name: payload.file.name},
          ));
          showAutosaveHint(true);
        });
      })
      .catch(function (err) { setFileDraftLine(err.message || gettext('Your file was not saved.')); });
  }
  if (isFileProblem() && serverDraftsEnabled()) {
    document.addEventListener('change', function (event) {
      var input = event.target;
      if (!input || input.type !== 'file' || !input.closest('.ws-file-upload')) return;
      if (input.files && input.files.length) uploadFileDraft(input.files[0]);
    });
  }

  function saveDraft() {
    if (!draftsEnabled() || !source) return;
    try { localStorage.setItem(draftKey(), sourceValue()); } catch (err) { /* ignore */ }
    setLocalDraftStatus();
    var language = languageSelect();
    queueServerDraft(sourceValue(), language ? language.value : '');
  }
  function resetFileInputs() {
    // After a successful submit the input still holds that file. Picking a revised copy with the
    // same name does not fire `change` in most browsers, so the new answer would never be saved
    // as a draft. Clearing the value makes the next selection a real change.
    $$('.ws-file-upload input[type="file"]').forEach(function (input) { input.value = ''; });
  }
  function clearCurrentDrafts() {
    // Called after a successful submit, which already dropped the server draft. Cancelling the
    // pending save matters: otherwise a queued keystroke lands afterwards and recreates the draft,
    // making an already-submitted answer look like unsubmitted work to the deadline sweep.
    cancelPendingServerDraft();
    // The submit deleted the whole draft server-side, file included, so the "saved" line would
    // otherwise still be advertising a file that is no longer held for this student.
    setFileDraftLine('');
    showAutosaveHint(false);
    resetFileInputs();
    if (!draftsEnabled() || !window.localStorage) return;
    var prefix = draftContextPrefix() + '-';
    var toRemove = [];
    for (var i = 0; i < localStorage.length; i++) {
      var key = localStorage.key(i);
      if (key && key.indexOf(prefix) === 0) toRemove.push(key);
    }
    toRemove.forEach(function (key) { localStorage.removeItem(key); });
    setDraftStatus('');
  }
  function monacoLanguageFromOption(option) {
    if (!option) return 'cpp';
    var monaco = option.dataset.monaco || option.dataset.ace || '';
    if (ACE_TO_MONACO[monaco]) return ACE_TO_MONACO[monaco];
    if (monaco) return monaco;
    var name = (option.textContent || '').toLowerCase();
    if (name.indexOf('python') >= 0) return 'python';
    if (name.indexOf('java') >= 0 && name.indexOf('script') < 0) return 'java';
    return 'cpp';
  }
  function setEditorLanguage() {
    if (!editor) return;
    var select = languageSelect();
    var option = select && select.selectedOptions && select.selectedOptions[0];
    window.monaco.editor.setModelLanguage(editor.getModel(), monacoLanguageFromOption(option));
  }

  $('#btn-overview').addEventListener('click', function () { showDrawer(!drawer.classList.contains('open')); });
  var drawerClose = $('#drawer-close');
  if (drawerClose) drawerClose.addEventListener('click', function () { showDrawer(false); });
  backdrop.addEventListener('click', function () { showDrawer(false); });
  document.addEventListener('keydown', function (event) { if (event.key === 'Escape') showDrawer(false); });
  $('#layout-toggle').addEventListener('click', function () { layout(body.classList.contains('layout-lr') ? 'tb' : 'lr'); });
  layout(localStorage.getItem(layoutKey) || (window.innerWidth <= 800 ? 'tb' : 'lr'));
  $$('[data-tab]').forEach(function (tab) {
    tab.addEventListener('click', function () {
      activateTab(tab.dataset.tab);
      if (tab.dataset.tab === 'subs') loadSubScope(currentSubScope || 'my');
    });
  });
  $$('[data-bottom-tab]').forEach(function (tab) { tab.addEventListener('click', function () { activateBottom(tab.dataset.bottomTab); }); });
  var splitter = $('#ws-splitter'), dragging = false;
  splitter.addEventListener('pointerdown', function (event) { dragging = true; splitter.classList.add('is-dragging'); splitter.setPointerCapture(event.pointerId); });
  splitter.addEventListener('pointermove', function (event) { if (!dragging) return; var rect = body.getBoundingClientRect(), topBottom = body.classList.contains('layout-tb'); var raw = topBottom ? (event.clientY - rect.top) / rect.height * 100 : (event.clientX - rect.left) / rect.width * 100; raw = Math.max(22, Math.min(78, raw)); body.style.setProperty('--workspace-split', raw + '%'); localStorage.setItem(splitKey + (topBottom ? 'tb' : 'lr'), raw); });
  splitter.addEventListener('pointerup', function () { dragging = false; splitter.classList.remove('is-dragging'); });

  applyIdeSplit(localStorage.getItem(ideSplitKey) || 0.62);
  var ideStack = $('.ide-stack'), ideSplitter = $('#ide-v-splitter'), ideDragging = false;
  if (ideStack && ideSplitter) {
    ideSplitter.addEventListener('pointerdown', function (event) {
      ideDragging = true;
      ideSplitter.classList.add('is-dragging');
      ideSplitter.setPointerCapture(event.pointerId);
    });
    ideSplitter.addEventListener('pointermove', function (event) {
      if (!ideDragging) return;
      var rect = ideStack.getBoundingClientRect();
      applyIdeSplit((event.clientY - rect.top) / rect.height);
    });
    ideSplitter.addEventListener('pointerup', function () {
      ideDragging = false;
      ideSplitter.classList.remove('is-dragging');
    });
  }
  if (darkQuery.addEventListener) darkQuery.addEventListener('change', syncMonacoTheme);
  else if (darkQuery.addListener) darkQuery.addListener(syncMonacoTheme);

  var items = $$('.drawer-problem');
  (function applyAssignmentEditorPreview() {
    var params = new URLSearchParams(window.location.search);
    if (params.get('assignment_preview') !== '1') return;
    var codes = (params.get('preview_codes') || '').split(',').map(function (c) {
      return c.trim();
    }).filter(Boolean);
    var names = {};
    try {
      var stored = JSON.parse(localStorage.getItem('dkuoj_assignment_preview') || 'null');
      if (stored && Array.isArray(stored.codes) && stored.codes.length) {
        if (!codes.length) codes = stored.codes.map(String);
        names = stored.names || {};
      }
    } catch (err) { names = {}; }
    if (!codes.length) return;
    app.dataset.mode = 'assignment';
    var order = {};
    codes.forEach(function (code, index) { order[code] = index + 1; });
    var list = $('#drawer-problems');
    items.forEach(function (item) {
      var code = item.dataset.code || '';
      if (!Object.prototype.hasOwnProperty.call(order, code)) {
        if (item.parentNode) item.parentNode.removeChild(item);
        return;
      }
      var nameEl = item.querySelector('.drawer-problem-name');
      if (nameEl) {
        var label = names[code] || (nameEl.textContent || '').replace(/^\d+\.\s*/, '');
        nameEl.textContent = order[code] + '. ' + label;
      }
      try {
        var url = new URL(item.getAttribute('href'), window.location.origin);
        url.searchParams.set('assignment_preview', '1');
        url.searchParams.set('preview_codes', codes.join(','));
        item.setAttribute('href', url.pathname + url.search);
      } catch (err) {}
    });
    items = $$('.drawer-problem');
    if (list) {
      items.sort(function (a, b) {
        return (order[a.dataset.code] || 0) - (order[b.dataset.code] || 0);
      });
      items.forEach(function (item) { list.appendChild(item); });
    }
    var practiceToolbar = document.querySelector('.ws-drawer-toolbar');
    if (practiceToolbar) practiceToolbar.hidden = true;
    var pager = document.querySelector('.ws-drawer-pagination');
    if (pager) pager.hidden = true;
    if (!$('#drawer-filter-status') && drawer && list) {
      var wrap = document.createElement('div');
      wrap.className = 'ws-drawer-summary';
      var select = document.createElement('select');
      select.id = 'drawer-filter-status';
      [['', gettext('All')], ['answered', gettext('Answered')], ['unanswered', gettext('Unanswered')]].forEach(function (pair) {
        var opt = document.createElement('option');
        opt.value = pair[0];
        opt.textContent = pair[1];
        select.appendChild(opt);
      });
      wrap.appendChild(select);
      drawer.insertBefore(wrap, list);
    }
  })();
  // Practice drawer paginates to fill the visible list; assignment lists are short and have no pager UI.
  var useDrawerPaging = !!$('#drawer-page-info') && (app.dataset.mode || '') === 'practice';
  function drawerPageSize() {
    if (!useDrawerPaging) return Math.max(items.length, 1);
    var list = $('#drawer-problems');
    var available = list ? list.clientHeight : 0;
    if (available < 48) available = Math.max(160, (window.innerHeight || 800) - 280);
    var row = 0;
    var sample = items[0];
    if (sample) {
      var wasHidden = sample.hidden;
      sample.hidden = false;
      row = sample.getBoundingClientRect().height;
      sample.hidden = wasHidden;
    }
    if (!(row > 20)) row = 72;
    return Math.max(1, Math.floor(available / row));
  }
  function multiValues(id) {
    var el = document.getElementById(id);
    if (!el) return [];
    var $jq = window.jQuery;
    if ($jq && $jq.fn.select2 && $jq(el).data('select2')) {
      var selected = $jq(el).val();
      if (!selected) return [];
      return (Array.isArray(selected) ? selected : [selected]).map(String);
    }
    return Array.prototype.map.call(el.selectedOptions || [], function (opt) {
      return String(opt.value);
    });
  }
  function tokenHit(haystack, needles) {
    if (!needles.length) return true;
    var padded = ' ' + (haystack || '') + ' ';
    return needles.some(function (needle) {
      return padded.indexOf(' ' + needle + ' ') !== -1;
    });
  }
  function matchesCourse(item, courses) {
    if (!courses.length) return true;
    return tokenHit(item.dataset.orgs, courses);
  }
  function matchesFormat(item, formats) {
    if (!formats.length) return true;
    var type = item.dataset.type || '';
    var format = item.dataset.format || type;
    return formats.some(function (code) {
      return code === format || code === type;
    });
  }
  function visibleItems() {
    var q = (($('#drawer-q') || {}).value || '').toLowerCase();
    var formats = multiValues('fp-type');
    var diffs = multiValues('fp-diff');
    var courses = multiValues('fp-course');
    var authors = multiValues('fp-author');
    var tags = multiValues('fp-tags');
    var status = ($('#fp-status') || $('#drawer-filter-status') || {}).value || '';
    var min = Number(($('#fp-ac-min') || {}).value || 0);
    var maxRaw = ($('#fp-ac-max') || {}).value;
    var max = maxRaw === '' || maxRaw === undefined ? 100 : Number(maxRaw);
    return items.filter(function (item) {
      var acRaw = Number(item.dataset.ac || 0);
      var ac = acRaw * (acRaw <= 1 ? 100 : 1);
      return (!q || item.textContent.toLowerCase().indexOf(q) !== -1)
        && matchesFormat(item, formats)
        && (!diffs.length || diffs.indexOf(item.dataset.diff || '') !== -1)
        && matchesCourse(item, courses)
        && tokenHit(item.dataset.authorIds, authors)
        && tokenHit(item.dataset.kp, tags)
        && (!status
          || item.dataset.status === status
          || (status === 'answered' && item.dataset.status !== 'unsolved')
          || (status === 'unanswered' && item.dataset.status === 'unsolved')
          || (status === 'unsolved' && item.dataset.status !== 'solved'))
        && ac >= min && ac <= max;
    });
  }
  var renderingDrawer = false;
  function interpolate(message, values) {
    return Object.keys(values).reduce(function (text, key) {
      return text.split('%(' + key + ')s').join(values[key]);
    }, message);
  }
  function renderDrawer() {
    if (renderingDrawer) return;
    renderingDrawer = true;
    try {
    var visible = visibleItems();
    var size = drawerPageSize();
    var pages = Math.max(1, Math.ceil(visible.length / size));
    page = Math.min(page, pages - 1);
    items.forEach(function (item) {
      var index = visible.indexOf(item);
      item.hidden = index < 0 || Math.floor(index / size) !== page;
    });
    var info = $('#drawer-page-info');
    if (info) {
      info.textContent = interpolate(
        gettext('Page %(page)s / %(pages)s • %(count)s problems'),
        {page: page + 1, pages: pages, count: visible.length},
      );
    }
    var progressLabel = $('#toolbar-progress-label');
    var solvedCount = visible.filter(function (x) { return x.dataset.status === 'solved'; }).length;
    var answeredCount = items.filter(function (x) { return x.dataset.status !== 'unsolved'; }).length;
    var isPractice = !!(app.dataset.mode === 'practice');
    var done = isPractice ? solvedCount : answeredCount;
    var total = isPractice ? visible.length : items.length;
    if (progressLabel) progressLabel.textContent = done + ' / ' + total;
    var bar = $('#toolbar-progress-bar');
    if (bar) {
      bar.max = Math.max(total, 1);
      bar.value = done;
    }
    var prev = $('#drawer-page-prev');
    var next = $('#drawer-page-next');
    if (prev) prev.disabled = page === 0;
    if (next) next.disabled = page + 1 >= pages;
    } finally {
      renderingDrawer = false;
    }
  }
  ['drawer-q', 'fp-type', 'fp-diff', 'fp-course', 'fp-author', 'fp-tags', 'fp-status', 'drawer-filter-status', 'fp-ac-min', 'fp-ac-max'].forEach(function (id) {
    var element = document.getElementById(id);
    if (!element) return;
    element.addEventListener('input', function () { page = 0; renderDrawer(); });
    element.addEventListener('change', function () { page = 0; renderDrawer(); });
  });
  (function initDrawerSelect2() {
    var $jq = window.jQuery;
    if (!$jq || !$jq.fn.select2) return;
    var theme = app.dataset.select2Theme || 'dmoj';
    var multiOpts = {theme: theme, multiple: true, width: '100%'};
    ['fp-type', 'fp-diff', 'fp-course', 'fp-tags'].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      $jq(el).select2($jq.extend({}, multiOpts, {
        placeholder: el.getAttribute('data-placeholder') || '',
      })).on('change', function () { page = 0; renderDrawer(); });
    });
    var authorEl = document.getElementById('fp-author');
    if (authorEl) {
      var authorOpts = $jq.extend({}, multiOpts, {
        placeholder: authorEl.getAttribute('data-placeholder') || '',
      });
      var ajaxUrl = authorEl.getAttribute('data-ajax-url');
      if (ajaxUrl) {
        authorOpts.ajax = {
          url: ajaxUrl,
          dataType: 'json',
          delay: 250,
          data: function (params) { return { term: params.term || '' }; },
          processResults: function (data) {
            return { results: data.results || [], pagination: { more: !!data.more } };
          },
        };
      }
      $jq(authorEl).select2(authorOpts).on('change', function () { page = 0; renderDrawer(); });
    }
    var statusEl = document.getElementById('fp-status');
    if (statusEl) {
      $jq(statusEl).select2({
        theme: theme,
        minimumResultsForSearch: Infinity,
        width: '100%',
      }).on('change', function () { page = 0; renderDrawer(); });
    }
  })();
  $('#drawer-page-prev') && $('#drawer-page-prev').addEventListener('click', function () { page--; renderDrawer(); });
  $('#drawer-page-next') && $('#drawer-page-next').addEventListener('click', function () { page++; renderDrawer(); });
  renderDrawer();
  window.addEventListener('resize', function () { renderDrawer(); });
  var drawerList = $('#drawer-problems');
  if (drawerList && window.ResizeObserver) {
    new window.ResizeObserver(function () { renderDrawer(); }).observe(drawerList);
  }
  var current = $('.drawer-problem.active');
  var index = items.indexOf(current);
  var prevBtn = $('#btn-prev');
  var nextBtn = $('#btn-next');
  function setProblemNav(button, disabled, enabledTitle, disabledTitle) {
    if (!button) return;
    button.disabled = disabled;
    button.setAttribute('aria-disabled', String(disabled));
    button.title = disabled ? disabledTitle : enabledTitle;
    button.style.opacity = disabled ? '0.35' : '';
    button.style.cursor = disabled ? 'not-allowed' : '';
  }
  var atFirst = items.length === 0 || index <= 0;
  var atLast = items.length === 0 || index < 0 || index >= items.length - 1;
  setProblemNav(prevBtn, atFirst, gettext('Previous problem'), gettext('Already the first problem'));
  setProblemNav(nextBtn, atLast, gettext('Next problem'), gettext('Already the last problem'));
  if (prevBtn) prevBtn.addEventListener('click', function () { if (index > 0) location.assign(items[index - 1].href); });
  if (nextBtn) nextBtn.addEventListener('click', function () { if (index >= 0 && index + 1 < items.length) location.assign(items[index + 1].href); });

  // Participation countdown: same seconds as #contest-time-remaining (time_remaining).
  // Digits use count_down; this tick turns the clock red at 10%, blocks Submit,
  // and sends the student to the assignment page when the window hits 0.
  var timerExpired = false;
  function setSubmitBlocked(blocked) {
    timerExpired = !!blocked;
    var button = $('#btn-submit');
    if (button) button.disabled = !!blocked || button.dataset.forceDisabled === '1';
  }
  function submitNoticeKey(kind) {
    return 'dkuoj-ws-submit-notice-' + kind + '-' + (app.dataset.contextId || '') + '-' + (app.dataset.participationId || '');
  }
  function noticeSeen(kind) {
    try { return !!sessionStorage.getItem(submitNoticeKey(kind)); } catch (err) { return false; }
  }
  function markNoticeSeen(kind) {
    try { sessionStorage.setItem(submitNoticeKey(kind), '1'); } catch (err) {}
  }
  function timeWarnBody(remainingSecs) {
    var secs = Math.max(0, Math.floor(Number(remainingSecs) || 0));
    var minutes = Math.floor(secs / 60);
    var seconds = secs % 60;
    return interpolate(
      gettext('%(min)s min %(sec)s sec remaining. Please make sure you have manually submitted your answers before it ends.'),
      {min: String(minutes), sec: String(seconds)},
    );
  }
  function joinPendingKey() {
    return 'dkuoj-ws-join-pending-' + (app.dataset.contextId || '');
  }
  function consumeJoinPending() {
    try {
      if (sessionStorage.getItem(joinPendingKey()) === '1') {
        sessionStorage.removeItem(joinPendingKey());
        return true;
      }
    } catch (err) {}
    return false;
  }
  function showManualSubmitNotice(kind, remainingSecs) {
    if (app.dataset.manualSubmitNotice !== '1') return false;
    if (kind === 'join') {
      if (!consumeJoinPending() && noticeSeen(kind)) return false;
    } else if (noticeSeen(kind)) {
      return false;
    }
    if (typeof window.dkuojShowAssignmentNotice !== 'function') return false;
    var opts = { kind: kind };
    if (kind === 'join') {
      opts.title = window.DKUOJ_PREEXAM_TITLE || gettext('Pre-exam reminder');
      opts.warn = window.DKUOJ_MANUAL_SUBMIT_WARN;
    } else if (kind === '10pct') {
      opts.title = window.DKUOJ_TIME_WARN_TITLE || gettext('A kind reminder');
      opts.warn = timeWarnBody(remainingSecs);
    }
    var shown = window.dkuojShowAssignmentNotice(opts);
    if (shown) markNoticeSeen(kind);
    return shown;
  }
  var joinNoticeShownThisPage = false;
  function maybeTenPercentNotice(remaining, warnAt) {
    if (app.dataset.manualSubmitNotice !== '1') return;
    if (!(warnAt > 0 && remaining <= warnAt && remaining > 0)) return;
    if (noticeSeen('10pct')) return;
    if (joinNoticeShownThisPage) {
      markNoticeSeen('10pct');
      return;
    }
    showManualSubmitNotice('10pct', remaining);
  }
  var timerWindowSecs = Number(app.dataset.timerWindowSeconds || 0);
  var timerWarnAt = timerWindowSecs > 0 ? Math.max(1, Math.floor(timerWindowSecs * 0.1)) : 0;
  var timerInitial = Number(app.dataset.timerSeconds || 0);
  var timerStartedAt = Date.now();
  function timerRemaining() {
    return Math.max(0, timerInitial - Math.round((Date.now() - timerStartedAt) / 1000));
  }
  joinNoticeShownThisPage = showManualSubmitNotice('join');
  maybeTenPercentNotice(timerRemaining(), timerWarnAt);
  joinNoticeShownThisPage = false;

  (function initDurationTimer() {
    if (app.dataset.showTimer !== '1') return;
    var timer = $('#ws-timer');
    if (!timer) return;
    var label = timer.querySelector('.time-remaining');
    var warnAt = timerWarnAt;
    var returnUrl = app.dataset.returnUrl || '';
    function expire() {
      timer.classList.add('expired');
      timer.classList.remove('warn');
      if (label) label.textContent = gettext('Time expired');
      setSubmitBlocked(true);
      flushServerDraft();
      if (returnUrl) window.location.assign(returnUrl);
    }
    if (timerInitial <= 0) {
      expire();
      return;
    }
    if (label && window.jQuery && typeof count_down === 'function') {
      count_down(window.jQuery(label), returnUrl || undefined);
    }
    var tick = function () {
      var remaining = timerRemaining();
      timer.classList.toggle('warn', warnAt > 0 && remaining <= warnAt && remaining > 0);
      maybeTenPercentNotice(remaining, warnAt);
      if (remaining <= 0) {
        window.clearInterval(handle);
        expire();
      }
    };
    tick();
    var handle = window.setInterval(tick, 1000);
  })();


  function renderInlineBlanks(root) {
    var desc = (root || document).querySelector('.content-description');
    if (!desc) return;
    var re = /\{\{\s*blank\s*\}\}/g;
    var walker = document.createTreeWalker(desc, NodeFilter.SHOW_TEXT, null);
    var nodes = [], node;
    while ((node = walker.nextNode())) {
      if (node.nodeValue && node.nodeValue.match(re)) nodes.push(node);
    }
    if (!nodes.length) return;
    var idx = 0;
    nodes.forEach(function (textNode) {
      var frag = document.createDocumentFragment();
      var parts = textNode.nodeValue.split(re);
      parts.forEach(function (part, i) {
        if (part) frag.appendChild(document.createTextNode(part));
        if (i < parts.length - 1) {
          idx++;
          var ph = document.createElement('span');
          ph.className = 'inline-blank-ph';
          ph.textContent = '(' + idx + ')';
          ph.setAttribute('title', 'Blank ' + idx);
          frag.appendChild(ph);
        }
      });
      textNode.parentNode.replaceChild(frag, textNode);
    });
  }
  renderInlineBlanks(app);

  function answerDraftKey() {
    return draftContextPrefix() + '-answer';
  }
  function fillBlankDraftKey() {
    return draftContextPrefix() + '-fillblank';
  }
  function mcqDraftKey() {
    return draftContextPrefix() + '-mcq';
  }

  if (window.RichEditor && document.querySelector('[data-rich-mount="workspace-answer"]')) {
    RichEditor.configure({
      fileUploadUrl: app.dataset.fileUploadUrl || '',
      fileAccept: app.dataset.fileAccept || 'image/*',
      csrfToken: ($('[name=csrfmiddlewaretoken]') || {}).value || '',
      messages: {
        fileUploadFailed: gettext('File upload failed.'),
        fileUploadFailedAuth: gettext('File upload failed. Make sure you are signed in.'),
      },
      onChange: function () {
        var md = RichEditor.getMarkdown('workspace-answer');
        var ta = $('#id_answer');
        if (ta) ta.value = md;
        if (!draftsEnabled()) return;
        try { localStorage.setItem(answerDraftKey(), md); } catch (err) { /* ignore */ }
        setLocalDraftStatus();
        queueServerDraft(JSON.stringify({ text: md }));
      },
    });
    RichEditor.mount(app);
    var initialAnswer = ($('#id_answer') || {}).value || '';
    // The server draft wins: it is the copy the deadline sweep will submit, and the only one that
    // is there after switching devices.
    var remoteAnswer = serverDraftAnswer();
    var savedAnswer = remoteAnswer && typeof remoteAnswer.text === 'string' ? remoteAnswer.text : null;
    if (savedAnswer === null && draftsEnabled()) {
      try { savedAnswer = localStorage.getItem(answerDraftKey()); } catch (err) { savedAnswer = null; }
    }
    if (savedAnswer) initialAnswer = savedAnswer;
    if (initialAnswer) RichEditor.setMarkdown('workspace-answer', initialAnswer);
  }

  // Fill-in-blank: disable browser autofill always; persist drafts only in assignment/exam.
  (function initFillBlankDraft() {
    var fields = $$('input[name^="fb_"], textarea[name^="fb_"], input[name^="blank_"], textarea[name^="blank_"]');
    if (!fields.length) return;
    var form = fields[0].form;
    if (form) form.setAttribute('autocomplete', 'off');
    fields.forEach(function (field) {
      field.setAttribute('autocomplete', 'new-password');
      field.setAttribute('autocapitalize', 'off');
      field.setAttribute('autocorrect', 'off');
      field.setAttribute('spellcheck', 'false');
      field.setAttribute('data-lpignore', 'true');
      field.setAttribute('data-1p-ignore', 'true');
      if (!field.readOnly) {
        field.setAttribute('readonly', 'readonly');
        field.addEventListener('focus', function () { field.removeAttribute('readonly'); }, { once: true });
      }
    });
    if (!draftsEnabled()) return;
    // The submitted answer is a list in blank order, and the field name carries that order, so
    // sort by it rather than trusting DOM order (blanks may be rendered inline in the statement).
    var ordered = fields.slice().sort(function (a, b) {
      return (parseInt(a.name.replace(/^\D+/, ''), 10) || 0) - (parseInt(b.name.replace(/^\D+/, ''), 10) || 0);
    });
    var remoteAnswers = (serverDraftAnswer() || {}).answers;
    if (Array.isArray(remoteAnswers)) {
      ordered.forEach(function (field, index) {
        if (remoteAnswers[index] != null) field.value = remoteAnswers[index];
      });
    } else {
      var saved = null;
      try { saved = JSON.parse(localStorage.getItem(fillBlankDraftKey()) || 'null'); } catch (err) { saved = null; }
      if (saved && typeof saved === 'object') {
        fields.forEach(function (field) {
          if (!field.value && saved[field.name] != null) field.value = saved[field.name];
        });
      }
    }
    function persist() {
      var payload = {};
      fields.forEach(function (field) { payload[field.name] = field.value; });
      try { localStorage.setItem(fillBlankDraftKey(), JSON.stringify(payload)); } catch (err) { /* ignore */ }
      setLocalDraftStatus();
      queueServerDraft(JSON.stringify({
        answers: ordered.map(function (field) { return field.value; }),
      }));
    }
    fields.forEach(function (field) {
      field.addEventListener('input', persist);
      field.addEventListener('change', persist);
    });
  })();

  // MCQ: radio / checkbox options — assignment/exam drafts only.
  (function initMcqDraft() {
    var inputs = $$('input[name="options"]');
    if (!inputs.length) return;
    var form = inputs[0].form;
    if (form) form.setAttribute('autocomplete', 'off');
    if (!draftsEnabled()) return;
    var remoteSelected = (serverDraftAnswer() || {}).selected;
    var saved = Array.isArray(remoteSelected) ? remoteSelected : null;
    if (saved === null) {
      try { saved = JSON.parse(localStorage.getItem(mcqDraftKey()) || 'null'); } catch (err) { saved = null; }
    }
    if (Array.isArray(saved) && saved.length) {
      var selected = {};
      saved.forEach(function (id) { selected[String(id)] = true; });
      var anyChecked = inputs.some(function (input) { return input.checked; });
      if (!anyChecked) {
        inputs.forEach(function (input) {
          if (selected[input.value]) input.checked = true;
        });
      }
    }
    function persist() {
      var values = inputs.filter(function (input) { return input.checked; }).map(function (input) { return input.value; });
      try { localStorage.setItem(mcqDraftKey(), JSON.stringify(values)); } catch (err) { /* ignore */ }
      setLocalDraftStatus();
      // A click is a whole answer, not a keystroke: send it right away so the last choice before
      // the deadline is never sitting in a debounce timer.
      saveServerDraftNow(JSON.stringify({ selected: values }));
    }
    inputs.forEach(function (input) {
      input.addEventListener('change', persist);
    });
  })();

  function syncAnswerFields() {
    if (window.RichEditor && document.querySelector('.rich-editor[data-editor="workspace-answer"]')) {
      var answerField = $('#id_answer');
      if (answerField) answerField.value = RichEditor.getMarkdown('workspace-answer') || '';
    }
    if (source) {
      source.value = sourceValue();
      saveDraft();
    }
  }
  function isCodeProblem() {
    return app.dataset.isCode === '1';
  }
  function isFileProblem() {
    return app.dataset.isFile === '1';
  }
  function isMcqProblem() {
    return app.dataset.isMcq === '1';
  }
  function formatScorePart(n) {
    if (n == null || n === '') return '-';
    var num = Number(n);
    if (!isFinite(num)) return String(n);
    return String(parseFloat(num.toFixed(3)));
  }
  function resultClassFromStatus(status) {
    var code = status.result || status.status || '';
    if (code === 'AC' && status.case_points != null && status.case_total != null
        && Number(status.case_total) > 0
        && Number(status.case_points) < Number(status.case_total)) {
      return '_AC';
    }
    return code || 'QU';
  }
  function appendResultCode(parent, code, className) {
    var span = document.createElement('span');
    span.className = 'ws-result-code case-' + (className || code || 'QU');
    span.textContent = code || '?';
    parent.appendChild(span);
  }
  function appendScoreLine(parent, status) {
    if (!parent || !status || status.points == null) return;
    var score = document.createElement('p');
    var scoreLabel = document.createElement('b');
    scoreLabel.textContent = gettext('Score') + ':';
    score.appendChild(scoreLabel);
    score.appendChild(document.createTextNode(
      ' ' + formatScorePart(status.points) + ' / ' + formatScorePart(status.points_total)
    ));
    parent.appendChild(score);
  }
  function appendTeacherFeedback(parent, status) {
    if (!parent || !status || status.results_hidden) return;
    if (status.released) {
      var tag = document.createElement('div');
      tag.className = 'ws-released-tag';
      tag.textContent = gettext('Released');
      parent.appendChild(tag);
    }
    if (status.review_note_html) {
      var note = document.createElement('div');
      note.className = 'ws-review-note';
      var noteLabel = document.createElement('div');
      noteLabel.className = 'ws-review-note-label';
      noteLabel.textContent = gettext('Teacher feedback');
      var noteBody = document.createElement('div');
      noteBody.className = 'ws-review-note-body';
      noteBody.innerHTML = status.review_note_html; // bleach-sanitized server-side
      note.appendChild(noteLabel);
      note.appendChild(noteBody);
      parent.appendChild(note);
    }
  }
  function showReleasedOrTeacherScore(status) {
    return !!(status && (status.released || status.show_ai_scores));
  }
  function renderFileUploadStatus(status) {
    ensureResultStructure();
    var main = $('#workspace-result-main');
    if (!main) return;
    main.textContent = '';
    var wrap = document.createElement('div');
    wrap.className = 'ws-result-summary';
    var heading = document.createElement('h3');
    heading.textContent = gettext('File submitted');
    wrap.appendChild(heading);
    var showScore = !status.results_hidden && status.points != null && (status.released || status.is_graded);
    if (status.results_hidden) {
      var hiddenMsg = document.createElement('p');
      hiddenMsg.textContent = gettext('Your submission has been received. Results for this problem are hidden until the contest is finalized.');
      wrap.appendChild(hiddenMsg);
      main.appendChild(wrap);
    } else if (showScore) {
      appendScoreLine(wrap, status);
      main.appendChild(wrap);
      appendTeacherFeedback(main, status);
    } else if (status.is_graded === false || status.is_graded == null) {
      var waiting = document.createElement('p');
      waiting.className = 'file-awaiting-grading';
      waiting.textContent = gettext('Awaiting grading');
      wrap.appendChild(waiting);
      main.appendChild(wrap);
    } else {
      appendScoreLine(wrap, status);
      main.appendChild(wrap);
      appendTeacherFeedback(main, status);
    }
    updateFileUploadResult(status.file_upload);
  }
  function renderFormalStatus(status) {
    ensureResultStructure();
    var main = $('#workspace-result-main');
    if (!main) return;
    if (isFileProblem()) {
      renderFileUploadStatus(status);
      return;
    }
    main.textContent = '';
    var wrap = document.createElement('div');
    wrap.className = 'ws-result-summary';
    if (status.results_hidden) {
      var hiddenHeading = document.createElement('h3');
      hiddenHeading.textContent = gettext('Results hidden');
      var hiddenMsg = document.createElement('p');
      hiddenMsg.textContent = gettext('Your submission has been received. Results for this problem are hidden until the contest is finalized.');
      wrap.appendChild(hiddenHeading);
      wrap.appendChild(hiddenMsg);
      main.appendChild(wrap);
      updateSubmittedSource(status.source, status.answer_display);
      updateFileUploadResult(status.file_upload);
      return;
    }
    if (awaitingManualAiGrade(status)) {
      var savedHeading = document.createElement('h3');
      savedHeading.textContent = gettext('Answer saved');
      var savedMsg = document.createElement('p');
      savedMsg.textContent = gettext(
        'Your answer has been saved. Press the button below to run AI Feedback.'
      );
      wrap.appendChild(savedHeading);
      wrap.appendChild(savedMsg);
      ensureAiGradeButton(wrap, status);
      if (showReleasedOrTeacherScore(status)) appendScoreLine(wrap, status);
      main.appendChild(wrap);
      appendTeacherFeedback(main, status);
      updateSubmittedSource(status.source, status.answer_display);
      updateFileUploadResult(status.file_upload);
      updateAiGradeSection(status);
      return;
    }
    if (problemAiGradingDisabled(status)) {
      var offHeading = document.createElement('h3');
      offHeading.textContent = gettext('Answer saved');
      var offMsg = document.createElement('p');
      offMsg.textContent = gettext(
        'Your answer has been saved.'
      );
      wrap.appendChild(offHeading);
      wrap.appendChild(offMsg);
      if (showReleasedOrTeacherScore(status)) appendScoreLine(wrap, status);
      main.appendChild(wrap);
      appendTeacherFeedback(main, status);
      updateSubmittedSource(status.source, status.answer_display);
      updateFileUploadResult(status.file_upload);
      updateAiGradeSection(status);
      return;
    }
    var heading = document.createElement('h3');
    heading.textContent = isCodeProblem() ? gettext('Execution results') : gettext('Judging results');
    var line = document.createElement('p');
    var label = document.createElement('b');
    label.textContent = gettext('Result') + ': ';
    line.appendChild(label);
    appendResultCode(line, status.result || status.status, resultClassFromStatus(status));
    if (isCodeProblem() && status.points != null) {
      line.appendChild(document.createTextNode(' · ' + status.points));
    }
    wrap.appendChild(heading);
    wrap.appendChild(line);
    if (isCodeProblem()) {
      var detail = document.createElement('p');
      detail.textContent = gettext('Resources') + ': ' + (status.time || 0) + 's, ' + (status.memory || 0) + 'KB';
      wrap.appendChild(detail);
    } else if (isMcqProblem() || showReleasedOrTeacherScore(status)) {
      appendScoreLine(wrap, status);
    }
    main.appendChild(wrap);
    appendTeacherFeedback(main, status);
    updateSubmittedSource(status.source, status.answer_display);
    updateFileUploadResult(status.file_upload);
    updateAiGradeSection(status);
  }
  function ensureAiGradeButton(container, status) {
    if (!container || !status || !status.can_trigger_ai_grade || !status.ai_grade_url) return null;
    var btn = $('#ai-grade-btn');
    if (!btn) {
      var row = document.createElement('p');
      row.style.marginTop = '0.75rem';
      btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'tool-btn ai-grade-btn';
      btn.id = 'ai-grade-btn';
      btn.textContent = gettext('View AI Feedback');
      row.appendChild(btn);
      container.appendChild(row);
    } else {
      var row = btn.closest('p') || btn.parentNode;
      if (row && row !== container && container.appendChild) {
        if (row.tagName !== 'P') {
          var holder = document.createElement('p');
          holder.style.marginTop = '0.75rem';
          holder.appendChild(btn);
          container.appendChild(holder);
        } else {
          container.appendChild(row);
        }
      }
    }
    btn.hidden = false;
    btn.disabled = false;
    btn.textContent = gettext('View AI Feedback');
    app.dataset.aiGradeUrl = status.ai_grade_url;
    return btn;
  }
  function updateAiGradeSection(status) {
    var show = status && status.can_trigger_ai_grade && status.ai_grade_url;
    if (show) {
      ensureAiGradeButton($('#workspace-result-main .ws-result-summary') || $('#workspace-result-main'), status);
    }
    var section = $('#workspace-result-ai-grade');
    if (section) section.hidden = true;
    if (!show) {
      var btn = $('#ai-grade-btn');
      if (btn) btn.hidden = true;
      return;
    }
    app.dataset.aiGradeUrl = status.ai_grade_url;
    var help = $('#ai-grade-help');
    if (help && status.ai_grade_remaining != null && Number(status.ai_grade_remaining) > 0) {
      help.textContent = gettext(
        'AI Feedback uses your latest submission for this problem. Press the button to score this version.'
      ) + ' (' + status.ai_grade_remaining + ' ' + gettext('uses remaining') + ')';
    }
    var result = $('#ai-grade-result');
    if (result) result.hidden = true;
    var btn = $('#ai-grade-btn');
    if (btn) {
      btn.disabled = false;
      btn.textContent = gettext('View AI Feedback');
    }
  }
  function formatAnswerDisplay(rows) {
    if (!rows || !rows.length) return '';
    return rows.map(function (item) {
      var value = item && item.value != null ? String(item.value) : '';
      if (item && item.label) return item.label + ': ' + value;
      return value;
    }).filter(Boolean).join('\n');
  }
  function ensureResultStructure() {
    var box = $('#workspace-submit-status');
    if (!box) return null;
    if (!$('#workspace-result-main')) {
      var main = document.createElement('div');
      main.id = 'workspace-result-main';
      box.insertBefore(main, box.firstChild);
    }
    if (!$('#workspace-result-ai-grade')) {
      var aiGrade = document.createElement('div');
      aiGrade.id = 'workspace-result-ai-grade';
      aiGrade.className = 'ai-grade-section';
      aiGrade.hidden = true;
      var aiResult = document.createElement('div');
      aiResult.id = 'ai-grade-result';
      aiResult.className = 'ai-hint-result';
      aiResult.hidden = true;
      aiGrade.appendChild(aiResult);
      box.appendChild(aiGrade);
    }
    if (!$('#workspace-result-file')) {
      var fileBlock = document.createElement('div');
      fileBlock.id = 'workspace-result-file';
      fileBlock.className = 'ws-result-file';
      fileBlock.hidden = true;
      box.appendChild(fileBlock);
    }
    if (!$('#workspace-result-source')) {
      var sourceBlock = document.createElement('div');
      sourceBlock.id = 'workspace-result-source';
      sourceBlock.className = 'source-block';
      sourceBlock.hidden = true;
      if (isCodeProblem()) {
        var actions = document.createElement('div');
        actions.className = 'result-actions';
        var loadBtn = document.createElement('button');
        loadBtn.className = 'tool-btn';
        loadBtn.id = 'btn-load-editor';
        loadBtn.type = 'button';
        loadBtn.textContent = gettext('Load into editor');
        actions.appendChild(loadBtn);
        sourceBlock.appendChild(actions);
        bindLoadEditorButton(loadBtn);
      }
      var details = document.createElement('details');
      details.className = 'result-source-details';
      var summary = document.createElement('summary');
      summary.id = 'submitted-source-summary';
      summary.textContent = gettext('Submitted source');
      var sourceEl = document.createElement(isCodeProblem() ? 'pre' : 'div');
      sourceEl.className = isCodeProblem() ? 'result-source' : 'result-source result-source-md content-description';
      sourceEl.id = 'submitted-source';
      details.appendChild(summary);
      details.appendChild(sourceEl);
      sourceBlock.appendChild(details);
      box.appendChild(sourceBlock);
    }
    if (!$('#workspace-result-comments')) {
      var comments = document.createElement('div');
      comments.id = 'workspace-result-comments';
      comments.className = 'ws-result-comments';
      comments.hidden = true;
      box.appendChild(comments);
    }
    return box;
  }
  function typesetSubmittedSource(el) {
    // KaTeX from RichEditor.mdToHtml is already painted. MathJax re-typeset
    // of that tree can hide or double-render the formula.
    if (!el || el.querySelector('.katex')) return;
    if (window.MathJax && typeof MathJax.typesetPromise === 'function') {
      MathJax.typesetPromise([el]).catch(function () {});
    }
  }
  function bindSubmittedSourceDetails(block) {
    var details = block && block.querySelector('details.result-source-details');
    if (!details || details.dataset.mathBound === '1') return;
    details.dataset.mathBound = '1';
    details.addEventListener('toggle', function () {
      if (details.open) typesetSubmittedSource($('#submitted-source'));
    });
  }
  function submittedSourceHost(wantHtml) {
    var el = $('#submitted-source');
    if (!el) return null;
    if (wantHtml && el.tagName === 'PRE') {
      var div = document.createElement('div');
      div.id = 'submitted-source';
      div.className = 'result-source result-source-md content-description';
      el.parentNode.replaceChild(div, el);
      return div;
    }
    return el;
  }
  function renderedAnswerHtml(answerDisplay) {
    var rows = answerDisplay || [];
    if (isCodeProblem() || isMcqProblem() || isFileProblem()) return '';
    // Fill-blank rows are labeled plain text. Joining them and running markdown
    // collapses "Blank 1\nBlank 2" into one paragraph.
    if (rows.some(function (item) { return item && item.label; })) return '';
    var md = formatAnswerDisplay(rows);
    // Prefer the same client renderer as the editor. Server HTML uses MathJax
    // delimiters that sit inside a closed <details> and often never paint.
    if (md && window.RichEditor && typeof RichEditor.mdToHtml === 'function') {
      try {
        var client = RichEditor.mdToHtml(md);
        if (client) return client;
      } catch (e) { /* fall through to server HTML */ }
    }
    var htmlParts = rows.map(function (item) {
      return item && item.html ? String(item.html) : '';
    }).filter(Boolean);
    if (htmlParts.length) return htmlParts.join('');
    return '';
  }
  function usesAnswerDisplayRows(answerDisplay, html) {
    if (html || !answerDisplay || !answerDisplay.length) return false;
    return answerDisplay.length > 1 || answerDisplay.some(function (item) {
      return item && item.label;
    });
  }
  function renderAnswerDisplayRows(el, answerDisplay) {
    el.textContent = '';
    el.classList.remove('result-source-md');
    (answerDisplay || []).forEach(function (item) {
      var row = document.createElement('div');
      row.className = 'result-source-row';
      var value = item && item.value != null ? String(item.value) : '';
      row.textContent = (item && item.label) ? (item.label + ': ' + value) : value;
      el.appendChild(row);
    });
  }
  function parseJsonResponse(response) {
    return response.text().then(function (text) {
      var json = null;
      try { json = text ? JSON.parse(text) : {}; } catch (parseErr) {
        throw new Error(gettext('Failed to load submission.'));
      }
      if (!response.ok) throw new Error((json && json.error) || gettext('Failed to load submission.'));
      return json;
    });
  }
  function updateSubmittedSource(sourceText, answerDisplay) {
    var block = $('#workspace-result-source');
    if (!block) return;
    var html = renderedAnswerHtml(answerDisplay);
    var readable = formatAnswerDisplay(answerDisplay);
    var display = readable || sourceText || '';
    if (!display && !html) return;
    var useRows = usesAnswerDisplayRows(answerDisplay, html);
    var el = submittedSourceHost(!!html || useRows);
    if (!el) return;
    if (html) {
      el.innerHTML = html;
      el.classList.add('result-source-md', 'content-description');
      typesetSubmittedSource(el);
    } else if (useRows) {
      renderAnswerDisplayRows(el, answerDisplay);
    } else {
      el.textContent = display;
    }
    el.dataset.rawSource = sourceText || '';
    var summary = $('#submitted-source-summary') || block.querySelector('summary');
    if (summary) summary.textContent = gettext('Submitted source');
    var details = block.querySelector('details.result-source-details');
    if (details) details.open = false;
    bindSubmittedSourceDetails(block);
    block.hidden = false;
  }
  (function hydrateSubmittedSource() {
    var node = $('#workspace-answer-display');
    if (!node || !node.textContent) return;
    var rows;
    try { rows = JSON.parse(node.textContent); } catch (e) { return; }
    if (!rows || !rows.length) return;
    var sourceEl = $('#submitted-source');
    var raw = sourceEl && sourceEl.dataset ? sourceEl.dataset.rawSource : '';
    updateSubmittedSource(raw || '', rows);
  })();
  function updateFileUploadResult(fileUpload) {
    var block = $('#workspace-result-file');
    if (!block) return;
    if (!fileUpload || !fileUpload.url) {
      block.hidden = true;
      block.textContent = '';
      return;
    }
    block.textContent = '';
    var line = document.createElement('p');
    var link = document.createElement('a');
    link.className = 'tool-btn';
    link.href = fileUpload.url;
    link.textContent = gettext('Download uploaded file');
    line.appendChild(link);
    if (fileUpload.name) {
      var name = document.createElement('span');
      name.className = 'ws-file-name';
      name.textContent = fileUpload.name;
      line.appendChild(name);
    }
    block.appendChild(line);
    block.hidden = false;
  }
  function syncCommentBody(form) {
    if (!form || !window.RichEditor) return;
    var editor = form.querySelector('.rich-editor[data-editor]');
    var ta = form.querySelector('textarea[name="body"]');
    if (!editor || !ta) return;
    var name = editor.getAttribute('data-editor');
    ta.value = RichEditor.getMarkdown(name) || '';
  }
  function mountWorkspaceComments(root) {
    if (!window.RichEditor) return;
    var scope = root || document;
    try {
      RichEditor.configure({
        fileUploadUrl: app.dataset.fileUploadUrl || '',
        fileAccept: app.dataset.fileAccept || 'image/*',
        csrfToken: csrfToken() || '',
      });
    } catch (err) { /* ignore */ }
    RichEditor.mount(scope);
    bindWorkspaceCommentForms(scope);
  }
  function bindWorkspaceCommentForms(root) {
    var scope = root || document;
    var forms = scope.querySelectorAll
      ? scope.querySelectorAll('form.comment-submit-form')
      : [];
    Array.prototype.forEach.call(forms, function (form) {
      if (form.dataset.wsCommentBound === '1') return;
      form.dataset.wsCommentBound = '1';
      form.addEventListener('submit', function () {
        syncCommentBody(form);
      });
    });
  }
  window.bindWorkspaceCommentForms = bindWorkspaceCommentForms;
  function updateResultComments(html) {
    var box = $('#workspace-result-comments');
    if (!box) return;
    if (!html) {
      box.hidden = true;
      box.innerHTML = '';
      return;
    }
    box.innerHTML = html;
    box.hidden = false;
    mountWorkspaceComments(box);
  }
  function bindLoadEditorButton(button) {
    if (!button || button.dataset.bound === '1') return;
    button.dataset.bound = '1';
    button.addEventListener('click', function () {
      var submitted = $('#submitted-source');
      if (!submitted) return;
      var raw = submitted.dataset.rawSource || submitted.textContent;
      setSource(raw);
      activateTab('desc');
      saveDraft();
    });
  }
  function registerResultToggles(root) {
    if (!(window.jQuery && typeof register_toggle === 'function')) return;
    window.jQuery(root).find('.toggle').each(function () {
      register_toggle(window.jQuery(this));
    });
  }
  function refreshResultPanel(submissionId) {
    ensureResultStructure();
    var main = $('#workspace-result-main');
    var baseUrl = app.dataset.testcasesUrl;
    if (!main || !submissionId || !baseUrl) return Promise.resolve();
    var url = baseUrl + (baseUrl.indexOf('?') >= 0 ? '&' : '?') + 'id=' + encodeURIComponent(submissionId);
    return fetch(url, { credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) throw new Error('testcases fetch failed');
        return response.text();
      })
      .then(function (html) {
        main.innerHTML = html;
        registerResultToggles(main);
      })
      .catch(function () {
        renderFormalStatus({ result: gettext('Unavailable'), status: '?', points: null, time: 0, memory: 0 });
      });
  }
  function appendTestcasesPanel(submissionId) {
    if (isFileProblem() || isMcqProblem() || !isCodeProblem()) return Promise.resolve();
    ensureResultStructure();
    var main = $('#workspace-result-main');
    var baseUrl = app.dataset.testcasesUrl;
    if (!main || !submissionId || !baseUrl) return Promise.resolve();
    var url = baseUrl + (baseUrl.indexOf('?') >= 0 ? '&' : '?') + 'id=' + encodeURIComponent(submissionId);
    return fetch(url, { credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) throw new Error('testcases fetch failed');
        return response.text();
      })
      .then(function (html) {
        if (!html || !html.trim()) return;
        var old = $('#workspace-result-testcases');
        if (old) old.remove();
        var holder = document.createElement('div');
        holder.id = 'workspace-result-testcases';
        holder.innerHTML = html;
        main.appendChild(holder);
        registerResultToggles(holder);
      })
      .catch(function () { /* keep summary */ });
  }
  var formalPollTimer = null;
  var formalEventReceiver = null;
  function stopFormalWatch() {
    if (formalPollTimer) {
      window.clearInterval(formalPollTimer);
      formalPollTimer = null;
    }
    formalEventReceiver = null;
  }
  function statusIsPending(status) {
    if (!status || isFileProblem()) return false;
    if (status.is_graded === true) return false;
    if (status.is_graded === false) return true;
    return status.status === 'QU' || status.status === 'P' || status.status === 'G';
  }
  function awaitingManualAiGrade(status) {
    // Code problems are judged by the bridge; a judge IR (Invalid Return) is a real
    // verdict, never the "awaiting AI" placeholder used by internal AI types.
    return !!(status && !isCodeProblem() && status.can_trigger_ai_grade && status.result === 'IR');
  }
  function problemAiGradingDisabled(status) {
    return !!(status && !isCodeProblem() && status.result === 'IR' &&
      status.problem_has_ai_grading === false);
  }
  function applyStatusPayload(status, button) {
    if (!status) return;
    if (status.source || (status.answer_display && status.answer_display.length)) {
      updateSubmittedSource(status.source, status.answer_display);
    }
    if (status.file_upload) updateFileUploadResult(status.file_upload);
    if (status.comments_html != null) updateResultComments(status.comments_html);
    // Code + visible results: classic testcase HTML (siblings source/comments stay).
    // Non-code: keep Judging results summary, then append AI/testcase HTML when present.
    if (status.id && isCodeProblem() && !status.results_hidden) {
      refreshResultPanel(status.id).then(function () {
        updateAiGradeSection(status);
      });
    } else if (!isFileProblem()) {
      renderFormalStatus(status);
      if (status.id && !status.results_hidden && !awaitingManualAiGrade(status)) {
        appendTestcasesPanel(status.id);
      }
    } else {
      renderFormalStatus(status);
    }
    if (!statusIsPending(status)) {
      stopFormalWatch();
      if (button && !timerExpired) button.disabled = false;
      updateAiHintsForSubmission(status.id, status.result, status.results_hidden);
      if ($('#submissions-table') && currentSubScope === 'my') loadSubScope('my');
      app.dataset.submissionGraded = '1';
    }
  }
  function loadSubmissionResult(statusUrl, submissionId) {
    if (!statusUrl) return Promise.resolve();
    $('#tab-result') && $('#tab-result').classList.remove('tab-hidden');
    activateTab('result');
    history.replaceState(null, '', location.pathname + '?submission=' + submissionId);
    app.dataset.submissionId = String(submissionId);
    app.dataset.statusUrl = statusUrl;
    app.dataset.submissionGraded = '0';
    return fetch(statusUrl, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(parseJsonResponse)
      .then(function (status) {
        applyStatusPayload(status, null);
        if (statusIsPending(status)) pollFormal(statusUrl, null);
      })
      .catch(function (error) {
        ensureResultStructure();
        var main = $('#workspace-result-main');
        if (main) {
          main.textContent = '';
          var err = document.createElement('p');
          err.textContent = error.message;
          main.appendChild(err);
        }
      });
  }
  function bindSubmissionLinks(root) {
    var scope = root || document;
    var links = scope.querySelectorAll ? scope.querySelectorAll('a.ws-submission-link') : [];
    Array.prototype.forEach.call(links, function (link) {
      if (link.dataset.bound === '1') return;
      link.dataset.bound = '1';
      link.addEventListener('click', function (event) {
        var statusUrl = link.dataset.statusUrl;
        if (!statusUrl) return;
        event.preventDefault();
        var match = (link.getAttribute('href') || '').match(/[?&]submission=(\d+)/);
        var submissionId = match ? match[1] : link.textContent.trim();
        loadSubmissionResult(statusUrl, submissionId);
      });
    });
  }
  function pollFormal(url, button) {
    if (!url || isFileProblem()) return;
    stopFormalWatch();
    var attempts = 0;
    var lastStatus = null;
    var maxAttempts = 300; // 5 minutes at 1s; covers long compile/judge queues
    formalPollTimer = window.setInterval(function () {
      attempts++;
      fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
        .then(parseJsonResponse)
        .then(function (status) {
          lastStatus = status;
          applyStatusPayload(status, button);
          if (attempts >= maxAttempts && statusIsPending(status)) {
            stopFormalWatch();
            if (button && !timerExpired) button.disabled = false;
          }
        })
        .catch(function () {
          if (lastStatus) renderFormalStatus(lastStatus);
          stopFormalWatch();
          if (button && !timerExpired) button.disabled = false;
        });
    }, 1000);
    // Optional EventReceiver when event daemon channel data is present (same as classic status page).
    if (window.EventReceiver && app.dataset.subChannel && app.dataset.eventWs) {
      try {
        formalEventReceiver = new window.EventReceiver(
          app.dataset.eventWs,
          app.dataset.eventPoll || '/channels/',
          [app.dataset.subChannel],
          Number(app.dataset.lastMsg || 0),
          function (message) {
            if (!message || !message.type) return;
            if (message.type === 'test-case' || message.type === 'grading-begin' ||
                message.type === 'processing' || message.type === 'grading-end' ||
                message.type === 'compile-error' || message.type === 'aborted' ||
                message.type === 'internal-error') {
              fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
                .then(parseJsonResponse)
                .then(function (status) { applyStatusPayload(status, button); });
            }
          },
        );
      } catch (err) { /* polling remains the fallback */ }
    }
  }

  function csrfToken() {
    return ($('[name=csrfmiddlewaretoken]') || {}).value || '';
  }
  function updateAiHintsForSubmission(submissionId, result, resultsHidden) {
    if (!app.dataset.aiHintsPossible) return;
    var section = $('#ai-hint-section');
    var waiting = $('#ai-hint-waiting');
    var btn = $('#ai-hint-btn');
    // Hidden exam results must not unlock AI hints (that would leak non-AC).
    var ready = !resultsHidden && submissionId && result && result !== 'AC' && result !== 'AB';
    if (section) section.hidden = !ready;
    if (waiting) waiting.hidden = !!ready;
    if (ready) {
      app.dataset.submissionId = String(submissionId);
      app.dataset.submissionResult = result;
      app.dataset.aiHintUrl = '/submission/' + submissionId + '/ai-hint';
      if (btn && Number(app.dataset.aiHintsRemaining || 0) > 0) btn.disabled = false;
    }
  }

  var currentSubScope = 'my';
  var subHints = {
    my: gettext('Showing your submissions for this problem.'),
    all: gettext('Showing all submissions for this problem.'),
    best: gettext('Showing best submissions for this problem.'),
  };
  function renderSubRows(rows) {
    var table = $('#submissions-table');
    if (!table) return;
    table.textContent = '';
    if (!rows || !rows.length) {
      var empty = document.createElement('div');
      empty.className = 'ws-subs-empty';
      empty.textContent = gettext('No submissions yet.');
      table.appendChild(empty);
      return;
    }
    rows.forEach(function (row) {
      if (row.html) {
        var wrap = document.createElement('div');
        wrap.innerHTML = row.html;
        while (wrap.firstChild) table.appendChild(wrap.firstChild);
      }
    });
    bindSubmissionLinks(table);
  }
  function loadSubScope(scope) {
    var url = app.dataset.subsUrl;
    if (!url) return;
    currentSubScope = scope;
    var hint = $('#sub-hint');
    if (hint) hint.textContent = subHints[scope] || subHints.my;
    fetch(url + (url.indexOf('?') >= 0 ? '&' : '?') + 'scope=' + encodeURIComponent(scope), { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (response) {
        return response.json().then(function (json) {
          if (!response.ok) throw new Error(json.error || gettext('Failed to load submissions.'));
          return json;
        });
      })
      .then(function (payload) { renderSubRows(payload.rows || []); })
      .catch(function (error) {
        var table = $('#submissions-table');
        if (!table) return;
        table.textContent = '';
        var err = document.createElement('div');
        err.className = 'ws-subs-empty';
        err.textContent = error.message;
        table.appendChild(err);
      });
  }
  $$('.sub-tabs [data-sub]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      $$('.sub-tabs [data-sub]').forEach(function (el) { el.classList.toggle('active', el === btn); });
      loadSubScope(btn.dataset.sub);
    });
  });
  if ($('#submissions-table') && csrfToken()) loadSubScope('my');
  ensureResultStructure();
  bindLoadEditorButton($('#btn-load-editor'));
  bindSubmissionLinks(document);
  if ($('#workspace-result-comments') && !$('#workspace-result-comments').hidden) {
    mountWorkspaceComments($('#workspace-result-comments'));
  }
  if (app.dataset.submissionId) {
    $('#tab-result') && $('#tab-result').classList.remove('tab-hidden');
    activateTab('result');
    // Code: hydrate full testcase table. Non-code: keep SSR summary and append AI/testcase HTML.
    // File uploads are human-graded: do not poll or inject judge-queue HTML.
    if (isCodeProblem()) {
      refreshResultPanel(app.dataset.submissionId).then(function () {
        updateAiGradeSection({ can_trigger_ai_grade: !!app.dataset.aiGradeUrl, ai_grade_url: app.dataset.aiGradeUrl || null });
      });
    } else if (!isFileProblem() && app.dataset.statusUrl) {
      fetch(app.dataset.statusUrl, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
        .then(parseJsonResponse)
        .then(function (status) {
          applyStatusPayload(status, null);
          if (statusIsPending(status)) pollFormal(app.dataset.statusUrl, null);
        })
        .catch(function () { /* keep SSR */ });
    } else if (!isFileProblem()) {
      appendTestcasesPanel(app.dataset.submissionId);
    }
  }

  (function initAiGrading() {
    document.addEventListener('click', function (event) {
      var btn = event.target && event.target.closest ? event.target.closest('#ai-grade-btn') : null;
      if (!btn) return;
      var gradeUrl = app.dataset.aiGradeUrl;
      if (!gradeUrl || btn.disabled) return;
      btn.disabled = true;
      btn.textContent = gettext('Thinking…');
      fetch(gradeUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'X-CSRFToken': csrfToken(),
          'X-Requested-With': 'XMLHttpRequest',
          'Accept': 'application/json',
        },
      })
        .then(function (response) {
          return response.json().then(function (json) {
            return { ok: response.ok, json: json };
          });
        })
        .then(function (res) {
          var box = $('#ai-grade-result');
          if (box) box.hidden = false;
          if (res.ok && res.json.ok) {
            if (box) box.textContent = gettext('AI Feedback finished. Reloading…');
            location.reload();
          } else {
            if (box) {
              box.textContent = (res.json && res.json.error) || gettext('Request failed. Please try again.');
            }
            btn.disabled = false;
            btn.textContent = gettext('View AI Feedback');
          }
        })
        .catch(function () {
          var box = $('#ai-grade-result');
          if (box) {
            box.hidden = false;
            box.textContent = gettext('Request failed. Please try again.');
          }
          btn.disabled = false;
          btn.textContent = gettext('View AI Feedback');
        });
    });
  })();

  (function initAiHints() {
    var btn = $('#ai-hint-btn');
    if (!btn || !app.dataset.aiHintsPossible) return;
    btn.addEventListener('click', function () {
      var hintUrl = app.dataset.aiHintUrl;
      if (!hintUrl || btn.disabled) return;
      btn.disabled = true;
      fetch(hintUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': csrfToken(), 'Accept': 'application/json' },
      })
        .then(function (response) {
          return response.json().then(function (json) {
            if (!response.ok) throw new Error(json.error || gettext('Request failed. Please try again.'));
            return json;
          });
        })
        .then(function (payload) {
          var box = $('#ai-hint-result');
          if (box) {
            box.hidden = false;
            box.textContent = payload.hint || '';
          }
          if (payload.remaining != null) {
            app.dataset.aiHintsRemaining = String(payload.remaining);
            var quota = $('#ai-hint-quota');
            if (quota) quota.textContent = '(' + payload.remaining + ' ' + gettext('uses remaining today') + ')';
            btn.disabled = Number(payload.remaining) <= 0;
          } else {
            btn.disabled = false;
          }
        })
        .catch(function (error) {
          var box = $('#ai-hint-result');
          if (box) {
            box.hidden = false;
            box.textContent = gettext('Error') + ': ' + (error.message || gettext('Request failed. Please try again.'));
          }
          btn.disabled = Number(app.dataset.aiHintsRemaining || 0) <= 0;
        });
    });
  })();

  function applyLeaveProgress(progress) {
    if (!progress) return;
    var forms = document.querySelectorAll('form.assignment-leave-form');
    if (!forms.length) return;
    // Empty name lists are valid (every problem submitted). Do not treat [] as missing.
    var submittedNames = progress.submitted_names;
    var unsubmittedNames = progress.unsubmitted_names;
    Array.prototype.forEach.call(forms, function (form) {
      if (progress.submitted != null) form.setAttribute('data-submitted', String(progress.submitted));
      if (progress.total != null) form.setAttribute('data-total', String(progress.total));
      if (progress.unsubmitted != null) form.setAttribute('data-unsubmitted', String(progress.unsubmitted));
      if (submittedNames != null) {
        form.setAttribute('data-submitted-names', JSON.stringify(submittedNames));
      }
      if (unsubmittedNames != null) {
        form.setAttribute('data-unsubmitted-names', JSON.stringify(unsubmittedNames));
      }
    });
  }

  function leaveProgressFromDrawer() {
    var submittedNames = [];
    var unsubmittedNames = [];
    items.forEach(function (item) {
      var nameEl = item.querySelector('.drawer-problem-name');
      var label = ((nameEl && nameEl.textContent) || item.dataset.code || '').trim();
      if (item.dataset.status !== 'unsolved') submittedNames.push(label);
      else unsubmittedNames.push(label);
    });
    return {
      submitted: submittedNames.length,
      unsubmitted: unsubmittedNames.length,
      total: items.length,
      submitted_names: submittedNames,
      unsubmitted_names: unsubmittedNames,
    };
  }

  function markCurrentProblemAnswered() {
    var code = app.dataset.problem;
    if (!code) return;
    items.forEach(function (item) {
      if (item.dataset.code !== code) return;
      if (item.dataset.status === 'unsolved') item.dataset.status = 'attempted';
      var icon = item.querySelector('.drawer-st i');
      if (icon) {
        icon.className = 'attempted-problem-color fa fa-minus-circle';
        icon.title = gettext('Attempted');
      }
    });
    renderDrawer();
    applyLeaveProgress(leaveProgressFromDrawer());
  }

  var submitForm = $('#workspace-submit-form');
  if (submitForm) {
    submitForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (timerExpired) return;
      if (source && !languageSelect()) return;
      syncAnswerFields();
      var button = $('#btn-submit');
      if (button) button.disabled = true;
      var form = event.currentTarget;
      fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        credentials: 'same-origin',
        headers: { 'X-Workspace-Submit': '1', 'Accept': 'application/json' },
      })
        .then(function (response) {
          return response.text().then(function (text) {
            var json = null;
            try { json = text ? JSON.parse(text) : {}; } catch (err) {
              throw new Error(gettext('Submit failed.'));
            }
            if (!response.ok) throw new Error((json && json.error) || gettext('Submit failed.'));
            return json;
          });
        })
        .then(function (payload) {
          if (timerExpired) {
            if (button) button.disabled = true;
            throw new Error(gettext('Time expired'));
          }
          activateTab('result');
          history.replaceState(null, '', location.pathname + '?submission=' + payload.submission_id);
          clearCurrentDrafts();
          applyLeaveProgress(payload.leave_progress);
          markCurrentProblemAnswered();
          app.dataset.submissionGraded = '0';
          if (payload.status_url) app.dataset.statusUrl = payload.status_url;
          if (payload.ai_grade_url) app.dataset.aiGradeUrl = payload.ai_grade_url;
          if (payload.submission_id) {
            app.dataset.submissionId = String(payload.submission_id);
            app.dataset.aiHintUrl = '/submission/' + payload.submission_id + '/ai-hint';
            ensureResultStructure();
            // Capture editor source immediately so it survives testcase HTML refresh.
            if (source || (payload.answer_display && payload.answer_display.length)) {
              updateSubmittedSource(source ? sourceValue() : payload.source, payload.answer_display);
            }
            if (payload.file_upload) updateFileUploadResult(payload.file_upload);
            if (payload.comments_html != null) updateResultComments(payload.comments_html);
            if (isCodeProblem() && !payload.results_hidden) {
              refreshResultPanel(payload.submission_id).then(function () {
                updateAiGradeSection(payload);
              });
            } else {
              renderFormalStatus(payload);
              updateAiGradeSection(payload);
              if (payload.submission_id && !payload.results_hidden && !awaitingManualAiGrade(payload)) {
                appendTestcasesPanel(payload.submission_id);
              }
            }
          } else {
            renderFormalStatus(payload);
          }
          $$('.sub-tabs [data-sub]').forEach(function (el) {
            el.classList.toggle('active', el.dataset.sub === 'my');
          });
          loadSubScope('my');
          if (isFileProblem()) {
            app.dataset.submissionGraded = '1';
            if (button && !timerExpired) button.disabled = false;
          } else {
            pollFormal(payload.status_url, button);
          }
        })
        .catch(function (error) {
          if (button) button.disabled = false;
          // Only replace the summary pane — keep submitted source / comments siblings.
          ensureResultStructure();
          var main = $('#workspace-result-main');
          if (main) {
            main.textContent = '';
            var err = document.createElement('p');
            err.textContent = error.message;
            main.appendChild(err);
          }
          activateTab('result');
        });
    });
  }
  var submitBtn = $('#btn-submit');
  if (submitBtn && submitForm) {
    submitBtn.addEventListener('click', function () {
      if (timerExpired) return;
      submitForm.requestSubmit();
    });
  }

  (function initMetaCollapse() {
    var meta = $('#ws-meta');
    var toggle = $('#ws-meta-toggle');
    if (!meta || !toggle) return;
    var storageKey = 'ws-meta-collapsed:' + (meta.dataset.problem || app.dataset.problem || '');
    var apply = function (collapsed) {
      meta.classList.toggle('collapsed', collapsed);
      toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      toggle.setAttribute('title', collapsed ? gettext('Expand') : gettext('Collapse'));
    };
    var stored = null;
    try { stored = localStorage.getItem(storageKey); } catch (err) { stored = null; }
    apply(stored === '1');
    toggle.addEventListener('click', function () {
      var collapsed = !meta.classList.contains('collapsed');
      apply(collapsed);
      try { localStorage.setItem(storageKey, collapsed ? '1' : '0'); } catch (err) { /* ignore */ }
    });
  })();

  (function initProblemDelete() {
    var form = document.getElementById('ws-problem-delete-form');
    var modal = document.getElementById('ws-delete-modal');
    var openBtn = document.querySelector('button[data-action="delete-problem"]');
    if (!form || !modal || !openBtn) return;
    var cancelBtn = modal.querySelector('[data-action="cancel-delete-problem"]');
    var confirmBtn = modal.querySelector('[data-action="confirm-delete-problem"]');
    var errorEl = document.getElementById('ws-delete-modal-error');
    var submitting = false;

    function setError(message) {
      if (!errorEl) return;
      if (message) {
        errorEl.textContent = message;
        errorEl.hidden = false;
      } else {
        errorEl.textContent = '';
        errorEl.hidden = true;
      }
    }
    function openModal() {
      setError('');
      modal.hidden = false;
      if (confirmBtn) confirmBtn.focus();
    }
    function closeModal() {
      if (submitting) return;
      modal.hidden = true;
      openBtn.focus();
    }
    openBtn.addEventListener('click', function (event) {
      event.preventDefault();
      openModal();
    });
    if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
    modal.addEventListener('click', function (event) {
      if (event.target === modal) closeModal();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !modal.hidden) closeModal();
    });
    function goToList(listUrl) {
      window.location.assign(listUrl || form.getAttribute('data-list-url') || '/problems/');
    }
    function submitDelete() {
      if (submitting) return;
      submitting = true;
      if (confirmBtn) confirmBtn.disabled = true;
      setError('');
      fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        credentials: 'same-origin',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
          'Accept': 'application/json',
        },
      }).then(function (response) {
        return response.text().then(function (txt) {
          var data = null;
          try { data = JSON.parse(txt); } catch (err) { data = null; }
          if (!response.ok || !data || !data.ok || !data.deleted) {
            throw new Error((data && (data.error || data.message)) || gettext('Could not delete this problem.'));
          }
          goToList(data.list_url);
          return data;
        });
      }).catch(function (error) {
        submitting = false;
        if (confirmBtn) confirmBtn.disabled = false;
        setError(error.message || gettext('Could not delete this problem.'));
      });
    }
    if (confirmBtn) confirmBtn.addEventListener('click', submitDelete);
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      if (modal.hidden) openModal();
      else submitDelete();
    });
  })();

  if (!source) return;

  if (draftsEnabled()) {
    // The server draft wins over the local one: it is what the deadline sweep will submit, and the
    // only copy that follows the student to another device. Its language comes back too, so the
    // restored source is not read as a different language's code.
    var remoteDraft = serverDraft();
    var saved = null;
    if (remoteDraft) {
      saved = remoteDraft.content;
      var remoteLanguage = languageSelect();
      if (remoteLanguage && remoteDraft.language
          && remoteLanguage.querySelector('option[value="' + remoteDraft.language + '"]')) {
        remoteLanguage.value = String(remoteDraft.language);
      }
    } else {
      try { saved = localStorage.getItem(draftKey()); } catch (err) { saved = null; }
    }
    if (saved !== null) source.value = saved;
    else if (!source.value) applyLanguageTemplate();
  } else if (!source.value) {
    applyLanguageTemplate();
  }
  source.addEventListener('input', saveDraft);
  var languageEl = languageSelect();
  if (languageEl) {
    languageEl.addEventListener('change', function () {
      if (draftsEnabled()) {
        var languageDraft = null;
        try { languageDraft = localStorage.getItem(draftKey()); } catch (err) { languageDraft = null; }
        if (languageDraft !== null) {
          setSource(languageDraft);
          setEditorLanguage();
          // The language is part of what gets submitted, so the server copy has to follow it.
          saveDraft();
          return;
        }
      }
      setSource(templateForLanguage(languageEl.value));
      setEditorLanguage();
      saveDraft();
    });
  }
  bindLoadEditorButton($('#btn-load-editor'));
  var collapseBottom = $('#btn-collapse-bottom');
  if (collapseBottom) {
    collapseBottom.addEventListener('click', function () { $('#ide-bottom').classList.toggle('collapsed'); });
  }

  function fallback() { $('#editor-fallback').hidden = false; source.hidden = false; }
  var loader = document.createElement('script');
  loader.src = '/static/libs/monaco/vs/loader.js';
  loader.onload = function () {
    window.require.config({ paths: { vs: '/static/libs/monaco/vs' } });
    window.require(['vs/editor/editor.main'], function () {
      mount.hidden = false;
      editor = window.monaco.editor.create(mount, {
        value: source.value,
        language: monacoLanguageFromOption(languageSelect() && languageSelect().selectedOptions[0]),
        theme: monacoTheme(),
        automaticLayout: true,
        minimap: { enabled: false },
      });
      source.hidden = true;
      setEditorLanguage();
      syncMonacoTheme();
      editor.onDidChangeModelContent(function () { source.value = editor.getValue(); saveDraft(); });
      editor.onDidChangeCursorPosition(function (event) {
        $('#cursor-status').textContent = 'Ln ' + event.position.lineNumber + ', Col ' + event.position.column;
      });
    });
  };
  loader.onerror = fallback;
  document.head.appendChild(loader);

  var runBtn = $('#btn-run');
  if (runBtn) {
    runBtn.addEventListener('click', function () {
      var button = runBtn, select = languageSelect();
      if (runTimer || !select) return;
      button.disabled = true;
      activateBottom('result');
      var outEl = $('#workspace-run-output');
      var msgEl = $('#run-message');
      function setRunMessageHtml(html) {
        if (msgEl) msgEl.innerHTML = html || '';
      }
      function setRunMessageText(text) {
        setRunMessageHtml('');
        if (msgEl) msgEl.textContent = text || '';
      }
      function setRunOutputHtml(html) {
        if (outEl) outEl.innerHTML = html || '';
      }
      function setRunOutputText(text) {
        if (outEl) outEl.textContent = text || '';
      }
      function applyCompletedRun(data) {
        if (typeof formatIdeRunParts === 'function') {
          var parts = formatIdeRunParts(data);
          setRunMessageHtml(parts.infoHtml);
          setRunOutputHtml(parts.outputHtml);
          return;
        }
        setRunOutputHtml(typeof formatIdeRunResult === 'function' ? formatIdeRunResult(data) : '');
      }
      setRunMessageText(gettext('Submitting isolated run...'));
      setRunOutputHtml('');
      var data = new FormData();
      data.append('source', sourceValue());
      data.append('language', select.value);
      data.append('input', ($('#workspace-input') || {}).value || '');
      data.append('csrfmiddlewaretoken', $('[name=csrfmiddlewaretoken]').value);
      fetch(app.dataset.runUrl, { method: 'POST', body: data, credentials: 'same-origin' })
        .then(function (response) { return response.json().then(function (json) { if (!response.ok) throw new Error(json.error || gettext('Run failed.')); return json; }); })
        .then(function (payload) {
          setRunMessageText(gettext('Queued...'));
          setRunOutputHtml('');
          var attempts = 0;
          runTimer = window.setInterval(function () {
            attempts++;
            fetch(payload.submission_url, { credentials: 'same-origin' })
              .then(function (response) { return response.json(); })
              .then(function (status) {
                var complete = typeof isIdeRunComplete === 'function'
                  ? isIdeRunComplete(status.status)
                  : (status.status === 'D' || status.status === 'IE' || status.status === 'CE' || status.status === 'AB');
                if (complete) {
                  applyCompletedRun(status);
                } else if (!complete && typeof formatIdeRunPending === 'function') {
                  setRunMessageText(formatIdeRunPending(status.status) || gettext('Queued...'));
                  setRunOutputHtml('');
                } else if (!complete) {
                  setRunMessageText(status.status || gettext('Queued...'));
                  setRunOutputHtml('');
                }
                if (complete || attempts >= 60) {
                  window.clearInterval(runTimer);
                  runTimer = null;
                  button.disabled = false;
                  if (!complete && attempts >= 60) {
                    setRunMessageText(gettext(
                      'Run stayed queued. Check that the IDE sandbox problem (idepractice) is loaded on a judge.',
                    ));
                    setRunOutputHtml('');
                  }
                }
              })
              .catch(function () {
                window.clearInterval(runTimer);
                runTimer = null;
                button.disabled = false;
                setRunMessageText(gettext('Could not retrieve run status.'));
                setRunOutputHtml('');
              });
          }, 1000);
        })
        .catch(function (error) {
          button.disabled = false;
          setRunMessageText(error.message);
          setRunOutputHtml('');
        });
    });
  }
}());
