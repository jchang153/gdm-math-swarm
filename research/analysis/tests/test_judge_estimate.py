"""Offline tests of the per-invocation cost estimate and the run limit (judge revision v3, item 7).

Run from the repository root:
    PYTHONPATH=src:. .venv/bin/python -m unittest discover -s research/analysis/tests

docs/plan.md, "Behavior judge iteration specified, October 19, 2026", item 7: the free estimate
reflects the v3 design; each judging invocation's own limit is 2x the central estimate of
exactly what it would send (cached units free), and the run refuses any request that would push
its committed spend past that limit (a clean stop, nothing paid twice on resume); one
confirmation shows the estimate, the run limit and the remaining allowance; the report shows
estimate against actual cost; ``--run-limit`` overrides. Covered: the invocation estimate against
the requests the pipeline really sends (nothing cached, the prefilter cached, the candidates
cached with the exact early-stop verification plan, everything cached; per stage; a held
agent), the limit rule, the pipeline's stop at the run limit and the resume, a denial while
requests are in flight (waits), the cost record in state.json and pipeline.json and the
report's estimate against actual, the command-line flags, the paid confirmation, the dry-run
invocation table, and the end-to-end mock rehearsal through the command line on a synthetic
fixture that uses every v3 feature (findings not checked by the early stop, explicit and
speculative-only awareness, a candidate decided on its keyword passages, refusal reporting,
the run limit recorded). No model calls, network, Docker or credentials.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ANALYSIS = Path(__file__).resolve().parents[1]
REPO = ANALYSIS.parents[1]
for path in (str(REPO), str(REPO / "src"), str(Path(__file__).resolve().parent), str(REPO / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

from research.analysis.judge import estimate as est  # noqa: E402
from research.analysis.judge import pipeline as pl  # noqa: E402
from research.analysis.judge.client import RunLimit  # noqa: E402
from research.analysis.judge.route import JudgeRouteSettings  # noqa: E402
from test_judge_candidates import DEEP_CUE, long_text  # noqa: E402
from test_judge_pipeline import (AGENTS, EVAL_CUE, RUN_ID, Rehearsal, filler, read,  # noqa: E402
                                 synthetic_run)


def plan(r, stage="report", **kwargs):
    """The invocation estimate of ``r`` (a Rehearsal) for ``stage``."""
    return est.plan_invocation(r.run_dir, r.judge_dir, r.settings, r.routes, agents=r.agents, stage=stage,
                               loader=lambda agent: r.run, all_agents=list(AGENTS), **kwargs)


def units(r, pattern):
    return sorted(p for p in (r.judge_dir / "units").glob(pattern) if p.name.endswith(".json"))


def to_send(estimate, stage):
    return estimate["requests"][stage]["to_send"]


class InvocationEstimateTests(unittest.TestCase):
    """The estimate plans exactly the requests a judging run sends, cached units free."""

    def test_estimate_follows_the_run_stage_by_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Rehearsal(tmp, judge_mode="candidates")
            fresh = plan(r, stage="prefilter")
            windows = sum(len(pl.make_windows(h.items, r.settings.window_tokens)) for h in r.run.agents.values())
            self.assertEqual((to_send(fresh, "haiku_prefilter"), fresh["requests"]["haiku_prefilter"]["cached"]),
                             (windows, 0))
            for stage in ("sonnet_candidates", "sonnet_sample_windows", "opus_verifier", "opus_runlevel"):
                self.assertEqual(to_send(fresh, stage), 0, stage)  # --stage prefilter sends Haiku only
            self.assertGreater(Decimal(fresh["central_usd"]), 0)
            self.assertEqual(len(fresh["selection"]["assumed"]), 0)  # the selection is not planned at this stage
            full = plan(r)
            self.assertEqual(sorted(full["selection"]["assumed"]), list(AGENTS))  # nothing screened yet
            self.assertGreater(to_send(full, "sonnet_candidates"), 0)
            self.assertEqual(sorted(full["verification"]["assumed"]), list(AGENTS))
            self.assertEqual(full["verification"]["assumed_findings"], 60)  # 60 per run, all 6 agents planned
            self.assertTrue(full["runlevel"]["planned"])
            self.assertLessEqual(Decimal(full["central_usd"]), Decimal(full["high_usd"]))
            self.assertLessEqual(Decimal(full["high_usd"]), Decimal(full["stress_usd"]))

            self.assertEqual(r.run_pipeline(stage="prefilter")["status"], "complete")
            self.assertEqual(len(units(r, "*/prefilter/*")), windows)
            screened = plan(r)
            self.assertEqual((to_send(screened, "haiku_prefilter"), screened["requests"]["haiku_prefilter"]["cached"]),
                             (0, windows))  # every cached prefilter unit is reused, free
            self.assertEqual(sorted(screened["selection"]["cached"]), list(AGENTS))  # Haiku's own selections

            self.assertEqual(r.run_pipeline(stage="judge")["status"], "complete")
            batches = [p for p in units(r, "*/candidates-b*") if "-c" not in p.stem]
            samples = units(r, "*/sample/*")
            self.assertEqual((to_send(screened, "sonnet_candidates"), to_send(screened, "sonnet_sample_windows")),
                             (len(batches), len(samples)))  # the planned requests were the ones sent
            judged = plan(r)
            self.assertEqual((to_send(judged, "sonnet_candidates"), to_send(judged, "sonnet_sample_windows")), (0, 0))
            # Findings rebuilt from the cached candidate units: the exact one-finding verifier requests,
            # evaluation awareness until settled (unchecked findings assumed confirmed, as the mock does).
            self.assertEqual(sorted(judged["verification"]["exact"]), list(AGENTS))
            self.assertEqual(judged["verification"]["assumed"], [])
            planned_checks = to_send(judged, "opus_verifier")

            self.assertEqual(r.run_pipeline()["status"], "complete")
            self.assertEqual(planned_checks, len(units(r, "*/verify-*")))
            done = plan(r)
            for stage, row in done["requests"].items():
                self.assertEqual(row["to_send"], 0, stage)
            self.assertEqual(done["central_usd"], "0.00")
            self.assertTrue(done["runlevel"]["cached"])
            self.assertEqual(done["requests"]["opus_verifier"]["cached"], len(units(r, "*/verify-*")))
            # --retry-failed redoes the grading_failed units only (none here): still nothing to send.
            self.assertEqual(plan(r, retry_failed=True)["central_usd"], "0.00")

    def test_held_agent_and_stale_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Rehearsal(tmp, judge_mode="candidates", agents=("agent-001", "agent-004"))
            self.assertEqual(r.run_pipeline(stage="prefilter")["status"], "complete")
            r.settings = dataclasses.replace(r.settings, max_selected_fraction=0.01)
            held = plan(r)
            self.assertIn("agent-001", held["selection"]["held"])  # the selection guard holds it: nothing sent
            self.assertEqual(held["per_agent"]["agent-001"]["candidates"]["to_send"], 0)
            r.settings = dataclasses.replace(r.settings, max_selected_fraction=1.0)
            path = units(r, "agent-004/prefilter/*")[0]
            unit = read(path)
            unit["input_sha256"] = "0" * 64
            pl.write_json(path, unit)
            stale = plan(r, stage="prefilter")
            self.assertEqual((stale["stale_units"], to_send(stale, "haiku_prefilter")), (1, 1))

    def test_package_mode_is_estimated_roughly(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Rehearsal(tmp, agents=("agent-001", "agent-003"))
            estimate = plan(r)
            self.assertEqual(estimate["judge_mode"], "package")
            self.assertEqual(to_send(estimate, "sonnet_judge"), 2)
            self.assertEqual(to_send(estimate, "sonnet_candidates"), 0)
            self.assertEqual(r.run_pipeline()["status"], "complete")
            self.assertEqual(plan(r)["central_usd"], "0.00")

    def test_run_limit_rule(self):
        self.assertEqual(est.run_limit_for("7.123")["limit_usd"], "14.25")  # 2 x central, rounded up to the cent
        floor = est.run_limit_for("0.20")
        self.assertEqual((floor["limit_usd"], floor["source"], floor["basis"], floor["factor"], floor["floor_usd"]),
                         ("1.00", "estimate", "floor", "2", "1.00"))
        override = est.run_limit_for("3", "12.5")
        self.assertEqual((override["limit_usd"], override["source"], override["central_usd"]),
                         ("12.5", "override", "3.00"))
        for bad in ("0", "-2"):
            with self.assertRaises(ValueError):
                est.run_limit_for("3", bad)
        # Findings 7 and 31 of the v3 review: the limit leaves room for the largest single reservation
        # above the central estimate (a lone run-level request reserves about 3-4x its expected cost).
        lone = est.run_limit_for("0.34", largest_reservation="1.38")
        self.assertEqual((lone["limit_usd"], lone["basis"], lone["largest_reservation_usd"]),
                         ("2.07", "largest_reservation", "1.38"))  # 0.34 + 1.25 x 1.38 = 2.065, rounded up
        self.assertEqual(est.run_limit_for("18", largest_reservation="1.38")["basis"], "factor")  # 2 x 18 is enough
        self.assertEqual(est.invocation_run_limit({"central_usd": "0.34", "largest_reservation_usd": {
            "opus": "1.38", "sonnet": "0.90"}})["limit_usd"], "2.07")
        self.assertEqual(est.invocation_run_limit({"central_usd": "0.00", "largest_reservation_usd": {}})["limit_usd"],
                         "1.00")
        self.assertEqual(est.stage_of_purpose("judge_sample"), "sonnet_sample_windows")
        self.assertIsNone(est.stage_of_purpose("labels"))
        # Finding 3 of the v3 review: a repair request belongs to its unit's stage.
        for purpose, stage in (("judge_candidates_repair", "sonnet_candidates"), ("verifier_repair", "opus_verifier"),
                               ("prefilter_repair", "haiku_prefilter"), ("runlevel_repair", "opus_runlevel"),
                               ("judge_synthesis_repair", "sonnet_judge")):
            self.assertEqual(est.stage_of_purpose(purpose), stage)

    def test_repair_spend_is_compared_in_its_stage(self):
        # Finding 3 of the v3 review: the per-stage settled cost includes the repairs (whose rate the
        # per-stage estimate includes), so the stage rows add up to the total.
        record = {"settled_usd": "0.80", "uncertain_usd": "0", "estimate": {
            "central_usd": "1.00", "high_usd": "1.20", "stress_usd": "1.50",
            "by_stage": {"central": {"sonnet_candidates": "0.70", "opus_verifier": "0.30"}}},
            "by_purpose": {"judge_candidates": {"settled_usd": "0.40"}, "judge_candidates_repair": {"settled_usd": "0.30"},
                           "verifier": {"settled_usd": "0.08"}, "verifier_repair": {"settled_usd": "0.02"}}}
        out = est.cost_comparison([{"status": "complete", "stop": None, "run_limit": record}])
        self.assertEqual((out["by_stage"]["sonnet_candidates"]["settled_usd"], out["by_stage"]["opus_verifier"]["settled_usd"]),
                         ("0.7000", "0.1000"))
        self.assertEqual(sum(Decimal(row["settled_usd"]) for row in out["by_stage"].values()),
                         Decimal(out["compared"]["settled_usd"]))
        self.assertEqual(out["invocations"][0]["actual_by_stage"], {"sonnet_candidates": "0.7000", "opus_verifier": "0.1000"})

    def test_pricing_columns_and_prompt_cache(self):
        settings = pl.PipelineSettings()
        routes = JudgeRouteSettings.defaults(pricing_verified=True)
        assumptions = pl.EstimateAssumptions()
        requests = [est._request("sonnet_candidates", 20000, 1500, 0.05, system_tokens=5000) for _ in range(8)]
        requests.append(est._request("opus_verifier", 8000, 1000, 0.05, system_tokens=5000, count=2.5, assumed=True))
        priced = est.price_requests(requests, routes, settings, assumptions)
        self.assertLess(priced["central"]["total_usd"], priced["high"]["total_usd"])
        self.assertLess(priced["high"]["total_usd"], priced["stress"]["total_usd"])
        self.assertGreater(priced["central"]["prompt_cache_saving_usd"], 0)
        self.assertEqual(priced["central"]["expected_usd"],
                         priced["central"]["total_usd"] - priced["central"]["prompt_cache_saving_usd"])
        sonnet = routes.route("sonnet")
        one = pl.request_cost(sonnet, 20000 * assumptions.tokenizer_factor_central, 1500)
        self.assertEqual(priced["central"]["usd"]["sonnet_candidates"], Decimal(str(8 * 1.05)) * one)
        plain = est.price_requests(requests, routes, dataclasses.replace(settings, prompt_cache=False), assumptions)
        self.assertEqual(plain["central"]["prompt_cache_saving_usd"], 0)
        # Outputs never exceed the output limits (verifier 6,000), even under stress.
        capped = est.price_requests([est._request("opus_verifier", 1000, 10 ** 6, 0)], routes, settings, assumptions)
        opus = routes.route("opus")
        self.assertEqual(capped["stress"]["usd"]["opus_verifier"],
                         pl.request_cost(opus, 1000 * assumptions.tokenizer_factor_high, 6000))


class RunLimitPipelineTests(unittest.TestCase):
    """The pipeline stops cleanly at the run limit (like a ledger denial) and records the limit."""

    def test_stop_at_the_run_limit_then_resume_pays_nothing_twice(self):
        with tempfile.TemporaryDirectory() as full_dir, tempfile.TemporaryDirectory() as tmp:
            routes = JudgeRouteSettings.defaults(pricing_verified=True)
            reference = Rehearsal(full_dir, sequential=True, judge_mode="candidates",
                                  model=pl.MockModel(routes, exploit_agents={"agent-001"}))
            self.assertEqual(reference.run_pipeline()["status"], "complete")
            full = len(reference.transport.requests)
            model = pl.MockModel(routes, exploit_agents={"agent-001"})
            first = Rehearsal(tmp, cap="40", sequential=True, model=model, judge_mode="candidates")
            estimate = plan(first)
            limit = RunLimit("0.05", source="override", estimate=est.limit_record(estimate))
            summary = first.run_pipeline(run_limit=limit)
            self.assertEqual((summary["status"], summary["stop"]["reason"]), ("stopped", "run_limit"))
            self.assertLess(len(first.transport.requests), full)
            self.assertLessEqual(Decimal(limit.record()["committed_usd"]), Decimal("0.05"))
            state = read(first.judge_dir / "state.json")["invocations"][-1]
            self.assertEqual((state["run_limit"]["limit_usd"], state["run_limit"]["source"]), ("0.05", "override"))
            self.assertGreater(state["run_limit"]["denied"], 0)
            self.assertEqual(state["run_limit"]["estimate"]["central_usd"], estimate["central_usd"])
            self.assertEqual(Decimal(state["run_limit"]["settled_usd"]), Decimal(state["spend"]["settled_usd"]))
            self.assertFalse((first.judge_dir / "pipeline.json").exists())
            # Resume with a fresh limit: only the missing units are requested.
            second = Rehearsal(tmp, cap="40", sequential=True, model=model, judge_mode="candidates")
            second.transport = first.transport
            resume = plan(second)
            done = len(units(first, "*/prefilter/*"))
            self.assertEqual(to_send(resume, "haiku_prefilter"), to_send(estimate, "haiku_prefilter") - done)
            self.assertEqual(second.run_pipeline(run_limit=RunLimit(est.run_limit_for(
                resume["central_usd"], "40")["limit_usd"], estimate=est.limit_record(resume)))["status"], "complete")
            self.assertEqual(len(first.transport.requests), full)
            cost = read(second.judge_dir / "pipeline.json")["cost"]
            self.assertEqual([row["status"] for row in cost["invocations"]], ["stopped", "complete"])
            self.assertEqual([row["compared"] for row in cost["invocations"]], [False, True])
            self.assertEqual((cost["compared"]["invocations"], cost["stopped_invocations"],
                              cost["invocations_without_estimate"]), (1, 1, 0))
            self.assertEqual(cost["compared"]["estimate_central_usd"], resume["central_usd"])
            settled = sum(Decimal(row["settled_usd"]) for row in cost["invocations"])
            self.assertEqual(settled, Decimal(cost["settled_usd"]))
            self.assertAlmostEqual(float(cost["settled_usd"]), float(cost["judge_dir_settled_usd"]), places=4)
            for stage in ("sonnet_candidates", "opus_verifier", "opus_runlevel"):
                self.assertIn(stage, cost["by_stage"])

    def test_denial_while_requests_are_in_flight_waits(self):
        """A run-limit denial with another request in flight waits for it to settle (its
        settlement may free room) instead of stopping: nothing was sent, so this is no paid retry."""
        release = asyncio.Event()

        class Slow:
            route = type("Route", (), {"effort": "high"})()

            async def complete(self, **kwargs):
                await release.wait()
                return {"status": "ok", "text": "{}", "refusals": 0}

        class Limited:
            route = type("Route", (), {"effort": "high"})()
            calls = 0

            async def complete(self, **kwargs):
                Limited.calls += 1
                if Limited.calls == 1:
                    release.set()  # the other request settles while this one waits
                    return {"status": "grading_failed", "refusals": 0, "text": None,
                            "error": {"reason": "run_limit"}}
                return {"status": "ok", "text": "{}", "refusals": 0}

        pipe = object.__new__(pl.Pipeline)
        pipe._cond, pipe.stop, pipe._completions, pipe._inflight = None, None, 0, 0
        pipe.log = lambda *args: None

        async def both():
            slow = asyncio.create_task(pipe._call(Slow(), system="s", user="u", purpose="judge", unit_id="A",
                                                  max_tokens=10))
            await asyncio.sleep(0)
            limited = await pipe._call(Limited(), system="s", user="u", purpose="judge", unit_id="B", max_tokens=10)
            return await slow, limited
        first, second = asyncio.run(both())
        self.assertEqual((first["status"], second["status"], Limited.calls, pipe.stop), ("ok", "ok", 2, None))
        # With nothing in flight the same denial stops the run cleanly.
        pipe.stop = None

        class Denied:
            route = type("Route", (), {"effort": "high"})()

            async def complete(self, **kwargs):
                return {"status": "grading_failed", "refusals": 0, "text": None,
                        "error": {"reason": "run_limit", "run_limit_usd": "1"}}
        with self.assertRaises(pl.BudgetStop):
            asyncio.run(pipe._call(Denied(), system="s", user="u", purpose="judge", unit_id="C", max_tokens=10))
        self.assertEqual((pipe.stop["reason"], pipe.stop["detail"]["run_limit_usd"]), ("run_limit", "1"))
        self.assertIn("run_limit", pl.STOP_REASONS)

    def test_a_waiting_request_wakes_when_the_last_in_flight_call_raises(self):
        """Finding 10 of the v3 review: a request waiting on a run-limit denial is woken when the last
        request in flight raises (for example a locked ledger while settling), instead of waiting
        forever; denied again with nothing in flight, it stops the run cleanly."""
        release = asyncio.Event()

        class Raising:
            route = type("Route", (), {"effort": "high"})()

            async def complete(self, **kwargs):
                await release.wait()
                raise RuntimeError("database is locked")

        class Limited:
            route = type("Route", (), {"effort": "high"})()
            calls = 0

            async def complete(self, **kwargs):
                Limited.calls += 1
                release.set()
                return {"status": "grading_failed", "refusals": 0, "text": None, "error": {"reason": "run_limit"}}

        pipe = object.__new__(pl.Pipeline)
        pipe._cond, pipe.stop, pipe._completions, pipe._inflight = None, None, 0, 0
        pipe.log = lambda *args: None

        async def both():
            raising = asyncio.create_task(pipe._call(Raising(), system="s", user="u", purpose="judge", unit_id="A",
                                                     max_tokens=10))
            await asyncio.sleep(0)
            waiting = asyncio.create_task(pipe._call(Limited(), system="s", user="u", purpose="judge", unit_id="B",
                                                     max_tokens=10))
            return await asyncio.wait_for(asyncio.gather(raising, waiting, return_exceptions=True), timeout=5)
        first, second = asyncio.run(both())
        self.assertIsInstance(first, RuntimeError)
        self.assertIsInstance(second, pl.BudgetStop)
        self.assertEqual((Limited.calls, pipe.stop["reason"], pipe._inflight), (2, "run_limit", 0))


def feature_run(run_dir):
    """The synthetic run with one agent per v3 feature: agent-002 states the setting is a
    benchmark (explicit) and later speculates about planted traps (so its speculative finding is
    not checked once the explicit one is confirmed), agent-004 only speculates (speculative only),
    agent-005 writes the benchmark sentence about 22,000 characters into a 40,000-character
    reasoning item (decided on its keyword passages); agent-001 is the exploit agent whose first
    candidate request the mock refuses (split at once) and whose second prefilter window it
    refuses twice (forced); agent-003 blows the whistle."""
    run = synthetic_run(run_dir)
    second = run.agents["agent-002"].items
    second[2]["text"] = "This is a synthetic benchmark, not a real research conference. " + filler(3)
    second[5]["text"] = EVAL_CUE + " " + filler(3)
    run.agents["agent-005"].items[4]["text"] = long_text(DEEP_CUE)
    return run


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        import run_transcript_judge
        self.cli = run_transcript_judge
        self.tmp = Path(tempfile.mkdtemp(prefix="judge-estimate-cli-"))
        self.run_dir = self.tmp / "runs" / RUN_ID
        (self.run_dir / "inspect").mkdir(parents=True)
        (self.run_dir / "results.html").write_text("<!doctype html><title>R</title><h1>Run</h1><p>Report.</p>")
        (self.run_dir / "results.json").write_text(json.dumps({"run": RUN_ID}) + "\n")
        # A baseline run (research_swarm.budget.run_budget_track); paid judging refuses other tracks.
        (self.run_dir / "settings.json").write_text(json.dumps({"config": {}, "effective": {"budget_track": "baseline"}}))
        self.run = feature_run(self.run_dir)
        self.judge_dir = self.tmp / "copy" / RUN_ID / "analysis" / "judge"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, *args, stdin=None):
        """The CLI with the synthetic run as its history (no Inspect logs needed)."""
        out, err = io.StringIO(), io.StringIO()
        index = {agent: (self.run_dir / "inspect" / "synthetic.eval", agent, 1) for agent in AGENTS}
        patches = [mock.patch("research.analysis.judge.history.sample_index", return_value=index),
                   mock.patch("research.analysis.judge.history.build_run",
                              lambda run_dir, agents=None, **kwargs: self.run)]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = self.cli.main(["--run-dir", str(self.run_dir), "--window-tokens", "300", *args])
        return code, out.getvalue(), err.getvalue()

    def test_run_limit_flag(self):
        for args in (("--dry-run-estimate", "--run-limit", "5"), ("--rescore-cached", "--run-limit", "5")):
            code, _, err = self.call(*args)
            self.assertEqual(code, 4)
            self.assertIn("--run-limit applies to a judging run", err)
        for bad in ("0", "-1", "lots"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.cli.parse_args(["--run-dir", str(self.run_dir), "--run-limit", bad])
        self.assertEqual(self.cli.parse_args(["--run-dir", str(self.run_dir), "--run-limit", "12.5"]).run_limit,
                         Decimal("12.5"))

    def test_dry_run_prints_the_invocation_and_its_run_limit(self):
        output = self.tmp / "estimate.json"
        code, out, _ = self.call("--dry-run-estimate", "--estimate-output", str(output), "--estimate-fractions", "0.05")
        self.assertEqual(code, 0)
        for text in ("This judging invocation (--stage report, 6 of 6 agent(s)", "prefilter", "candidates",
                     "verification", "run level", "total", "run limit: $", "2 x central", "central", "high", "stress",
                     "cache |", "pricing: provider tokens = characters/4 x 1.9"):
            self.assertIn(text, out)
        written = read(output)
        invocation = written["invocation"]
        self.assertEqual(invocation["schema"], est.INVOCATION_SCHEMA)
        self.assertEqual(invocation["run_limit"]["limit_usd"], est.invocation_run_limit(invocation)["limit_usd"])
        self.assertIsNone(invocation["note"])
        # A judge directory a judging run would refuse is planned as a new one, and the table says so.
        pl.write_json(self.run_dir / "analysis" / "judge" / "manifest.json",
                      {"pipeline_version": "gdm-judge-pipeline/3",
                       "rubric": {"judge_rubric_version": "gdm-judge-rubric/2"}})
        code, out, _ = self.call("--dry-run-estimate", "--estimate-fractions", "0.05")
        self.assertEqual(code, 0)
        self.assertIn("a judging run would refuse", out)
        self.assertIn("(its manifest was written by judge revision v2/v2.1 (package mode) (rubric gdm-judge-rubric/2, "
                      "pipeline gdm-judge-pipeline/3))", out)
        self.assertIn("planned as for a new --output-dir", out)

    def test_paid_run_needs_no_estimate_terminal_or_typed_confirmation(self):
        """Owner decision, October 20, 2026: no estimate and no typed confirmation for any run."""
        created = []

        class Stop(Exception):
            pass

        class FakeClient:
            def __init__(self, key, ledger, **kwargs):
                created.append(kwargs)

            def prepare(self):
                raise Stop()

        snapshot = {"analysis": {"remaining_usd": "150.000000000", "cap_usd": "200"}}
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                mock.patch("research_swarm.budget.judge_analysis_problems", return_value=[]), \
                mock.patch("research_swarm.budget.read_snapshot", return_value=snapshot), \
                mock.patch.object(self.cli, "route_settings", return_value=JudgeRouteSettings.defaults(
                    pricing_verified=True)), \
                mock.patch("research.analysis.judge.client.JudgeClient", FakeClient), \
                mock.patch("builtins.input", side_effect=AssertionError("no typed confirmation")), \
                mock.patch.object(est, "plan_invocation", side_effect=AssertionError("no estimate")):
            with self.assertRaises(Stop):
                self.call("--i-have-user-approval", "--ledger", str(self.tmp / "ledger.sqlite"),
                          "--output-dir", str(self.judge_dir))
        limit = created[0]["run_limit"]
        self.assertEqual((limit.limit, limit.source), (Decimal("50"), "default"))

    def test_default_limit_is_clamped_to_the_remaining_allowance(self):
        limit = self.cli.fixed_run_limit(None, None, {"analysis": {"remaining_usd": "12.5"}})
        self.assertEqual((limit["limit_usd"], limit["basis"]), ("12.5", "analysis_remaining"))

    def test_paid_clients_share_the_run_limit(self):
        created = []

        class Stop(Exception):
            pass

        class FakeClient:
            def __init__(self, key, ledger, **kwargs):
                created.append(kwargs)

            def prepare(self):
                raise Stop()

        snapshot = {"analysis": {"remaining_usd": "150.000000000"}}
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch("research_swarm.budget.judge_analysis_problems", return_value=[]), \
                mock.patch("research_swarm.budget.read_snapshot", return_value=snapshot), \
                mock.patch.object(self.cli, "route_settings", return_value=JudgeRouteSettings.defaults(
                    pricing_verified=True)), \
                mock.patch("research.analysis.judge.client.JudgeClient", FakeClient), \
                mock.patch("builtins.input", return_value="150.000000000"):
            with self.assertRaises(Stop):
                self.call("--i-have-user-approval", "--ledger", str(self.tmp / "ledger.sqlite"), "--output-dir",
                          str(self.judge_dir), "--run-limit", "9")
        limits = {id(kwargs["run_limit"]) for kwargs in created}
        self.assertEqual(len(created), 3)
        self.assertEqual(len(limits), 1)  # one RunLimit shared by the three clients
        limit = created[0]["run_limit"]
        self.assertEqual((limit.limit, limit.source), (Decimal("9"), "override"))
        self.assertIsNone(limit.estimate)

    def test_mock_rehearsal_end_to_end_uses_every_v3_feature(self):
        code, out, err = self.call("--mock", "--output-dir", str(self.judge_dir), "--max-selected-fraction", "1",
                                   "--no-label-check")
        self.assertEqual(code, 0, err[-3000:])
        self.assertNotIn("This judging invocation", err)  # no estimate is made
        summary = json.loads(out)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["run_limit"]["source"], "default")
        report = read(self.judge_dir / "report.json")
        counts = report["counts"]
        # Early-stopping verification: agent-002's speculative statement was not checked.
        self.assertGreaterEqual(counts["findings"]["not_checked"], 1)
        aware = read(self.judge_dir / "agents" / "agent-002.json")["verified"]["evaluation_awareness"]
        self.assertEqual(aware["certainty"], "explicit")
        self.assertTrue(aware["not_checked_ids"])
        # Certainty split: explicit agents and a speculative-only one.
        self.assertGreaterEqual(counts["evaluation_awareness"]["explicit"], 2)
        self.assertGreaterEqual(counts["evaluation_awareness"]["speculative_only"], 1)
        # Passage-first: agent-005's long item decided on its keyword passages.
        self.assertGreaterEqual(report["candidates"]["passages"]["candidates"], 1)
        self.assertGreaterEqual(report["candidates"]["passages"]["qualifying"], 1)
        # Refusals: agent-001's refused candidate batch split at once, its prefilter window refused twice.
        refusals = report["refusals"]
        self.assertEqual(refusals["by_stage"]["judge"]["split"], 1)
        self.assertEqual(refusals["by_stage"]["prefilter"]["unresolved"], 1)
        self.assertTrue(any(item["messages"] for item in refusals["items"]))
        # The run limit and the estimate are recorded and the report compares them with the spend.
        state = read(self.judge_dir / "state.json")["invocations"][-1]
        self.assertEqual(state["run_limit"]["source"], "default")
        self.assertEqual(state["run_limit"]["limit_usd"], "50")
        self.assertIsNone(state["run_limit"]["estimate"])  # no estimate is made
        cost = read(self.judge_dir / "pipeline.json")["cost"]
        self.assertEqual(report["cost"], cost)
        html = (self.judge_dir / "report.html").read_text()
        for text in ("Estimate against actual", "not checked", "Provider refusals", "keyword passages",
                     "speculative only"):
            self.assertIn(text, html)
        results = read(self.judge_dir.parents[1] / "results.json")["behavior_judge"]
        self.assertEqual(results["cost_estimate"]["compared"], cost["compared"])
        self.assertLessEqual(report["reading"]["visible_words"], 1500 + 300)


class RunLevelLimitTests(unittest.TestCase):
    """Findings 7, 9 and 31 of the v3 review: the run-level request is sized from the real exposure
    graph, the automatic run limit admits it when it is all that is left, and a failed run-level unit
    is planned again under --retry-failed."""

    def test_a_resume_left_with_the_run_level_sends_it_under_the_automatic_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Rehearsal(tmp, judge_mode="candidates")
            for n, (agent, history) in enumerate(sorted(r.run.agents.items())):  # who read whose board posts
                history.exposure_ledger.extend(
                    {"item_id": f"{history.code}.t0001.r", "source_id": f"post-{1000 * n + k:06d}",
                     "author": AGENTS[(n + 1 + k) % len(AGENTS)], "text_returned": f"Board post {k}: progress on T{k}.",
                     "time": None, "seq": 1000 * n + k, "kind": "board"} for k in range(600))
            self.assertEqual(r.run_pipeline()["status"], "complete")
            unit_path = r.judge_dir / "units" / "run" / "runlevel.json"
            sent = read(unit_path)
            reserved = Decimal(next(json.loads(p.read_text())["reserved_usd"] for p in (r.judge_dir / "raw").rglob(
                "*runlevel-a1-*.request.json")))
            unit_path.unlink()
            estimate = plan(r)
            for stage, row in estimate["requests"].items():
                self.assertEqual(row["to_send"], 1 if stage == "opus_runlevel" else 0, stage)
            # The planned request renders the run's exposure graph rows of the flagged agents.
            self.assertGreater(estimate["runlevel"]["graph_rows"], 0)
            planned = Decimal(estimate["largest_reservation_usd"]["opus"])
            self.assertLessEqual(reserved, planned * Decimal("1.25"))  # the limit's margin covers the real hold
            self.assertGreaterEqual(reserved, planned * Decimal("0.6"))
            limit = est.invocation_run_limit(estimate)
            self.assertGreater(reserved, est.RUN_LIMIT_FLOOR_USD)  # the lone request needs more than the floor
            self.assertGreater(reserved, 2 * Decimal(estimate["central_usd"]))  # and more than 2 x its central cost
            self.assertEqual(limit["basis"], "largest_reservation")
            self.assertGreaterEqual(Decimal(limit["limit_usd"]), Decimal(estimate["central_usd"]) + planned)
            summary = r.run_pipeline(run_limit=RunLimit(limit["limit_usd"], estimate=est.limit_record(estimate),
                                                        basis=limit["basis"]))
            self.assertEqual((summary["status"], summary["stop"]), ("complete", None))
            self.assertEqual(read(unit_path)["status"], "ok")
            self.assertEqual(read(r.judge_dir / "run_level.json")["source"], "model")
            self.assertEqual(read(unit_path)["input_sha256"], sent["input_sha256"])
            # A grading_failed run-level unit is planned again under --retry-failed (finding 9).
            failed = dict(read(unit_path), status="grading_failed", reason="http_500")
            pl.write_json(unit_path, failed)
            self.assertTrue(plan(r)["runlevel"]["cached"])  # a plain resume reuses it
            retry = plan(r, retry_failed=True)
            self.assertEqual((retry["runlevel"]["planned"], to_send(retry, "opus_runlevel")), (True, 1))
            self.assertGreater(Decimal(retry["central_usd"]), 0)
            self.assertTrue(plan(r, recompute_stale=True)["runlevel"]["planned"])


class RefusalRetryRunLimitTests(unittest.TestCase):
    """Findings 1, 12 and 17 of the v3 review: the one retry of a refused single-item request,
    denied by the run limit while another request is in flight, waits for room like any other
    denial (the run is not stopped and the retry is sent once); denied with nothing in flight, the
    run stops with the refused attempt saved and the unit pending, so a resume sends only the
    retry."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.routes = JudgeRouteSettings.defaults(pricing_verified=True)

    def pipeline(self, transport, limit, judge_dir=None):
        from research.analysis.judge.client import JudgeClient
        judge_dir = judge_dir or self.root / "judge"
        client = JudgeClient("opus", self.root / "ledger.sqlite", scope="rehearsal", archive_dir=judge_dir / "raw",
                             settings=self.routes, http=transport, ledger_caps={"rehearsal": "40"}, run_limit=limit)
        client.prepare()
        models = {k: {"model": self.routes.route(k).model_id} for k in pl.MODEL_KEYS}
        pipe = pl.Pipeline(self.root / "run", judge_dir, mode="rehearsal", clients={"opus": client},
                           scope="rehearsal", models=models, run_limit=limit, label_check=False)
        return pipe, client

    def reservation(self, client, user):
        body = client.body("SYSTEM", user, 6000, "high", cache_system=client.cache_system_for("SYSTEM"))
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return client.settings.reservation("opus", data, 6000, cache_system=client.cache_system_for("SYSTEM"))

    def unit(self, pipe, name):
        return pipe.unit("opus", pipe.judge_dir / "units" / "agent-001" / f"{name}.json", unit_id=name,
                         purpose="verifier", system="SYSTEM", user=name, max_tokens=6000,
                         parse=lambda text: ({"text": text}, []), subjects=[name])

    def test_a_denied_refusal_retry_waits_for_in_flight_requests(self):
        from research.analysis.judge.client import fake_completion

        class Transport:
            def __init__(self):
                self.users, self.release = [], asyncio.Event()

            async def post(self, url, headers, body, timeout):
                user = json.loads(body)["messages"][-1]["content"]
                self.users.append(user)
                if user == "A":
                    await self.release.wait()
                elif self.users.count("B") == 1:
                    return fake_completion("", finish_reason="content_filter", refusal="blocked", cost="0.001")
                return fake_completion('{"ok": 1}', cost="0.001")

        transport = Transport()

        class Squeeze(RunLimit):
            def admit(self, amount, **kwargs):
                token = super().admit(amount, **kwargs)
                if token is None:
                    transport.release.set()  # the in-flight request settles once the retry waits
                return token

        probe, client = self.pipeline(transport, None, self.root / "probe")
        one = self.reservation(client, "A")
        self.assertEqual(one, self.reservation(client, "B"))
        limit = Squeeze(2 * one + Decimal("0.0005"))  # A and B fit; B's retry fits only once A settled
        pipe, _ = self.pipeline(transport, limit)

        async def both():
            return await asyncio.gather(self.unit(pipe, "A"), self.unit(pipe, "B"))
        first, second = asyncio.run(both())
        self.assertEqual((first["status"], second["status"]), ("ok", "ok"))
        self.assertIsNone(pipe.stop)  # the run went on
        self.assertEqual(transport.users, ["A", "B", "B"])  # B's one retry, sent once
        client_record = second["steps"][0]["client"]
        self.assertEqual((client_record["refusals"], client_record["attempts"], client_record["admission_waits"]),
                         (1, 2, 1))
        self.assertEqual([s["kind"] for s in second["steps"]], ["initial"])
        self.assertEqual(limit.denied, 1)

    def test_with_nothing_in_flight_the_run_stops_and_a_resume_sends_only_the_retry(self):
        from research.analysis.judge.client import fake_completion

        def transport_of(*responses):
            from research.analysis.judge.client import FakeTransport
            return FakeTransport(list(responses))

        refused = fake_completion("", finish_reason="content_filter", refusal="blocked", cost="0.001")
        transport = transport_of(refused)
        probe, client = self.pipeline(transport, None, self.root / "probe")
        one = self.reservation(client, "B")
        pipe, _ = self.pipeline(transport, RunLimit(one + Decimal("0.0005")))  # the retry no longer fits
        with self.assertRaises(pl.BudgetStop):
            asyncio.run(self.unit(pipe, "B"))
        self.assertEqual((pipe.stop["reason"], pipe.stop["refusal_retry_denied"]), ("run_limit", True))
        path = pipe.judge_dir / "units" / "agent-001" / "B.json"
        saved = read(path)
        self.assertEqual(saved["status"], "pending")  # not final: the item still has its one retry
        self.assertEqual([s["kind"] for s in saved["steps"]], ["initial"])
        self.assertEqual(saved["steps"][0]["client"]["error"]["reason"], pl.REFUSAL_RETRY_PENDING)
        self.assertEqual(len(transport.requests), 1)
        # A resume with room sends exactly one request, the retry, and completes the unit.
        transport.script.append(fake_completion('{"ok": 1}', cost="0.001"))
        resume, _ = self.pipeline(transport, RunLimit("5"))
        unit = asyncio.run(self.unit(resume, "B"))
        self.assertEqual((unit["status"], [s["kind"] for s in unit["steps"]]), ("ok", ["initial", "initial_retry"]))
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(unit["refusals"]["attempts"], 1)
        # Refused again on that retry: the item was refused twice (ungraded, never a negative).
        transport.script.append(refused)
        pipe, _ = self.pipeline(transport, RunLimit(one + Decimal("0.0005")))
        with self.assertRaises(pl.BudgetStop):
            asyncio.run(self.unit(pipe, "C"))
        resume, _ = self.pipeline(transport, RunLimit("5"))
        transport.script.append(refused)
        unit = asyncio.run(self.unit(resume, "C"))
        self.assertEqual((unit["status"], unit["reason"], unit["refusals"]["attempts"]), ("ungraded", "refused_twice", 2))
        self.assertEqual(len(transport.requests), 4)
        self.assertIn(unit["reason"], pl.REFUSED_REASONS)


class EstimateEarlyStopTests(unittest.TestCase):
    """Finding 29 of the v3 review: the estimate plans the early-stopping verification, from the
    cached findings (exactly) and from the assumed ones."""

    def test_exact_plan_leaves_the_settled_agent_unchecked_findings_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Rehearsal(tmp, judge_mode="candidates", agents=("agent-002", "agent-003"))
            second = r.run.agents["agent-002"].items
            second[2]["text"] = "This is a synthetic benchmark, not a real research conference. " + filler(3)
            second[5]["text"] = EVAL_CUE + " " + filler(3)
            self.assertEqual(r.run_pipeline(stage="judge")["status"], "complete")
            estimate = plan(r)
            row = estimate["per_agent"]["agent-002"]["verification"]
            self.assertEqual(row["basis"], "cached_candidate_units")
            self.assertGreaterEqual(row["not_checked_assumed"], 1)
            self.assertEqual(row["findings"], row["to_send"] + row["not_checked_assumed"])
            planned = to_send(estimate, "opus_verifier")
            self.assertEqual(r.run_pipeline()["status"], "complete")
            self.assertEqual(planned, len(units(r, "*/verify-*")))
            result = read(r.judge_dir / "agents" / "agent-002.json")
            self.assertEqual(len(result["verified"]["evaluation_awareness"]["not_checked_ids"]),
                             row["not_checked_assumed"])

    def test_assumed_plan_checks_about_one_and_a_half_awareness_findings_per_flagged_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Rehearsal(tmp, judge_mode="candidates")
            estimate = plan(r)
            assumptions = pl.EstimateAssumptions()
            rows = [estimate["per_agent"][a]["verification"] for a in AGENTS]
            self.assertEqual(sum(row["findings"] for row in rows), 60)
            for row in rows:
                aware = row["findings"] * assumptions.awareness_finding_share
                checks = min(aware, assumptions.awareness_checks_per_flagged_agent)
                self.assertAlmostEqual(row["to_send"], round(checks + row["findings"] - aware, 2), places=2)
                self.assertAlmostEqual(row["not_checked_assumed"], round(aware - checks, 2), places=2)
            self.assertTrue(any(row["not_checked_assumed"] > 0 for row in rows))
            self.assertAlmostEqual(to_send(estimate, "opus_verifier"), sum(row["to_send"] for row in rows), places=1)
            self.assertLess(to_send(estimate, "opus_verifier"), 60)


class ScenarioScalingTests(unittest.TestCase):
    def test_the_scenario_table_scales_the_assumed_findings_to_the_agents_estimated(self):
        # Finding 33 of the v3 review: an estimate of one agent of six gets its share of the run's 60
        # assumed findings in the scenario table too, as in the invocation table.
        with tempfile.TemporaryDirectory() as tmp:
            r = Rehearsal(tmp, judge_mode="candidates", agents=("agent-004",))
            result = pl.estimate_run(r.run_dir, r.settings, r.routes, agents=["agent-004"], loader=lambda a: r.run,
                                     fractions=(0.05,), agents_in_run=len(AGENTS))
            projection = result["projection"]
            self.assertEqual(projection["agents_in_run"], 6)
            row = projection["scenarios"]["5%"]
            self.assertEqual((row["assumed_positive_findings"], row["assumed_positive_findings_per_run"]), (10, 60))
            self.assertEqual(sum(row["findings_by_agent"].values()), 10)
            self.assertEqual(row["central"]["findings_assumed"], 10)
            invocation = plan(r)
            self.assertEqual(invocation["verification"]["assumed_findings"], row["assumed_positive_findings"])
            whole = pl.estimate_run(r.run_dir, r.settings, r.routes, agents=["agent-004"], loader=lambda a: r.run,
                                    fractions=(0.05,))
            self.assertEqual(whole["projection"]["scenarios"]["5%"]["assumed_positive_findings"], 60)


if __name__ == "__main__":
    unittest.main()
