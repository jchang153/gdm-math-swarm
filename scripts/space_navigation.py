"""Private-Space browser navigation; no host authentication or model calls."""
import json
import re


SCRIPT = r'''<script id="private-space-navigation">
(()=>{
if(!location.hostname.endsWith('.static.hf.space'))return;
// The visitor's temporary HF session stays in this browser and this origin.
// Never embed a publisher token, persist the session, or send it off-origin.
const session=new URLSearchParams(location.search).get('__sign');
if(!session)return;
function signed(value){
 const url=new URL(value,document.baseURI);
 if(url.origin!==location.origin||url.username||url.password)return value;
 url.searchParams.set('__sign',session);return url.href;
}
const originalFetch=window.fetch.bind(window);
window.fetch=(input,options)=>originalFetch(input instanceof Request?
 new Request(signed(input.url),input):signed(String(input)),options);
const open=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(method,url,...rest){return open.call(this,method,signed(String(url)),...rest);};
const imports={};
for(const name of MODULE_PATHS){const url=new URL(name,document.baseURI);imports[url.href]=signed(url.href);}
if(Object.keys(imports).length){const map=document.createElement('script');map.type='importmap';map.textContent=JSON.stringify({imports});document.head.append(map);}
function anchor(a){const value=a.getAttribute('href');if(value&&!value.startsWith('#'))a.setAttribute('href',signed(value));}
for(const event of ['click','auxclick','contextmenu','focusin','pointerover'])document.addEventListener(event,e=>{const a=e.target.closest?.('a[href]');if(a)anchor(a);},true);
const handled=new WeakSet();
function resources(){
 for(const a of document.querySelectorAll('a[href]'))anchor(a);
 for(const image of document.querySelectorAll('img[src],source[src],video[src],audio[src]')){const old=image.getAttribute('src'),next=signed(old);if(old!==next)image.setAttribute('src',next);}
 for(const link of document.querySelectorAll('link[href]')){
  if(handled.has(link))continue;handled.add(link);
  const href=signed(link.getAttribute('href'));
  if(link.rel==='stylesheet'&&new URL(href,document.baseURI).origin===location.origin){
   window.fetch(href).then(r=>{if(!r.ok)throw new Error('Styles unavailable');return r.text();}).then(css=>{
    const style=document.createElement('style');style.textContent=css.replace(/url\(\s*(['"]?)(.*?)\1\s*\)/g,(all,q,value)=>'url('+JSON.stringify(signed(new URL(value,href).href))+')');link.replaceWith(style);
   }).catch(()=>{link.href=href;});
  }else link.href=href;
 }
}
new MutationObserver(resources).observe(document.documentElement,{subtree:true,childList:true});
document.addEventListener('DOMContentLoaded',resources);resources();
})();
</script>'''
GATE = "if(!location.hostname.endsWith('.static.hf.space'))return;\n"
# Inspect View pages only. Inspect fetches its logs with redirect:"error", but the hosted Space
# serves large logs (stored in HF LFS/Xet) through a redirect to the HF CDN, so the log never
# loads. Follow redirects of GET/HEAD requests to this exact origin; credentials stay
# same-origin, no referrer is sent and the session is never added to the redirect target.
FOLLOW_LOG_REDIRECTS = r'''{const direct=window.fetch.bind(window);
window.fetch=(input,options)=>{
 const request=new Request(input,options);
 if(request.redirect==='error'&&(request.method==='GET'||request.method==='HEAD')&&new URL(request.url).origin===location.origin)
  return input instanceof Request?direct(new Request(input,{...options,redirect:'follow'})):direct(input,{...options,redirect:'follow'});
 return direct(input,options);
};}
'''


def private_space_navigation(document, module_paths=()):
    """Add same-origin session continuity only to a derived hosted HTML page.

    No credential values are available to this exporter. The script uses the
    visitor's existing short-lived session at runtime, without storage/logging.
    """
    if 'id="private-space-navigation"' in document:
        return document
    paths = list(module_paths)
    if any(not re.fullmatch(r'\./assets/[A-Za-z0-9_.-]+\.js', p) for p in paths):
        raise ValueError('Expected local Inspect module assets')
    script = SCRIPT.replace('MODULE_PATHS', json.dumps(paths))
    if paths:  # Inspect View: also follow same-origin redirects of log fetches (see above)
        script = script.replace(GATE, GATE + FOLLOW_LOG_REDIRECTS, 1)
    # Module imports must pass through the import map rather than a parser src.
    if paths:
        document = re.sub(r'<script\s+type="module"\s+crossorigin\s+src="(\./assets/[A-Za-z0-9_.-]+\.js)"></script>',
                          lambda m: '<script type="module">import ' + json.dumps(m[1]) + ';</script>', document)
    prefix = '<meta name="referrer" content="no-referrer">' + script
    head = re.search(r'<head(?:\s[^>]*)?>', document, re.I)
    if head:
        return document[:head.end()] + prefix + document[head.end():]
    doctype = re.match(r'<!doctype[^>]*>', document, re.I)
    at = doctype.end() if doctype else 0
    return document[:at] + prefix + document[at:]
