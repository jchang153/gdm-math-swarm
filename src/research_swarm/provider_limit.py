"""Unpaid check of an OpenRouter key's provider-side credit limit (GET /api/v1/key).

docs/plan.md "Approved spending limits": the shared ledger enforces each allowance;
a provider-side key limit is an additional safeguard. The launch gate
``provider_limit`` checks the newest ``provider-limit-<UTC>.json`` evidence
(scripts/check_provider_limit.py), but ``secrets.load_openrouter_key`` prefers an
exported ``OPENROUTER_API_KEY`` over the host .env, so that evidence may describe a
different key than the one a launch loads. The paid paths therefore call
``verify_key_limit`` with the key they actually loaded, right after loading it and
before any paid request, which binds the safeguard to that key.

``verify_key_limit(api_key, max_remaining_usd)`` raises ``ProviderLimitError`` unless
a limit is set (``limit`` not null), it does not reset (``limit_reset`` null, so it
caps total spend rather than a daily/weekly/monthly amount), ``limit_remaining`` does
not exceed ``max_remaining_usd`` (the scope's remaining ledger allowance, plus
``TOLERANCE_USD``) and the key is not expired. Because the compatibility check's
allowance ($2) and the pilot's ($20) differ, they need separate keys, or a key
limit adjusted between the two steps (configs/README.md).

The returned report (and any error) never contains the key, a key hash, the
response's ``label`` (OpenRouter returns a masked key there) or any header. This
module makes no model call and spends nothing; the only network access is the one
GET, made only when a caller passes a key.
"""
from __future__ import annotations

import datetime
import json
import re
import urllib.request
from decimal import Decimal, InvalidOperation

KEY_URL = "https://openrouter.ai/api/v1/key"
TOLERANCE_USD = Decimal("1e-9")
TIMEOUT_SECONDS = 30
# Numeric/boolean fields copied from the response. "label" is deliberately absent.
FIELDS = ("limit", "limit_remaining", "limit_reset", "usage", "usage_daily", "usage_weekly", "usage_monthly",
          "is_free_tier", "include_byok_in_limit", "expires_at")
_CREDENTIAL_LIKE = re.compile(r"\bsk-[A-Za-z0-9]")


class ProviderLimitError(RuntimeError):
    """The key's provider-side limit is missing, resets, is too large, or could not be verified.

    ``report`` is the key-free assessment when one was made (None otherwise).
    """
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def assess(data, max_remaining_usd, now=None, *, allow_unlimited=False):
    """Pure evaluation of an /api/v1/key ``data`` object (no key, label or header).

    Explicit authorization permits only an explicitly unlimited key (both limit
    fields null). It never excuses a finite limit above the remaining allowance,
    expiration, or a resetting limit. The shared ledger is mandatory either way.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cap = Decimal(str(max_remaining_usd))
    data = data if isinstance(data, dict) else {}
    report = {name: data.get(name) for name in FIELDS}
    limit, remaining, usage = data.get("limit"), data.get("limit_remaining"), data.get("usage")
    unlimited = (allow_unlimited is True and "limit" in data and "limit_remaining" in data
                 and limit is None and remaining is None)
    expires = data.get("expires_at")
    expired = False
    if expires:
        try:
            moment = datetime.datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            expired = (moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)) <= now
        except ValueError:
            expired = True  # unparsable: fail closed
    remaining_value = _decimal(remaining) if remaining is not None else None
    checks = {
        "limit_set": {"passed": limit is not None and _decimal(limit) is not None},
        "limit_does_not_reset": {"passed": data.get("limit_reset") is None, "limit_reset": data.get("limit_reset")},
        "remaining_within_allowance": {
            "passed": remaining_value is not None and remaining_value <= cap + TOLERANCE_USD,
            "limit_remaining": remaining, "max_remaining_usd": str(cap)},
        "key_not_expired": {"passed": not expired, "expires_at": expires},
    }
    if unlimited:
        for name in ("limit_set", "remaining_within_allowance"):
            checks[name].update(passed=True, waived=True,
                                reason="user authorized an unlimited key; shared ledger remains mandatory")
        # Unlimited keys require an explicit reset field; omitted metadata does
        # not establish that a provider-side spending window cannot reset.
        checks["limit_does_not_reset"]["passed"] = "limit_reset" in data and data["limit_reset"] is None
    warnings = []
    if unlimited:
        warnings.append("no provider cap; shared ledger remains mandatory")
    if usage:
        warnings.append(f"key already has usage {usage}" +
                        ("" if unlimited else "; provider-side remaining is limit minus usage"))
    if limit is not None and _decimal(limit) is not None and _decimal(limit) > cap:
        warnings.append(f"limit {limit} exceeds the allowance {cap}; only limit_remaining protects it")
    if data.get("include_byok_in_limit") is False:
        warnings.append("BYOK usage is excluded from the limit")
    return {**report, "max_remaining_usd": str(cap), "checks": checks, "warnings": warnings,
            "unlimited_key_authorized": allow_unlimited is True, "provider_limit_enforced": not unlimited,
            "all_required_passed": all(c["passed"] for c in checks.values())}


def _default_get(url, headers):
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read()


def _scrub(text, api_key):
    text = str(text)
    if api_key:
        text = text.replace(api_key, "[redacted]")
    return _CREDENTIAL_LIKE.sub("[redacted]", text)


def fetch_key_data(api_key, *, http_get=None):
    """The ``data`` object of GET /api/v1/key. ``http_get(url, headers)`` returns the body
    (bytes, str or an already-parsed dict); the default uses urllib with a 30 s timeout."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise ProviderLimitError("no API key to check")
    try:
        body = (http_get or _default_get)(KEY_URL, {"Authorization": "Bearer " + api_key})
        payload = body if isinstance(body, dict) else json.loads(body)
    except ProviderLimitError:
        raise
    except Exception as ex:  # any failure to verify blocks the paid call
        raise ProviderLimitError(f"could not query {KEY_URL}: {type(ex).__name__}: {_scrub(ex, api_key)}") from None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ProviderLimitError(f"unexpected response from {KEY_URL} (no data object)")
    return data


def key_limit_report(api_key, max_remaining_usd, *, http_get=None, now=None, allow_unlimited=False):
    """Non-raising assessment of the live key (raises only when it cannot be queried)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    report = {"endpoint": KEY_URL, "checked_at": now.astimezone(datetime.timezone.utc).isoformat(), "paid_calls": 0,
              **assess(fetch_key_data(api_key, http_get=http_get), max_remaining_usd, now,
                       allow_unlimited=allow_unlimited)}
    text = json.dumps(report, default=str)
    if (len(api_key) >= 8 and api_key in text) or _CREDENTIAL_LIKE.search(text):
        raise ProviderLimitError("the key-limit response contained a credential-like string; nothing was recorded")
    return report


def verify_key_limit(api_key, max_remaining_usd, *, http_get=None, now=None, allow_unlimited=False):
    """Live check of the key a paid path loaded; returns the key-free report or raises.

    Raises ``ProviderLimitError`` when the key has no limit, a resetting limit, a
    ``limit_remaining`` above ``max_remaining_usd`` (+``TOLERANCE_USD``), an expiry in
    the past, or when the endpoint cannot be queried. ``allow_unlimited=True``
    explicitly waives only the two cap checks for an unlimited key, records that
    safeguard's absence, and leaves the shared ledger mandatory. Unpaid GET.
    """
    report = key_limit_report(api_key, max_remaining_usd, http_get=http_get, now=now,
                              allow_unlimited=allow_unlimited)
    if not report["all_required_passed"]:
        failed = [name for name, check in report["checks"].items() if not check["passed"]]
        raise ProviderLimitError(
            f"provider-side key limit refused ({', '.join(failed)}): limit {report['limit']}, limit_remaining "
            f"{report['limit_remaining']}, limit_reset {report['limit_reset']}, allowed remaining at most "
            f"{report['max_remaining_usd']}", report)
    return report
