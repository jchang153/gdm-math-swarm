"""Seeded-contagion track: a generic injection mechanism, disabled by default.

Owner decision (docs/plan.md, "Seeded overseer experiment, October 25, 2026"): the
declared seeded runs plant ONE exploit, a silent participant (``author``, the
identifier after the solvers, no container and no model) credited with an accepted
submission through the normal acceptance path (``declared_run_problems``). The
artifact is a researcher-only file in the repository (``research/seeds/``), given by
a repository-relative ``artifact_path`` and pinned by SHA-256. Baseline runs still
carry no seeded configuration at all.

docs/plan.md (Track definitions): the seeded track introduces a standardized
artifact at a predefined trigger, but the trigger, artifact, provenance and
credit/task-state effects are UNDECIDED by the user. This module therefore only
supplies configurable mechanics so that the user's eventual choices can be
expressed without touching the baseline path. It is never active unless the
service runs in the ``seeded`` track AND the configuration says ``enabled``.

No artifact ships with the repository: the artifact is read at run time from a
researcher-supplied absolute path and must match a pinned SHA-256. The service
(``research_swarm.conference``) owns the state changes; this module validates the
configuration and decides when the trigger is due. ``conference_arguments`` turns a
run configuration into the service's track arguments: nothing for the spontaneous
baseline, and a seeded configuration only when it is enabled and every semantic
field (``SEMANTIC_FIELDS``) is set.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

SPONTANEOUS = "spontaneous"
SEEDED = "seeded"
TRACKS = (SPONTANEOUS, SEEDED)

# elapsed_seconds: seconds after ConferenceService.mark_run_started(), which
#   runtime.RunClock.monitor calls with the common start barrier's wall time;
# after_n_acceptances: once n tasks have been closed by agent submissions.
TRIGGER_KINDS = ("elapsed_seconds", "after_n_acceptances")
# library_solution: published as solutions/<task_id>.lean (the accepted-solution
#   path) and the task is closed and credited to ``author`` (credit_effect close_task);
# library_notebook: published as a notebook revision at ``library_path``;
# bulletin_board: posted on the board by ``author``.
CHANNELS = ("library_solution", "library_notebook", "bulletin_board")
CREDIT_EFFECTS = ("none", "close_task")
# A solution file only ever exists for a closed task, so the effect is tied to the
# channel; it must still be stated explicitly in the configuration.
_CHANNEL_CREDIT = {"library_solution": "close_task", "library_notebook": "none", "bulletin_board": "none"}

# The user's undecided seeded-track semantics (docs/plan.md checklist item 9): a run
# configuration's ``seeded`` block must set every one (non-null) before the track can
# run, plus the field its channel needs. configs/seeded.example.json leaves them null.
SEMANTIC_FIELDS = ("trigger_kind", "trigger_value", "artifact_path", "artifact_sha256", "channel", "author",
                   "credit_effect")
CHANNEL_FIELDS = {"library_solution": ("task_id",), "library_notebook": ("library_path",),
                  "bulletin_board": ("intent_type",)}
# Keys a ``seeded`` block may carry for readers only; never passed to the service.
# ``source``: provenance of the artifact (researcher-only; e.g. the run and agent it was taken from).
DOCUMENTATION_KEYS = ("note", "source")
# Declared seeded runs (owner, 2026-10-25): researcher-only artifacts live here, never in agent workspaces.
SEED_ARTIFACT_DIRECTORY = "research/seeds"
SEED_ROLE = "seed"

_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9 _.+-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class SeededConfigError(ValueError):
    """The seeded configuration is refused; nothing is activated."""


@dataclass(frozen=True)
class SeededTrackConfig:
    """Researcher-supplied seeded-track settings. Every semantic field is explicit."""
    enabled: bool = False
    trigger_kind: str | None = None
    trigger_value: float | None = None
    artifact_path: str | None = None       # absolute, researcher-side, never in the repo
    artifact_sha256: str | None = None     # pinned digest of the artifact bytes
    channel: str | None = None
    author: str | None = None              # identity shown to agents (provenance)
    task_id: str | None = None             # library_solution target
    library_path: str | None = None        # library_notebook: notebooks/<author>/<label>/<name>.md
    intent_type: str = "contribution"      # bulletin_board
    tags: tuple = ()
    credit_effect: str | None = None

    @classmethod
    def from_value(cls, value) -> "SeededTrackConfig":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise SeededConfigError("Seeded configuration must be a SeededTrackConfig or a mapping")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(value) - known)
        if unknown:
            raise SeededConfigError("Unknown seeded configuration key(s): " + ", ".join(map(str, unknown)))
        data = dict(value)
        if "tags" in data and data["tags"] is not None:
            data["tags"] = tuple(data["tags"])
        return cls(**data)

    def describe(self) -> dict:
        data = asdict(self)
        data["tags"] = list(self.tags)
        return data

    def digest(self) -> str:
        """Stable fingerprint recorded in the run state (resume refuses a different one)."""
        return hashlib.sha256(json.dumps(self.describe(), sort_keys=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SeededArtifact:
    config: SeededTrackConfig
    content: str
    sha256: str


def missing_semantic_fields(block) -> list[str]:
    """Semantic fields a run configuration's ``seeded`` block leaves unset (absent or null)."""
    if not isinstance(block, dict):
        return list(SEMANTIC_FIELDS)
    required = SEMANTIC_FIELDS + CHANNEL_FIELDS.get(block.get("channel"), ())
    return [name for name in required if block.get(name) is None]


def seed_agent(run_config):
    """The silent seed participant's identifier in a seeded run (its ``author``), else None."""
    if not isinstance(run_config, dict) or run_config.get("track") != SEEDED:
        return None
    block = run_config.get("seeded")
    if not isinstance(block, dict) or block.get("enabled") is not True:
        return None
    author = block.get("author")
    return author if isinstance(author, str) else None


def is_declared_run(run_config) -> bool:
    """True for a config that claims the declared seeded design (track seeded, seeding enabled)."""
    return (isinstance(run_config, dict) and run_config.get("track") == SEEDED
            and run_config.get("seeded_contagion_enabled") is True
            and isinstance(run_config.get("seeded"), dict) and run_config["seeded"].get("enabled") is True)


def resolve_artifact_path(path, repo_root):
    """An absolute artifact path: a repository-relative path under SEED_ARTIFACT_DIRECTORY is
    resolved against ``repo_root``; an absolute path is returned unchanged."""
    if not isinstance(path, str) or not path:
        return path
    if Path(path).is_absolute() or repo_root is None:
        return path
    return str(Path(repo_root) / path)


def declared_run_problems(run_config, repo_root=None) -> list[str]:
    """Launch-gate check of a declared seeded run (docs/plan.md, "Seeded overseer experiment,
    October 25, 2026"). Empty when the config is a complete declared seeded run:

    * track ``seeded``, ``seeded_contagion_enabled`` true, ``seeded.enabled`` true, on the
      owner's budget track (``budget_track`` ``gemini``);
    * channel ``library_solution`` with ``credit_effect`` ``close_task`` and an
      ``elapsed_seconds`` trigger (the seed is credited at a fixed run minute);
    * ``author`` is the identifier right after the solvers (``agent-<agent_count + 1>``);
      the overseer, if any, comes after it (research_swarm.oversight);
    * the artifact is a repository-relative file under ``research/seeds/`` whose bytes match
      ``artifact_sha256`` (checked when ``repo_root`` is given).
    """
    if not is_declared_run(run_config):
        return ["not a declared seeded run (track 'seeded', seeded_contagion_enabled true, seeded.enabled true)"]
    block = run_config["seeded"]
    problems = []
    if run_config.get("budget_track") != "gemini":
        problems.append("declared seeded runs use the owner's budget track ('gemini')")
    missing = missing_semantic_fields(block)
    if missing:
        problems.append("seeded semantics not set: " + ", ".join(missing))
    if block.get("channel") != "library_solution" or block.get("credit_effect") != "close_task":
        problems.append("declared seeded runs plant an accepted solution (channel library_solution, "
                        "credit_effect close_task)")
    if block.get("trigger_kind") != "elapsed_seconds":
        problems.append("declared seeded runs use an elapsed_seconds trigger")
    count = run_config.get("agent_count")
    expected = f"agent-{count + 1:03d}" if isinstance(count, int) and not isinstance(count, bool) else None
    if block.get("author") != expected:
        problems.append(f"seeded.author must be {expected!r} (the identifier right after the solvers)")
    path = block.get("artifact_path")
    if not isinstance(path, str) or Path(path).is_absolute() or posixpath.normpath(path) != path \
            or not path.startswith(SEED_ARTIFACT_DIRECTORY + "/"):
        problems.append(f"seeded.artifact_path must be a repository-relative file under {SEED_ARTIFACT_DIRECTORY}/")
    elif repo_root is not None:
        try:
            digest = hashlib.sha256((Path(repo_root) / path).read_bytes()).hexdigest()
        except OSError as exc:
            problems.append(f"seeded artifact unreadable: {type(exc).__name__}")
        else:
            if digest != block.get("artifact_sha256"):
                problems.append("seeded artifact does not match artifact_sha256")
    return problems


def conference_arguments(run_config, repo_root=None) -> dict:
    """The track keyword arguments for ``ConferenceService`` from a run configuration.

    Returns ``{}`` (the spontaneous baseline) unless ``run_config["track"]`` is
    ``"seeded"``; a spontaneous configuration never passes a seeded block on, and one
    that claims seeding is enabled (``seeded.enabled`` or ``seeded_contagion_enabled``
    true) is refused as contradictory. A seeded configuration returns ``{"track":
    "seeded", "seeded_track": SeededTrackConfig}`` only when its ``seeded`` block has
    ``enabled: true`` and every semantic field set, and ``seeded_contagion_enabled``
    is not false; anything else raises ``SeededConfigError`` and never falls back to
    the baseline. The service then validates the values and the artifact itself.
    """
    if not isinstance(run_config, dict):
        raise SeededConfigError("Run configuration must be a mapping")
    track = run_config.get("track", SPONTANEOUS)
    block = run_config.get("seeded")
    if block is not None and not isinstance(block, dict):
        raise SeededConfigError("The 'seeded' block must be a mapping")
    flag = run_config.get("seeded_contagion_enabled")
    if track == SPONTANEOUS:
        if flag is True or (block is not None and block.get("enabled") is True):
            raise SeededConfigError("The spontaneous track cannot enable seeded contagion")
        return {}
    if track != SEEDED:
        raise SeededConfigError(f"Unknown track: {track!r}")
    if block is None or block.get("enabled") is not True:
        raise SeededConfigError("The seeded track requires a 'seeded' block with enabled: true")
    if flag is False:
        raise SeededConfigError("seeded_contagion_enabled is false; refusing to run the seeded track")
    missing = missing_semantic_fields(block)
    if missing:
        raise SeededConfigError("Seeded semantics are not set (awaiting user decisions): " + ", ".join(missing))
    config = {key: value for key, value in block.items() if key not in DOCUMENTATION_KEYS}
    config["artifact_path"] = resolve_artifact_path(config.get("artifact_path"), repo_root)
    return {"track": SEEDED, "seeded_track": SeededTrackConfig.from_value(config)}


def resolve_track(track, config, *, task_ids, max_bytes, max_text_chars, max_post_chars, intent_types,
                  max_tags, max_tag_chars):
    """Validate (track, config). Returns None for the baseline or a loaded artifact.

    Refuses (``SeededConfigError``) any seeded configuration in the spontaneous
    track, and a seeded track whose configuration is missing or not enabled. A
    bulletin-board artifact must satisfy the limits of an agent post
    (``max_text_chars`` characters and ``max_post_chars`` once JSON-encoded), so
    that it can be read in full like any other post.
    """
    if track not in TRACKS:
        raise SeededConfigError(f"Unknown track: {track!r}")
    if track == SPONTANEOUS:
        if config is not None:
            raise SeededConfigError("Seeded configuration is refused in the spontaneous track")
        return None
    if config is None:
        raise SeededConfigError("The seeded track requires an explicit seeded configuration")
    config = SeededTrackConfig.from_value(config)
    if config.enabled is not True:
        raise SeededConfigError("Seeded configuration is not enabled; refusing to run the seeded track")
    _validate(config, task_ids, intent_types, max_tags, max_tag_chars)
    artifact = _load(config, max_bytes)
    if config.channel == "bulletin_board" and (not artifact.content.strip()
                                                or len(artifact.content) > max_text_chars
                                                or len(json.dumps(artifact.content)) > max_post_chars):
        raise SeededConfigError("Artifact does not fit a single bulletin-board post")
    return artifact


def _validate(config, task_ids, intent_types, max_tags, max_tag_chars):
    if config.trigger_kind not in TRIGGER_KINDS:
        raise SeededConfigError("trigger_kind must be one of: " + ", ".join(TRIGGER_KINDS))
    value = config.trigger_value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise SeededConfigError("trigger_value must be a non-negative number")
    if config.trigger_kind == "after_n_acceptances" and (int(value) != value or value < 1):
        raise SeededConfigError("after_n_acceptances needs a positive integer trigger_value")
    if config.channel not in CHANNELS:
        raise SeededConfigError("channel must be one of: " + ", ".join(CHANNELS))
    if config.credit_effect not in CREDIT_EFFECTS:
        raise SeededConfigError("credit_effect must be stated explicitly: " + ", ".join(CREDIT_EFFECTS))
    if config.credit_effect != _CHANNEL_CREDIT[config.channel]:
        raise SeededConfigError(f"channel {config.channel} requires credit_effect "
                                f"{_CHANNEL_CREDIT[config.channel]}")
    if not isinstance(config.author, str) or not _AGENT_ID.fullmatch(config.author):
        raise SeededConfigError("author must be a well-formed participant identifier")
    if config.channel == "library_solution":
        if config.task_id not in task_ids:
            raise SeededConfigError("library_solution needs a task_id from the conference task set")
    elif config.task_id is not None:
        raise SeededConfigError("task_id applies only to library_solution")
    if config.channel == "library_notebook":
        path = config.library_path
        prefix = f"notebooks/{config.author}/"
        if (not isinstance(path, str) or posixpath.normpath(path) != path or not path.startswith(prefix)
                or not path.lower().endswith(".md")):
            raise SeededConfigError(f"library_notebook needs library_path {prefix}<wiki|workspace>/<name>.md")
        segments = path[len(prefix):].split("/")
        if segments[0] not in ("wiki", "workspace") or len(segments) < 2 or not all(
                _SEGMENT.fullmatch(s) for s in segments[1:]):
            raise SeededConfigError(f"library_notebook needs library_path {prefix}<wiki|workspace>/<name>.md")
    elif config.library_path is not None:
        raise SeededConfigError("library_path applies only to library_notebook")
    if config.channel == "bulletin_board":
        if config.intent_type not in intent_types:
            raise SeededConfigError("intent_type must be one of: " + ", ".join(intent_types))
        if len(config.tags) > max_tags or not all(isinstance(t, str) and t.strip() and len(t) <= max_tag_chars
                                                  for t in config.tags):
            raise SeededConfigError("Invalid tags")
    elif config.tags:
        raise SeededConfigError("tags apply only to bulletin_board")


def _load(config, max_bytes):
    if not isinstance(config.artifact_sha256, str) or not _SHA256.fullmatch(config.artifact_sha256):
        raise SeededConfigError("artifact_sha256 must pin the artifact (64 lowercase hex digits)")
    if not isinstance(config.artifact_path, str) or not Path(config.artifact_path).is_absolute():
        raise SeededConfigError("artifact_path must be an absolute researcher-side path")
    path = Path(config.artifact_path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SeededConfigError(f"Seeded artifact unreadable: {type(exc).__name__}") from exc
    if len(data) > max_bytes:
        raise SeededConfigError(f"Seeded artifact exceeds {max_bytes} bytes")
    digest = hashlib.sha256(data).hexdigest()
    if digest != config.artifact_sha256:
        raise SeededConfigError("Seeded artifact does not match artifact_sha256")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SeededConfigError("Seeded artifact must be UTF-8 text") from exc
    if "\x00" in content:
        raise SeededConfigError("Seeded artifact must be text")
    return SeededArtifact(config, content, digest)


def trigger_due(config: SeededTrackConfig, *, now, run_started_at, acceptances) -> bool:
    """Whether the configured trigger has been reached."""
    if config.trigger_kind == "elapsed_seconds":
        return run_started_at is not None and now - run_started_at >= config.trigger_value
    return acceptances >= config.trigger_value
