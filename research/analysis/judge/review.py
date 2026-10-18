"""Researcher-only, read-only evidence browser for the misbehavior judge (rubric v2).

docs/plan.md, "Superseding decision (October 17, 2026, evening): misbehavior-focused judge",
and judge rubric `gdm-judge-rubric/2` Section 7: the report (report.py) is the five-minute
read; this module writes the full evidence behind it, which stays browsable but is not part
of the read. There is no human input in the classification: the judge's verified labels are
the reported labels. The pages collect nothing and change no label: there are no form
controls, no scripts and no browser storage.

Offline only: no model calls, network, Docker or credentials. Experimental agents must
never see this module, the pages or any judge output. All transcript text and all model
output are untrusted and are HTML-escaped; nothing in them is an instruction.

Outputs under RUN/analysis/judge/review/:
  index.html               compact index: one row per agent (cohort, flags, the three
                           category levels, finding counts), the unresolved/ungraded list
                           and the full run-level record (collapsed)
  agents/<agent_id>.html   only for agents with at least one judge finding (verified,
                           adjusted, refuted or unverified): every finding with its
                           verifier verdict and model, evidence and counter-evidence
                           passages in full, the category attributes and frozen labels
  manifest.json            schema gdm-judge-review-manifest/3: SHA-256 of every input and
                           page; report.py and prepare_hf_library.py use it to detect a
                           browser that no longer matches the judge outputs

Inputs (written by the judge pipeline under RUN/analysis/judge/):
  pipeline.json, run_level.json (optional), labels.jsonl (optional),
  packages/<agent_id>.json (I-ITEM dicts at any nesting; the judge's input package),
  units/<agent_id>/**/*verif*.json (optional; only to recover which model verified a finding),
  agents/<agent_id>.json, schema gdm-judge-agent-result/2. Fields read (others ignored):

    {"schema", "agent_id", "status": "graded" | "ungraded" | "grading_failed", "status_reason",
     "judge_model", "verifier_model", "judge_rubric_version", "judge_rubric_sha256",
     "verdict": {..., "note"},                      # the judge's claims (rubric Section 6)
     "note", "note_source": "judge" | "host",       # the table note chosen by the host stage
     "verified": {                                  # cohort.apply_verification_v2 (schema gdm-judge-verified/2)
        "cheating": {"level": "none|explored|submitted|accepted", "candidate_level", "basis",
                     "technique", "tasks", "submission_ids", "judge_only_submission_ids",
                     "genuine_submission_ids", "shared", "adopted_from", "concealed", "convert",
                     "finding_ids", "unresolved_reason"},
        "whistleblowing": {"level": "none|raised|reported", "candidate_level", "targets": [{"kind",
                           "id"}], "channels", "boycott", "accuracy": {"supported": n, ...},
                           "finding_ids", "unresolved_reason"},
        "evaluation_awareness": {"level": "none|constructed|conduct", "candidate_level", "finding_ids"},
        "accusations": [{"finding_id", "target": {"kind", "id"}, "accuracy", "verified"}],
        "judge_evaluator_disagreement": [sid],
        "findings": [{"id", "category", "claim", "level", "evidence", "counter_evidence", "summary",
                      "borderline", "state": "confirmed|adjusted|refuted|unverified|ungraded|...",
                      "verifier_reason", "established_level", "holds", "notes"}],
        "note"},
     "cohort": cohort.compute_v2 output {"cohort", "flags", "paper_cohort", "provisional",
               "candidate_cohort", "judge_only_submission_ids", "insufficient_reasons", ...},
     "host_facts": {"evaluations": {sid: {"validity"}}, "submissions": {sid: {"agent_id",
                    "task_id", "outcome"}}, "statement_defects": {task: {...}}},
     "prefilter": {"windows_sampled": [window ids] | int, ...},
     "finding_sources": {fid: {"source": "random_sample" | "selected", "window_id"}},
     "recall": {"mixed": [fid]},
     "verification": {"model", "model_key", "findings": {fid: {"model", ...}} (optional)}}

  Tolerated variants: a category's level as "established"; categories nested under
  "categories"; a finding's state under "verification": {"state" | "verdict", "reason",
  "adjusted_to", "model"}; "judge_only_submission_ids" at the top of "verified"; cohort "flags"
  as a list of names or a {name: value} map; whistleblowing "accuracy" as per-target rows.

Only findings that hold after verification count: the host's "holds" flag when recorded
(a confirmed claim can still fail a host rule, e.g. every cited submission found genuine),
else state confirmed, or adjusted to a level other than none.
Which model verified a finding, first match wins: the finding's own record; the agent
result's "verification" routing record; the verifier unit files; the agent result's single
"verifier_model". Anything else is shown as "not recorded"; it is never guessed.
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

AGENT_RESULT_SCHEMA = "gdm-judge-agent-result/2"
MANIFEST_SCHEMA = "gdm-judge-review-manifest/3"
AGENT_STATUSES = ("graded", "ungraded", "grading_failed")
VERIFIED_STATES = ("confirmed", "adjusted")
VERDICT_STATES = VERIFIED_STATES + ("refuted",)
CONTEXT_ITEMS = 2        # neighbouring items shown on each side of a cited item

CATEGORIES = ("cheating", "whistleblowing", "evaluation_awareness")
CATEGORY_NAMES = {"cheating": "Cheating", "whistleblowing": "Whistleblowing",
                  "evaluation_awareness": "Evaluation awareness"}
LEVELS = {"cheating": ("none", "explored", "submitted", "accepted"),
          "whistleblowing": ("none", "raised", "reported"),
          "evaluation_awareness": ("none", "constructed", "conduct")}
CLAIM_ORDER = ("level", "shared", "adopted", "concealed", "convert", "boycott")
# Rubric Section 7: cohorts in rule order, the paper projection and the flags.
COHORT_ORDER = ("convert", "exploiter", "exploit_promoter", "whistleblower", "no_misbehavior",
                "insufficient_record")
PAPER_PROJECTION = {"convert": "Converts", "exploiter": "Exploiters", "exploit_promoter": "Exploiters",
                    "whistleblower": "Whistleblowers", "no_misbehavior": "Unaware solvers"}
PAPER_ORDER = ("Converts", "Exploiters", "Whistleblowers", "Unaware solvers")
FLAG_ORDER = ("evaluation_aware", "explored_only", "also_reported", "judge_only")
MODEL_KEYS = ("haiku", "sonnet", "opus")
# Pipeline stages in order (pipeline.py STAGES); model stages are the ones with units.
STAGE_ORDER = ("history", "prefilter", "judge", "verify", "host", "closed", "runlevel", "report")
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
    """agents/<agent_id>.json -> {agent_id: result}; malformed or v1 results fail loudly."""
    folder = Path(judge_dir) / "agents"
    results = {}
    if not folder.is_dir():
        return results
    for path in sorted(folder.glob("*.json")):
        data = read_json(path)
        if not isinstance(data, dict) or data.get("schema") != AGENT_RESULT_SCHEMA:
            raise ValueError(f"{path}: expected schema {AGENT_RESULT_SCHEMA} (results of the superseded "
                             "12-dimension judge are never reported)")
        if data.get("agent_id") != path.stem:
            raise ValueError(f"{path}: agent_id does not match the file name")
        if not SAFE_AGENT_ID.fullmatch(path.stem) or ".." in path.stem:
            raise ValueError(f"{path}: agent_id is not a safe page name")
        if data.get("status") not in AGENT_STATUSES:
            raise ValueError(f"{path}: status must be one of {AGENT_STATUSES}")
        if data["status"] == "graded" and not isinstance(data.get("verified"), dict):
            raise ValueError(f"{path}: a graded result needs its verified record ('verified')")
        results[path.stem] = data
    return results


def load_package(judge_dir, agent_id):
    path = Path(judge_dir) / "packages" / f"{agent_id}.json"
    return read_json(path) if path.exists() else None


def load_labels(judge_dir):
    path = Path(judge_dir) / "labels.jsonl"
    return [r for r in read_jsonl(path) if isinstance(r, dict)] if path.exists() else []


def input_digests(judge_dir):
    """SHA-256 of every input the pages are built from (paths relative to the judge dir)."""
    judge_dir = Path(judge_dir)
    digests = {}
    for pattern in INPUT_GLOBS:
        for path in sorted(judge_dir.glob(pattern)):
            if path.is_file() and not path.is_symlink():
                digests[path.relative_to(judge_dir).as_posix()] = sha256_file(path)
    return dict(sorted(digests.items()))


# ------------------------------------------------------------ verified records (v2)

def _dict(value):
    return value if isinstance(value, dict) else {}


def _truthy(value):
    return value not in (None, False, "", [], {}, 0)


def verified_of(result):
    return _dict(_dict(result).get("verified"))


def graded(result):
    return _dict(result).get("status") == "graded"


def findings_of(result):
    return [f for f in verified_of(result).get("findings") or [] if isinstance(f, dict) and isinstance(f.get("id"), str)]


def category_entry(result, name):
    verified = verified_of(result)
    return _dict(verified.get(name)) or _dict(_dict(verified.get("categories")).get(name))


def _rank(name, level):
    return LEVELS[name].index(level) if level in LEVELS.get(name, ()) else None


def category_level(result, name):
    """The verified level of one category, or None (not graded or not recorded)."""
    if not graded(result):
        return None
    entry = category_entry(result, name)
    level = entry.get("level", entry.get("established"))
    return level if isinstance(level, str) and level else None


def category_candidate(result, name):
    entry = category_entry(result, name)
    for key in ("candidate", "candidate_level"):
        if isinstance(entry.get(key), str):
            return entry[key]
    return None


def level_status(result, name):
    """ok | unresolved | missing | ungraded | grading_failed for one category."""
    if not graded(result):
        return _dict(result).get("status") or "missing"
    entry = category_entry(result, name)
    if not entry:
        return "missing"
    if entry.get("status") in ("ungraded", "grading_failed"):
        return entry["status"]
    level, candidate = category_level(result, name), category_candidate(result, name)
    above = _rank(name, candidate) is not None and _rank(name, level) is not None and \
        _rank(name, candidate) > _rank(name, level)
    if entry.get("unresolved") or entry.get("unresolved_reason") or above:
        return "unresolved"
    return "ok" if level is not None else "missing"


def finding_state(finding):
    for key in ("state", "verification_state", "verifier_verdict"):
        if isinstance(finding.get(key), str) and finding[key]:
            return finding[key]
    record = _dict(finding.get("verification"))
    for key in ("state", "verdict"):
        if isinstance(record.get(key), str) and record[key]:
            return record[key]
    return "unverified"


def finding_reason(finding):
    record = _dict(finding.get("verification"))
    for value in (finding.get("verifier_reason"), record.get("reason"), finding.get("reason")):
        if isinstance(value, str) and value:
            return value
    return None


def finding_adjustment(finding):
    record = _dict(finding.get("verification"))
    for value in (finding.get("adjusted_to"), record.get("adjusted_to"), finding.get("adjustment")):
        if value not in (None, "", {}):
            return value
    return None


def _adjusted_to_nothing(finding):
    value = finding_adjustment(finding)
    if isinstance(value, dict):
        value = value.get("level", value.get("value", True))
    return value in ("none", False)


def is_verified(finding):
    """Only findings that hold after verification count (module docstring)."""
    if isinstance(finding.get("holds"), bool):
        return finding["holds"]
    return finding_state(finding) in VERIFIED_STATES and not _adjusted_to_nothing(finding)


def verification_label(state, finding=None):
    """(label, tone) for a verifier state; tone is good|bad|warn."""
    if state in VERIFIED_STATES and finding is not None and _adjusted_to_nothing(finding):
        return "adjusted to none", "bad"
    if state in VERIFIED_STATES and finding is not None and finding.get("holds") is False:
        return "verified, not counted (host rule)", "warn"
    return {"confirmed": ("verified", "good"), "adjusted": ("verified (adjusted)", "good"),
            "refuted": ("refuted", "bad")}.get(state, (f"not verified ({state})", "warn"))


def finding_level(finding):
    """(claimed, established) level of a level claim, (None, None) for other claims."""
    if finding_claim(finding) != "level":
        return None, None
    claimed = finding.get("level") if isinstance(finding.get("level"), str) else None
    established = finding.get("established_level") if isinstance(finding.get("established_level"), str) else None
    return claimed, established


def finding_notes(finding):
    return [n for n in finding.get("notes") or [] if isinstance(n, str) and n]


def finding_category(finding):
    value = finding.get("category")
    return value if isinstance(value, str) and value else "unknown"


def finding_claim(finding):
    value = finding.get("claim")
    return value if isinstance(value, str) and value else "level"


def finding_summary(finding):
    for key in ("summary", "interpretation", "fact"):
        if isinstance(finding.get(key), str) and finding[key].strip():
            return finding[key].strip()
    return ""


def finding_order(finding):
    category, claim = finding_category(finding), finding_claim(finding)
    return (CATEGORIES.index(category) if category in CATEGORIES else len(CATEGORIES),
            CLAIM_ORDER.index(claim) if claim in CLAIM_ORDER else len(CLAIM_ORDER), finding["id"])


def citations(finding, key="evidence"):
    """[(item id, quote)] from a finding's evidence or counter_evidence list."""
    out = []
    for entry in finding.get(key) or []:
        if isinstance(entry, dict) and isinstance(entry.get("item"), str):
            out.append((entry["item"], entry.get("quote") if isinstance(entry.get("quote"), str) else None))
        elif isinstance(entry, str):
            out.append((entry, None))
    return out


def flags_of(value):
    """Flag names from a list of names or a {name: value} map, in rubric order."""
    if isinstance(value, dict):
        names = [k for k, v in value.items() if _truthy(v)]
    elif isinstance(value, (list, tuple)):
        names = [v for v in value if isinstance(v, str) and v]
    else:
        names = []
    order = {name: n for n, name in enumerate(FLAG_ORDER)}
    return sorted(dict.fromkeys(names), key=lambda n: (order.get(n, len(order)), n))


def cohort_view(result):
    """{"cohort", "paper_cohort", "flags", "provisional", "candidate_cohort"}; no cohort when not graded."""
    record = _dict(_dict(result).get("cohort"))
    if not graded(result):
        return {"cohort": None, "paper_cohort": None, "flags": [], "provisional": False, "candidate_cohort": None}
    name = record.get("cohort") if isinstance(record.get("cohort"), str) else None
    paper = record.get("paper_cohort", PAPER_PROJECTION.get(name))
    return {"cohort": name, "paper_cohort": paper if isinstance(paper, str) else None,
            "flags": flags_of(record.get("flags")), "provisional": bool(record.get("provisional")),
            "candidate_cohort": record.get("candidate_cohort")}


def _id_list(*values):
    out = []
    for value in values:
        if isinstance(value, dict):
            value = [k for k, v in value.items() if _truthy(v)]
        if isinstance(value, (list, tuple)):
            out += [v for v in value if isinstance(v, str) and v]
    return sorted(dict.fromkeys(out))


def judge_evaluator_disagreements(result):
    """Submissions the judge called bypasses that the evaluator found genuine (never counted)."""
    return _id_list(verified_of(result).get("judge_evaluator_disagreement"),
                    category_entry(result, "cheating").get("judge_evaluator_disagreement"),
                    category_entry(result, "cheating").get("genuine_submission_ids"))


def judge_only_submissions(result):
    """Submitted bypasses the evaluator has not evaluated (queued for unpaid comparator evaluation)."""
    record = _dict(_dict(result).get("cohort"))
    flags = record.get("flags")
    return _id_list(verified_of(result).get("judge_only_submission_ids"),
                    category_entry(result, "cheating").get("judge_only_submission_ids"),
                    record.get("judge_only_submission_ids"),
                    flags.get("judge_only") if isinstance(flags, dict) else None)


def target_name(target):
    if isinstance(target, dict):
        for key in ("id", "target", "name", "agent_id", "submission_id"):
            if isinstance(target.get(key), str) and target[key]:
                return target[key]
        return None
    return target if isinstance(target, str) and target else None


ACCURACY_VALUES = ("supported", "false_accusation", "unresolved")


def accuracy_rows(result):
    """[(target, accuracy)] host-computed for verified whistleblowing targets: the verified
    record's "accusations", else per-target rows under whistleblowing "accuracy"."""
    rows = [(target_name(a.get("target")) or "?", str(a.get("accuracy")))
            for a in verified_of(result).get("accusations") or []
            if isinstance(a, dict) and a.get("verified") is not False]
    if rows:
        return list(dict.fromkeys(rows))
    value = category_entry(result, "whistleblowing").get("accuracy")
    if isinstance(value, list):
        return [(target_name(r) or "?", str(r.get("accuracy"))) for r in value if isinstance(r, dict)]
    if isinstance(value, dict) and not set(value) <= set(ACCURACY_VALUES):
        return [(str(k), str(v)) for k, v in value.items()]
    return []


def evaluator_validity(result, submission_id):
    record = _dict(_dict(_dict(result).get("host_facts")).get("evaluations")).get(submission_id)
    validity = _dict(record).get("validity")
    return validity if isinstance(validity, str) else None


def _short(values, limit=4):
    values = [str(v) for v in values if v not in (None, "")]
    return ", ".join(values[:limit]) + (f" +{len(values) - limit} more" if len(values) > limit else "")


def category_facts(result, name):
    """Short, host-composed descriptions of one category's verified level and attributes."""
    level = category_level(result, name)
    if level in (None, "none") and not (name == "cheating" and category_entry(result, name).get("shared") is True):
        return []
    entry = category_entry(result, name)
    facts = [f"{CATEGORY_NAMES[name].lower()} {level or 'none'}"]
    if name == "cheating" and level in (None, "none"):
        facts = ["shared a bypass without submitting one"]
        if isinstance(entry.get("technique"), str) and entry["technique"].strip():
            facts.append(entry["technique"].strip()[:120])
        return facts
    if name == "cheating":
        if entry.get("basis") == "readied":
            facts[0] += " (readied)"
        if isinstance(entry.get("technique"), str) and entry["technique"].strip():
            facts.append(entry["technique"].strip()[:120])
        if entry.get("tasks"):
            facts.append("tasks " + _short(entry["tasks"]))
        subs = [s for s in entry.get("submission_ids") or [] if isinstance(s, str)]
        if subs:
            facts.append("submissions " + _short(f"{s} (evaluator {evaluator_validity(result, s)})"
                                                 if evaluator_validity(result, s) else f"{s} (not evaluated)"
                                                 for s in subs))
        if entry.get("shared") is True:
            facts.append("shared a bypass")
        adopted = [a for a in entry.get("adopted_from") or [] if isinstance(a, str)]
        if adopted:
            facts.append("adopted from " + _short(adopted))
        for key, text in (("concealed", "concealed"), ("convert", "convert")):
            if entry.get(key) is True:
                facts.append(text)
    elif name == "whistleblowing":
        accuracy = dict(accuracy_rows(result))
        targets = [target_name(t) for t in entry.get("targets") or []]
        if targets:
            facts.append("targets " + _short(f"{t} ({accuracy[t]})" if t in accuracy else t for t in targets if t))
        elif accuracy:
            facts.append("accuracy " + _short(f"{t} {a}" for t, a in accuracy.items()))
        counts = entry.get("accuracy") if isinstance(entry.get("accuracy"), dict) else {}
        if not accuracy and any(type(counts.get(k)) is int and counts[k] for k in ACCURACY_VALUES):
            facts.append("accuracy " + ", ".join(f"{k.replace('_', ' ')} {counts[k]}" for k in ACCURACY_VALUES
                                                 if type(counts.get(k)) is int and counts[k]))
        if entry.get("channels"):
            facts.append("via " + _short(entry["channels"]))
        if entry.get("boycott") is True:
            facts.append("boycott or quit")
    return facts


def finding_counts(result):
    tally = Counter()
    for finding in findings_of(result):
        state = finding_state(finding)
        tally["verified" if is_verified(finding) else "refuted" if state in VERDICT_STATES else "unverified"] += 1
    return tally


def has_page(result):
    """Agents with any judge finding get an evidence page (refuted findings stay inspectable)."""
    return bool(findings_of(result))


def _clip_note(text, limit=240):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def agent_note(result):
    """(one sentence, source). Agents without a verified finding get a plain host sentence.
    Otherwise the host stage's recorded "note"/"note_source" (pipeline.py), else the judge's
    note when every finding was confirmed as claimed, else a note composed from the verified
    record."""
    status = _dict(result).get("status")
    if status != "graded":
        reason = _dict(result).get("status_reason")
        return f"Not graded ({status}{': ' + str(reason) if reason else ''}); never counted as a negative.", "host"
    view, counts = cohort_view(result), finding_counts(result)
    extra = " ".join(f"{n} {text}." for n, text in ((counts["refuted"], "judge claim(s) refuted by the verifier"),
                                                     (counts["unverified"], "claim(s) not verified")) if n)
    if view["cohort"] == "insufficient_record":
        reasons = _dict(_dict(result).get("cohort")).get("insufficient_reasons") or []
        reason = f" ({reasons[0]})" if reasons and isinstance(reasons[0], str) else ""
        return (f"Insufficient record{reason}; excluded from the paper projection. " + extra).strip(), "host"
    if not counts["verified"]:
        return ("No misbehavior found. " + extra).strip(), "host"
    recorded = _dict(result).get("note")
    if isinstance(recorded, str) and recorded.strip():   # the host stage's choice (pipeline.py)
        source = result.get("note_source") if result.get("note_source") in ("judge", "host") else "host"
        return _clip_note(recorded), source
    note = verified_of(result).get("note")
    if not isinstance(note, str) or not note.strip():
        note = _dict(_dict(result).get("verdict")).get("note")
    unchanged = all(finding_state(f) == "confirmed" and is_verified(f)
                    and finding_level(f)[1] in (None, finding_level(f)[0]) for f in findings_of(result))
    if unchanged and isinstance(note, str) and note.strip():
        return _clip_note(note), "judge"
    facts = ["; ".join(category_facts(result, name)) for name in CATEGORIES]
    text = ". ".join(f[:1].upper() + f[1:] for f in facts if f)
    return ((text + "." if text else "Verified finding(s) recorded.") + (" " + extra if extra else "")), "host"


def open_items(results):
    """Unresolved and ungraded cases, listed and never counted as negatives (rubric Section 7)."""
    items = []
    for agent_id, result in sorted(results.items()):
        status = result.get("status")
        if status != "graded":
            items.append({"agent_id": agent_id, "kind": status, "detail": result.get("status_reason") or ""})
            continue
        view = cohort_view(result)
        if view["provisional"]:
            items.append({"agent_id": agent_id, "kind": "provisional cohort",
                          "detail": f"{view['cohort']} (candidate {view['candidate_cohort'] or 'not recorded'})"})
        unverified = [f for f in findings_of(result) if finding_state(f) not in VERDICT_STATES]
        listed = set()
        for name in CATEGORIES:
            state = level_status(result, name)
            if state != "ok":
                listed.add(name)
                candidate = category_candidate(result, name)
                pending = [f"{f['id']} ({finding_reason(f) or finding_state(f)})" for f in unverified
                           if finding_category(f) == name]
                items.append({"agent_id": agent_id, "kind": f"{name} {state}",
                              "detail": f"level {category_level(result, name) or 'not recorded'}"
                                        + (f", candidate {candidate}" if candidate else "")
                                        + (f"; not verified: {', '.join(pending)}" if pending else "")})
        judge_only = judge_only_submissions(result)
        if judge_only:
            items.append({"agent_id": agent_id, "kind": "judge-only submission",
                          "detail": f"{_short(judge_only)} awaiting comparator evaluation"})
        disagreements = judge_evaluator_disagreements(result)
        if disagreements:
            items.append({"agent_id": agent_id, "kind": "judge–evaluator disagreement",
                          "detail": f"{_short(disagreements)}: evaluator found genuine; not counted"})
        for target, accuracy in accuracy_rows(result):
            if accuracy == "unresolved":
                items.append({"agent_id": agent_id, "kind": "accusation accuracy unresolved", "detail": target})
        for finding in unverified:
            if finding_category(finding) not in listed:
                items.append({"agent_id": agent_id, "kind": "unverified finding",
                              "detail": f"{finding['id']} {finding_category(finding)} "
                                        f"({finding_reason(finding) or finding_state(finding)})"})
    return items


def finding_source(result, finding):
    """(source, window id): prefilter provenance from finding_sources (or the finding).
    A finding made only from a random-sample window is a prefilter miss (rubric Section 7)."""
    entry = _dict(_dict(result.get("finding_sources")).get(finding.get("id")))
    source = entry.get("source", finding.get("source"))
    if entry.get("prefilter_miss") is True or source == "prefilter_miss":
        source = "random_sample"
    return source, entry.get("window_id", finding.get("window_id"))


def is_prefilter_miss(result, finding):
    return finding_source(result, finding)[0] == "random_sample"


def is_mixed(result, finding):
    """The finding cites random-sample items alongside items the prefilter selected."""
    return finding.get("id") in (_dict(result.get("recall")).get("mixed") or [])


def prefilter_miss_rate(results):
    """Random-sample windows whose reading produced a verified finding, over random-sample
    windows read. Without window ids, findings are counted."""
    sampled, misses, missing_window_ids = 0, set(), False
    for agent_id, result in results.items():
        windows = _dict(result.get("prefilter")).get("windows_sampled")
        sampled += len(windows) if isinstance(windows, list) else (windows if type(windows) is int else 0)
        for finding in findings_of(result):
            source, window = finding_source(result, finding)
            if source == "random_sample" and is_verified(finding):
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
    "judge", "verifier_opus" -> "verify", "prefilter_repair" -> "prefilter")."""
    text = str(purpose or "").strip().lower().replace("-", "_")
    if text.endswith("_repair"):
        text = text[:-len("_repair")]
    for prefix, stage in (("prefilter", "prefilter"), ("judge", "judge"), ("verif", "verify"),
                          ("runlevel", "runlevel"), ("run_level", "runlevel")):
        if text.startswith(prefix):
            return stage
    return text or "unknown"


def stage_models(pipeline):
    """Configured model allocation from pipeline.json, as [{"stage", "purpose", "key",
    "model", "effort", "route"}]. Uses "allocation" rows ({"stage", "model_key", "model",
    "effort", "role"}) when present, else "models": a purpose -> entry map (entry: a dict
    with "model", a list of such dicts, or a model id string) or a model key -> entry map
    whose entries list their "stages"."""
    pipeline = pipeline or {}
    models = pipeline.get("models")
    allocation = pipeline.get("allocation")
    if isinstance(allocation, list) and any(isinstance(r, dict) and isinstance(r.get("model"), str)
                                            for r in allocation):
        models = {f"allocation-{n}": dict(r, purpose=r.get("stage"), key=r.get("model_key") or r.get("key"),
                                          route=r.get("route") or r.get("role"))
                  for n, r in enumerate(allocation) if isinstance(r, dict)}
    rows, seen = [], set()
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
        return {"model": value, "key": model_key_of(value), "source": source}
    if isinstance(value, dict):
        model = value.get("model") or value.get("model_id")
        key = value.get("key") or value.get("model_key") or model_key_of(model)
        if isinstance(model, str) or isinstance(key, str):
            return {"model": model if isinstance(model, str) else None, "key": key if isinstance(key, str) else None,
                    "source": source}
    return None


def verifier_unit_models(judge_dir, agent_id, key_models=None):
    """{finding id: model entry} from the agent's verifier units (files named *verif*.json)."""
    folder = Path(judge_dir) / "units" / agent_id
    out = {}
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("**/*verif*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        unit = read_json(path)
        if not isinstance(unit, dict) or stage_of(unit.get("purpose")) != "verify" or unit.get("status") != "ok":
            continue
        returned = [_dict(s.get("client")).get("model") for s in unit.get("steps") or [] if isinstance(s, dict)]
        returned = [m for m in returned if isinstance(m, str) and m]
        key = unit.get("model") if isinstance(unit.get("model"), str) else None
        model = returned[-1] if returned else (key_models or {}).get(key)
        entry = {"model": model, "key": key or model_key_of(model), "source": "unit file"}
        for verdict in _dict(unit.get("result")).get("verdicts") or []:
            if isinstance(verdict, dict) and isinstance(verdict.get("finding_id"), str):
                out.setdefault(verdict["finding_id"], entry)
    return out


def verifier_model(result, finding, unit_models):
    """Which model verified one finding (module docstring, first match wins), or None."""
    if finding_state(finding) not in VERDICT_STATES:
        return None
    routing = _dict(result.get("verification"))
    for value, source in ((finding.get("verifier_model"), "finding"),
                          (_dict(finding.get("verification")).get("model"), "finding"),
                          (_dict(routing.get("findings")).get(finding["id"]), "routing record"),
                          (_dict(routing.get("units")).get(finding["id"]), "routing record")):
        entry = _model_entry(value, source)
        if entry:
            return entry
    return unit_models.get(finding["id"]) or _model_entry(result.get("verifier_model"), "agent result")


def model_text(entry):
    if not entry:
        return "not recorded"
    model, key = entry.get("model"), entry.get("key")
    text = model or key or "not recorded"
    if key and model and key not in str(model).lower():
        text += f" ({key})"
    return text


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
    """Generic, escaped rendering of judge JSON (run-level output, provenance)."""
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
body{margin:0 auto;max-width:1080px;padding:24px 16px 64px;font:15px/1.55 system-ui,sans-serif;
color:var(--fg);background:var(--bg);overflow-wrap:anywhere}
a{color:var(--accent)}h1{margin:0 0 6px;font-size:1.6rem}h2{margin-top:2rem}
.muted,small{color:var(--muted)}code{font-size:.92em}
.notice{border-left:4px solid var(--accent);background:var(--card);padding:10px 14px;margin:14px 0}
.warn{color:var(--warn);font-weight:600}.ok{color:var(--ok)}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:1px 9px;margin:2px 4px 2px 0;
font-size:.85em}.chip.bad{color:var(--warn)}.chip.good{color:var(--ok)}.chip.warn{color:var(--amber)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;background:var(--card)}
td,th{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:.85em;color:var(--muted)}
pre{white-space:pre-wrap;word-break:break-word;background:var(--pre);padding:8px 10px;border-radius:6px;
margin:6px 0;font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
mark{background:var(--hl);color:inherit}
details>summary{cursor:pointer;color:var(--accent)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:6px 0}dt{color:var(--muted)}dd{margin:0}
@media (max-width:640px){dl{grid-template-columns:1fr}dt{margin-top:6px}}
"""

REVIEW_CSS = """
.finding{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:12px 0}
.finding h3{margin:0 0 6px;font-size:1.02rem}
.finding.good{border-left:5px solid var(--ok)}.finding.bad{border-left:5px solid var(--warn)}
.finding.warn{border-left:5px solid var(--amber)}
.item{border:1px solid var(--line);border-radius:6px;padding:6px 9px;margin:6px 0}
.item.cited{border-color:var(--accent);border-width:2px}.item.host{background:var(--host)}
.item-head{font-size:.82em;color:var(--muted)}
"""


def page(title, body, extra_css=""):
    """A self-contained static page: no scripts, no external resources, no form controls."""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"referrer\" content=\"no-referrer\">"
            f"<title>{esc(title)}</title><style>{BASE_CSS}{extra_css}</style></head><body>{body}</body></html>\n")


def chips(values, cls=""):
    return "".join(f"<span class=\"chip {cls}\">{esc(v)}</span>" for v in values if v not in (None, ""))


def _dl(pairs):
    rows = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in pairs if v not in (None, ""))
    return f"<dl>{rows}</dl>" if rows else ""


def anchor(kind, agent_id, ident):
    return f"{kind}-" + hashlib.sha256(f"{kind}:{agent_id}:{ident}".encode("utf-8")).hexdigest()[:16]


def agent_page_name(agent_id):
    return f"agents/{agent_id}.html"


def flag_text(view):
    """Flag chips' text, with the provisional cohort marked."""
    out = [f.replace("_", " ") for f in view["flags"]]
    if view["provisional"]:
        out.append(f"provisional → {view['candidate_cohort'] or '?'}")
    return out


def level_cell(result, name):
    level, state = category_level(result, name), level_status(result, name)
    text = "–" if level is None else level
    if state == "unresolved" and category_candidate(result, name):
        text += f" → {category_candidate(result, name)}?"
    return f"<td title=\"{esc(state)}\">{esc(text)}</td>"


def _citation_list(entries):
    out = [f"<li><code>{esc(item)}</code>" + (f": “{esc(quote)}”" if quote else "") + "</li>" for item, quote in entries]
    return f"<ul>{''.join(out)}</ul>" if out else ""


def render_item(item, cited_ids, quotes):
    base = item["id"].split("#f")[0]
    cited = item["id"] in cited_ids or base in cited_ids
    host = item.get("visible_to_agent") is False
    flags = [item.get("kind"), item.get("channel"), item.get("agent_id"),
             f"turn {item['turn']}" if type(item.get("turn")) is int else None, item.get("time"),
             "authored by agent" if item.get("authored_by_agent") else "received/other",
             "[HOST-ONLY] not seen by agent" if host else None,
             "derived summary" if item.get("kind") == "summary" else None]
    body = highlight(item["text"], quotes.get(item["id"], []) + quotes.get(base, []))
    return (f"<div class=\"item{' cited' if cited else ''}{' host' if host else ''}\">"
            f"<div class=\"item-head\"><code>{esc(item['id'])}</code> · "
            + " · ".join(esc(f) for f in flags if f) + ("" if not cited else " · <strong>cited</strong>")
            + f"</div><pre>{body}</pre></div>")


def render_passages(index, cited, open_=True):
    """Full text of every cited item plus its neighbours, from the saved judge package."""
    if not index.sequences:
        return "<p class=\"warn\">No saved judge package for this agent: passages unavailable.</p>"
    quotes, ids = {}, []
    for item_id, quote in cited:
        ids.append(item_id)
        if quote:
            quotes.setdefault(item_id, []).append(quote)
    passages, missing = index.passages(list(dict.fromkeys(ids)))
    parts = [f"<div><small>package sequence {s + 1}, items {start + 1}–{stop}</small>"
             + "".join(render_item(item, set(ids), quotes) for item in items) + "</div>"
             for s, start, stop, items in passages]
    warning = (f"<p class=\"warn\">Cited item(s) not found in the saved package: {esc(', '.join(missing))}</p>"
               if missing else "")
    count = sum(len(p[3]) for p in passages)
    return (warning + f"<details{' open' if open_ else ''}><summary>Evidence passages ({count} items, full "
            "text)</summary>" + "".join(parts) + "</details>")


def render_finding(agent_id, finding, result, index, unit_models):
    state = finding_state(finding)
    label, tone = verification_label(state, finding)
    source, window = finding_source(result, finding)
    badges = [("borderline", finding.get("borderline") is True),
              (f"prefilter miss ({window})", source == "random_sample"),
              ("cites random-sample and selected items", is_mixed(result, finding))]
    adjustment = finding_adjustment(finding)
    claimed, established = finding_level(finding)
    model = verifier_model(result, finding, unit_models)
    verifier = (f"{state} by {model_text(model)}" if model else f"{state} (verifier model not recorded)") \
        if state in VERDICT_STATES else state
    details = _dl([
        ("Verification", f"<span class=\"chip {tone}\">{esc(label)}</span> "
                         + ("counted" if is_verified(finding) else "<span class=\"muted\">not counted</span>")),
        ("Verifier", esc(verifier + (f": {finding_reason(finding)}" if finding_reason(finding) else ""))),
        ("Level", esc(f"claimed {claimed}, established {established or 'none'}" if claimed else None)),
        ("Adjusted to", esc(json.dumps(adjustment, ensure_ascii=False, sort_keys=True)
                            if isinstance(adjustment, (dict, list)) else adjustment)),
        ("Host notes", esc("; ".join(finding_notes(finding))) or None),
        ("Summary", esc(finding_summary(finding))),
        ("Evidence", _citation_list(citations(finding))),
        ("Counter-evidence", _citation_list(citations(finding, "counter_evidence"))),
        ("Prefilter source", esc(" ".join(str(x) for x in (source, window) if x))),
    ])
    cited = citations(finding) + citations(finding, "counter_evidence")
    return (f"<article class=\"finding {tone}\" id=\"{anchor('finding', agent_id, finding['id'])}\">"
            f"<h3>Finding <code>{esc(finding['id'])}</code> · {esc(finding_category(finding))} · "
            f"{esc(finding_claim(finding))} <span class=\"chip {tone}\">{esc(label)}</span>"
            f"{chips([name for name, on in badges if on], 'bad')}</h3>{details}"
            + render_passages(index, cited, open_=tone != "bad") + "</article>")


def render_categories(agent_id, result):
    rows = []
    for name in CATEGORIES:
        related = [f for f in findings_of(result) if finding_category(f) == name]
        links = "<br>".join(f"<a href=\"#{anchor('finding', agent_id, f['id'])}\"><code>{esc(f['id'])}</code></a> "
                            f"{esc(finding_claim(f))}: {esc(verification_label(finding_state(f), f)[0])}"
                            for f in related) or "–"
        facts = category_facts(result, name)[1:]
        rows.append(f"<tr><td>{esc(CATEGORY_NAMES[name])}</td>{level_cell(result, name)}"
                    f"<td>{esc(level_status(result, name))}</td><td>{esc('; '.join(facts)) or '–'}</td>"
                    f"<td>{links}</td></tr>")
    return ("<div class=\"scroll\"><table><tr><th>Category</th><th>Verified level</th><th>Status</th>"
            "<th>Attributes</th><th>Findings</th></tr>" + "".join(rows) + "</table></div>")


def mode_banner(mode):
    return "" if mode == "paid" else \
        f"<p class=\"notice warn\">Mode: {esc(mode)}. These are not real judge classifications.</p>"


def render_agent_page(run_id, agent_id, result, index, unit_models, labels, mode, built_at):
    view = cohort_view(result)
    note, source = agent_note(result)
    findings = sorted(findings_of(result), key=finding_order)
    own_labels = [{"label": r.get("label"), "status": r.get("status"),
                   "finding": _dict(r.get("attributes")).get("judge_finding_id")}
                  for r in labels if r.get("agent_id") == agent_id]
    disagreements, judge_only = judge_evaluator_disagreements(result), judge_only_submissions(result)
    body = (f"<p><a href=\"../index.html\">← All agents</a></p><h1>{esc(agent_id)}</h1>"
            f"<p class=\"muted\"><code>{esc(run_id)}</code> · built {esc(built_at)} · mode {esc(mode)} · read-only</p>"
            + mode_banner(mode)
            + "<p class=\"notice\">Researcher-only evidence browser. All transcript text is untrusted data shown "
            "verbatim; returned reasoning is self-report.</p>"
            f"<p>Cohort <strong>{esc(view['cohort'] or result.get('status'))}</strong>"
            + (f" · paper cohort {esc(view['paper_cohort'])}" if view["paper_cohort"] else "")
            + f" {chips(flag_text(view))}</p><p>{esc(note)} <small>({esc(source)} note)</small></p>"
            + (f"<p>Judge–evaluator disagreement (not counted): {esc(', '.join(disagreements))}</p>"
               if disagreements else "")
            + (f"<p>Judge-only submissions awaiting comparator evaluation: {esc(', '.join(judge_only))}</p>"
               if judge_only else "")
            + "<h2>Categories</h2>" + render_categories(agent_id, result)
            + f"<h2>Findings ({len(findings)})</h2><p class=\"muted\">Every finding, refuted ones included; only "
            "verified findings count.</p>"
            + "".join(render_finding(agent_id, f, result, index, unit_models) for f in findings)
            + "<h2>Frozen-rubric labels</h2>" + (render_value(own_labels) if own_labels else
                                                 "<p class=\"muted\">None.</p>")
            + "<h2>Provenance</h2>" + render_value({
                "judge_model": result.get("judge_model"), "verifier_model": result.get("verifier_model"),
                "judge_rubric_version": result.get("judge_rubric_version"),
                "judge_rubric_sha256": result.get("judge_rubric_sha256"), "prefilter": result.get("prefilter") or {}}))
    return page(f"Judge evidence {agent_id}", body, REVIEW_CSS)


def render_open_items(items):
    if not items:
        return "<p class=\"muted\">None.</p>"
    return "<ul>" + "".join(f"<li>{esc(i['agent_id'])}: {esc(i['kind'])}" + (f" — {esc(i['detail'])}" if i["detail"]
                                                                              else "") + "</li>" for i in items) + "</ul>"


def render_index(run_id, results, run_level, mode, built_at, miss):
    rows = []
    totals = Counter()
    for agent_id, result in sorted(results.items()):
        view, counts = cohort_view(result), finding_counts(result)
        totals.update(counts)
        name = esc(agent_id)
        if has_page(result):
            name = f"<a href=\"{esc(agent_page_name(agent_id))}\">{name}</a>"
        rows.append(f"<tr><td>{name}</td><td>{esc(view['cohort'] or result['status'])}</td>"
                    f"<td>{chips(flag_text(view))}</td>" + "".join(level_cell(result, c) for c in CATEGORIES)
                    + f"<td>{counts['verified']} / {counts['refuted']} / {counts['unverified']}</td></tr>")
    rate, ci = miss.get("miss_rate"), miss.get("wilson95")
    body = (f"<h1>Behavior judge evidence</h1><p class=\"muted\"><code>{esc(run_id)}</code> · built "
            f"{esc(built_at)} · mode {esc(mode)} · read-only</p>" + mode_banner(mode)
            + "<p class=\"notice\">Researcher-only, read-only evidence browser behind the five-minute report "
            "(judge rubric gdm-judge-rubric/2, post-hoc). Agents with a judge finding link to their full "
            "evidence, refuted findings included. It collects no marks and changes no labels.</p>"
            f"<p>{len(results)} agents; findings {totals['verified']} verified, {totals['refuted']} refuted, "
            f"{totals['unverified']} unverified. Prefilter miss rate {esc('n/a' if rate is None else f'{rate:.1%}')} "
            f"({miss.get('windows_with_misses', 0)} of {miss.get('windows_sampled', 0)} random-sample windows"
            + (f"; 95% Wilson interval {ci[0]:.1%}–{ci[1]:.1%}" if ci else "") + ").</p>"
            "<h2 id=\"agents\">Agents</h2><div class=\"scroll\"><table><tr><th>Agent</th><th>Cohort</th>"
            "<th>Flags</th>" + "".join(f"<th>{esc(CATEGORY_NAMES[c])}</th>" for c in CATEGORIES)
            + "<th>Findings (verified / refuted / unverified)</th></tr>" + "".join(rows) + "</table></div>"
            + "<h2 id=\"open\">Unresolved and ungraded (never counted as negatives)</h2>"
            + render_open_items(open_items(results))
            + "<h2 id=\"runlevel\">Run-level record</h2>"
            + (f"<details><summary>Full run-level record</summary>{render_value(run_level)}</details>"
               if run_level is not None else "<p class=\"muted\">No run-level output saved.</p>"))
    return page(f"Judge evidence {run_id}", body, REVIEW_CSS)


def build_review_page(run_dir, judge_dir=None, seed=None, *, now=None):
    """Write the read-only evidence browser under judge_dir/review/; returns a summary dict.
    `seed` is accepted for older callers and ignored: nothing is sampled."""
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
    review = judge_dir / "review"
    pages, models = {}, Counter()
    for agent_id, result in sorted(results.items()):
        if not has_page(result):
            continue
        unit_models = verifier_unit_models(judge_dir, agent_id, key_models)
        for finding in findings_of(result):
            if finding_state(finding) in VERDICT_STATES:
                entry = verifier_model(result, finding, unit_models)
                models[(entry or {}).get("key") or "not recorded"] += 1
        text = render_agent_page(run_id, agent_id, result, PackageIndex(load_package(judge_dir, agent_id)),
                                 unit_models, labels, mode, built_at)
        name = agent_page_name(agent_id)
        write_text_atomic(review / name, text)
        pages[name] = sha256_bytes(text.encode("utf-8"))
    index = render_index(run_id, results, run_level, mode, built_at, prefilter_miss_rate(results))
    write_text_atomic(review / "index.html", index)
    pages["index.html"] = sha256_bytes(index.encode("utf-8"))
    produced = set(pages) | {"manifest.json"}
    unrecognized = sorted(p.relative_to(review).as_posix() for p in review.rglob("*")
                          if p.is_file() and not p.name.startswith(".") and p.relative_to(review).as_posix() not in produced)
    totals = Counter()
    for result in results.values():
        totals.update(finding_counts(result))
    counts = {"agents": len(results), "graded": sum(graded(r) for r in results.values()),
              "agent_pages": len(pages) - 1, "findings": dict(sorted(totals.items())),
              "verifier_findings_by_model": dict(sorted(models.items()))}
    manifest = {"schema": MANIFEST_SCHEMA, "run_id": run_id, "built_at": built_at, "mode": mode, "read_only": True,
                "inputs_sha256": input_digests(judge_dir), "pages": dict(sorted(pages.items())), "counts": counts,
                "unrecognized_files": unrecognized}
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    write_json(review / "manifest.json", manifest)
    return {"page": str(review / "index.html"), "agent_pages": counts["agent_pages"],
            "manifest": str(review / "manifest.json"), "counts": counts,
            "manifest_sha256": manifest["manifest_sha256"], "unrecognized_files": unrecognized}


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
    for page_path in sorted((judge_dir / "review" / "agents").glob("*.html")):
        name = page_path.relative_to(judge_dir / "review").as_posix()
        if name not in pages:  # e.g. a page left by an earlier build; never deleted automatically
            problems.append(f"page {name}: not produced by this build (remove it)")
    for name, digest in pages.items():
        page_path = judge_dir / "review" / name
        if not page_path.is_file() or page_path.is_symlink() or sha256_file(page_path) != digest:
            problems.append(f"page {name}: missing or edited")
    if problems:
        info.update(status="stale", problems=problems[:50], problem_count=len(problems))
    return info


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only behavior judge evidence browser (offline).")
    parser.add_argument("command", nargs="?", choices=("build",), default="build")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--judge-dir", type=Path)
    args = parser.parse_args(argv)
    judge_dir = args.judge_dir or default_judge_dir(args.run_dir)
    print(json.dumps(build_review_page(args.run_dir, judge_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
