// evallog-fetch: Inspect View reads its .eval logs from the private HF dataset, not from this Space
// (docs/experiment-library.md, "Logs in a dataset repo"). Inlined first into each Inspect View
// index.html by scripts/dataset_logs.py, with its settings as data-* attributes of the script tag:
//   data-dataset  the dataset repository (organization/name)
//   data-commit   the dataset commit the run's logs were published at (links are immutable)
//   data-path     the logs' folder in the dataset (RUN/inspect or live/RUN/inspect)
//   data-signin   the sign-in page, relative to this page
// It uses only the viewer's own HF OAuth access token (signin.html, "Sign in with HF"), kept in this
// origin's localStorage; no publisher token or secret is ever embedded. Same-origin GET/HEAD requests
// for logs/<name>.eval go to the dataset's resolve URL with "Authorization: Bearer", redirect "follow"
// (HF answers with a 302 to its CDN, which serves Range requests with 206) and credentials "omit".
(() => {
  const config = document.currentScript.dataset;
  const KEY = 'evallog-oauth';
  const HUB = 'https://huggingface.co';
  const NAME = /^[A-Za-z0-9][A-Za-z0-9_.+=-]*\.eval$/;
  const logs = new URL('logs/', document.baseURI);
  const folder = config.path.split('/').map(encodeURIComponent).join('/');

  function token() {
    try {
      const stored = JSON.parse(localStorage.getItem(KEY) || 'null');
      if (stored && stored.accessToken && (!stored.expiresAt || stored.expiresAt > Date.now() + 30000)) {
        return stored.accessToken;
      }
    } catch (error) { /* storage unavailable or blocked */ }
    return null;
  }

  function target(value) {
    let url;
    try { url = new URL(value, document.baseURI); } catch (error) { return null; }
    if (url.origin !== location.origin || !url.pathname.startsWith(logs.pathname)) return null;
    let name;
    try { name = decodeURIComponent(url.pathname.slice(logs.pathname.length)); } catch (error) { return null; }
    if (!NAME.test(name)) return null;
    return `${HUB}/datasets/${config.dataset}/resolve/${config.commit}/${folder}/${encodeURIComponent(name)}`;
  }

  let bar = null;
  function banner(reason) {
    const show = () => {
      if (bar) return;
      bar = document.createElement('div');
      bar.id = 'evallog-signin';
      bar.setAttribute('role', 'status');
      bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;padding:8px 14px;'
        + 'font:14px/1.4 system-ui,sans-serif;background:#fff8c5;color:#3b2300;border-top:1px solid #d4a72c';
      const link = document.createElement('a');
      const signin = new URL(config.signin, document.baseURI);
      signin.searchParams.set('return', location.pathname + location.hash);
      link.href = signin.href;
      if (window.top !== window) link.target = '_blank';  // HF's login page cannot be framed
      link.textContent = 'Sign in with HF';
      link.style.fontWeight = '600';
      bar.append(reason + ' ', link,
        window.top !== window ? ' (opens a new tab; read the logs there).' : '.');
      document.body.append(bar);
    };
    if (document.body) show(); else document.addEventListener('DOMContentLoaded', show);
  }

  // A sign-in popup that kept its opener hands the token back to this page (HF's login pages send
  // Cross-Origin-Opener-Policy: same-origin, which usually severs the opener; then read the logs in
  // the sign-in tab, which is first-party and has the token).
  window.addEventListener('message', (event) => {
    const data = event.data;
    if (event.origin !== location.origin || !data || data.type !== 'evallog-oauth' || !data.accessToken) return;
    try {
      localStorage.setItem(KEY, JSON.stringify({ accessToken: data.accessToken, expiresAt: data.expiresAt }));
    } catch (error) { return; }
    location.reload();
  });

  const inner = window.fetch.bind(window);
  window.fetch = (input, options) => {
    const request = input instanceof Request ? input : null;
    const destination = target(request ? request.url : String(input));
    if (!destination) return inner(input, options);
    const method = String((options && options.method) || (request && request.method) || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') return inner(input, options);
    const bearer = token();
    if (!bearer) {
      banner('This run\'s Inspect logs are stored in a private HF dataset.');
      return Promise.resolve(new Response('Sign in with HF to read this Inspect log.',
        { status: 401, statusText: 'Sign in with HF', headers: { 'content-type': 'text/plain' } }));
    }
    const headers = new Headers();
    const original = new Headers((options && options.headers) || (request && request.headers) || undefined);
    for (const name of ['range', 'if-range', 'if-none-match', 'if-match']) {
      if (original.has(name)) headers.set(name, original.get(name));
    }
    headers.set('Authorization', 'Bearer ' + bearer);
    return inner(destination, {
      method, headers, redirect: 'follow', credentials: 'omit', mode: 'cors', referrerPolicy: 'no-referrer',
      signal: (options && options.signal) || (request && request.signal) || undefined,
    }).then((response) => {
      if (response.status === 401 || response.status === 403 || response.status === 404) {
        banner(`The dataset refused this log (HTTP ${response.status}): your sign-in may have expired or lack `
          + 'read access to the organization.');
      }
      return response;
    });
  };
  if (!token()) banner('This run\'s Inspect logs are stored in a private HF dataset.');
})();
