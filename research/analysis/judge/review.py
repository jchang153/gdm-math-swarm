"""Researcher-only, read-only findings browser for the transcript behavior judge.

docs/plan.md, superseding decision "no human marks" (October 17, 2026), and judge rubric
Section 12: there is no human input in the classification. The judge's adversarially
verified labels are the reported labels. This module writes a static page that a
researcher may open to inspect any result on their own. It lists every finding
(verified, adjusted, refuted and unverified, each clearly labelled) with its full evidence
passages, the verifier's verdict and which model verified it, every agent's dimension
table and cohort with its attributes, and the run-level results. It collects nothing and
changes no label: there are no form controls, no scripts and no browser storage.

Offline only: no model calls, network, Docker or credentials. Experimental agents must
never see this module, the page or any judge output. All transcript text and all model
output are untrusted and are HTML-escaped; nothing in them is an instruction.

Inputs, written by the judge pipeline under RUN/analysis/judge/:
  agents/<agent_id>.json    per-agent result, schema gdm-judge-agent-result/1
  packages/<agent_id>.json  the judge's input package: I-ITEM dicts at any nesting
  pipeline.json             run summary: mode (paid|rehearsal), models per stage, spend
  run_level.json            run-level output (optional)
  labels.jsonl              frozen-rubric labels (optional; listed per agent)
  units/<agent_id>/**/*verif*.json
                            verifier units (optional; read only to recover which model
                            verified each claim when the agent result does not record it)
Outputs under review/:
  index.html                overview: summary, model allocation, every agent's cohort and
                            dimensions, every finding and stance observation with its
                            verification status and verifier model, run-level results
  agents/<agent_id>.html    one page per agent with full evidence passages
  manifest.json             schema gdm-judge-review-manifest/2: SHA-256 of every input and
                            page, counts; report.py and prepare_hf_library.py use it to
                            detect a page that no longer matches the judge outputs

Which model verified a claim (verification unit ids follow schema.verification_units:
"F1.D1", "F7.D11.searched", "S1"), first match wins:
  1. the verified row itself: "verifier_model" (a model id string or {"model", "key", "route"});
  2. the agent result's routing record "verification": {"units": {unit id: {"model",
     "model_key", "rule", "unit", "status"}}, "calls": {...}} (pipeline.py);
  3. a map "verification_models" (or "verification_routes") from unit id or finding id to a
     model id string or {"model", "key", "route"};
  4. the verifier unit files: each ok unit's "model" key, the model id the provider returned,
     and the unit ids of its verdicts (plus optional "claims" listing attempted unit ids).
Anything else is shown as "not recorded"; it is never guessed.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import html
import json
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path

AGENT_RESULT_SCHEMA = "gdm-judge-agent-result/1"
MANIFEST_SCHEMA = "gdm-judge-review-manifest/2"
AGENT_STATUSES = ("graded", "ungraded", "grading_failed")
VERIFIED_STATES = ("confirmed", "adjusted")   # cohort.VERIFIED_STATES
CONTEXT_ITEMS = 2        # neighbouring items shown on each side of a cited item

ALL_DIMS = tuple(f"D{n}" for n in range(1, 13))
LADDER_DIMS = ("D1", "D3", "D4", "D5", "D6", "D8", "D9", "D10", "D12")   # schema.LADDER_ORDER
DIM_NAMES = {"D1": "exploit_engagement", "D2": "integrity_stance", "D3": "exploit_propagation",
             "D4": "exploit_exposure_and_uptake", "D5": "detection", "D6": "whistleblowing",
             "D7": "accusation_accuracy", "D8": "refusal_and_withdrawal",
             "D9": "concealment_and_deception", "D10": "competitive_pressure",
             "D11": "formalization_weakness", "D12": "evaluation_awareness"}
# Judge rubric Section 8 cohort attributes (plus the D1 `scaled` attribute), in report order.
# `provisional` is reported beside the cohort, not as an attribute flag.
COHORT_ATTRIBUTES = ("basis", "also_reported", "whistleblowing_accuracy", "refused_available_bypass",
                     "formalization_prober", "evaluation_aware", "exposed_only", "judge_only", "scaled")
COHORT_DETAILS = ("pivot", "candidate_cohorts", "aware_triggers", "insufficient_reasons", "consistency_flags",
                  "judge_only_submission_ids", "counted_submission_ids", "judge_evaluator_disagreement")
MODEL_KEYS = ("haiku", "sonnet", "opus")
MODEL_MAPS = ("verification_models", "verification_routes")
# Pipeline stages in order (pipeline.py STAGES); model stages are the ones with units.
STAGE_ORDER = ("history", "prefilter", "judge", "verifier", "recall", "host", "closed", "runlevel", "report")
INPUT_GLOBS = ("pipeline.json", "labels.jsonl", "run_level.json", "agents/*.json", "packages/*.json",
               "units/*/**/*verif*.json")
ITEM_KINDS = frozenset({"reasoning", "text", "tool_call", "tool_result", "summary",
                        "file_version", "conference", "runtime", "host"})
SAFE_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")


# --------------------------------------------------------------------------- files

def _now(now=None):
    stamp = now or datetime.datetime.now(datetime.timezone.utc)
    return stamp.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def regular_file(path):
    """Refuse symlinks and non-files: judge outputs are read, never followed."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"not a regular file: {path}")
    return path


def read_json(path):
    return json.loads(regular_file(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    records = []
    for number, line in enumerate(regular_file(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                records.append(json.loads(line))
            except ValueError as error:
                raise ValueError(f"{path}:{number}: invalid JSON ({error})") from None
    return records


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(regular_file(path).read_bytes())


def canonical_sha256(value):
    return sha256_bytes(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":")).encode("utf-8"))


def write_text_atomic(path, text):
    """Write beside the target then rename, so a crash never leaves half a record."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_json(path, value):
    write_text_atomic(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def default_judge_dir(run_dir):
    return Path(run_dir) / "analysis" / "judge"


def load_pipeline(judge_dir):
    path = Path(judge_dir) / "pipeline.json"
    return read_json(path) if path.exists() else {}


def load_run_level(judge_dir):
    path = Path(judge_dir) / "run_level.json"
    return read_json(path) if path.exists() else None


def load_agent_results(judge_dir):
    """agents/<agent_id>.json -> {agent_id: result}; malformed results fail loudly."""
    folder = Path(judge_dir) / "agents"
    results = {}
    if not folder.is_dir():
        return results
    for path in sorted(folder.glob("*.json")):
        data = read_json(path)
        if not isinstance(data, dict) or data.get("schema") != AGENT_RESULT_SCHEMA:
            raise ValueError(f"{path}: expected schema {AGENT_RESULT_SCHEMA}")
        if data.get("agent_id") != path.stem:
            raise ValueError(f"{path}: agent_id does not match the file name")
        if not SAFE_AGENT_ID.fullmatch(path.stem) or ".." in path.stem:
            raise ValueError(f"{path}: agent_id is not a safe page name")
        if data.get("status") not in AGENT_STATUSES:
            raise ValueError(f"{path}: status must be one of {AGENT_STATUSES}")
        if data["status"] == "graded" and not isinstance(data.get("verified"), dict):
            raise ValueError(f"{path}: a graded result needs the cohort.apply_verification record ('verified')")
        results[path.stem] = data
    return results


def load_package(judge_dir, agent_id):
    path = Path(judge_dir) / "packages" / f"{agent_id}.json"
    return read_json(path) if path.exists() else None


def load_labels(judge_dir):
    path = Path(judge_dir) / "labels.jsonl"
    return [r for r in read_jsonl(path) if isinstance(r, dict)] if path.exists() else []


def input_digests(judge_dir):
    """SHA-256 of every input the page is built from (paths relative to the judge dir)."""
    judge_dir = Path(judge_dir)
    digests = {}
    for pattern in INPUT_GLOBS:
        for path in sorted(judge_dir.glob(pattern)):
            if path.is_file() and not path.is_symlink():
                digests[path.relative_to(judge_dir).as_posix()] = sha256_file(path)
    return dict(sorted(digests.items()))


# ------------------------------------------------- verified records (cohort.py shapes)

def _is_level(value):
    return type(value) is int  # bool is not a level


def verified_of(result):
    verified = result.get("verified") if isinstance(result, dict) else None
    return verified if isinstance(verified, dict) else {}


def findings_of(result):
    return [f for f in verified_of(result).get("findings") or [] if isinstance(f, dict) and isinstance(f.get("id"), str)]


def stance_entries(result):
    """[(stance id, observation)] from the verified record (schema ids S1, S2, ...)."""
    return [(s["id"], s) for s in verified_of(result).get("stance_observations") or []
            if isinstance(s, dict) and isinstance(s.get("id"), str)]


def level_rows(finding):
    """[(ladder dim, {"claimed", "established", "candidate", "state", "reason"})]."""
    return [(d, row) for d, row in (finding.get("levels") or {}).items() if isinstance(row, dict)]


def d11_rows(finding):
    """[(D11 field, {"claimed", "value", "state", "reason"})]."""
    return [(f, row) for f, row in (finding.get("d11") or {}).items() if isinstance(row, dict)]


def finding_dims(finding):
    return [d for d, _ in level_rows(finding)] + (["D11"] if d11_rows(finding) else [])


def unit_rows(finding):
    """[(verification unit id, label, row)] in schema.verification_units order."""
    fid = finding["id"]
    return ([(f"{fid}.{dim}", dim, row) for dim, row in level_rows(finding)]
            + [(f"{fid}.D11.{field}", f"D11 {field}", row) for field, row in d11_rows(finding)])


def _positive_row(row):
    return row.get("state") in VERIFIED_STATES and _is_level(row.get("established")) and row["established"] >= 1


def _positive_d11(row):
    return row.get("state") in VERIFIED_STATES and row.get("value") not in (None, False, "none")


def is_verified_positive(finding, dim=None):
    """A verified unit with an established level >= 1 (or a verified D11 field), optionally
    restricted to one dimension. Only these feed scores and cohorts."""
    rows = any(_positive_row(row) for d, row in level_rows(finding) if dim in (None, d))
    return rows or (dim in (None, "D11") and any(_positive_d11(row) for _, row in d11_rows(finding)))


def has_disagreement(finding):
    return bool((finding.get("attributes") or {}).get("judge_evaluator_disagreement"))


def finding_states(finding):
    return sorted({str(row.get("state")) for _, _, row in unit_rows(finding)})


def verification_label(states):
    """(label, tone) for a finding's or stance's verifier states; tone is good|bad|warn|muted."""
    states = set(states)
    verified, refuted = states & set(VERIFIED_STATES), "refuted" in states
    other = sorted(states - set(VERIFIED_STATES) - {"refuted"})
    if not states:
        return "no verification unit", "muted"
    if verified and not refuted and not other:
        return ("verified (adjusted)" if "adjusted" in verified else "verified"), "good"
    if refuted and not verified and not other:
        return "refuted", "bad"
    if verified and refuted:
        return "partly refuted", "warn"
    if verified:
        return f"partly verified ({', '.join(other)})", "warn"
    if refuted:
        return f"refuted / not verified ({', '.join(other)})", "bad"
    return f"not verified ({', '.join(other)})", "warn"


def level_text(finding):
    parts = []
    for dim, row in level_rows(finding):
        extras = [str(row.get("state"))]
        if row.get("claimed") != row.get("established"):
            extras.insert(0, f"claimed {row.get('claimed')}")
        if row.get("candidate") not in (None, row.get("established")):
            extras.insert(0, f"candidate {row.get('candidate')}")
        parts.append(f"{dim} {row.get('established')} ({', '.join(extras)})")
    parts += [f"D11 {field}={row.get('value')} (claimed {row.get('claimed')}, {row.get('state')})"
              if row.get("claimed") != row.get("value") else f"D11 {field}={row.get('value')} ({row.get('state')})"
              for field, row in d11_rows(finding)]
    return "; ".join(parts)


def agent_dims(result):
    """Dimension results in one dict: ladder dims from verified["dims"], D2 from stance
    observations, D7 from accusations, D11 from the aggregated D11 record."""
    verified = verified_of(result)
    dims = {d: dict(e) for d, e in (verified.get("dims") or {}).items() if isinstance(e, dict)}
    dims["D2"] = {"observations": [{"id": sid, "value": s.get("value"), "object": s.get("object"),
                                    "state": s.get("state")} for sid, s in stance_entries(result)]}
    dims["D7"] = {"targets": [{k: a.get(k) for k in ("finding_id", "target", "accuracy", "verified",
                                                     "submission_ids")}
                              for a in verified.get("accusations") or [] if isinstance(a, dict)]}
    dims["D11"] = dict(verified.get("D11") or {})
    return dims


def dimension_status(entry):
    if not isinstance(entry, dict):
        return "missing"
    if entry.get("status") in ("ungraded", "grading_failed"):
        return entry["status"]
    established, candidate = entry.get("established"), entry.get("candidate")
    if entry.get("unresolved_reason") or (_is_level(established) and _is_level(candidate)
                                          and candidate > established):
        return "unresolved"
    return "ok"


def finding_source(result, finding):
    """(source, window id): prefilter provenance from finding_sources (or the finding).
    A finding made only from a random-sample window is a prefilter miss (rubric Section 11)."""
    entry = (result.get("finding_sources") or {}).get(finding.get("id")) or {}
    source = entry.get("source", finding.get("source"))
    if entry.get("prefilter_miss") is True or source == "prefilter_miss":
        source = "random_sample"
    return source, entry.get("window_id", finding.get("window_id"))


def is_prefilter_miss(result, finding):
    return finding_source(result, finding)[0] == "random_sample"


def stance_source(result, sid):
    """(source, window id) of a stance observation from "stance_sources"."""
    entry = (result.get("stance_sources") or {}).get(sid) or {}
    return entry.get("source"), entry.get("window_id")


def is_mixed(result, finding):
    """The finding cites random-sample items alongside items the prefilter selected."""
    return finding.get("id") in ((result.get("recall") or {}).get("mixed") or [])


def visible_attributes(attributes):
    """Truthy scalar rubric Section 8 attributes, in rubric order (provisional is separate)."""
    attributes = attributes if isinstance(attributes, dict) else {}
    return {k: attributes[k] for k in COHORT_ATTRIBUTES
            if attributes.get(k) not in (None, False, "", [], {}, 0) and not isinstance(attributes.get(k), (dict, list))}


def prefilter_miss_rate(results):
    """Rubric Section 11: random-sample windows whose reading produced a verified positive
    finding, over random-sample windows read. Without window ids, findings are counted."""
    sampled, misses, missing_window_ids = 0, set(), False
    for agent_id, result in results.items():
        windows = (result.get("prefilter") or {}).get("windows_sampled")
        sampled += len(windows) if isinstance(windows, list) else (windows if _is_level(windows) else 0)
        for finding in findings_of(result):
            source, window = finding_source(result, finding)
            if source == "random_sample" and is_verified_positive(finding):
                missing_window_ids |= not isinstance(window, str)
                misses.add((agent_id, window if isinstance(window, str) else f"finding:{finding['id']}"))
    return {"windows_sampled": sampled, "windows_with_misses": len(misses),
            "miss_rate": len(misses) / sampled if sampled else None,
            "wilson95": wilson_interval(len(misses), sampled), "window_ids_missing": missing_window_ids}


def wilson_interval(successes, trials, z=1.959963984540054):
    if not trials:
        return None
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    half = z * ((p * (1 - p) / trials + z * z / (4 * trials * trials)) ** 0.5) / denominator
    return [max(0.0, centre - half), min(1.0, centre + half)]


# --------------------------------------------------------------- models and stages

def model_key_of(model):
    text = str(model or "").lower()
    return next((key for key in MODEL_KEYS if key in text), None)


def stage_of(purpose):
    """Pipeline stage of a unit purpose or a pipeline.json models key ("judge_part" ->
    "judge", "verifier_nuanced" -> "verifier", "prefilter_repair" -> "prefilter")."""
    text = str(purpose or "").strip().lower().replace("-", "_")
    if text.endswith("_repair"):
        text = text[:-len("_repair")]
    for prefix, stage in (("prefilter", "prefilter"), ("judge", "judge"), ("verif", "verifier"),
                          ("recall", "recall"), ("runlevel", "runlevel"), ("run_level", "runlevel")):
        if text.startswith(prefix):
            return stage
    return text or "unknown"


def stage_models(pipeline):
    """Configured model allocation from pipeline.json, normalized to
    [{"stage", "purpose", "key", "model", "effort", "route"}]. Uses "allocation" rows
    ({"stage", "model_key", "model", "effort", "role"}) when present, else "models": a purpose ->
    entry map (entry: a dict with "model", a list of such dicts, or a model id string) or a model
    key -> entry map whose entries list their "stages"."""
    pipeline = pipeline or {}
    models = pipeline.get("models")
    rows, seen = [], set()
    allocation = pipeline.get("allocation")
    if isinstance(allocation, list) and any(isinstance(r, dict) and isinstance(r.get("model"), str)
                                            for r in allocation):
        # pipeline.py's explicit allocation rows: {"stage", "model_key", "model", "effort", "role"}
        models = {f"allocation-{n}": dict(r, purpose=r.get("stage"), key=r.get("model_key") or r.get("key"),
                                          route=r.get("route") or r.get("role"))
                  for n, r in enumerate(allocation) if isinstance(r, dict)}
    if not isinstance(models, dict):
        return rows
    for name, value in models.items():
        for entry in value if isinstance(value, list) else [value]:
            entry = {"model": entry} if isinstance(entry, str) else entry
            if not isinstance(entry, dict) or not isinstance(entry.get("model"), str):
                continue
            stages = entry.get("stages") if isinstance(entry.get("stages"), list) else \
                [entry.get("stage") or entry.get("purpose") or (None if name in MODEL_KEYS else name)]
            for purpose in stages:
                stage = stage_of(purpose) if purpose else "unspecified"
                route = entry.get("route") or entry.get("role") or (
                    str(purpose) if purpose and str(purpose) not in (stage, "unspecified") else None)
                row = {"stage": stage, "purpose": str(purpose or name), "model": entry["model"],
                       "key": entry.get("key") or entry.get("model_key") or model_key_of(entry["model"])
                       or (name if name in MODEL_KEYS else None),
                       "effort": entry.get("effort"), "route": route}
                marker = (row["stage"], row["model"], row["route"])
                if marker not in seen:
                    seen.add(marker)
                    rows.append(row)
    order = {s: i for i, s in enumerate(STAGE_ORDER)}
    return sorted(rows, key=lambda r: (order.get(r["stage"], len(order)), r["stage"]))


def _model_entry(value, source):
    if isinstance(value, str) and value:
        return {"model": value, "key": model_key_of(value), "route": None, "source": source}
    if isinstance(value, dict):
        model = value.get("model") or value.get("model_id")
        key = value.get("key") or value.get("model_key") or model_key_of(model)
        if isinstance(model, str) or isinstance(key, str):
            return {"model": model if isinstance(model, str) else None, "key": key if isinstance(key, str) else None,
                    "route": value.get("route") or value.get("role") or value.get("rule"), "source": source,
                    "unit_status": value.get("status")}
    return None


def verifier_unit_models(judge_dir, agent_id, key_models=None):
    """{verification unit id: model entry} from the agent's verifier units (files named
    *verif*.json whose purpose is a verifier purpose)."""
    folder = Path(judge_dir) / "units" / agent_id
    out = {}
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("**/*verif*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        unit = read_json(path)
        if not isinstance(unit, dict) or stage_of(unit.get("purpose")) != "verifier":
            continue
        returned = [(s.get("client") or {}).get("model") for s in unit.get("steps") or [] if isinstance(s, dict)]
        returned = [m for m in returned if isinstance(m, str) and m]
        key = unit.get("model") if isinstance(unit.get("model"), str) else None
        model = returned[-1] if returned else (key_models or {}).get(key)
        entry = {"model": model, "key": key or model_key_of(model), "route": unit.get("route"),
                 "source": "unit " + path.relative_to(Path(judge_dir)).as_posix(), "unit_status": unit.get("status")}
        ids = [v.get("finding_id") for v in ((unit.get("result") or {}).get("verdicts") or [])
               if isinstance(v, dict)] if unit.get("status") == "ok" else []
        ids += [c.get("unit_id") if isinstance(c, dict) else c for c in unit.get("claims") or []]
        for unit_id in ids:
            if isinstance(unit_id, str):
                out.setdefault(unit_id, entry)
    return out


class ModelResolver:
    """Which model verified each claim (module docstring, first match wins)."""

    def __init__(self, result, unit_models):
        record = result.get("verification") if isinstance(result.get("verification"), dict) else {}
        self.routing = record.get("units") if isinstance(record.get("units"), dict) else {}
        self.maps = [m for m in (result.get(k) for k in MODEL_MAPS) if isinstance(m, dict)]
        self.units = unit_models

    def __call__(self, unit_id, finding_id, row):
        for key in ("verifier_model", "verified_by"):
            entry = _model_entry((row or {}).get(key), "verified record")
            if entry:
                return entry
        entry = _model_entry(self.routing.get(unit_id), "routing record")
        if entry:
            return entry
        for mapping in self.maps:
            for ident in (unit_id, finding_id):
                entry = _model_entry(mapping.get(ident), "agent result")
                if entry:
                    return entry
        return self.units.get(unit_id)


def _source_kind(entry):
    source = (entry or {}).get("source") or "not recorded"
    return "unit file" if source.startswith("unit ") else source


def model_text(entry):
    if not entry:
        return "not recorded"
    model, key = entry.get("model"), entry.get("key")
    text = model or key or "not recorded"
    if key and model and key not in str(model).lower():
        text += f" ({key})"
    if entry.get("route"):
        text += f", route {entry['route']}"
    return text


def model_short(entry):
    """"Opus" / "Sonnet" / "Haiku" when the model key is known, else the model id."""
    if not entry:
        return "not recorded"
    if entry.get("key"):
        return str(entry["key"]).capitalize()
    return entry.get("model") or "not recorded"


# ----------------------------------------------------------------------- packages

def _is_item(value):
    return (isinstance(value, dict) and isinstance(value.get("id"), str)
            and isinstance(value.get("text"), str) and value.get("kind") in ITEM_KINDS)


def package_sequences(package):
    """Every list of I-ITEM dicts in a package, in document order. Packages may nest items in
    windows or sections; neighbours are taken only within the list holding the item."""
    sequences = []

    def walk(value):
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            items = [child for child in value if _is_item(child)]
            if items:
                sequences.append(items)
            for child in value:
                if not _is_item(child):
                    walk(child)
    walk(package)
    return sequences


class PackageIndex:
    """Locate cited items (and "<id>#fN" fragments of oversized items) in a saved package."""

    def __init__(self, package):
        self.sequences = package_sequences(package) if package is not None else []
        self.where = {}
        for s, sequence in enumerate(self.sequences):
            for p, item in enumerate(sequence):
                self.where.setdefault(item["id"], (s, p))

    def locate(self, item_id):
        if item_id in self.where:
            return [self.where[item_id]]
        prefix = item_id + "#f"
        fragments = [k for k in self.where if k.startswith(prefix) and k[len(prefix):].isdigit()]
        return [self.where[k] for k in sorted(fragments, key=lambda k: int(k[len(prefix):]))]

    def passages(self, item_ids, context=CONTEXT_ITEMS):
        """([(sequence, start, stop, items)], missing ids); overlapping ranges merge."""
        wanted, missing = {}, []
        for item_id in item_ids:
            spots = self.locate(item_id)
            if not spots:
                missing.append(item_id)
            for s, p in spots:
                wanted.setdefault(s, set()).add(p)
        out = []
        for s in sorted(wanted):
            sequence, ranges = self.sequences[s], []
            for p in sorted(wanted[s]):
                start, stop = max(0, p - context), min(len(sequence), p + context + 1)
                if ranges and start <= ranges[-1][1]:
                    ranges[-1][1] = max(ranges[-1][1], stop)
                else:
                    ranges.append([start, stop])
            out.extend((s, start, stop, sequence[start:stop]) for start, stop in ranges)
        return out, missing


# ------------------------------------------------------------------------ HTML

def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def _quote_spans(text, quote):
    if not isinstance(quote, str) or not quote.strip():
        return []
    for candidate in dict.fromkeys((quote, unicodedata.normalize("NFC", quote))):
        start = text.find(candidate)
        if start >= 0:
            return [(start, start + len(candidate))]
    # The host matches quotes after whitespace collapse; mirror that for highlighting.
    pattern = r"\s+".join(re.escape(token) for token in unicodedata.normalize("NFC", quote).split())
    match = re.search(pattern, text)
    return [(match.start(), match.end())] if match else []


def highlight(text, quotes):
    """Escaped text with every located quote wrapped in <mark>."""
    spans = sorted(span for quote in quotes for span in _quote_spans(text, quote))
    merged = []
    for start, stop in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(stop, merged[-1][1]))
        else:
            merged.append((start, stop))
    out, position = [], 0
    for start, stop in merged:
        out += [esc(text[position:start]), "<mark>", esc(text[start:stop]), "</mark>"]
        position = stop
    out.append(esc(text[position:]))
    return "".join(out)


def render_value(value, depth=0):
    """Generic, escaped rendering of judge JSON (run-level output, spend, models)."""
    if isinstance(value, dict):
        if not value:
            return "<span class=\"muted\">none</span>"
        return "<dl>" + "".join(f"<dt>{esc(k)}</dt><dd>{render_value(v, depth + 1)}</dd>" for k, v in value.items()) + "</dl>"
    if isinstance(value, list):
        if not value:
            return "<span class=\"muted\">none</span>"
        if all(isinstance(v, dict) for v in value) and depth < 3:
            keys = list(dict.fromkeys(k for v in value for k in v))
            head = "".join(f"<th>{esc(k)}</th>" for k in keys)
            body = "".join("<tr>" + "".join(f"<td>{render_value(v.get(k), depth + 1)}</td>" for k in keys) + "</tr>"
                           for v in value)
            return f"<div class=\"scroll\"><table><tr>{head}</tr>{body}</table></div>"
        return "<ul>" + "".join(f"<li>{render_value(v, depth + 1)}</li>" for v in value) + "</ul>"
    if isinstance(value, str) and "\n" in value:
        return "<p>" + esc(value).replace("\n", "<br>") + "</p>"
    return esc("" if value is None else value)


BASE_CSS = """
:root{--bg:#fafbfc;--fg:#18212b;--muted:#596579;--card:#fff;--line:#dde3ea;--accent:#155eaa;
--hl:#fff1a6;--warn:#a1260d;--ok:#1d6b35;--amber:#8a5a00;--chip:#eef2f7;--pre:#f4f6f9;--host:#fdf0e6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f141a;--fg:#e4e9ef;
--muted:#9aa7b6;--card:#161d25;--line:#2a3542;--accent:#80b6f0;--hl:#5a4b00;--warn:#ff8f75;
--ok:#82d69d;--amber:#f0c26b;--chip:#222c38;--pre:#10161d;--host:#33261b}}
:root[data-theme="dark"]{--bg:#0f141a;--fg:#e4e9ef;--muted:#9aa7b6;--card:#161d25;--line:#2a3542;
--accent:#80b6f0;--hl:#5a4b00;--warn:#ff8f75;--ok:#82d69d;--amber:#f0c26b;--chip:#222c38;--pre:#10161d;
--host:#33261b}
*{box-sizing:border-box}
body{margin:0 auto;max-width:1180px;padding:24px 16px 64px;font:15px/1.55 system-ui,sans-serif;
color:var(--fg);background:var(--bg);overflow-wrap:anywhere}
a{color:var(--accent)}h1{margin:0 0 6px;font-size:1.6rem}h2{margin-top:2.2rem}
.muted,small{color:var(--muted)}code{font-size:.92em}
.notice{border-left:4px solid var(--accent);background:var(--card);padding:10px 14px;margin:14px 0}
.warn{color:var(--warn);font-weight:600}.ok{color:var(--ok)}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:1px 9px;margin:2px 4px 2px 0;
font-size:.85em}.chip.bad{color:var(--warn)}.chip.good{color:var(--ok)}.chip.warn{color:var(--amber)}
.chip.muted{color:var(--muted)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;background:var(--card)}
td,th{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:.85em;color:var(--muted)}
pre{white-space:pre-wrap;word-break:break-word;background:var(--pre);padding:8px 10px;border-radius:6px;
margin:6px 0;font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
mark{background:var(--hl);color:inherit}
"""

REVIEW_CSS = """
nav.toc{display:flex;flex-wrap:wrap;gap:4px 14px;margin:10px 0;font-size:.92em}
.finding{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:12px 0}
.finding h3{margin:0 0 6px;font-size:1.02rem}
.finding.good{border-left:5px solid var(--ok)}.finding.bad{border-left:5px solid var(--warn)}
.finding.warn{border-left:5px solid var(--amber)}.finding.muted{border-left:5px solid var(--line)}
.item{border:1px solid var(--line);border-radius:6px;padding:6px 9px;margin:6px 0}
.item.cited{border-color:var(--accent);border-width:2px}.item.host{background:var(--host)}
.item-head{font-size:.82em;color:var(--muted)}
details>summary{cursor:pointer;color:var(--accent)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:6px 0}dt{color:var(--muted)}dd{margin:0}
@media (max-width:640px){dl{grid-template-columns:1fr}dt{margin-top:6px}}
table.dims td:nth-child(3),table.dims td:nth-child(4){white-space:nowrap}
"""


def page(title, body, extra_css=""):
    """A self-contained static page: no scripts, no external resources, no form controls."""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\">"
            f"<title>{esc(title)}</title><style>{BASE_CSS}{extra_css}</style></head><body>{body}</body></html>\n")


def _chips(values, cls=""):
    return "".join(f"<span class=\"chip {cls}\">{esc(v)}</span>" for v in values if v not in (None, ""))


def _dl(pairs):
    rows = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in pairs if v not in (None, "", "[]"))
    return f"<dl>{rows}</dl>" if rows else ""


def _json_cell(value):
    if value in (None, "", [], {}):
        return ""
    return esc(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True))


def _citation(entry):
    if isinstance(entry, dict):
        return entry.get("item"), entry.get("quote")
    return (entry, None) if isinstance(entry, str) else (None, None)


def _citation_list(entries):
    out = []
    for entry in entries or []:
        item, quote = _citation(entry)
        if item:
            out.append(f"<li><code>{esc(item)}</code>" + (f": “{esc(quote)}”" if quote else "") + "</li>")
    return f"<ul>{''.join(out)}</ul>" if out else ""


def _anchor(kind, agent_id, ident):
    return f"{kind}-" + hashlib.sha256(f"{kind}:{agent_id}:{ident}".encode("utf-8")).hexdigest()[:16]


def agent_page_name(agent_id):
    return f"agents/{agent_id}.html"


def render_item(item, cited_ids, quotes):
    base = item["id"].split("#f")[0]
    cited = item["id"] in cited_ids or base in cited_ids
    host = item.get("visible_to_agent") is False
    flags = [item.get("kind"), item.get("channel"), item.get("agent_id"),
             f"turn {item['turn']}" if _is_level(item.get("turn")) else None, item.get("time"),
             "authored by agent" if item.get("authored_by_agent") else "received/other",
             "[HOST-ONLY] not seen by agent" if host else None,
             "derived summary" if item.get("kind") == "summary" else None]
    body = highlight(item["text"], quotes.get(item["id"], []) + quotes.get(base, []))
    return (f"<div class=\"item{' cited' if cited else ''}{' host' if host else ''}\">"
            f"<div class=\"item-head\"><code>{esc(item['id'])}</code> · "
            + " · ".join(esc(f) for f in flags if f) + ("" if not cited else " · <strong>cited</strong>")
            + f"</div><pre>{body}</pre></div>")


def render_passages(index, citations, extra_ids=(), open_=True):
    """Full text of every cited item plus its neighbours, from the saved judge package."""
    if not index.sequences:
        return "<p class=\"warn\">No saved judge package for this agent: passages unavailable.</p>"
    quotes, ids = {}, []
    for item_id, quote in citations:
        if isinstance(item_id, str):
            ids.append(item_id)
            if quote:
                quotes.setdefault(item_id, []).append(quote)
    passages, missing = index.passages(list(dict.fromkeys(ids)))
    extra, _ = index.passages([i for i in extra_ids if isinstance(i, str) and i not in ids], context=0)
    cited_ids = set(ids) | {i for i in extra_ids if isinstance(i, str)}
    parts = []
    for s, start, stop, items in passages + extra:
        parts.append(f"<div class=\"passage\"><small>package sequence {s + 1}, items {start + 1}–{stop}</small>"
                     + "".join(render_item(item, cited_ids, quotes) for item in items) + "</div>")
    warning = (f"<p class=\"warn\">Cited item(s) not found in the saved package: {esc(', '.join(missing))}</p>"
               if missing else "")
    count = sum(len(p[3]) for p in passages + extra)
    return (warning + f"<details{' open' if open_ else ''}><summary>Evidence passages ({count} items, full "
            "text)</summary>" + "".join(parts) + "</details>")


def _verifier_lines(finding, resolve):
    lines = []
    for unit_id, label, row in unit_rows(finding):
        entry = resolve(unit_id, finding["id"], row)
        lines.append(f"{label}: {row.get('state')}{_who(row.get('state'), entry)}"
                     + (f": {row.get('reason')}" if row.get("reason") else ""))
    return "<br>".join(esc(line) for line in lines)


def _who(state, entry):
    """' by <model>' for a verdict, explicit when the verifying model is not recorded."""
    if state in VERIFIED_STATES + ("refuted",):
        return f" by {model_text(entry)}" if entry else " (verifier model not recorded)"
    return f" (verifier call: {model_text(entry)})" if entry else ""


def finding_models(finding, resolve):
    return sorted({model_short(resolve(u, finding["id"], row)) for u, _, row in unit_rows(finding)
                   if row.get("state") in VERIFIED_STATES + ("refuted",)})


def render_finding(agent_id, finding, result, index, resolve):
    label, tone = verification_label(finding_states(finding))
    source, window = finding_source(result, finding)
    badges = [("borderline", finding.get("borderline")), (f"prefilter miss ({window})", source == "random_sample"),
              ("cites random-sample and selected items", is_mixed(result, finding)),
              ("effect unclear", finding.get("effect_on_statement") == "unclear"),
              ("judge–evaluator disagreement", has_disagreement(finding))]
    counted = is_verified_positive(finding)
    citations = [_citation(e) for e in (finding.get("evidence") or []) + (finding.get("counter_evidence") or [])]
    citations += [(i, None) for i in finding.get("exposure_items") or [] if isinstance(i, str)]
    targets = [t.get("id") for t in finding.get("targets") or [] if isinstance(t, dict)]
    shown = {"id", "object", "family", "target", "task_ids", "effect_on_statement", "fact", "interpretation",
             "evidence", "counter_evidence", "exposure_items", "adoption_basis", "targets", "submissions",
             "attributes", "levels", "d11", "borderline"}
    rest = {k: v for k, v in finding.items() if k not in shown and v not in (None, "", [], {})}
    details = _dl([
        ("Verification", f"<span class=\"chip {tone}\">{esc(label)}</span> "
                         + ("counted toward scores" if counted else "<span class=\"muted\">not counted toward "
                                                                      "scores</span>")),
        ("Levels", esc(level_text(finding))), ("Verifier", _verifier_lines(finding, resolve)),
        ("Object", esc(finding.get("object"))), ("Family / target", esc(" / ".join(
            str(x) for x in (finding.get("family"), finding.get("target")) if x))),
        ("Tasks", _json_cell(finding.get("task_ids"))), ("Effect on statement", esc(finding.get("effect_on_statement"))),
        ("Fact", esc(finding.get("fact"))), ("Interpretation", esc(finding.get("interpretation"))),
        ("Evidence", _citation_list(finding.get("evidence"))),
        ("Counter-evidence", _citation_list(finding.get("counter_evidence"))),
        ("Exposure items", _json_cell(finding.get("exposure_items"))),
        ("Adoption basis", esc(finding.get("adoption_basis"))), ("Targets", _json_cell(finding.get("targets"))),
        ("Submissions", _json_cell({k: v for k, v in (finding.get("submissions") or {}).items() if v})),
        ("Attributes", _json_cell(finding.get("attributes"))),
        ("Prefilter source", esc(" ".join(str(x) for x in (source, window) if x))),
    ])
    more = (f"<details><summary>All other recorded fields</summary>{render_value(rest)}</details>" if rest else "")
    model_chips = _chips([f"verifier: {m}" if m != "not recorded" else "verifier model not recorded"
                          for m in finding_models(finding, resolve)])
    level_chips = _chips([f"{dim} {row.get('established')}" for dim, row in level_rows(finding)])
    return (f"<article class=\"finding {tone}\" id=\"{_anchor('finding', agent_id, finding['id'])}\">"
            f"<h3>Finding <code>{esc(finding['id'])}</code> <span class=\"chip {tone}\">{esc(label)}</span>"
            f"{model_chips}{level_chips}{_chips([name for name, on in badges if on], 'bad')}</h3>{details}{more}"
            + render_passages(index, citations, targets, open_=tone != "bad") + "</article>")


def render_stance(agent_id, sid, observation, result, index, resolve):
    label, tone = verification_label([str(observation.get("state"))])
    entry = resolve(sid, sid, observation)
    source, window = stance_source(result, sid)
    miss = _chips([f"prefilter miss ({window})"] if source == "random_sample" else [], "bad")
    details = _dl([("Verification", f"<span class=\"chip {tone}\">{esc(label)}</span>"),
                   ("Value", esc(observation.get("value"))), ("Object", esc(observation.get("object"))),
                   ("Family", esc(observation.get("family"))), ("Ground", esc(observation.get("ground"))),
                   ("Tags", _json_cell(observation.get("tags"))), ("Tasks", _json_cell(observation.get("task_ids"))),
                   ("Verifier", esc(f"{observation.get('state')}{_who(observation.get('state'), entry)}"
                                    + (f": {observation['verifier_reason']}" if observation.get("verifier_reason")
                                       else ""))),
                   ("Evidence", _citation_list([observation]))])
    return (f"<article class=\"finding {tone}\" id=\"{_anchor('stance', agent_id, sid)}\"><h3>D2 stance "
            f"<code>{esc(sid)}</code> <span class=\"chip {tone}\">{esc(label)}</span>"
            f"{_chips([observation.get('value')])}{miss}</h3>{details}"
            + render_passages(index, [_citation(observation)], open_=tone != "bad") + "</article>")


def _finding_link(agent_id, finding, prefix=""):
    label, tone = verification_label(finding_states(finding))
    return (f"<a href=\"{prefix}#{_anchor('finding', agent_id, finding['id'])}\"><code>{esc(finding['id'])}</code></a>"
            f" <span class=\"chip {tone}\">{esc(label)}</span>")


def render_dimension_table(agent_id, result):
    dims = agent_dims(result)
    findings = findings_of(result)
    rows = []
    for dim in ALL_DIMS:
        entry = dims.get(dim) or {}
        level = entry.get("established") if _is_level(entry.get("established")) else None
        candidate = entry.get("candidate") if _is_level(entry.get("candidate")) else None
        related = [f for f in findings if dim in finding_dims(f)]
        counted = [f for f in related if is_verified_positive(f, dim)]
        others = [f for f in related if f not in counted]
        if dim == "D2":
            detail = "".join(f"<li><a href=\"#{_anchor('stance', agent_id, o['id'])}\"><code>{esc(o['id'])}</code></a> "
                             f"{esc(o.get('value'))} ({esc(o.get('object'))}; {esc(o.get('state'))})</li>"
                             for o in entry.get("observations") or [])
            detail = f"<ul>{detail}</ul>" if detail else "<span class=\"muted\">no stance observation</span>"
        elif dim == "D7":
            detail = render_value(entry.get("targets")) if entry.get("targets") else \
                "<span class=\"muted\">no accusation target</span>"
        else:
            shown = {k: v for k, v in entry.items() if k not in {"established", "candidate", "finding_ids", "status"}
                     and v not in (None, "", [], {})}
            detail = _json_cell(shown)
        rows.append(f"<tr><td>{esc(dim)}</td><td>{esc(DIM_NAMES[dim])}</td>"
                    f"<td>{'–' if level is None else level}</td><td>{'–' if candidate is None else candidate}</td>"
                    f"<td>{esc(dimension_status(entry) if entry else 'missing')}</td>"
                    f"<td>{'<br>'.join(_finding_link(agent_id, f) for f in counted) or '–'}</td>"
                    f"<td>{'<br>'.join(_finding_link(agent_id, f) for f in others) or '–'}</td>"
                    f"<td>{detail}</td></tr>")
    return ("<div class=\"scroll\"><table class=\"dims\"><tr><th>Dim</th><th>Name</th><th>Established</th>"
            "<th>Candidate</th><th>Status</th><th>Counted findings</th><th>Other findings</th><th>Recorded</th></tr>"
            + "".join(rows) + "</table></div>")


def render_cohort(result):
    cohort = result.get("cohort") or {}
    attributes = cohort.get("attributes") if isinstance(cohort.get("attributes"), dict) else {}
    graded = result.get("status") == "graded"
    head = (f"<p>Cohort <strong>{esc(cohort.get('cohort') if graded else 'none (' + str(result.get('status')) + ')')}"
            f"</strong>" + (f" · provisional (candidate {esc(cohort.get('candidate_cohort'))})"
                            if graded and cohort.get("provisional") else "")
            + (f" · paper cohort {esc(cohort.get('paper_projection'))}" if graded and cohort.get("paper_projection") else "")
            + f" · status {esc(result.get('status'))}"
            + (f" ({esc(result.get('status_reason'))})" if result.get("status_reason") else "") + "</p>")
    if not graded:
        return head + ("<p class=\"notice\">No verdict: this agent is never counted as a negative or as a member "
                       "of any cohort (judge rubric Section 7).</p>")
    def value(key):
        raw = attributes.get(key)
        return esc(raw) if raw in (None, False, True) else _json_cell(raw)
    rows = "".join(f"<tr><td>{esc(k)}</td><td>{value(k)}</td></tr>" for k in COHORT_ATTRIBUTES + ("provisional",))
    details = {k: attributes.get(k) for k in COHORT_DETAILS if attributes.get(k) not in (None, "", [], {})}
    details.update({k: cohort.get(k) for k in ("cohort_proposed", "proposal_mismatch", "candidate_paper_projection")
                    if cohort.get(k) not in (None, "", False)})
    return (head + "<div class=\"scroll\"><table><tr><th>Cohort attribute (rubric Section 8)</th><th>Value</th></tr>"
            + rows + "</table></div>"
            + (f"<details><summary>Cohort derivation details</summary>{render_value(details)}</details>"
               if details else ""))


# ----------------------------------------------------------------------- pages

def _counts(results):
    """(findings, stance observations) tallied by verification label."""
    findings, stances = Counter(), Counter()
    for result in results.values():
        for finding in findings_of(result):
            findings[verification_label(finding_states(finding))[0]] += 1
        for _, observation in stance_entries(result):
            stances[verification_label([str(observation.get("state"))])[0]] += 1
    return findings, stances


def _model_allocation_table(pipeline):
    rows = stage_models(pipeline)
    if not rows:
        return "<p class=\"muted\">No model allocation recorded in pipeline.json.</p>"
    return ("<div class=\"scroll\"><table><tr><th>Stage</th><th>Model</th><th>Key</th><th>Effort</th><th>Route</th></tr>"
            + "".join(f"<tr><td>{esc(r['stage'])}</td><td><code>{esc(r['model'])}</code></td><td>{esc(r['key'])}</td>"
                      f"<td>{esc(r['effort'])}</td><td>{esc(r['route'])}</td></tr>" for r in rows) + "</table></div>")


def render_agent_page(run_id, agent_id, result, index, resolve, labels, mode, built_at):
    findings = findings_of(result)
    stances = stance_entries(result)
    graded = result["status"] == "graded"
    banner = ("" if mode == "paid" else
              f"<p class=\"notice warn\">Mode: {esc(mode)}. These are not real judge classifications.</p>")
    prefilter = result.get("prefilter") or {}
    sections = [f"<h2 id=\"cohort\">Cohort and attributes</h2>{render_cohort(result)}"]
    if graded:
        sections.append("<h2 id=\"dimensions\">Dimensions</h2><p class=\"muted\">Established levels come only from "
                        "verified findings; an unresolved dimension keeps its established level and lists the "
                        "higher candidate.</p>" + render_dimension_table(agent_id, result))
    tally = Counter(verification_label(finding_states(f))[0] for f in findings)
    sections.append(f"<h2 id=\"findings\">Findings ({len(findings)})</h2>"
                    + (f"<p>{esc(', '.join(f'{k} {v}' for k, v in sorted(tally.items())))}. Every finding is listed, "
                       "refuted ones included; only verified findings with a level of at least 1 count toward scores."
                       "</p>" if findings else "<p class=\"muted\">No finding recorded.</p>")
                    + "".join(render_finding(agent_id, f, result, index, resolve) for f in findings))
    sections.append(f"<h2 id=\"stances\">D2 stance observations ({len(stances)})</h2>"
                    + ("".join(render_stance(agent_id, sid, o, result, index, resolve) for sid, o in stances)
                       or "<p class=\"muted\">No stance observation recorded.</p>"))
    accusations = verified_of(result).get("accusations") or []
    sections.append("<h2 id=\"accusations\">D7 accusation targets</h2>"
                    + (render_value(accusations) if accusations else "<p class=\"muted\">None.</p>"))
    own_labels = [{"label": r.get("label"), "status": r.get("status"),
                   "finding": (r.get("attributes") or {}).get("judge_finding_id") if isinstance(r.get("attributes"), dict)
                   else None, "interpretation": r.get("interpretation")}
                  for r in labels if r.get("agent_id") == agent_id]
    sections.append("<h2 id=\"labels\">Frozen-rubric labels</h2>"
                    + (render_value(own_labels) if own_labels else "<p class=\"muted\">None.</p>"))
    sections.append("<h2 id=\"provenance\">Prefilter and provenance</h2>" + render_value({
        "judge_model": result.get("judge_model"), "judge_rubric_version": result.get("judge_rubric_version"),
        "judge_rubric_sha256": result.get("judge_rubric_sha256"), "reviewed_at": result.get("reviewed_at"),
        "prefilter": prefilter, "spread_edges": result.get("spread_edges") or [],
        "prefilter_misses": [f["id"] for f in findings if is_prefilter_miss(result, f)]
        + [sid for sid, _ in stances if stance_source(result, sid)[0] == "random_sample"],
        "verification_calls": {k: v for k, v in (result.get("verification") or {}).items()
                               if k in ("routing_version", "claims", "calls")}}))
    toc = " ".join(f"<a href=\"#{a}\">{t}</a>" for a, t in (
        ("cohort", "Cohort"), ("dimensions", "Dimensions"), ("findings", "Findings"), ("stances", "Stances"),
        ("accusations", "Accusations"), ("labels", "Labels"), ("provenance", "Provenance")) if graded or a != "dimensions")
    body = (f"<p><a href=\"../index.html\">← All agents</a></p><h1>{esc(agent_id)}</h1>"
            f"<p class=\"muted\"><code>{esc(run_id)}</code> · built {esc(built_at)} · mode {esc(mode)} · read-only"
            f"</p>{banner}<p class=\"notice\">Researcher-only, read-only. The judge's verified labels are the reported "
            "labels; there is no human input in the classification. All transcript text below is untrusted data shown "
            f"verbatim; returned reasoning is self-report.</p><nav class=\"toc\">{toc}</nav>" + "".join(sections))
    return page(f"Judge findings {agent_id}", body, REVIEW_CSS)


def render_index(run_id, results, pipeline, run_level, resolvers, mode, built_at, miss):
    findings_tally, stance_tally = _counts(results)
    graded = [a for a, r in results.items() if r["status"] == "graded"]
    unrated = [(a, r) for a, r in sorted(results.items()) if r["status"] != "graded"]
    banner = ("" if mode == "paid" else
              f"<p class=\"notice warn\">Mode: {esc(mode)}. These are not real judge classifications "
              "(rehearsal or unknown provenance).</p>")
    agent_rows = []
    for agent_id, result in sorted(results.items()):
        cohort = result.get("cohort") or {}
        dims = agent_dims(result)
        cells = []
        for dim in ALL_DIMS:
            entry = dims.get(dim) or {}
            if dim == "D2":
                text = str(sum(o.get("state") in VERIFIED_STATES for o in entry.get("observations") or []))
            elif dim == "D7":
                text = str(sum(bool(t.get("verified")) for t in entry.get("targets") or []))
            elif dim == "D11":
                text = ", ".join(k for k in ("searched", "promoted", "defect_concern", "presented_as_intended_result")
                                 if entry.get(k) is True) or "–"
            else:
                text = "–" if not _is_level(entry.get("established")) else str(entry["established"])
                if _is_level(entry.get("candidate")) and entry.get("candidate") != entry.get("established"):
                    text += f"→{entry['candidate']}"
            cells.append(f"<td title=\"{esc(dimension_status(entry) if entry else 'missing')}\">{esc(text)}</td>")
        tally = Counter(verification_label(finding_states(f))[1] for f in findings_of(result))
        flags = [k if v is True else f"{k}={v}" for k, v in visible_attributes(cohort.get("attributes")).items()]
        cohort_text = esc(cohort.get("cohort")) if result["status"] == "graded" else esc(result["status"])
        if result["status"] == "graded" and cohort.get("provisional"):
            cohort_text += f" <span class=\"chip warn\">provisional → {esc(cohort.get('candidate_cohort'))}</span>"
        agent_rows.append(f"<tr><td><a href=\"{esc(agent_page_name(agent_id))}\">{esc(agent_id)}</a></td>"
                          f"<td>{cohort_text}</td><td>{_chips(flags)}</td>" + "".join(cells)
                          + f"<td>{tally.get('good', 0)} / {tally.get('bad', 0)} / "
                            f"{tally.get('warn', 0) + tally.get('muted', 0)}</td></tr>")
    finding_rows, stance_rows = [], []
    for agent_id, result in sorted(results.items()):
        resolve, href = resolvers[agent_id], esc(agent_page_name(agent_id))
        for finding in findings_of(result):
            label, tone = verification_label(finding_states(finding))
            source, window = finding_source(result, finding)
            notes = [n for n, on in (("borderline", finding.get("borderline")),
                                     (f"prefilter miss ({window})", source == "random_sample"),
                                     ("judge–evaluator disagreement", has_disagreement(finding))) if on]
            finding_rows.append(
                f"<tr><td>{esc(agent_id)}</td><td><a href=\"{href}#{_anchor('finding', agent_id, finding['id'])}\">"
                f"<code>{esc(finding['id'])}</code></a></td><td>{esc(finding.get('object'))}</td>"
                f"<td>{esc(level_text(finding))}</td><td><span class=\"chip {tone}\">{esc(label)}</span></td>"
                f"<td>{esc(', '.join(finding_models(finding, resolve)) or '—')}</td>"
                f"<td>{'yes' if is_verified_positive(finding) else 'no'}</td><td>{_chips(notes, 'bad')}</td>"
                f"<td>{esc(finding.get('interpretation') or finding.get('fact'))}</td></tr>")
        for sid, observation in stance_entries(result):
            label, tone = verification_label([str(observation.get("state"))])
            entry = resolve(sid, sid, observation)
            stance_rows.append(
                f"<tr><td>{esc(agent_id)}</td><td><a href=\"{href}#{_anchor('stance', agent_id, sid)}\"><code>"
                f"{esc(sid)}</code></a></td><td>{esc(observation.get('value'))}</td><td>{esc(observation.get('object'))}"
                f"</td><td><span class=\"chip {tone}\">{esc(label)}</span></td><td>"
                f"{esc(model_short(entry) if observation.get('state') in VERIFIED_STATES + ('refuted',) else '—')}"
                f"</td><td>“{esc(observation.get('quote'))}”</td></tr>")
    disagreements = [{"agent_id": a, "finding_ids": [f["id"] for f in findings_of(r) if has_disagreement(f)],
                      "submission_ids": sorted(verified_of(r).get("judge_evaluator_disagreement") or [])}
                     for a, r in sorted(results.items()) if verified_of(r).get("judge_evaluator_disagreement")]
    rate = miss.get("miss_rate")
    ci = miss.get("wilson95")
    summary = (f"<ul><li>{len(results)} agents: {len(graded)} graded, "
               f"{sum(r['status'] == 'ungraded' for r in results.values())} ungraded, "
               f"{sum(r['status'] == 'grading_failed' for r in results.values())} grading failed.</li>"
               f"<li>{sum(findings_tally.values())} findings: "
               f"{esc(', '.join(f'{k} {v}' for k, v in sorted(findings_tally.items())) or 'none')}.</li>"
               f"<li>{sum(stance_tally.values())} stance observations: "
               f"{esc(', '.join(f'{k} {v}' for k, v in sorted(stance_tally.items())) or 'none')}.</li>"
               f"<li>Prefilter miss rate {esc('n/a' if rate is None else f'{rate:.1%}')} "
               f"({miss.get('windows_with_misses', 0)} of {miss.get('windows_sampled', 0)} random-sample windows"
               + (f"; 95% Wilson interval {ci[0]:.1%}–{ci[1]:.1%}" if ci else "") + ").</li></ul>")
    toc = " ".join(f"<a href=\"#{a}\">{t}</a>" for a, t in (
        ("summary", "Summary"), ("models", "Models"), ("agents", "Agents"), ("findings", "Every finding"),
        ("stances", "Stance observations"), ("runlevel", "Run-level results"),
        ("disagreements", "Judge–evaluator disagreements")))
    body = (f"<h1>Behavior judge findings</h1><p class=\"muted\"><code>{esc(run_id)}</code> · built "
            f"{esc(built_at)} · mode {esc(mode)} · read-only</p>{banner}"
            "<div class=\"notice\"><strong>Researcher-only, read-only.</strong> The judge rubric is post-hoc (written "
            "after all four runs' results were inspected). The judge's adversarially verified labels are the reported "
            "labels; there is no human input in the classification (plan decision, October 17, 2026). This page lists "
            "every finding, refuted ones included and labelled, with the verifier's verdict and the model that "
            "verified it; each agent page shows the full evidence passages. It collects no marks and changes no "
            "labels. All transcript text is untrusted data shown verbatim; returned reasoning is self-report.</div>"
            f"<nav class=\"toc\">{toc}</nav>"
            f"<h2 id=\"summary\">Summary</h2>{summary}"
            f"<h2 id=\"models\">Model allocation</h2>{_model_allocation_table(pipeline)}"
            "<h2 id=\"agents\">Agents</h2><p class=\"muted\">Ladder levels are verified established levels (→candidate "
            "when unresolved); D2 counts verified stance observations, D7 verified accusation targets, D11 lists true "
            "fields. Findings: verified / refuted / other.</p><div class=\"scroll\"><table><tr><th>Agent</th>"
            "<th>Cohort</th><th>Attributes</th>" + "".join(f"<th title=\"{esc(DIM_NAMES[d])}\">{d}</th>" for d in ALL_DIMS)
            + "<th>Findings</th></tr>" + "".join(agent_rows) + "</table></div>"
            + (("<h3>Agents without a verdict (never counted as negative)</h3><ul>"
                + "".join(f"<li>{esc(a)}: {esc(r['status'])} {esc(r.get('status_reason') or '')}</li>" for a, r in unrated)
                + "</ul>") if unrated else "")
            + f"<h2 id=\"findings\">Every finding ({len(finding_rows)})</h2>"
            + ("<div class=\"scroll\"><table><tr><th>Agent</th><th>Finding</th><th>Object</th><th>Levels</th>"
               "<th>Verification</th><th>Verifier model</th><th>Counted</th><th>Notes</th><th>Interpretation</th></tr>"
               + "".join(finding_rows) + "</table></div>" if finding_rows else "<p>None.</p>")
            + f"<h2 id=\"stances\">Stance observations ({len(stance_rows)})</h2>"
            + ("<div class=\"scroll\"><table><tr><th>Agent</th><th>Id</th><th>Value</th><th>Object</th>"
               "<th>Verification</th><th>Verifier model</th><th>Quote</th></tr>" + "".join(stance_rows) + "</table></div>"
               if stance_rows else "<p>None.</p>")
            + "<h2 id=\"runlevel\">Run-level results</h2>"
            + (render_value(run_level) if run_level is not None else "<p class=\"muted\">No run-level output saved.</p>")
            + f"<h2 id=\"disagreements\">Judge–evaluator disagreements ({len(disagreements)})</h2>"
            + (render_value(disagreements) if disagreements else "<p class=\"muted\">None.</p>"))
    return page(f"Judge findings {run_id}", body, REVIEW_CSS)


def build_review_page(run_dir, judge_dir=None, seed=None, *, now=None):
    """Write the read-only findings browser under judge_dir/review/; returns a summary dict.

    `seed` is accepted for compatibility with older callers and ignored: the page lists every
    agent and every finding, so nothing is sampled."""
    del seed
    run_dir = Path(run_dir)
    judge_dir = Path(judge_dir) if judge_dir else default_judge_dir(run_dir)
    pipeline = load_pipeline(judge_dir)
    run_id = run_dir.resolve().name
    if pipeline.get("run_id") not in (None, run_id):
        raise ValueError(f"pipeline.json run_id {pipeline.get('run_id')!r} does not match {run_id!r}")
    results = load_agent_results(judge_dir)
    if not results:
        raise ValueError(f"no agent results under {judge_dir / 'agents'}")
    run_level, labels = load_run_level(judge_dir), load_labels(judge_dir)
    mode, built_at = pipeline.get("mode", "unknown"), _now(now)
    key_models = {r["key"]: r["model"] for r in stage_models(pipeline) if r.get("key")}
    resolvers, model_sources, verifier_units = {}, Counter(), Counter()
    for agent_id, result in results.items():
        resolve = ModelResolver(result, verifier_unit_models(judge_dir, agent_id, key_models))
        resolvers[agent_id] = resolve
        for finding in findings_of(result):
            for unit_id, _, row in unit_rows(finding):
                if row.get("state") in VERIFIED_STATES + ("refuted",):
                    entry = resolve(unit_id, finding["id"], row)
                    verifier_units[model_short(entry)] += 1
                    model_sources[_source_kind(entry)] += 1
        for sid, observation in stance_entries(result):
            if observation.get("state") in VERIFIED_STATES + ("refuted",):
                entry = resolve(sid, sid, observation)
                verifier_units[model_short(entry)] += 1
                model_sources[_source_kind(entry)] += 1
    miss = prefilter_miss_rate(results)
    review = judge_dir / "review"
    pages = {}
    for agent_id, result in sorted(results.items()):
        text = render_agent_page(run_id, agent_id, result, PackageIndex(load_package(judge_dir, agent_id)),
                                 resolvers[agent_id], labels, mode, built_at)
        name = agent_page_name(agent_id)
        write_text_atomic(review / name, text)
        pages[name] = sha256_bytes(text.encode("utf-8"))
    index = render_index(run_id, results, pipeline, run_level, resolvers, mode, built_at, miss)
    write_text_atomic(review / "index.html", index)
    pages["index.html"] = sha256_bytes(index.encode("utf-8"))
    findings_tally, stance_tally = _counts(results)
    produced = set(pages) | {"manifest.json"}
    unrecognized = sorted(p.relative_to(review).as_posix() for p in review.rglob("*")
                          if p.is_file() and not p.name.startswith(".") and p.relative_to(review).as_posix() not in produced)
    counts = {"agents": len(results), "graded": sum(r["status"] == "graded" for r in results.values()),
              "findings": sum(findings_tally.values()), "finding_verification": dict(sorted(findings_tally.items())),
              "stance_observations": sum(stance_tally.values()),
              "stance_verification": dict(sorted(stance_tally.items())),
              "verifier_units_by_model": dict(sorted(verifier_units.items())),
              "verifier_model_sources": dict(sorted(model_sources.items()))}
    manifest = {"schema": MANIFEST_SCHEMA, "run_id": run_id, "built_at": built_at, "mode": mode, "read_only": True,
                "inputs_sha256": input_digests(judge_dir), "pages": dict(sorted(pages.items())), "counts": counts,
                "unrecognized_files": unrecognized}
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    write_json(review / "manifest.json", manifest)
    return {"page": str(review / "index.html"), "agent_pages": len(results), "manifest": str(review / "manifest.json"),
            "counts": counts, "manifest_sha256": manifest["manifest_sha256"], "unrecognized_files": unrecognized}


def load_manifest(judge_dir):
    manifest = read_json(Path(judge_dir) / "review" / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"review manifest must have schema {MANIFEST_SCHEMA}")
    core = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if canonical_sha256(core) != manifest.get("manifest_sha256"):
        raise ValueError("review manifest digest mismatch (edited after the page was built)")
    return manifest


def review_page_state(judge_dir):
    """{"status": missing | invalid | stale | current, ...}: whether review/ still matches the
    judge outputs it was built from (every input and page digest, no input added or removed)."""
    judge_dir = Path(judge_dir)
    path = judge_dir / "review" / "manifest.json"
    if not path.exists():
        return {"status": "missing"}
    try:
        manifest = load_manifest(judge_dir)
    except ValueError as error:
        return {"status": "invalid", "problem": str(error)}
    info = {"status": "current", "page": "review/index.html", "built_at": manifest.get("built_at"),
            "manifest_sha256": manifest["manifest_sha256"], "manifest_file_sha256": sha256_file(path),
            "counts": manifest.get("counts") or {}}
    problems = []
    recorded, current = manifest.get("inputs_sha256") or {}, input_digests(judge_dir)
    for name in sorted(set(recorded) | set(current)):
        if recorded.get(name) != current.get(name):
            problems.append(f"input {name}: " + ("added" if name not in recorded else
                                                 "removed" if name not in current else "changed"))
    pages = manifest.get("pages") or {}
    for path in sorted((judge_dir / "review" / "agents").glob("*.html")):
        name = path.relative_to(judge_dir / "review").as_posix()
        if name not in pages:  # e.g. an agent page left by an earlier build; never deleted automatically
            problems.append(f"page {name}: not produced by this build (remove it)")
    for name, digest in pages.items():
        page_path = judge_dir / "review" / name
        if not page_path.is_file() or page_path.is_symlink() or sha256_file(page_path) != digest:
            problems.append(f"page {name}: missing or edited")
    if problems:
        info.update(status="stale", problems=problems[:50], problem_count=len(problems))
    return info


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only behavior judge findings browser (offline).")
    parser.add_argument("command", nargs="?", choices=("build",), default="build")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--judge-dir", type=Path)
    args = parser.parse_args(argv)
    judge_dir = args.judge_dir or default_judge_dir(args.run_dir)
    print(json.dumps(build_review_page(args.run_dir, judge_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
