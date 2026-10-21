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
    live/consistency-journal.jsonl          each adoption decision as it is made (a killed invocation's
                                            decisions survive into the next consistency.json)

A store named by ``--warm-from`` outside the judge directory is only read: its consistency record
and journal are written to ``<judge dir>/warm-consistency.json`` and ``warm-consistency-journal.jsonl``.

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

A refused request is never resent identically (judge revision v3, item 6), so a live unit whose
request met a refusal is never simply dropped by these guards (``WarmStore.refused_twin``,
``Pipeline._carry_refusal``): when a guard rejects it (or the store is disabled), but the request
body the post-run judge would send now is byte-identical to the one the live pass sent (the
archived ``body_sha256`` of its initial request), its recorded provider outcomes are carried into
``units/`` (``refusal_carried_from``) and the post-run judge's own flow decides from them, sending
only what they lack (the one retry of a refusal whose retry was not admitted, or a repair), never
the refused request again. A request that is not byte-identical is a different request and is
sent as usual.

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


def relative_key(path, judge_dir):
    """How adoption records name a file: its path relative to the judge directory, or its absolute
    path when it lies outside (a store named by ``--warm-from``)."""
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(Path(judge_dir).resolve()))
    except ValueError:
        return str(resolved)


def body_sha256(body):
    """SHA-256 of a request body exactly as ``JudgeClient`` sends it (and archives ``body_sha256``)."""
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def refused_attempts(unit):
    """Refused attempts recorded in a unit's steps (judge revision v3, item 6)."""
    return sum(int(((step or {}).get("client") or {}).get("refusals") or 0) for step in (unit or {}).get("steps") or [])


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


SET_ASIDE = ("*.json.failed-*", "*.json.code-changed-*")   # live units set aside by live.send_requests


def adopted_sources(judge_dir):
    """``{live unit key: judge-relative unit path}`` of every unit under ``units/`` copied from a live
    unit (``adopted_from``, or ``refusal_carried_from``), keys as ``relative_key`` names them."""
    out = {}
    units = Path(judge_dir) / "units"
    for path in sorted(units.rglob("*.json")) if units.is_dir() else []:
        unit = _read(path) if path.is_file() else None
        record = (unit or {}).get("adopted_from") or (unit or {}).get("refusal_carried_from") \
            if isinstance(unit, dict) else None
        if isinstance(record, dict) and record.get("path"):
            out[str(record["path"])] = relative_key(path, judge_dir)
    return out


def live_spend(judge_dir, live_dir=None):
    """What the live passes paid for, split by adoption: ``{"units", "settled_usd", "requests",
    "uncertain_requests", "adopted_units", "adopted_settled_usd", "unadopted_units",
    "set_aside_units", "unadopted_settled_usd"}``. An adopted unit's cost (or a carried refusal's)
    is already in the post-run judge's own spend (its copy in ``units/``); ``unadopted_settled_usd``
    (units never adopted, and live units set aside as ``*.json.failed-*`` or
    ``*.json.code-changed-*`` and sent again) adds to the run's judging cost (the ledger's per-run
    total, ``client.committed_for_run``, counts everything once). ``live_dir`` may lie outside
    ``judge_dir`` (``--warm-from``)."""
    judge_dir = Path(judge_dir)
    live_dir = Path(live_dir) if live_dir else judge_dir / "live"
    out = {"units": 0, "settled_usd": "0", "requests": 0, "uncertain_requests": 0, "adopted_units": 0,
           "adopted_settled_usd": "0", "unadopted_units": 0, "set_aside_units": 0, "unadopted_settled_usd": "0"}
    folder = live_dir / "units"
    if not folder.is_dir():
        return out
    adopted = adopted_sources(judge_dir)
    totals = {"all": Decimal(0), "adopted": Decimal(0)}
    current = sorted(folder.rglob("*.json"))
    set_aside = sorted({p for pattern in SET_ASIDE for p in folder.rglob(pattern)})
    for path in current + set_aside:
        unit = _read(path) or {}
        if not isinstance(unit, dict) or not unit.get("schema"):
            continue
        settled, requests, uncertain = unit_settlements(unit)
        out["units"] += 1
        out["requests"] += requests
        out["uncertain_requests"] += uncertain
        totals["all"] += settled
        if path.suffix != ".json":
            out["set_aside_units"] += 1
        elif relative_key(path, judge_dir) in adopted:
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
        self.carried = {}        # target path -> live unit whose refused request's outcomes were carried
        self._index = None
        # The store's own record (live/consistency.json) when it is the judge directory's; a store
        # named by --warm-from outside the judge directory is only read.
        inside = self.folder.is_relative_to(self.judge_dir)
        self.record_path = self.folder / "consistency.json" if inside else self.judge_dir / "warm-consistency.json"
        self.journal_path = (self.folder / "consistency-journal.jsonl" if inside
                             else self.judge_dir / "warm-consistency-journal.jsonl")

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
        return relative_key(path, self.judge_dir)

    def _journal(self, decision, target, **fields):
        """Append one decision to the journal at once, so the adoptions and rejections of an
        invocation killed before ``finish`` still reach consistency.json."""
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"at": _now(), "decision": decision, "target": target, **fields},
                                        sort_keys=True) + "\n")
        except OSError:
            pass   # the in-memory record still reaches finish()

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
                self._journal("screened_for_another_request", target, others=others)
            else:
                self.not_found.append(target)
                self._journal("not_screened_live", target)
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
        entry = {"source": self._relative(source), "reason": reason}
        self.rejected.setdefault(self._relative(path), []).append(entry)
        self._journal("rejected", self._relative(path), **entry)

    def refused_twin(self, path, input_sha, *, purpose, model_id, mode):
        """``(unit, live path)`` of the live unit made for exactly this request (request hash,
        purpose, model and mode) that met a refusal, whatever the store-wide and code guards say
        (``Pipeline._carry_refusal`` then checks that the request body is byte-identical), else None."""
        if purpose != "prefilter":
            return None
        path = Path(path)
        source = live_unit_path(self.folder, path.parent.parent.name, path.stem, input_sha)
        unit = _read(source) if source.exists() else None
        if not isinstance(unit, dict) or unit.get("input_sha256") != input_sha or unit.get("purpose") != purpose \
                or unit.get("model_id") != model_id or unit.get("mode") != mode or not refused_attempts(unit):
            return None
        return unit, source

    def sent_body_sha256(self, unit):
        """The ``body_sha256`` every archived request of ``unit``'s initial step was sent with, or
        None when it cannot be read or the attempts differ (then nothing is carried)."""
        found = set()
        for step in unit.get("steps") or []:
            if step.get("kind") != "initial":
                continue
            for raw in (step.get("client") or {}).get("raw_paths") or []:
                if not str(raw).endswith(".request.json"):
                    continue
                record = _read(raw if Path(raw).is_absolute() else self.folder / raw)
                if not isinstance(record, dict) or not record.get("body_sha256"):
                    return None
                found.add(record["body_sha256"])
        return found.pop() if len(found) == 1 else None

    def _copy(self, unit):
        """A copy of a live unit with its raw archive paths re-rooted under the live directory."""
        prefix = self.folder.relative_to(self.judge_dir) if self.folder.is_relative_to(self.judge_dir) else None
        copy = json.loads(json.dumps(unit))
        for step in copy.get("steps") or []:
            client = step.get("client") or {}
            client["raw_paths"] = [str(prefix / p) if prefix is not None and not Path(p).is_absolute()
                                   else str(self.folder / p) for p in client.get("raw_paths") or []]
        return copy

    def _provenance(self, source, unit):
        return {"path": self._relative(source), "at": _now(),
                "screened_at": (unit.get("live") or {}).get("screened_at"),
                "code_sha256": (unit.get("live") or {}).get("code_sha256")}

    def adopt(self, path, source, unit):
        """Copy ``unit`` (from ``source``) to ``path`` with ``adopted_from`` and its raw archive paths
        re-rooted under the live directory; returns the copy."""
        copy = self._copy(unit)
        copy["adopted_from"] = self._provenance(source, unit)
        from .pipeline import write_json
        write_json(path, copy)
        self.adopted[self._relative(path)] = self._relative(source)
        self._journal("adopted", self._relative(path), source=self._relative(source))
        return copy

    def carry(self, path, source, unit, fresh):
        """The refused live ``unit`` (from ``source``), not adopted, whose request is byte-identical:
        ``fresh`` (a new pending unit for this request) with the live unit's recorded steps, saved at
        ``path`` with ``refusal_carried_from``. The post-run judge's own flow then decides from those
        steps and sends only what they lack (never the refused request again)."""
        carried = dict(fresh, steps=self._copy(unit).get("steps") or [], status="pending")
        if unit.get("subjects") is not None:
            carried.setdefault("subjects", list(unit["subjects"]))
        carried["refusal_carried_from"] = dict(self._provenance(source, unit), live_status=unit.get("status"),
                                               live_reason=unit.get("reason"), refused_attempts=refused_attempts(unit))
        from .pipeline import write_json
        write_json(path, carried)
        self.carried[self._relative(path)] = self._relative(source)
        self._journal("refusal_carried", self._relative(path), source=self._relative(source))
        return carried

    def _journal_entries(self):
        entries = []
        try:
            lines = self.journal_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return entries
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue   # a line cut short by a killed invocation
            if isinstance(entry, dict) and entry.get("target"):
                entries.append(entry)
        return entries

    def finish(self):
        """Write the consistency record (live/consistency.json) and return its summary (also kept in
        state.json). Several invocations of one judge directory (the early model stages, then the
        post-run resume) add to one record: a unit adopted by an earlier invocation stays adopted,
        and a window decided then is not counted again. The record is rebuilt from durable facts as
        well (the journal of decisions, and the ``adopted_from`` and ``refusal_carried_from`` of the
        units in ``units/``), so an invocation killed before this point loses nothing."""
        earlier = (_read(self.record_path) or {}).get("detail") or {}
        adopted, carried = dict(earlier.get("adopted") or {}), dict(earlier.get("refusals_carried") or {})
        rejected = {k: list(v) for k, v in (earlier.get("rejected") or {}).items()}
        mismatched = dict(earlier.get("screened_for_another_request") or {})
        not_found = set(earlier.get("not_screened_live") or [])
        for entry in self._journal_entries():
            target, decision = entry["target"], entry.get("decision")
            if decision == "adopted":
                adopted[target] = entry.get("source")
            elif decision == "refusal_carried":
                carried[target] = entry.get("source")
            elif decision == "rejected":
                row = {"source": entry.get("source"), "reason": entry.get("reason")}
                if row not in rejected.setdefault(target, []):
                    rejected[target].append(row)
            elif decision == "screened_for_another_request":
                mismatched[target] = entry.get("others") or []
            elif decision == "not_screened_live":
                not_found.add(target)
        for source, target in adopted_sources(self.judge_dir).items():   # the units themselves
            unit = _read(self.judge_dir / target) or {}
            (adopted if unit.get("adopted_from") else carried).setdefault(target, source)
        adopted.update(self.adopted)
        carried.update(self.carried)
        for target, rows in self.rejected.items():
            for row in rows:
                if row not in rejected.setdefault(target, []):
                    rejected[target].append(row)
        mismatched.update(self.mismatched)
        not_found = sorted((not_found | set(self.not_found)) - set(adopted) - set(carried))
        rejected = {k: v for k, v in rejected.items() if k not in adopted}
        record = {"schema": CONSISTENCY_SCHEMA, "written_at": _now(), "enabled": self.enabled,
                  "disabled_reason": self.disabled_reason, "code_sha256": self.code_hash,
                  "store": str(self.folder),
                  "adopted": len(adopted), "rejected": sum(len(v) for v in rejected.values()),
                  "refusals_carried": len(carried),
                  "windows_not_screened_live": len(not_found),
                  "windows_screened_for_another_request": len(mismatched),
                  "spend": live_spend(self.judge_dir, self.folder),
                  "detail": {"adopted": dict(sorted(adopted.items())), "rejected": dict(sorted(rejected.items())),
                             "refusals_carried": dict(sorted(carried.items())),
                             "screened_for_another_request": dict(sorted(mismatched.items())),
                             "not_screened_live": not_found}}
        from .pipeline import write_json
        if self.record_path.parent.is_dir():
            write_json(self.record_path, record)
        return {k: v for k, v in record.items() if k != "detail"}
