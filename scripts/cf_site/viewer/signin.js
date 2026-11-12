// signin: HF OAuth (authorization code + PKCE S256, public client, no secret) for the log viewer origin only
// (docs/experiment-library.md, "Log viewer with HF sign-in"). Same-tab redirect: no popup, no postMessage. The
// state and PKCE verifier live in this tab's sessionStorage for one attempt; the one-time code is removed from the
// address bar before anything else runs; the token goes to this tab's sessionStorage (at most 8 h) and is never
// logged, put in a URL or sent anywhere but https://huggingface.co.
(() => {
  const KEY = 'evallog-oauth';
  const PENDING = 'evallog-oauth-pending';
  const HUB = 'https://huggingface.co';
  const MAX_AGE = 8 * 3600 * 1000;
  const root = document.documentElement.dataset;
  const status = document.getElementById('status');
  const signin = document.getElementById('signin');
  const signout = document.getElementById('signout');
  const next = document.getElementById('next');
  const configured = /^[A-Za-z0-9-]{8,80}$/.test(root.clientId || '') && /^https:\/\//.test(root.redirectUri || '');

  // Capture and strip a returned code/state at once (before any other work), so the one-time code never stays
  // in the address bar, the history or a Referer.
  const params = new URLSearchParams(location.search);
  if (location.search) history.replaceState(null, '', location.pathname);

  document.getElementById('org').textContent = root.orgName || '';
  function say(text, error) { status.textContent = text; status.className = error ? 'error' : ''; }
  // Only a path on this origin, never another origin.
  function safeReturn(value) {
    if (!value || !value.startsWith('/') || value.startsWith('//') || value.includes('\\')) return null;
    let url;
    try { url = new URL(value, location.origin); } catch (error) { return null; }
    return url.origin === location.origin ? url.pathname : null;
  }
  function stored() {
    try {
      const value = JSON.parse(sessionStorage.getItem(KEY) || 'null');
      const now = Date.now();
      return value && typeof value.accessToken === 'string' && typeof value.expiresAt === 'number'
        && value.expiresAt > now + 30000 && value.expiresAt <= now + MAX_AGE + 60000 ? value : null;
    } catch (error) { return null; }
  }
  function base64url(bytes) {
    return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function random() { return base64url(crypto.getRandomValues(new Uint8Array(32))); }
  function showNext(target) {
    next.textContent = '';
    if (!target) return;
    const link = document.createElement('a');
    link.href = target;
    link.textContent = 'Continue to the run you came from';
    next.append(link);
  }
  function refresh() {
    const token = stored();
    signin.hidden = !!token || !configured;
    signout.hidden = !token;
    if (token) say('Signed in with HF until ' + new Date(token.expiresAt).toLocaleString() + ' (this tab only).');
  }

  async function start(target) {
    const verifier = random();
    const state = random();
    const challenge = base64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
    sessionStorage.setItem(PENDING, JSON.stringify({ verifier, state, target, started: Date.now() }));
    const url = new URL(HUB + '/oauth/authorize');
    url.search = new URLSearchParams({
      client_id: root.clientId, redirect_uri: root.redirectUri, response_type: 'code',
      scope: 'openid profile read-repos', state, code_challenge: challenge, code_challenge_method: 'S256',
      ...(root.orgIds ? { orgIds: root.orgIds } : {}),
    }).toString();
    location.assign(url.href);
  }

  async function finish() {
    let pending = null;
    try { pending = JSON.parse(sessionStorage.getItem(PENDING) || 'null'); } catch (error) { /* ignore */ }
    sessionStorage.removeItem(PENDING);  // single use, whatever happens next
    if (!pending || !pending.state || pending.state !== params.get('state')
        || !(Date.now() - pending.started < 15 * 60 * 1000)) {
      say('This sign-in was not started from this tab or has expired; sign in again.', true);
      return;
    }
    say('Finishing sign-in…');
    const response = await fetch(HUB + '/oauth/token', {
      method: 'POST', credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'authorization_code', code: params.get('code'), redirect_uri: root.redirectUri,
        client_id: root.clientId, code_verifier: pending.verifier,
      }).toString(),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok || typeof body.access_token !== 'string' || !body.access_token) {
      say('HF refused the sign-in (' + (body.error_description || body.error || 'HTTP ' + response.status)
        + '). Sign in again.', true);
      return;
    }
    const lifetime = Math.min(MAX_AGE, 1000 * (Number(body.expires_in) || 28800));
    sessionStorage.setItem(KEY, JSON.stringify({ accessToken: body.access_token, expiresAt: Date.now() + lifetime }));
    refresh();
    const target = safeReturn(pending.target);
    showNext(target);
    if (target) location.replace(target);
  }

  function main() {
    const target = safeReturn(params.get('return'));
    signout.addEventListener('click', () => {
      try { sessionStorage.removeItem(KEY); } catch (error) { /* ignore */ }
      say('Signed out.');
      refresh();
    });
    if (!configured) {
      say('HF sign-in is not configured for this viewer yet; the logs cannot be read here for now.', true);
      refresh();
      return;
    }
    signin.addEventListener('click', () => { start(target).catch((error) => say('Sign-in failed: ' + error.message, true)); });
    if (params.has('error')) {
      sessionStorage.removeItem(PENDING);
      say('HF sign-in did not complete: ' + (params.get('error_description') || params.get('error')), true);
    } else if (params.has('code')) {
      finish().catch((error) => say('Sign-in failed: ' + error.message, true));
      return;
    } else if (!stored()) {
      say('Not signed in. The logs are private: sign in with HF (organization members only) to view them.');
    }
    refresh();
    if (stored()) showNext(target);
  }
  main();
})();
