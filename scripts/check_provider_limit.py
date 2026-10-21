#!/usr/bin/env python3
"""Unpaid, informational record of the OpenRouter key's provider-side limit (GET /api/v1/key).

No model call and no cost. Like every paid launch, it loads the host credential in this process
(``secrets.load_openrouter_key``) and sends one unpaid GET. Every paid launch already records the
key it loads automatically (``runtime.build_openrouter_model``); this script records it on demand.

Writes data/validation/provider-limit-<UTC>.json (interface I6), which the ``provider_limit``
launch gate reports. The record holds only limit/usage numbers, informational checks and
warnings: never the key, a key hash, the key label (OpenRouter returns a masked key there) or any
header. Nothing is refused (owner decision, docs/plan.md "One per-run budget, no manual approvals
(October 20, 2026)"): the run's budget, enforced by the ledger on every request, is the spending
control; a key without a limit, a resetting limit, or a remaining amount above the run's budget
only adds warnings.

    .venv/bin/python scripts/check_provider_limit.py [--config configs/pilot.json]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_swarm.budget import run_budget  # noqa: E402
from research_swarm.provider_limit import KEY_URL, key_limit_record, write_record  # noqa: E402

URL = KEY_URL


def main(argv=None, http_get=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "validation")
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "pilot.json",
                        help="run config whose run budget the key's remaining limit is compared with (informational)")
    args = parser.parse_args(argv)
    budget, _ = run_budget(json.loads(args.config.read_text()) if args.config.is_file() else {})
    from research_swarm.secrets import load_openrouter_key
    record = key_limit_record(load_openrouter_key(ROOT), budget, http_get=http_get)
    path = write_record(args.output_dir, record, source="scripts/check_provider_limit.py")
    print(json.dumps({"path": None if path is None else str(path),
                      **{k: record.get(k) for k in ("limit", "limit_remaining", "limit_reset", "usage",
                                                    "run_budget_usd", "warnings", "error")}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
