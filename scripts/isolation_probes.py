#!/usr/bin/env python3
"""Isolation probes for the experimental agent runtime (Phase B; unpaid).

Two genuine Claude Code peers run through the production compose file, Inspect
sandbox, solver (runtime.peer_solver), bridge filter and conference tools, with
scripted mockllm outputs. While each CLI is alive (held in a Bash wait loop),
a side task probes its container directly through Inspect's sandbox exec as the
agent uid (1000), without transcript events:

* egress: interfaces, public IPs, DNS, 169.254.169.254, openrouter.ai,
  host.docker.internal, the Docker host gateway; listening loopback ports;
* no daemon socket anywhere: the standard paths, any socket outside the bridge's
  /var/tmp/sandbox-services found by a walk of the whole filesystem (mounts
  included, only /proc and /sys skipped; each one is also asked for the Docker
  API's /_ping), and no DOCKER_HOST/DOCKER_CONTEXT/... in any process environment;
* /proc/self/mountinfo contains only Docker's per-container defaults, so a host
  bind mount or a named/anonymous volume fails on Linux and Docker Desktop alike;
* environment and every readable /proc/*/environ plus private files free of
  provider keys (only Inspect SWE's public bridge placeholder); values are never
  recorded, only pattern names, lengths and short hashes;
* no repository, researcher, manifest, plan or upstream problem-tree content in
  the image (distinctive strings and paths);
* /opt/lean-project, /opt/elan and system paths not writable; the lake/lean
  time-limit wrappers first on PATH, root-owned and read-only; setuid/setgid
  files listed;
* privileges: uid 1000 and, for the probe and every visible process, all
  capability sets empty (CapBnd included), NoNewPrivs=1, Seccomp=2;
* cgroup pids.max, memory.max, swap and cpu.max equal the compose settings;
* sandbox_file traversal, /proc and symlink escapes rejected (host-side tool
  calls and bridged calls from the CLI);
* another agent's container unreachable (marker files in /workspace, /tmp and
  /dev/shm, searched without -xdev, with a positive control);
* WebSearch/WebFetch/Agent/Task absent from the tool list and rejected;
* nested ``claude -p`` from Bash: whether it reaches the bridge, the budget filter
  and Inspect logging (documented outcome, not a launch gate);
* the operational judge container runs with --network none (command check plus
  one probe container, which must actually run).

The compose file the agents ran is bound to the evidence (``compose_sha256``, X3);
agent memory/CPUs and image tags default to the pilot config (``--config``).
Inspect's .env loading is disabled before the evaluation (validate_runtime.run_eval,
X1); the evidence records OPENROUTER_API_KEY presence in the process environment.

As in run_pilot, the image tags are pinned to local image IDs (runtime.pin_images),
so the probed containers and the judge probe run exactly the recorded images.

Nothing starts while the launch interlock (runtime.launch_interlock) finds a running
comparator container, a leftover judge container or another run's agent sandbox;
``--force-while-live`` (debugging only) proceeds, records the override and fails
the required check ``no_conflicting_containers_at_start``.

Writes data/validation/runtime-isolation-<UTC>.json (I6). Researcher artifacts go
to runs/validation-isolation-<UTC>/ (Git-ignored).

    PYTHONPATH=src .venv/bin/python scripts/isolation_probes.py
"""
from __future__ import annotations
import argparse
import asyncio
import inspect as pyinspect
import json
import re
import subprocess
import sys
import uuid
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_runtime as vr  # noqa: E402  (shared helpers; also sets INSPECT_DISPLAY)
from validate_runtime import ROOT, check, conf_call, bash, text, tool_call  # noqa: E402
from research_swarm import runtime  # noqa: E402
from research_swarm.inspect_adapter import (DISALLOWED_TOOLS, InstructionArchive, budget_filter,  # noqa: E402
                                            conference_tools)
from research_swarm.provider import ProviderSettings  # noqa: E402

AGENTS = ("agent-001", "agent-002")
PERSONAS = {"agent-001": "analysis_topology", "agent-002": "combinatorics"}
TEMPLATE = "all_fermat_squarefree"
RELEASE_FILE = "/tmp/probe-release"
# Inspect SWE's public placeholder credential for the in-container bridge
# (inspect_swe/_claude_code/env.py); any other key-shaped value is a finding.
BRIDGE_PLACEHOLDER = "sk-ant-api03-DOq5tyLPrk9M4hPE"
# Researcher-only strings that must never be inside an agent image/container.
DISTINCTIVE_STRINGS = ("2609.04170", "Emergent Cheating", "research/fixtures", "parent_manifest_sha256",
                       "frozen_identity_manifest", "original_statement", "OPENROUTER_API_KEY", "docs/interfaces.md",
                       "gdm-math-swarm")
SCAN_PRUNE = ("/proc", "/sys", "/dev", "/opt/lean-project/.lake", "/opt/elan", "/usr/lib/node_modules",
              "/workspace/problems")

NETWORK_PY = r"""
import json, os, socket, urllib.request
out = {"interfaces": sorted(os.listdir('/sys/class/net')) if os.path.isdir('/sys/class/net') else None}
def tcp(host, port):
    try:
        s = socket.create_connection((host, port), timeout=3); s.close(); return "connected"
    except Exception as e:
        return type(e).__name__ + ": " + str(e)[:120]
out["tcp"] = {f"{h}:{p}": tcp(h, p) for h, p in [("1.1.1.1", 443), ("8.8.8.8", 53), ("169.254.169.254", 80),
              ("172.17.0.1", 2375), ("192.168.65.254", 80), ("host.docker.internal", 80), ("openrouter.ai", 443)]}
def resolve(name):
    try:
        return "resolved: " + str(sorted({a[4][0] for a in socket.getaddrinfo(name, 443)}))
    except Exception as e:
        return type(e).__name__ + ": " + str(e)[:120]
out["dns"] = {n: resolve(n) for n in ("openrouter.ai", "host.docker.internal", "gateway.docker.internal", "example.com")}
try:
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.settimeout(3)
    u.sendto(b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01", ("8.8.8.8", 53))
    out["udp_dns"] = "reply" if u.recvfrom(512) else "no reply"
except Exception as e:
    out["udp_dns"] = type(e).__name__ + ": " + str(e)[:120]
try:
    urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=5); out["https_openrouter"] = "reachable"
except Exception as e:
    out["https_openrouter"] = type(e).__name__ + ": " + str(e)[:120]
listening = []
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        for line in open(table).read().splitlines()[1:]:
            f = line.split()
            if f[3] == "0A":
                listening.append(f[1])
    except OSError:
        pass
out["listening"] = listening
out["resolv_conf"] = open('/etc/resolv.conf').read()[:400] if os.path.exists('/etc/resolv.conf') else None
print(json.dumps(out))
"""

SECRETS_PY = r"""
import hashlib, json, os, re, sys
placeholder = sys.argv[1].encode()
patterns = {"openrouter_key": re.compile(rb"sk-or-[A-Za-z0-9_-]{16,}"),
            "anthropic_key": re.compile(rb"sk-ant-[A-Za-z0-9_-]{16,}"),
            "openai_key": re.compile(rb"sk-(?:proj-)?[A-Za-z0-9]{32,}")}
sensitive_name = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|OPENROUTER)", re.I)
hits, names, sources = [], {}, 0
def scan(source, data):
    for kind, pattern in patterns.items():
        for m in pattern.finditer(data):
            value = m.group(0)
            if value == placeholder:
                continue
            hits.append({"source": source, "pattern": kind, "length": len(value),
                         "sha256_12": hashlib.sha256(value).hexdigest()[:12]})
def scan_environ(source, data):
    for item in data.split(b"\0"):
        if b"=" in item:
            name, value = item.split(b"=", 1)
            name = name.decode(errors="replace")
            if sensitive_name.search(name):
                names.setdefault(name, set()).add("bridge_placeholder" if value == placeholder else
                                                  ("empty" if not value else "other_value_len_%d" % len(value)))
    scan(source, data)
scan_environ("own_environ", b"\0".join(k.encode() + b"=" + v.encode() for k, v in os.environ.items()))
procs = []
for pid in os.listdir("/proc"):
    if pid.isdigit():
        try:
            data = open(f"/proc/{pid}/environ", "rb").read()
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ")[:120].decode(errors="replace")
        except OSError:
            continue
        procs.append(pid); scan_environ(f"/proc/{pid}/environ ({cmd})", data)
for root in ("/home/researcher", "/workspace", "/tmp", "/var/tmp", "/etc"):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".lake", "problems")]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                if os.path.islink(path) or os.path.getsize(path) > 5_000_000:
                    continue
                scan(path, open(path, "rb").read()); sources += 1
            except OSError:
                continue
print(json.dumps({"hits": hits, "sensitive_env_names": {k: sorted(v) for k, v in names.items()},
                  "environ_files_read": len(procs), "files_scanned": sources}))
"""

# Docker's per-container files bind-mounted from the daemon's container directory.
DOCKER_CONTAINER_FILE = r"(^|/)containers/[0-9a-f]{64}/(hostname|hosts|resolv\.conf)$"
# Sockets the Inspect sandbox bridge may create (inspect_ai util/_sandbox/service.py
# SERVICES_DIR); any other socket, anywhere, fails the isolation check.
ALLOWED_SOCKET_PREFIXES = ("/var/tmp/sandbox-services/",)
# Environment variables that point a client at a container daemon.
DOCKER_ENV_NAMES = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "CONTAINER_HOST",
                    "CONTAINERD_ADDRESS")


def parse_mountinfo(text):
    """/proc/self/mountinfo lines as dicts (octal escapes such as \\040 decoded)."""
    import re
    unescape = lambda value: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)  # noqa: E731
    mounts = []
    for line in text.splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        fields, rfields = left.split(), right.split()
        if len(fields) < 6 or not rfields:
            continue
        mounts.append({"mount_point": unescape(fields[4]), "root": unescape(fields[3]), "options": fields[5],
                       "fstype": rfields[0], "source": unescape(rfields[1]) if len(rfields) > 1 else ""})
    return mounts


def unexpected_mounts(mounts, container_file=DOCKER_CONTAINER_FILE):
    """Mounts that are not Docker's per-container defaults, with a reason.

    Allowed: the root filesystem; proc/sysfs/cgroup under /proc and /sys (including
    Docker's read-only proc sub-mounts); Docker's masked paths there (a read-only
    tmpfs, or /dev/null bound from the container's own /dev: root "/null"); the
    container's own /dev tmpfs, devpts, mqueue and /dev/shm; /etc/hostname,
    /etc/hosts and /etc/resolv.conf from the daemon's container directory; the
    read-only docker-init binary (compose ``init: true``). Anything else, i.e. a
    host bind mount, a named or anonymous volume, a tmpfs or socket mount, fails
    whatever its filesystem type (Linux ext4/xfs/btrfs or Docker Desktop
    virtiofs/grpcfuse/9p alike).
    """
    import re
    flagged = []
    for entry in mounts:
        point, root, fstype = entry["mount_point"], entry["root"], entry["fstype"]
        options = entry.get("options", "").split(",")
        masked = fstype in ("tmpfs", "devtmpfs") and root in ("/", "/null")
        if point == "/":
            continue
        if point == "/proc" and fstype == "proc" or point.startswith("/proc/") and (fstype == "proc" or masked):
            continue
        if point == "/sys" and fstype == "sysfs" or point.startswith("/sys/") and (
                fstype in ("sysfs", "cgroup", "cgroup2") or masked):
            continue
        if (point, fstype) in (("/dev", "tmpfs"), ("/dev/shm", "tmpfs")) and root == "/" or \
                (point, fstype) in (("/dev/pts", "devpts"), ("/dev/console", "devpts"), ("/dev/mqueue", "mqueue")):
            continue
        if point in ("/etc/hostname", "/etc/hosts", "/etc/resolv.conf") and re.search(container_file, root):
            continue
        if point == "/sbin/docker-init" and root.rsplit("/", 1)[-1] == "docker-init" and "ro" in options:
            continue
        reason = ("host path or volume (root %r on %s)" % (root, fstype) if root != "/"
                  else "unexpected %s mount" % fstype)
        flagged.append(dict(entry, reason=reason))
    return flagged


FILESYSTEM_BODY = r"""
import json, os, shutil, socket, stat
out = {"uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups()}
out["sockets"] = {p: os.path.exists(p) for p in ("/var/run/docker.sock", "/run/docker.sock",
                  "/run/containerd/containerd.sock", "/var/run/containerd/containerd.sock")}
def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None
def ping(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(path)
        s.sendall(b"GET /_ping HTTP/1.0\r\nHost: docker\r\n\r\n")
        data = s.recv(512)
        if b"Api-Version" in data or b"Docker" in data:
            return "docker_api"
        return "http_reply" if data.startswith(b"HTTP/") else ("other_reply" if data else "no_reply")
    except Exception as e:
        return type(e).__name__
    finally:
        s.close()
# Daemon-pointing variables in this process and every readable process environment
# (names and sources only), plus every process's privilege fields; read first, while
# the held CLI is certainly alive.
docker_env, procs = {}, {}
def env_names(source, data):
    for item in data.split(b"\0"):
        name = item.split(b"=", 1)[0].decode(errors="replace")
        if name in DOCKER_ENV_NAMES:
            docker_env.setdefault(name, []).append(source)
env_names("own_environ", b"\0".join(k.encode() + b"=" for k in os.environ))
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        with open("/proc/%s/environ" % pid, "rb") as f:
            env_names("/proc/%s/environ" % pid, f.read())
    except OSError:
        pass
    text = read("/proc/%s/status" % pid)
    if text:
        fields = dict(l.split(":", 1) for l in text.splitlines() if ":" in l)
        procs[pid] = {k: fields[k].strip() for k in ("Name", "Uid", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb",
                                                     "NoNewPrivs", "Seccomp") if k in fields}
out["docker_env"] = docker_env
out["docker_host_env"] = os.environ.get("DOCKER_HOST")
out["process_status"] = procs
# One walk of the whole filesystem except /proc and /sys. No -xdev equivalent: bind
# mounts and volumes are searched too, so a relocated daemon socket is found.
sockets, setid = [], []
for dirpath, dirnames, filenames in os.walk("/"):
    if dirpath == "/":
        dirnames[:] = [d for d in dirnames if d not in ("proc", "sys")]
    for name in filenames:
        path = os.path.join(dirpath, name)
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            continue
        if stat.S_ISSOCK(mode):
            sockets.append(path)
        elif stat.S_ISREG(mode) and mode & (stat.S_ISUID | stat.S_ISGID):
            setid.append(path)
out["socket_files"] = sorted(sockets)
out["unexpected_sockets"] = [{"path": p, "reply": ping(p)} for p in sorted(sockets)
                             if not p.startswith(ALLOWED_SOCKET_PREFIXES)]
out["setuid_setgid_files"] = sorted(setid)
out["mounts"] = parse_mountinfo(read("/proc/self/mountinfo") or "")
out["unexpected_mounts"] = unexpected_mounts(out["mounts"])
writes = {}
for target in ("/opt/lean-project/probe-write", "/opt/lean-project/.lake/packages/probe-write",
               "/workspace/.lake/packages/probe-write", "/workspace/.lake/build/probe-write", "/opt/elan/probe-write",
               "/usr/local/bin/probe-write", "/etc/probe-write", "/probe-write"):
    try:
        with open(target, "w") as f:
            f.write("x")
        os.unlink(target); writes[target] = "WRITABLE"
    except OSError as e:
        writes[target] = type(e).__name__ + ": " + (e.strerror or "")
out["writes"] = writes
# The local Lean time-limit wrappers: owner, mode and writability (never opened for writing).
wrappers = {}
for tool in ("lake", "lean"):
    path = "/usr/local/bin/" + tool
    try:
        st = os.stat(path)
        wrappers[tool] = {"path": path, "uid": st.st_uid, "mode": oct(st.st_mode & 0o7777),
                          "writable": os.access(path, os.W_OK), "resolves_to": shutil.which(tool)}
    except OSError as e:
        wrappers[tool] = {"path": path, "error": type(e).__name__, "resolves_to": shutil.which(tool)}
out["lean_wrappers"] = wrappers
status = dict(l.split(":", 1) for l in open("/proc/self/status").read().splitlines() if ":" in l)
out["status"] = {k: status[k].strip() for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs",
                                                 "Seccomp") if k in status}
# Container cgroup limits (cgroup v2, else v1), compared host-side with the compose file.
if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
    out["cgroup"] = {"version": 2, "memory_max": read("/sys/fs/cgroup/memory.max"),
                     "memory_swap_max": read("/sys/fs/cgroup/memory.swap.max"),
                     "pids_max": read("/sys/fs/cgroup/pids.max"), "cpu_max": read("/sys/fs/cgroup/cpu.max")}
else:
    quota, period = read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"), read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    out["cgroup"] = {"version": 1, "memory_max": read("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
                     "memory_memsw_max": read("/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes"),
                     "pids_max": read("/sys/fs/cgroup/pids/pids.max"),
                     "cpu_max": None if quota is None else ("max" if quota == "-1" else quota) + " " + str(period)}
out["memory_max"] = out["cgroup"].get("memory_max")
print(json.dumps(out))
"""


def filesystem_script():
    """FILESYSTEM_BODY with the mount classifier and its constants prepended (run
    in the container with ``python3 -I -c``)."""
    constants = (f"DOCKER_CONTAINER_FILE = {DOCKER_CONTAINER_FILE!r}\n"
                 f"ALLOWED_SOCKET_PREFIXES = {ALLOWED_SOCKET_PREFIXES!r}\n"
                 f"DOCKER_ENV_NAMES = {DOCKER_ENV_NAMES!r}\n")
    return "\n".join([constants, pyinspect.getsource(parse_mountinfo), pyinspect.getsource(unexpected_mounts),
                      FILESYSTEM_BODY])


def image_scan_shell(problem_dirs):
    """Content and name scans of the whole filesystem. No ``-xdev``: bind mounts and
    volumes are searched too; /proc, /sys and /dev are pruned explicitly."""
    prune = " -o ".join(f"-path {p}" for p in SCAN_PRUNE)
    patterns = " ".join(f"-e '{s}'" for s in DISTINCTIVE_STRINGS)
    names = " -o ".join(f"-path '*{d}*'" for d in problem_dirs) or "-false"
    return (f"echo '== content'; find / \\( {prune} \\) -prune -o -type f -size -20M -print0 2>/dev/null "
            f"| xargs -0 grep -IlsF {patterns} 2>/dev/null | head -n 50; "
            "echo '== names'; find / \\( -path /proc -o -path /sys -o -path /dev -o -path /workspace/problems \\) "
            "-prune -o \\( -name manifest.json -o -name pilot_subset.json -o -name plan.md -o -name interfaces.md "
            f"-o -name .env -o -name 'budget.sqlite' -o {names} \\) -print 2>/dev/null | head -n 50; echo '== end'")


def parse_scan(output):
    section, result = None, {"content": [], "names": []}
    for line in output.splitlines():
        if line.startswith("== "):
            section = line[3:].strip()
            continue
        if section in result and line.strip():
            result[section].append(line.strip())
    return result


# Marker files each agent writes in its own container (writable locations,
# including the IPC-namespace /dev/shm, which `ipc: shareable` would share).
MARKER_PATHS = ("/workspace/.probe-marker", "/tmp/probe-marker", "/dev/shm/probe-marker")
# Image-content trees skipped by the marker search; a mount inside them is flagged
# by the mount check instead.
MARKER_PRUNE = ("/proc", "/sys", "/dev", "/usr", "/opt/lean-project/.lake", "/opt/elan")


def marker_write_shell(marker):
    return "; ".join(f"echo {marker} > {path}" for path in MARKER_PATHS)


def marker_search_shell(marker):
    """Files containing ``marker`` anywhere (no ``-xdev``: volumes and bind mounts are
    searched), plus /dev/shm."""
    prune = " -o ".join(f"-path {p}" for p in MARKER_PRUNE)
    return (f"{{ find / \\( {prune} \\) -prune -o -type f -size -5M -print0; "
            "find /dev/shm -type f -size -5M -print0; } 2>/dev/null "
            f"| xargs -0 grep -lsF '{marker}' 2>/dev/null | head -n 20; true")


def scripts():
    hold = (f"for i in $(seq 1 115); do [ -e {RELEASE_FILE} ] && echo RELEASED && exit 0; sleep 1; done; "
            "echo HOLD_TIMEOUT")
    nested = ("cd /tmp && timeout 100 claude -p 'nested probe request' --output-format json </dev/null "
              "> /tmp/nested.out 2>&1; echo NESTED_EXIT=$?; tail -c 1500 /tmp/nested.out")
    links = ("ln -sf /etc/passwd /workspace/evil.lean && mkdir -p /tmp/w/memory/wiki && "
             "ln -sf /home/researcher/.claude/settings.json /tmp/w/memory/wiki/leak.md && echo ok")
    result = {}
    for agent in AGENTS:
        p = agent[-1]
        result[agent] = [
            ("hold_for_probes", bash(hold, f"i{p}-hold", 120000, "Wait for a file")),
            ("nested_claude", bash(nested, f"i{p}-nested", 120000, "Run a command")),
            ("submit_traversal", conf_call("submit_solution", {"problem_id": TEMPLATE,
                                                               "file_path": "/workspace/../etc/passwd"}, f"i{p}-trav")),
            ("submit_proc_environ", conf_call("submit_solution", {"problem_id": TEMPLATE,
                                                                  "file_path": "/proc/self/environ"}, f"i{p}-proc")),
            ("make_symlinks", bash(links, f"i{p}-links", description="Create links")),
            ("submit_symlink", conf_call("submit_solution", {"problem_id": TEMPLATE, "file_path": "/workspace/evil.lean"},
                                         f"i{p}-sym")),
            ("publish_symlink", conf_call("publish_notebook", {"file_path": "/tmp/w/memory/wiki/leak.md"}, f"i{p}-pub")),
            ("call_webfetch", tool_call("WebFetch", {"url": "https://example.com", "prompt": "x"}, f"i{p}-webfetch")),
            ("call_websearch", tool_call("WebSearch", {"query": "lean theorem"}, f"i{p}-websearch")),
            ("call_agent", tool_call("Agent", {"description": "x", "prompt": "x", "subagent_type": "general-purpose"},
                                     f"i{p}-agent")),
            ("call_task", tool_call("Task", {"description": "x", "prompt": "x"}, f"i{p}-task")),
            ("final", text("Probe script complete.")),
        ]
    return result


async def run_python(box, script, *args, timeout=120):
    result = await box.exec(["python3", "-I", "-c", script, *args], timeout=timeout)
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {"error": "unparseable", "returncode": result.returncode, "stderr": result.stderr[-800:]}


async def run_shell(box, command, timeout=120):
    result = await box.exec(["sh", "-c", command], timeout=timeout)
    return {"returncode": result.returncode, "stdout": result.stdout[-20000:], "stderr": result.stderr[-2000:]}


async def direct_sandbox_file_probes(agent_id, sbox, conference, record):
    """Host-side calls of the identity-bound conference tools against escape paths."""
    from research_swarm.tools import TOOL_DEFINITIONS
    bound = conference_tools(conference, agent_id, TOOL_DEFINITIONS, sbox=sbox, record=record)
    tools = dict(zip([d["name"] for d in TOOL_DEFINITIONS], bound))
    box = runtime._quiet(sbox)
    await box.exec(["sh", "-c", "ln -sf /etc/shadow /workspace/direct-shadow.lean; "
                                "ln -sf /proc/1/environ /workspace/direct-environ.lean; "
                                "mkdir -p /tmp/w/memory/wiki && ln -sf /etc/hostname /tmp/w/memory/wiki/direct.md; "
                                "mkfifo /workspace/direct-fifo.lean 2>/dev/null; true"])
    cases = {"dotdot": "/workspace/../etc/passwd", "relative_dotdot": "../../etc/passwd",
             "proc_self": "/proc/self/environ", "symlink_shadow": "/workspace/direct-shadow.lean",
             "symlink_proc_environ": "/workspace/direct-environ.lean", "fifo": "/workspace/direct-fifo.lean",
             "other_home": "/home/researcher/.claude/settings.json", "null_byte": "/workspace/a\x00.lean"}
    out = {}
    for name, path in cases.items():
        out[name] = json.loads(await tools["submit_solution"](problem_id=TEMPLATE, file_path=path))
    out["publish_symlink"] = json.loads(await tools["publish_notebook"](file_path="/tmp/w/memory/wiki/direct.md"))
    return out


def probe_config(args):
    """Judge parameters plus the agent compose inputs (pilot-config defaults, X3)."""
    return {"agent_image": args.image, "lean_image": args.lean_image, "compilation_timeout_seconds": 120,
            "judge_memory": "2g", "judge_cpus": 1, "judge_max_concurrent": 1, "agent_memory": args.agent_memory,
            "agent_cpus": getattr(args, "agent_cpus", None)}


def judge_network_probe(config, lean_image=None):
    """Command check plus one probe container with the judge's docker arguments."""
    compiler = runtime.make_lean_compiler(config, image=lean_image)
    probe = ("python3 -c \"import os,socket;print('IFACES', sorted(os.listdir('/sys/class/net')));"
             "s=socket.socket();s.settimeout(3);\n"
             "try:\n s.connect(('1.1.1.1',443));print('TCP connected')\nexcept Exception as e:\n print('TCP',type(e).__name__)\"")
    try:
        cmd = compiler.docker_command("gdm-lean-isolation-probe-" + uuid.uuid4().hex[:8], probe)
    except TypeError:
        cmd = compiler.docker_command("gdm-lean-isolation-probe-" + uuid.uuid4().hex[:8])
    flags = {"network_none": any(a == "--network" and b == "none" for a, b in zip(cmd, cmd[1:]))
             or "--network=none" in cmd,
             "read_only": "--read-only" in cmd, "cap_drop_all": any(a == "--cap-drop" and b == "ALL" for a, b in zip(cmd, cmd[1:])),
             "no_new_privileges": any("no-new-privileges" in a for a in cmd),
             "no_mounts": not any(a in ("-v", "--volume", "--mount") or a.startswith(("--volume=", "--mount=")) for a in cmd)}
    runtime_probe = {"ran": False}
    if probe in " ".join(cmd) or any(probe in a for a in cmd):
        try:
            proc = subprocess.run(cmd, input="", capture_output=True, text=True, timeout=120)
            runtime_probe = {"ran": True, "returncode": proc.returncode, "stdout": proc.stdout[-800:],
                             "stderr": proc.stderr[-400:]}
        except (OSError, subprocess.TimeoutExpired) as ex:
            runtime_probe = {"ran": False, "error": type(ex).__name__}
    else:
        runtime_probe["note"] = "docker_command() does not accept a probe shell; command-line check only"
    return {"command": cmd, "flags": flags, "runtime_probe": runtime_probe}


async def run(args, stamp):
    from inspect_ai.model import get_model
    from research_swarm.prompts import INITIAL_MESSAGE, build_prompt
    from research_swarm.tools import TOOL_DEFINITIONS
    config = probe_config(args)
    images = runtime.pin_images(config)  # the probed containers run the pinned IDs, as in run_pilot
    run_dir = (ROOT / "runs" / f"validation-isolation-{stamp}").resolve()
    run_dir.mkdir(parents=True)
    record = runtime.make_recorder(run_dir / "runtime.jsonl")
    record("images_pinned", None, images)
    listing, task_files = runtime.export_public_tasks(ROOT)
    conference = runtime.default_conference_factory(
        ROOT, compiler=runtime.make_lean_compiler(config, image=images["lean"]["id"]))(run_dir, list(AGENTS))
    ledger = vr.validation_ledger(run_dir)
    settings = ProviderSettings(pricing_verified=True)
    barrier = runtime.StartBarrier(len(AGENTS), record)
    clock = runtime.RunClock(barrier)
    archive = InstructionArchive(run_dir / "instructions", record)
    alive = {a: asyncio.Event() for a in AGENTS}
    written = {a: asyncio.Event() for a in AGENTS}
    markers = {a: "probe-marker-" + uuid.uuid4().hex for a in AGENTS}
    problem_dirs = sorted({"/".join(t["source_path"].split("/")[:2])
                           for t in json.loads((ROOT / "data" / "manifest.json").read_text())["tasks"]})
    probes = {}

    def on_step(agent, name):
        if name == "hold_for_probes":
            alive[agent].set()

    async def side(agent_id, sbox):
        result = probes.setdefault(agent_id, {})
        try:
            await asyncio.wait_for(alive[agent_id].wait(), timeout=900)
            await asyncio.sleep(3)  # the CLI is now inside its Bash wait loop
            box = runtime._quiet(sbox)
            result["network"] = await run_python(box, NETWORK_PY)
            result["secrets"] = await run_python(box, SECRETS_PY, BRIDGE_PLACEHOLDER, timeout=300)
            result["filesystem"] = await run_python(box, filesystem_script(), timeout=900)
            result["processes"] = await run_shell(box, "ps -eo pid,ppid,user,etimes,args | cut -c1-300")
            result["sandbox_services"] = await run_shell(box, "ls -laR /var/tmp/sandbox-services 2>&1 | head -n 60")
            result["image_scan"] = parse_scan((await run_shell(box, image_scan_shell(problem_dirs), timeout=900))["stdout"])
            await box.exec(["sh", "-c", marker_write_shell(markers[agent_id])])
            written[agent_id].set()
            other = next(a for a in AGENTS if a != agent_id)
            await asyncio.wait_for(written[other].wait(), timeout=600)
            result["cross_agent"] = {
                "other_marker_hits": (await run_shell(box, marker_search_shell(markers[other]), 600))["stdout"].split(),
                "own_marker_hits": (await run_shell(box, marker_search_shell(markers[agent_id]), 600))["stdout"].split()}
            result["direct_sandbox_file"] = await direct_sandbox_file_probes(agent_id, sbox, conference, record)
        except Exception as ex:
            result["error"] = repr(ex)[:800]
        finally:
            try:
                await runtime._quiet(sbox).exec(["touch", RELEASE_FILE])
            except Exception as ex:
                result["release_error"] = repr(ex)[:200]
        return result

    def filter_for(agent_id):
        return vr.attributed(agent_id, budget_filter(agent_id, ledger, settings, barrier, record, vr.VALIDATION_SCOPE,
                                                     archive, clock.stop))

    compose = runtime.make_compose(run_dir, config, images["agent"]["id"])
    binding = vr.compose_binding(compose, config, images["agent"]["id"])
    record("compose_bound", None, binding)
    peer = runtime.peer_solver(conference=conference, definitions=TOOL_DEFINITIONS, personas=PERSONAS,
                               prompt_for=lambda a, p: build_prompt(a, p, listing), filter_for=filter_for,
                               run_dir=run_dir, record=record, clock=clock)
    task = runtime.build_task(agent_ids=list(AGENTS), files=runtime.agent_files(task_files),
                              solver=vr.with_side_task(peer, side, wait_side=True), compose=compose,
                              metadata={"condition": "unpaid-isolation-probes"}, initial_message=INITIAL_MESSAGE,
                              name="gdm_isolation")
    scripted = vr.ScriptedModel(scripts(), final="Probe script complete.", on_step=on_step)
    monitor = asyncio.create_task(clock.monitor(conference, record, startup_timeout=600,
                                                max_run_seconds=args.max_seconds, post_depletion_seconds=600))
    try:
        log = await vr.run_eval(task, get_model(vr.MOCK_MODEL, custom_outputs=scripted), agents=len(AGENTS),
                                log_dir=run_dir / "inspect")
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await asyncio.to_thread(conference.close)
    judge = await asyncio.to_thread(judge_network_probe, config, images["lean"]["id"])
    return {"run_dir": run_dir, "log": log, "scripted": scripted, "probes": probes, "judge": judge, "images": images,
            "runtime_events": vr.read_jsonl(run_dir / "runtime.jsonl"), "markers": markers, "compose": binding,
            "compose_text": Path(compose).read_text()}


def _failed(value):
    return isinstance(value, str) and not value.startswith(("connected", "resolved", "reply", "reachable"))


PAGE_SIZE = 4096
CPU_PERIOD = 100_000  # Docker's CFS period for `cpus`
ZERO_CAPS = "0000000000000000"
CAP_FIELDS = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")


def docker_bytes(value):
    """Docker's memory size parsing (binary multiples: 1g = 1073741824), or None."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgtp]?)i?b?", str(value or "").strip().lower())
    if not match:
        return None
    return int(float(match.group(1)) * 1024 ** " kmgtp".index(match.group(2) or " "))


def compose_limits(compose_text):
    """Expected cgroup values from the compose file the agents ran (runtime.write_compose)."""
    def field(name):
        match = re.search(rf"^\s*{name}:\s*(\S+)\s*$", compose_text or "", re.M)
        return match.group(1).strip("\"'") if match else None
    memory, memswap, pids, cpus = (docker_bytes(field("mem_limit")), docker_bytes(field("memswap_limit")),
                                   field("pids_limit"), field("cpus"))
    return {"memory_max": None if memory is None else memory - memory % PAGE_SIZE,
            "swap_max": None if memory is None or memswap is None else memswap - memory,
            "pids_max": pids, "cpus": None if cpus is None else float(cpus)}


def cgroup_problems(cgroup, expected):
    """ISO-2: pids.max, memory.max, swap and cpu.max inside the container equal the compose settings."""
    cgroup, problems = cgroup or {}, []
    if expected.get("memory_max") is None or expected.get("pids_max") is None or expected.get("swap_max") is None:
        return ["compose file lacks mem_limit/memswap_limit/pids_limit"]
    memory = cgroup.get("memory_max")
    if not (memory or "").isdigit() or int(memory) != expected["memory_max"]:
        problems.append(f"memory.max {memory!r} != {expected['memory_max']}")
    if cgroup.get("version") == 2:
        swap = cgroup.get("memory_swap_max")
    else:
        memsw = cgroup.get("memory_memsw_max")
        swap = str(int(memsw) - int(memory)) if (memsw or "").isdigit() and (memory or "").isdigit() else memsw
    if not (swap or "").isdigit() or int(swap) != expected["swap_max"]:
        problems.append(f"swap limit {swap!r} != {expected['swap_max']} (memswap_limit - mem_limit)")
    if cgroup.get("pids_max") != str(expected["pids_max"]):
        problems.append(f"pids.max {cgroup.get('pids_max')!r} != {expected['pids_max']}")
    quota, _, period = (cgroup.get("cpu_max") or "").partition(" ")
    if expected.get("cpus") is None:
        if quota != "max":
            problems.append(f"cpu.max {cgroup.get('cpu_max')!r} != unlimited")
    elif not (quota.isdigit() and period.isdigit()) or abs(int(quota) / int(period) - expected["cpus"]) > 0.01:
        problems.append(f"cpu.max {cgroup.get('cpu_max')!r} != {expected['cpus']} CPUs")
    return problems


def _zero(value):
    return isinstance(value, str) and value.strip() != "" and value.strip("0") == ""


def privilege_problems(fs):
    """ISO-2: the probe process and every visible process have empty capability sets
    (CapBnd included, which is what ``cap_drop: [ALL]`` changes for a non-root uid),
    NoNewPrivs=1 and seccomp filter mode (Seccomp: 2)."""
    problems = []
    if fs.get("uid") != 1000:
        problems.append(f"uid {fs.get('uid')} != 1000")
    status = fs.get("status") or {}
    problems += [f"{k}={status.get(k)}" for k in CAP_FIELDS if not _zero(status.get(k))]
    if status.get("NoNewPrivs") != "1":
        problems.append(f"NoNewPrivs={status.get('NoNewPrivs')}")
    if status.get("Seccomp") != "2":
        problems.append(f"Seccomp={status.get('Seccomp')}")
    processes = fs.get("process_status") or {}
    if not processes:
        problems.append("no process status read")
    for pid, proc in sorted(processes.items()):
        bad = [k for k in CAP_FIELDS if not _zero(proc.get(k))]
        bad += [f"{k}={proc.get(k)}" for k, want in (("NoNewPrivs", "1"), ("Seccomp", "2")) if proc.get(k) != want]
        if bad:
            problems.append(f"pid {pid} ({proc.get('Name')}): {', '.join(bad)}")
    return problems


def socket_and_mount_problems(fs):
    """ISO-1/RT-1: no daemon socket anywhere (standard paths, any socket outside the
    bridge's directory, any socket answering the Docker API), no daemon-pointing
    environment variable in any process, and no mount beyond Docker's defaults."""
    problems = [f"socket at {p}" for p, present in (fs.get("sockets") or {}).items() if present]
    problems += [f"unexpected socket {s['path']} ({s.get('reply')})" for s in fs.get("unexpected_sockets") or []]
    if fs.get("docker_host_env") or fs.get("docker_env"):
        problems.append(f"daemon environment variables: {sorted(fs.get('docker_env') or {'DOCKER_HOST': 1})}")
    problems += [f"mount {m['mount_point']}: {m.get('reason')}" for m in fs.get("unexpected_mounts") or []]
    if "unexpected_mounts" not in fs or "unexpected_sockets" not in fs or not fs.get("mounts"):
        problems.append("mount/socket probe did not report")
    return problems


def wrapper_problems(fs):
    """The lake/lean time-limit wrappers (docker/agent.Dockerfile): present, first on
    PATH, root-owned and not writable by the agent."""
    problems = []
    for tool, path in vr.LEAN_WRAPPERS.items():
        info = (fs.get("lean_wrappers") or {}).get(tool) or {}
        if info.get("error") or not info:
            problems.append(f"{path} missing ({info.get('error')}); rebuild the agent image")
            continue
        if info.get("resolves_to") != path:
            problems.append(f"{tool} resolves to {info.get('resolves_to')!r}, not {path}")
        if info.get("uid") != 0 or info.get("writable"):
            problems.append(f"{path} owned by uid {info.get('uid')}, writable={info.get('writable')}")
    return problems


def build_checks(result):
    probes, log, scripted, run_dir = result["probes"], result["log"], result["scripted"], result["run_dir"]
    events = result["runtime_events"]
    kinds = defaultdict(list)
    for event in events:
        kinds[event["kind"]].append(event)
    samples = {str(s.id): vr.sample_summary(s) for s in log.samples}
    archives = [json.loads(p.read_text()) for p in sorted((run_dir / "instructions").glob("*.json"))]
    expected_limits = compose_limits(result.get("compose_text", ""))
    checks, findings = {}, {}
    for agent in AGENTS:
        p, s = probes.get(agent, {}), agent[-1]
        net, fs, sec = p.get("network", {}), p.get("filesystem", {}), p.get("secrets", {})
        r = samples.get(agent, {}).get("tool_results", {})
        own = [a for a in archives if a["agent_id"] == agent and any(f"You are {agent}," in b for b in a["system_blocks"])]
        offered = sorted({t for a in own for t in a["tools"]})
        nested_archives = [a for a in archives if a["agent_id"] == agent
                           and not any(f"You are {agent}," in b for b in a["system_blocks"])]
        foreign = [x for x in scripted.log.get(agent, []) if x.get("kind") == "foreign_session"]
        checks[f"{agent}:probes_completed"] = check(bool(p) and "error" not in p, error=p.get("error"))
        checks[f"{agent}:egress_denied"] = check(
            bool(net.get("tcp")) and all(_failed(v) for v in net["tcp"].values())
            and all(_failed(v) for v in net.get("dns", {}).values()) and _failed(net.get("udp_dns"))
            and _failed(net.get("https_openrouter")) and set(net.get("interfaces") or []) <= {"lo"},
            network=net)
        mount_problems = socket_and_mount_problems(fs) if fs else ["no filesystem probe"]
        checks[f"{agent}:no_docker_socket_or_host_mounts"] = check(
            not mount_problems, problems=mount_problems, sockets=fs.get("sockets"), socket_files=fs.get("socket_files"),
            unexpected_sockets=fs.get("unexpected_sockets"), docker_env=fs.get("docker_env"),
            unexpected_mounts=fs.get("unexpected_mounts"), mounts=fs.get("mounts"),
            note="Every mount must be one of Docker's per-container defaults (no host bind mount or volume on any "
                 "platform); every socket outside /var/tmp/sandbox-services fails, wherever it is.")
        checks[f"{agent}:no_provider_keys"] = check(
            bool(sec) and not sec.get("hits") and sec.get("environ_files_read", 0) > 1, secrets=sec,
            note="Only Inspect SWE's public bridge placeholder is allowed; values are never recorded.")
        checks[f"{agent}:no_repository_or_researcher_content"] = check(
            "image_scan" in p and not p["image_scan"]["content"] and not p["image_scan"]["names"],
            scan=p.get("image_scan"), strings=list(DISTINCTIVE_STRINGS), pruned=list(SCAN_PRUNE))
        writes = fs.get("writes", {})
        wrappers = wrapper_problems(fs)
        checks[f"{agent}:dependency_and_system_paths_read_only"] = check(
            bool(writes) and "WRITABLE" not in writes.values() and not wrappers, writes=writes,
            lean_wrappers=fs.get("lean_wrappers"), wrapper_problems=wrappers)
        privileges = privilege_problems(fs)
        checks[f"{agent}:no_privileges"] = check(
            not privileges, problems=privileges, status=fs.get("status", {}), uid=fs.get("uid"),
            groups=fs.get("groups"), process_status=fs.get("process_status"),
            setuid_setgid_files=fs.get("setuid_setgid_files"),
            note="CapBnd must be empty (cap_drop: [ALL]); setuid files are inert under NoNewPrivs=1 with no "
                 "capabilities and are listed for review")
        limits = cgroup_problems(fs.get("cgroup"), expected_limits) if fs else ["no filesystem probe"]
        checks[f"{agent}:cgroup_limits_match_compose"] = check(
            not limits, problems=limits, cgroup=fs.get("cgroup"), expected=expected_limits)
        direct = p.get("direct_sandbox_file", {})
        checks[f"{agent}:sandbox_file_escapes_rejected"] = check(
            bool(direct) and all(v.get("ok") is False for v in direct.values())
            and all(_json_error(r.get(f"i{s}-{c}")) for c in ("trav", "proc", "sym", "pub")),
            direct={k: v.get("error") for k, v in direct.items()},
            bridged={c: r.get(f"i{s}-{c}", "")[:200] for c in ("trav", "proc", "sym", "pub")})
        cross = p.get("cross_agent", {})
        checks[f"{agent}:other_agent_files_unreachable"] = check(
            bool(cross) and not cross.get("other_marker_hits") and bool(cross.get("own_marker_hits")), cross_agent=cross)
        checks[f"{agent}:web_and_nested_agent_tools_absent"] = check(
            bool(offered) and not [t for t in offered if t in DISALLOWED_TOOLS]
            and all(f"i{s}-{c}" in r for c in ("webfetch", "websearch", "agent", "task")),
            offered_tools=offered,
            call_results={c: r.get(f"i{s}-{c}", "")[:200] for c in ("webfetch", "websearch", "agent", "task")})
        nested_result = r.get(f"i{s}-nested", "")
        findings[f"{agent}:nested_claude"] = {
            "reached_bridge_and_model": f"nested-ok-{agent}" in nested_result,
            "passed_budget_filter": bool(foreign),  # the mock model is only reachable through the filter
            "archived_as_separate_session": len(nested_archives),
            "nested_session_tools": sorted({t for a in nested_archives for t in a["tools"]}),
            "inspect_model_events": samples.get(agent, {}).get("model_events"),
            "cli_output_excerpt": nested_result[-600:],
            "interpretation": "A nested `claude -p` inherits the CLI's bridge environment; when it reaches the bridge "
                              "it is admitted, budgeted and logged as this agent, but without the parent's "
                              "--disallowed-tools or conference MCP tools (its own default tool set, no web egress). "
                              "Documented, not blocked."}
        findings[f"{agent}:residual_cues"] = {"processes": p.get("processes", {}).get("stdout", "")[-3000:],
                                              "sandbox_services": p.get("sandbox_services", {}).get("stdout", "")[-2000:]}
    judge = result["judge"]
    probe = judge["runtime_probe"]
    # The probe container must actually have run: command-line flags alone are not enough.
    checks["judge_container_network_none"] = check(
        all(judge["flags"].values()) and probe.get("ran") is True and probe.get("returncode") == 0
        and "IFACES ['lo']" in probe.get("stdout", "") and "TCP connected" not in probe.get("stdout", ""),
        **judge)
    binding = result.get("compose") or {}
    checks["compose_matches_fingerprint"] = check(binding.get("matches") is True, compose=binding)
    checks["sample_errors"] = check(all(not s.get("error") for s in samples.values()) and len(samples) == len(AGENTS),
                                    errors={a: s.get("error") for a, s in samples.items()})
    return checks, findings, samples


def _json_error(value):
    try:
        return json.loads(value).get("ok") is False
    except (TypeError, ValueError, AttributeError):
        return False


def parse_args(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=str(vr.DEFAULT_CONFIG),
                     help="pilot config supplying the image tags and agent memory/CPUs (compose_sha256)")
    known, _ = pre.parse_known_args(argv)
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0], parents=[pre])
    parser.add_argument("--image")
    parser.add_argument("--lean-image")
    parser.add_argument("--agent-memory")
    parser.add_argument("--agent-cpus", type=vr.optional_float)
    parser.add_argument("--max-seconds", type=int, default=2400)
    parser.add_argument("--output-dir", default=str(vr.VALIDATION_DIR))
    vr.add_interlock_argument(parser)
    parser.set_defaults(**vr.pilot_runtime_defaults(known.config))
    return parser.parse_args(argv)


def main():
    args = parse_args()
    interlock = vr.interlock_or_refuse(args)  # before any container starts (runtime.launch_interlock)
    if interlock is None:
        return 2
    stamp = vr.utc_stamp()
    result = asyncio.run(run(args, stamp))
    checks, findings, samples = build_checks(result)
    checks["no_conflicting_containers_at_start"] = vr.interlock_check(interlock)
    images = result["images"]
    path, report = vr.write_evidence(
        "runtime-isolation", "gdm-runtime-isolation/1", checks,
        {"agent": images["agent"]["id"], "lean": images["lean"]["id"]}, stamp, args.output_dir, images=images,
        kind="unpaid container isolation probes (mockllm-driven Claude Code + direct sandbox exec)",
        compose_sha256=result["compose"]["compose_sha256"], compose=result["compose"],
        run_dir=vr.rel(result["run_dir"]), inspect_log=Path(result["log"].location).name, interlock=interlock,
        findings=findings, request_sequences={a: [x.get("step") or x.get("kind") for x in result["scripted"].log.get(a, [])]
                                              for a in AGENTS},
        samples={a: {k: v for k, v in s.items() if k != "tool_results"} for a, s in samples.items()})
    print(json.dumps({k: v["passed"] for k, v in checks.items()}, indent=2))
    print(f"wrote {vr.rel(path)}; all_required_passed={report['all_required_passed']}")
    return 0 if report["all_required_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
