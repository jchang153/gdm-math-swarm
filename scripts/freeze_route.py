#!/usr/bin/env python3
"""Freeze the pinned OpenRouter route's public pricing. Unpaid; no credential is read.

Fetches the public endpoint catalog, validates the pinned route against the
ProviderSettings that the run config describes (provider.settings_from_config:
max time-dependent prices, extra price components, context, tools/reasoning
support), archives the raw catalog under catalog-snapshots/<sha256>.json and writes
an I6 evidence record data/validation/route-pricing-<UTC>.json. Launch gates
re-validate that snapshot offline. scripts/run_pilot.py and scripts/compat_check.py run
this automatically (``refresh_if_needed``, retried) when the newest record is missing, not valid,
older than ``route_pricing_max_age_hours`` (default 72) or frozen for other settings; a record that
is still stale, or whose catalog no longer validates against the config's reservation prices,
blocks the paid launch (nothing is sent).

    .venv/bin/python scripts/freeze_route.py [--config configs/pilot.json]

A config whose overseers have their own model (``overseer.model``) gets one record per model.
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

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "pilot.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "validation")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    # One freeze per model the run calls: the solvers' and, when the overseers have their own model
    # (research_swarm.oversight ``overseer.model``), the overseers'.
    from research_swarm.launch_gates import role_configs
    roles = role_configs(config)
    codes = []
    for role, role_config in roles.items():
        settings = settings_from_config(role_config)
        if not settings.provider_route:
            parser.error(f"config provider_route is not set ({role} model)")
        codes.append(freeze(args, settings, role if len(roles) > 1 else None))
    return max(codes)


def freeze(args, settings, role=None):
    """Freeze one model's route; returns 0 when the record is valid."""
    request = urllib.request.Request(URL.format(model=settings.model_id), headers={"User-Agent": "gdm-math-swarm-route-freeze"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    catalog = json.loads(raw)
    now = datetime.datetime.now(datetime.timezone.utc)
    record = {"kind": "public_route_pricing_freeze", "retrieved_utc": now.isoformat(),
              "url": URL.format(model=settings.model_id), "catalog_sha256": hashlib.sha256(raw).hexdigest(),
              "credential_used": False, "paid_calls": 0, "config": str(args.config), "settings": asdict(settings),
              **({"role": role} if role else {})}
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
    path = write_evidence(args.output_dir, "route-pricing", record, _distinct_stamp(args.output_dir, now))
    print(json.dumps({"path": str(path), "model_id": settings.model_id, "valid": record["valid"],
                      "reason": record.get("reason")}))
    return 0 if record["valid"] else 1


def _distinct_stamp(output_dir, now):
    """``now``, moved forward by whole seconds while a route-pricing record of that second exists (two
    models frozen in the same second must not overwrite each other)."""
    from research_swarm.launch_gates import evidence_name
    while (Path(output_dir) / evidence_name("route-pricing", now)).exists():
        now += datetime.timedelta(seconds=1)
    return now

REFRESH_DELAYS_SECONDS = (10, 30)   # waits before the second and third attempts


def refresh_if_needed(config, config_path, output_dir=None, *, freeze=None, out=print, sleep=None):
    """Run the unpaid freeze when the newest route-pricing record is missing, not valid, stale or
    frozen for other settings (``launch_gates.route_pricing_refresh_reason``), up to three times
    until a fresh valid record is written. Never raises: a failed refresh is reported and the launch
    gates then judge the newest record (a stale or invalid one refuses the paid launch; nothing is
    sent). Returns the reason or None."""
    import time
    from research_swarm.launch_gates import route_pricing_refresh_reason
    output_dir = Path(output_dir) if output_dir else ROOT / "data" / "validation"
    sleep = sleep or time.sleep
    reason = route_pricing_refresh_reason(config, output_dir)
    if reason is None:
        return None
    out(f"Refreshing the route-pricing freeze ({reason}; unpaid public catalog, no credential).")
    for attempt, delay in enumerate((0, *REFRESH_DELAYS_SECONDS), start=1):
        if delay:
            sleep(delay)
        try:
            code = (freeze or main)(["--config", str(config_path), "--output-dir", str(output_dir)])
            left = route_pricing_refresh_reason(config, output_dir)
            if not code and left is None:
                return reason
            out(f"Refresh attempt {attempt}: " + ("the new freeze did not validate against the config's reservation "
                                                  "prices (see its record)." if code else f"still {left}."))
        except Exception as error:  # e.g. no network: the gates judge the newest record
            out(f"Refresh attempt {attempt} failed ({type(error).__name__}: {error}).")
    return reason


if __name__ == "__main__":
    raise SystemExit(main())
