/**
 * Server-authoritative Markdown preview helper.
 * Posts to Django markdown preview endpoints (same pipeline as problem pages).
 *
 * Browser: window.MarkdownPreview
 * Node (tests): module.exports
 *
 *   MarkdownPreview.configure({ url, batchUrl, csrfToken })
 *   MarkdownPreview.render(md) -> Promise<html>
 *   MarkdownPreview.renderMany(mds) -> Promise<html[]>
 *   MarkdownPreview.renderKeyed([{key, md}]) -> Promise<[{key, html}]>
 *   MarkdownPreview.getCached(md) / clearCache()
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.MarkdownPreview = factory();
  }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const state = {
    url: '',
    batchUrl: '',
    csrfToken: '',
    cache: new Map(),
    inflight: new Map(),
  };

  function configure(opts) {
    if (!opts) return;
    if (opts.url != null) state.url = opts.url;
    if (opts.batchUrl != null) state.batchUrl = opts.batchUrl;
    if (opts.csrfToken != null) state.csrfToken = opts.csrfToken;
  }

  function getCached(md) {
    return state.cache.get(md == null ? '' : String(md));
  }

  function clearCache() {
    state.cache.clear();
    state.inflight.clear();
  }

  // Stale servers may leave [user:] tokens in HTML. Match problem-page get_user().
  const USER_REF_RE = /\[(r?user):(\w+)\]/g;

  function decorateUserRefs(html) {
    const src = html == null ? '' : String(html);
    if (src.indexOf('[user:') < 0 && src.indexOf('[ruser:') < 0) return src;
    return src.replace(USER_REF_RE, (_, _kind, name) => (
      '<span class="rating rate-none user"><a href="/user/' + name + '">' + name + '</a></span>'
    ));
  }

  function render(md) {
    const key = md == null ? '' : String(md);
    if (state.cache.has(key)) return Promise.resolve(state.cache.get(key));
    if (state.inflight.has(key)) return state.inflight.get(key);
    if (!state.url) {
      return Promise.reject(new Error('MarkdownPreview: url not configured'));
    }
    const body = new URLSearchParams();
    body.set('content', key);
    const p = fetch(state.url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-CSRFToken': state.csrfToken,
      },
      body: body.toString(),
    })
      .then(r => {
        if (!r.ok) throw new Error('MarkdownPreview HTTP ' + r.status);
        return r.text();
      })
      .then(html => {
        const out = decorateUserRefs(html || '');
        state.cache.set(key, out);
        state.inflight.delete(key);
        return out;
      })
      .catch(err => {
        state.inflight.delete(key);
        throw err;
      });
    state.inflight.set(key, p);
    return p;
  }

  function renderMany(mds) {
    const list = Array.isArray(mds) ? mds : [];
    return renderKeyed(list.map((md, i) => ({ key: String(i), md }))).then(rows =>
      rows.map(r => r.html));
  }

  /**
   * Prefer one batch POST for uncached fragments; fall back to per-item render.
   * @param {{key:string, md:string}[]} items
   * @returns {Promise<{key:string, html:string}[]>}
   */
  function renderKeyed(items) {
    const list = Array.isArray(items) ? items : [];
    if (!list.length) return Promise.resolve([]);

    const results = new Array(list.length);
    const pendingIdx = [];
    const pendingPayload = [];

    list.forEach((item, i) => {
      const md = !item || item.md == null ? '' : String(item.md);
      const key = item && item.key != null ? String(item.key) : String(i);
      if (state.cache.has(md)) {
        results[i] = { key: key, html: state.cache.get(md) };
      } else {
        pendingIdx.push(i);
        pendingPayload.push({ key: key, content: md, _md: md });
      }
    });

    if (!pendingIdx.length) return Promise.resolve(results);

    const finishLocal = (htmlList) => {
      pendingIdx.forEach((idx, j) => {
        const md = pendingPayload[j]._md;
        const html = decorateUserRefs(htmlList[j] || '');
        state.cache.set(md, html);
        results[idx] = { key: pendingPayload[j].key, html: html };
      });
      return results;
    };

    if (state.batchUrl) {
      return fetch(state.batchUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json; charset=UTF-8',
          'X-CSRFToken': state.csrfToken,
        },
        body: JSON.stringify({
          items: pendingPayload.map(p => ({ key: p.key, content: p.content })),
        }),
      })
        .then(r => {
          if (!r.ok) throw new Error('MarkdownPreview batch HTTP ' + r.status);
          return r.json();
        })
        .then(body => {
          const rows = (body && body.items) || [];
          const byKey = Object.create(null);
          rows.forEach(row => { byKey[String(row.key)] = row.html || ''; });
          return finishLocal(pendingPayload.map(p =>
            (Object.prototype.hasOwnProperty.call(byKey, p.key) ? byKey[p.key] : '')));
        })
        .catch(() =>
          // Fall back to single-fragment endpoint if batch fails.
          Promise.all(pendingPayload.map(p => render(p._md))).then(finishLocal));
    }

    return Promise.all(pendingPayload.map(p => render(p._md))).then(finishLocal);
  }

  return {
    configure: configure,
    render: render,
    renderMany: renderMany,
    renderKeyed: renderKeyed,
    getCached: getCached,
    clearCache: clearCache,
  };
}));
