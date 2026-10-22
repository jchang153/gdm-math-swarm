"""Resume bundle: the per-agent state a later segment needs to continue a run.

docs/runtime.md, "Resume bundle"; owner decision of 2026-10-22 (docs/plan.md): every run
saves, before its containers are torn down, for each agent

- the Claude Code session JSONL(s) in its config directory (``~/.agent/projects`` in the
  neutral environment, ``~/.claude/projects`` otherwise), inside
- one archive of the agent's private state: ``/workspace`` (RESEARCH.md included), ``/tmp``
  (the private wiki ``/tmp/w/memory/wiki`` included) and ``/home/researcher`` (the config
  directory); symlinks are stored as links (the read-only Mathlib tree is never copied);
- the processes alive in the container and the tool calls still open in its sessions;

and a run-level ``RUN/resume/manifest.json`` with everything a continuation must match
(session ids, persona, prompt variant and prompt hash, role, reasoning-transport key id,
image ids, model route and pricing, code commit, dataset revision, neutral-env version,
clock state, budget-ledger run id). Mid-run checkpoints (config
``resume_checkpoint_minutes``, default off) write the same layout under
``RUN/resume/checkpoint-<min>/``.

Saving never changes what an agent sees: no prompt, tool, message or file the agent
reads is touched. The archive is built by a short in-container script into one hidden
temporary file in ``/tmp`` that is read back and removed at once (excluded from the
archive itself). Every member is screened with the publication credential screen
(scripts/publish_hf_library.py ``SECRET``) on the host; a matching member is withheld
and only its path is recorded. The containers hold no provider credential.

The bundle is private raw state: it stays in the packaged run (completed-run.tgz) and is
never published to the HF Space (prepare_hf_library.py ``run_excluded``).
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import re
import secrets
import subprocess
import tarfile
import threading
import time
from pathlib import Path

SCHEMA = "gdm-resume-bundle/1"
RESUME_DIR = "resume"
FINAL_LABEL = "final"
CHECKPOINT_KEY = "resume_checkpoint_minutes"
HOME = "/home/researcher"
ARCHIVE_ROOTS = ("/workspace", "/tmp", HOME)
CONFIG_DIRS = {True: HOME + "/.agent", False: HOME + "/.claude"}
TEMP_PREFIX = ".gdm-resume-"
CHUNK_BYTES = 64 * 1024 * 1024          # below Inspect's 100 MiB read_file limit
MAX_MEMBER_BYTES = 256 * 1024 * 1024    # larger single files are listed, not archived
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
SAVE_TIMEOUT_SECONDS = 300

# The publication screen (scripts/publish_hf_library.py _SECRET_PATTERN / PUBLIC_PLACEHOLDERS);
# tests/test_resume_bundle.py checks the two stay identical.
_SECRET_PATTERN = re.compile(rb'(?<![A-Za-z0-9+/_-])(?:sk-[A-Za-z0-9_-]{24,}|hf_[A-Za-z0-9]{30,})'
                             rb'|-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----')
PUBLIC_PLACEHOLDERS = frozenset({b'sk-ant-api03-DOq5tyLPrk9M4hPE'})


def credential_like(data):
    return any(m.group(0) not in PUBLIC_PLACEHOLDERS for m in _SECRET_PATTERN.finditer(data))


# Runs inside the agent container (python3 -I). argv: JSON {roots, out, chunk, max_member, max_total}.
# Writes the gzip tar to out (split into out.000, out.001, ... of ``chunk`` bytes), and prints
# JSON: parts, sha256, bytes, skipped members, and the live process table (from /proc).
_ARCHIVE_SCRIPT = r"""
import hashlib, json, os, sys, tarfile
spec = json.loads(sys.argv[1])
out, prefix = spec["out"], os.path.basename(spec["out"])
strip = spec.get("strip") or ""  # test sandboxes only: the host folder standing in for "/"
skipped, total = [], [0]
def shown(path):
    path = path or ""
    return path[len(strip):] if strip and path.startswith(strip) else path
def keep(info):
    if os.path.basename(info.name).startswith(prefix):
        return None
    if info.isfile() and info.size > spec["max_member"]:
        skipped.append({"path": "/" + info.name, "reason": "member_size_limit", "bytes": info.size})
        return None
    if info.isfile():
        total[0] += info.size
        if total[0] > spec["max_total"]:
            skipped.append({"path": "/" + info.name, "reason": "archive_size_limit", "bytes": info.size})
            total[0] -= info.size
            return None
    return info
def add(tar, path):
    try:
        tar.add(path, arcname=shown(path).lstrip("/"), recursive=False, filter=keep)
    except Exception as ex:  # unreadable or vanished: listed, the rest is still archived
        skipped.append({"path": shown(path), "reason": type(ex).__name__})
with tarfile.open(out, "w:gz") as tar:
    for root in spec["roots"]:
        if not os.path.lexists(root):
            continue
        add(tar, root)
        if os.path.islink(root) or not os.path.isdir(root):
            continue
        def failed(error):
            skipped.append({"path": shown(getattr(error, "filename", None)), "reason": type(error).__name__})
        for folder, dirs, files in os.walk(root, onerror=failed):  # never follows symlinks
            dirs.sort()
            for name in dirs + sorted(files):
                add(tar, os.path.join(folder, name))
def processes():
    rows = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in sorted(pids, key=int):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
            with open(f"/proc/{pid}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            rows.append({"pid": int(pid), "ppid": int(fields[1]), "state": fields[0],
                         "start_ticks": int(fields[19]), "argv": argv[:20]})
        except (OSError, IndexError, ValueError):
            continue
    return rows
digest, size, parts = hashlib.sha256(), 0, []
with open(out, "rb") as f:
    while True:
        block = f.read(spec["chunk"])
        if not block:
            break
        name = f"{out}.{len(parts):03d}"
        with open(name, "wb") as p:
            p.write(block)
        parts.append(name)
        digest.update(block)
        size += len(block)
os.remove(out)
uptime = None
try:
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
except (OSError, ValueError):
    pass
print(json.dumps({"parts": parts, "sha256": digest.hexdigest(), "bytes": size, "skipped": skipped,
                  "processes": processes(), "uptime_seconds": uptime,
                  "clock_ticks": os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else None}))
"""


def _utc(ts=None):
    return datetime.datetime.fromtimestamp(time.time() if ts is None else ts, datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def session_files(members, config_dir):
    """{archive path: bytes} of the Claude Code session transcripts under ``config_dir``/projects."""
    projects = config_dir.strip("/") + "/projects/"
    return {name: data for name, data in members.items() if name.startswith(projects) and name.endswith(".jsonl")}


def session_summary(files, config_dir):
    """Main session ids (a ``projects/<cwd>/<uuid>.jsonl`` file), sidechain files, and the
    tool calls still open at the end of each main session (a tool_use without its result)."""
    projects = config_dir.strip("/") + "/projects/"
    sessions, sidechains = [], []
    for name, data in sorted(files.items()):
        relative = name[len(projects):].split("/")
        entry = {"path": "/" + name, "bytes": len(data), "sha256": _sha(data)}
        if len(relative) != 2:
            sidechains.append(entry)
            continue
        uses, results, lines, last = {}, set(), 0, None
        for line in data.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            lines += 1
            last = row.get("timestamp") or last
            content = (row.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else ():
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("id"):
                    uses[block["id"]] = {"id": block["id"], "name": block.get("name"),
                                         "input_preview": json.dumps(block.get("input"))[:300]}
                elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                    results.add(block["tool_use_id"])
        sessions.append({**entry, "session_id": relative[1][:-len(".jsonl")], "project": relative[0],
                         "lines": lines, "last_timestamp": last,
                         "open_tool_calls": [u for i, u in uses.items() if i not in results]})
    return sessions, sidechains


def screen_archive(data):
    """Rebuild the archive without credential-like members. Returns (archive bytes, members
    {name: bytes} of regular files, withheld paths, member count)."""
    withheld, members, count = [], {}, 0
    source = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as target:
        for info in source:
            payload = source.extractfile(info).read() if info.isfile() else None
            if payload is not None and credential_like(payload):
                withheld.append("/" + info.name)
                continue
            if credential_like(info.name.encode()) or credential_like((info.linkname or "").encode()):
                withheld.append("/" + info.name)
                continue
            count += 1
            if payload is not None:
                members[info.name] = payload
                target.addfile(info, io.BytesIO(payload))
            else:
                target.addfile(info)
    return output.getvalue(), members, withheld, count


async def capture_agent(sbox, *, roots=ARCHIVE_ROOTS, neutral=True, chunk=CHUNK_BYTES,
                        max_member=MAX_MEMBER_BYTES, max_total=MAX_ARCHIVE_BYTES):
    """Archive one agent's private state through its sandbox. Returns (archive bytes, info)."""
    out = f"/tmp/{TEMP_PREFIX}{secrets.token_hex(8)}.tgz"
    spec = {"roots": list(roots), "out": out, "chunk": int(chunk), "max_member": int(max_member),
            "max_total": int(max_total)}
    result = await sbox.exec(["python3", "-I", "-c", _ARCHIVE_SCRIPT, json.dumps(spec)], timeout=SAVE_TIMEOUT_SECONDS)
    if not result.success:
        await _remove(sbox, out)
        raise RuntimeError("archive script failed: " + (result.stderr or "")[-500:])
    info = json.loads(result.stdout)
    try:
        data = b"".join([await sbox.read_file(part, text=False) for part in info["parts"]])
    finally:
        await _remove(sbox, out)
    if _sha(data) != info["sha256"] or len(data) != info["bytes"]:
        raise RuntimeError("archive changed in transfer")
    return data, info


async def _remove(sbox, out):
    try:
        await sbox.exec(["python3", "-I", "-c",
                         "import glob, os, sys\nfor p in glob.glob(sys.argv[1] + '*'):\n    os.remove(p)", out],
                        timeout=60)
    except Exception:
        pass


class ResumeBundler:
    """Writes per-agent resume state and the run-level manifest under ``run_dir/resume``.

    ``run_info`` is the run-level part of the manifest (set once settings are known, see
    runtime._run_pilot); ``clock`` the RunClock (its barrier gives the common start)."""

    def __init__(self, run_dir, record, *, neutral=True, run_info=None, clock=None, checkpoint_minutes=None,
                 roots=ARCHIVE_ROOTS):
        self.root = Path(run_dir) / RESUME_DIR
        self.record = record
        self.neutral = bool(neutral)
        self.config_dir = CONFIG_DIRS[self.neutral]
        self.run_info = dict(run_info or {})
        self.clock = clock
        self.checkpoint_minutes = checkpoint_minutes
        self.roots = tuple(roots)
        self.lock = threading.Lock()

    def folder(self, label):
        return self.root if label == FINAL_LABEL else self.root / label

    def clock_state(self):
        barrier = getattr(self.clock, "barrier", None)
        started_at = getattr(barrier, "started_at", None)
        stop = getattr(self.clock, "stop", None)
        return {"common_start_utc": None if getattr(barrier, "started_wall", None) is None
                else _utc(barrier.started_wall),
                "elapsed_seconds": None if started_at is None else round(time.monotonic() - started_at, 3),
                "stopped": bool(stop is not None and stop.is_set()),
                "stop_reason": getattr(self.clock, "reason", None)}

    async def save_agent(self, sbox, agent_id, label=FINAL_LABEL, *, agent=None):
        """Archive one agent and write ``<folder>/agents/<agent_id>/{state.tar.gz, agent.json}``.
        ``agent`` holds the per-agent manifest fields known to the solver. Never raises: a failure
        is recorded (``resume_bundle_error``) and in the manifest."""
        folder = self.folder(label) / "agents" / agent_id
        entry = {"agent_id": agent_id, "label": label, "captured_utc": _utc(), "clock": self.clock_state(),
                 "config_dir": self.config_dir, "roots": list(self.roots), **(agent or {})}
        try:
            raw, info = await capture_agent(sbox, roots=self.roots, neutral=self.neutral)
            archive, members, withheld, count = screen_archive(raw)
            sessions, sidechains = session_summary(session_files(members, self.config_dir), self.config_dir)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "state.tar.gz").write_bytes(archive)
            entry.update({
                "status": "saved",
                "archive": {"path": str((folder / "state.tar.gz").relative_to(self.root.parent)),
                            "bytes": len(archive), "sha256": _sha(archive), "members": count,
                            "content_bytes": sum(len(v) for v in members.values()),
                            "skipped": info.get("skipped") or [], "credential_withheld": withheld},
                "sessions": sessions, "sidechain_files": sidechains,
                "session_ids": [s["session_id"] for s in sessions],
                "open_tool_calls": {s["session_id"]: s["open_tool_calls"] for s in sessions if s["open_tool_calls"]},
                "processes": info.get("processes"),
                "container_uptime_seconds": info.get("uptime_seconds"), "clock_ticks": info.get("clock_ticks"),
                "wiki_files": sorted("/" + n for n in members if n.startswith("tmp/w/memory/wiki/")),
                "research_md_bytes": len(members.get("workspace/RESEARCH.md", b""))})
            if withheld:
                self.record("resume_bundle_credential_withheld", agent_id, {"label": label, "paths": withheld})
        except Exception as ex:
            entry.update({"status": "error", "error": f"{type(ex).__name__}: {str(ex)[:300]}"})
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "agent.json").write_text(json.dumps(entry, indent=2, default=str))
        summary = {"label": label, "status": entry["status"],
                   "bytes": (entry.get("archive") or {}).get("bytes"), "sessions": len(entry.get("session_ids") or []),
                   **({"error": entry["error"]} if "error" in entry else {})}
        self.record("resume_bundle_saved" if entry["status"] == "saved" else "resume_bundle_error", agent_id, summary)
        self.write_manifest(label)
        return entry

    def write_manifest(self, label=FINAL_LABEL, expected=None):
        """(Re)write ``<folder>/manifest.json`` from the agent entries present (idempotent)."""
        folder = self.folder(label)
        with self.lock:
            agents = {}
            for path in sorted((folder / "agents").glob("*/agent.json")) if (folder / "agents").is_dir() else ():
                try:
                    agents[path.parent.name] = json.loads(path.read_text())
                except ValueError:
                    agents[path.parent.name] = {"status": "unreadable"}
            expected = expected or self.run_info.get("agent_ids") or []
            manifest = {"schema": SCHEMA, "label": label, "written_utc": _utc(), "clock": self.clock_state(),
                        **self.run_info, "agents": agents,
                        "missing_agents": [a for a in expected if a not in agents],
                        "total_archive_bytes": sum((a.get("archive") or {}).get("bytes") or 0 for a in agents.values())}
            folder.mkdir(parents=True, exist_ok=True)
            temp = folder / "manifest.json.tmp"
            temp.write_text(json.dumps(manifest, indent=2, default=str))
            temp.replace(folder / "manifest.json")
        return manifest

    async def run_checkpoints(self, sbox, agent_id, agent_fields, sleep=None):
        """Every ``checkpoint_minutes`` after the common start, save under checkpoint-<min>
        until the run stops (started only when the config sets the interval)."""
        import asyncio
        sleep = sleep or asyncio.sleep
        try:
            minutes = float(self.checkpoint_minutes)
            await self.clock.barrier.event.wait()
            k = 1
            while not self.clock.stop.is_set():
                due = self.clock.barrier.started_at + k * minutes * 60
                await sleep(max(0.0, due - time.monotonic()))
                if self.clock.stop.is_set():
                    break
                label = f"checkpoint-{int(round(k * minutes)):04d}"
                await self.save_agent(sbox, agent_id, label, agent=agent_fields())
                k += 1
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # never ends the agent's task group
            try:
                self.record("resume_bundle_error", agent_id, {"label": "checkpoint", "error": type(ex).__name__})
            except Exception:
                pass
        await asyncio.Event().wait()  # stay pending like the snapshot loop; cancelled with the group


def checkpoint_minutes(config):
    """Config ``resume_checkpoint_minutes``: None (default, off) or a positive number."""
    value = config.get(CHECKPOINT_KEY)
    if value in (None, False, 0):
        return None
    value = float(value)
    if value <= 0:
        raise ValueError(f"{CHECKPOINT_KEY} must be positive")
    return value


def code_version(repo_root):
    """The checkout's commit and whether it had uncommitted changes (host-side git; None on failure)."""
    def git(*args):
        try:
            done = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True, timeout=30)
            return done.stdout.strip() if done.returncode == 0 else None
        except Exception:
            return None
    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"commit": commit, "dirty": None if status is None else bool(status)}


def file_digest(path):
    try:
        return _sha(Path(path).read_bytes())
    except Exception:
        return None
