"""Per-invocation cost estimate and run limit of the misbehavior judge (researcher-only).

Judge revision v3, docs/plan.md, "Behavior judge iteration specified, October 19, 2026", item 7:
each paid judging run's own limit is set automatically at 2x its central estimate, the run
refuses any request that would push its committed spend past that limit, one confirmation shows
the estimate, the run limit and the remaining analysis allowance, the report shows estimate
against actual cost, and an explicit ``--run-limit`` overrides the limit.

``plan_invocation`` (or ``InvocationPlanner`` fed one loaded agent at a time) estimates exactly
what one judging invocation would send: its agents, its ``--stage``, its settings and its judge
directory, with no model call, credential or ledger write. A request whose cached unit a run
would reuse (``pipeline.cached_unit``: complete and made from exactly this request) is free.

* prefilter: one Haiku request per window without such a unit;
* the selection: Haiku's own cached selections when the agent's whole prefilter stage is
  cached, else random clusters of ``assumed_selection_fraction`` of its item tokens (stated in
  the estimate); an agent whose cached selection is above ``max_selected_fraction`` is held, so
  nothing is sent for it (an assumed selection never holds an agent);
* candidates and random-sample windows: the requests the pipeline renders
  (``pipeline.plan_candidates`` with passage-first views, ``pipeline.plan_samples`` with the rest
  of passage-view candidates); a cached refused request of several items brings its single-item
  re-sends, one whose repair was refused the re-sends of its undecided items
  (``pipeline.resend_plan``);
* verification: when every candidate and sample unit of the agent is cached, its findings are
  rebuilt exactly as the pipeline builds them (``pipeline.candidate_outcome``) and each
  one-finding verifier request is rendered (cached ones free): every cheating and
  whistleblowing finding, and the evaluation-awareness findings in ``cohort.awareness_order``
  until ``cohort.awareness_progress`` settles (those already cached counting as checked first),
  an unchecked one assumed confirmed as claimed;
  otherwise the run's ``assumed_positive_findings`` (scaled to the agents planned, allocated by
  their evaluation-awareness keyword candidates) with the early stop
  (``awareness_checks_per_flagged_agent`` per flagged agent plus every other finding), each
  request sized from the agent's measured one-finding requests;
* run level: one Opus request when the stage reaches it and some agent has, or is assumed to
  have, a finding, sized from the real exposure-graph rows of the flagged agents
  (``InvocationPlanner.runlevel_request``); free only when its unit is complete, nothing else is
  sent, ``--recompute-stale`` is not set and ``--retry-failed`` does not redo it.

Every planned request is priced at its own tier (``pipeline.request_cost``) in three columns:
``central`` (characters/4 x ``tokenizer_factor_central`` provider tokens, the central output
assumptions), ``high`` (inputs x ``tokenizer_factor_high``) and ``stress`` (high inputs, outputs
and assumed findings x ``output_factor_stress``); each includes the one repair at its assumed
rate and, when prompt caching is on, the saving on the shared Sonnet/Opus system prompts
(reservations never assume it); a rendered request's reservation uses its body bytes
(``body_bytes``). ``run_limit_for`` (``invocation_run_limit``) turns the central total into the
run limit: ``RUN_LIMIT_FACTOR`` x central, raised when needed to central plus
``RUN_LIMIT_RESERVATION_MARGIN`` x the largest planned reservation, at least
``RUN_LIMIT_FLOOR_USD``, unless overridden.
Package mode (v2/v2.1) is estimated more roughly: its cached judge unit is recognised by status
only, and its verification is always the assumed one.
"""
from __future__ import annotations

import datetime
import json
import random
from dataclasses import asdict
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from . import cohort, prompts, schema
from . import pipeline as pl
from .history import agent_code, estimate_tokens
from .windows import make_windows

INVOCATION_SCHEMA = "gdm-judge-invocation-estimate/1"
# docs/plan.md, October 19, 2026, item 7: "2x is good". The floor keeps a tiny resume (almost
# everything cached) able to send its few remaining requests. A request is admitted only when its
# whole conservative reservation (byte bound plus max_tokens) fits under the limit, so the limit is
# also at least the central estimate plus RUN_LIMIT_RESERVATION_MARGIN x the largest single
# reservation the invocation plans: otherwise a lone request (for example the run-level request of
# a resume with little else left, whose hold is about 3-4x its expected cost) could never be sent
# and every resume would stop at the same place.
RUN_LIMIT_FACTOR = Decimal(2)
RUN_LIMIT_FLOOR_USD = Decimal("1.00")
RUN_LIMIT_RESERVATION_MARGIN = Decimal("1.25")
# Estimated tokens of the run-level request beyond the system prompt, the exposure graph and the
# agents' results (headers, cohort counts, the agents without findings, the miss rate).
RUNLEVEL_FIXED_TOKENS = 500
COLUMNS = ("central", "high", "stress")
# Estimate stages (columns of the cost tables) with their model, max_tokens key and label.
STAGES = {"haiku_prefilter": ("haiku", "prefilter", "prefilter"),
          "sonnet_candidates": ("sonnet", "judge", "candidates"),
          "sonnet_sample_windows": ("sonnet", "judge", "random-sample windows"),
          "sonnet_judge": ("sonnet", "judge", "package judge (v2/v2.1)"),
          "opus_verifier": ("opus", "verifier", "verification"),
          "opus_runlevel": ("opus", "runlevel", "run level")}
# Unit purposes of the settled spend per estimate stage (estimate against actual).
STAGE_PURPOSES = {"haiku_prefilter": ("prefilter",), "sonnet_candidates": ("judge_candidates",),
                  "sonnet_sample_windows": ("judge_sample",),
                  "sonnet_judge": ("judge", "judge_part", "judge_synthesis"),
                  "opus_verifier": ("verifier",), "opus_runlevel": ("runlevel",)}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _money(value, places="0.01"):
    return str(Decimal(value).quantize(Decimal(places)))


def stage_of_purpose(purpose):
    """Estimate stage of a request purpose (``None`` when unknown). A repair request
    (``<purpose>_repair``, ``Pipeline.unit``) belongs to the stage of its unit, whose estimate
    includes the repair rate."""
    base = purpose[:-len("_repair")] if isinstance(purpose, str) and purpose.endswith("_repair") else purpose
    return next((stage for stage, purposes in STAGE_PURPOSES.items() if base in purposes), None)


def column_factors(assumptions):
    """(input factor, output factor, assumed-findings factor) per column."""
    stress = assumptions.output_factor_stress
    return {"central": (assumptions.tokenizer_factor_central, 1.0, 1.0),
            "high": (assumptions.tokenizer_factor_high, 1.0, 1.0),
            "stress": (assumptions.tokenizer_factor_high, stress, stress)}


# Bytes of a request body beyond its JSON-encoded system prompt and user message (model id,
# max_tokens, reasoning, provider preferences, the cache_control marker), rounded up.
BODY_OVERHEAD_BYTES = 1024


def body_bytes(system, user):
    """About the UTF-8 bytes of the request body the client sends (``JudgeClient.body``: the
    system prompt and the user message JSON-encoded, escapes included, plus
    ``BODY_OVERHEAD_BYTES``), which its reservation bounds (``route.prompt_token_bound``)."""
    return (len(json.dumps(system, ensure_ascii=False).encode("utf-8"))
            + len(json.dumps(user, ensure_ascii=False).encode("utf-8")) + BODY_OVERHEAD_BYTES)


def _request(stage, tokens, output, repair_rate, *, system_tokens=0, count=1.0, assumed=False, bytes_=None):
    """One planned request (``count`` may be fractional for assumed verifier checks; ``bytes_``:
    its body bytes when rendered, for the reservation)."""
    return {"stage": stage, "tokens": int(tokens), "output": int(output), "repair_rate": float(repair_rate),
            "system_tokens": int(system_tokens), "count": float(count), "assumed": bool(assumed),
            "bytes": None if bytes_ is None else int(bytes_)}


def _rendered(stage, system_text, user, output, repair_rate, *, system_tokens=0):
    """A planned request rendered exactly as the pipeline sends it."""
    return _request(stage, estimate_tokens(system_text) + estimate_tokens(user), output, repair_rate,
                    system_tokens=system_tokens, bytes_=body_bytes(system_text, user))


def price_requests(requests, route_settings, settings, assumptions):
    """``{column: {"usd": {stage: USD}, "total_usd", "prompt_cache_saving_usd", "expected_usd",
    "requests", "input_tokens", "output_tokens"}}`` of the planned requests (unrounded
    Decimals; ``expected_usd`` is the total less the prompt-cache saving when caching is on)."""
    routes = {key: route_settings.route(key) for key in pl.MODEL_KEYS}
    out = {}
    for column, (fin, fout, ffind) in column_factors(assumptions).items():
        usd = {stage: Decimal(0) for stage in STAGES}
        groups, calls, tokens_in, tokens_out = {}, 0.0, 0, 0
        for request in requests:
            model, limit_key, _ = STAGES[request["stage"]]
            count = request["count"] * (ffind if request["assumed"] else 1.0) * (1 + request["repair_rate"])
            output = min(request["output"] * fout, settings.max_tokens[limit_key])
            usd[request["stage"]] += Decimal(str(count)) * pl.request_cost(routes[model], request["tokens"] * fin, output)
            calls += count
            tokens_in += int(request["tokens"] * fin * count)
            tokens_out += int(output * count)
            if settings.prompt_cache and model != "haiku" and request["system_tokens"]:
                key = (model, request["system_tokens"])
                groups[key] = groups.get(key, 0.0) + count
        saving = sum((pl._cache_saving(routes[model], system * fin, n, assumptions)
                      for (model, system), n in groups.items()), Decimal(0))
        saving = max(saving, Decimal(0))
        total = sum(usd.values(), Decimal(0))
        out[column] = {"usd": usd, "total_usd": total, "prompt_cache_saving_usd": saving,
                       "expected_usd": total - saving, "requests": round(calls, 2), "input_tokens": tokens_in,
                       "output_tokens": tokens_out}
    return out


def run_limit_for(central_usd, override=None, *, factor=RUN_LIMIT_FACTOR, floor=RUN_LIMIT_FLOOR_USD,
                  largest_reservation=None, margin=RUN_LIMIT_RESERVATION_MARGIN):
    """``{"limit_usd", "source", "basis", "factor", "floor_usd", "central_usd",
    "largest_reservation_usd", "reservation_margin"}``: ``factor`` x the central estimate, raised
    when needed to the central estimate plus ``margin`` x the ``largest_reservation`` the
    invocation plans (so that request can be admitted once the others have settled) and to at
    least ``floor``, rounded up to the cent; ``basis`` says which of ``factor``,
    ``largest_reservation`` or ``floor`` set it. An explicit ``override`` (USD, > 0) replaces it
    (``source`` ``override``)."""
    central = Decimal(str(central_usd))
    largest = Decimal(str(largest_reservation)) if largest_reservation is not None else None
    common = {"central_usd": _money(central), "largest_reservation_usd": None if largest is None else _money(largest)}
    if override is not None:
        limit = Decimal(str(override))
        if not limit.is_finite() or limit <= 0:
            raise ValueError("--run-limit must be a positive amount in USD")
        return {"limit_usd": str(limit), "source": "override", "basis": "override", "factor": None, "floor_usd": None,
                "reservation_margin": None, **common}
    cent = Decimal("0.01")
    candidates = [("factor", (central * factor).quantize(cent, rounding=ROUND_CEILING))]
    if largest is not None:
        candidates.append(("largest_reservation", (central + margin * largest).quantize(cent, rounding=ROUND_CEILING)))
    candidates.append(("floor", Decimal(floor)))
    basis, limit = max(candidates, key=lambda pair: pair[1])  # the first wins a tie
    return {"limit_usd": str(limit), "source": "estimate", "basis": basis, "factor": str(factor),
            "floor_usd": str(floor), "reservation_margin": str(margin), **common}


def largest_reservation(estimate):
    """The largest single reservation (USD) an invocation estimate (or its ``limit_record``)
    plans, else None."""
    values = [Decimal(str(v)) for v in ((estimate or {}).get("largest_reservation_usd") or {}).values()]
    return max(values) if values else None


def invocation_run_limit(estimate, override=None):
    """``run_limit_for`` of an invocation estimate (its central total and largest reservation)."""
    return run_limit_for(estimate["central_usd"], override, largest_reservation=largest_reservation(estimate))


class InvocationPlanner:
    """Plans one judging invocation agent by agent (``add`` with the loaded run, then ``finish``),
    so the dry-run estimate can share each agent's rebuilt history with the scenario projection.
    See the module docstring for what is planned and priced."""

    def __init__(self, judge_dir, settings, route_settings, *, stage="report", assumptions=None, info=None,
                 retry_failed=False, recompute_stale=False, all_agents=None):
        if stage not in pl.STAGES:
            raise ValueError(f"stage must be one of {pl.STAGES}")
        self.judge_dir = Path(judge_dir) if judge_dir else None
        self.settings = settings
        self.route_settings = route_settings
        self.stage = stage
        self.stage_index = pl.STAGES.index(stage)
        self.assumptions = assumptions or pl.EstimateAssumptions()
        self.info = info or prompts.rubric_info()
        self.retry_failed = retry_failed
        self.recompute_stale = recompute_stale
        self.all_agents = list(all_agents) if all_agents is not None else None
        self.system = pl.system_prompt_tokens(self.info)
        self.requests = []
        self.agents = {}
        self.assumed = []        # agents whose verification uses the assumed findings
        self.flagged = set()     # agents with (or assumed to have) a finding: the run level runs
        self.graph = {}          # agent -> its exposure-graph rows (the run-level request's input)

    def reached(self, stage):
        return self.stage_index >= pl.STAGES.index(stage)

    # -- one agent ------------------------------------------------------------
    def add(self, run, agent_id):
        """Plan ``agent_id`` from its loaded ``run`` (``history.build_run`` output)."""
        history = pl._get(run, "agents")[agent_id]
        items = list(pl._get(history, "items", []) or [])
        by_id = {item["id"]: item for item in items}
        code = pl._get(history, "code") or agent_code(agent_id)
        windows = make_windows(items, self.settings.window_tokens)
        unit_dir = self.judge_dir / "units" / agent_id if self.judge_dir else None
        total = sum(estimate_tokens(i.get("text")) for i in items)
        detail = {"windows": len(windows), "item_tokens": total, "prefilter": {"cached": 0, "to_send": 0},
                  "selection": None, "selected_fraction": None, "held": False,
                  "candidates": {"cached": 0, "to_send": 0}, "sample_windows": {"cached": 0, "to_send": 0},
                  "verification": None, "stale_units": []}
        self.agents[agent_id] = detail
        self.graph[agent_id] = pl.exposure_graph_rows(history)
        assumptions, settings, system = self.assumptions, self.settings, self.system

        def cached(relative, system_text, user):
            if unit_dir is None:
                return None
            path = unit_dir / relative
            unit = pl.cached_unit(path, system_text, user, retry_failed=self.retry_failed)
            if unit is None and path.exists():
                old = pl.read_json(path) or {}
                if old.get("status") in pl.FINAL and old.get("input_sha256") != pl.sha256_text(system_text + "\0" + user):
                    detail["stale_units"].append(relative)  # refused by the run unless --recompute-stale
            return unit

        prefilter = {}
        for window in windows:
            system_text, user = pl.prefilter_request(agent_id, code, window, by_id, settings, len(windows))
            unit = cached(f"prefilter/{window['id']}.json", system_text, user)
            if unit is not None:
                prefilter[window["id"]] = unit
                detail["prefilter"]["cached"] += 1
            else:
                detail["prefilter"]["to_send"] += 1
                self.requests.append(_rendered("haiku_prefilter", system_text, user, assumptions.haiku_output_per_window,
                                               assumptions.prefilter_repair_rate))
        if not self.reached("judge"):
            return detail
        if windows and len(prefilter) == len(windows):
            selected, forced, unselected = set(), set(), []
            for window in windows:
                unit = prefilter[window["id"]]
                if unit["status"] == "ok":
                    ids = set(unit["result"]["selected_item_ids"])
                    selected |= ids
                    if not ids:
                        unselected.append(window["id"])
                else:
                    forced |= {schema.base_item_id(i) for i in window["item_ids"]}
            detail["selection"] = "cached"
        else:
            rng = random.Random(f"{settings.sample_seed}:{agent_id}:invocation")
            selected = set(pl.cluster_selection(items, assumptions.assumed_selection_fraction, rng)) if items else set()
            forced = set()
            base_of = {}
            for window in windows:
                for piece in window["item_ids"]:
                    base_of.setdefault(schema.base_item_id(piece), set()).add(window["id"])
            chosen = set().union(*(base_of.get(i, set()) for i in selected)) if selected else set()
            unselected = [w["id"] for w in windows if w["id"] not in chosen]
            detail["selection"] = "assumed"
        tokens = {item["id"]: estimate_tokens(item.get("text")) for item in items}
        fraction = sum(tokens.get(i, 0) for i in selected | forced) / total if total else 0.0
        detail["selected_fraction"] = round(fraction, 6)
        if detail["selection"] == "cached" and fraction > settings.max_selected_fraction \
                and not self._judge_cached(unit_dir):
            detail["held"] = True       # the selection guard holds the agent: nothing is sent for it
            return detail
        sampled = [w for w in unselected if pl.window_sampled(w, settings.sample_seed, settings.sample_fraction)]
        structural = prompts.structural_items(run, agent_id)
        if settings.judge_mode == "package":
            self._package_agent(agent_id, history, structural, windows, by_id, sorted(selected), sorted(forced),
                                sampled, unit_dir, detail)
            return detail
        sources = pl.candidate_sources(history, sorted(selected), sorted(forced))
        plan = pl.plan_candidates(history, structural, sources, settings)
        samples, covered = pl.plan_samples(history, structural, windows, sampled, plan["candidates"],
                                           views=plan["views"])
        results, complete = [], True

        def candidate(name, request, role):
            nonlocal complete
            system_text, user = prompts.candidate_messages_v2(request, info=self.info)
            unit = cached(f"{name}.json", system_text, user)
            if unit is None:
                complete = False
                detail["candidates"]["to_send"] += 1
                self.requests.append(_rendered(
                    "sonnet_candidates", system_text, user,
                    assumptions.candidate_output_base + assumptions.candidate_output_per_item * len(request["ids"]),
                    assumptions.candidate_repair_rate, system_tokens=system["candidates"]))
                return None
            detail["candidates"]["cached"] += 1
            results.append((name, request, unit, role))
            return unit

        def window_request(name, request, role):
            nonlocal complete
            system_text, user = prompts.sample_window_messages_v2(request, info=self.info)
            unit = cached(f"{name}.json", system_text, user)
            if unit is None:
                complete = False
                detail["sample_windows"]["to_send"] += 1
                self.requests.append(_rendered("sonnet_sample_windows", system_text, user,
                                               assumptions.sample_window_output, assumptions.candidate_repair_rate,
                                               system_tokens=system["candidates"]))
                return None
            detail["sample_windows"]["cached"] += 1
            results.append((name, request, unit, role))
            return unit

        # A cached request of several items that was refused, or whose repair was refused, brings
        # its single-item re-sends (``pipeline.resend_plan``), exactly as the pipeline sends them.
        for request in plan["requests"]:
            name = f"candidates-b{request['index']:02d}"
            unit = candidate(name, request, "batch")
            role, only = pl.resend_plan(unit, request["ids"]) if unit is not None else (None, None)
            if role is not None:
                results[-1] = (name, request, unit, role)
                for sub in pl.split_candidate_request(history, structural, request, plan["sources"], settings,
                                                      only=only):
                    candidate(f"{name}-c{sub['number']:02d}", sub, "split")
        for request in samples:
            name = f"sample/{request['window_id']}"
            unit = window_request(name, request, "sample")
            role, only = pl.resend_plan(unit, request["judged_ids"]) if unit is not None else (None, None)
            if role is not None:
                results[-1] = (name, request, unit, role)
                for sub in pl.split_sample_request(request, only=only):
                    window_request(f"{name}-i{sub['number']:02d}", sub, "sample_split")
        if not self.reached("verify"):
            return detail
        if complete:
            outcome = pl.candidate_outcome(agent_id, history, structural, plan, results, covered)
            self._verify_exact(agent_id, history, structural, outcome["verdict"], cached, detail,
                               shown=pl.shown_passages(outcome))
        else:
            self._verify_assumed(agent_id, history, structural, plan["candidates"], by_id, detail)
        return detail

    def _judge_cached(self, unit_dir):
        """``Pipeline._judge_cached``: a judge unit with a recorded request exists (the selection
        guard then no longer holds the agent)."""
        for name in ("judge.json", "judge-p01.json", "candidates-b01.json"):
            unit = pl.read_json(unit_dir / name) if unit_dir is not None and (unit_dir / name).exists() else None
            if unit is not None and unit.get("steps"):
                return True
        return False

    def _verify_exact(self, agent_id, history, structural, verdict, cached, detail, shown=None):
        """The verifier requests of a verdict rebuilt from cached units, as ``Pipeline.verify_stage``
        sends them: one per cheating and whistleblowing finding; evaluation awareness until settled,
        every finding whose unit is already complete counting as checked before the stop rule (an
        unchecked finding assumed confirmed as claimed, so this is the fewest checks)."""
        findings = list((verdict or {}).get("findings") or [])
        record = {"basis": "cached_candidate_units", "findings": len(findings), "cached": 0, "to_send": 0,
                  "not_checked_assumed": 0}
        detail["verification"] = record
        if not findings:
            return
        self.flagged.add(agent_id)
        package = prompts.build_verification_package_v2(verdict, history, structural=structural, shown=shown,
                                                        **self.settings.verifier_view())
        blocks = {b["finding_id"]: b for b in package["blocks"]}

        def check(fid):
            system_text, user = pl.verifier_request(package, blocks[fid], info=self.info)
            unit = cached(f"verify-{pl._safe(fid)}.json", system_text, user)
            if unit is None:
                record["to_send"] += 1
                self.requests.append(_rendered("opus_verifier", system_text, user,
                                               self.assumptions.verifier_output_per_call,
                                               self.assumptions.verifier_repair_rate,
                                               system_tokens=self.system["verifier"]))
                return {"finding_id": fid, "verdict": "confirmed", "level": None}
            record["cached"] += 1
            if unit["status"] != "ok":
                return None
            return next((v for v in (unit.get("result") or {}).get("verdicts") or [] if v.get("finding_id") == fid),
                        None)

        def reusable(fid):
            if self.judge_dir is None:
                return False
            system_text, user = pl.verifier_request(package, blocks[fid], info=self.info)
            return pl.cached_unit(self.judge_dir / "units" / agent_id / f"verify-{pl._safe(fid)}.json", system_text,
                                  user, retry_failed=self.retry_failed) is not None

        positions = {item["id"]: n for n, item in enumerate(pl._get(history, "items", []) or [])}
        order = cohort.awareness_order(findings, positions)
        for finding in findings:
            if finding["id"] not in set(order):
                check(finding["id"])
        outcomes = {fid: check(fid) for fid in order if reusable(fid)}
        progress = cohort.awareness_progress(findings, order, outcomes)
        for fid in order:
            if progress["settled"]:
                break
            if fid in outcomes:
                continue
            outcomes[fid] = check(fid)
            progress = cohort.awareness_progress(findings, order, outcomes)
        record["not_checked_assumed"] = len(progress["unchecked"])

    def _verify_assumed(self, agent_id, history, structural, candidates, by_id, detail):
        """Measured one-finding verifier request size and the evaluation-awareness keyword weight
        of an agent whose findings are not known yet (allocated in ``finish``)."""
        eval_like = [i for i in candidates if pl.EVAL_HINT.search(by_id[i].get("text") or "")]
        measured = (eval_like + [i for i in candidates if i not in set(eval_like)])[
            :self.assumptions.verifier_blocks_measured]
        sizes = pl.verifier_request_tokens(history, structural, measured, self.settings, self.system)
        detail["verification"] = {"basis": "assumed_findings", "eval_like_candidates": len(eval_like),
                                  "request_tokens_mean": round(sum(sizes) / len(sizes)) if sizes else None}
        self.assumed.append(agent_id)

    def _package_agent(self, agent_id, history, structural, windows, by_id, selected, forced, sampled, unit_dir,
                       detail):
        """Package mode (v2/v2.1), roughly: one judge request per package part (plus the
        synthesis) unless a judge unit is already complete; verification always assumed."""
        done = any(((pl.read_json(unit_dir / name) or {}).get("status") in pl.FINAL)
                   for name in ("judge.json", "judge-synthesis.json")
                   if unit_dir is not None and (unit_dir / name).exists())
        items = list(pl._get(history, "items", []) or [])
        if not done:
            by_window = {w["id"]: w for w in windows}
            chosen = sorted(set(selected) | set(forced) | set(pl.recall_selection(items)))
            sample = [{"window_id": w, "items": pl.window_items(by_window[w], by_id)} for w in sampled]
            package = prompts.build_agent_package_v2(history, chosen, neighbors=self.settings.neighbors,
                                                     structural=structural, sample_windows=sample)
            judge_system = self.system["judge"]
            parts = [package["estimated_tokens"]]
            if package["estimated_tokens"] + judge_system > self.settings.package_token_cap:
                positions = {item["id"]: n for n, item in enumerate(items)}
                parts = [p["estimated_tokens"] for p in pl.split_package(package, self.settings.package_token_cap
                                                                         - judge_system, positions)]
            for tokens in parts:
                self.requests.append(_request("sonnet_judge", tokens + judge_system,
                                              self.assumptions.sonnet_judge_output, self.assumptions.judge_repair_rate,
                                              system_tokens=judge_system))
            if len(parts) > 1:
                self.requests.append(_request("sonnet_judge", self.assumptions.synthesis_input + judge_system,
                                              self.assumptions.sonnet_synthesis_output,
                                              self.assumptions.judge_repair_rate, system_tokens=judge_system))
            detail["candidates"]["to_send"] += len(parts) + (1 if len(parts) > 1 else 0)
        else:
            detail["candidates"]["cached"] += 1
        if not self.reached("verify"):
            return
        if done and self.judge_dir is not None and (self.judge_dir / "agents" / f"{agent_id}.json").exists():
            detail["verification"] = {"basis": "agent_result", "findings": None, "cached": None, "to_send": 0}
            return
        self._verify_assumed(agent_id, history, structural, sorted(set(selected) | set(forced)), by_id, detail)

    # -- the run level --------------------------------------------------------
    def _graph_rows(self, agent_id):
        """The exposure-graph rows of ``agent_id``: from its rebuilt history when planned here, else
        from the judge directory's ``history/<agent>.json``; None when unknown."""
        if agent_id in self.graph:
            return self.graph[agent_id]
        if self.judge_dir is not None and (self.judge_dir / "history" / f"{agent_id}.json").exists():
            return (pl.read_json(self.judge_dir / "history" / f"{agent_id}.json") or {}).get("exposure_graph") or []
        return None

    def runlevel_request(self, flagged):
        """The planned run-level request for the ``flagged`` agents (``Pipeline.runlevel_stage``,
        ``prompts.build_run_package_v2``): every exposure-graph row whose reader or author is
        flagged, rendered as the request renders it (from the agents' rebuilt histories or the
        judge directory's history summaries), plus ``runlevel_tokens_per_flagged_agent`` per
        flagged agent for its verified results; an agent whose rows are unknown is assumed to
        bring ``runlevel_graph_rows_per_flagged_agent`` rows of ``runlevel_tokens_per_graph_row``
        tokens. Returns the request with its body bytes (escapes included)."""
        assumptions, flagged = self.assumptions, set(flagged)
        agents = sorted(set(self.all_agents or []) | set(self.agents) | flagged)
        rows, unknown = [], 0
        for agent_id in agents:
            found = self._graph_rows(agent_id)
            if found is None:
                unknown += agent_id in flagged
                continue
            rows += [row for row in found if row.get("reader") in flagged or row.get("author") in flagged]
        graph = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
        system_text = prompts.runlevel_system_v2(self.info)
        assumed = unknown * assumptions.runlevel_graph_rows_per_flagged_agent * assumptions.runlevel_tokens_per_graph_row
        per_agent = len(flagged) * assumptions.runlevel_tokens_per_flagged_agent
        tokens = estimate_tokens(system_text) + estimate_tokens(graph) + assumed + per_agent + RUNLEVEL_FIXED_TOKENS
        # Agent results are JSON lines too: every quote is escaped in the body.
        size = (body_bytes(system_text, graph) + int((assumed + per_agent + RUNLEVEL_FIXED_TOKENS) * 4 * 1.2))
        request = _request("opus_runlevel", tokens, assumptions.opus_runlevel_output, 0,
                           system_tokens=self.system["runlevel"], bytes_=size)
        request["graph_rows"] = len(rows) + unknown * assumptions.runlevel_graph_rows_per_flagged_agent
        return request

    # -- the whole invocation -------------------------------------------------
    def finish(self, *, agents=None):
        """The invocation estimate (JSON-ready; see the module docstring)."""
        assumptions, settings, system = self.assumptions, self.settings, self.system
        planned = list(agents) if agents is not None else sorted(self.agents)
        run_agents = len(self.all_agents) if self.all_agents else len(planned)
        requests = list(self.requests)
        findings_total = 0
        if self.assumed and self.reached("verify"):
            rows = [self.agents[a]["verification"] for a in self.assumed]
            findings_total = int(round(assumptions.assumed_positive_findings * len(self.assumed) / max(1, run_agents)))
            allocation = pl.allocate_findings(findings_total, [r["eval_like_candidates"] for r in rows])
            share = min(1.0, max(0.0, float(assumptions.awareness_finding_share)))
            fallback = system["verifier"] + 6000
            for agent_id, row, findings in zip(self.assumed, rows, allocation):
                row.update(findings=findings, to_send=0.0, not_checked_assumed=0.0)
                if findings <= 0:
                    continue
                self.flagged.add(agent_id)
                aware = findings * share
                checks = min(aware, float(assumptions.awareness_checks_per_flagged_agent))
                calls = checks + (findings - aware)
                row.update(to_send=round(calls, 2), not_checked_assumed=round(aware - checks, 2))
                requests.append(_request("opus_verifier", row["request_tokens_mean"] or fallback,
                                         assumptions.verifier_output_per_call, assumptions.verifier_repair_rate,
                                         system_tokens=system["verifier"], count=calls, assumed=True))
        outside = []
        if self.judge_dir is not None and (self.judge_dir / "agents").is_dir():
            for path in sorted((self.judge_dir / "agents").glob("*.json")):
                if path.stem not in self.agents and prompts.has_findings(pl.read_json(path) or {}):
                    outside.append(path.stem)
        flagged = len(self.flagged) + len(outside)
        runlevel = {"planned": False, "cached": False, "flagged_agents": flagged}
        if self.reached("runlevel") and flagged:
            path = self.judge_dir / "units" / "run" / "runlevel.json" if self.judge_dir is not None else None
            unit = pl.read_json(path) if path is not None and path.exists() else None
            # A complete run-level unit is reused only when nothing else is sent (its input, the
            # agents' results, then stays as it was), it is not a grading_failed unit that
            # --retry-failed redoes, and --recompute-stale is not set (it may then be redone).
            if (unit is not None and unit.get("status") in pl.FINAL and not requests and not self.recompute_stale
                    and not (self.retry_failed and unit.get("status") == "grading_failed")):
                runlevel["cached"] = True
            else:
                runlevel["planned"] = True
                request = self.runlevel_request(self.flagged | set(outside))
                runlevel["graph_rows"] = request.pop("graph_rows")
                requests.append(request)
        priced = price_requests(requests, self.route_settings, settings, assumptions)
        counts = {stage: {"to_send": 0.0} for stage in STAGES}
        for request in requests:
            counts[request["stage"]]["to_send"] += request["count"]
        cached = {"haiku_prefilter": sum(d["prefilter"]["cached"] for d in self.agents.values()),
                  "sonnet_candidates": sum(d["candidates"]["cached"] for d in self.agents.values()
                                           if settings.judge_mode == "candidates"),
                  "sonnet_sample_windows": sum(d["sample_windows"]["cached"] for d in self.agents.values()),
                  "sonnet_judge": sum(d["candidates"]["cached"] for d in self.agents.values()
                                      if settings.judge_mode == "package"),
                  "opus_verifier": sum((d["verification"] or {}).get("cached") or 0 for d in self.agents.values()),
                  "opus_runlevel": 1 if runlevel["cached"] else 0}
        for stage, row in counts.items():
            row["to_send"] = round(row["to_send"], 2)
            row["cached"] = cached[stage]
        framing = getattr(self.route_settings, "framing_token_allowance", 8192)
        largest = {}
        for request in requests:
            model, limit_key, _ = STAGES[request["stage"]]
            amount = pl._reservation(self.route_settings.route(model), request["tokens"], settings.max_tokens[limit_key],
                                     framing, cache=settings.prompt_cache and model != "haiku",
                                     body_bytes=request.get("bytes"),
                                     output_tokens=pl.reserved_output_tokens(self.route_settings, model,
                                                                             settings.max_tokens[limit_key]))
            largest[model] = max(largest.get(model, Decimal(0)), amount)
        central = priced["central"]["expected_usd"]
        return {"schema": INVOCATION_SCHEMA, "created_at": _now(), "stage": self.stage, "judge_mode": settings.judge_mode,
                "agents": planned, "agents_in_run": run_agents, "prompt_cache": settings.prompt_cache,
                "retry_failed": self.retry_failed, "recompute_stale": self.recompute_stale,
                "judge_dir": str(self.judge_dir) if self.judge_dir else None,
                "selection": {"cached": sorted(a for a, d in self.agents.items() if d["selection"] == "cached"),
                              "assumed": sorted(a for a, d in self.agents.items() if d["selection"] == "assumed"),
                              "held": sorted(a for a, d in self.agents.items() if d["held"]),
                              "assumed_fraction": assumptions.assumed_selection_fraction},
                "verification": {"exact": sorted(a for a, d in self.agents.items()
                                                 if (d["verification"] or {}).get("basis") == "cached_candidate_units"),
                                 "assumed": sorted(self.assumed), "assumed_findings": findings_total,
                                 "flagged_agents_outside": outside},
                "runlevel": runlevel, "requests": counts,
                "stale_units": sum(len(d["stale_units"]) for d in self.agents.values()),
                "central_usd": _money(central), "high_usd": _money(priced["high"]["expected_usd"]),
                "stress_usd": _money(priced["stress"]["expected_usd"]),
                "columns": {column: {"usd": {stage: _money(v, "0.0001") for stage, v in value["usd"].items()},
                                     "total_usd": _money(value["total_usd"], "0.0001"),
                                     "prompt_cache_saving_usd": _money(value["prompt_cache_saving_usd"], "0.0001"),
                                     "expected_usd": _money(value["expected_usd"], "0.0001"),
                                     "requests": value["requests"], "input_tokens": value["input_tokens"],
                                     "output_tokens": value["output_tokens"]}
                            for column, value in priced.items()},
                "largest_reservation_usd": {k: _money(v.quantize(Decimal("0.01"), rounding=ROUND_CEILING))
                                            for k, v in sorted(largest.items())},
                "assumptions": asdict(assumptions), "per_agent": self.agents}


def plan_invocation(run_dir, judge_dir, settings, route_settings, *, agents=None, stage="report", assumptions=None,
                    loader=None, retry_failed=False, recompute_stale=False, all_agents=None, info=None, log=None):
    """The estimate of one judging invocation (``InvocationPlanner``), each agent rebuilt offline
    one at a time (``loader(agent_id)``, default ``history.build_run``)."""
    from .history import build_run, sample_index
    log = log or (lambda message: None)
    if all_agents is None:
        all_agents = sorted(sample_index(run_dir))
    agent_ids = sorted(agents) if agents else list(all_agents)
    planner = InvocationPlanner(judge_dir, settings, route_settings, stage=stage, assumptions=assumptions, info=info,
                                retry_failed=retry_failed, recompute_stale=recompute_stale, all_agents=all_agents)
    for agent_id in agent_ids:
        run = loader(agent_id) if loader else build_run(run_dir, agents=[agent_id])
        planner.add(run, agent_id)
        log(f"invocation estimate {agent_id}: {planner.agents[agent_id]['windows']} windows")
        del run
    return planner.finish(agents=agent_ids)


def limit_record(estimate):
    """The compact estimate kept with the run limit (state.json, pipeline.json): totals per
    column and stage, the request counts and the bases, without the per-agent detail."""
    return {"schema": estimate["schema"], "created_at": estimate["created_at"], "stage": estimate["stage"],
            "judge_mode": estimate["judge_mode"], "agents": len(estimate["agents"]),
            "central_usd": estimate["central_usd"], "high_usd": estimate["high_usd"],
            "stress_usd": estimate["stress_usd"],
            "by_stage": {column: estimate["columns"][column]["usd"] for column in COLUMNS},
            "prompt_cache_saving_usd": {column: estimate["columns"][column]["prompt_cache_saving_usd"]
                                        for column in COLUMNS},
            "requests": estimate["requests"],
            "selection": {k: (len(v) if isinstance(v, list) else v) for k, v in estimate["selection"].items()},
            "verification": {k: (len(v) if isinstance(v, list) else v) for k, v in estimate["verification"].items()},
            "largest_reservation_usd": estimate["largest_reservation_usd"]}


def cost_comparison(invocations, spend=None):
    """Estimate against actual cost (judge revision v3, item 7) from the run-limit records of the
    judging invocations (``client.RunLimit.record()``, kept in state.json): one row per invocation
    with its central (high, stress) estimate, its run limit, its own settled spend and its outcome.
    The comparison (``compared``, and ``by_stage`` before the prompt-cache saving, which is given
    separately) sums only the invocations that completed: a stopped invocation's estimate covers
    work it never did, and the resume that did it carries its own estimate (its spend is in
    ``settled_usd`` and in the judge directory's total, ``spend``, ``Pipeline.spend()``)."""
    rows, by_stage = [], {stage: {"estimate_central_usd": Decimal(0), "settled_usd": Decimal(0)} for stage in STAGES}
    compared = {"central": Decimal(0), "high": Decimal(0), "stress": Decimal(0), "saving": Decimal(0),
                "settled": Decimal(0), "invocations": 0}
    settled_all, uncertain_all, stopped = Decimal(0), Decimal(0), 0
    for entry in invocations:
        record = entry.get("run_limit") or {}
        estimate = record.get("estimate") or {}
        actual = {stage: Decimal(0) for stage in STAGES}
        for purpose, row in (record.get("by_purpose") or {}).items():
            stage = stage_of_purpose(purpose)
            if stage:
                actual[stage] += pl._decimal(row.get("settled_usd"))
        complete = not entry.get("stop") and entry.get("status") in ("complete", "this invocation")
        settled = pl._decimal(record.get("settled_usd"))
        settled_all += settled
        uncertain_all += pl._decimal(record.get("uncertain_usd"))
        if complete:
            compared["invocations"] += 1
            central = (estimate.get("by_stage") or {}).get("central") or {}
            for stage in STAGES:
                by_stage[stage]["estimate_central_usd"] += pl._decimal(central.get(stage))
                by_stage[stage]["settled_usd"] += actual[stage]
            for key in ("central", "high", "stress"):
                compared[key] += pl._decimal(estimate.get(f"{key}_usd"))
            compared["saving"] += pl._decimal((estimate.get("prompt_cache_saving_usd") or {}).get("central"))
            compared["settled"] += settled
        else:
            stopped += 1
        status = entry.get("status")
        rows.append({"started_at": entry.get("started_at"), "stage": entry.get("stage"),
                     "status": "complete" if complete else (status if status != "this invocation" else "stopped"),
                     "compared": complete, "stop_reason": (entry.get("stop") or {}).get("reason"),
                     "limit_usd": record.get("limit_usd"), "source": record.get("source"),
                     "estimate_central_usd": estimate.get("central_usd"), "estimate_high_usd": estimate.get("high_usd"),
                     "estimate_stress_usd": estimate.get("stress_usd"), "settled_usd": _money(settled, "0.0001"),
                     "uncertain_usd": record.get("uncertain_usd"), "peak_committed_usd": record.get("peak_committed_usd"),
                     "requests": record.get("requests"), "denied": record.get("denied"),
                     "actual_by_stage": {stage: _money(v, "0.0001") for stage, v in actual.items() if v}})
    out = {"invocations": rows,
           "compared": {"invocations": compared["invocations"], "estimate_central_usd": _money(compared["central"]),
                        "estimate_high_usd": _money(compared["high"]), "estimate_stress_usd": _money(compared["stress"]),
                        "prompt_cache_saving_central_usd": _money(compared["saving"], "0.0001"),
                        "settled_usd": _money(compared["settled"], "0.0001")},
           "stopped_invocations": stopped, "settled_usd": _money(settled_all, "0.0001"),
           "uncertain_usd": _money(uncertain_all, "0.0001"),
           "by_stage": {stage: {"label": STAGES[stage][2], "model_key": STAGES[stage][0],
                                "estimate_central_usd": _money(v["estimate_central_usd"], "0.0001"),
                                "settled_usd": _money(v["settled_usd"], "0.0001")}
                        for stage, v in by_stage.items() if v["estimate_central_usd"] or v["settled_usd"]}}
    if spend is not None:
        out["judge_dir_settled_usd"] = spend.get("settled_usd")
    return out
