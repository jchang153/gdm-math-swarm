#!/usr/bin/env python3
"""Freeze the pinned OpenRouter route's public pricing. Unpaid; no credential is read.

Fetches the public endpoint catalog, validates the pinned route against the
ProviderSettings that the run config describes (provider.settings_from_config:
max time-dependent prices, extra price components, context, tools/reasoning
support), archives the raw catalog under catalog-snapshots/<sha256>.json and writes
an I6 evidence record data/validation/route-pricing-<UTC>.json. Launch gates
re-validate that snapshot offline and require the record to be fresh (default 72 h).

    .venv/bin/python scripts/freeze_route.py [--config configs/pilot.json]
"""
import argparse, datetime, hashlib, json, sys, urllib.request
from dataclasses import asdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_swarm.launch_gates import write_evidence  # noqa: E402
from research_swarm.provider import (route_cache_read_bound, route_price_bounds, settings_from_config,  # noqa: E402
                                     validate_catalog)

URL = "https://openrouter.ai/api/v1/models/{model}/endpoints"

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "pilot.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "validation")
    args = parser.parse_args()
    settings = settings_from_config(json.loads(args.config.read_text()))
    if not settings.provider_route:
        parser.error("config provider_route is not set")
    request = urllib.request.Request(URL.format(model=settings.model_id), headers={"User-Agent": "gdm-math-swarm-route-freeze"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    catalog = json.loads(raw)
    now = datetime.datetime.now(datetime.timezone.utc)
    record = {"kind": "public_route_pricing_freeze", "retrieved_utc": now.isoformat(),
              "url": URL.format(model=settings.model_id), "catalog_sha256": hashlib.sha256(raw).hexdigest(),
              "credential_used": False, "paid_calls": 0, "config": str(args.config), "settings": asdict(settings)}
    try:
        endpoint = validate_catalog(catalog, settings)
        prompt, completion = route_price_bounds(endpoint)
        cache_read = route_cache_read_bound(endpoint)
        record.update(valid=True, endpoint={k: endpoint.get(k) for k in ("tag", "provider_name", "status", "context_length", "max_completion_tokens", "pricing", "supported_parameters", "quantization")},
                      max_prompt_usd_per_token=str(prompt), max_completion_usd_per_token=str(completion),
                      max_cache_read_usd_per_token=None if cache_read is None else str(cache_read))
    except ValueError as error:
        record.update(valid=False, reason=str(error))
    record["all_required_passed"] = record["valid"]
    (args.output_dir / "catalog-snapshots").mkdir(parents=True, exist_ok=True)
    (args.output_dir / "catalog-snapshots" / f"{record['catalog_sha256']}.json").write_bytes(raw)
    path = write_evidence(args.output_dir, "route-pricing", record, now)
    print(json.dumps({"path": str(path), "valid": record["valid"], "reason": record.get("reason")}))
    return 0 if record["valid"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
