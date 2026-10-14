"""Offline regression tests for the Phase B runtime scripts (review fixes SPEND-3/F1,
ISO-1/RT-1, ISO-2, RT-2, RT-5 and the X1/X3 writer side). Fakes only: no Docker,
no network, no model API, no credential."""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    import inspect_ai  # noqa: F401
    HAVE_INSPECT = True
except ImportError:  # the scripts import Inspect-dependent helpers lazily, but the adapter needs it
    HAVE_INSPECT = False

if HAVE_INSPECT:
    import validate_runtime as vr
    import isolation_probes as iso
    import capacity_probe as cap
    from research_swarm import inspect_adapter, runtime

CONTAINER_ID = "a" * 64
LINUX_MOUNTINFO = f"""\
1290 1173 0:110 / / rw,relatime master:529 - overlay overlay rw,lowerdir=/var/lib/docker/overlay2/l/X,upperdir=/var/lib/docker/overlay2/y/diff,workdir=/var/lib/docker/overlay2/y/work
1291 1290 0:113 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw
1292 1290 0:114 / /dev rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755,inode64
1293 1292 0:115 / /dev/pts rw,nosuid,noexec,relatime - devpts devpts rw,gid=5,mode=620,ptmxmode=666
1294 1290 0:116 / /sys ro,nosuid,nodev,noexec,relatime - sysfs sysfs ro
1295 1294 0:30 / /sys/fs/cgroup ro,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw,nsdelegate,memory_recursiveprot
1296 1292 0:112 / /dev/mqueue rw,nosuid,nodev,noexec,relatime - mqueue mqueue rw
1297 1292 0:117 / /dev/shm rw,nosuid,nodev,noexec,relatime - tmpfs shm rw,size=65536k,inode64
1298 1290 8:1 /var/lib/docker/containers/{CONTAINER_ID}/resolv.conf /etc/resolv.conf rw,relatime - ext4 /dev/sda1 rw
1299 1290 8:1 /var/lib/docker/containers/{CONTAINER_ID}/hostname /etc/hostname rw,relatime - ext4 /dev/sda1 rw
1300 1290 8:1 /var/lib/docker/containers/{CONTAINER_ID}/hosts /etc/hosts rw,relatime - ext4 /dev/sda1 rw
1301 1290 8:1 /usr/bin/docker-init /sbin/docker-init ro,relatime - ext4 /dev/sda1 rw
1174 1291 0:113 /bus /proc/bus ro,nosuid,nodev,noexec,relatime - proc proc rw
1175 1291 0:113 /sys /proc/sys ro,nosuid,nodev,noexec,relatime - proc proc rw
1176 1291 0:113 /sysrq-trigger /proc/sysrq-trigger ro,nosuid,nodev,noexec,relatime - proc proc rw
1177 1291 0:118 / /proc/acpi ro,relatime - tmpfs tmpfs ro,inode64
1178 1291 0:114 /null /proc/kcore rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755,inode64
1179 1291 0:114 /null /proc/keys rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755,inode64
1180 1294 0:119 / /sys/firmware ro,relatime - tmpfs tmpfs ro,inode64
"""
# Docker Desktop (Mac) defaults: the per-container files live on the VM's disk.
DESKTOP_MOUNTINFO = f"""\
700 600 0:200 / / rw,relatime - overlay overlay rw,lowerdir=/var/lib/desktop-containerd/x
701 700 0:201 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw
702 700 0:202 / /dev rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755
703 702 0:203 / /dev/pts rw,nosuid,noexec,relatime - devpts devpts rw,gid=5,mode=620,ptmxmode=666
704 700 0:204 / /sys ro,nosuid,nodev,noexec,relatime - sysfs sysfs ro
705 704 0:28 / /sys/fs/cgroup ro,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw
706 702 0:205 / /dev/mqueue rw,nosuid,nodev,noexec,relatime - mqueue mqueue rw
707 702 0:206 / /dev/shm rw,nosuid,nodev,noexec,relatime - tmpfs shm rw,size=65536k
708 700 254:1 /docker/containers/{CONTAINER_ID}/resolv.conf /etc/resolv.conf rw,relatime - ext4 /dev/vda1 rw
709 700 254:1 /docker/containers/{CONTAINER_ID}/hostname /etc/hostname rw,relatime - ext4 /dev/vda1 rw
710 700 254:1 /docker/containers/{CONTAINER_ID}/hosts /etc/hosts rw,relatime - ext4 /dev/vda1 rw
711 701 0:202 /null /proc/timer_list rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755
"""
INJECTED = {
    "named_volume": "1400 1290 8:1 /var/lib/docker/volumes/shared/_data /workspace rw,relatime - ext4 /dev/sda1 rw",
    "linux_bind_repo": "1401 1290 8:1 /home/ubuntu/gdm-math-swarm/research /research rw,relatime - ext4 /dev/sda1 rw",
    "relocated_socket": "1402 1290 0:25 /docker.sock /tmp/d.sock rw,nosuid,nodev - tmpfs tmpfs rw,size=814k,mode=755",
    "xfs_bind": "1403 1290 253:0 /srv/private /data rw,relatime - xfs /dev/mapper/vg-root rw",
    "tmpfs_volume": "1404 1290 0:90 / /tmp/w rw,relatime - tmpfs tmpfs rw",
    "desktop_bind": "1405 1290 0:91 /Users/me/gdm-math-swarm /repo rw,relatime - fakeowner /run/host_mark/Users rw",
    "foreign_hosts": "1406 1290 8:1 /home/ubuntu/hosts /etc/hosts rw,relatime - ext4 /dev/sda1 rw",
    "spaced_bind": "1407 1290 8:1 /home/ubuntu/my\\040repo /mnt/my\\040repo rw,relatime - ext4 /dev/sda1 rw",
}
CLEAN_STATUS = {"CapInh": "0000000000000000", "CapPrm": "0000000000000000", "CapEff": "0000000000000000",
                "CapBnd": "0000000000000000", "CapAmb": "0000000000000000", "NoNewPrivs": "1", "Seccomp": "2"}


@unittest.skipUnless(HAVE_INSPECT, "inspect_ai is not installed")
class DotenvGuardTests(unittest.TestCase):
    """SPEND-3/F1 (X1): the unpaid scripts never let Inspect load a .env."""

    def test_guard_runs_before_eval_and_model_apis_are_disabled_during_it(self):
        order = []

        def guard():
            order.append(("guard", os.environ.get("INSPECT_DISABLE_MODEL_API")))
            return {"dotenv_disabled": True, "patched": ["x"], "credentials_in_environment": False}

        async def fake_eval(task, **kwargs):
            order.append(("eval", os.environ.get("INSPECT_DISABLE_MODEL_API")))
            return [types.SimpleNamespace(location="log.eval")]
        with mock.patch.dict(os.environ), mock.patch.object(inspect_adapter, "disable_inspect_dotenv", guard), \
                mock.patch("inspect_ai.eval_async", fake_eval), \
                mock.patch("inspect_ai.log.read_eval_log", lambda location, resolve_attachments: "LOG"):
            os.environ.pop("INSPECT_DISABLE_MODEL_API", None)
            self.assertEqual(asyncio.run(vr.run_eval("task", "model", agents=1, log_dir="/nonexistent")), "LOG")
            # Assertions name the variable only: a failure must never print the environment.
            self.assertFalse("INSPECT_DISABLE_MODEL_API" in os.environ, "not restored after the evaluation")
        self.assertEqual(order, [("guard", None), ("eval", "1")])
        self.assertTrue(vr.DOTENV_GUARD["dotenv_disabled"])

    def test_fails_closed_when_the_guard_does_not_confirm(self):
        called = []

        async def fake_eval(task, **kwargs):
            called.append(True)
        for answer in ({"dotenv_disabled": False}, None):
            with mock.patch.object(inspect_adapter, "disable_inspect_dotenv", lambda: answer), \
                    mock.patch("inspect_ai.eval_async", fake_eval):
                with self.assertRaises(RuntimeError):
                    asyncio.run(vr.run_eval("task", "model", agents=1, log_dir="/nonexistent"))
        self.assertEqual(called, [])

    def test_real_eval_does_not_load_a_dotenv_in_the_cwd(self):
        """A fake .env in the working directory (find_dotenv walks up from it) must not
        reach os.environ through run_eval (real eval_async, mockllm, no sandbox)."""
        from inspect_ai import Task
        from inspect_ai.dataset import Sample
        from inspect_ai.model import get_model
        from inspect_ai.solver import generate
        from inspect_ai.util._display import display_type, init_display_type
        previous_display = display_type()
        init_display_type("none")
        self.addCleanup(init_display_type, previous_display)
        name = "GDM_FAKE_DOTENV_PROBE_" + str(os.getpid())
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ):
            os.environ.pop(name, None)
            Path(folder, ".env").write_text(f"{name}=not-a-secret\n")
            os.chdir(folder)
            try:
                log = asyncio.run(vr.run_eval(Task(dataset=[Sample(input="hi")], solver=[generate()]),
                                              get_model("mockllm/model"), agents=1, log_dir=Path(folder) / "logs"))
            finally:
                os.chdir(cwd)
            self.assertEqual(log.status, "success")
            self.assertFalse(name in os.environ, "the fake .env entry reached os.environ")
            self.assertFalse("INSPECT_DISABLE_MODEL_API" in os.environ, "not restored after the evaluation")

    def test_evidence_measures_credential_presence_instead_of_asserting_it(self):
        checks = {"a": vr.check(True)}
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ):
            os.environ.pop("OPENROUTER_API_KEY", None)
            _, absent = vr.write_evidence("runtime-e2e", "s/1", checks, {}, "20261013T000000Z", folder)
            os.environ["OPENROUTER_API_KEY"] = "placeholder-for-presence-test"
            _, present = vr.write_evidence("runtime-e2e", "s/1", checks, {}, "20261013T000001Z", folder)
            text = Path(folder, "runtime-e2e-20261013T000001Z.json").read_text()
        self.assertNotIn("credentials_read", absent)
        self.assertEqual((absent["credentials_in_environment"], present["credentials_in_environment"]), (False, True))
        self.assertNotIn("placeholder-for-presence-test", text)  # presence only, never the value
        for key in ("credentials_in_environment_before_eval", "inspect_dotenv_disabled"):
            self.assertIn(key, absent)


@unittest.skipUnless(HAVE_INSPECT, "inspect_ai is not installed")
class ComposeBindingTests(unittest.TestCase):
    """X3 writer side: compose_sha256 is the file the agents ran, checked against the fingerprint."""

    def test_binding_matches_fingerprint_and_detects_a_changed_file(self):
        config = {"agent_memory": "1g", "agent_cpus": None}
        image = "sha256:" + "c" * 64
        with tempfile.TemporaryDirectory() as folder:
            compose = runtime.make_compose(folder, config, image)
            binding = vr.compose_binding(compose, config, image)
            self.assertTrue(binding["matches"])
            self.assertEqual(binding["compose_sha256"], runtime.compose_fingerprint(config, image))
            Path(compose).write_text(Path(compose).read_text() + "    volumes: [\"/srv:/srv\"]\n")
            self.assertFalse(vr.compose_binding(compose, config, image)["matches"])

    def test_scripts_default_to_the_pilot_config_compose_inputs(self):
        config = json.loads((ROOT / "configs" / "pilot.json").read_text())
        for module in (vr, iso):
            args = module.parse_args([])
            self.assertEqual((args.image, args.lean_image, args.agent_memory, args.agent_cpus),
                             (config["agent_image"], config["lean_image"], config["agent_memory"],
                              config["agent_cpus"]), module.__name__)
        self.assertTrue(vr.parse_args([]).background_lean_check)
        self.assertFalse(vr.parse_args(["--skip-background-lean-check"]).background_lean_check)
        with tempfile.TemporaryDirectory() as folder:
            other = Path(folder) / "c.json"
            other.write_text(json.dumps({"agent_memory": "2g", "agent_cpus": 1.5}))
            args = iso.parse_args(["--config", str(other)])
            self.assertEqual((args.agent_memory, args.agent_cpus), ("2g", 1.5))
            self.assertEqual(iso.probe_config(args)["agent_cpus"], 1.5)
            self.assertEqual(vr.validation_config(vr.parse_args(["--config", str(other)]))["agent_cpus"], 1.5)

    def test_all_three_writers_emit_compose_sha256(self):
        for script in ("validate_runtime.py", "isolation_probes.py", "capacity_probe.py"):
            text = (ROOT / "scripts" / script).read_text()
            self.assertRegex(text, r"\bcompose_sha256=", script)
            self.assertNotRegex(text, r"[\"']credentials_read[\"']\s*:\s*False", script)


@unittest.skipUnless(HAVE_INSPECT, "inspect_ai is not installed")
class MountAndSocketTests(unittest.TestCase):
    """ISO-1/RT-1: host bind mounts, volumes and relocated sockets fail on any platform."""

    def test_docker_defaults_are_accepted(self):
        for text in (LINUX_MOUNTINFO, DESKTOP_MOUNTINFO):
            mounts = iso.parse_mountinfo(text)
            self.assertGreater(len(mounts), 10)
            self.assertEqual(iso.unexpected_mounts(mounts), [])

    def test_binds_volumes_and_socket_mounts_are_flagged(self):
        for name, line in INJECTED.items():
            flagged = iso.unexpected_mounts(iso.parse_mountinfo(LINUX_MOUNTINFO + line + "\n"))
            self.assertEqual(len(flagged), 1, name)
            self.assertIn(flagged[0]["mount_point"], line.replace("\\040", " "), name)
        spaced = iso.parse_mountinfo(INJECTED["spaced_bind"])[0]
        self.assertEqual((spaced["root"], spaced["mount_point"]), ("/home/ubuntu/my repo", "/mnt/my repo"))
        rw_init = LINUX_MOUNTINFO.replace("/sbin/docker-init ro,relatime", "/sbin/docker-init rw,relatime")
        self.assertEqual([m["mount_point"] for m in iso.unexpected_mounts(iso.parse_mountinfo(rw_init))],
                         ["/sbin/docker-init"])

    def test_container_script_embeds_the_same_classifier(self):
        script = iso.filesystem_script()
        compile(script, "filesystem_probe", "exec")
        prelude = script.split("\nimport json, os, shutil, socket, stat")[0]
        namespace = {}
        exec(prelude, namespace)  # constants + parse_mountinfo + unexpected_mounts, as run in the container
        mounts = namespace["parse_mountinfo"](LINUX_MOUNTINFO + INJECTED["named_volume"] + "\n")
        self.assertEqual([m["mount_point"] for m in namespace["unexpected_mounts"](mounts)], ["/workspace"])
        self.assertIn('os.walk("/")', script)
        self.assertIn("/var/tmp/sandbox-services/", script)

    def test_socket_env_and_mount_problems(self):
        clean = {"sockets": {"/var/run/docker.sock": False}, "unexpected_sockets": [], "docker_env": {},
                 "docker_host_env": None, "mounts": iso.parse_mountinfo(LINUX_MOUNTINFO), "unexpected_mounts": []}
        self.assertEqual(iso.socket_and_mount_problems(clean), [])
        relocated = dict(clean, unexpected_sockets=[{"path": "/tmp/d.sock", "reply": "docker_api"}])
        self.assertTrue(any("/tmp/d.sock" in p for p in iso.socket_and_mount_problems(relocated)))
        env = dict(clean, docker_env={"DOCKER_HOST": ["/proc/42/environ"]})
        self.assertTrue(iso.socket_and_mount_problems(env))
        self.assertTrue(iso.socket_and_mount_problems({k: v for k, v in clean.items() if k != "unexpected_mounts"}))

    def test_scans_do_not_stop_at_mount_boundaries(self):
        for shell in (iso.image_scan_shell(["FormalConjectures/ErdosProblems"]), iso.marker_search_shell("m")):
            self.assertNotIn("-xdev", shell)
            for pruned in ("-path /proc", "-path /sys", "-path /dev"):
                self.assertIn(pruned, shell)
            subprocess.run(["sh", "-n", "-c", shell], check=True)  # syntax only; nothing is searched
        self.assertIn("/dev/shm", iso.marker_search_shell("m"))
        self.assertIn("/dev/shm/probe-marker", iso.marker_write_shell("m"))

    def test_shared_volume_now_fails_the_isolation_checks(self):
        """The review's offline reproduction: a shared /workspace volume, a /research
        bind mount and /tmp/d.sock previously passed every per-agent check."""
        with tempfile.TemporaryDirectory() as folder:
            compose = Path(runtime.write_compose(Path(folder) / "compose.yaml", "sha256:" + "d" * 64)).read_text()
            (Path(folder) / "instructions").mkdir()
            leaky = LINUX_MOUNTINFO + "\n".join(INJECTED[k] for k in ("named_volume", "linux_bind_repo",
                                                                      "relocated_socket")) + "\n"
            results = {}
            for label, text in (("clean", LINUX_MOUNTINFO), ("leaky", leaky)):
                mounts = iso.parse_mountinfo(text)
                sockets = [{"path": "/tmp/d.sock", "reply": "PermissionError"}] if label == "leaky" else []
                fs = {"uid": 1000, "groups": [1000], "sockets": {}, "socket_files": [s["path"] for s in sockets],
                      "unexpected_sockets": sockets, "docker_env": {}, "docker_host_env": None, "mounts": mounts,
                      "unexpected_mounts": iso.unexpected_mounts(mounts), "status": dict(CLEAN_STATUS),
                      "process_status": {"1": dict(CLEAN_STATUS, Name="docker-init")},
                      "cgroup": {"version": 2, "memory_max": "1073741824", "memory_swap_max": "0", "pids_max": "256",
                                 "cpu_max": "max 100000"},
                      "writes": {"/probe-write": "PermissionError: denied"},
                      "lean_wrappers": {t: {"path": p, "uid": 0, "mode": "0o755", "writable": False, "resolves_to": p}
                                        for t, p in vr.LEAN_WRAPPERS.items()}}
                probe = {"filesystem": fs, "network": {}, "secrets": {}, "image_scan": {"content": [], "names": []},
                         "cross_agent": {"other_marker_hits": [], "own_marker_hits": ["/tmp/probe-marker"]}}
                result = {"probes": {"agent-001": probe, "agent-002": probe}, "log": types.SimpleNamespace(samples=[]),
                          "scripted": vr.ScriptedModel({}), "run_dir": Path(folder), "runtime_events": [],
                          "judge": {"flags": {"network_none": True}, "runtime_probe": {"ran": False}},
                          "compose": {"matches": True}, "compose_text": compose}
                checks, _, _ = iso.build_checks(result)
                results[label] = checks
        for agent in ("agent-001", "agent-002"):
            self.assertTrue(results["clean"][f"{agent}:no_docker_socket_or_host_mounts"]["passed"])
            self.assertTrue(results["clean"][f"{agent}:no_privileges"]["passed"])
            self.assertTrue(results["clean"][f"{agent}:cgroup_limits_match_compose"]["passed"])
            leaky_check = results["leaky"][f"{agent}:no_docker_socket_or_host_mounts"]
            self.assertFalse(leaky_check["passed"])
            self.assertEqual(sorted(m["mount_point"] for m in leaky_check["unexpected_mounts"]),
                             ["/research", "/tmp/d.sock", "/workspace"])
        self.assertFalse(results["clean"]["judge_container_network_none"]["passed"])  # probe did not run (ISO-2)


@unittest.skipUnless(HAVE_INSPECT, "inspect_ai is not installed")
class PrivilegeAndCgroupTests(unittest.TestCase):
    """ISO-2: CapBnd/seccomp and the compose resource limits are asserted."""

    def fs(self, **overrides):
        base = {"uid": 1000, "status": dict(CLEAN_STATUS),
                "process_status": {"1": dict(CLEAN_STATUS, Name="docker-init"), "57": dict(CLEAN_STATUS, Name="claude")}}
        base.update(overrides)
        return base

    def test_privileges(self):
        self.assertEqual(iso.privilege_problems(self.fs()), [])
        default_caps = dict(CLEAN_STATUS, CapBnd="00000000a80425fb")  # Docker's default set: cap_drop removed
        self.assertTrue(iso.privilege_problems(self.fs(status=default_caps)))
        self.assertTrue(iso.privilege_problems(self.fs(status=dict(CLEAN_STATUS, Seccomp="0"))))
        self.assertTrue(iso.privilege_problems(self.fs(status=dict(CLEAN_STATUS, NoNewPrivs="0"))))
        self.assertTrue(iso.privilege_problems(self.fs(uid=0)))
        cli = {"57": dict(CLEAN_STATUS, Name="claude", CapBnd="00000000a80425fb")}
        self.assertTrue(any("claude" in p for p in iso.privilege_problems(self.fs(process_status=cli))))
        self.assertTrue(iso.privilege_problems(self.fs(process_status={})))
        missing = {k: v for k, v in CLEAN_STATUS.items() if k != "CapBnd"}
        self.assertTrue(iso.privilege_problems(self.fs(status=missing)))

    def test_cgroup_limits_follow_the_compose_file(self):
        with tempfile.TemporaryDirectory() as folder:
            plain = Path(runtime.write_compose(Path(folder) / "a.yaml", "sha256:" + "e" * 64, "1g")).read_text()
            capped = Path(runtime.write_compose(Path(folder) / "b.yaml", "sha256:" + "e" * 64, "1.5g", 1.5)).read_text()
        expected = iso.compose_limits(plain)
        self.assertEqual(expected, {"memory_max": 1073741824, "swap_max": 0, "pids_max": "256", "cpus": None})
        v2 = {"version": 2, "memory_max": "1073741824", "memory_swap_max": "0", "pids_max": "256", "cpu_max": "max 100000"}
        self.assertEqual(iso.cgroup_problems(v2, expected), [])
        for key, value in (("memory_max", "max"), ("memory_swap_max", "max"), ("pids_max", "max"),
                           ("cpu_max", "200000 100000")):
            self.assertTrue(iso.cgroup_problems(dict(v2, **{key: value}), expected), key)
        self.assertTrue(iso.cgroup_problems(None, expected))
        v1 = {"version": 1, "memory_max": "1073741824", "memory_memsw_max": "1073741824", "pids_max": "256",
              "cpu_max": "max 100000"}
        self.assertEqual(iso.cgroup_problems(v1, expected), [])
        self.assertTrue(iso.cgroup_problems(dict(v1, memory_memsw_max=None), expected))  # unverifiable swap fails
        capped_expected = iso.compose_limits(capped)
        self.assertEqual(capped_expected["memory_max"], 1610612736)
        self.assertEqual(iso.cgroup_problems(dict(v2, memory_max="1610612736", cpu_max="150000 100000"),
                                             capped_expected), [])
        self.assertTrue(iso.compose_limits("services: {}\n")["memory_max"] is None)
        self.assertTrue(iso.cgroup_problems(v2, iso.compose_limits("")))

    def test_wrapper_problems(self):
        good = {t: {"path": p, "uid": 0, "writable": False, "resolves_to": p} for t, p in vr.LEAN_WRAPPERS.items()}
        self.assertEqual(iso.wrapper_problems({"lean_wrappers": good}), [])
        self.assertTrue(iso.wrapper_problems({"lean_wrappers": dict(good, lake={"error": "FileNotFoundError"})}))
        self.assertTrue(iso.wrapper_problems({"lean_wrappers": dict(good, lean=dict(good["lean"], writable=True))}))
        self.assertTrue(iso.wrapper_problems(
            {"lean_wrappers": dict(good, lake=dict(good["lake"], resolves_to="/opt/elan/bin/lake"))}))


def _status_text(a="rc=137 seconds=120", b="rc=137 seconds=121", notices=(1, 1), survivors=0,
                 lake="/usr/local/bin/lake", lean="/usr/local/bin/lean"):
    return "\n".join([f"SLOW_a status={a} notice={notices[0]}", f"SLOW_b status={b} notice={notices[1]}",
                      f"LEAN_PROCESSES {survivors}", f"WHICH_LAKE {lake}", f"WHICH_LEAN {lean}",
                      "LAKE_ENV_LEAN /opt/elan/toolchains/leanprover--lean4---v4.27.0/bin/lean"])


@unittest.skipUnless(HAVE_INSPECT, "inspect_ai is not installed")
class LocalCompileLimitTests(unittest.TestCase):
    """RT-2: backgrounded compiles must be killed at about 120 s by the wrappers."""

    def test_scenario_steps(self):
        with_bg = [name for name, _ in vr.scenario_scripts("/tmp/run", background_lean=True)["agent-002"]]
        self.assertEqual(with_bg[:5], ["bash_background_lean_task", "bash_background_lean_detached", "bash_time_limit",
                                       "bash_timed_out_process_check", "bash_background_lean_status"])
        without = [name for name, _ in vr.scenario_scripts("/tmp/run")["agent-002"]]
        self.assertEqual(without[:2], ["bash_time_limit", "bash_timed_out_process_check"])
        self.assertFalse([n for n in without if "background" in n])
        steps = dict(vr.scenario_scripts("/tmp/run", background_lean=True)["agent-002"])
        args = {n: steps[n]([]).message.tool_calls[0].arguments for n in steps if n.startswith("bash_")}
        self.assertIs(args["bash_background_lean_task"].get("run_in_background"), True)
        self.assertIn("setsid nohup sh -c", args["bash_background_lean_detached"]["command"])
        self.assertIn("lake env lean SlowCheckB.lean", args["bash_background_lean_detached"]["command"])
        self.assertLessEqual(args["bash_background_lean_status"]["timeout"], 120000)
        self.assertIn("seq 1 100", args["bash_background_lean_status"]["command"])  # waits < 120 s
        for name, value in args.items():
            subprocess.run(["sh", "-n", "-c", value["command"]], check=True)
        own = args["bash_background_lean_status"]["command"]
        self.assertNotIn("SlowCheck", own.replace("[S]lowCheck", ""))  # its own shell is not counted

    def test_background_check_outcomes(self):
        check = vr.background_lean_check
        self.assertTrue(check(_status_text(), ran=True)["passed"])
        ended = check(_status_text(a="missing", notices=("", 1)), ran=True)
        self.assertTrue(ended["passed"])
        self.assertEqual(ended["run_in_background_outcome"], "ended_without_status")
        failing = {
            "detached_ran_to_completion": _status_text(b="rc=0 seconds=600", notices=(1, 0)),
            "oom_not_limit": _status_text(b="rc=137 seconds=40", notices=(1, 0)),
            "no_notice": _status_text(notices=(1, 0)),
            "too_late": _status_text(b="rc=137 seconds=300"),
            "detached_missing": _status_text(b="missing", notices=(1, "")),
            "task_not_killed": _status_text(a="rc=0 seconds=600", notices=(0, 1)),
            "survivor": _status_text(survivors=1),
            "no_wrapper_on_path": _status_text(lake="/opt/elan/bin/lake"),
            "empty": ""}
        for name, text_value in failing.items():
            self.assertFalse(check(text_value, ran=True)["passed"], name)
        skipped = check("", ran=False)
        self.assertEqual((skipped["passed"], skipped["required"], skipped["skipped"]), (False, False, True))

    def test_sleep_survivor_marker_and_kill_classification(self):
        self.assertEqual(vr.marker_int("x\nSLEEP_PROCESSES 0\n", "SLEEP_PROCESSES"), 0)
        self.assertEqual(vr.marker_int("SLEEP_PROCESSES 2", "SLEEP_PROCESSES"), 2)
        self.assertIsNone(vr.marker_int("Command timed out", "SLEEP_PROCESSES"))
        self.assertEqual(cap.local_status_key(137, 1), "timeout_local_limit")
        self.assertEqual(cap.local_status_key(137, 0), "killed_137")
        self.assertEqual(cap.local_status_key(1), "ok_or_lean_error")
        match = cap.LOCAL_MARK.search("CAP_LOCAL rc=137 ms=120004 task=t limit_notice=1")
        self.assertEqual((match.group(1), match.group(4)), ("137", "1"))
        command = cap.peer_scripts(["agent-001"], ["t"], {"at": None}, 1)["agent-001"][0][1]([])
        self.assertIn("limit_notice=$n", command.message.tool_calls[0].arguments["command"])

    def test_dockerfile_installs_wrappers_first_on_path(self):
        text = (ROOT / "docker" / "agent.Dockerfile").read_text()
        path = re.search(r"^ENV PATH=(\S+)", text, re.M).group(1).split(":")
        self.assertEqual(path[:2], ["/usr/local/bin", "/opt/elan/bin"])
        self.assertIn("ARG LOCAL_LEAN_TIMEOUT_SECONDS=120", text)
        template = self.wrapper_template(text)
        self.assertEqual(template.count("%s"), 3)
        self.assertIn("-v -s KILL", template)
        self.assertNotIn("--foreground", template)  # the whole process group (lake and its lean) must be killed
        for tool, wrapper in vr.LEAN_WRAPPERS.items():
            self.assertEqual(wrapper, f"/usr/local/bin/{tool}")
        self.assertIn("chown root:root", text)

    @staticmethod
    def wrapper_template(dockerfile_text):
        return re.search(r"printf '(#!/bin/sh[^']*)'", dockerfile_text).group(1)

    @unittest.skipUnless(shutil.which("timeout") and shutil.which("setsid") and sys.platform.startswith("linux"),
                         "needs GNU timeout and setsid (Linux)")
    def test_wrapper_kills_a_detached_lake_env_lean(self):
        """The Dockerfile's wrapper text, with a fake elan whose `lake env` runs the
        toolchain `lean` as a child (as Lake does) and a `lean` that sleeps."""
        template = self.wrapper_template((ROOT / "docker" / "agent.Dockerfile").read_text())
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            wrappers, elan, toolchain, work = (base / d for d in ("wrappers", "elan", "toolchain", "w"))
            for directory in (wrappers, elan, toolchain, work):
                directory.mkdir()
            (elan / "lake").write_text(f'#!/bin/sh\n[ "$1" = env ] && shift\nPATH="{toolchain}:$PATH" "$@"\n')
            (toolchain / "lean").write_text("#!/bin/sh\nsleep 30\n")
            for tool in ("lake", "lean"):
                body = template.replace("\\n", "\n") % (shutil.which("timeout"), 2, tool)
                (wrappers / tool).write_text(body.replace("/opt/elan/bin", str(elan)))
            for script in (*wrappers.iterdir(), elan / "lake", toolchain / "lean"):
                script.chmod(0o755)
            compile_cmd = vr.slow_compile_command("b").replace("/tmp/w/", f"{work}/")
            env = dict(os.environ, PATH=f"{wrappers}:{os.environ['PATH']}")
            start = time.monotonic()
            subprocess.run(["sh", "-c", f"cd {work} && setsid nohup sh -c '{compile_cmd}' > /dev/null 2>&1 "
                                        "< /dev/null & echo started"], env=env, check=True, timeout=10)
            status = work / "slow-b.status"
            while not status.exists() and time.monotonic() - start < 20:
                time.sleep(0.2)
            text_value = status.read_text()
            self.assertRegex(text_value, r"^rc=137 seconds=[23]\b")
            self.assertIn(vr.KILL_NOTICE, (work / "slow-b.out").read_text())
            time.sleep(0.5)
            survivors = subprocess.run(["ps", "-ww", "-eo", "args"], capture_output=True, text=True).stdout
            self.assertNotIn(f"{toolchain}/lean", survivors)
            self.assertNotIn("sleep 30", survivors)


@unittest.skipUnless(HAVE_INSPECT, "inspect_ai is not installed")
class CapacityStartupTests(unittest.TestCase):
    """RT-5: the capacity probe uses the pilot's startup watchdog and measures startup."""

    def test_startup_timeout_defaults_to_the_config(self):
        config = json.loads((ROOT / "configs" / "pilot.json").read_text())
        args = cap.parse_args([])
        self.assertEqual(args.startup_timeout, config["startup_timeout_seconds"])
        self.assertEqual(args.config_startup_timeout, config["startup_timeout_seconds"])
        self.assertEqual(cap.parse_args(["--startup-timeout", "900"]).config_startup_timeout,
                         config["startup_timeout_seconds"])

    def test_capacity_uses_config_limits_and_records_overrides(self):
        config = {"agent_count": 20, "max_subprocesses": 23, "max_connections": 20,
                  "startup_timeout_seconds": 321, "warm_up_memory": "8g", "warm_up_timeout_seconds": 777}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pilot.json"
            path.write_text(json.dumps(config))
            args = cap.parse_args(["--config", str(path)])
            self.assertEqual((args.max_subprocesses, args.max_connections, args.startup_timeout,
                              args.warm_up_memory, args.warm_up_timeout), (23, 20, 321, "8g", 777))
            measured = cap.validated_config(args)
            for key, value in config.items():
                self.assertEqual(measured[key], value)
            self.assertEqual(runtime.warm_up_options(cap.probe_config(args)), {"memory": "8g", "timeout": 777})
            self.assertEqual(runtime.eval_options(20, folder, cap.probe_config(args))["max_subprocesses"], 23)
            args = cap.parse_args(["--config", str(path), "--max-subprocesses", "19", "--max-connections", "7",
                                   "--startup-timeout", "300", "--warm-up-memory", "7g", "--warm-up-timeout", "555"])
            measured = cap.validated_config(args)
            self.assertEqual((measured["max_subprocesses"], measured["max_connections"],
                              measured["startup_timeout_seconds"], measured["warm_up_memory"],
                              measured["warm_up_timeout_seconds"]), (19, 7, 300, "7g", 555))
            self.assertEqual(runtime.warm_up_options(cap.probe_config(args)), {"memory": "7g", "timeout": 555})

    def events(self, ready_offsets, start=1000.0, barrier=None, stop=None):
        out = [{"kind": "agent_ready", "agent_id": f"agent-{i + 1:03d}", "timestamp": start + t, "payload": {}}
               for i, t in enumerate(ready_offsets)]
        if barrier is not None:
            out.append({"kind": "common_start", "agent_id": None, "timestamp": start + barrier, "payload": {}})
        if stop:
            out.append({"kind": "run_stop", "agent_id": None, "timestamp": start + 1, "payload": {"reason": stop}})
        return out

    def test_startup_measurements(self):
        ok = cap.startup_measurements(self.events([100, 250, 400], barrier=400), 1000.0, 3, 600, 600)
        self.assertTrue(ok["within_timeout"])
        self.assertEqual((ok["barrier_seconds"], ok["slowest_agent_seconds"]), (400, 400))
        self.assertEqual(ok["per_agent_seconds"]["agent-002"], 250)
        self.assertEqual(ok["fraction_of_timeout"], round(400 / 600, 3))
        # 700 s passed a 900 s probe watchdog before; the pilot would have aborted at 600 s.
        self.assertFalse(cap.startup_measurements(self.events([100, 700], barrier=700), 1000.0, 2, 900, 600)
                         ["within_timeout"])
        self.assertFalse(cap.startup_measurements(self.events([100, 650], barrier=650), 1000.0, 2, 600, 600)
                         ["within_timeout"])
        self.assertFalse(cap.startup_measurements(self.events([100], stop="startup_timeout"), 1000.0, 2, 600, 600)
                         ["within_timeout"])  # an agent never arrived
        self.assertFalse(cap.startup_measurements(self.events([100, 200], barrier=200), None, 2, 600, 600)
                         ["within_timeout"])


if __name__ == "__main__":
    unittest.main()
