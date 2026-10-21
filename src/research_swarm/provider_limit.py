"""Unpaid, informational record of an OpenRouter key's provider-side limit (GET /api/v1/key).

Owner decision (docs/plan.md, "One per-run budget, no manual approvals (October 20, 2026)"):
the run's own budget, enforced by the ledger on every request, is the one spending control.
The key's provider-side limit is recorded, never enforced: ``key_limit_record`` never raises
and never refuses a key (no limit, a resetting limit, a remaining amount above the run's
budget, an expiry or a failed query only add warnings). Every paid path calls it right after
loading its key and before any paid request, so each run records the key it actually used
(``secrets.load_openrouter_key`` prefers an exported ``OPENROUTER_API_KEY`` over the host
.env), and ``write_record`` refreshes ``data/validation/provider-limit-<UTC>.json``
automatically (the ``provider_limit`` launch gate only reports it).

The record (and any error text) never contains the key, a key hash, the response's ``label``
(OpenRouter returns a masked key there) or any header. This module makes no model call and
spends nothing; the only network access is the one GET, made only when a caller passes a key.
"""
from __future__ import annotations

import datetime
import json
import re
import urllib.request
from decimal import Decimal, InvalidOperation

KEY_URL = "https://openrouter.ai/api/v1/key"
TIMEOUT_SECONDS = 30
# Numeric/boolean fields copied from the response. "label" is deliberately absent.
FIELDS = ("limit", "limit_remaining", "limit_reset", "usage", "usage_daily", "usage_weekly", "usage_monthly",
          "is_free_tier", "include_byok_in_limit", "expires_at")
_CREDENTIAL_LIKE = re.compile(r"\bsk-[A-Za-z0-9]")


class ProviderLimitError(RuntimeError):
    """The key endpoint could not be queried or answered unexpectedly (``fetch_key_data``)."""


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def assess(data, run_budget_usd=None, now=None):
    """Pure, informational assessment of an /api/v1/key ``data`` object (no key, label or header).

    ``checks`` are facts with ``passed`` flags; each one not passed adds a warning. Nothing is
    required: ``all_required_passed`` is always true and nothing here refuses a key."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    budget = _decimal(run_budget_usd) if run_budget_usd is not None else None
    data = data if isinstance(data, dict) else {}
    report = {name: data.get(name) for name in FIELDS}
    limit, remaining, usage = data.get("limit"), data.get("limit_remaining"), data.get("usage")
    expires = data.get("expires_at")
    expired = False
    if expires:
        try:
            moment = datetime.datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            expired = (moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)) <= now
        except ValueError:
            expired = True
    remaining_value = _decimal(remaining) if remaining is not None else None
    checks = {
        "limit_set": {"passed": limit is not None and _decimal(limit) is not None},
        "limit_does_not_reset": {"passed": data.get("limit_reset") is None, "limit_reset": data.get("limit_reset")},
        "key_not_expired": {"passed": not expired, "expires_at": expires},
    }
    if budget is not None:
        checks["remaining_within_run_budget"] = {
            "passed": remaining_value is not None and remaining_value <= budget,
            "limit_remaining": remaining, "run_budget_usd": str(budget)}
    warnings = []
    if not checks["limit_set"]["passed"]:
        warnings.append("the key has no provider-side limit (the run's budget is the spending control)")
    if not checks["limit_does_not_reset"]["passed"]:
        warnings.append(f"the key's limit resets ({data.get('limit_reset')})")
    if expired:
        warnings.append(f"the key expired or its expiry is unreadable ({expires})")
    if budget is not None and remaining_value is not None and remaining_value > budget:
        warnings.append(f"limit_remaining {remaining} is above this run's budget {budget} (informational)")
    elif budget is not None and remaining_value is not None and remaining_value < budget:
        warnings.append(f"limit_remaining {remaining} is below this run's budget {budget}: the provider would stop "
                        "the run first")
    if usage:
        warnings.append(f"key already has usage {usage}")
    if data.get("include_byok_in_limit") is False:
        warnings.append("BYOK usage is excluded from the limit")
    return {**report, "run_budget_usd": None if budget is None else str(budget), "checks": checks,
            "warnings": warnings, "informational": True, "all_required_passed": True}


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
    except Exception as ex:
        raise ProviderLimitError(f"could not query {KEY_URL}: {type(ex).__name__}: {_scrub(ex, api_key)}") from None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ProviderLimitError(f"unexpected response from {KEY_URL} (no data object)")
    return data


def key_limit_record(api_key, run_budget_usd=None, *, http_get=None, now=None):
    """Informational record of the live key; never raises for the key's limit policy or for a
    failed query (the failure is recorded as ``error`` with a warning). Unpaid GET."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    record = {"endpoint": KEY_URL, "checked_at": now.astimezone(datetime.timezone.utc).isoformat(), "paid_calls": 0}
    try:
        record.update(assess(fetch_key_data(api_key, http_get=http_get), run_budget_usd, now))
    except ProviderLimitError as ex:
        record.update({name: None for name in FIELDS}, checks={}, informational=True, all_required_passed=True,
                      run_budget_usd=None if run_budget_usd is None else str(run_budget_usd),
                      error=_scrub(ex, api_key)[:500], warnings=[f"provider-side limit not checked: {_scrub(ex, api_key)}"[:500]])
    text = json.dumps(record, default=str)
    if (isinstance(api_key, str) and len(api_key) >= 8 and api_key in text) or _CREDENTIAL_LIKE.search(text):
        return {"endpoint": KEY_URL, "checked_at": record["checked_at"], "paid_calls": 0, "informational": True,
                "all_required_passed": True, "error": "the key-limit response contained a credential-like string; "
                "nothing from it was recorded", "warnings": ["provider-side limit not recorded"]}
    return record


def write_record(validation_dir, record, *, source):
    """Write ``record`` as ``provider-limit-<UTC>.json`` evidence (key-free, informational);
    returns the path or None when writing failed (never raises: the record is informational)."""
    try:
        from .launch_gates import write_evidence
        return write_evidence(validation_dir, "provider-limit",
                              {"kind": "provider_side_limit_record", "source": source, "credential_used": True,
                               "credential_recorded": False, **record})
    except Exception:  # noqa: BLE001 - an informational record never stops a run
        return None
