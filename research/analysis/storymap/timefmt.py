"""Run times as shown on the story-map pages: hours, minutes and seconds (owner, November 17, 2026: "all story maps
should list times in terms of hour / min / sec instead of like '7.66' minutes").

Records keep times as minutes from run start; only the display changes. One compact format everywhere:
``45s``, ``7m 40s``, ``1h 03m 12s`` (seconds rounded; minutes and seconds two-digit after a larger unit). Axis ticks
are whole minutes: ``0m``, ``30m``, ``1h``, ``1h 30m``. ``FMT_JS`` defines the same two formatters for the pages'
scripts (``fmtHMS``, ``fmtTick``); tests check both agree.
"""

import re

__all__ = ["fmt_hms", "fmt_tick", "fmt_span", "fmt_minutes_in_text", "fmt_times_in_prose", "FMT_JS"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fmt_hms(minutes, clamp=True):
    """Minutes from run start as '45s', '7m 40s' or '1h 03m 12s' (negative values clamp to 0 unless ``clamp`` is
    False, which shows them with a minus sign)."""
    m = _num(minutes)
    neg = m < 0 and not clamp
    if m < 0:
        m = 0.0 if clamp else -m
    total = int(m * 60 + 0.5)  # half up, as Math.round in FMT_JS
    h, rest = divmod(total, 3600)
    mm, s = divmod(rest, 60)
    sign = "−" if neg and total else ""
    if h:
        return f"{sign}{h}h {mm:02d}m {s:02d}s"
    if mm:
        return f"{sign}{mm}m {s:02d}s"
    return f"{sign}{s}s"


def fmt_tick(minutes):
    """An axis tick (whole minutes): '0m', '30m', '1h', '1h 30m'."""
    total = int(max(_num(minutes), 0.0) + 0.5)
    h, mm = divmod(total, 60)
    if not h:
        return f"{mm}m"
    return f"{h}h" if not mm else f"{h}h {mm:02d}m"


def fmt_span(a, b):
    return f"{fmt_hms(a)}–{fmt_hms(b)}"


_MIN_IN_TEXT = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?) ?(?:min|mins|minutes?)\b(?! ?\d)")


def fmt_minutes_in_text(text):
    """Decimal-minute phrases in generated text ('7.66 min', '12.3 minutes') as '7m 40s'; whole-minute phrases such as
    '2 minutes' are left alone unless they carry a decimal point."""
    if text is None:
        return text
    return _MIN_IN_TEXT.sub(lambda m: fmt_hms(float(m.group(1))) if "." in m.group(1) else m.group(0), str(text))


_RANGE_IN_TEXT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?) ?[\u2013-] ?(\d+(?:\.\d+)?) ?(?:min|mins|minutes)\b")
_AT_IN_TEXT = re.compile(r"\b(at|At|by|By|after|After|until|Until|before|Before|around|Around|near|since|Since) "
                         r"(\d+\.\d+)(?![\d.]|\s*(?:%|s\b|sec|seconds?\b|h\b|hours?\b|x\b))")


def fmt_times_in_prose(text):
    """Model-written prose (story-map stories, notes, flags): run minutes as h/m/s. Covers 'At 8.913–10.231
    minutes' (a range), '7.66 min' and a bare decimal minute after a time word ('at 11.969', 'by 40.5'); integers
    without a unit and agent quotes are never touched (callers pass prose fields only)."""
    if not isinstance(text, str):
        return text

    def rng(m):
        a, b = m.group(1), m.group(2)
        return f"{fmt_hms(float(a))}\u2013{fmt_hms(float(b))}" if "." in a + b else m.group(0)
    text = _RANGE_IN_TEXT.sub(rng, text)
    text = fmt_minutes_in_text(text)
    return _AT_IN_TEXT.sub(lambda m: f"{m.group(1)} {fmt_hms(float(m.group(2)))}", text)


FMT_JS = r"""
function fmtHMS(m,keepSign){m=+m||0;const neg=m<0&&keepSign;if(m<0)m=keepSign?-m:0;const T=Math.round(m*60),
h=Math.floor(T/3600),mm=Math.floor(T%3600/60),s=T%60,p=v=>String(v).padStart(2,'0'),g=neg&&T?'−':'';
return h?g+h+'h '+p(mm)+'m '+p(s)+'s':mm?g+mm+'m '+p(s)+'s':g+s+'s';}
function fmtTick(m){const T=Math.round(Math.max(+m||0,0)),h=Math.floor(T/60),mm=T%60;
return !h?mm+'m':mm?h+'h '+String(mm).padStart(2,'0')+'m':h+'h';}
"""
