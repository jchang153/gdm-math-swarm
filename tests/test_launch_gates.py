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
from types import SimpleNamespace
from unittest import mock

from research_swarm import launch_gates
from research_swarm.budget import BudgetLedger, DEFAULT_CAPS, cap_changes, raise_cap, read_snapshot
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
    config = json.loads((ROOT / "configs" / "pilot.json").read_text())
    for key in ("pilot_prior_held_usd", "pilot_prior_committed_usd", "pilot_prior_uncertain_requests"):
        config.pop(key, None)
    config.pop("pilot_prior_settled_usd", None)
    config.pop("pilot_additional_allowance_usd", None)
    # Historical caps, matching GateFixture's patched DEFAULT_CAPS (pilot $20, analysis $5
    # before the October 17 judge raises; the shipped config now states analysis $50).
    config.update({"max_pilot_usd": 20, "max_analysis_usd": 5, "verified_integration": True,
                   "paid_launch_enabled": True, "allow_unlimited_provider_key": False, **overrides})
    return config


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
        # Existing evidence/provider tests describe the historical $20 pilot.
        caps = mock.patch.dict(DEFAULT_CAPS, {"pilot":20,"analysis":5,"compatibility":2}, clear=True)
        caps.start()
        self.addCleanup(caps.stop)
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
            ledger = BudgetLedger(self.ledger)
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
        self.assertBlocked(self.evaluate(now=NOW + datetime.timedelta(hours=80)), "route_pricing", "old")
        lower = pilot_config(provider_settings={"prompt_usd_per_token": "0.00000015"})
        result = self.evaluate(lower)
        self.assertBlocked(result, "route_pricing", "differ from the ProviderSettings")
        self.assertIn("offline catalog validation failed", result["evidence"]["route_pricing"]["reason"])
        self.assertBlocked(self.evaluate(pilot_config(provider_route=other_route(pilot_config()["provider_route"]))),
                           "route_pricing")
        snapshot = next((self.validation / "catalog-snapshots").iterdir())
        snapshot.write_text("{}")
        self.assertBlocked(self.evaluate(), "route_pricing", "snapshot")

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
        self.assertBlocked(self.evaluate(pilot_config(max_compatibility_usd=0)), "config_caps", "max_compatibility_usd")
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

    def test_prompt_variant_gate(self):
        """Absent or a known variant passes; anything else blocks the launch."""
        self.assertTrue(self.evaluate()["evidence"]["prompt_variant"]["passed"])
        for variant in ("baseline", "no_stop", "lenient"):
            result = self.evaluate(pilot_config(prompt_variant=variant))
            self.assertTrue(result["evidence"]["prompt_variant"]["passed"], variant)
        self.assertBlocked(self.evaluate(pilot_config(prompt_variant="permissive")), "prompt_variant",
                           "is not one of")
        self.assertBlocked(self.evaluate(pilot_config(prompt_variant=None)), "prompt_variant", "is not one of")

    def ablation_config(self, **overrides):
        values = dict(budget_track="ablation", agent_count=10, max_connections=10,
                      persona_counts={"algebra": 3, "analysis_topology": 3, "combinatorics": 2, "number_theory": 2},
                      max_pilot_usd="60", max_analysis_usd="0", max_compatibility_usd=2,
                      allow_unlimited_provider_key=False)
        return pilot_config(**{**values, **overrides})

    def evaluate_ablation(self, config=None, ledger_path=None):
        return launch_gates.evaluate(config or self.ablation_config(), ROOT, validation_dir=self.validation,
                                     ledger_path=ledger_path or self.tmp / "ablation" / "ablation-budget.sqlite",
                                     legacy_ledger_path=self.legacy, now=NOW, image_id=lambda tag: self.images.get(tag))

    def test_ablation_track_settings(self):
        """Ablation track: 10 or 20 agents, its own caps, no unlimited key; the baseline is unchanged."""
        result = self.evaluate_ablation()
        for gate in ("approved_settings", "budget_track", "config_caps"):
            self.assertTrue(result["evidence"][gate]["passed"], (gate, result["evidence"][gate]["reason"]))
        self.assertEqual(result["ledger"]["default_caps"], {"pilot": "60", "analysis": "0", "compatibility": "2"})
        self.assertTrue(self.evaluate_ablation(self.ablation_config(agent_count=20, max_connections=20))["evidence"]
                        ["approved_settings"]["passed"])
        self.assertBlocked(self.evaluate_ablation(self.ablation_config(agent_count=15, max_connections=15)),
                           "approved_settings", "agent_count")
        self.assertBlocked(self.evaluate_ablation(self.ablation_config(allow_unlimited_provider_key=True,
                                                              unlimited_provider_key_approval="x")),
                           "approved_settings", "must be false on the ablation track")
        self.assertBlocked(self.evaluate_ablation(self.ablation_config(max_pilot_usd="100")), "config_caps", "max_pilot_usd")
        self.assertBlocked(self.evaluate(pilot_config(budget_track="other")), "budget_track", "is not 'ablation'")
        # Without the track, 10 agents stay refused.
        self.assertBlocked(self.evaluate(pilot_config(agent_count=10, max_connections=10)),
                           "approved_settings", "agent_count")

    def test_ablation_track_uses_its_own_ledger(self):
        from research_swarm import budget
        self.assertEqual(budget.ledger_path_for(self.ablation_config(), ROOT), ROOT / "state" / "ablation-budget.sqlite")
        self.assertEqual(budget.ledger_path_for(pilot_config(), ROOT), budget.DEFAULT_LEDGER_PATH(ROOT))
        self.assertEqual(budget.caps_for(pilot_config()), budget.DEFAULT_CAPS)
        # A fresh ablation ledger starts at the ablation caps and passes; one with other caps does not.
        own = self.tmp / "ablation" / "ablation-budget.sqlite"
        BudgetLedger(own, budget.ABLATION_CAPS)
        self.assertTrue(self.evaluate_ablation(ledger_path=own)["evidence"]["ledger"]["passed"])
        other = self.tmp / "other" / "budget.sqlite"
        BudgetLedger(other, {"pilot": 100, "analysis": 0, "compatibility": 2})
        result = self.evaluate_ablation(ledger_path=other)
        self.assertBlocked(result, "ledger", "differs from the approved $60")
        # The ablation track never reserves against the baseline ledger.
        result = self.evaluate_ablation(ledger_path=budget.DEFAULT_LEDGER_PATH(ROOT))
        self.assertBlocked(result, "ledger", "must not use the baseline ledger")

    def test_ablation_ledger_holds_block(self):
        from research_swarm import budget
        own = self.tmp / "ablation" / "ablation-budget.sqlite"
        ledger = BudgetLedger(own, budget.ABLATION_CAPS)
        ledger.reserve("pilot", "agent-001", "0.5")  # left in flight
        self.assertBlocked(self.evaluate_ablation(ledger_path=own), "ledger", "unresolved pilot hold")

    def test_ablation_example_config_passes_the_settings_gates(self):
        config = json.loads((ROOT / "configs" / "ablation.example.json").read_text())
        result = self.evaluate_ablation({**config, "paid_launch_enabled": True, "verified_integration": True})
        for gate in ("approved_settings", "budget_track", "prompt_variant", "config_caps", "seeded_track"):
            self.assertTrue(result["evidence"][gate]["passed"], (gate, result["evidence"][gate]["reason"]))
        from research_swarm.runtime import assign_personas, persona_counts
        self.assertEqual(config["persona_counts"],
                         persona_counts(assign_personas(config["agent_count"], config["persona_seed"])))
        self.assertFalse(config["paid_launch_enabled"])

    def test_invalid_budget_track_never_selects_a_ledger(self):
        from research_swarm import budget
        bad = pilot_config(budget_track="ablations")
        for helper in (lambda: budget.ledger_path_for(bad, ROOT), lambda: budget.caps_for(bad)):
            with self.assertRaises(ValueError):
                helper()
        result = launch_gates.evaluate(bad, ROOT, validation_dir=self.validation, legacy_ledger_path=self.legacy,
                                       now=NOW, image_id=lambda tag: self.images.get(tag))
        self.assertBlocked(result, "budget_track", "is not 'ablation'")
        self.assertEqual(result["ledger"]["path"], "state/invalid-budget-track.sqlite")
        self.assertFalse((ROOT / "state" / "invalid-budget-track.sqlite").exists())

    def test_compat_check_refuses_an_invalid_track_before_opening_a_ledger(self):
        compat = load_script("compat_check")
        config = self.tmp / "bad.json"
        config.write_text(json.dumps(pilot_config(budget_track="ablations")))
        ledger = self.tmp / "never" / "budget.sqlite"
        with mock.patch.object(compat, "ledger_path_for", side_effect=compat.ledger_path_for) as chosen, \
                redirect_stdout(io.StringIO()) as out:
            code = compat.main(["--i-have-user-approval", "--config", str(config)],
                               gates=lambda c: self.fail("gates must not run"),
                               environment=lambda c: self.fail("environment must not run"))
        self.assertEqual(code, 2)
        self.assertIn("is not 'ablation'", out.getvalue())
        self.assertFalse(ledger.exists())
        self.assertIn("budget_track", compat.REQUIRED_GATES)

    def test_paid_judging_refuses_ablation_runs(self):
        from research_swarm import budget
        run = self.tmp / "ablation-run"
        run.mkdir()
        (run / "settings.json").write_text(json.dumps({"config": {"budget_track": "ablation"},
                                                       "effective": {"budget_track": "ablation"}}))
        self.assertEqual(budget.run_budget_track(run), "ablation")
        (run / "settings.json").write_text(json.dumps({"config": {}, "effective": {"budget_track": "baseline"}}))
        self.assertEqual(budget.run_budget_track(run), "baseline")
        self.assertEqual(budget.run_budget_track(self.tmp / "missing"), "unknown")
        source = (ROOT / "scripts" / "run_transcript_judge.py").read_text()
        self.assertIn('if track != "baseline":', source)

    def test_reconcile_opens_the_ablation_ledger_with_its_caps(self):
        from research_swarm import budget
        reconcile = load_script("reconcile_ledger")
        path = self.tmp / "state" / "ablation-budget.sqlite"
        ledger = BudgetLedger(path, budget.ABLATION_CAPS)
        ledger.reserve("pilot", "agent-001", "0.5")
        self.assertEqual(reconcile.ledger_caps(path), budget.ABLATION_CAPS)
        self.assertIsNone(reconcile.ledger_caps(self.tmp / "state" / "budget.sqlite"))
        BudgetLedger(path, caps=reconcile.ledger_caps(path))  # opens without a cap mismatch

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
        result = self.evaluate(dict(example, verified_integration=True, paid_launch_enabled=True))
        self.assertBlocked(result, "seeded_track", "track 'seeded'")
        self.assertIn("seeded-track configuration", result["evidence"]["seeded_track"]["reason"])
        self.assertBlocked(result, "approved_settings", "track")

    def test_seeded_example_config(self):
        """S3: configs/seeded.example.json is the pilot config plus a disabled seeded block with null semantics."""
        from dataclasses import fields
        from research_swarm import seeded as seeded_module
        from research_swarm.seeded import SeededConfigError, SeededTrackConfig, resolve_track
        pilot = json.loads((ROOT / "configs" / "pilot.json").read_text())
        example = json.loads((ROOT / "configs" / "seeded.example.json").read_text())
        track_keys = {"experiment", "track", "seeded", "verified_integration", "paid_launch_enabled"}
        self.assertEqual({k: v for k, v in example.items() if k not in track_keys},
                         {k: v for k, v in pilot.items() if k not in track_keys})  # stays in sync with the pilot
        self.assertEqual((example["track"], example["seeded_contagion_enabled"], example["verified_integration"],
                          example["paid_launch_enabled"]), ("seeded", False, pilot["verified_integration"], False))
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
        keys |= set(launch_gates.approved_settings()) | set(launch_gates.CONFIG_CAP_KEYS.values())
        keys |= {key for key, _ in launch_gates.JUDGE_LIMIT_KEYS.values()} | set(launch_gates.CAPACITY_KEYS)
        keys |= {f"{role}_image" for role in launch_gates.DEFAULT_IMAGES}
        keys |= {launch_gates.MAX_CONNECTIONS_KEY, launch_gates.SEEDED_CONFIG_KEY}
        for required in ("max_connections", "max_subprocesses", "warm_up_memory", "warm_up_timeout_seconds",
                         "in_flight_grace_seconds", "stop_grace_seconds", "seeded"):
            self.assertIn(required, keys)
        self.assertEqual(sorted(k for k in keys if f"`{k}`" not in readme), [])

    def test_attestations_remain_required(self):
        result = self.evaluate(pilot_config(verified_integration=False, paid_launch_enabled=False))
        self.assertBlocked(result, "verified_integration")
        self.assertBlocked(result, "paid_launch_enabled")

    def test_ledger_holds_violations_caps_and_legacy(self):
        ledger = BudgetLedger(self.ledger)
        call = ledger.reserve("pilot", "agent-001", 1)
        self.assertBlocked(self.evaluate(), "ledger", "unresolved")
        ledger.settle(call, 0.25, "provider_reported_cost")
        result = self.evaluate()
        self.assertTrue(result["evidence"]["ledger"]["passed"])
        self.assertEqual(result["evidence"]["ledger"]["pilot_remaining_usd"], "19.75")
        self.assertEqual(result["ledger"]["snapshot"]["pilot"]["settled_usd"], "0.25")
        ledger.settle(ledger.reserve("pilot", "agent-001", 0.1), 0.5)
        self.assertBlocked(self.evaluate(), "ledger", "estimate_violation")
        self.legacy.parent.mkdir(parents=True)
        self.legacy.write_bytes(b"")
        self.assertBlocked(self.evaluate(), "ledger", "legacy ledger")

    def test_repeat_ledger_requires_preserved_spending_and_audited_raise(self):
        self.ledger.unlink()
        config = pilot_config(max_pilot_usd="119.636804044", pilot_prior_settled_usd="19.636804044",
                              pilot_additional_allowance_usd="100")
        with mock.patch.dict(DEFAULT_CAPS, {"pilot":"119.636804044","analysis":5,"compatibility":2}, clear=True):
            self.assertBlocked(self.evaluate(config), "ledger", "existing cumulative pilot ledger")
            without_prior = dict(config)
            del without_prior["pilot_prior_settled_usd"]
            self.assertBlocked(self.evaluate(without_prior), "ledger", "repeat approval requires")
            old = BudgetLedger(self.ledger, {"pilot":20,"analysis":5,"compatibility":2})
            old.settle(old.reserve("pilot", "old-run", 20), "19.636804044")
            self.assertBlocked(self.evaluate(config), "ledger", "audited cap raise")
            raise_cap(self.ledger, "pilot", "119.636804044", "User approved fresh $100 baseline")
            result = self.evaluate(config)
            self.assertTrue(result["evidence"]["ledger"]["passed"], result["evidence"]["ledger"])
            self.assertEqual(result["evidence"]["ledger"]["pilot_remaining_usd"], "100")
            current = BudgetLedger(self.ledger)
            call = current.reserve("pilot", "new-run", 1)
            self.assertBlocked(self.evaluate(config), "ledger", "unresolved")
            current.settle(call, ".5")
            self.assertBlocked(self.evaluate(config), "ledger", "prior spending")

    def test_eight_gib_repeat_requires_exact_preserved_snapshot_and_metadata(self):
        from research_swarm.budget import _pilot_calls_sha256, _read_only
        from contextlib import closing
        import sqlite3
        self.ledger.unlink()
        old = BudgetLedger(self.ledger, {"pilot":20,"analysis":5,"compatibility":2})
        old.settle(old.reserve("pilot", "first", 20), "19.636804044")
        raise_cap(self.ledger, "pilot", "119.636804044", "First repeat approved")
        old = BudgetLedger(self.ledger, {"pilot":"119.636804044","analysis":5,"compatibility":2})
        old.settle(old.reserve("pilot", "second", 90), "86.091194430")
        for amount in [".3"] * 6 + [".5276232"]:
            old.settle(old.reserve("pilot", "uncertain", amount), None)
        with closing(_read_only(self.ledger)) as db:
            digest = _pilot_calls_sha256(db)
        config = pilot_config(max_pilot_usd="208.055621674", pilot_prior_settled_usd="105.727998474",
                              pilot_prior_held_usd="2.3276232", pilot_prior_committed_usd="108.055621674",
                              pilot_additional_allowance_usd="100", pilot_prior_uncertain_requests=7)
        with mock.patch.dict(DEFAULT_CAPS, {"pilot":"208.055621674","analysis":5,"compatibility":2}, clear=True), \
                mock.patch("research_swarm.budget.EIGHT_GIB_PRIOR_CALLS_SHA256", digest):
            self.assertBlocked(self.evaluate(config), "ledger")
            raise_cap(self.ledger, "pilot", "208.055621674", "User authorized 8 GiB repeat")
            result = self.evaluate(config)
            self.assertTrue(result["evidence"]["ledger"]["passed"], result["evidence"]["ledger"])
            self.assertEqual(result["evidence"]["ledger"]["pilot_remaining_usd"], "100")
            for key in ("pilot_prior_settled_usd", "pilot_prior_held_usd", "pilot_prior_committed_usd",
                        "pilot_additional_allowance_usd", "pilot_prior_uncertain_requests"):
                for replacement in (None, "0"):
                    with self.subTest(key=key, replacement=replacement):
                        changed = dict(config)
                        if replacement is None:
                            del changed[key]
                        else:
                            changed[key] = replacement
                        self.assertBlocked(self.evaluate(changed), "ledger", "metadata")
            clean = self.ledger.read_bytes()
            for sql in ("UPDATE calls SET agent='changed' WHERE agent='first'", "DELETE FROM cap_changes",
                        "UPDATE calls SET state='reserved' WHERE agent='uncertain'"):
                with self.subTest(sql=sql):
                    self.ledger.write_bytes(clean)
                    with sqlite3.connect(self.ledger) as db:
                        db.execute(sql)
                    self.assertBlocked(self.evaluate(config), "ledger")
            self.ledger.write_bytes(clean)
            current = BudgetLedger(self.ledger)
            call = current.reserve("pilot", "new", 1)
            self.assertBlocked(self.evaluate(config), "ledger")
            current.settle(call, ".5")
            self.assertBlocked(self.evaluate(config), "ledger")
            self.ledger.unlink()
            self.assertBlocked(self.evaluate(config), "ledger", "replacement ledger")
            BudgetLedger(self.ledger)
            self.assertBlocked(self.evaluate(config), "ledger")

    def test_ledger_with_old_caps_blocks(self):
        self.ledger.unlink()
        BudgetLedger(self.ledger, {"pilot": 20, "analysis": 5, "compatibility": 0})
        result = self.evaluate()
        self.assertBlocked(result, "ledger", "compatibility cap 0 is below the approved $2")
        self.assertIn("--raise-ledger-cap compatibility", result["evidence"]["ledger"]["reason"])

    def test_gates_never_create_a_missing_ledger(self):
        self.ledger.unlink()
        result = self.evaluate()
        self.assertFalse(self.ledger.exists())
        self.assertTrue(result["evidence"]["ledger"]["passed"])
        self.assertBlocked(result, "compatibility", "no compatibility scope")

    def test_compatibility_evidence_required_beyond_attestation(self):
        for path in launch_gates.evidence_files(self.validation, "compatibility"):
            path.unlink()
        result = self.evaluate(pilot_config(verified_integration=True))
        self.assertBlocked(result, "compatibility", "no compatibility-<UTC>.json")
        self.assertIn("$2", result["evidence"]["compatibility"]["reason"])
        self.assertTrue(result["evidence"]["verified_integration"]["passed"])
        self.write_compatibility(settle=False)
        result = self.evaluate(pilot_config(verified_integration=False))
        self.assertTrue(result["evidence"]["compatibility"]["passed"], result["evidence"]["compatibility"])
        self.assertBlocked(result, "verified_integration")  # the explicit user confirmation is still required

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
        blocked("exceeds the $2", spent_usd="2.01")
        blocked("nonnegative decimal", spent_usd="NaN")
        blocked("requests", requests=0)
        blocked("agent image", image_ids={"lean": IMAGES["gdm-math-swarm-lean:local"]})
        checks = passed_checks(launch_gates.COMPATIBILITY_REQUIRED_CHECKS)
        del checks["reasoning_cap_enforced"]  # required since the reasoning-cap check was added
        blocked("missing required checks (older or different script version?): reasoning_cap_enforced", checks=checks)
        blocked("lacks request_ids", request_ids=[])
        blocked("lacks request_ids", drop=("request_ids",))
        blocked("lacks request_ids", request_ids="not-a-list")
        blocked("not in the shared ledger", request_ids=["not-a-ledger-request"])
        blocked("requests 3 differs from the 1 recorded request_ids", requests=3)
        blocked("duplicates", request_ids=self.compat_ids * 2, requests=1)
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

    def test_compatibility_ledger_holds_block(self):
        ledger = BudgetLedger(self.ledger)
        hold = ledger.reserve("compatibility", "compat-agent", "0.5")
        ledger.settle(hold, None)
        self.assertBlocked(self.evaluate(), "compatibility", "1 unresolved compatibility hold")
        ledger.reconcile(hold, "0.6", note="generation total_cost")
        self.assertBlocked(self.evaluate(), "compatibility", "estimate_violation")
        self.assertTrue(self.evaluate()["evidence"]["ledger"]["passed"])  # the pilot scope is unaffected

    def test_compatibility_gate_passes_after_reconciling_the_checks_hold(self):
        """SPEND-1: spent_usd counted a hold's full reservation; reconciling it lower must not deadlock the gate."""
        ledger = BudgetLedger(self.ledger)
        first = ledger.reserve("compatibility", "agent-001", "0.05")
        ledger.settle(first, "0.009", "provider_reported_cost")
        hold = ledger.reserve("compatibility", "agent-001", "0.055")
        ledger.settle(hold, None)  # uncertain (e.g. cancelled by the max_minutes abort)
        # compat_check records the committed delta at run end, which includes the whole hold.
        self.write_compatibility(spent="0.064", settle=False, request_ids=[first, hold],
                                 when=NOW - datetime.timedelta(minutes=30))
        self.assertBlocked(self.evaluate(), "compatibility", "1 of the evidence's requests are unresolved")
        ledger.reconcile(hold, "0.004", note="generation total_cost")
        self.assertLess(Decimal(read_snapshot(self.ledger)["compatibility"]["committed_usd"]) - Decimal("0.0421"),
                        Decimal("0.064"))  # the old committed-delta comparison would have blocked here
        result = self.evaluate()
        self.assertTrue(result["evidence"]["compatibility"]["passed"], result["evidence"]["compatibility"])
        self.assertEqual(result["evidence"]["compatibility"]["ledger_requests"],
                         {"count": 2, "states": {"settled": 2}, "settled_usd": "0.013"})
        # A manual (user-approved) reconciliation resolves a hold the same way.
        manual = ledger.reserve("compatibility", "agent-001", "0.05")
        ledger.settle(manual, None)
        self.write_compatibility(spent="0.05", settle=False, request_ids=[manual],
                                 when=NOW - datetime.timedelta(minutes=20))
        self.assertBlocked(self.evaluate(), "compatibility", "unresolved")
        ledger.reconcile(manual, "0.02", basis="manual_reconciliation", note="activity log; user approved")
        self.assertTrue(self.evaluate()["evidence"]["compatibility"]["passed"])

    def test_compatibility_requests_must_not_exceed_reservation_or_leave_scope(self):
        ledger = BudgetLedger(self.ledger)
        hold = ledger.reserve("compatibility", "agent-001", "0.05")
        ledger.settle(hold, None)
        self.write_compatibility(spent="0.05", settle=False, request_ids=[hold],
                                 when=NOW - datetime.timedelta(minutes=30))
        ledger.reconcile(hold, "0.06", note="generation total_cost")  # above its reservation
        self.assertBlocked(self.evaluate(), "compatibility", "1 of the evidence's requests exceeded their reservation")
        ledger.acknowledge_violation(hold, "estimator reviewed")
        result = self.evaluate()  # an acknowledged violation unblocks the scope, not this evidence
        self.assertBlocked(result, "compatibility", "exceeded their reservation")
        self.assertNotIn("unacknowledged", result["evidence"]["compatibility"]["reason"])
        pilot = ledger.reserve("pilot", "agent-001", "0.05")
        ledger.settle(pilot, "0.01", "provider_reported_cost")
        self.write_compatibility(settle=False, request_ids=[pilot], when=NOW - datetime.timedelta(minutes=20))
        self.assertBlocked(self.evaluate(), "compatibility", "not in ledger scope 'compatibility'")

    def test_compatibility_requests_settled_above_the_cap_block(self):
        self.ledger.unlink()
        ledger = BudgetLedger(self.ledger, {"pilot": 20, "analysis": 5, "compatibility": 5})
        request = ledger.reserve("compatibility", "agent-001", "3")
        ledger.settle(request, "2.5", "provider_reported_cost")
        self.write_compatibility(spent="1.5", settle=False, request_ids=[request],
                                 when=NOW - datetime.timedelta(minutes=30))
        self.assertBlocked(self.evaluate(), "compatibility", "settled 2.5, above the $2 compatibility allowance")

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

    def test_provider_limit_evidence(self):
        self.write("provider-limit", {"all_required_passed": False, "checks": {"limit_does_not_reset": {"passed": False}}})
        self.assertBlocked(self.evaluate(), "provider_limit", "limit_does_not_reset")
        self.write("provider-limit", {"all_required_passed": True, "note": "sk-or-v1-abcdef"},
                   NOW - datetime.timedelta(minutes=1))
        self.assertBlocked(self.evaluate(), "provider_limit", "credential")
        self.write("provider-limit", {"all_required_passed": True, "limit": 5, "limit_remaining": 5,
                                      "limit_reset": None}, NOW - datetime.timedelta(seconds=30))
        result = self.evaluate()
        self.assertTrue(result["evidence"]["provider_limit"]["passed"])
        self.assertTrue(any("below the ledger" in w for w in result["evidence"]["provider_limit"]["warnings"]))
        self.assertBlocked(self.evaluate(now=NOW + datetime.timedelta(hours=73)), "provider_limit", "old")
        self.assertIn("paid launch re-verifies the key it loads", result["evidence"]["provider_limit"]["reason"])
        ledger = BudgetLedger(self.ledger)
        ledger.settle(ledger.reserve("pilot", "agent-001", 1), "0.25", "provider_reported_cost")
        self.write("provider-limit", {"all_required_passed": True, "limit": 20, "limit_remaining": 20,
                                      "limit_reset": None},
                   NOW - datetime.timedelta(seconds=20))
        # The live check at paid launch would refuse 20 > 19.75; the gate reports it first.
        self.assertBlocked(self.evaluate(), "provider_limit", "exceeds the remaining pilot allowance 19.75")
        self.write("provider-limit", {"all_required_passed": True, "limit": 20, "limit_remaining": "19.75",
                                      "limit_reset": None},
                   NOW - datetime.timedelta(seconds=10))
        self.assertTrue(self.evaluate()["evidence"]["provider_limit"]["passed"])
        for path in launch_gates.evidence_files(self.validation, "provider-limit"):
            path.unlink()
        self.assertBlocked(self.evaluate(), "provider_limit", "no provider-limit")

    def test_credential_presence_only(self):
        with mock.patch("research_swarm.secrets.credential_status",
                        return_value=dict(CREDENTIALS_OK, environment_variable_set=False)):
            self.assertBlocked(self.evaluate(), "credential_presence", "no OPENROUTER_API_KEY")
        with mock.patch("research_swarm.secrets.credential_status",
                        return_value=dict(CREDENTIALS_OK, env_file_present=True, env_file_regular=True,
                                          env_file_private=False)):
            self.assertBlocked(self.evaluate(), "credential_presence", "mode 600")

    def test_unlimited_key_requires_explicit_boolean_policy_and_approval_note(self):
        for value in (None, "true", 1, [], {}):
            with self.subTest(value=value):
                self.assertBlocked(self.evaluate(pilot_config(allow_unlimited_provider_key=value)),
                                   "approved_settings", "must be a boolean")
        for note in (None, "", "   ", True, 42):
            with self.subTest(note=note):
                self.assertBlocked(self.evaluate(pilot_config(
                    allow_unlimited_provider_key=True, unlimited_provider_key_approval=note)),
                    "approved_settings", "nonempty unlimited_provider_key_approval")

    def test_unlimited_provider_evidence_reassessed_and_waiver_explicit(self):
        from research_swarm.provider_limit import assess
        unlimited = {"limit": None, "limit_remaining": None, "limit_reset": None,
                     "usage": 0, "expires_at": None}
        report = assess(unlimited, Decimal("20"), NOW, allow_unlimited=True)
        self.write("provider-limit", report, NOW - datetime.timedelta(minutes=1))
        self.assertBlocked(self.evaluate(), "provider_limit", "waiver evidence")
        config = pilot_config(allow_unlimited_provider_key=True,
                              unlimited_provider_key_approval="Explicit user approval for this pilot")
        result = self.evaluate(config)
        self.assertTrue(result["ready"], result["blockers"])
        gate = result["evidence"]["provider_limit"]
        self.assertTrue(gate["unlimited_key_authorized"])
        self.assertFalse(gate["provider_limit_enforced"])
        self.assertIn("waived, not enforced", gate["reason"])
        self.assertTrue(any(check.get("waived") for check in gate["checks"].values()))
        # Even the authorized waiver cannot bless stale/tampered expiry metadata.
        report["expires_at"] = "2026-10-01T00:00:00Z"
        self.write("provider-limit", report, NOW - datetime.timedelta(seconds=30))
        self.assertBlocked(self.evaluate(config), "provider_limit", "key_not_expired")

    def test_passing_flag_cannot_hide_invalid_provider_metadata(self):
        self.write("provider-limit", {"all_required_passed": True, "limit": None,
                                      "limit_remaining": None, "limit_reset": None},
                   NOW - datetime.timedelta(minutes=1))
        self.assertBlocked(self.evaluate(), "provider_limit", "metadata requirements not met")

    def test_unlimited_policy_preserves_ledger_caps_and_holds(self):
        from research_swarm.provider_limit import assess
        config = pilot_config(allow_unlimited_provider_key=True,
                              unlimited_provider_key_approval="User approved unlimited provider key")
        self.write("provider-limit", assess({"limit": None, "limit_remaining": None, "limit_reset": None},
                                            Decimal("20"), NOW, allow_unlimited=True))
        ledger = BudgetLedger(self.ledger)
        ledger.reserve("pilot", "agent-001", "1")
        self.assertBlocked(self.evaluate(config), "ledger", "unresolved")


class ScriptTests(GateFixture):
    def test_provider_limit_assessment(self):
        assess = load_script("check_provider_limit").assess
        cap = Decimal("20")
        good = assess({"limit": 20, "limit_remaining": 20, "limit_reset": None, "usage": 0, "label": "sk-or-v1-x"}, cap, NOW)
        self.assertTrue(good["all_required_passed"])
        self.assertNotIn("label", good)
        self.assertNotIn("sk-", json.dumps(good))
        self.assertFalse(assess({"limit": None, "limit_remaining": None}, cap, NOW)["all_required_passed"])
        self.assertFalse(assess({"limit": 20, "limit_remaining": 20, "limit_reset": "monthly"}, cap, NOW)["all_required_passed"])
        self.assertFalse(assess({"limit": 25, "limit_remaining": 25}, cap, NOW)["all_required_passed"])
        used = assess({"limit": 22, "limit_remaining": 19.5, "usage": 2.5}, cap, NOW)
        self.assertTrue(used["all_required_passed"])
        self.assertEqual(len(used["warnings"]), 2)  # prior usage and limit above cap
        self.assertFalse(assess({"limit": 20, "limit_remaining": 20, "expires_at": "2026-10-01T00:00:00Z"}, cap, NOW)
                         ["all_required_passed"])

    def test_provider_limit_requires_approval_flag(self):
        module = load_script("check_provider_limit")
        with mock.patch("research_swarm.secrets.load_openrouter_key", side_effect=AssertionError("key read")), \
                mock.patch.object(sys, "argv", ["check_provider_limit.py"]), redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                module.main()
        self.assertEqual(caught.exception.code, 2)

    def test_preflight_spending_checkpoint(self):
        preflight = load_script("preflight")
        empty = preflight.spending_checkpoint(ROOT, self.ledger)
        self.assertEqual(list(empty["scopes"]), ["pilot", "analysis", "compatibility"])
        self.assertEqual(empty["scopes"]["pilot"]["remaining_usd"], "20")
        ledger = BudgetLedger(self.ledger)
        ledger.settle(ledger.reserve("pilot", "a", 1), 0.4, "provider_reported_cost")
        ledger.reserve("pilot", "b", 2)
        pilot = preflight.spending_checkpoint(ROOT, self.ledger)["scopes"]["pilot"]
        self.assertEqual((pilot["settled_usd"], pilot["held_usd"], pilot["remaining_usd"], pilot["unresolved_requests"]),
                         ("0.4", "2", "17.6", 1))
        text = "\n".join(preflight.render_spending(preflight.spending_checkpoint(ROOT, self.ledger)))
        self.assertIn("prior known costs $0.4000", text)
        self.assertIn("compatibility $2 (separate paid allowance for the live compatibility check", text)
        self.assertIn('"Live compatibility check"', text)
        self.assertNotIn("unpaid", text.lower())

    def test_preflight_raise_ledger_cap_is_explicit_and_audited(self):
        preflight = load_script("preflight")
        old = self.tmp / "old" / "budget.sqlite"
        ledger = BudgetLedger(old, {"pilot": 20, "analysis": 5, "compatibility": 0})
        ledger.settle(ledger.reserve("pilot", "a", 1), "0.7", "provider_reported_cost")
        ready = {"ready": True, "blockers": [], "launch_gates": {"evidence": {}}, "runtime_preflight": {},
                 "spending": preflight.spending_checkpoint(ROOT, self.ledger)}
        with mock.patch.object(preflight, "DEFAULT_LEDGER_PATH", lambda root: old), \
                mock.patch.object(preflight, "collect", return_value=ready) as collect, \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(preflight.main(["--raise-ledger-cap", "compatibility"]), 2)
            self.assertEqual(preflight.main(["--raise-ledger-cap", "compatibility", "--i-have-user-approval"]), 2)
            self.assertEqual(preflight.main(["--raise-ledger-cap", "compatibility", "--approval-note", "user ok"]), 2)
            self.assertEqual(read_snapshot(old)["compatibility"]["cap_usd"], "0")
            collect.assert_not_called()
            self.assertEqual(preflight.main(["--raise-ledger-cap", "compatibility", "--approval-note",
                                             "user approved $2 in chat", "--i-have-user-approval"]), 0)
            self.assertEqual(preflight.main(["--raise-ledger-cap", "compatibility", "--approval-note", "again",
                                             "--i-have-user-approval"]), 2)  # already at the approved cap
        self.assertIn("recorded spending is kept (committed: analysis $0.0000, compatibility $0.0000, pilot $0.7000)",
                      out.getvalue())
        snapshot = read_snapshot(old)
        self.assertEqual((snapshot["compatibility"]["cap_usd"], snapshot["pilot"]["committed_usd"]), ("2", "0.7"))
        self.assertEqual([c["note"] for c in cap_changes(old)], ["user approved $2 in chat"])
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            preflight.main(["--approval-note", "x"])
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            preflight.main(["--raise-ledger-cap", "pilotx"])

    def test_preflight_drops_duplicate_paid_launch_blocker(self):
        preflight = load_script("preflight")
        gates = {"ready": False, "blockers": ["paid_launch_enabled: paid_launch_enabled is not true (user decision)"],
                 "evidence": {}}
        runtime_report = {"ready": False, "blockers": ["Paid launch is disabled", "Docker CLI unavailable",
                                                       gates["blockers"][0]]}
        with mock.patch.object(preflight.launch_gates, "evaluate", return_value=gates), \
                mock.patch("research_swarm.runtime.preflight", return_value=runtime_report):
            report = preflight.collect({}, ROOT, self.ledger)
        self.assertEqual(report["blockers"], [gates["blockers"][0], "Docker CLI unavailable"])
        with mock.patch.object(preflight.launch_gates, "evaluate", return_value=dict(gates, blockers=[])), \
                mock.patch("research_swarm.runtime.preflight", return_value=runtime_report):
            self.assertIn("Paid launch is disabled", preflight.collect({}, ROOT, self.ledger)["blockers"])

    def test_run_pilot_requires_flag_config_gates_and_typed_confirmation(self):
        run_pilot = load_script("run_pilot")
        ready = {"ready": True, "blockers": [], "launch_gates": {"evidence": {}}, "runtime_preflight": {},
                 "spending": {"ledger": "x", "ledger_exists": False, "scopes": {"pilot": {
                     "cap_usd": "20", "settled_usd": "0", "held_usd": "0", "committed_usd": "0", "remaining_usd": "20",
                     "unresolved_requests": 0, "violation": False}}}}
        args = SimpleNamespace(launch_paid_pilot=False)
        self.assertFalse(run_pilot.launch_decision({"paid_launch_enabled": True}, ready, args)[0])
        args.launch_paid_pilot = True
        self.assertFalse(run_pilot.launch_decision({"paid_launch_enabled": False}, ready, args)[0])
        self.assertFalse(run_pilot.launch_decision({"paid_launch_enabled": True}, dict(ready, ready=False), args)[0])
        self.assertTrue(run_pilot.launch_decision({"paid_launch_enabled": True}, ready, args)[0])
        prompts = []
        self.assertTrue(run_pilot.confirm("20", lambda prompt: prompts.append(prompt) or "$20.00"))
        self.assertIn("$2 compatibility and $5 analysis allowances are separate", prompts[0])
        self.assertFalse(run_pilot.confirm("20", lambda prompt: "yes"))
        self.assertFalse(run_pilot.confirm("19.5", lambda prompt: "20"))
        config_path = self.tmp / "pilot.json"
        config_path.write_text(json.dumps({"paid_launch_enabled": True}))
        calls = []

        async def fake_run(config, repo):
            calls.append(config)
        with mock.patch.object(run_pilot, "collect", return_value=ready), redirect_stdout(io.StringIO()):
            self.assertEqual(run_pilot.main(["--config", str(config_path), "--launch-paid-pilot"],
                                            ask=lambda p: "19", run=fake_run), 2)
            self.assertEqual(calls, [])
            self.assertEqual(run_pilot.main(["--config", str(config_path)], ask=lambda p: "20", run=fake_run), 0)
            self.assertEqual(calls, [])  # preflight only by default
            self.assertEqual(run_pilot.main(["--config", str(config_path), "--launch-paid-pilot"],
                                            ask=lambda p: "20", run=fake_run), 0)
        self.assertEqual(len(calls), 1)
        self.assertIn(str(Path("runs") / "pilot-"), calls[0]["run_dir"])

    def test_run_pilot_reports_a_refused_provider_limit(self):
        """SPEND-6: runtime.run_pilot's live key check refuses before any paid request; the launcher exits 2."""
        from research_swarm.provider_limit import ProviderLimitError
        run_pilot = load_script("run_pilot")
        ready = {"ready": True, "blockers": [], "launch_gates": {"evidence": {}}, "runtime_preflight": {},
                 "spending": {"ledger": "x", "ledger_exists": False, "scopes": {"pilot": {
                     "cap_usd": "20", "settled_usd": "0", "held_usd": "0", "committed_usd": "0", "remaining_usd": "20",
                     "unresolved_requests": 0, "violation": False}}}}
        config_path = self.tmp / "pilot.json"
        config_path.write_text(json.dumps({"paid_launch_enabled": True}))

        async def refused(config, repo):
            raise ProviderLimitError("provider-side key limit refused (limit_set): limit None")
        with mock.patch.object(run_pilot, "collect", return_value=ready), \
                mock.patch.object(run_pilot, "spending_checkpoint", return_value=ready["spending"]), \
                redirect_stdout(io.StringIO()) as out:
            code = run_pilot.main(["--config", str(config_path), "--launch-paid-pilot", "--yes"], run=refused)
        self.assertEqual(code, 2)
        self.assertIn("No paid call was made", out.getvalue())
        self.assertIn("limit_set", out.getvalue())

    def test_reconcile_script(self):
        module = load_script("reconcile_ledger")
        ledger = BudgetLedger(self.ledger)
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
            self.assertEqual(module.main(["--ledger", str(self.ledger), "--run-dir", str(run_dir)]), 0)
        self.assertIn("Dry run", out.getvalue())

    def test_reconcile_judge_holds_with_expected_models_and_credentials_root(self):
        module = load_script("reconcile_ledger")
        ledger = BudgetLedger(self.ledger)
        opus, sonnet, pilot = (ledger.reserve(scope, agent, 1) for scope, agent in (
            ("analysis", "judge-opus:verifier:agent-008.verify-b01"),
            ("analysis", "judge-sonnet:judge_recheck:agent-020.judge.recheck.b01"), ("pilot", "agent-001")))
        for request in (opus, sonnet, pilot):
            ledger.settle(request, None)
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
                                "--expect-model", "anthropic/claude-opus-5.5", "--credentials-root", str(creds),
                                "--i-have-user-approval"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, [creds])
        self.assertIn('"reconciled"', out.getvalue())
        self.assertEqual(read_snapshot(self.ledger)["pilot"]["uncertain_requests"], 1)  # other scopes untouched
        audit = json.loads(sorted((self.ledger.parent / "reconciliations").glob("reconcile-*.json"))[-1].read_text())
        self.assertEqual((audit["expected_models"], audit["scope"], audit["credential_recorded"]),
                         (["anthropic/claude-opus-5.5"], "analysis", False))

    def test_reconcile_exception_path_hold_listing_and_manual_settlement(self):
        module = load_script("reconcile_ledger")
        ledger = BudgetLedger(self.ledger)
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

        code, text = run()
        listing = json.loads(text.split("\nDry run")[0])["unresolved"]
        entry = next(e for e in listing if e["request_id"] == hold)
        self.assertEqual((entry["generation_id"], entry["provider_request_id"], entry["agent_id"]),
                         (None, "req_abc123", "agent-007"))
        self.assertIn("--manual REQUEST_ID COST", entry["next_step"])
        self.assertIn("generation lookup", next(e for e in listing if e["request_id"] == other)["next_step"])
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            run("--manual", hold, "0.31")  # a note is required
        self.assertEqual(run("--manual", hold, "0.31", "--note", "activity log")[0], 0)  # dry run without approval
        self.assertEqual(run("--manual", "nope", "0.31", "--note", "x", "--i-have-user-approval")[0], 2)
        self.assertEqual(run("--manual", hold, "-1", "--note", "x", "--i-have-user-approval")[0], 2)
        self.assertEqual(run("--manual", hold, "0.31", "--manual", "nope", "0.1", "--note", "x",
                             "--i-have-user-approval")[0], 2)  # all-or-nothing
        self.assertEqual(read_snapshot(self.ledger)["pilot"]["uncertain_requests"], 2)
        code, text = run("--manual", hold, "0.31", "--note", "OpenRouter activity 12:40Z; user approved",
                         "--i-have-user-approval")
        self.assertEqual(code, 0)
        snapshot = read_snapshot(self.ledger)["pilot"]
        self.assertEqual((snapshot["uncertain_requests"], snapshot["manual_reconciliations"]), (1, 1))
        audit = BudgetLedger(self.ledger).audit_log()[-1]
        self.assertEqual((audit["request_id"], audit["basis"], audit["new_actual_usd"]),
                         (hold, "manual_reconciliation", "0.31"))
        self.assertIn("OpenRouter activity 12:40Z; user approved", audit["note"])
        files = list((self.ledger.parent / "reconciliations").glob("reconcile-*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text())["mode"], "manual")
        self.assertEqual(run("--manual", hold, "0.31", "--note", "again", "--i-have-user-approval")[0], 2)

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
