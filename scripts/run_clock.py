"""One way to show a time on every generated page: run-relative clock plus absolute UTC time.

Owner request (October 25, 2026): every timestamp on the website shows both the time relative to the run
start and the absolute time, consistently, for example ``+12:34 (05:17:40 UTC)``; ``+1:02:03 (06:07:09 UTC)``
past the first hour; ``-0:05 (05:04:57 UTC)`` before the start. The run start is the runtime's
``common_start`` event (runtime.jsonl), which ``build_results.py`` records as ``results.json``
``start_utc``; ``run_start`` reads that one source (results.json first, runtime.jsonl if there is no
results.json yet) and every page uses it. Pages that show times in the browser use ``CLOCK_JS``, which
formats exactly like ``clock``.

No network, credentials or model calls.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
import re

UTC = datetime.timezone.utc
UNKNOWN = 'time unknown'
# An ISO 8601 UTC instant as the run records write it: 2026-10-22T19:22:34.656546Z or ...+00:00.
ISO_RE = re.compile(r'(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?(Z|\+00:00)')
# The page format itself (so a transform never converts a time twice).
CLOCK_RE = re.compile(r'[+\-]\d+:\d{2}(?::\d{2})? \(\d{2}:\d{2}:\d{2} UTC\)')


def epoch(value):
    """Seconds since the epoch for a number, an ISO 8601 string (``Z`` or an offset) or a datetime; None if
    the value is missing or unreadable."""
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        stamp = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).timestamp()


def offset(seconds):
    """``+12:34``, ``+1:02:03`` or ``-0:05`` for a whole number of seconds from the run start (truncated
    towards minus infinity, so an event 0.4 s before the start reads ``-0:01``)."""
    whole = int(seconds // 1)
    sign = '-' if whole < 0 else '+'
    whole = abs(whole)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{sign}{hours}:{minutes:02d}:{secs:02d}' if hours else f'{sign}{minutes}:{secs:02d}'


def absolute(value):
    """``05:17:40 UTC`` (seconds truncated), or None."""
    seconds = epoch(value)
    if seconds is None:
        return None
    return datetime.datetime.fromtimestamp(int(seconds // 1), UTC).strftime('%H:%M:%S UTC')


def clock(value, start):
    """``+12:34 (05:17:40 UTC)`` for an event time and the run start (numbers, ISO strings or datetimes);
    only the absolute part when the start is unknown, ``time unknown`` when the time is."""
    seconds = epoch(value)
    if seconds is None:
        return UNKNOWN
    begin = epoch(start)
    if begin is None:
        return absolute(seconds)
    return f'{offset(int(seconds // 1) - int(begin // 1))} ({absolute(seconds)})'


def clock_minutes(minutes, start):
    """``clock`` for a time given in minutes after the run start (the contagion and judge facts)."""
    if minutes is None:
        return UNKNOWN
    begin = epoch(start)
    if begin is None:
        return f'minute {minutes:.1f}'
    return clock(begin + float(minutes) * 60, begin)


def run_start(run_dir):
    """The run start (epoch seconds) of a saved run, or None: ``results.json`` ``start_utc`` (written by
    build_results.py from the runtime's common_start), else the first ``common_start`` in runtime.jsonl."""
    run_dir = Path(run_dir)
    results = run_dir / 'results.json'
    if results.is_file():
        try:
            found = epoch(json.loads(results.read_text()).get('start_utc'))
        except (ValueError, OSError, AttributeError):
            found = None
        if found is not None:
            return found
    runtime = run_dir / 'runtime.jsonl'
    if runtime.is_file():
        try:
            with runtime.open() as lines:
                for line in lines:
                    if '"common_start"' not in line:
                        continue
                    record = json.loads(line)
                    if record.get('kind') == 'common_start':
                        return epoch(record.get('timestamp'))
        except (ValueError, OSError):
            return None
    return None


def results_start(data):
    """The run start of a results.json document (bytes, text or dict), or None."""
    if isinstance(data, (bytes, str)):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    return epoch(data.get('start_utc')) if isinstance(data, dict) else None


def restamp_iso(text, start, *, pattern=None):
    """``text`` with every ISO UTC instant (or only those inside ``pattern``'s group 1, when given) shown as
    ``clock``. Idempotent: the page format is not an ISO instant."""
    if pattern is None:
        return ISO_RE.sub(lambda m: clock(m.group(0), start), text)
    return pattern.sub(lambda m: m.group(0).replace(m.group(1), clock(m.group(1), start)), text)


# The browser twin of ``clock``: runClock(eventSeconds, startSeconds) -> the same text. Inline, no imports.
CLOCK_JS = r'''function runClock(t,s){if(t==null||t===''||isNaN(t))return 'time unknown';
const p=n=>String(n).padStart(2,'0'),abs=x=>{const d=new Date(Math.floor(x)*1000);return p(d.getUTCHours())+':'+p(d.getUTCMinutes())+':'+p(d.getUTCSeconds())+' UTC';};
if(s==null||s===''||isNaN(s))return abs(t);
let w=Math.floor(t)-Math.floor(s);const sign=w<0?'-':'+';w=Math.abs(w);const h=Math.floor(w/3600),m=Math.floor(w%3600/60),x=w%60;
return sign+(h?h+':'+p(m)+':'+p(x):m+':'+p(x))+' ('+abs(t)+')';}'''
