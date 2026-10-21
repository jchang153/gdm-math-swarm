"""Offline tests for evidence-based launch gates and the W5 launcher/ledger scripts.

Uses the real frozen data files (read-only), temporary evidence/ledger paths, a fake
image-ID lookup and a patched credential-status check. No network, no credential.
"""
import datetime
import hashlib
import importlib.util
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

from research_swarm import launch_gates
from research_swarm.budget import BudgetLedger, list_requests, list_runs, read_snapshot
from research_swarm.dataset import FrozenDataset
from research_swarm.provider import MODEL_ID, ProviderSettings, frozen_fields, settings_from_config

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.datetime(2026, 10, 14, 12, 0, tzinfo=datetime.timezone.utc)
IMAGES = {"gdm-math-swarm-lean:local": "sha256:" + "a" * 64, "gdm-math-swarm-agent:local": "sha256:" + "b" * 64,
          "gdm-math-swarm-comparator:local": "sha256:" + "c" * 64}
CREDENTIALS_OK = {"environment_variable_set": True, "env_file_present": False, "env_file_regular": None,
                  "env_file_private": None, "credentials_read": False}
JUDGE_LIMITS = {"timeout_seconds": 120, "memory": "2g", "cpus": 1}  # configs/pilot.json judge limits


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"w5_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pilot_config(**overrides):
    return {**json.loads((ROOT / "configs" / "pilot.json").read_text()), **overrides}


def ledger_run(path, run_id, scope, budget):
    """A ledger bound to a registered run (the per-run budget)."""
    ledger = BudgetLedger(path)
    ledger.start_run(run_id, scope, budget)
    return ledger


def other_route(route):
    return "deepseek" if route != "deepseek" else "fireworks"


def route_catalog(settings):
    """Fabricate catalog evidence for the configured route and price bounds."""
    return {"data": {"id": settings.model_id, "endpoints": [{
        "tag": settings.provider_route, "status": 0, "context_length": settings.context_length,
        "max_completion_tokens": max(393216, settings.max_tokens),
        "supported_parameters": ["tools", "reasoning", "max_tokens"],
        "pricing": {"prompt": str(Decimal(settings.prompt_usd_per_token) / 2),
                    "completion": str(Decimal(settings.output_usd_per_token) / 2),
                    "input_cache_read": str(Decimal(settings.prompt_usd_per_token) / 100),
                    "overrides": [{"prompt": settings.prompt_usd_per_token,
                                   "completion": settings.output_usd_per_token,
                                   "input_cache_read": str(Decimal(settings.prompt_usd_per_token) / 50)}]}}]}}


def passed_checks(names, **overrides):
    checks = {name: {"passed": True, "required": True, "detail": "ok"} for name in names}
    checks.update(overrides)
    return checks


def e2e_checks(**overrides):
    names = (*launch_gates.REQUIRED_EVIDENCE_CHECKS["runtime-e2e"], "compaction_triggered")
    return passed_checks(names, **overrides)


def isolation_checks(agents=("agent-001", "agent-002"), **overrides):
    names = [f"{a}:{c}" for a in agents for c in launch_gates.ISOLATION_AGENT_CHECKS]
    return passed_checks([*names, *launch_gates.REQUIRED_EVIDENCE_CHECKS["runtime-isolation"]], **overrides)


def pilot_self_compare(skip=()):
    """Passing pristine (and, for answer tasks, bridge) self-comparison entries for the pilot templates."""
    entries = []
    for task in FrozenDataset(ROOT).pilot_tasks:
        for kind in ("pristine", "bridge") if task.answer_based else ("pristine",):
            if (task.task_id, kind) not in skip:
                entries.append({"task_id": task.task_id, "kind": kind, "template_sha256": task.sha256,
                                "status": "illegal_axiom", "axiom": "sorryAx", "real_lean": True, "passed": True})
    return entries


class GateFixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.validation = self.tmp / "validation"
        self.ledger = self.tmp / "state" / "budget.sqlite"
        self.legacy = self.tmp / "runs" / "budget.sqlite"
        self.images = dict(IMAGES)
        patcher = mock.patch("research_swarm.secrets.credential_status", return_value=dict(CREDENTIALS_OK))
        patcher.start()
        self.addCleanup(patcher.stop)
        # evaluator.validation_status runs `docker image inspect`; tests use a fake
        # that accepts the newest evaluator record unless told otherwise.
        self.evaluator_error, self.status_calls = None, []
        patcher = mock.patch("research_swarm.evaluator.validation_status", side_effect=self.fake_validation_status)
        patcher.start()
        self.addCleanup(patcher.stop)
        # runtime.compose_fingerprint (X3) is faked so these gate tests do not depend on
        # the compose text; ``compose_salt`` stands in for an edit to write_compose.
        self.compose_salt, self.fingerprint_calls = "", []
        self.compose_patcher = mock.patch("research_swarm.runtime.compose_fingerprint",
                                          side_effect=self.fake_fingerprint, create=True)
        self.compose_patcher.start()
        self.addCleanup(self.compose_patcher.stop)
        self.compat_ids = []
        self.write_all()

    def fake_fingerprint(self, config, agent_image_id):
        self.fingerprint_calls.append((config, agent_image_id))
        text = json.dumps([self.compose_salt, config.get("agent_memory", "1g"), config.get("agent_cpus"),
                           agent_image_id])
        return hashlib.sha256(text.encode()).hexdigest()

    def fake_validation_status(self, validation_dir, image):
        self.status_calls.append((Path(validation_dir), image))
        path, _ = launch_gates.newest_evidence(validation_dir, "evaluator")
        status = {"record": str(path) if path else None, "valid": False, "error": None, "image_id": None,
                  "self_compared_templates": 0, "bridge_self_compared_templates": 0}
        if path is None or self.evaluator_error:
            return dict(status, error=self.evaluator_error or "No evaluator validation record")
        return dict(status, valid=True, image_id=self.images.get(image), self_compared_templates=14,
                    bridge_self_compared_templates=8)

    def write(self, prefix, record, when=NOW - datetime.timedelta(hours=1)):
        return launch_gates.write_evidence(self.validation, prefix, record, when)

    def write_route(self, catalog=None, settings=None, when=NOW - datetime.timedelta(hours=1), **extra):
        settings = settings or settings_from_config(pilot_config())
        catalog = route_catalog(settings) if catalog is None else catalog
        raw = json.dumps(catalog).encode()
        sha = hashlib.sha256(raw).hexdigest()
        (self.validation / "catalog-snapshots").mkdir(parents=True, exist_ok=True)
        (self.validation / "catalog-snapshots" / f"{sha}.json").write_bytes(raw)
        from dataclasses import asdict
        record = {"valid": True, "credential_used": False, "catalog_sha256": sha,
                  "settings": asdict(settings), "all_required_passed": True, **extra}
        return self.write("route-pricing", record, when)

    def write_all(self):
        lean, agent, comparator = (IMAGES[f"gdm-math-swarm-{r}:local"] for r in ("lean", "agent", "comparator"))
        for prefix in ("declarations", "judge", "timing", "reuse"):
            extra = {"complete": True, "limits": dict(JUDGE_LIMITS)} if prefix in launch_gates.JUDGE_LIMIT_EVIDENCE else {}
            self.write(prefix, {"all_required_passed": True, "image_ids": {"gdm-math-swarm-lean:local": lean,
                                                                          "gdm-math-swarm-deps:local": "sha256:dd"},
                                **extra})
        self.write("conference-race", {"all_required_passed": True, "complete": True, "limits": dict(JUDGE_LIMITS)})
        self.write("evaluator", {"all_required_passed": True, "image_ids": {"comparator": comparator},
                                 "self_compare": pilot_self_compare()})
        config = pilot_config()
        compose = self.fake_fingerprint(config, agent)
        self.write("runtime-e2e", {"all_required_passed": True, "image_ids": {"agent": agent, "lean": lean},
                                   "checks": e2e_checks(), "compose_sha256": compose})
        self.write("runtime-isolation", {"all_required_passed": True, "image_ids": {"gdm-math-swarm-agent:local": agent},
                                         "checks": isolation_checks(), "compose_sha256": compose})
        self.write("runtime-capacity", {"all_required_passed": True, "image_ids": {"agent": agent, "lean": lean},
                                        "validated_config": launch_gates.capacity_config(config),
                                        "checks": passed_checks(launch_gates.REQUIRED_EVIDENCE_CHECKS["runtime-capacity"]),
                                        "compose_sha256": compose})
        self.write("provider-limit", {"all_required_passed": True, "limit": 20, "limit_remaining": 20,
                                      "limit_reset": None, "usage": 0, "checks": {}, "warnings": []})
        self.write_route()
        self.write_compatibility()

    def write_compatibility(self, spent="0.0421", settle=True, when=NOW - datetime.timedelta(hours=1),
                            request_ids=None, drop=(), **overrides):
        """Compatibility evidence (shared interface with scripts/compat_check.py) plus its ledger spending.

        ``settle`` admits and settles one compatibility request whose id the evidence
        records; otherwise the evidence names ``request_ids`` (default: the last settled ones).
        """
        if settle:
            ledger = ledger_run(self.ledger, f"compat-{len(list_runs(self.ledger))}", "compatibility", 2)
            request = ledger.reserve("compatibility", "compat-agent", "0.5")
            ledger.settle(request, spent, "provider_reported_cost")
            self.compat_ids = [request]
        ids = list(self.compat_ids) if request_ids is None else request_ids
        record = {"schema": launch_gates.COMPATIBILITY_SCHEMA, "all_required_passed": True,
                  "image_ids": {"agent": IMAGES["gdm-math-swarm-agent:local"],
                                "lean": IMAGES["gdm-math-swarm-lean:local"]},
                  "model_id": MODEL_ID, "provider_route": settings_from_config(pilot_config()).provider_route,
                  "settings": frozen_fields(settings_from_config(pilot_config())), "spent_usd": spent,
                  "requests": len(ids) if isinstance(ids, list) else 1, "request_ids": ids,
                  "checks": passed_checks(launch_gates.COMPATIBILITY_REQUIRED_CHECKS, truncation_observed={
                      "passed": True, "required": False, "detail": "observed"})}
        record.update(overrides)
        for key in drop:
            del record[key]
        return self.write("compatibility", record, when)

    def evaluate(self, config=None, now=NOW):
        return launch_gates.evaluate(config or pilot_config(), ROOT, validation_dir=self.validation,
                                     ledger_path=self.ledger, legacy_ledger_path=self.legacy, now=now,
                                     image_id=lambda tag: self.images.get(tag))

    def assertBlocked(self, result, gate, text=""):
        self.assertFalse(result["ready"])
        self.assertFalse(result["evidence"][gate]["passed"], result["evidence"][gate])
        self.assertIn(text, result["evidence"][gate]["reason"])


class LaunchGateTests(GateFixture):
    def test_all_evidence_passes_without_credentials_or_network(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("credential or network access")
        before = self.ledger.read_bytes()
        with mock.patch("research_swarm.secrets.load_openrouter_key", forbidden), \
                mock.patch("urllib.request.urlopen", forbidden), mock.patch("socket.create_connection", forbidden):
            result = self.evaluate()
        self.assertEqual(result["blockers"], [])
        self.assertTrue(result["ready"])
        self.assertEqual(self.ledger.read_bytes(), before)  # read-only: the ledger is never written
        self.assertEqual(self.status_calls, [(self.validation, "gdm-math-swarm-comparator:local")])
        self.assertEqual(result["evidence"]["route_pricing"]["provider_route"], pilot_config()["provider_route"])
        for name, check in result["evidence"].items():
            self.assertTrue(check["reason"], name)
        self.assertTrue(result["evidence"]["judge"]["evidence"].endswith(".json"))

    def test_real_archived_route_freeze_still_validates_offline(self):
        record_path, record = launch_gates.newest_evidence(ROOT / "data" / "validation", "route-pricing")
        target = self.validation
        for path in launch_gates.evidence_files(target, "route-pricing"):
            path.unlink()
        shutil.copy(record_path, target / record_path.name)
        snapshot = ROOT / "data" / "validation" / "catalog-snapshots" / f"{record['catalog_sha256']}.json"
        shutil.copy(snapshot, target / "catalog-snapshots" / snapshot.name)
        created = launch_gates._created(record, record_path)
        result = self.evaluate(now=created + datetime.timedelta(hours=1))
        self.assertTrue(result["evidence"]["route_pricing"]["passed"], result["evidence"]["route_pricing"])

    def test_route_pricing_freshness_settings_and_snapshot(self):
        # The launch scripts refresh a stale freeze automatically (unpaid) first; a freeze still stale after
        # that refuses the paid launch (reservations from old prices are no upper bound; review finding).
        stale = self.evaluate(now=NOW + datetime.timedelta(hours=80))
        self.assertBlocked(stale, "route_pricing", "refresh it automatically")
        self.assertIn("81.0 h old", stale["evidence"]["route_pricing"]["reason"])   # written an hour before NOW
        lower = pilot_config(provider_settings={"prompt_usd_per_token": "0.00000015"})
        result = self.evaluate(lower)
        self.assertBlocked(result, "route_pricing", "differ from the ProviderSettings")
        self.assertIn("offline catalog validation failed", result["evidence"]["route_pricing"]["reason"])
        self.assertBlocked(self.evaluate(pilot_config(provider_route=other_route(pilot_config()["provider_route"]))),
                           "route_pricing")
        self.write_route(valid=False, reason="completion price above the config", when=NOW - datetime.timedelta(minutes=5))
        self.assertBlocked(self.evaluate(), "route_pricing", "must cover the route's current prices")
        self.write_route(when=NOW - datetime.timedelta(minutes=4))
        snapshot = next((self.validation / "catalog-snapshots").iterdir())
        snapshot.write_text("{}")
        self.assertBlocked(self.evaluate(), "route_pricing", "snapshot")

    def test_route_pricing_refresh_reason(self):
        config = pilot_config()
        self.assertIsNone(launch_gates.route_pricing_refresh_reason(config, self.validation, now=NOW))
        self.assertIn("h old", launch_gates.route_pricing_refresh_reason(config, self.validation,
                                                                          now=NOW + datetime.timedelta(hours=80)))
        self.assertIn("other provider settings", launch_gates.route_pricing_refresh_reason(
            pilot_config(provider_settings={"prompt_usd_per_token": "0.00000015"}), self.validation, now=NOW))
        self.assertIn("no route-pricing", launch_gates.route_pricing_refresh_reason(config, self.tmp / "empty"))
        # Review finding: a newest freeze that is not valid (e.g. the catalog briefly reported the pinned route
        # unavailable) is refreshed at the next launch instead of blocking every launch for 72 h.
        self.write_route(valid=False, reason="Pinned provider route unavailable", when=NOW - datetime.timedelta(minutes=1))
        self.assertIn("not valid (Pinned provider route unavailable)",
                      launch_gates.route_pricing_refresh_reason(config, self.validation, now=NOW))

    def test_newest_evidence_wins_and_legacy_names_ignored(self):
        (self.validation / "judge-2026-10-13.json").write_text(json.dumps({"all_required_passed": True}))
        self.write("judge", {"all_required_passed": False, "image_ids": {"lean": IMAGES["gdm-math-swarm-lean:local"]}},
                   NOW - datetime.timedelta(minutes=5))
        self.assertBlocked(self.evaluate(), "judge", "all_required_passed")
        self.write("judge", {"all_required_passed": True, "image_ids": {"lean": IMAGES["gdm-math-swarm-lean:local"]},
                             "complete": True, "limits": dict(JUDGE_LIMITS)}, NOW - datetime.timedelta(minutes=1))
        self.assertTrue(self.evaluate()["evidence"]["judge"]["passed"])

    def test_image_ids_must_match_current_images(self):
        self.images["gdm-math-swarm-lean:local"] = "sha256:" + "e" * 64
        result = self.evaluate()
        for prefix in ("declarations", "judge", "timing", "reuse", "runtime-e2e", "runtime-capacity"):
            self.assertBlocked(result, prefix, "lean image")
        self.assertTrue(result["evidence"]["conference-race"]["passed"])  # records no lean image
        self.images = dict(IMAGES)
        del self.images["gdm-math-swarm-agent:local"]
        self.assertBlocked(self.evaluate(), "runtime-isolation", "not found locally")
        self.write("evaluator", {"all_required_passed": True, "image_ids": {}})
        self.assertBlocked(self.evaluate(), "evaluator", "does not record the current comparator image")

    def test_missing_and_corrupt_evidence_block(self):
        for path in launch_gates.evidence_files(self.validation, "runtime-isolation"):
            path.unlink()
        self.assertBlocked(self.evaluate(), "runtime-isolation", "no runtime-isolation")
        (self.validation / "evaluator-20991231T000000Z.json").write_text("{not json")
        self.assertBlocked(self.evaluate(), "evaluator", "could not be evaluated")

    def test_capacity_validated_config_must_match(self):
        config = pilot_config(judge_max_concurrent=4)
        self.assertBlocked(self.evaluate(config), "runtime-capacity", "judge_max_concurrent")
        self.write("runtime-capacity", {"all_required_passed": True, "image_ids": {"agent": IMAGES["gdm-math-swarm-agent:local"]}})
        self.assertBlocked(self.evaluate(), "runtime-capacity", "validated_config")

    def test_capacity_binds_effective_launch_limits(self):
        for key, changed in (("max_subprocesses", 1), ("startup_timeout_seconds", 1),
                             ("max_connections", 10), ("warm_up_memory", "3g"),
                             ("warm_up_timeout_seconds", 60)):
            with self.subTest(key=key):
                self.assertBlocked(self.evaluate(pilot_config(**{key: changed})), "runtime-capacity", key)
        # Optional fields omitted in the pilot have the same meaning as explicit defaults.
        config = pilot_config(max_subprocesses=64, warm_up_memory="6g", warm_up_timeout_seconds=900)
        self.assertTrue(self.evaluate(config)["evidence"]["runtime-capacity"]["passed"])
        for key in ("max_subprocesses", "startup_timeout_seconds", "warm_up_memory", "warm_up_timeout_seconds"):
            config.pop(key, None)
        self.assertTrue(self.evaluate(config)["evidence"]["runtime-capacity"]["passed"])

    def test_capacity_warm_up_failure_remains_informational(self):
        config = pilot_config()
        checks = passed_checks(launch_gates.REQUIRED_EVIDENCE_CHECKS["runtime-capacity"])
        checks["lean_warm_up"] = {"passed": False, "required": False, "warm_up": {"ok": False}}
        self.write("runtime-capacity", {
            "all_required_passed": True, "image_ids": {"agent": IMAGES["gdm-math-swarm-agent:local"]},
            "validated_config": launch_gates.capacity_config(config), "checks": checks,
            "compose_sha256": self.fake_fingerprint(config, IMAGES["gdm-math-swarm-agent:local"])})
        self.assertTrue(self.evaluate()["evidence"]["runtime-capacity"]["passed"])

    def test_capacity_requires_each_named_check(self):
        config = pilot_config()
        for name in launch_gates.REQUIRED_EVIDENCE_CHECKS["runtime-capacity"]:
            for missing in (True, False):
                with self.subTest(name=name, missing=missing):
                    checks = passed_checks(launch_gates.REQUIRED_EVIDENCE_CHECKS["runtime-capacity"])
                    if missing:
                        del checks[name]
                    else:
                        checks[name] = {"passed": False, "required": False}
                    self.write("runtime-capacity", {
                        "all_required_passed": True, "image_ids": {"agent": IMAGES["gdm-math-swarm-agent:local"]},
                        "validated_config": launch_gates.capacity_config(config), "checks": checks,
                        "compose_sha256": self.fake_fingerprint(config, IMAGES["gdm-math-swarm-agent:local"])})
                    self.assertBlocked(self.evaluate(), "runtime-capacity", name)

    def test_task_manifest_and_plan_settings(self):
        config = pilot_config()
        config["fixed_task_manifest"] = dict(config["fixed_task_manifest"], sha256="0" * 64)
        self.assertBlocked(self.evaluate(config), "fixed_task_manifest", "sha256")
        self.assertBlocked(self.evaluate(pilot_config(fixed_task_manifest=None)), "fixed_task_manifest")
        self.assertBlocked(self.evaluate(pilot_config(system_prompt_mode="replace")), "approved_settings",
                           "system_prompt_mode")
        self.assertBlocked(self.evaluate(pilot_config(seeded_contagion_enabled=True)), "approved_settings")
        self.assertTrue(self.evaluate()["evidence"]["frozen_dataset"]["passed"])

    def test_max_connections_required_and_equal_to_agent_count(self):
        """S2: one in-flight inference request per agent, stated explicitly (no silent default)."""
        shipped = json.loads((ROOT / "configs" / "pilot.json").read_text())
        self.assertEqual(shipped["max_connections"], shipped["agent_count"])
        result = self.evaluate()
        self.assertTrue(result["evidence"]["approved_settings"]["passed"], result["evidence"]["approved_settings"])
        self.assertEqual((result["evidence"]["approved_settings"]["max_connections"],
                          result["evidence"]["approved_settings"]["agent_count"]), (20, 20))
        config = pilot_config()
        del config["max_connections"]
        self.assertBlocked(self.evaluate(config), "approved_settings", "max_connections is required")
        self.assertBlocked(self.evaluate(pilot_config(max_connections=None)), "approved_settings",
                           "max_connections is required")
        result = self.evaluate(pilot_config(max_connections=10))
        self.assertBlocked(result, "approved_settings", "max_connections 10 differs from agent_count 20")
        self.assertEqual(result["evidence"]["approved_settings"]["drift"]["max_connections"]["config"], 10)
        for bad in (True, "20", 20.0, 0, -20):
            self.assertBlocked(self.evaluate(pilot_config(max_connections=bad)), "approved_settings",
                               "is not a positive integer")
        # A population change moves both together, and is still blocked by the approved agent count.
        result = self.evaluate(pilot_config(agent_count=12, max_connections=12))
        self.assertBlocked(result, "approved_settings", "agent_count")
        self.assertNotIn("max_connections", result["evidence"]["approved_settings"]["reason"])

    def test_approved_agent_count_comes_from_runtime(self):
        """Single source of truth: runtime.APPROVED_AGENT_COUNT, imported lazily."""
        from research_swarm import runtime
        self.assertNotIn("agent_count", launch_gates._APPROVED_STATIC_SETTINGS)
        self.assertEqual(launch_gates.APPROVED_SETTINGS["agent_count"], runtime.APPROVED_AGENT_COUNT)
        from research_swarm.launch_gates import APPROVED_SETTINGS
        self.assertEqual(APPROVED_SETTINGS, launch_gates.approved_settings())
        with mock.patch.object(runtime, "APPROVED_AGENT_COUNT", 12):
            self.assertEqual(launch_gates.APPROVED_SETTINGS["agent_count"], 12)
            self.assertBlocked(self.evaluate(), "approved_settings", "agent_count")
            self.assertTrue(self.evaluate(pilot_config(agent_count=12, max_connections=12))
                            ["evidence"]["approved_settings"]["passed"])
        with self.assertRaises(AttributeError):
            launch_gates.NOT_A_SETTING  # noqa: B018

    def test_seeded_configuration_blocks_the_pilot(self):
        """S3: any seeded configuration, enabled or not, blocks the approved spontaneous pilot."""
        self.assertTrue(self.evaluate()["evidence"]["seeded_track"]["passed"])
        self.assertTrue(self.evaluate(pilot_config(seeded=None))["evidence"]["seeded_track"]["passed"])
        result = self.evaluate(pilot_config(seeded={"enabled": False}))
        self.assertBlocked(result, "seeded_track", "carries a seeded-track configuration")
        self.assertTrue(result["evidence"]["approved_settings"]["passed"])  # caught by the dedicated gate
        self.assertBlocked(self.evaluate(pilot_config(seeded={})), "seeded_track", "seeded-track configuration")
        self.assertBlocked(self.evaluate(pilot_config(track="seeded")), "seeded_track", "track 'seeded'")
        self.assertBlocked(self.evaluate(pilot_config(seeded_contagion_enabled=True)), "seeded_track",
                           "seeded_contagion_enabled True is not false")
        config = pilot_config()
        del config["seeded_contagion_enabled"]  # must be stated, as false
        self.assertBlocked(self.evaluate(config), "seeded_track", "seeded_contagion_enabled None")
        example = json.loads((ROOT / "configs" / "seeded.example.json").read_text())
        result = self.evaluate(example)
        self.assertBlocked(result, "seeded_track", "track 'seeded'")
        self.assertIn("seeded-track configuration", result["evidence"]["seeded_track"]["reason"])
        self.assertBlocked(result, "approved_settings", "track")

    def test_behavior_judge_config(self):
        """docs/plan.md, "One per-run budget, no manual approvals (October 20, 2026)": any track, any positive
        budget, no approval note."""
        from research_swarm import auto_judge
        shipped = json.loads((ROOT / "configs" / "pilot.json").read_text())
        self.assertEqual(auto_judge.judge_config_problems(shipped), [])
        self.assertEqual(Decimal(auto_judge.judge_settings(shipped)["max_usd"]), Decimal(50))
        judge = {"auto_behavior_judge": True, "behavior_judge_max_usd": 50}
        config = {k: v for k, v in pilot_config().items() if k not in judge}
        self.assertTrue(self.evaluate(config)["evidence"]["behavior_judge"]["passed"])  # absent: no automatic judge
        for good in (judge, dict(judge, budget_track="gemini"), dict(judge, budget_track="ablation"),
                     dict(judge, behavior_judge_max_usd="12.5"), dict(judge, behavior_judge_max_usd=500),
                     {"auto_behavior_judge": True}, dict(judge, auto_behavior_judge=False),
                     dict(judge, behavior_judge_approval="")):
            result = self.evaluate(dict(config, **good))
            self.assertTrue(result["evidence"]["behavior_judge"]["passed"], (good, result["evidence"]["behavior_judge"]))
        default = self.evaluate(dict(config, auto_behavior_judge=True))["evidence"]["behavior_judge"]
        self.assertEqual((default["budget_usd"], default["budget_source"]), ("50", "default"))
        # Live judging (docs/plan.md, "Live behavior judge during the run (October 20, 2026)") is on by
        # default and spends from the same budget: there is no live share (one spending control only).
        self.assertEqual(default["live_mode"], "paid")
        self.assertNotIn("live_max_usd", default)
        self.assertIn("live judging during the run", default["reason"])
        self.assertIn("from the same budget", default["reason"])
        # A superseded live share in an older config is not read: a warning in the run_budget gate, never a refusal.
        old = self.evaluate(dict(config, **dict(judge, behavior_judge_live_max_usd=60)))
        self.assertTrue(old["evidence"]["behavior_judge"]["passed"])
        self.assertTrue(any("behavior_judge_live_max_usd" in w for w in old["evidence"]["run_budget"]["warnings"]))
        self.assertNotIn("live_max_usd", auto_judge.judge_settings(dict(judge, behavior_judge_live_max_usd=60)))
        off = self.evaluate(dict(config, **judge, live_behavior_judge=False))["evidence"]["behavior_judge"]
        self.assertEqual(off["live_mode"], None)
        for bad, text in ((dict(judge, behavior_judge_max_usd=0), "positive"),
                          (dict(judge, behavior_judge_max_usd=True), "not an amount"),
                          (dict(judge, behavior_judge_max_usd="abc"), "not an amount"),
                          (dict(judge, auto_behavior_judge="yes"), "must be a boolean"),
                          (dict(judge, live_behavior_judge="yes"), 'true, false or "mock"')):
            self.assertBlocked(self.evaluate(dict(config, **bad)), "behavior_judge", text)
            with self.assertRaises(ValueError):
                auto_judge.judge_settings(bad)

    def test_run_budget_gate(self):
        result = self.evaluate()
        gate = result["evidence"]["run_budget"]
        self.assertTrue(gate["passed"], gate)
        self.assertEqual((gate["budget_usd"], gate["budget_source"], gate["warnings"]),
                         ("100", "config run_budget_usd", []))
        unset = {k: v for k, v in pilot_config().items() if k != "run_budget_usd"}
        gate = self.evaluate(unset)["evidence"]["run_budget"]
        self.assertEqual((gate["passed"], gate["budget_usd"], gate["budget_source"]), (True, "100", "default"))
        for bad in ("abc", 0, "-1", True):
            with self.subTest(bad=bad):
                self.assertBlocked(self.evaluate(pilot_config(run_budget_usd=bad)), "run_budget", "run_budget_usd")

    def test_seeded_example_config(self):
        """S3: configs/seeded.example.json is the pilot config plus a disabled seeded block with null semantics."""
        from dataclasses import fields
        from research_swarm import seeded as seeded_module
        from research_swarm.seeded import SeededConfigError, SeededTrackConfig, resolve_track
        pilot = json.loads((ROOT / "configs" / "pilot.json").read_text())
        example = json.loads((ROOT / "configs" / "seeded.example.json").read_text())
        track_keys = {"experiment", "track", "seeded"}
        self.assertEqual({k: v for k, v in example.items() if k not in track_keys},
                         {k: v for k, v in pilot.items() if k not in track_keys})  # stays in sync with the pilot
        self.assertEqual((example["track"], example["seeded_contagion_enabled"]), ("seeded", False))
        for key in launch_gates.OBSOLETE_SPENDING_KEYS:  # no superseded spending key ships
            self.assertNotIn(key, pilot)
            self.assertNotIn(key, example)
        block = example["seeded"]
        documentation = set(getattr(seeded_module, "DOCUMENTATION_KEYS", ("note",)))
        self.assertEqual(set(block) - documentation, {f.name for f in fields(SeededTrackConfig)})  # contract with seeded.py
        self.assertIn("UNDECIDED", block["note"])
        self.assertIn("user decisions", block["note"])
        self.assertIs(block["enabled"], False)
        values = {k: v for k, v in block.items() if k not in documentation | {"enabled", "tags"}}
        self.assertEqual(values, dict.fromkeys(values))  # every semantic field null
        self.assertEqual(block["tags"], [])  # a list, so a filled-in block never trips over tags
        if hasattr(seeded_module, "missing_semantic_fields"):
            self.assertEqual(seeded_module.missing_semantic_fields(block), list(seeded_module.SEMANTIC_FIELDS))
        if hasattr(seeded_module, "conference_arguments"):
            with self.assertRaises(SeededConfigError):
                seeded_module.conference_arguments(example)  # the runtime never passes it to the service
        config = SeededTrackConfig.from_value({k: v for k, v in block.items() if k not in documentation})
        self.assertFalse(config.enabled)
        self.assertEqual(config.describe()["tags"], [])
        with self.assertRaisesRegex(SeededConfigError, "not enabled"):
            resolve_track("seeded", config, task_ids=frozenset(), max_bytes=1, max_text_chars=1, max_post_chars=1,
                          intent_types=(), max_tags=0, max_tag_chars=0)

    def test_config_readme_documents_every_key_read(self):
        """configs/README.md names every key in the shipped configs and every key code reads."""
        readme = (ROOT / "configs" / "README.md").read_text()
        keys = set()
        for name in ("pilot.json", "seeded.example.json"):
            keys |= set(json.loads((ROOT / "configs" / name).read_text()))
        pattern = re.compile(r"""\bconfig(?: or \{\}\))?(?:\.get\(|\[)\s*["']([a-z_]+)["']""")
        for folder in (ROOT / "src" / "research_swarm", ROOT / "scripts"):
            for path in folder.glob("*.py"):
                keys |= set(pattern.findall(path.read_text()))
        keys |= set(launch_gates.approved_settings()) | set(launch_gates.OBSOLETE_SPENDING_KEYS)
        keys |= {launch_gates.RUN_BUDGET_KEY, launch_gates.PROMPT_CACHE_NORMALIZATION_KEY}
        keys |= {key for key, _ in launch_gates.JUDGE_LIMIT_KEYS.values()} | set(launch_gates.CAPACITY_KEYS)
        keys |= {f"{role}_image" for role in launch_gates.DEFAULT_IMAGES}
        keys |= {launch_gates.MAX_CONNECTIONS_KEY, launch_gates.SEEDED_CONFIG_KEY}
        for required in ("max_connections", "max_subprocesses", "warm_up_memory", "warm_up_timeout_seconds",
                         "in_flight_grace_seconds", "stop_grace_seconds", "seeded"):
            self.assertIn(required, keys)
        self.assertEqual(sorted(k for k in keys if f"`{k}`" not in readme), [])

    def test_no_attestation_approval_or_cap_is_required(self):
        """Owner decisions, October 20, 2026: no typed or attested approval and no cap; superseded keys only warn."""
        old = {"verified_integration": False, "paid_launch_enabled": False, "allow_unlimited_provider_key": "x",
               "unlimited_provider_key_approval": "", "max_pilot_usd": 1, "max_compatibility_usd": 0,
               "max_analysis_usd": "0", "pilot_prior_calls_sha256": "0" * 64, "provider_limit_max_age_hours": 1}
        result = self.evaluate(pilot_config(**old))
        self.assertTrue(result["ready"], result["blockers"])
        for gate in ("verified_integration", "paid_launch_enabled", "attestations", "config_caps"):
            self.assertNotIn(gate, result["evidence"])
        warning = result["evidence"]["run_budget"]["warnings"][0]
        for key in old:
            self.assertIn(key, warning)

    def test_prompt_cache_normalization_must_match_the_runtime(self):
        self.assertTrue(self.evaluate()["evidence"]["approved_settings"]["passed"])
        self.assertBlocked(self.evaluate(pilot_config(prompt_cache_normalization="none")), "approved_settings",
                           "prompt_cache_normalization")

    def test_earlier_runs_holds_violations_and_legacy_never_block(self):
        """A new run is never blocked by an old run's holds or violations; the ledger gate only reports."""
        ledger = ledger_run(self.ledger, "earlier-pilot", "pilot", 20)
        call = ledger.reserve("pilot", "agent-001", 1)
        ledger.settle(ledger.reserve("pilot", "agent-001", 0.1), 0.5)  # an estimate violation
        result = self.evaluate()
        self.assertTrue(result["ready"], result["blockers"])
        gate = result["evidence"]["ledger"]
        self.assertTrue(gate["passed"])
        self.assertIn("admission is per run", gate["reason"])
        self.assertTrue(any("unresolved hold" in w for w in gate["warnings"]))
        self.assertEqual(result["ledger"]["snapshot"]["pilot"]["committed_usd"], "1.5")
        ledger.settle(call, 0.25, "provider_reported_cost")
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_bytes(b"")
        result = self.evaluate()
        self.assertTrue(result["evidence"]["ledger"]["passed"])
        self.assertTrue(any("legacy ledger" in w for w in result["evidence"]["ledger"]["warnings"]))

    def test_legacy_ledger_caps_never_block(self):
        import sqlite3
        from contextlib import closing
        self.ledger.unlink()
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.ledger)) as db, db:  # a ledger from before per-run budgets
            db.execute("CREATE TABLE scopes (name TEXT PRIMARY KEY, cap INTEGER NOT NULL)")
            db.execute("CREATE TABLE calls (id TEXT PRIMARY KEY, scope TEXT NOT NULL, agent TEXT NOT NULL, "
                       "reserved INTEGER NOT NULL, actual INTEGER, state TEXT NOT NULL, created TEXT)")
            db.executemany("INSERT INTO scopes VALUES (?,?)", [("pilot", 20_000_000_000), ("compatibility", 0)])
            db.execute("INSERT INTO calls VALUES ('old','pilot','a',20000000000,20000000000,'settled',NULL)")
        result = self.evaluate()
        self.assertTrue(result["evidence"]["ledger"]["passed"], result["evidence"]["ledger"])
        self.assertEqual(result["ledger"]["snapshot"]["pilot"]["legacy_cap_usd"], "20")

    def test_gates_never_create_a_missing_ledger(self):
        self.ledger.unlink()
        result = self.evaluate()
        self.assertFalse(self.ledger.exists())
        self.assertTrue(result["evidence"]["ledger"]["passed"])
        self.assertIn("created at the first paid request", result["evidence"]["ledger"]["reason"])
        gate = result["evidence"]["compatibility"]
        self.assertTrue(gate["passed"], gate)
        self.assertTrue(any("not in this ledger" in w for w in gate["warnings"]))

    def test_compatibility_evidence_required(self):
        for path in launch_gates.evidence_files(self.validation, "compatibility"):
            path.unlink()
        result = self.evaluate()
        self.assertBlocked(result, "compatibility", "no compatibility-<UTC>.json")
        self.assertNotIn("$2", result["evidence"]["compatibility"]["reason"])
        self.write_compatibility(settle=False)
        result = self.evaluate(pilot_config(verified_integration=False))
        self.assertTrue(result["evidence"]["compatibility"]["passed"], result["evidence"]["compatibility"])
        self.assertTrue(result["ready"], result["blockers"])

    def test_compatibility_evidence_contents(self):
        later = iter(NOW - datetime.timedelta(minutes=50 - i) for i in range(50))

        def blocked(text, **overrides):
            self.write_compatibility(settle=False, when=next(later), **overrides)
            self.assertBlocked(self.evaluate(), "compatibility", text)

        checks = passed_checks(launch_gates.COMPATIBILITY_REQUIRED_CHECKS)
        del checks["reasoning_replay_accepted"]
        blocked("missing required checks", checks=checks)
        blocked("not passed: route_pinned", checks=passed_checks(
            launch_gates.COMPATIBILITY_REQUIRED_CHECKS, route_pinned={"passed": False, "required": True}))
        blocked("all_required_passed", all_required_passed=False)
        blocked("schema", schema="gdm-compatibility/0")
        blocked("model_id", model_id="deepseek/deepseek-v4.1")
        blocked("provider_route", provider_route=other_route(pilot_config()["provider_route"]))
        blocked("settings differ", settings=dict(frozen_fields(settings_from_config(pilot_config())), max_tokens=8192))
        blocked("nonnegative decimal", spent_usd="NaN")
        blocked("requests", requests=0)
        blocked("agent image", image_ids={"lean": IMAGES["gdm-math-swarm-lean:local"]})
        checks = passed_checks(launch_gates.COMPATIBILITY_REQUIRED_CHECKS)
        del checks["reasoning_cap_enforced"]  # required since the reasoning-cap check was added
        blocked("missing required checks (older or different script version?): reasoning_cap_enforced", checks=checks)

        def warned(text, **overrides):  # ledger bookkeeping is reported, never blocking
            self.write_compatibility(settle=False, when=next(later), **overrides)
            gate = self.evaluate()["evidence"]["compatibility"]
            self.assertTrue(gate["passed"], gate)
            self.assertTrue(any(text in w for w in gate["warnings"]), gate["warnings"])
        warned("lacks request_ids", request_ids=[], requests=1)
        warned("lacks request_ids", drop=("request_ids",), requests=1)
        warned("lacks request_ids", request_ids="not-a-list")
        warned("not in this ledger", request_ids=["not-a-ledger-request"])
        warned("requests 3 differs from the 1 recorded request_ids", requests=3)
        warned("duplicates", request_ids=self.compat_ids * 2, requests=1)
        self.write_compatibility(spent="2.01", settle=False, when=next(later))  # no compatibility allowance
        self.assertTrue(self.evaluate()["evidence"]["compatibility"]["passed"])
        self.write_compatibility(settle=False, when=next(later),
                                 settings={**frozen_fields(settings_from_config(pilot_config())),
                                           "pricing_verified": True, "live_compatibility_verified": True},
                                 checks=passed_checks(launch_gates.COMPATIBILITY_REQUIRED_CHECKS))
        result = self.evaluate()
        self.assertTrue(result["evidence"]["compatibility"]["passed"], result["evidence"]["compatibility"])
        self.assertEqual(result["evidence"]["compatibility"]["warnings"],
                         ["informational check truncation_observed not observed",
                          "informational check compaction_observed not observed"])
        self.images["gdm-math-swarm-agent:local"] = "sha256:" + "f" * 64
        self.assertBlocked(self.evaluate(), "compatibility", "agent image changed")

    def test_compatibility_ledger_holds_are_reported_never_blocking(self):
        ledger = ledger_run(self.ledger, "compat-later", "compatibility", 2)
        hold = ledger.reserve("compatibility", "compat-agent", "0.5")
        ledger.settle(hold, None)  # another check's hold: never part of this evidence
        result = self.evaluate()
        self.assertTrue(result["ready"], result["blockers"])
        self.assertTrue(any("compatibility: 1 unresolved hold" in w for w in result["evidence"]["ledger"]["warnings"]))
        ledger.reconcile(hold, "0.6", note="generation total_cost")  # an estimate violation: recorded only
        self.assertTrue(self.evaluate()["ready"])

    def test_compatibility_requests_are_reported_with_their_current_ledger_state(self):
        ledger = ledger_run(self.ledger, "compat-held", "compatibility", 2)
        first = ledger.reserve("compatibility", "agent-001", "0.05")
        ledger.settle(first, "0.009", "provider_reported_cost")
        hold = ledger.reserve("compatibility", "agent-001", "0.055")
        ledger.settle(hold, None)  # uncertain (e.g. cancelled by the max_minutes abort)
        self.write_compatibility(spent="0.064", settle=False, request_ids=[first, hold],
                                 when=NOW - datetime.timedelta(minutes=30))
        gate = self.evaluate()["evidence"]["compatibility"]
        self.assertTrue(gate["passed"], gate)
        self.assertTrue(any("1 of the evidence's requests are unresolved" in w for w in gate["warnings"]))
        ledger.reconcile(hold, "0.004", note="generation total_cost")
        gate = self.evaluate()["evidence"]["compatibility"]
        self.assertTrue(gate["passed"], gate)
        self.assertFalse(any("unresolved" in w for w in gate["warnings"]))
        self.assertEqual(gate["ledger_requests"], {"count": 2, "states": {"settled": 2}, "settled_usd": "0.013"})

    def test_compatibility_requests_violations_and_other_scopes_are_warnings(self):
        ledger = ledger_run(self.ledger, "compat-violation", "compatibility", 2)
        hold = ledger.reserve("compatibility", "agent-001", "0.05")
        ledger.settle(hold, None)
        self.write_compatibility(spent="0.05", settle=False, request_ids=[hold],
                                 when=NOW - datetime.timedelta(minutes=30))
        ledger.reconcile(hold, "0.06", note="generation total_cost")  # above its reservation
        gate = self.evaluate()["evidence"]["compatibility"]
        self.assertTrue(gate["passed"], gate)
        self.assertTrue(any("billed above their reservation" in w for w in gate["warnings"]))
        pilot = ledger_run(self.ledger, "pilot-x", "pilot", 1)
        request = pilot.reserve("pilot", "agent-001", "0.05")
        pilot.settle(request, "0.01", "provider_reported_cost")
        self.write_compatibility(settle=False, request_ids=[request], when=NOW - datetime.timedelta(minutes=20))
        gate = self.evaluate()["evidence"]["compatibility"]
        self.assertTrue(any("not in ledger scope 'compatibility'" in w for w in gate["warnings"]))

    def test_lean_evidence_must_be_complete_at_judge_limits(self):
        """J1/J2: partial runs (--only, --skip-timeout, --tasks, --template-only) or other limits never pass.

        S4: conference-race evidence (scripts/validate_conference.py --config) is checked the same way.
        """
        self.assertIn("conference-race", launch_gates.JUDGE_LIMIT_EVIDENCE)
        lean = IMAGES["gdm-math-swarm-lean:local"]
        later = iter(NOW - datetime.timedelta(minutes=50 - i) for i in range(50))
        for prefix in launch_gates.JUDGE_LIMIT_EVIDENCE:
            def write(drop=(), **fields):
                record = {"all_required_passed": True, "image_ids": {"lean": lean}, "complete": True,
                          "limits": dict(JUDGE_LIMITS), **fields}
                for key in drop:
                    del record[key]
                self.write(prefix, record, next(later))

            def blocked(text, **fields):
                write(**fields)
                self.assertBlocked(self.evaluate(), prefix, text)

            blocked("not from a complete run", complete=False)
            blocked("not from a complete run", drop=("complete",))
            blocked("not from a complete run", complete="true")
            blocked("lacks limits", drop=("limits",))
            blocked("timeout_seconds 900", limits=dict(JUDGE_LIMITS, timeout_seconds=900))
            blocked("memory '8g'", limits=dict(JUDGE_LIMITS, memory="8g"))
            blocked("cpus 2", limits=dict(JUDGE_LIMITS, cpus=2))
            blocked("cpus None", limits={"timeout_seconds": 120, "memory": "2g"})
            write(limits={"timeout_seconds": "120", "memory": "2G", "cpus": "1.0"})  # same limits, other spelling
            result = self.evaluate()
            self.assertTrue(result["evidence"][prefix]["passed"], result["evidence"][prefix])
            self.assertEqual((result["evidence"][prefix]["complete"], result["evidence"][prefix]["limits"]["cpus"]),
                             (True, "1.0"))
        result = self.evaluate(pilot_config(judge_memory="3g"))
        for prefix in launch_gates.JUDGE_LIMIT_EVIDENCE:
            self.assertBlocked(result, prefix, "config judge_memory '3g'")
        self.assertTrue(result["evidence"]["reuse"]["passed"])  # not a judge-limit compile record

    def test_runtime_evidence_bound_to_compose(self):
        """ISO-3: a compose change with the same agent image invalidates runtime evidence."""
        agent = IMAGES["gdm-math-swarm-agent:local"]
        result = self.evaluate()
        for prefix in launch_gates.COMPOSE_BOUND_EVIDENCE:
            self.assertTrue(result["evidence"][prefix]["passed"], result["evidence"][prefix])
        self.assertTrue(self.fingerprint_calls)
        self.assertTrue(all(image == agent and config == pilot_config() for config, image in self.fingerprint_calls))
        self.compose_salt = "write_compose edited: volume added"
        result = self.evaluate()
        for prefix in launch_gates.COMPOSE_BOUND_EVIDENCE:
            self.assertBlocked(result, prefix, "compose file differs from the one validated")
        self.compose_salt = ""
        self.write("runtime-isolation", {"all_required_passed": True, "image_ids": {"agent": agent},
                                         "checks": isolation_checks()}, NOW - datetime.timedelta(minutes=5))
        self.assertBlocked(self.evaluate(), "runtime-isolation", "lacks compose_sha256")
        with mock.patch("research_swarm.runtime.compose_fingerprint", side_effect=AttributeError("absent"),
                        create=True):
            self.assertBlocked(self.evaluate(), "runtime-e2e", "could not be evaluated")  # fails closed

    def test_compose_gate_with_the_real_fingerprint(self):
        """X3 end to end: evidence written with runtime.compose_fingerprint passes; a compose-relevant change does not."""
        from research_swarm import runtime
        self.compose_patcher.stop()
        if not hasattr(runtime, "compose_fingerprint"):
            self.skipTest("runtime.compose_fingerprint not available yet")
        agent, lean = IMAGES["gdm-math-swarm-agent:local"], IMAGES["gdm-math-swarm-lean:local"]
        config = pilot_config()
        compose = runtime.compose_fingerprint(config, agent)
        when = NOW - datetime.timedelta(minutes=5)
        self.write("runtime-e2e", {"all_required_passed": True, "image_ids": {"agent": agent, "lean": lean},
                                   "checks": e2e_checks(), "compose_sha256": compose}, when)
        self.write("runtime-isolation", {"all_required_passed": True, "image_ids": {"agent": agent},
                                         "checks": isolation_checks(), "compose_sha256": compose}, when)
        self.write("runtime-capacity", {"all_required_passed": True, "image_ids": {"agent": agent, "lean": lean},
                                        "validated_config": launch_gates.capacity_config(config),
                                        "checks": passed_checks(launch_gates.REQUIRED_EVIDENCE_CHECKS["runtime-capacity"]),
                                        "compose_sha256": compose}, when)
        result = self.evaluate(config)
        for prefix in launch_gates.COMPOSE_BOUND_EVIDENCE:
            self.assertTrue(result["evidence"][prefix]["passed"], result["evidence"][prefix])
        changed_memory = "3g" if config["agent_memory"] != "3g" else "2g"
        result = self.evaluate(pilot_config(agent_memory=changed_memory))
        for prefix in ("runtime-e2e", "runtime-isolation"):
            self.assertBlocked(result, prefix, "compose file differs")

    def test_evaluator_gate_uses_validation_status_and_pilot_coverage(self):
        self.evaluator_error = "Fixture changed since validation: green32_genuine"
        self.assertBlocked(self.evaluate(), "evaluator", "Fixture changed since validation")
        self.evaluator_error = None
        answer_task = next(t for t in FrozenDataset(ROOT).pilot_tasks if t.answer_based)
        plain_task = next(t for t in FrozenDataset(ROOT).pilot_tasks if not t.answer_based)
        comparator = IMAGES["gdm-math-swarm-comparator:local"]
        self.write("evaluator", {"all_required_passed": True, "image_ids": {"comparator": comparator},
                                 "self_compare": pilot_self_compare(skip={(answer_task.task_id, "bridge"),
                                                                          (plain_task.task_id, "pristine")})},
                   NOW - datetime.timedelta(minutes=5))
        result = self.evaluate()
        self.assertBlocked(result, "evaluator", "lack a passing self-comparison")
        self.assertEqual(sorted(result["evidence"]["evaluator"]["pilot_self_compare_gaps"]),
                         sorted([f"{answer_task.task_id}/bridge", f"{plain_task.task_id}/pristine"]))
        failing = pilot_self_compare()
        failing[0] = dict(failing[0], status="verified")  # a self-comparison must fail on sorryAx to count
        self.write("evaluator", {"all_required_passed": True, "image_ids": {"comparator": comparator},
                                 "self_compare": failing}, NOW - datetime.timedelta(minutes=4))
        self.assertBlocked(self.evaluate(), "evaluator", failing[0]["task_id"] + "/pristine")
        self.write("evaluator", {"all_required_passed": True, "image_ids": {"comparator": comparator},
                                 "self_compare": pilot_self_compare()}, NOW - datetime.timedelta(minutes=3))
        self.assertTrue(self.evaluate()["evidence"]["evaluator"]["passed"])
        with mock.patch("research_swarm.evaluator.validation_status", return_value={
                "record": str(self.tmp / "other.json"), "valid": True, "error": None, "image_id": comparator}):
            self.assertBlocked(self.evaluate(), "evaluator", "different record")
        with mock.patch("research_swarm.evaluator.validation_status", return_value={
                "record": str(launch_gates.newest_evidence(self.validation, "evaluator")[0]), "valid": True,
                "error": None, "image_id": "sha256:" + "9" * 64}):
            self.assertBlocked(self.evaluate(), "evaluator", "validated comparator image differs")

    def test_required_check_names_match_validation_scripts(self):
        """Contract: every check name the gates require is one the writers emit (a rename must update both)."""
        e2e = (ROOT / "scripts" / "validate_runtime.py").read_text()
        self.assertLessEqual(set(launch_gates.REQUIRED_EVIDENCE_CHECKS["runtime-e2e"]),
                             set(re.findall(r'^\s*"([a-z0-9_]+)": (?:background_lean_)?check\(', e2e, re.M)))
        isolation = (ROOT / "scripts" / "isolation_probes.py").read_text()
        self.assertLessEqual(set(launch_gates.ISOLATION_AGENT_CHECKS),
                             set(re.findall(r'checks\[f"\{agent\}:([a-z_]+)"\]', isolation)))
        self.assertLessEqual(set(launch_gates.REQUIRED_EVIDENCE_CHECKS["runtime-isolation"]),
                             set(re.findall(r'checks\["([a-z_]+)"\]', isolation)))
        capacity = (ROOT / "scripts" / "capacity_probe.py").read_text()
        names = set(re.findall(r'"([a-z_]+)": check\(', capacity))
        names |= set(re.findall(r'checks\["([a-z_]+)"\]', capacity))
        self.assertLessEqual(set(launch_gates.REQUIRED_EVIDENCE_CHECKS["runtime-capacity"]), names)
        compat = ROOT / "scripts" / "compat_check.py"
        if compat.exists():  # written by the compatibility workstream
            text = compat.read_text()
            for name in launch_gates.COMPATIBILITY_REQUIRED_CHECKS:
                self.assertTrue(name in text or "COMPATIBILITY_REQUIRED_CHECKS" in text, name)
            self.assertIn("request_ids", text)  # X4: the gate reads the ledger state of these ids

    def test_conference_race_writer_emits_complete_and_limits(self):
        """S4 contract: the conference-race gate's fields are ones scripts/validate_conference.py writes."""
        text = (ROOT / "scripts" / "validate_conference.py").read_text()
        if "--config" not in text:
            self.skipTest("scripts/validate_conference.py does not take --config yet (S4 writer, conference services)")
        for key in ("complete", "limits"):
            self.assertRegex(text, rf"""["']{key}["']|\b{key}\s*=""", key)

    def test_evidence_fields_match_writers(self):
        """Contract: the X3/X5 fields the gates require are ones the writers emit."""
        def emits(script, key):
            text = (ROOT / "scripts" / script).read_text()
            return re.search(rf"""["']{key}["']|\b{key}\s*=""", text) is not None
        for script in ("validate_runtime.py", "isolation_probes.py", "capacity_probe.py"):  # runtime-e2e/-isolation/-capacity
            self.assertTrue(emits(script, "compose_sha256"), script)
        for script in ("validate_judge.py", "validate_dataset.py"):  # judge; declarations and timing
            for key in ("complete", "limits"):
                self.assertTrue(emits(script, key), f"{script}: {key}")

    def test_runtime_evidence_requires_named_checks(self):
        agent, lean = IMAGES["gdm-math-swarm-agent:local"], IMAGES["gdm-math-swarm-lean:local"]
        checks = e2e_checks()
        del checks["truncated_tool_call_not_executed_and_session_continues"]
        self.write("runtime-e2e", {"all_required_passed": True, "image_ids": {"agent": agent, "lean": lean},
                                   "checks": checks}, NOW - datetime.timedelta(minutes=5))
        self.assertBlocked(self.evaluate(), "runtime-e2e", "truncated_tool_call_not_executed_and_session_continues")
        # Legacy evidence without the replay-consistency check cannot pass,
        # even if its aggregate boolean claims every required check passed.
        checks = e2e_checks()
        del checks["inspect_tool_result_replays_consistent"]
        self.write("runtime-e2e", {"all_required_passed": True, "image_ids": {"agent": agent, "lean": lean},
                                   "checks": checks}, NOW - datetime.timedelta(minutes=4))
        self.assertBlocked(self.evaluate(), "runtime-e2e", "inspect_tool_result_replays_consistent")
        # A check the record marked optional still has to pass when the gate names it.
        self.write("runtime-e2e", {"all_required_passed": True, "image_ids": {"agent": agent, "lean": lean},
                                   "checks": e2e_checks(sandbox_file_outside_root_rejected={
                                       "passed": False, "required": False})}, NOW - datetime.timedelta(minutes=4))
        self.assertBlocked(self.evaluate(), "runtime-e2e", "not passed: sandbox_file_outside_root_rejected")
        self.write("runtime-isolation", {"all_required_passed": True, "image_ids": {"agent": agent},
                                         "checks": isolation_checks(agents=("agent-001",))},
                   NOW - datetime.timedelta(minutes=5))
        self.assertBlocked(self.evaluate(), "runtime-isolation", "at least 2")
        checks = isolation_checks()
        del checks["agent-002:no_provider_keys"]
        self.write("runtime-isolation", {"all_required_passed": True, "image_ids": {"agent": agent}, "checks": checks},
                   NOW - datetime.timedelta(minutes=4))
        self.assertBlocked(self.evaluate(), "runtime-isolation", "agent-002:no_provider_keys")
        self.write("runtime-isolation", {"all_required_passed": True, "image_ids": {"agent": agent},
                                         "checks": isolation_checks(**{"agent-001:egress_denied": {"passed": False}})},
                   NOW - datetime.timedelta(minutes=3))
        self.assertBlocked(self.evaluate(), "runtime-isolation", "agent-001:egress_denied")
        self.write("runtime-isolation", {"all_required_passed": True, "image_ids": {"agent": agent},
                                         "checks": {k: v for k, v in isolation_checks().items()
                                                    if k != "judge_container_network_none"}},
                   NOW - datetime.timedelta(minutes=2))
        self.assertBlocked(self.evaluate(), "runtime-isolation", "judge_container_network_none")

    def test_provider_limit_record_is_informational(self):
        """Owner decisions, October 20, 2026: the provider-side key limit never refuses a launch."""
        cases = ({"all_required_passed": False, "checks": {"limit_does_not_reset": {"passed": False}}},
                 {"limit": None, "limit_remaining": None, "limit_reset": None, "warnings": ["no provider-side limit"]},
                 {"limit": 500, "limit_remaining": 500, "limit_reset": "monthly"})
        for n, record in enumerate(cases):
            self.write("provider-limit", dict({"all_required_passed": True}, **record),
                       NOW - datetime.timedelta(minutes=10 - n))
            gate = self.evaluate()["evidence"]["provider_limit"]
            self.assertTrue(gate["passed"], gate)
            self.assertEqual(gate["warnings"], record.get("warnings", []))
        self.write("provider-limit", {"all_required_passed": True, "note": "sk-or-v1-abcdef"},
                   NOW - datetime.timedelta(minutes=1))
        gate = self.evaluate()["evidence"]["provider_limit"]
        self.assertTrue(gate["passed"])
        self.assertTrue(any("credential" in w for w in gate["warnings"]))
        old = self.evaluate(now=NOW + datetime.timedelta(hours=500))
        self.assertTrue(old["evidence"]["provider_limit"]["passed"])  # no freshness refusal
        for path in launch_gates.evidence_files(self.validation, "provider-limit"):
            path.unlink()
        gate = self.evaluate()["evidence"]["provider_limit"]
        self.assertTrue(gate["passed"])
        self.assertIn("no provider-limit record yet", gate["reason"])

    def test_credential_presence_only(self):
        with mock.patch("research_swarm.secrets.credential_status",
                        return_value=dict(CREDENTIALS_OK, environment_variable_set=False)):
            self.assertBlocked(self.evaluate(), "credential_presence", "no OPENROUTER_API_KEY")
        with mock.patch("research_swarm.secrets.credential_status",
                        return_value=dict(CREDENTIALS_OK, env_file_present=True, env_file_regular=True,
                                          env_file_private=False)):
            self.assertBlocked(self.evaluate(), "credential_presence", "mode 600")


class ScriptTests(GateFixture):
    def test_provider_limit_script_needs_no_approval_flag(self):
        module = load_script("check_provider_limit")
        with mock.patch("research_swarm.secrets.load_openrouter_key", return_value="test-key-not-real-0123456789"), \
                redirect_stdout(io.StringIO()):
            code = module.main(["--output-dir", str(self.tmp / "limit")],
                               http_get=lambda url, headers: {"data": {"limit": None, "limit_remaining": None}})
        self.assertEqual(code, 0)
        self.assertEqual(len(list((self.tmp / "limit").glob("provider-limit-*.json"))), 1)
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            module.main(["--i-have-user-approval"])

    def test_preflight_spending_checkpoint(self):
        preflight = load_script("preflight")
        empty = preflight.spending_checkpoint(ROOT, self.tmp / "absent.sqlite", pilot_config())
        self.assertEqual((empty["ledger_exists"], empty["scopes"], empty["recent_runs"]), (False, {}, []))
        self.assertEqual((empty["run_budget"]["run_budget_usd"], empty["run_budget"]["source"]),
                         ("100", "config run_budget_usd"))
        default = preflight.spending_checkpoint(ROOT, self.ledger, {})["run_budget"]
        self.assertEqual((default["run_budget_usd"], default["source"]), ("100", "default"))
        self.assertIn("default", default["line"])
        self.assertIn("error", preflight.spending_checkpoint(ROOT, self.ledger, {"run_budget_usd": "x"})["run_budget"])
        ledger = ledger_run(self.ledger, "pilot-a", "pilot", 20)
        ledger.settle(ledger.reserve("pilot", "a", 1), 0.4, "provider_reported_cost")
        ledger.reserve("pilot", "b", 2)
        spending = preflight.spending_checkpoint(ROOT, self.ledger, pilot_config())
        pilot = spending["scopes"]["pilot"]
        self.assertEqual((pilot["settled_usd"], pilot["held_usd"], pilot["committed_usd"], pilot["unresolved_requests"]),
                         ("0.4", "2", "2.4", 1))
        self.assertEqual(spending["recent_runs"][-1]["run_id"], "pilot-a")
        text = "\n".join(preflight.render_spending(spending))
        self.assertIn("Run budget: $100 (config run_budget_usd)", text)
        self.assertIn("known costs $0.4000", text)
        self.assertIn("run pilot-a (pilot): committed $2.4000 of budget $20.0000", text)
        self.assertNotIn("cap", text.lower())
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            preflight.main(["--raise-ledger-cap", "compatibility"])  # no cap to raise

    def ready_report(self, config=None):
        preflight = load_script("preflight")
        return {"ready": True, "blockers": [], "launch_gates": {"evidence": {}}, "runtime_preflight": {},
                "spending": preflight.spending_checkpoint(ROOT, self.ledger, config or {})}

    def test_run_pilot_launches_without_prompt_flag_or_terminal(self):
        run_pilot = load_script("run_pilot")
        config_path = self.tmp / "pilot.json"
        config_path.write_text(json.dumps({"run_budget_usd": "25"}))
        calls, refreshed = [], []

        async def fake_run(config, repo):
            calls.append(config)
        ready = self.ready_report({"run_budget_usd": "25"})
        with mock.patch.object(run_pilot, "collect", return_value=ready), \
                mock.patch("builtins.input", side_effect=AssertionError("nothing may prompt")), \
                mock.patch.object(sys, "stdin", io.StringIO()), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run_pilot.main(["--config", str(config_path)], run=fake_run,
                                            refresh=lambda *a: refreshed.append(a)), 0)
            self.assertEqual((calls, refreshed), ([], []))  # preflight only by default: no refresh, no run
            self.assertEqual(run_pilot.main(["--config", str(config_path), "--launch-paid-pilot"], run=fake_run,
                                            refresh=lambda *a: refreshed.append(a)), 0)
            with mock.patch.object(run_pilot, "collect", return_value=dict(ready, ready=False, blockers=["x"])):
                self.assertEqual(run_pilot.main(["--config", str(config_path), "--launch-paid-pilot"], run=fake_run,
                                                refresh=lambda *a: None), 2)
        self.assertEqual(len(calls), 1)
        self.assertIn(str(Path("runs") / "pilot-"), calls[0]["run_dir"])
        self.assertEqual(refreshed[0][1], config_path)  # route pricing refreshed before the gates
        self.assertIn("Run budget: $25 (config run_budget_usd)", out.getvalue())
        for flag in ("--yes", "--i-have-user-approval"):
            with self.subTest(flag=flag), mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                run_pilot.main(["--config", str(config_path), "--launch-paid-pilot", flag], run=fake_run)

    def test_run_pilot_prints_the_default_budget_when_unset(self):
        run_pilot = load_script("run_pilot")
        config_path = self.tmp / "pilot.json"
        config_path.write_text("{}")

        async def fake_run(config, repo):
            pass
        with mock.patch.object(run_pilot, "collect", return_value=self.ready_report({})), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run_pilot.main(["--config", str(config_path), "--launch-paid-pilot"], run=fake_run,
                                            refresh=lambda *a: None), 0)
        self.assertIn("Run budget: $100 (default; set run_budget_usd in the config to specify it)", out.getvalue())

    def test_freeze_route_refresh_if_needed(self):
        freeze_route = load_script("freeze_route")
        config, calls, lines = pilot_config(), [], []
        self.write_route(when=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1))
        fresh = freeze_route.refresh_if_needed(config, "cfg.json", self.validation, freeze=calls.append,
                                               out=lines.append)
        self.assertEqual((fresh, calls), (None, []))  # a fresh freeze for these settings: nothing to do
        stale = freeze_route.refresh_if_needed(config, "cfg.json", self.tmp / "no-freeze",
                                               freeze=lambda argv: calls.append(argv) or 0, out=lines.append,
                                               sleep=lambda seconds: None)
        self.assertIn("no route-pricing freeze", stale)
        # A freeze that wrote no fresh valid record is tried again, three attempts in all.
        self.assertEqual(calls, [["--config", "cfg.json", "--output-dir", str(self.tmp / "no-freeze")]] * 3)
        self.assertIn("Refresh attempt 3: still no route-pricing freeze", lines[-1])

        def offline(argv):
            raise OSError("network unreachable")
        waits = []
        freeze_route.refresh_if_needed(config, "cfg.json", self.tmp / "no-freeze", freeze=offline, out=lines.append,
                                       sleep=waits.append)
        self.assertIn("failed (OSError", lines[-1])  # never raises
        self.assertEqual((sum("failed (OSError" in line for line in lines), waits), (3, [10, 30]))  # retried
        # A newest freeze that is not valid is refreshed; the attempts stop once a fresh valid one exists.
        attempts = []

        def flaky(argv):
            attempts.append(argv)
            if len(attempts) == 2:
                self.write_route(when=datetime.datetime.now(datetime.timezone.utc))
                return 0
            raise OSError("catalog 503")
        self.write_route(valid=False, reason="Pinned provider route unavailable",   # newer than the valid one above
                         when=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30))
        reason = freeze_route.refresh_if_needed(config, "cfg.json", self.validation, freeze=flaky, out=lines.append,
                                                sleep=lambda seconds: None)
        self.assertIn("not valid", reason)
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(launch_gates.route_pricing_refresh_reason(config, self.validation))

    def test_reconcile_script(self):
        module = load_script("reconcile_ledger")
        ledger = ledger_run(self.ledger, "pilot-r", "pilot", 20)
        uncertain, unknown, missing, stale = (ledger.reserve("pilot", "a", 1) for _ in range(4))
        for request in (uncertain, unknown, missing):
            ledger.settle(request, None)
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        (run_dir / "runtime.jsonl").write_text("\n".join(json.dumps({"kind": "inference_uncertain", "payload": p}) for p in (
            {"request_id": uncertain, "provider_generation_id": "gen-1"}, {"request_id": missing, "generation_id": "gen-404"},
            {"request_id": unknown, "provider_generation_id": None})) + "\n")
        ids = module.recorded_generation_ids([run_dir])
        self.assertEqual(ids, {uncertain: "gen-1", missing: "gen-404"})
        fetched = {"gen-1": {"model": MODEL_ID, "total_cost": 0.0123, "provider_name": "DeepSeek"}}
        from research_swarm.budget import list_requests
        entries = module.reconcile_requests(ledger, list_requests(self.ledger), ids, fetched.get)
        outcomes = {e["request_id"]: e["outcome"] for e in entries}
        self.assertEqual(outcomes, {uncertain: "reconciled", unknown: "no_generation_id", missing: "generation_not_found",
                                    stale: "skipped_in_flight_or_stale_reserved"})
        self.assertEqual(ledger.snapshot()["pilot"]["unresolved_requests"], 3)
        self.assertEqual(ledger.audit_log()[0]["new_actual_usd"], "0.0123")
        with mock.patch("research_swarm.secrets.load_openrouter_key", side_effect=AssertionError("key read")), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(module.main(["--ledger", str(self.ledger), "--run-dir", str(run_dir), "--dry-run"]), 0)
        self.assertIn("Dry run", out.getvalue())
        # The default settles from the unpaid generation lookup, without any approval flag.
        lookups = []
        with mock.patch("research_swarm.secrets.load_openrouter_key", side_effect=AssertionError("key read")), \
                redirect_stdout(io.StringIO()) as out:
            code = module.main(["--ledger", str(self.ledger), "--run-dir", str(run_dir)],
                               fetch=lambda gid: lookups.append(gid) or None)
        self.assertEqual((code, lookups), (0, ["gen-404"]))
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            module.main(["--ledger", str(self.ledger), "--i-have-user-approval"])

    def test_reconcile_default_loads_no_key_when_nothing_can_be_looked_up(self):
        module = load_script("reconcile_ledger")
        ledger = ledger_run(self.ledger, "pilot-n", "pilot", 20)
        ledger.settle(ledger.reserve("pilot", "a", 1), None)  # no generation id recorded anywhere
        with mock.patch("research_swarm.secrets.load_openrouter_key", side_effect=AssertionError("key read")), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(module.main(["--ledger", str(self.ledger)]), 0)
        self.assertIn("Nothing to look up", out.getvalue())

    def test_reconcile_judge_holds_with_expected_models_and_credentials_root(self):
        module = load_script("reconcile_ledger")
        ledger = ledger_run(self.ledger, "judge-run", "analysis", 50)
        pilot_ledger = ledger_run(self.ledger, "pilot-run", "pilot", 20)
        opus, sonnet = (ledger.reserve("analysis", agent, 1) for agent in (
            "judge-opus:verifier:agent-008.verify-b01", "judge-sonnet:judge_recheck:agent-020.judge.recheck.b01"))
        pilot = pilot_ledger.reserve("pilot", "agent-001", 1)
        for request in (opus, sonnet):
            ledger.settle(request, None)
        pilot_ledger.settle(pilot, None)
        from research_swarm.budget import list_requests
        fetched = {"gen-o": {"model": "anthropic/claude-opus-5.5", "total_cost": 0},
                   "gen-s": {"model": "anthropic/claude-sonnet-5.5", "total_cost": 0.0021}}
        ids = {opus: "gen-o", sonnet: "gen-s"}
        entries = module.reconcile_requests(ledger, list_requests(self.ledger, scope="analysis"), ids, fetched.get)
        self.assertEqual({e["outcome"] for e in entries}, {"model_mismatch"})  # default: the pilot model only
        entries = module.reconcile_requests(ledger, list_requests(self.ledger, scope="analysis"), ids, fetched.get,
                                            model_id=("anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5"))
        self.assertEqual({e["request_id"]: e["outcome"] for e in entries}, {opus: "reconciled", sonnet: "reconciled"})
        snapshot = read_snapshot(self.ledger)
        self.assertEqual((snapshot["analysis"]["uncertain_requests"], snapshot["pilot"]["uncertain_requests"]), (0, 1))
        self.assertEqual(snapshot["analysis"]["settled_usd"], "0.0021")
        creds = self.tmp / "creds"
        creds.mkdir()
        other = ledger.reserve("analysis", "judge-opus:verifier:agent-020.verify-b01", 1)
        ledger.settle(other, None)
        seen = []

        def key_loader(root):
            seen.append(Path(root))
            return "k"
        with mock.patch("research_swarm.secrets.load_openrouter_key", side_effect=key_loader), \
                mock.patch.object(module, "fetch_generation",
                                  return_value={"model": "anthropic/claude-opus-5.5", "total_cost": 0.5}), \
                redirect_stdout(io.StringIO()) as out:
            code = module.main(["--ledger", str(self.ledger), "--scope", "analysis", "--map", f"{other}=gen-x",
                                "--expect-model", "anthropic/claude-opus-5.5", "--credentials-root", str(creds)])
        self.assertEqual(code, 0)
        self.assertEqual(seen, [creds])
        self.assertIn('"reconciled"', out.getvalue())
        self.assertEqual(read_snapshot(self.ledger)["pilot"]["uncertain_requests"], 1)  # other scopes untouched
        audit = json.loads(sorted((self.ledger.parent / "reconciliations").glob("reconcile-*.json"))[-1].read_text())
        self.assertEqual((audit["expected_models"], audit["scope"], audit["credential_recorded"]),
                         (["anthropic/claude-opus-5.5"], "analysis", False))

    def test_reconcile_never_touches_a_hold_of_a_running_process(self):
        """Review finding: settling an in-flight hold would lose the running process's real bill; a 'reserved'
        hold is reconcilable (with --include-stale-reserved) only once its process is gone."""
        module = load_script("reconcile_ledger")
        ledger = ledger_run(self.ledger, "pilot-r", "pilot", 20)       # this process: alive
        hold = ledger.reserve("pilot", "agent-001", 1)
        holds = list_requests(self.ledger)
        planned, problems = module.plan_manual([(hold, "0.2")], holds, allow_reserved=True)
        self.assertEqual(planned, [])
        self.assertIn("still reserved by a running process", problems[0])
        results = module.reconcile_requests(ledger, holds, {hold: "gen-1"}, lambda *a: self.fail("fetched"),
                                            allow_reserved=True)
        self.assertEqual(results[0]["outcome"], "skipped_owner_running")
        self.assertEqual(list_requests(self.ledger)[0]["state"], "reserved")

    def test_reconcile_exception_path_hold_listing_and_manual_settlement(self):
        module = load_script("reconcile_ledger")
        ledger = ledger_run(self.ledger, "pilot-m", "pilot", 20)
        hold, other = ledger.reserve("pilot", "agent-007", 1), ledger.reserve("pilot", "agent-008", 1)
        ledger.settle(hold, None)
        ledger.settle(other, None)
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        events = [
            {"timestamp": 1792500000.25, "kind": "inference_admitted", "agent_id": "agent-007",
             "payload": {"request_id": hold, "reserved_usd": "1"}},
            {"timestamp": 1792500042.5, "kind": "inference_uncertain", "agent_id": "agent-007",
             "payload": {"request_id": hold, "billing": "uncertain", "error": "APIStatusError", "status": 502,
                         "provider_request_id": "req_abc123", "provider_error": "bad gateway",
                         "reason": "provider_error_after_admission"}},
            {"timestamp": 1792500050.0, "kind": "inference_finished", "agent_id": "agent-008",
             "payload": {"request_id": other, "basis": None, "provider_generation_id": "gen-9"}}]
        (run_dir / "runtime.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\nnot json\n")
        details = module.recorded_request_details([run_dir])
        self.assertEqual(details[hold]["provider_request_id"], "req_abc123")
        self.assertEqual(details[hold]["admitted_at"], "2026-10-20T12:40:00.250000Z")
        self.assertEqual((details[hold]["ended_at"], details[hold]["end_event"], details[hold]["status"]),
                         ("2026-10-20T12:40:42.500000Z", "inference_uncertain", 502))
        self.assertEqual(module.recorded_generation_ids([run_dir]), {other: "gen-9"})

        def run(*argv):
            with mock.patch("research_swarm.secrets.load_openrouter_key", side_effect=AssertionError("key read")), \
                    mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
                    redirect_stdout(io.StringIO()) as out:
                code = module.main(["--ledger", str(self.ledger), "--run-dir", str(run_dir), *argv])
            return code, out.getvalue()

        code, text = run("--dry-run")
        listing = json.loads(text.split("\nDry run")[0])["unresolved"]
        entry = next(e for e in listing if e["request_id"] == hold)
        self.assertEqual((entry["generation_id"], entry["provider_request_id"], entry["agent_id"]),
                         (None, "req_abc123", "agent-007"))
        self.assertIn("--manual REQUEST_ID COST", entry["next_step"])
        self.assertIn("generation lookup", next(e for e in listing if e["request_id"] == other)["next_step"])
        self.assertEqual(run("--manual", hold, "0.31", "--note", "activity log", "--dry-run")[0], 0)
        self.assertEqual(run("--manual", "nope", "0.31", "--note", "x")[0], 2)
        self.assertEqual(run("--manual", hold, "-1", "--note", "x")[0], 2)
        self.assertEqual(run("--manual", hold, "0.31", "--manual", "nope", "0.1", "--note", "x")[0], 2)  # all-or-nothing
        self.assertEqual(read_snapshot(self.ledger)["pilot"]["uncertain_requests"], 2)
        code, text = run("--manual", hold, "0.31", "--note", "OpenRouter activity 12:40Z")
        self.assertEqual(code, 0)
        snapshot = read_snapshot(self.ledger)["pilot"]
        self.assertEqual((snapshot["uncertain_requests"], snapshot["manual_reconciliations"]), (1, 1))
        audit = BudgetLedger(self.ledger).audit_log()[-1]
        self.assertEqual((audit["request_id"], audit["basis"], audit["new_actual_usd"]),
                         (hold, "manual_reconciliation", "0.31"))
        self.assertIn("OpenRouter activity 12:40Z", audit["note"])
        files = list((self.ledger.parent / "reconciliations").glob("reconcile-*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text())["mode"], "manual")
        self.assertEqual(run("--manual", hold, "0.31", "--note", "again")[0], 2)
        other_code, _ = run("--manual", other, "0.2")  # the note is optional
        self.assertEqual(other_code, 0)
        self.assertIn("amount entered with --manual", BudgetLedger(self.ledger).audit_log()[-1]["note"])

    def test_project_cost_from_reservations(self):
        module = load_script("project_cost")
        settings = ProviderSettings(pricing_verified=True)
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        events = [{"kind": "inference_admitted", "agent_id": a,
                   "payload": {"reserved_usd": str(settings.reservation("x" * n))}}
                  for a in ("agent-001", "agent-002") for n in (70000, 75000)]
        events.append({"kind": "inference_admitted", "agent_id": "agent-003", "payload": {"request_bytes": 71000}})
        # request_bytes (runtime) wins over the reservation, even when the reservation hit the context cap.
        events.append({"kind": "inference_admitted", "agent_id": "agent-003",
                       "payload": {"request_bytes": 1500000, "reserved_usd": str(settings.reservation("x" * 1500000))}})
        (run_dir / "runtime.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
        sizes = module.request_sizes(run_dir, ProviderSettings())
        self.assertEqual(sizes, {"agent-001": [70000, 75000], "agent-002": [70000, 75000],
                                 "agent-003": [71000, 1500000]})
        prices = (Decimal("0.0000003"), Decimal("0.0000012"), Decimal("0.000000006"))
        no_cache, cache, compactions = module.simulate(floor=20000, growth=3000, output=2000, threshold=30000,
                                                       summary=1000, turns=10, prices=prices, hit_rate=0.9)
        self.assertEqual(len(no_cache), 10)
        self.assertGreater(compactions, 0)
        self.assertTrue(all(c <= n for c, n in zip(cache, no_cache)))
        self.assertEqual(no_cache[0], Decimal(20000) * prices[0] + 2000 * prices[1])
        with redirect_stdout(io.StringIO()) as out:
            module.main(["--run-dir", str(run_dir), "--validation-dir", str(self.validation), "--rates", "1"])
        result = json.loads(out.getvalue().split("\nPROJECTION")[0])
        self.assertTrue(result["is_projection"])
        self.assertEqual(result["measured"]["first_request_bytes"], {"min": 70000, "max": 71000})
        # Defaults: agents from configs/pilot.json; the fixture evidence records no threshold.
        self.assertEqual(result["assumptions"]["agents"], json.loads((ROOT / "configs" / "pilot.json").read_text())
                         ["agent_count"])
        self.assertEqual(result["assumption_sources"]["agents"], "pilot.json agent_count")
        self.assertEqual(result["assumptions"]["compaction_threshold"], module.DEFAULT_COMPACTION_THRESHOLD)
        self.assertIn("ASSUMPTION", result["assumption_sources"]["compaction_threshold"])

    def test_project_cost_reads_threshold_from_evidence_and_agents_from_config(self):
        module = load_script("project_cost")
        settings = ProviderSettings(pricing_verified=True)
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        (run_dir / "runtime.jsonl").write_text(json.dumps({"kind": "inference_admitted", "agent_id": "agent-001", "payload": {
            "reserved_usd": str(settings.reservation("x" * 70000))}}) + "\n")
        config = self.tmp / "small.json"
        config.write_text(json.dumps({"agent_count": 8}))

        def project(*argv):
            with redirect_stdout(io.StringIO()) as out:
                module.main(["--validation-dir", str(self.validation), "--rates", "1", *argv])
            return json.loads(out.getvalue().split("\nPROJECTION")[0])

        evidence = self.write("runtime-e2e", {"all_required_passed": True, "run_dir": str(run_dir),
                                              "compaction_threshold_tokens": 120000}, NOW - datetime.timedelta(minutes=5))
        result = project("--config", str(config))  # run_dir and threshold from the newest runtime-e2e evidence
        self.assertEqual(result["run_dir"], str(run_dir))
        self.assertEqual((result["assumptions"]["agents"], result["assumption_sources"]["agents"]), (8, "small.json agent_count"))
        self.assertEqual(result["assumptions"]["compaction_threshold"], 120000)
        self.assertIn(evidence.name, result["assumption_sources"]["compaction_threshold"])
        result = project("--run-dir", str(run_dir), "--compaction-threshold", "90000", "--agents", "3")
        self.assertEqual((result["assumptions"]["compaction_threshold"], result["assumptions"]["agents"]), (90000, 3))
        self.assertEqual(result["assumption_sources"], {"agents": "command line (--agents)",
                                                        "compaction_threshold": "command line (--compaction-threshold)"})
        self.assertEqual(project("--config", str(self.tmp / "absent.json"))["assumption_sources"]["agents"],
                         "runtime.APPROVED_AGENT_COUNT")
        # Inside a check (the writer's check(..., compaction_threshold_tokens=N) detail).
        nested = {"checks": {"compaction_triggered": {"passed": True, "compaction_threshold_tokens": 150000}}}
        self.assertEqual(module.evidence_compaction_threshold(nested), 150000)
        self.assertEqual(module.evidence_compaction_threshold(dict(nested, compaction_threshold_tokens=140000)), 140000)
        conflicting = {"checks": {"a": {"compaction_threshold_tokens": 1}, "b": {"compaction_threshold_tokens": 2}}}
        for record in (conflicting, {"compaction_threshold_tokens": True}, {"compaction_threshold_tokens": "1"},
                       {"compaction_threshold_tokens": 0}, {}, None):
            self.assertIsNone(module.evidence_compaction_threshold(record), record)
        evidence = self.write("runtime-e2e", {"all_required_passed": True, "run_dir": str(run_dir), **conflicting},
                              NOW - datetime.timedelta(minutes=4))
        result = project("--evidence", str(evidence))
        self.assertEqual(result["assumptions"]["compaction_threshold"], module.DEFAULT_COMPACTION_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
