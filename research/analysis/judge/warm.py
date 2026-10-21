"""Adoption of the units the live behavior judge screened during the run (researcher-only).

docs/plan.md, "Live behavior judge during the run (October 20, 2026)". While a run is going,
``live.py`` screens (Haiku prefilter) the transcript windows that can no longer change and keeps
each result as a unit file in the warm store ``<judge dir>/live/``::

    live/manifest.json                      the live judge's manifest (Pipeline.manifest() + code)
    live/units/<agent>/prefilter/<window id>@<input sha256[:16]>.json
    live/raw/                               JudgeClient archive of the live requests
    live/state.json                         every live pass (polls, windows, spend, run limit)
    live/consistency.json                   written by the post-run judge: what was adopted, and why
                                            anything screened during the run was not

A unit's file name carries the hash of its exact request, so two versions of one window (a wrong
guess about which windows were final) never collide and no unit is ever stale.

The post-run judge is unchanged except for one step (``Pipeline.unit``): when it has no unit at a
path, it adopts a live unit only if

* every guard of the store holds (``WarmStore.open``, checked once, all or nothing): the live
  manifest's binding fields (``MANIFEST_BINDING``: run, mode, settings, models, versions, rubric,
  scope) equal the post-run judge's, the pinned provider route is the same, and each live unit was
  made by judge code whose hashes (``adoption_code_hash``: pipeline, prompts, schema, validate,
  history, windows, client, route) equal the code running now;
* the unit was made for exactly this request (``input_sha256`` of system + user), purpose, model
  and mode;
* its status is ``ok`` or ``ungraded`` (a refused window is never resent identically), or
  ``pending`` with only the saved refusal whose retry was not admitted (the post-run judge then
  sends only that retry, as a resume would); ``grading_failed`` is never adopted (sent again);
* replaying its recorded outputs through the post-run judge's own decision flow
  (``Pipeline._drive_unit(replay=True)``: parse, repair, salvage) gives the same status, reason,
  result and problems without needing any request.

An adopted unit is copied (never moved) into ``units/`` with ``adopted_from`` and its raw archive
paths re-rooted under ``live/``. Everything downstream (selection, forced windows, the seeded
random sample, candidates, verification, host stages, run level, report) is computed from the
final record and the unit results exactly as before, so a run judged with adoption sends or reuses
exactly the requests a post-run-only judge would send; timing only changes which requests were
paid for early. A unit that is not adopted costs its (small) price once and is reported here.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from decimal import Decimal
from pathlib import Path

CONSISTENCY_SCHEMA = "gdm-judge-live-consistency/1"
ADOPTION_CODE = ("pipeline.py", "prompts.py", "schema.py", "validate.py", "history.py", "windows.py", "client.py",
                 "route.py")
ADOPTABLE = ("ok", "ungraded")
SHA_PREFIX = 16


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def adoption_code_hash(here=None):
    """SHA-256 over the judge modules whose code decides a unit's request, its parsing and its
    client record (``ADOPTION_CODE``). A live unit records the hash it was made under."""
    here = Path(here or Path(__file__).resolve().parent)
    digest = hashlib.sha256()
    for name in ADOPTION_CODE:
        path = here / name
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("utf-8") if path.exists() else b"absent")
    return digest.hexdigest()


def route_pin(route):
    """The part of a route record that decides which model serves a request (the provider pin);
    prices and the pricing record file may be refreshed between passes and are not compared."""
    return json.dumps((route or {}).get("provider"), sort_keys=True)


def live_unit_name(window_id, input_sha):
    """``<window id>@<first 16 hex digits of the request hash>.json``."""
    return f"{window_id}@{str(input_sha)[:SHA_PREFIX]}.json"


def live_unit_path(live_dir, agent_id, window_id, input_sha):
    return Path(live_dir) / "units" / agent_id / "prefilter" / live_unit_name(window_id, input_sha)


def binding_of(manifest):
    from .pipeline import MANIFEST_BINDING
    return {key: (manifest or {}).get(key) for key in MANIFEST_BINDING}


def unit_settlements(unit):
    """(settled USD, requests, uncertain requests) of one unit's recorded calls."""
    settled, requests, uncertain = Decimal(0), 0, 0
    for step in (unit or {}).get("steps") or []:
        for settlement in ((step or {}).get("client") or {}).get("settlements") or []:
            requests += 1
            if settlement.get("cost_usd") is None:
                uncertain += 1
            else:
                settled += Decimal(str(settlement["cost_usd"]))
    return settled, requests, uncertain


def live_spend(judge_dir, live_dir=None):
    """What the live passes paid for, split by adoption: ``{"units", "settled_usd", "requests",
    "uncertain_requests", "adopted_units", "adopted_settled_usd", "unadopted_units",
    "unadopted_settled_usd"}``. An adopted unit's cost is already in the post-run judge's own
    spend (its copy in ``units/``); only ``unadopted_settled_usd`` adds to the run's judging cost
    (the ledger's per-run total, ``client.committed_for_run``, counts everything once)."""
    judge_dir = Path(judge_dir)
    live_dir = Path(live_dir) if live_dir else judge_dir / "live"
    out = {"units": 0, "settled_usd": "0", "requests": 0, "uncertain_requests": 0, "adopted_units": 0,
           "adopted_settled_usd": "0", "unadopted_units": 0, "unadopted_settled_usd": "0"}
    folder = live_dir / "units"
    if not folder.is_dir():
        return out
    adopted = set()
    units = judge_dir / "units"
    for path in units.rglob("*.json") if units.is_dir() else []:
        record = (_read(path) or {}).get("adopted_from") if path.is_file() else None
        if isinstance(record, dict) and record.get("path"):
            adopted.add(str(record["path"]))
    totals = {"all": Decimal(0), "adopted": Decimal(0)}
    for path in sorted(folder.rglob("*.json")):
        unit = _read(path) or {}
        if not isinstance(unit, dict) or not unit.get("schema"):
            continue
        settled, requests, uncertain = unit_settlements(unit)
        out["units"] += 1
        out["requests"] += requests
        out["uncertain_requests"] += uncertain
        totals["all"] += settled
        if str(path.relative_to(judge_dir)) in adopted:
            out["adopted_units"] += 1
            totals["adopted"] += settled
        else:
            out["unadopted_units"] += 1
    out.update(settled_usd=str(totals["all"]), adopted_settled_usd=str(totals["adopted"]),
               unadopted_settled_usd=str(totals["all"] - totals["adopted"]))
    return out


class WarmStore:
    """The live units of one judge directory, opened by the post-run ``Pipeline`` (see the module
    docstring for every adoption rule). ``enabled`` is False, with ``disabled_reason``, when a
    store-wide guard fails: then nothing is adopted and every request is sent as usual."""

    def __init__(self, folder, pipeline):
        self.folder = Path(folder).resolve()
        self.pipeline = pipeline
        self.judge_dir = Path(pipeline.judge_dir).resolve()
        self.enabled = False
        self.disabled_reason = None
        self.code_hash = adoption_code_hash()
        self.adopted = {}        # judge-relative target path -> live-relative source path
        self.rejected = {}       # judge-relative target path -> [{"source", "reason"}]
        self.not_found = []      # target paths without any live unit of the same window
        self.mismatched = {}     # target path -> live units of the same window made for another request
        self._index = None

    @classmethod
    def open(cls, folder, pipeline):
        store = cls(folder, pipeline)
        store.check()
        return store

    def describe(self):
        if not self.enabled:
            return f"not adopted ({self.disabled_reason})"
        return f"adoption enabled from {self.folder}"

    def check(self):
        """The store-wide guards (binding, route pin); the code hash is checked per unit."""
        manifest = _read(self.folder / "manifest.json")
        if not isinstance(manifest, dict):
            self.disabled_reason = "no live manifest.json"
            return
        current = self.pipeline.manifest()
        differs = [key for key, value in binding_of(manifest).items() if value != binding_of(current)[key]]
        if differs:
            self.disabled_reason = "binding differs: " + ", ".join(differs)
            return
        if route_pin(manifest.get("route")) != route_pin(self.pipeline.route):
            self.disabled_reason = "provider route differs"
            return
        self.enabled = True

    def _relative(self, path):
        try:
            return str(Path(path).resolve().relative_to(self.judge_dir))
        except ValueError:
            return str(path)

    def candidate(self, path, input_sha, *, purpose, model_id, mode):
        """``(unit, live path)`` of the live unit made for exactly this request at ``path``
        (``units/<agent>/prefilter/<window>.json``), or None (recorded in the report)."""
        if not self.enabled or purpose != "prefilter":
            return None
        path = Path(path)
        agent_id, window_id = path.parent.parent.name, path.stem
        target = self._relative(path)
        source = live_unit_path(self.folder, agent_id, window_id, input_sha)
        others = sorted(p.name for p in source.parent.glob(f"{window_id}@*.json") if p != source) \
            if source.parent.is_dir() else []
        if not source.exists():
            if others:
                self.mismatched[target] = others
            else:
                self.not_found.append(target)
            return None
        unit = _read(source)
        reason = None
        if not isinstance(unit, dict):
            reason = "unreadable"
        elif unit.get("input_sha256") != input_sha:
            reason = "input_hash_differs"
        elif unit.get("purpose") != purpose or unit.get("model_id") != model_id or unit.get("mode") != mode:
            reason = "purpose_model_or_mode_differs"
        elif (unit.get("live") or {}).get("code_sha256") != self.code_hash:
            reason = "judge_code_differs"
        elif unit.get("status") == "pending":
            steps = unit.get("steps") or []
            if not (len(steps) == 1 and ((steps[0].get("client") or {}).get("error") or {}).get("reason")
                    == "refusal_retry_not_admitted"):
                reason = "pending"
        elif unit.get("status") not in ADOPTABLE:
            reason = f"status_{unit.get('status')}"
        if reason:
            self.reject(path, source, reason)
            return None
        return unit, source

    def reject(self, path, source, reason):
        self.rejected.setdefault(self._relative(path), []).append({"source": self._relative(source),
                                                                   "reason": reason})

    def adopt(self, path, source, unit):
        """Copy ``unit`` (from ``source``) to ``path`` with ``adopted_from`` and its raw archive paths
        re-rooted under the live directory; returns the copy."""
        prefix = self.folder.relative_to(self.judge_dir) if self.folder.is_relative_to(self.judge_dir) else None
        copy = json.loads(json.dumps(unit))
        for step in copy.get("steps") or []:
            client = step.get("client") or {}
            client["raw_paths"] = [str(prefix / p) if prefix is not None and not Path(p).is_absolute()
                                   else str(self.folder / p) for p in client.get("raw_paths") or []]
        copy["adopted_from"] = {"path": self._relative(source), "at": _now(),
                                "screened_at": (unit.get("live") or {}).get("screened_at"),
                                "code_sha256": (unit.get("live") or {}).get("code_sha256")}
        from .pipeline import write_json
        write_json(path, copy)
        self.adopted[self._relative(path)] = self._relative(source)
        return copy

    def finish(self):
        """Write live/consistency.json and return its summary (also kept in state.json). Several
        invocations of one judge directory (the early model stages, then the post-run resume) add to
        one record: a unit adopted by an earlier invocation stays adopted, and a window decided then
        is not counted again."""
        earlier = (_read(self.folder / "consistency.json") or {}).get("detail") or {}
        adopted = dict(earlier.get("adopted") or {}, **self.adopted)
        rejected = {k: v for k, v in dict(earlier.get("rejected") or {}, **self.rejected).items() if k not in adopted}
        mismatched = dict(earlier.get("screened_for_another_request") or {}, **self.mismatched)
        not_found = sorted(set(earlier.get("not_screened_live") or []) | set(self.not_found))
        record = {"schema": CONSISTENCY_SCHEMA, "written_at": _now(), "enabled": self.enabled,
                  "disabled_reason": self.disabled_reason, "code_sha256": self.code_hash,
                  "adopted": len(adopted), "rejected": sum(len(v) for v in rejected.values()),
                  "windows_not_screened_live": len(not_found),
                  "windows_screened_for_another_request": len(mismatched),
                  "spend": live_spend(self.judge_dir, self.folder),
                  "detail": {"adopted": dict(sorted(adopted.items())), "rejected": dict(sorted(rejected.items())),
                             "screened_for_another_request": dict(sorted(mismatched.items())),
                             "not_screened_live": not_found}}
        from .pipeline import write_json
        if self.folder.is_dir():
            write_json(self.folder / "consistency.json", record)
        return {k: v for k, v in record.items() if k != "detail"}
