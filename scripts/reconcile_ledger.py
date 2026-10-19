#!/usr/bin/env python3
"""Reconcile uncertain ledger holds with OpenRouter's reported generation cost.

Default (dry run): lists unresolved holds in the shared ledger with what the run
directories' runtime.jsonl recorded for each (researcher-side logs): the generation
id when one exists (payload ``provider_generation_id`` or ``generation_id``), and
otherwise the details needed to find the charge by hand: agent, admission and end
timestamps (UTC), the end event, HTTP status, reason and ``provider_request_id``
(the HTTP request id of an exception-path hold, which is not a generation id).
Explicit --map REQUEST_ID=GENERATION_ID entries add generation ids. Nothing is
fetched, no credential is read and the ledger is not modified.

With --i-have-user-approval (only after the user approves this check in chat): loads
the host credential, fetches GET https://openrouter.ai/api/v1/generation?id=<id>
(unpaid) for each uncertain hold that has a generation id, settles it through
BudgetLedger.reconcile (audit row, basis provider_reported_cost) and writes an audit
file <ledger dir>/reconciliations/reconcile-<UTC>.json. Holds without a generation
id, or whose generation is not found, model-mismatched or lacks total_cost, stay
unresolved. Nothing is ever released automatically.

Judge (analysis-scope) holds: the judge records each request's generation ids in its unit
files, not in runtime.jsonl; pass them with --map, restrict the run with --scope analysis,
name the judge models with --expect-model (repeatable; default the pilot model) and point
--credentials-root at the checkout holding the host .env (default: this checkout).

Manual settlement (an exception-path hold without a generation id, after the user
has identified its charge, e.g. in the OpenRouter activity log by time and model,
and approved the amount): ``--manual REQUEST_ID COST --note TEXT`` (repeatable
--manual, one note) with --i-have-user-approval settles each named hold at COST
with basis ``manual_reconciliation`` and the note in its audit row. It never loads
a credential or contacts the network; without the approval flag it only prints the
planned settlements.

    .venv/bin/python scripts/reconcile_ledger.py --run-dir runs/pilot-...           # dry run
    .venv/bin/python scripts/reconcile_ledger.py --run-dir runs/pilot-... --i-have-user-approval
    .venv/bin/python scripts/reconcile_ledger.py --scope analysis --map REQUEST_ID=gen-... \\
        --expect-model anthropic/claude-opus-5.5 --credentials-root ~/gdm-math-swarm --i-have-user-approval
    .venv/bin/python scripts/reconcile_ledger.py --manual REQUEST_ID 0.0123 \\
        --note "OpenRouter activity 2026-10-20 14:03Z, deepseek route; user approved in chat" --i-have-user-approval
"""
from __future__ import annotations
import argparse
import datetime
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_swarm.budget import (DEFAULT_LEDGER_PATH, MANUAL_BASIS, UNRESOLVED_STATES, BudgetLedger,  # noqa: E402
                                   list_requests, read_snapshot)
from research_swarm.provider import MODEL_ID  # noqa: E402

URL = "https://openrouter.ai/api/v1/generation?id={id}"
END_EVENTS = ("inference_finished", "inference_uncertain", "inference_unbilled")
DETAIL_KEYS = ("provider_request_id", "status", "reason", "error", "provider_error", "provider_name", "provider_model",
               "stop_reason", "basis")


def _utc(timestamp):
    try:
        moment = datetime.datetime.fromtimestamp(float(timestamp), datetime.timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError, OverflowError):
        return None


def recorded_request_details(run_dirs):
    """request_id -> what runtime.jsonl recorded about it (researcher-side logs)."""
    found = {}
    for run_dir in run_dirs:
        path = Path(run_dir) / "runtime.jsonl"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") if isinstance(event, dict) else None
            if not isinstance(payload, dict) or not payload.get("request_id"):
                continue
            entry = found.setdefault(payload["request_id"], {"run_dir": str(run_dir)})
            kind = event.get("kind")
            if event.get("agent_id"):
                entry["agent_id"] = event["agent_id"]
            if kind == "inference_admitted":
                entry["admitted_at"] = _utc(event.get("timestamp"))
            elif kind in END_EVENTS:
                entry.update(ended_at=_utc(event.get("timestamp")), end_event=kind)
            for key in DETAIL_KEYS:
                if payload.get(key) is not None:
                    entry[key] = payload[key]
            generation = payload.get("provider_generation_id") or payload.get("generation_id")
            if generation:
                entry["generation_id"] = generation
    return found


def recorded_generation_ids(run_dirs):
    """request_id -> generation_id from runtime.jsonl events (researcher-side logs)."""
    return {request_id: details["generation_id"] for request_id, details in recorded_request_details(run_dirs).items()
            if details.get("generation_id")}


def describe_holds(requests, details, generation_ids):
    """Unresolved ledger holds joined with their run-log details and the next step."""
    listing = []
    for item in requests:
        entry = {**item, **{k: v for k, v in details.get(item["request_id"], {}).items() if k != "generation_id"},
                 "generation_id": generation_ids.get(item["request_id"])}
        if entry["generation_id"]:
            entry["next_step"] = "unpaid generation lookup (--i-have-user-approval)"
        else:
            entry["next_step"] = ("no generation id: find the charge by time/model (and provider_request_id, if any), "
                                  "then --manual REQUEST_ID COST --note TEXT --i-have-user-approval after user approval")
        listing.append(entry)
    return listing


def fetch_generation(generation_id, key):
    """OpenRouter generation stats ``data`` dict, or None when not (yet) found."""
    request = urllib.request.Request(URL.format(id=urllib.parse.quote(generation_id, safe="")),
                                     headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read()).get("data")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def reconcile_requests(ledger, requests, generation_ids, fetch, *, model_id=MODEL_ID, allow_reserved=False):
    """Reconcile each hold that has a generation id; returns audit entries (no secrets).
    ``model_id``: the expected model (a string, or a tuple of accepted models); a generation
    whose reported model contains none of them is left unresolved (``model_mismatch``)."""
    accepted = (model_id,) if isinstance(model_id, str) else tuple(model_id)
    entries = []
    for item in requests:
        entry = {**item, "generation_id": generation_ids.get(item["request_id"])}
        if item["state"] == "reserved" and not allow_reserved:
            entry["outcome"] = "skipped_in_flight_or_stale_reserved"
        elif not entry["generation_id"]:
            entry["outcome"] = "no_generation_id"
        else:
            try:
                data = fetch(entry["generation_id"])
            except Exception as ex:  # network/HTTP failure: leave the hold in place
                data, entry["outcome"] = None, f"fetch_failed: {type(ex).__name__}"
            if data is None:
                entry.setdefault("outcome", "generation_not_found")
            else:
                entry.update(model=data.get("model"), provider_name=data.get("provider_name"),
                             reported_total_cost=data.get("total_cost"))
                if not any(m in str(data.get("model") or "") for m in accepted):
                    entry["outcome"] = "model_mismatch"
                elif data.get("total_cost") is None:
                    entry["outcome"] = "no_total_cost"
                else:
                    result = ledger.reconcile(item["request_id"], str(data["total_cost"]), "provider_reported_cost",
                                              note=f"OpenRouter generation {entry['generation_id']} total_cost "
                                                   "(scripts/reconcile_ledger.py)",
                                              allow_reserved=allow_reserved)
                    entry.update(outcome="reconciled", new_state=result["state"])
        entries.append(entry)
    return entries


def plan_manual(manual, requests, *, allow_reserved=False):
    """Validate --manual pairs against the unresolved holds; (planned, problems). Changes nothing."""
    holds = {r["request_id"]: r for r in requests}
    allowed = UNRESOLVED_STATES if allow_reserved else ("uncertain",)
    planned, problems, seen = [], [], set()
    for request_id, cost in manual:
        try:
            amount = Decimal(cost)
            if not amount.is_finite() or amount < 0:
                raise InvalidOperation
        except InvalidOperation:
            problems.append(f"{request_id}: cost {cost!r} is not a nonnegative decimal USD amount")
            continue
        hold = holds.get(request_id)
        if request_id in seen:
            problems.append(f"{request_id}: named more than once")
        elif hold is None:
            problems.append(f"{request_id}: not an unresolved hold in this ledger (and --scope, if given)")
        elif hold["state"] not in allowed:
            problems.append(f"{request_id}: state {hold['state']} (a 'reserved' hold needs --include-stale-reserved, "
                            "only after its run has fully stopped)")
        else:
            planned.append({**hold, "manual_cost_usd": str(amount)})
        seen.add(request_id)
    return planned, problems


def settle_manual(ledger, planned, note, *, allow_reserved=False):
    """Apply validated manual settlements (basis manual_reconciliation, audit note)."""
    entries = []
    for item in planned:
        try:
            result = ledger.reconcile(item["request_id"], item["manual_cost_usd"], MANUAL_BASIS,
                                      note=f"manual reconciliation, user-approved (scripts/reconcile_ledger.py): {note}",
                                      allow_reserved=allow_reserved)
        except ValueError as ex:  # e.g. resolved concurrently since it was listed; left as it is
            entries.append({**item, "outcome": f"refused: {ex}", "new_state": None})
            continue
        entries.append({**item, "outcome": "reconciled_manually", "new_state": result["state"], "basis": MANUAL_BASIS})
    return entries


def ledger_caps(path):
    """Caps to open ``path`` with: the ablation track's own ledger keeps its own caps
    (docs/plan.md, "Prompt ablation track"); every other ledger the approved DEFAULT_CAPS."""
    from research_swarm.budget import ABLATION_CAPS, ABLATION_LEDGER_RELATIVE, read_snapshot
    if Path(path).name != ABLATION_LEDGER_RELATIVE.name:
        return None
    # Reconciliation never changes caps: open the ledger at the caps it has (before or
    # after an audited raise), falling back to ABLATION_CAPS for a scope it lacks.
    snapshot = read_snapshot(path) or {}
    return {scope: snapshot[scope]["cap_usd"] if scope in snapshot else cap for scope, cap in ABLATION_CAPS.items()}


def write_audit(ledger_path, ledger, entries, **extra):
    now = datetime.datetime.now(datetime.timezone.utc)
    audit = {"kind": "ledger_reconciliation", "created_at": now.isoformat(), "ledger": str(ledger_path),
             "credential_recorded": False, "paid_calls": 0, **extra, "entries": entries,
             "snapshot_after": ledger.snapshot()}
    folder = Path(ledger_path).parent / "reconciliations"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"reconcile-{now.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    path.write_text(json.dumps(audit, indent=2) + "\n")
    return path


def parse_map(values):
    mapping = {}
    for value in values or []:
        request_id, separator, generation_id = value.partition("=")
        if not separator or not request_id or not generation_id:
            raise SystemExit(f"--map expects REQUEST_ID=GENERATION_ID, got {value!r}")
        mapping[request_id] = generation_id
    return mapping


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH(ROOT))
    parser.add_argument("--scope", default=None, help="limit to one scope (default: all)")
    parser.add_argument("--run-dir", type=Path, action="append", default=[], help="run dir(s) with runtime.jsonl")
    parser.add_argument("--map", action="append", help="explicit REQUEST_ID=GENERATION_ID (repeatable)")
    parser.add_argument("--manual", nargs=2, action="append", metavar=("REQUEST_ID", "COST"),
                        help="settle this hold at COST USD by hand (basis manual_reconciliation; repeatable); "
                             "needs --note and --i-have-user-approval")
    parser.add_argument("--note", help="audit note for --manual: where the amount came from and the user's approval")
    parser.add_argument("--expect-model", action="append",
                        help=f"accepted generation model (repeatable; default {MODEL_ID}), e.g. the judge's "
                             "anthropic/claude-opus-5.5 and anthropic/claude-sonnet-5.5")
    parser.add_argument("--credentials-root", type=Path, default=ROOT,
                        help="checkout whose private .env holds the host key (default: this checkout)")
    parser.add_argument("--include-stale-reserved", action="store_true",
                        help="also reconcile 'reserved' holds; only after their run has fully stopped")
    parser.add_argument("--i-have-user-approval", action="store_true",
                        help="the user approved this reconciliation in chat (generation lookups load the host "
                             "credential for unpaid requests; --manual settles the named holds)")
    args = parser.parse_args(argv)
    if args.manual and not str(args.note or "").strip():
        parser.error("--manual requires --note TEXT naming the billing source and the user's approval")
    if args.note and not args.manual:
        parser.error("--note is only used with --manual")
    requests = list_requests(args.ledger, scope=args.scope)
    details = recorded_request_details(args.run_dir)
    generation_ids = {**recorded_generation_ids(args.run_dir), **parse_map(args.map)}
    print(json.dumps({"ledger": str(args.ledger), "unresolved": describe_holds(requests, details, generation_ids),
                      "snapshot": read_snapshot(args.ledger)}, indent=2))
    if args.manual:
        planned, problems = plan_manual(args.manual, requests, allow_reserved=args.include_stale_reserved)
        print(json.dumps({"planned_manual_settlements": planned, "problems": problems}, indent=2))
        if problems:
            print("Not applied: fix the problems above. Ledger unchanged.")
            return 2
        if not args.i_have_user_approval:
            print("Dry run: manual settlements not applied (needs --i-have-user-approval). Ledger unchanged.")
            return 0
        ledger = BudgetLedger(args.ledger, caps=ledger_caps(args.ledger))  # the ledger exists: every planned hold was listed from it
        entries = settle_manual(ledger, planned, args.note.strip(), allow_reserved=args.include_stale_reserved)
        path = write_audit(args.ledger, ledger, entries, mode="manual", note=args.note.strip())
        print(json.dumps({"audit": str(path), "outcomes": {e["request_id"]: e["outcome"] for e in entries}}, indent=2))
        return 0 if all(e["outcome"] == "reconciled_manually" for e in entries) else 2
    if not args.i_have_user_approval:
        print("Dry run: nothing fetched, no credential read, ledger unchanged.")
        return 0
    if not Path(args.ledger).exists():
        print("No ledger: nothing to reconcile.")
        return 0
    from research_swarm.secrets import load_openrouter_key
    key = load_openrouter_key(args.credentials_root)
    ledger = BudgetLedger(args.ledger, caps=ledger_caps(args.ledger))
    models = tuple(args.expect_model or (MODEL_ID,))
    entries = reconcile_requests(ledger, requests, generation_ids, lambda gid: fetch_generation(gid, key),
                                 model_id=models, allow_reserved=args.include_stale_reserved)
    path = write_audit(args.ledger, ledger, entries, mode="generation_lookup", expected_models=list(models),
                       scope=args.scope)
    print(json.dumps({"audit": str(path), "outcomes": {e["request_id"]: e["outcome"] for e in entries}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
