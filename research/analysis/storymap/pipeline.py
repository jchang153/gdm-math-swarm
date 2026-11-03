"""Story map pipeline: stage 1 (thoughts per chunk), stage 2 (reaction per event), stage 3 (story per agent).

Researcher-only (docs/plan.md, "Agent story map", proposed November 2, 2026; contract:
private/research/analysis/storymap/INTERFACE.md, section W3). Nothing here may reach an experimental agent.

Usage::

    python -m research.analysis.storymap.pipeline --run-dir runs/<id> --timeline DIR \\
        [--out runs/<id>/analysis/storymap] [--agents agent-001,agent-002] [--run-limit USD] \\
        [--stage1-model M] [--stage2-model M] [--stage3-model M] [--crosscheck-model M] \\
        [--crosscheck-fraction 0.1] [--crosscheck-seed N] [--stage1-bakeoff [--bakeoff-models M1,M2,...]] \\
        [--mock]

Stages (``StoryMap``):

1. Each agent's W1 timeline (``DIR/timeline/<agent>.jsonl``) is cut into chunks of about 25,000 estimated tokens
   at turn boundaries (``chunk.chunk_items``). Chunks are sent in order, one request each, with the ``state`` the
   previous chunk returned (a failed chunk passes on the last good state with a note that a gap precedes).
   Agents run in parallel; each model's client bounds its own concurrency (the judge's per-model defaults).
2. One request per selected event (``chunk.stage2_events``: L2 of a fake file, B1/D1 by an overseer or tagged
   ``describes_trick``/``warning``, every P2) over its window (``chunk.event_window``: 3 turns before through the
   next stage-1 decision, a D-code thought, or 10 turns).
3. One request per agent from the stage-1 and stage-2 outputs and code-derived hard facts (``chunk.hard_facts``).

Cross-check: a seeded sample (``--crosscheck-fraction``, default 0.1, ``--crosscheck-seed``) of the run's solvers
is mapped again, all three stages, by ``--crosscheck-model`` (default ``openai/gpt-5.6-terra``) into
``OUT/crosscheck/``. ``--stage1-bakeoff`` runs only stage 1 for the given agents with each of
``--bakeoff-models`` (default haiku-5.5, gpt-5.6-luna, gemini-3.8-flash) into ``OUT/bakeoff/<model>/`` (model
selection; nothing else runs).

Every reply is parsed as JSON and validated by ``storymap/schema.py`` (W2; imported lazily). A malformed reply
(unparseable, failing the schema, or truncated at ``max_tokens``) gets exactly one repair request naming the
problems; if the repair is malformed too the unit is recorded as failed (``status`` ``invalid``) and nothing is
filled in. Code checks (quotes verbatim from the items shown, item numbers in range, event and agent ids) are
soft: named in the repair, and kept as ``check_problems`` on an otherwise valid output. Refusals and API
failures are recorded as failed units with their reason (never a negative).

Spending (AGENTS.md; docs/plan.md, "One per-run budget, no manual approvals (October 20, 2026)"): every request
goes through the behaviour judge's ``JudgeClient`` and ledger. This invocation's one budget is ``--run-limit``,
else the run's ``behavior_judge_max_usd`` (RUN/settings.json), else $50; what earlier invocations into the same
``--out`` committed (``client.committed_for_judging``, ledger label run id ``storymap-<run id>``) counts against
it, and the ledger admits a request only while committed spend plus its reservation fits. There are no other
caps, approvals or confirmations. A denial waits while other requests are in flight, else stops cleanly: the
remaining units are listed ``not_run`` and a later invocation resumes.

Resume: every unit is content-addressed (``units/.../<name>-<sha256(model, effort, system, user)[:16]>.json``)
and every completed request is saved at once, so a re-run sends only what is missing (``--retry-failed`` redoes
failed units). Prompt caching: the stage system prompt (the fixed prefix: prompts and taxonomy from
private/research/analysis/storymap/) is marked with a 5-minute ``cache_control`` breakpoint on Anthropic routes;
OpenAI/Google cache it implicitly. Prefix normalization ``storymap-prefix-v1``: the system prompt is exactly the
loaded text, everything variable is in the user message.

Outputs (``OUT``): ``stage1/<agent>.jsonl`` (one row per chunk), ``stage2/<agent>.jsonl`` (one row per event),
``stage3/<agent>.json``, ``map.jsonl`` (every stage-3 row in ``OUT/stage3``), ``pipeline.json`` (models, prompt
SHA-256s, per-stage requests, tokens and cost, budget record, stop), ``units/``, ``raw/`` (request and response
archives) and ``run-budgets.json`` (paid runs).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import fcntl
import hashlib
import json
import random
import re
import signal
import sys
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from . import chunk as chunking
from .pipeline_models import (ANTHROPIC_KEYS, EXTRA_SPECS, KNOWN_MODELS, ModelPool, check_model,
                              extra_route_settings, offline_settings, slug)

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = "gdm-storymap-pipeline/1"
UNIT_SCHEMA = "gdm-storymap-unit/1"
PREFIX_NORMALIZATION = "storymap-prefix-v1"
DEFAULT_MODELS = {"stage1": "anthropic/claude-haiku-5.5", "stage2": "anthropic/claude-sonnet-5.5",
                  "stage3": "anthropic/claude-sonnet-5.5"}
DEFAULT_CROSSCHECK_MODEL = "openai/gpt-5.6-terra"
DEFAULT_CROSSCHECK_FRACTION = 0.1
DEFAULT_CROSSCHECK_SEED = 20261102
DEFAULT_BAKEOFF_MODELS = ("anthropic/claude-haiku-5.5", "openai/gpt-5.6-luna", "google/gemini-3.8-flash")
DEFAULT_RUN_LIMIT_USD = Decimal("50")
MAX_TOKENS = {1: 16000, 2: 16000, 3: 24000}
STAGE_NAMES = {1: "stage1", 2: "stage2", 3: "stage3"}
DEFAULT_AGENT_CONCURRENCY = 48   # the judge's Haiku concurrency: stage 1 is sequential within an agent
FINAL = ("ok", "invalid", "ungraded", "grading_failed")
STOP_REASONS = ("budget_admission_denied", "budget_ledger_unavailable", "run_limit")
WAITING_DENIALS = ("budget_admission_denied", "run_limit")
RESPONSE_FORMAT = {"type": "json_object"}
HEADER = "STORYMAP UNIT "


class BudgetStop(RuntimeError):
    """The ledger or the run limit admits no further request: stop cleanly."""


class PipelineError(RuntimeError):
    pass


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def write_json(path, value):
    """Atomic JSON write (temporary file then rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)
    return path


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name))[:80]


# ---------------------------------------------------------------------------
# W2 schema module (prompts, taxonomy and validators)

class SchemaAdapter:
    """What this pipeline uses of ``storymap/schema.py`` (W2): ``prompt_info(stage)`` (the rendered system prompt
    of a stage, identical for every request, with ``sha256``, ``template_sha256``, ``taxonomy_sha256`` and
    versions), ``validate_stage1(raw, items=...)``, ``validate_stage2(raw, event_id=..., items=...,
    overseer_expected=...)`` and ``validate_stage3(raw, agent=..., event_ids=...)``, which return a normalized
    output or raise ``SchemaError`` whose ``problems`` are repair-ready."""

    STAGE_KEYS = ("stage1", "stage2", "stage3")
    RECORDED = ("sha256", "template_sha256", "taxonomy_sha256", "version", "taxonomy_version", "prompts_path",
                "taxonomy_path")

    def __init__(self, module):
        self.module = module
        self._info = None

    def prompts(self):
        if self._info is None:
            self._info = {stage: self.module.prompt_info(stage) for stage in self.STAGE_KEYS}
        return {stage: info["text"] for stage, info in self._info.items()}

    def record(self):
        self.prompts()
        return {"schema_version": getattr(self.module, "SCHEMA_VERSION", None),
                **{stage: {k: info.get(k) for k in self.RECORDED} for stage, info in self._info.items()}}

    def validate(self, stage, raw, **context):
        """(normalized output or None, hard problems, soft problems). Soft problems are the schema's record
        checks (quotes verbatim, item turn/t, cited event ids, length caps): named in the one repair, and kept
        as ``check_problems`` on an otherwise valid output."""
        fn = getattr(self.module, f"validate_stage{stage}")
        soft = []
        try:
            return fn(raw, soft=soft, **context), [], soft
        except Exception as error:   # SchemaError carries repair-ready problems
            problems = getattr(error, "problems", None)
            return None, [str(p) for p in problems] if problems else [f"{type(error).__name__}: {error}"], []


def load_schema_module():
    try:
        from . import schema  # noqa: F401  (W2)
    except ImportError as error:
        raise PipelineError(f"storymap/schema.py (W2) is not importable: {error}") from None
    return schema


# ---------------------------------------------------------------------------
# Parsing

def repair_user(user, previous, problems):
    """The one repair request: the original message, the previous reply and its problems."""
    listed = "\n".join(f"- {p}" for p in problems)
    return (f"{user}\n\nYOUR PREVIOUS REPLY (rejected):\n{str(previous or '')[:20000]}\n\nPROBLEMS:\n{listed}\n\n"
            "Return the corrected, complete JSON object only (no text around it).")


def output_limit_problem(max_tokens):
    return (f"output limit: your previous reply reached the {int(max_tokens):,}-token output limit (thinking "
            "included) before it was complete; reason more briefly and return the complete JSON object")


def _truncated(step):
    return step.get("status") == "ungraded" and (step.get("error") or {}).get("reason") == "max_tokens"


# ---------------------------------------------------------------------------
# Prompts (variable part: the user message)

def unit_line(header):
    return HEADER + _dumps(header)


def section(name, body):
    body = body if isinstance(body, str) else _dumps(body)
    return f"=== {name} ===\n{body.strip() if body and body.strip() else '(none)'}"


def stage1_user(agent, chunk, chunks, state, gap, role=None):
    header = {"stage": 1, "agent": agent, "chunk": chunk["chunk"], "chunks": chunks,
              **{k: chunk[k] for k in ("first_item", "last_item", "first_turn", "last_turn", "t_from", "t_to")}}
    carried = _dumps(state) + ("\nNote: the preceding chunk could not be analysed (its output failed or was refused); "
                               "this is the state after the last chunk that was, so the agent may have changed in "
                               "between." if gap else "")
    return "\n\n".join([
        unit_line(header),
        section("AGENT", f"id={agent} role={role or 'solver'}\nchunk {chunk['chunk'] + 1}/{chunks} (items "
                         f"i={chunk['first_item']} to i={chunk['last_item']})"),
        section("CARRIED STATE", carried),
        section("CHUNK", chunking.render_items(chunk["items"]))])


def _thought_line(thought):
    note = f" note={json.dumps(thought.get('note'), ensure_ascii=False)}" if thought.get("note") else ""
    return (f"i={thought.get('item')} turn={thought.get('turn')} t={chunking._fmt_t(thought.get('t'))} "
            f"{thought.get('code')} ({thought.get('certainty')}): {json.dumps(thought.get('quote'), ensure_ascii=False)}"
            f"{note}")


def stage2_user(agent, event, window, state_before, thoughts):
    item = event["item"]
    header = {"stage": 2, "agent": agent, "event_id": event["event_id"], "event_type": event["type"],
              "event_item": item["i"], "event_t": item.get("t"), "selected_because": event["reason"],
              "overseer_expected": overseer_expected(event),
              "window": {k: window[k] for k in ("first_item", "last_item", "first_turn", "last_turn", "end_rule",
                                                "truncated")}}
    span = (f"items i={window['first_item']} to i={window['last_item']}, turns {window['first_turn']} to "
            f"{window['last_turn']}; ends at: {window['end_rule']}{', trimmed to fit' if window['truncated'] else ''}")
    return "\n\n".join([
        unit_line(header),
        section("EVENT", f"event_id={event['event_id']} agent={agent}\n{chunking.render_item(item)}"),
        section("WINDOW", f"({span})\n{chunking.render_items(window['items'])}"),
        section("STATE BEFORE", _dumps(state_before)),
        section("STAGE-1 THOUGHTS", "\n".join(_thought_line(t) for t in thoughts))])


def stage3_user(agent, facts, s1_rows, s2_rows):
    header = {"stage": 3, "agent": agent, "first_min": facts.get("first_min"), "last_min": facts.get("last_min")}
    thoughts, states = [], []
    for row in s1_rows:
        span = (f"chunk {row['chunk'] + 1}/{row['chunks']} i={row['first_item']}-{row['last_item']} "
                f"t={chunking._fmt_t(row['t_from'])}-{chunking._fmt_t(row['t_to'])}")
        if row["status"] != "ok":
            states.append(f"{span}: {row['status']} ({row.get('reason')}): no output, a gap in the record")
            continue
        states.append(f"{span}: {_dumps(row.get('state'))}")
        thoughts.extend(_thought_line(t) for t in row.get("thoughts") or [])
    reactions = []
    for row in s2_rows:
        keep = ("event_id", "event_type", "event_t", "status", "reason") if row["status"] != "ok" else \
            ("event_id", "event_type", "event_t", "registered", "change", "direction", "reason", "self_attributed",
             "overseer", "next_action", "quotes", "certainty")
        reactions.append(_dumps({k: row.get(k) for k in keep}))
    return "\n\n".join([
        unit_line(header),
        section("FACTS", f"agent={agent}\n" + _dumps(facts)),
        section("STAGE-1 THOUGHTS", "\n".join(thoughts)),
        section("STAGE-1 STATES", "\n".join(states)),
        section("STAGE-2 REACTIONS", "\n".join(reactions))])


def overseer_expected(event):
    """True for an overseer-authored event or a P2 revert (the stage-2 ``overseer`` object is then required)."""
    return event["type"] == "P2" or (event["item"].get("event") or {}).get("author_role") == "overseer"


# ---------------------------------------------------------------------------
# The pipeline

class StoryMap:
    """One invocation: units, budget handling and the three stages for a set of agents."""

    def __init__(self, *, timeline_dir, out_dir, pool, schema, run_id, mode="paid", log=None,
                 chunk_tokens=chunking.DEFAULT_CHUNK_TOKENS, window_before=chunking.WINDOW_BEFORE_TURNS,
                 window_after=chunking.WINDOW_AFTER_TURNS, window_max_tokens=chunking.WINDOW_MAX_TOKENS,
                 retry_failed=False, run_limit=None, agent_concurrency=None):
        self.timeline = chunking.timeline_folder(timeline_dir)
        self.agent_concurrency = agent_concurrency   # agents mapped at once (None: all)
        self.out_dir = Path(out_dir)
        self.pool = pool
        self.schema = schema if isinstance(schema, SchemaAdapter) else SchemaAdapter(schema)
        self.prompts = self.schema.prompts()
        self.run_id = run_id
        self.mode = mode
        self.log = log or (lambda message: None)
        self.chunk_tokens = chunk_tokens
        self.window = {"before_turns": window_before, "after_turns": window_after, "max_tokens": window_max_tokens}
        self.retry_failed = retry_failed
        self.run_limit = run_limit
        self.index = chunking.load_index(self.timeline)
        self.stop = None
        self.used = []           # {"stage", "model", "path", "reused"} of every unit this invocation touched
        self._inflight = 0
        self._completions = 0
        self._cond = None
        self.started = _now()

    # -- requests -------------------------------------------------------------
    async def _wake(self):
        async with self._cond:
            self._cond.notify_all()

    async def _admission_wait(self, reason):
        """A refused request's one retry was denied admission: wait while other requests are in flight."""
        if self.stop is not None or reason not in WAITING_DENIALS or self._inflight <= 1:
            return False
        seen = self._completions
        async with self._cond:
            await self._cond.wait_for(lambda: self._completions > seen or self._inflight <= 1 or self.stop is not None)
        return self.stop is None

    async def call(self, client, **kwargs):
        """One ClientResult; a budget denial waits while other requests are in flight (their settlement may free
        room; nothing was sent), else stops the invocation (``BudgetStop``)."""
        if self._cond is None:
            self._cond = asyncio.Condition()
        while True:
            if self.stop is not None:
                raise BudgetStop(self.stop["reason"])
            seen = self._completions
            self._inflight += 1
            try:
                result = await client.complete(effort=client.route.effort, admission_wait=self._admission_wait,
                                               **kwargs)
            except BaseException:
                self._inflight -= 1
                self._completions += 1
                await self._wake()
                raise
            self._inflight -= 1
            reason = (result.get("error") or {}).get("reason")
            if result.get("status") == "grading_failed" and reason in STOP_REASONS:
                if reason in WAITING_DENIALS and not result.get("refusals") and self._inflight > 0 \
                        and self.stop is None:
                    async with self._cond:
                        await self._cond.wait_for(lambda: self._completions > seen or self._inflight == 0
                                                  or self.stop is not None)
                    continue
                if self.stop is None:
                    self.stop = {"reason": reason, "unit_id": kwargs.get("unit_id"), "purpose": kwargs.get("purpose"),
                                 "at": _now(), "detail": result.get("error"), "in_flight_at_stop": self._inflight}
                    self.log(f"STOP: {reason} at {kwargs.get('unit_id')}; finishing in-flight requests")
                await self._wake()
                if result.get("refusals"):
                    # Refused once and the one retry could not be admitted: record it (never resent identically).
                    return {**result, "status": "ungraded",
                            "error": {**(result.get("error") or {}), "reason": "refusal_retry_not_admitted"}}
                raise BudgetStop(reason)
            self._completions += 1
            await self._wake()
            return result

    def _summary(self, result):
        out = {k: result.get(k) for k in ("status", "text", "attempts", "refusals", "refusal_details", "rate_limited",
                                          "usage", "cost_usd", "request_ids", "finish_reason", "error",
                                          "generation_ids", "provider", "model", "service_tier", "settlements",
                                          "cache")}
        paths = []
        for path in result.get("raw_paths") or []:
            try:
                paths.append(str(Path(path).resolve().relative_to(self.out_dir.resolve())))
            except ValueError:
                paths.append(str(path))
        out["raw_paths"] = paths
        out["completed_at"] = _now()
        return out

    def unit_path(self, folder, name, model_id, system, user):
        client = self.pool.get(model_id)
        sha = sha256_text("\0".join((model_id, client.route.effort, system, user)))
        return Path(folder) / f"{_safe(name)}-{sha[:16]}.json", sha

    async def unit(self, *, stage, model_id, folder, name, unit_id, system, user, context=None, parse=None):
        """One content-addressed model unit: the request, one repair on a malformed reply, the outcome."""
        path, sha = self.unit_path(folder, name, model_id, system, user)
        unit = read_json(path) if path.exists() else None
        if unit is not None and unit.get("status") in FINAL:
            if self.retry_failed and unit["status"] != "ok":
                path.rename(path.with_name(path.name + f".failed-{_safe(_now())}"))
                unit = None
            else:
                self.used.append({"stage": stage, "model": model_id, "path": path, "reused": True})
                return unit
        client = self.pool.get(model_id)
        max_tokens = MAX_TOKENS[stage]
        purpose = f"storymap_s{stage}"
        if unit is None:
            unit = {"schema": UNIT_SCHEMA, "unit_id": unit_id, "stage": stage, "purpose": purpose,
                    "model_id": model_id, "effort": client.route.effort, "input_sha256": sha,
                    "system_sha256": sha256_text(system),
                    "input_tokens_estimate": (len(system) + len(user) + 3) // 4, "max_tokens": max_tokens,
                    "status": "pending", "steps": [], "created_at": _now()}
        self.used.append({"stage": stage, "model": model_id, "path": path, "reused": False})

        async def step(kind, step_user):
            for done in unit["steps"]:
                if done["kind"] == kind:
                    return done["client"]
            result = await self.call(client, system=system, user=step_user,
                                     purpose=purpose if kind == "initial" else f"{purpose}_repair", unit_id=unit_id,
                                     max_tokens=max_tokens,
                                     response_format=RESPONSE_FORMAT if client.route.structured_outputs else None)
            unit["steps"].append({"kind": kind, "client": self._summary(result)})
            write_json(path, unit)
            return unit["steps"][-1]["client"]

        def finish(status, reason=None, result=None, problems=(), check_problems=(), **extra):
            unit.update(status=status, reason=reason, result=result, problems=list(problems),
                        check_problems=list(check_problems), completed_at=_now(), **extra)
            write_json(path, unit)
            return unit

        def evaluate(done):
            """(result, hard problems, soft problems, checks)."""
            if _truncated(done):
                return None, [output_limit_problem(max_tokens)], [], None
            if not str(done.get("text") or "").strip():
                return None, ["empty reply: return the JSON object"], [], None
            obj, hard, soft = self.schema.validate(stage, done.get("text"), **(context or {}))
            if hard:
                return None, hard, [], None
            extra, checks = parse(obj) if parse is not None else ([], None)
            return obj, [], soft + list(extra), checks

        first = await step("initial", user)
        if first.get("status") != "ok" and not _truncated(first):
            return finish(first.get("status") or "grading_failed", (first.get("error") or {}).get("reason"))
        result, hard, soft, checks = evaluate(first)
        if not hard and not soft:
            return finish("ok", None, result, checks=checks)
        first_valid = not hard
        second = await step("repair", repair_user(user, first.get("text"), hard + soft))
        if second.get("status") != "ok" and not _truncated(second):
            reason = (second.get("error") or {}).get("reason")
            if first_valid:
                return finish("ok", "first_kept_repair_failed", result, check_problems=soft, checks=checks,
                              first_problems=hard + soft, repair_reason=reason)
            return finish("invalid", f"repair_{second.get('status')}:{reason}", None, hard, first_problems=hard)
        result2, hard2, soft2, checks2 = evaluate(second)
        if not hard2:
            return finish("ok", "repaired", result2, check_problems=soft2, checks=checks2, first_problems=hard + soft)
        if first_valid:
            return finish("ok", "first_kept_repair_invalid", result, check_problems=soft, checks=checks,
                          first_problems=hard + soft, repair_problems=hard2)
        return finish("invalid", "malformed_after_repair", None, hard2, first_problems=hard)

    # -- stage rows -------------------------------------------------------------
    @staticmethod
    def _base(unit, model_id):
        status = unit.get("status")
        return {"status": row_status(status), "unit_status": status, "reason": unit.get("reason"),
                "model": model_id, "check_problems": unit.get("check_problems") or [],
                "problems": unit.get("problems") or []}

    def _not_run(self, model_id):
        return {"status": "not_run", "unit_status": None, "reason": (self.stop or {}).get("reason", "budget_stop"),
                "model": model_id, "check_problems": [], "problems": []}

    def _rel(self, path):
        try:
            return str(Path(path).resolve().relative_to(self.out_dir.resolve()))
        except ValueError:
            return str(path)

    async def stage1(self, agent, items, model_id, root, prefix=""):
        chunks = chunking.chunk_items(items, self.chunk_tokens)
        system = self.prompts["stage1"]
        rows, state, gap, stopped = [], None, False, False
        for chunk in chunks:
            meta = {"agent": agent, "chunk": chunk["chunk"], "chunks": len(chunks),
                    **{k: chunk[k] for k in ("first_item", "last_item", "first_turn", "last_turn", "t_from", "t_to",
                                             "est_tokens", "split_turn")}, "carried_gap": gap}
            if stopped:
                rows.append({**meta, **self._not_run(model_id), "thoughts": None, "state": None})
                continue
            user = stage1_user(agent, chunk, len(chunks), state, gap, (self.index.get(agent) or {}).get("role"))
            try:
                unit = await self.unit(stage=1, model_id=model_id, folder=root / "units" / "stage1" / agent,
                                       name=f"c{chunk['chunk']:03d}", unit_id=f"{prefix}{agent}:s1:c{chunk['chunk']:03d}",
                                       system=system, user=user, context={"items": chunk["items"]})
            except BudgetStop:
                stopped = True
                rows.append({**meta, **self._not_run(model_id), "thoughts": None, "state": None})
                continue
            row = {**meta, **self._base(unit, model_id), "unit": self._rel(self.unit_path(
                root / "units" / "stage1" / agent, f"c{chunk['chunk']:03d}", model_id, system, user)[0])}
            if unit["status"] == "ok":
                result = unit["result"]
                row.update(thoughts=result.get("thoughts"), state=result.get("state"))
                state, gap = result.get("state"), False
            else:
                row.update(thoughts=None, state=None)
                gap = True
            rows.append(row)
        write_jsonl(root / "stage1" / f"{agent}.jsonl", rows)
        return rows, stopped

    async def stage2(self, agent, items, s1_rows, model_id, root, prefix=""):
        events = chunking.stage2_events(items)
        thoughts = [t for r in s1_rows if r["status"] == "ok" for t in (r.get("thoughts") or []) if isinstance(t, dict)]
        system = self.prompts["stage2"]

        async def one(event):
            item = event["item"]
            window = chunking.event_window(items, item, decision_turn=chunking.decision_turn_after(thoughts, item["i"]),
                                           **self.window)
            before = [r for r in s1_rows if r["status"] == "ok" and r["last_item"] < item["i"]]
            state_before = before[-1].get("state") if before else None
            inside = [t for t in thoughts if isinstance(t.get("item"), int)
                      and window["first_item"] <= t["item"] <= window["last_item"]]
            meta = {"agent": agent, "event_id": event["event_id"], "event_type": event["type"],
                    "event_item": item["i"], "event_t": item.get("t"), "selected_because": event["reason"],
                    "window": {k: window[k] for k in ("first_item", "last_item", "first_turn", "last_turn", "end_rule",
                                                      "truncated", "est_tokens")}}
            user = stage2_user(agent, event, window, state_before, inside)
            name = f"{item['i']:06d}-{event['event_id']}"
            try:
                unit = await self.unit(stage=2, model_id=model_id, folder=root / "units" / "stage2" / agent, name=name,
                                       unit_id=f"{prefix}{agent}:s2:{event['event_id']}", system=system, user=user,
                                       context={"event_id": event["event_id"], "items": window["items"],
                                                "overseer_expected": overseer_expected(event)})
            except BudgetStop:
                return {**meta, **self._not_run(model_id)}, True
            row = {**meta, **self._base(unit, model_id)}
            if unit["status"] == "ok":
                output = dict(unit["result"])
                output.pop("event_id", None)
                row.update(output)
            return row, False
        done = await asyncio.gather(*(one(e) for e in events))
        rows = [r for r, _ in done]
        write_jsonl(root / "stage2" / f"{agent}.jsonl", rows)
        return rows, any(s for _, s in done)

    async def stage3(self, agent, items, s1_rows, s2_rows, model_id, root, prefix=""):
        facts = chunking.hard_facts(agent, items, self.index.get(agent))
        user = stage3_user(agent, facts, s1_rows, s2_rows)
        meta = {"agent": agent, "facts": facts,
                "inputs": {"stage1_chunks": len(s1_rows), "stage1_failed": sum(r["status"] != "ok" for r in s1_rows),
                           "stage2_events": len(s2_rows), "stage2_failed": sum(r["status"] != "ok" for r in s2_rows)}}
        try:
            unit = await self.unit(stage=3, model_id=model_id, folder=root / "units" / "stage3", name=agent,
                                   unit_id=f"{prefix}{agent}:s3", system=self.prompts["stage3"], user=user,
                                   context={"agent": agent, "event_ids": chunking.event_ids(items)})
        except BudgetStop:
            row = {**meta, **self._not_run(model_id)}
            write_json(root / "stage3" / f"{agent}.json", row)
            return row, True
        row = {**meta, **self._base(unit, model_id)}
        if unit["status"] == "ok":
            output = dict(unit["result"])
            output.pop("agent", None)
            row.update(output)
        write_json(root / "stage3" / f"{agent}.json", row)
        return row, False

    async def agent(self, agent, models, root, *, stages=(1, 2, 3), prefix=""):
        """Map one agent; returns ``{"agent", "stage1", "stage2", "stage3", "stopped"}`` (counts and status)."""
        items = chunking.load_items(self.timeline, agent)
        s1, stopped = await self.stage1(agent, items, models["stage1"], root, prefix)
        s2 = []
        out = {"agent": agent, "stage1": _counts(s1), "stopped": stopped}
        if 2 in stages and not stopped:
            s2, stopped = await self.stage2(agent, items, s1, models["stage2"], root, prefix)
            out.update(stage2=_counts(s2), stopped=stopped)
        if 3 in stages and not out["stopped"]:
            row, stopped = await self.stage3(agent, items, s1, s2, models["stage3"], root, prefix)
            out.update(stage3=row["status"], stopped=stopped)
        self.log(f"{prefix}{agent}: stage1 {out['stage1']}" + (f", stage2 {out.get('stage2')}" if "stage2" in out else "")
                 + (f", stage3 {out.get('stage3')}" if "stage3" in out else "") + (" (stopped)" if out["stopped"] else ""))
        return out

    # -- invocation -------------------------------------------------------------
    async def run(self, agents, *, models, crosscheck_agents=(), crosscheck_model=None, bakeoff_models=None):
        """Map every agent. At most ``agent_concurrency`` agents of each group (main, cross-check, each bake-off
        model) run at once, in agent order, so a budget stop leaves complete maps for the agents started first."""
        def bounded(coros):
            if not self.agent_concurrency:
                return list(coros)
            gate = asyncio.Semaphore(self.agent_concurrency)

            async def one(coro):
                async with gate:
                    return await coro
            return [one(c) for c in coros]
        tasks = []
        if bakeoff_models:
            for model_id in bakeoff_models:
                root = self.out_dir / "bakeoff" / slug(model_id)
                tasks += bounded(self.agent(a, {"stage1": model_id}, root, stages=(1,),
                                            prefix=f"bakeoff-{slug(model_id)}:") for a in agents)
        else:
            tasks += bounded(self.agent(a, models, self.out_dir) for a in agents)
            if crosscheck_model:
                xmodels = {k: crosscheck_model for k in models}
                tasks += bounded(self.agent(a, xmodels, self.out_dir / "crosscheck", prefix="crosscheck:")
                                 for a in crosscheck_agents)
        return await asyncio.gather(*tasks)

    def stage_stats(self):
        """Per stage and model: units, outcomes, requests, tokens and cost (recorded in the unit files)."""
        stats = {}
        for entry in self.used:
            unit = read_json(entry["path"]) or {}
            key = f"{STAGE_NAMES[entry['stage']]}:{entry['model']}"
            row = stats.setdefault(key, {"stage": STAGE_NAMES[entry["stage"]], "model": entry["model"], "units": 0,
                                         "ok": 0, "failed": 0, "pending": 0, "reused_units": 0, "repairs": 0,
                                         "requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                         "reasoning_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
                                         "cost_usd": Decimal(0), "steps_with_unknown_cost": 0,
                                         "failure_reasons": {}})
            row["units"] += 1
            row["reused_units"] += bool(entry["reused"])
            status = unit.get("status")
            if status == "ok":
                row["ok"] += 1
            elif status in FINAL:
                row["failed"] += 1
                reason = str(unit.get("reason") or status)
                row["failure_reasons"][reason] = row["failure_reasons"].get(reason, 0) + 1
            else:
                row["pending"] += 1
            for step in unit.get("steps") or []:
                client = step.get("client") or {}
                row["repairs"] += step.get("kind") == "repair"
                row["requests"] += int(client.get("attempts") or 0)
                usage = client.get("usage") or {}
                for name in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens",
                             "cache_write_tokens"):
                    row[name] += int(usage.get(name) or 0)
                if client.get("cost_usd") is None:
                    row["steps_with_unknown_cost"] += 1 if client.get("attempts") else 0
                else:
                    row["cost_usd"] += Decimal(str(client["cost_usd"]))
        for row in stats.values():
            row["cost_usd"] = str(row["cost_usd"])
        return dict(sorted(stats.items()))


def row_status(unit_status):
    """Row status of a finished unit: ``ok``; ``unresolved`` for a refused (or truncated-twice) request, which is
    never read as an empty answer; ``failed`` for a malformed output after its repair or an API failure."""
    return {"ok": "ok", "ungraded": "unresolved"}.get(unit_status, "failed")


def _counts(rows):
    out = {}
    for row in rows:
        out[row["status"]] = out.get(row["status"], 0) + 1
    return out


def crosscheck_sample(solvers, fraction, seed):
    """The seeded cross-check sample of the run's solvers (stable for a given solver list and seed)."""
    solvers = sorted(solvers)
    if fraction <= 0 or not solvers:
        return []
    k = min(len(solvers), max(1, round(fraction * len(solvers))))
    return sorted(random.Random(seed).sample(solvers, k))


def rebuild_map(out_dir):
    """``map.jsonl``: every stage-3 row in ``out_dir/stage3`` (agent order), without the hard facts."""
    rows = []
    for path in sorted((Path(out_dir) / "stage3").glob("agent-*.json")):
        row = read_json(path)
        rows.append({k: v for k, v in row.items() if k != "facts"})
    write_jsonl(Path(out_dir) / "map.jsonl", rows)
    return rows


def write_pipeline_record(path, story, *, meta, results):
    previous = read_json(path, {}) or {}
    invocations = list(previous.get("invocations") or [])
    stats = story.stage_stats()
    budget = story.run_limit.record() if story.run_limit is not None else None
    invocations.append({"started_at": story.started, "finished_at": _now(), "mode": story.mode,
                        "agents": meta.get("agents"), "stop": story.stop,
                        "committed_usd": None if budget is None else budget.get("committed_usd"),
                        "settled_usd": None if budget is None else budget.get("settled_usd")})
    record = {"schema": SCHEMA, "run_id": story.run_id, "generated_at": _now(), "mode": story.mode, **meta,
              "prompts": story.schema.record(), "chunk_tokens": story.chunk_tokens, "stage2_window": story.window,
              "max_tokens": {STAGE_NAMES[k]: v for k, v in MAX_TOKENS.items()},
              "prompt_cache": {"normalization": PREFIX_NORMALIZATION,
                               "fixed_prefix": "the stage system prompt as loaded by storymap/schema.py (prompts and "
                                               "taxonomy from private/research/analysis/storymap/); everything "
                                               "variable is in the user message",
                               "anthropic": "system prompt as one text block with cache_control ephemeral (5 min)",
                               "other_providers": "implicit provider caching; nothing marked",
                               "token_counter_normalization": "not applicable: no agent conversation is replayed as "
                                                              "provider messages (counter-user-v1 concerns the "
                                                              "pilot's agent requests)"},
              "stages": stats,
              "totals": {"requests": sum(r["requests"] for r in stats.values()),
                         "prompt_tokens": sum(r["prompt_tokens"] for r in stats.values()),
                         "completion_tokens": sum(r["completion_tokens"] for r in stats.values()),
                         "cached_tokens": sum(r["cached_tokens"] for r in stats.values()),
                         "cost_usd": str(sum((Decimal(r["cost_usd"]) for r in stats.values()), Decimal(0))),
                         "note": "recorded in the unit files this invocation used (including units reused from "
                                 "earlier invocations); budget.settled_usd is this invocation's own spend"},
              "budget": budget, "stop": story.stop, "agents_result": results, "invocations": invocations}
    write_json(path, record)
    return record


# ---------------------------------------------------------------------------
# CLI

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeline", type=Path, required=True, help="W1 output DIR (holding timeline/) or the folder")
    parser.add_argument("--out", type=Path, help="default RUN/analysis/storymap")
    parser.add_argument("--agents", help="comma-separated agent ids (default: every solver in the timeline)")
    parser.add_argument("--run-limit", type=Decimal,
                        help="this run's story-map budget in USD (default: RUN/settings.json behavior_judge_max_usd, "
                             "else $50)")
    parser.add_argument("--stage1-model", default=DEFAULT_MODELS["stage1"], choices=KNOWN_MODELS)
    parser.add_argument("--stage2-model", default=DEFAULT_MODELS["stage2"], choices=KNOWN_MODELS)
    parser.add_argument("--stage3-model", default=DEFAULT_MODELS["stage3"], choices=KNOWN_MODELS)
    parser.add_argument("--crosscheck-model", default=DEFAULT_CROSSCHECK_MODEL,
                        help="model of the cross-check sample, or 'none'")
    parser.add_argument("--crosscheck-fraction", type=float, default=DEFAULT_CROSSCHECK_FRACTION)
    parser.add_argument("--crosscheck-seed", type=int, default=DEFAULT_CROSSCHECK_SEED)
    parser.add_argument("--stage1-bakeoff", action="store_true",
                        help="stage 1 only, for --agents, with each of --bakeoff-models (model selection)")
    parser.add_argument("--bakeoff-models", default=",".join(DEFAULT_BAKEOFF_MODELS))
    parser.add_argument("--chunk-tokens", type=int, default=chunking.DEFAULT_CHUNK_TOKENS)
    parser.add_argument("--retry-failed", action="store_true", help="redo units recorded as failed")
    parser.add_argument("--agent-concurrency", type=int, default=DEFAULT_AGENT_CONCURRENCY,
                        help="agents mapped at once per group, in agent order (0: all at once)")
    parser.add_argument("--ledger", type=Path, help="ledger to bill (default this checkout's state/budget.sqlite)")
    parser.add_argument("--credentials-root", type=Path, default=ROOT,
                        help="directory whose private .env holds the provider key (never printed)")
    parser.add_argument("--pricing-record", type=Path, help="judge route pricing record (default newest, refreshed)")
    parser.add_argument("--route-record", type=Path, help="story-map route record (default newest, refreshed)")
    parser.add_argument("--mock", action="store_true", help="offline deterministic model; no network, no key")
    return parser.parse_args(argv)


def _models_of(args):
    models = {"stage1": args.stage1_model, "stage2": args.stage2_model, "stage3": args.stage3_model}
    cross = None if str(args.crosscheck_model).lower() in ("", "none") else check_model(args.crosscheck_model)
    bakeoff = [check_model(m.strip()) for m in args.bakeoff_models.split(",") if m.strip()] if args.stage1_bakeoff \
        else None
    return models, cross, bakeoff


def _budget(args, run_dir, prior_usd, rtj=None):
    """(limit record) of ``--run-limit``, else RUN/settings.json behavior_judge_max_usd, else $50."""
    if rtj is not None:
        return rtj.fixed_run_limit(args.run_limit, rtj.recorded_budget_config(run_dir), prior_usd=prior_usd)
    limit = Decimal(str(args.run_limit)) if args.run_limit is not None else DEFAULT_RUN_LIMIT_USD
    prior = Decimal(str(prior_usd or 0))
    if limit - prior <= 0:
        raise PipelineError(f"no room under the story-map budget ${limit} (already committed ${prior}); nothing sent")
    return {"limit_usd": str(limit), "source": "override" if args.run_limit is not None else "default",
            "basis": "override" if args.run_limit is not None else "default", "prior_usd": str(prior)}


def build(args, run_dir, out_dir, model_ids, log, *, schema_module=None, transport=None):
    """(StoryMap, metadata) for one invocation: route settings, ledger, budget, clients."""
    from research.analysis.judge.client import FakeTransport, RunLimit, committed_for_judging
    label = f"storymap-{run_dir.name}"
    extras = [m for m in model_ids if m in EXTRA_SPECS]
    if args.mock:
        from .pipeline_mock import MockStorymapModel
        anthropic, extra = offline_settings(model_ids)
        ledger, scope = (args.ledger or out_dir / "rehearsal-ledger.sqlite").resolve(), "rehearsal"
        if ledger.name == "budget.sqlite":
            raise PipelineError("--mock never uses the authoritative ledger")
        prior = committed_for_judging([ledger], out_dir, scope=scope, run_id=label)
        limit = _budget(args, run_dir, prior["committed_usd"])
        common = {"http": transport or FakeTransport(responder=MockStorymapModel())}
        rtj = None
    else:
        if str(ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(ROOT / "scripts"))
        import run_transcript_judge as rtj
        from research_swarm.budget import DEFAULT_LEDGER_PATH
        ns = SimpleNamespace(pricing_record=args.pricing_record)
        if any(m in ANTHROPIC_KEYS for m in model_ids):
            rtj.refresh_pricing(ns, log=log)
            anthropic = rtj.route_settings(ns, paid=True).with_prompt_cache(True)
        else:
            anthropic = offline_settings([])[0]   # never used: no Anthropic model in this invocation
        extra = extra_route_settings(extras, route_dir=ROOT / "data" / "validation", record=args.route_record, log=log)
        ledger, scope = (args.ledger or DEFAULT_LEDGER_PATH(ROOT)).resolve(), "analysis"
        sources = rtj.judging_ledgers(ledger, out_dir, search=(run_dir,), log=log)
        prior = committed_for_judging(sources, out_dir, scope=scope, run_id=label)
        limit = _budget(args, run_dir, prior["committed_usd"], rtj)
        common = {"http": None, "paid_authorized": True, "credentials_root": args.credentials_root.resolve()}
    run_limit = RunLimit(limit["limit_usd"], source=limit["source"], basis=limit["basis"],
                         prior_usd=prior["committed_usd"], prior=prior, reservation_factor=prior["reservation_factor"])
    if rtj is not None:
        rtj.record_run_budget(out_dir, {"limit_usd": limit["limit_usd"], "source": limit["source"],
                                        "basis": limit["basis"], "prior_usd": prior["committed_usd"],
                                        "ledger_run_id": run_limit.run_id, "label_run_id": label,
                                        "purpose": "storymap"}, run_dir=run_dir, ledger=ledger)
    pool = ModelPool(ledger_path=ledger, archive_root=out_dir / "raw", anthropic_settings=anthropic,
                     extra_settings=extra, scope=scope, run_limit=run_limit, run_id=label, **common)
    schema = SchemaAdapter(schema_module if schema_module is not None else load_schema_module())
    story = StoryMap(timeline_dir=args.timeline, out_dir=out_dir, pool=pool, schema=schema, run_id=run_dir.name,
                     mode="mock" if args.mock else "paid", log=log, chunk_tokens=args.chunk_tokens,
                     retry_failed=args.retry_failed, run_limit=run_limit,
                     agent_concurrency=args.agent_concurrency or None)
    meta = {"ledger": str(ledger), "scope": scope, "ledger_label_run_id": label,
            "budget_rule": "--run-limit, else RUN/settings.json behavior_judge_max_usd, else $50; earlier "
                           "invocations into this output directory count against it"}
    return story, meta


def main(argv=None, *, schema_module=None, transport=None):
    args = parse_args(argv)
    log = lambda message: print(message, file=sys.stderr, flush=True)  # noqa: E731
    run_dir = args.run_dir.resolve()
    out_dir = (args.out or run_dir / "analysis" / "storymap").resolve()
    if args.mock and (out_dir == run_dir or run_dir in out_dir.parents):
        log("refused: --mock writes outside the run directory (pass --out)")
        return 2
    try:
        models, cross, bakeoff = _models_of(args)
    except ValueError as error:
        log(f"refused: {error}")
        return 2
    folder = chunking.timeline_folder(args.timeline)
    solvers = chunking.list_agents(folder)
    if args.agents:
        agents = [a.strip() for a in args.agents.split(",") if a.strip()]
        missing = [a for a in agents if not (folder / f"{a}.jsonl").is_file()]
        if missing:
            log(f"refused: no timeline for {missing} in {folder}")
            return 2
    else:
        agents = solvers
    if not agents:
        log(f"refused: no solver timelines in {folder}")
        return 2
    sample = [a for a in crosscheck_sample(solvers, args.crosscheck_fraction, args.crosscheck_seed) if a in agents] \
        if cross and not bakeoff else []
    model_ids = list(dict.fromkeys(bakeoff or [*models.values(), *([cross] if sample else [])]))
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / ".storymap.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log(f"refused: another invocation holds {out_dir / '.storymap.lock'}")
            return 2
        try:
            story, meta = build(args, run_dir, out_dir, model_ids, log, schema_module=schema_module,
                                transport=transport)
        except Exception as error:   # route, ledger or budget refused: nothing was sent
            log(f"refused: {type(error).__name__}: {error}")
            return 2
        for sig in (signal.SIGTERM,):
            try:
                signal.signal(sig, lambda *_: story.run_limit.close("SIGTERM"))
            except ValueError:   # not the main thread (tests)
                pass
        story.pool.prepare(model_ids)
        started = time.monotonic()
        log(f"story map: {len(agents)} agent(s), budget ${story.run_limit.limit} (already committed "
            f"${story.run_limit.prior}); " + (f"bakeoff {bakeoff}" if bakeoff else
                                                f"models {models}, crosscheck {cross} on {sample}"))
        results = asyncio.run(story.run(agents, models=models, crosscheck_agents=sample, crosscheck_model=cross,
                                        bakeoff_models=bakeoff))
        record_meta = {"run_dir": str(run_dir), "timeline": str(folder), "agents": agents,
                       "wall_seconds": round(time.monotonic() - started, 1), **meta,
                       "models": {k: story.pool.describe(v) for k, v in models.items()} if not bakeoff else None}
        if bakeoff:
            record_meta["bakeoff"] = {"models": {m: story.pool.describe(m) for m in bakeoff},
                                      "folders": {m: f"bakeoff/{slug(m)}" for m in bakeoff}}
            record = write_pipeline_record(out_dir / "bakeoff" / "pipeline.json", story, meta=record_meta,
                                           results=results)
        else:
            record_meta["crosscheck"] = {"model": cross, "fraction": args.crosscheck_fraction,
                                         "seed": args.crosscheck_seed, "population": len(solvers), "agents": sample,
                                         "route": story.pool.describe(cross) if sample else None,
                                         "folder": "crosscheck"}
            rebuild_map(out_dir)
            if sample:
                rebuild_map(out_dir / "crosscheck")
            record = write_pipeline_record(out_dir / "pipeline.json", story, meta=record_meta, results=results)
    summary = {"status": "stopped" if story.stop else "done", "out_dir": str(out_dir),
               "requests": record["totals"]["requests"], "cost_usd": record["totals"]["cost_usd"],
               "invocation_settled_usd": (record.get("budget") or {}).get("settled_usd"), "stop": story.stop}
    print(json.dumps(summary, indent=1))
    return 3 if story.stop else 0


if __name__ == "__main__":
    sys.exit(main())
