"""Privacy redaction and the privacy gate of the public experiment site (owner decision, November 17, 2026;
docs/plan.md, "Site privacy redaction (November 17, 2026)").

Run records written on the VMs carry host details that never belong on the public site: the ledger owner
(``<vm>.<zone>.c.<cloud project>.internal:<pid>``), absolute home paths of the VM user, the cloud project and VM
names (``results.json`` shutdown records), the provider key's usage figures (``provider_limit``) and, in the old
transcript behavior judge's per-agent results, a copy of the researcher-only statement-defect table.

``redact(name, data, terms)`` rewrites one site file (bundle preparation and every Cloudflare deployment apply
it): JSON records lose researcher-only objects, ``statement_defects`` and the provider usage/limit fields (the
gate's pass/fail flags stay); every text file gets host paths made repository-relative (``runs/<id>/...``) or
``~``, internal host names replaced by ``vm-host`` and the deny-listed identifiers replaced by ``<redacted>``.
Numbers and every other value stay as they were.

``violations(data, terms)`` is the gate: the names of the rules a file breaks (empty = clean).

The deny-listed identifiers themselves (cloud project, VM names) are never in tracked files: ``denylist()``
reads them at run time from the environment (``GDM_GCP_PROJECT``, ``GDM_SITE_DENYLIST``, comma-separated) and
from the Git-ignored ``HANDOFF.local.md`` (``export GDM_GCP_PROJECT=...`` / ``export GDM_SITE_DENYLIST=...``).
Only generic patterns live in this file.
"""
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / 'HANDOFF.local.md'
ENV_KEYS = ('GDM_GCP_PROJECT', 'GDM_SITE_DENYLIST')
TEXT_SUFFIXES = frozenset({'.json', '.html', '.htm', '.md', '.txt', '.jsonl', '.csv', '.js', '.svg', '.xml', '.yaml',
                           '.yml', '.log'})
REDACTED = '<redacted>'

# provider key figures (OpenRouter /api/v1/key): dropped; the gate's checks keep their pass/fail flags
PROVIDER_KEYS = frozenset({'usage', 'usage_daily', 'usage_weekly', 'usage_monthly', 'limit', 'limit_remaining',
                           'limit_reset', 'is_free_tier', 'include_byok_in_limit', 'expires_at'})
USAGE_WARNING = re.compile(r'(key already has usage )[0-9.]+')

# --- text rules (generic) -------------------------------------------------------------------------------------
_SEG = r'[^/\s"\'<>&\\]+'
HOME_REPO = re.compile(r'/home/' + _SEG + r'/(?:' + _SEG + r'/)*?gdm-math-swarm/')
USERS_REPO = re.compile(r'/Users/' + _SEG + r'/(?:' + _SEG + r'/)*?gdm-math-swarm/')
HOME_EXT = re.compile(r'/home/ext_' + _SEG)
USERS_HOME = re.compile(r'/Users/(?!alice/scans\b)[A-Za-z]' + _SEG.replace('+', '*'))
# Inspect View's bundled JavaScript documents its path encoding with the example ``/Users/alice/scans``: not a host
# path, left as it is (the bundle stays byte-identical to Inspect's)
EXT_USER = re.compile(r'\bext_[a-z0-9]+_[a-z0-9]+_edu\b')
INTERNAL_HOST = re.compile(r'\b[a-z0-9-]+(?:\.[a-z0-9-]+)?\.c\.[a-z0-9-]+\.internal\b')

_Q = r'(?:"|&quot;|&#34;|\\")'  # a JSON quote as written raw, HTML-escaped or inside a JSON string
GATE = {
    'home_ext_path': re.compile(r'/home/ext_'),
    'ext_user': EXT_USER,
    'users_path': re.compile(r'/Users/(?!alice/scans\b)[A-Za-z]'),
    'internal_host': re.compile(r'\.c\.[a-z0-9-]+\.internal\b'),
    'researcher_only': re.compile(_Q + r'researcher_only' + _Q + r'\s*:\s*true'),
    'statement_defects': re.compile(_Q + r'statement_defects' + _Q + r'\s*:'),
    'provider_usage': re.compile(_Q + r'(?:usage_daily|usage_weekly|usage_monthly|limit_remaining|limit_reset|'
                                 r'include_byok_in_limit)' + _Q + r'\s*:|key already has usage [0-9]'),
}


GATE_PROBE = {  # cheap substring tests before each pattern (sites of several GB are screened on every deployment)
    'home_ext_path': ('/home/ext_',),
    'ext_user': ('_edu',),
    'users_path': ('/Users/',),
    'internal_host': ('.internal',),
    'researcher_only': ('researcher_only',),
    'statement_defects': ('statement_defects',),
    'provider_usage': ('usage_daily', 'usage_weekly', 'usage_monthly', 'limit_remaining', 'limit_reset',
                       'byok_in_limit', 'already has usage'),
}


def denylist(env=None, local=None):
    """The real identifiers to keep off the site (cloud project, VM names), from the environment and the
    Git-ignored HANDOFF.local.md; never from tracked files. Longest first."""
    env = os.environ if env is None else env
    local = LOCAL if local is None else Path(local)
    values = [env.get(k, '') for k in ENV_KEYS]
    if local.is_file():
        text = local.read_text(errors='replace')
        for key in ENV_KEYS:
            values += re.findall(r'^\s*(?:export\s+)?' + key + r'=(\S+)', text, re.M)
    terms = {t.strip().strip('"\'') for v in values for t in v.split(',')}
    return sorted((t for t in terms if len(t) >= 4), key=lambda t: (-len(t), t))


_TERMS = {}


def _terms(terms):
    """One compiled alternation of the deny-listed identifiers (None when there are none)."""
    terms = tuple(terms)
    if terms not in _TERMS:
        _TERMS[terms] = (re.compile(r'(?<![A-Za-z0-9_-])(?:' + '|'.join(re.escape(t) for t in terms)
                                    + r')(?![A-Za-z0-9_])') if terms else None)
    return _TERMS[terms]


def is_text(name):
    return Path(name).suffix.lower() in TEXT_SUFFIXES


def redact_text(text, terms=()):
    if '/home/' in text:
        text = HOME_REPO.sub('', text)
        text = HOME_EXT.sub('~', text)
    if '/Users/' in text:
        text = USERS_REPO.sub('', text)
        text = USERS_HOME.sub('~', text)
    if '_edu' in text:
        text = EXT_USER.sub('vm-user', text)
    if '.internal' in text:
        text = INTERNAL_HOST.sub('vm-host', text)
    if any(t in text for t in terms):
        text = _terms(terms).sub(REDACTED, text)
    return text


# The one researcher-labelled object the site publishes on purpose: results.json's top-level ``seeded`` record
# (scripts/build_results.py), shown on every seeded run page as "Seeded source (researcher-only)": the label means
# hidden from the experimental agents, not from readers. Kept as it is; everything else researcher-only is removed.
PUBLISHED_RESEARCHER_LABELLED = {'results.json': frozenset({'seeded'})}


def _published_keys(name):
    return PUBLISHED_RESEARCHER_LABELLED.get(Path(name or '').name, frozenset())


def _clean(value, provider=False, keep=frozenset()):
    """(value, changed): researcher-only objects, statement_defects and provider figures removed; ``keep``: the
    top-level keys whose researcher-labelled object is published on purpose."""
    changed = False
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in keep:
                out[key] = item
                continue
            if key == 'statement_defects' or (isinstance(item, dict) and item.get('researcher_only') is True):
                changed = True
                continue
            if key == 'researcher_only' and item is True:
                changed = True
                continue
            if provider and key in PROVIDER_KEYS:
                changed = True
                continue
            item, sub = _clean(item, provider or key == 'provider_limit')
            out[key] = item
            changed |= sub
        return out, changed
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict) and item.get('researcher_only') is True:
                changed = True
                continue
            item, sub = _clean(item, provider)
            out.append(item)
            changed |= sub
        return out, changed
    if provider and isinstance(value, str) and USAGE_WARNING.search(value):
        return USAGE_WARNING.sub(r'\1(redacted)', value), True
    return value, False


def _dump(value, original):
    """JSON in the original file's layout (indent, separators, ASCII escaping) as far as it can be told."""
    text = original.lstrip()
    match = re.match(r'[\[{]\n( +|\t)', text)
    indent = match.group(1) if match else None
    ascii_only = all(ord(c) < 128 for c in original)
    separators = (',', ': ') if indent else ((', ', ': ') if '": ' in text[:2000] or ', "' in text[:2000]
                                            else (',', ':'))
    out = json.dumps(value, indent=indent, separators=separators, ensure_ascii=ascii_only)
    return out + ('\n' if original.endswith('\n') else '')


def redact(name, data, terms=()):
    """The bytes of one site file with the privacy redaction applied (unchanged bytes when nothing applies)."""
    if not is_text(name):
        return data
    try:
        text = original = data.decode('utf-8')
    except UnicodeDecodeError:
        return data
    if name.endswith('.json') and ('researcher_only' in text or 'statement_defects' in text
                                   or 'provider_limit' in text):
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        if value is not None:
            value, changed = _clean(value, keep=_published_keys(name))
            if changed:
                text = _dump(value, text)
    new = redact_text(text, terms)
    return data if new == original else new.encode('utf-8')


def _labelled_outside(value, keep):
    """Whether a researcher-only object or statement_defects occurs outside the ``keep`` top-level keys."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keep:
                continue
            if key == 'statement_defects' or (key == 'researcher_only' and item is True):
                return True
            if _labelled_outside(item, frozenset()):
                return True
    elif isinstance(value, list):
        return any(_labelled_outside(item, frozenset()) for item in value)
    return False


def violations(data, terms=(), name=None):
    """The privacy rules ``data`` breaks (generic patterns and the deny-listed identifiers); [] when clean.
    ``name``: the file's site path (results.json's published ``seeded`` record is allowed)."""
    raw = data if isinstance(data, bytes) else data.encode('utf-8')
    rules = [rule for rule, probes in GATE_PROBE.items() if any(p.encode() in raw for p in probes)]
    listed = any(t.encode() in raw for t in terms)
    if not rules and not listed:
        return []
    text = raw.decode('utf-8', 'replace')
    hits = [rule for rule in rules if GATE[rule].search(text)]
    keep = _published_keys(name)
    if keep and {'researcher_only', 'statement_defects'} & set(hits):
        try:
            outside = _labelled_outside(json.loads(text), keep)
        except ValueError:
            outside = True
        if not outside:
            hits = [h for h in hits if h not in ('researcher_only', 'statement_defects')]
    if listed and _terms(terms).search(text):
        hits.append('denylisted_identifier')
    return hits
