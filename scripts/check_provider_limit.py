#!/usr/bin/env python3
"""Unpaid check of the OpenRouter key's provider-side credit limit (GET /api/v1/key).

This makes no model call and costs nothing, but it DOES load the host credential
in this process, so it refuses to run without --i-have-user-approval, which may be
passed only after the user has explicitly approved this specific check in chat.

Writes data/validation/provider-limit-<UTC>.json (interface I6) for the launch
gates. The record holds only limit/usage numbers and check results: never the key,
a key hash, the key label (OpenRouter returns a masked key there) or any header.
The assessment is ``research_swarm.provider_limit`` (shared with the paid paths).

Required (all_required_passed): a credit limit is set; it does not reset
(limit_reset is null, so it caps total spend rather than a daily/weekly/monthly
amount); limit_remaining does not exceed --max-limit-usd (default: the pilot cap);
the key is not expired. Flagged as warnings: prior usage on the key, a limit above
the cap.

With explicit user authorization, --allow-unlimited permits a key whose limit and
limit_remaining are explicitly null. It records that no provider cap exists and
that the shared ledger remains mandatory; expiry and reset checks still apply.

This evidence describes whichever key ``secrets.load_openrouter_key`` loaded in this
shell (an exported OPENROUTER_API_KEY wins over the host .env). It is not bound to
the key a later launch loads: the paid launch (runtime.run_pilot) and the paid
compatibility check therefore re-query /api/v1/key with the key they actually
loaded, before any paid request, and refuse unless limit_remaining is at most that
scope's remaining ledger allowance (provider_limit.verify_key_limit). The $2
compatibility check and the $20 pilot need separate keys, or a key limit adjusted
between the two steps (configs/README.md).

    .venv/bin/python scripts/check_provider_limit.py --i-have-user-approval
    .venv/bin/python scripts/check_provider_limit.py --i-have-user-approval --max-limit-usd 2  # compatibility key
"""
import argparse, datetime, json, sys
from decimal import Decimal
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_swarm.budget import DEFAULT_CAPS  # noqa: E402
from research_swarm.launch_gates import write_evidence  # noqa: E402
from research_swarm.provider_limit import FIELDS, KEY_URL, assess, key_limit_report  # noqa: E402,F401

URL = KEY_URL


def main(argv=None, http_get=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--i-have-user-approval", action="store_true",
                        help="the user explicitly approved loading the host credential for this unpaid check")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "validation")
    parser.add_argument("--max-limit-usd", default=str(DEFAULT_CAPS["pilot"]),
                        help="largest acceptable limit_remaining (default: the pilot cap; 2 for a compatibility key)")
    parser.add_argument("--allow-unlimited", action="store_true",
                        help="the user authorized an unlimited key; the shared spending ledger remains mandatory")
    args = parser.parse_args(argv)
    if not args.i_have_user_approval:
        parser.error("loads the host credential: pass --i-have-user-approval only after the user approves this check")
    from research_swarm.secrets import load_openrouter_key
    now = datetime.datetime.now(datetime.timezone.utc)
    report = key_limit_report(load_openrouter_key(ROOT), Decimal(args.max_limit_usd), http_get=http_get, now=now,
                              allow_unlimited=args.allow_unlimited)
    record = {"kind": "provider_side_limit_check", "credential_used": True, "credential_recorded": False, **report}
    path = write_evidence(args.output_dir, "provider-limit", record, now)
    print(json.dumps({"path": str(path), **{k: record[k] for k in ("limit", "limit_remaining", "limit_reset", "usage",
                                                                    "max_remaining_usd", "all_required_passed",
                                                                    "warnings", "unlimited_key_authorized",
                                                                    "provider_limit_enforced")}}, indent=2))
    return 0 if record["all_required_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
