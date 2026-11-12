// auth-fetch: the log viewer's Inspect View reads a run's private .eval logs from the HF logs dataset with the
// viewer's own HF sign-in (docs/experiment-library.md, "Log viewer with HF sign-in"). Served from the separate
// log-viewer origin only (never the public library's origin, which renders agent-written content), loaded first
// in each viewer page as an external script (CSP script-src 'self'), with its settings as data-* attributes:
//   data-dataset  the dataset repository (organization/name)
//   data-commit   the dataset commit the run's logs were published at (links are immutable)
//   data-path     the logs' folder in the dataset (RUN/inspect)
//   data-signin   the sign-in page of this origin
// The token ({accessToken, expiresAt}) lives only in this tab's sessionStorage on this origin (gone when the tab
// closes; at most 8 h; Sign out clears it). It is sent only as "Authorization: Bearer" to https://huggingface.co
// (resolve URLs of the configured dataset); HF answers with a 302 to its CDN, and the browser drops the
// Authorization header on that cross-origin redirect. Same-origin GET/HEAD requests for logs/<name>.eval are
// rewritten; everything else passes through unchanged. Nothing is logged.
(() => {
  const config = document.currentScript.dataset;
  const KEY = 'evallog-oauth';
  const HUB = 'https://huggingface.co';
  const MAX_AGE = 8 * 3600 * 1000;
  const NAME = /^[A-Za-z0-9][A-Za-z0-9_.+=-]*\.eval$/;
  const REPO = /^[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9][A-Za-z0-9_.-]*$/;
  const COMMIT = /^[0-9a-f]{40}$/;
  const FOLDER = /^[A-Za-z0-9][A-Za-z0-9_.+=\/-]*$/;
  const valid = REPO.test(config.dataset || '') && COMMIT.test(config.commit || '')
    && FOLDER.test(config.path || '') && !(config.path || '').split('/').includes('..');
  const logs = new URL('logs/', document.baseURI);

  function token() {
    try {
      const stored = JSON.parse(sessionStorage.getItem(KEY) || 'null');
      const now = Date.now();
      if (stored && typeof stored.accessToken === 'string' && stored.accessToken
          && typeof stored.expiresAt === 'number' && stored.expiresAt > now + 30000
          && stored.expiresAt <= now + MAX_AGE + 60000) {
        return stored;
      }
      if (stored) sessionStorage.removeItem(KEY);
    } catch (error) { /* storage unavailable */ }
    return null;
  }

  function signOut() {
    try { sessionStorage.removeItem(KEY); } catch (error) { /* ignore */ }
    location.reload();
  }

  function target(value) {
    let url;
    try { url = new URL(value, document.baseURI); } catch (error) { return null; }
    if (url.origin !== location.origin || !url.pathname.startsWith(logs.pathname)) return null;
    let name;
    try { name = decodeURIComponent(url.pathname.slice(logs.pathname.length)); } catch (error) { return null; }
    if (!NAME.test(name)) return null;
    const folder = config.path.split('/').map(encodeURIComponent).join('/');
    return `${HUB}/datasets/${config.dataset}/resolve/${config.commit}/${folder}/${encodeURIComponent(name)}`;
  }

  let bar = null;
  function banner(message, signedIn) {
    const show = () => {
      if (!bar) {
        bar = document.createElement('div');
        bar.id = 'evallog-auth';
        bar.setAttribute('role', 'status');
        bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;padding:6px 14px;'
          + 'font:13px/1.4 system-ui,sans-serif;background:#fff8c5;color:#3b2300;border-top:1px solid #d4a72c';
        document.body.append(bar);
      }
      bar.textContent = message + ' ';
      const button = document.createElement(signedIn ? 'button' : 'a');
      button.style.cssText = 'font:inherit;font-weight:600;color:#0550ae;background:none;border:0;padding:0;'
        + 'cursor:pointer;text-decoration:underline';
      if (signedIn) {
        button.type = 'button';
        button.textContent = 'Sign out';
        button.addEventListener('click', signOut);
      } else {
        const signin = new URL(config.signin || '/signin', location.origin);
        signin.searchParams.set('return', location.pathname);
        button.href = signin.pathname + signin.search;
        button.textContent = 'Sign in with HF';
      }
      bar.append(button);
    };
    if (document.body) show(); else document.addEventListener('DOMContentLoaded', show);
  }

  // Without a sign-in, a clear page-wide state instead of Inspect View's load error.
  function privateState() {
    const show = () => {
      if (document.getElementById('evallog-private')) return;
      const panel = document.createElement('div');
      panel.id = 'evallog-private';
      panel.setAttribute('role', 'dialog');
      panel.setAttribute('aria-label', 'Private logs');
      panel.style.cssText = 'position:fixed;inset:0;z-index:2147483646;display:flex;align-items:center;'
        + 'justify-content:center;padding:16px;background:rgba(246,248,250,.97);font:16px/1.5 system-ui,sans-serif;'
        + 'color:#1f2328';
      const card = document.createElement('div');
      card.style.cssText = 'max-width:520px;background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:24px';
      const title = document.createElement('h1');
      title.style.cssText = 'font-size:1.3rem;margin:0 0 10px';
      title.textContent = 'These Inspect logs are private';
      const text = document.createElement('p');
      text.style.margin = '0 0 16px';
      text.textContent = 'Sign in with Hugging Face to view them. Only members of the organization that owns the '
        + 'private logs dataset can read them; the logs go straight from Hugging Face to your browser.';
      const signin = new URL(config.signin || '/signin', location.origin);
      signin.searchParams.set('return', location.pathname);
      const button = document.createElement('a');
      button.href = signin.pathname + signin.search;
      button.textContent = 'Sign in with HF';
      button.style.cssText = 'display:inline-block;padding:8px 16px;border-radius:6px;background:#0969da;color:#fff;'
        + 'font-weight:600;text-decoration:none';
      const back = document.createElement('a');
      back.href = '/';
      back.textContent = 'All runs';
      back.style.cssText = 'margin-left:16px;color:#0969da';
      card.append(title, text, button, back);
      panel.append(card);
      document.body.append(panel);
    };
    if (document.body) show(); else document.addEventListener('DOMContentLoaded', show);
  }

  const inner = window.fetch.bind(window);
  window.fetch = (input, options) => {
    const request = input instanceof Request ? input : null;
    const destination = valid ? target(request ? request.url : String(input)) : null;
    if (!destination) return inner(input, options);
    const method = String((options && options.method) || (request && request.method) || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') return inner(input, options);
    const stored = token();
    if (!stored) {
      banner('This run\'s Inspect logs are in a private HF dataset (organization members only).', false);
      return Promise.resolve(new Response('Sign in with HF to read this Inspect log.',
        { status: 401, statusText: 'Sign in with HF', headers: { 'content-type': 'text/plain' } }));
    }
    const headers = new Headers();
    const original = new Headers((options && options.headers) || (request && request.headers) || undefined);
    for (const name of ['range', 'if-range', 'if-none-match', 'if-match']) {
      if (original.has(name)) headers.set(name, original.get(name));
    }
    headers.set('Authorization', 'Bearer ' + stored.accessToken);
    return inner(destination, {
      method, headers, redirect: 'follow', credentials: 'omit', mode: 'cors', referrerPolicy: 'no-referrer',
      cache: 'no-store', signal: (options && options.signal) || (request && request.signal) || undefined,
    }).then((response) => {
      if (response.status === 401 || response.status === 403 || response.status === 404) {
        banner(`The HF dataset refused this log (HTTP ${response.status}): the sign-in may have expired or may `
          + 'not include the organization.', true);
      }
      return response;
    });
  };

  if (!valid) {
    banner('This page has no valid log location; the logs cannot be read here.', !!token());
  } else {
    const stored = token();
    if (stored) {
      banner('Signed in with HF until ' + new Date(stored.expiresAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        + ' (this tab only).', true);
    } else {
      banner('This run\'s Inspect logs are in a private HF dataset (organization members only).', false);
      privateState();
    }
  }
})();
