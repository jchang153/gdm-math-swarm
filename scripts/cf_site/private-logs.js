// private-logs: the Cloudflare site's Inspect View notice (docs/experiment-library.md, "Cloudflare site").
// Inspect logs are private and are never served by the public Cloudflare site. Inlined first into each
// Inspect View index.html by scripts/publish_cloudflare.py, replacing the HF sign-in fetch shim, with its
// settings as data-* attributes of the script tag:
//   data-records  optional link to where members can read the private logs (the private HF logs dataset
//                 folder of the run at its pinned commit); empty when the logs are kept offline
// Same-origin GET/HEAD requests for logs/<name>.eval answer 404 locally (nothing is fetched) and a bar explains
// where the logs are. It sends nothing and stores nothing.
(() => {
  const config = document.currentScript.dataset;
  const NAME = /^[A-Za-z0-9][A-Za-z0-9_.+=-]*\.eval$/;
  const logs = new URL('logs/', document.baseURI);

  function isLog(value) {
    let url;
    try { url = new URL(value, document.baseURI); } catch (error) { return false; }
    if (url.origin !== location.origin || !url.pathname.startsWith(logs.pathname)) return false;
    try { return NAME.test(decodeURIComponent(url.pathname.slice(logs.pathname.length))); } catch (error) { return false; }
  }

  let bar = null;
  function banner() {
    const show = () => {
      if (bar) return;
      bar = document.createElement('div');
      bar.id = 'private-logs';
      bar.setAttribute('role', 'status');
      bar.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;padding:8px 14px;'
        + 'font:14px/1.4 system-ui,sans-serif;background:#fff8c5;color:#3b2300;border-top:1px solid #d4a72c';
      bar.append('This run\'s Inspect logs (agent transcripts) are private and are not served by this public site.');
      if (config.records) {
        const link = document.createElement('a');
        link.href = config.records;
        link.target = '_blank';
        link.rel = 'noopener';
        link.textContent = 'Open them in the private logs dataset';
        link.style.fontWeight = '600';
        bar.append(' ', link, ' (organization members only).');
      } else {
        bar.append(' They are kept offline by the researchers.');
      }
      document.body.append(bar);
    };
    if (document.body) show(); else document.addEventListener('DOMContentLoaded', show);
  }

  const inner = window.fetch.bind(window);
  window.fetch = (input, options) => {
    const request = input instanceof Request ? input : null;
    if (!isLog(request ? request.url : String(input))) return inner(input, options);
    banner();
    return Promise.resolve(new Response('This Inspect log is private and not served by this site.',
      { status: 404, statusText: 'Private log', headers: { 'content-type': 'text/plain' } }));
  };
  banner();
})();
