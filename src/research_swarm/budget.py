"""Atomic host-only cost admission. Uncertain charges never release reservations.

Request states (one row per admitted request in ``calls``):

* ``reserved`` -> ``settle()`` -> ``settled`` | ``uncertain`` | ``estimate_violation``.
* ``uncertain`` (and, explicitly, a stale ``reserved`` hold whose process died) ->
  ``reconcile()`` -> ``settled`` | ``estimate_violation``. Every reconciliation writes
  an audit row; ``settle()`` never touches an uncertain request, so nothing is
  released silently.
* ``estimate_violation`` (billed more than reserved) blocks new admissions in its
  scope and keeps the actual cost counted. ``acknowledge_violation()`` records a
  researcher's review (audit row) and unblocks the scope; the cost stays counted.
  Handling: stop the run, inspect the request, correct the estimator (prices, token
  bound, max_tokens semantics), tell the user, and only then acknowledge.

Spending is never reset: caps are fixed per ledger file and the authoritative
ledger lives outside run scratch (``DEFAULT_LEDGER_PATH``). The only cap change is
``raise_cap()``: an explicit, user-approved raise of one scope to its current
approved value in ``DEFAULT_CAPS`` (e.g. a ledger created while the compatibility
cap was $0), recorded in the ``cap_changes`` audit table. Caps are never lowered
and recorded spending is never touched.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.parse
import uuid
from contextlib import closing
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

SCALE = Decimal(1_000_000_000)

# User decisions, October 13, 2026 (docs/plan.md): $20 pilot, $5 analysis, and a
# separate $2 paid compatibility allowance ("Live compatibility check" supersedes
# the earlier unpaid-only $0 compatibility cap). All scopes are outside each other.
# October 15: a fresh baseline received an ADDITIONAL $100. Preserve the first
# run's settled spending in the same ledger; this is a cumulative ceiling.
INITIAL_PILOT_SETTLED_USD = "19.636804044"
REPEAT_PILOT_ALLOWANCE_USD = "100"
FIRST_REPEAT_CAP_USD = "119.636804044"
EIGHT_GIB_PRIOR_SETTLED_USD = "105.727998474"
EIGHT_GIB_PRIOR_HELD_USD = "2.3276232"
EIGHT_GIB_PRIOR_COMMITTED_USD = "108.055621674"
EIGHT_GIB_CAP_USD = "208.055621674"
# Exact immutable call history at the authorized second-repeat boundary. The hash
# includes request identities, reservations and states, not just cost aggregates.
EIGHT_GIB_PRIOR_CALLS_SHA256 = "4bcff206b4726c9c651ffc543762b140289a3e29f14e12f90fe6afc08cce23a4"
# User-authorized fresh cached-counter run; this replaces the unused remainder.
CACHE_REPEAT_PRIOR_SETTLED_USD = "205.049799792"
CACHE_REPEAT_PRIOR_HELD_USD = "2.75986035"
CACHE_REPEAT_PRIOR_COMMITTED_USD = "207.809660142"
CACHE_REPEAT_CAP_USD = "307.809660142"
CACHE_REPEAT_PRIOR_CALLS_SHA256 = "5e0678ca0dd35c661d0633644d5461459f54910c3876f9296cc57216880199c9"
DEFAULT_CAPS = {"pilot": CACHE_REPEAT_CAP_USD, "analysis": 5, "compatibility": 2}

# How a settled amount was established. Only these bases release a reservation.
# ``unbilled_error``: the provider rejected the request before any generation
# began (an HTTP error status such as 400/401/402/403/429 with no generation id);
# its cost must be exactly 0. Timeouts, disconnects, 5xx and cancellations are
# not certain to be unbilled and stay uncertain until reconciled.
SETTLEMENT_BASES = ("provider_reported_cost", "usage_priced_upper_bound", "unbilled_error")

# ``manual_reconciliation``: a researcher entered the billed amount after the user
# approved it (scripts/reconcile_ledger.py --manual), e.g. for an exception-path
# hold without a generation id. Accepted only by ``reconcile()``, never by the
# runtime's ``settle()``; the audit row carries the note naming the billing source.
MANUAL_BASIS = "manual_reconciliation"
RECONCILIATION_BASES = (*SETTLEMENT_BASES, MANUAL_BASIS)

UNRESOLVED_STATES = ("reserved", "uncertain")

# Pre-October-13 location inside git-ignored run scratch; launch gates refuse to
# proceed while a ledger exists there (its prior spending must not be ignored).
LEGACY_LEDGER_RELATIVE = Path("runs") / "budget.sqlite"

_CAP_CHANGES = ("CREATE TABLE IF NOT EXISTS cap_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,"
                " previous_cap INTEGER NOT NULL, new_cap INTEGER NOT NULL, note TEXT NOT NULL,"
                " created TEXT DEFAULT CURRENT_TIMESTAMP)")
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS scopes (name TEXT PRIMARY KEY, cap INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, scope TEXT NOT NULL, agent TEXT NOT NULL,"
    " reserved INTEGER NOT NULL, actual INTEGER, state TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP,"
    " basis TEXT, settled TEXT)",
    "CREATE TABLE IF NOT EXISTS reconciliations (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,"
    " action TEXT NOT NULL, previous_state TEXT NOT NULL, previous_actual INTEGER, new_state TEXT NOT NULL,"
    " new_actual INTEGER, basis TEXT, note TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP)",
    _CAP_CHANGES,
)


def DEFAULT_LEDGER_PATH(repo_root) -> Path:  # noqa: N802 (agreed interface name)
    """Authoritative cross-run ledger: <repo>/state/budget.sqlite (git-ignored, never in runs/)."""
    return Path(repo_root) / "state" / "budget.sqlite"


class BudgetExceeded(RuntimeError):
    pass


def units(value):
    value = Decimal(str(value))
    if not value.is_finite() or value < 0:
        raise ValueError("Cost must be finite and nonnegative")
    return int((value * SCALE).to_integral_value(rounding=ROUND_CEILING))


def usd(amount):
    return str(Decimal(amount) / SCALE)


def _check_basis(cost, basis, allowed=SETTLEMENT_BASES):
    if basis not in allowed:
        raise ValueError("Unknown settlement basis")
    if basis == "unbilled_error" and (cost is None or units(cost) != 0):
        raise ValueError("unbilled_error settles with cost exactly 0")


def _snapshot(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {row[1] for row in db.execute("PRAGMA table_info(calls)")}
    unbilled = "COALESCE(SUM(state='settled' AND basis='unbilled_error'),0)" if "basis" in columns else "0"
    manual = f"COALESCE(SUM(basis='{MANUAL_BASIS}'),0)" if "basis" in columns else "0"
    result = {}
    for scope, cap in db.execute("SELECT name,cap FROM scopes ORDER BY name").fetchall():
        (total, unresolved, uncertain, in_flight, violation, acknowledged, settled, settled_amount, held,
         unbilled_count, manual_count) = db.execute(
            "SELECT COALESCE(SUM(COALESCE(actual,reserved)),0),"
            "COALESCE(SUM(state IN ('reserved','uncertain')),0),"
            "COALESCE(SUM(state='uncertain'),0),"
            "COALESCE(SUM(state='reserved'),0),"
            "COALESCE(SUM(state='estimate_violation'),0),"
            "COALESCE(SUM(state='violation_acknowledged'),0),"
            "COALESCE(SUM(state='settled'),0),"
            "COALESCE(SUM(CASE WHEN actual IS NOT NULL THEN actual ELSE 0 END),0),"
            "COALESCE(SUM(CASE WHEN state IN ('reserved','uncertain') THEN reserved ELSE 0 END),0),"
            f"{unbilled},{manual} FROM calls WHERE scope=?", (scope,)).fetchone()
        audits = raises = 0
        if "reconciliations" in tables:
            audits = db.execute("SELECT COUNT(*) FROM reconciliations r JOIN calls c ON c.id=r.request_id "
                                "WHERE c.scope=?", (scope,)).fetchone()[0]
        if "cap_changes" in tables:
            raises = db.execute("SELECT COUNT(*) FROM cap_changes WHERE scope=?", (scope,)).fetchone()[0]
        result[scope] = {
            "cap_usd": usd(cap), "committed_usd": usd(total), "remaining_usd": usd(max(0, cap - total)),
            # committed = known costs (settled_usd: every recorded actual, violations included)
            # + full reservations still held by unresolved requests (held_usd).
            "settled_usd": usd(settled_amount), "held_usd": usd(held),
            "unresolved_requests": unresolved, "uncertain_requests": uncertain, "in_flight_requests": in_flight,
            "settled_requests": settled, "unbilled_error_requests": unbilled_count,
            "violation": bool(violation), "acknowledged_violations": acknowledged, "reconciliations": audits,
            "manual_reconciliations": manual_count, "cap_raises": raises}
    return result


def _read_only(path):
    db = sqlite3.connect(f"file:{urllib.parse.quote(str(Path(path).resolve()))}?mode=ro", uri=True, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    return db


def read_snapshot(path):
    """Read-only snapshot of an existing ledger (None when absent). Never creates or migrates."""
    path = Path(path)
    if not path.exists():
        return None
    with closing(_read_only(path)) as db:
        return _snapshot(db)


def list_requests(path, states=UNRESOLVED_STATES, scope=None):
    """Read-only listing of requests in ``states`` (default: unresolved holds)."""
    path = Path(path)
    if not path.exists():
        return []
    marks = ",".join("?" * len(states))
    query = f"SELECT id,scope,agent,reserved,actual,state,created FROM calls WHERE state IN ({marks})"
    params = list(states)
    if scope is not None:
        query += " AND scope=?"
        params.append(scope)
    with closing(_read_only(path)) as db:
        rows = db.execute(query + " ORDER BY created,id", params).fetchall()
    return [{"request_id": r[0], "scope": r[1], "agent": r[2], "reserved_usd": usd(r[3]),
             "actual_usd": None if r[4] is None else usd(r[4]), "state": r[5], "created": r[6]} for r in rows]


def cap_changes(path):
    """Read-only audit trail of approved cap raises ([] when absent)."""
    path = Path(path)
    if not path.exists():
        return []
    with closing(_read_only(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cap_changes'").fetchone():
            return []
        rows = db.execute("SELECT id,scope,previous_cap,new_cap,note,created FROM cap_changes ORDER BY id").fetchall()
    return [{"id": r[0], "scope": r[1], "previous_cap_usd": usd(r[2]), "new_cap_usd": usd(r[3]), "note": r[4],
             "created": r[5]} for r in rows]


def _pilot_calls_sha256(db):
    rows = db.execute("SELECT id,scope,agent,reserved,actual,state,created,basis,settled "
                      "FROM calls WHERE scope='pilot' ORDER BY id").fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _eight_gib_prior_problems(db, *, after_raise):
    """Exact authorized carry-over; never waive/release an uncertain reservation."""
    pilot = _snapshot(db).get("pilot", {})
    problems = []
    expected = {"settled_usd": EIGHT_GIB_PRIOR_SETTLED_USD, "held_usd": EIGHT_GIB_PRIOR_HELD_USD,
                "committed_usd": EIGHT_GIB_PRIOR_COMMITTED_USD, "uncertain_requests": 7,
                "unresolved_requests": 7, "in_flight_requests": 0, "violation": False,
                "acknowledged_violations": 0,
                "cap_usd": EIGHT_GIB_CAP_USD if after_raise else FIRST_REPEAT_CAP_USD}
    if any(pilot.get(key) != value for key, value in expected.items()):
        problems.append("8 GiB repeat requires the exact approved prior spending and seven preserved uncertain holds; "
                        "no new spending, reserved calls or violations")
    changes = db.execute("SELECT previous_cap,new_cap FROM cap_changes WHERE scope='pilot' ORDER BY id").fetchall()
    expected_changes = [(units(20), units(FIRST_REPEAT_CAP_USD))]
    if after_raise:
        expected_changes.append((units(FIRST_REPEAT_CAP_USD), units(EIGHT_GIB_CAP_USD)))
    if changes != expected_changes:
        problems.append("8 GiB repeat requires the original audited cap raise history; never use a replacement ledger")
    if _pilot_calls_sha256(db) != EIGHT_GIB_PRIOR_CALLS_SHA256:
        problems.append("8 GiB repeat prior request history differs from the approved ledger snapshot")
    return problems


def eight_gib_prior_problems(path):
    """Read-only launch check, bound to all prior pilot calls and both cap raises."""
    if not Path(path).exists():
        return ["8 GiB repeat requires the existing cumulative pilot ledger; never create a replacement ledger"]
    with closing(_read_only(path)) as db:
        return _eight_gib_prior_problems(db, after_raise=True)


def _cache_repeat_prior_problems(db, *, after_raise):
    """Bind this one approval to the exact existing ledger, preserving eight holds."""
    pilot = _snapshot(db).get("pilot", {})
    expected = {"settled_usd": CACHE_REPEAT_PRIOR_SETTLED_USD,
                "held_usd": CACHE_REPEAT_PRIOR_HELD_USD,
                "committed_usd": CACHE_REPEAT_PRIOR_COMMITTED_USD,
                "uncertain_requests": 8, "unresolved_requests": 8,
                "in_flight_requests": 0, "violation": False,
                "acknowledged_violations": 0,
                "cap_usd": CACHE_REPEAT_CAP_USD if after_raise else EIGHT_GIB_CAP_USD}
    problems = []
    if any(pilot.get(key) != value for key, value in expected.items()):
        problems.append("cached repeat requires the exact approved prior spending and eight preserved uncertain "
                        "holds; no new spending, reserved calls or violations")
    changes = db.execute("SELECT previous_cap,new_cap FROM cap_changes WHERE scope='pilot' ORDER BY id").fetchall()
    expected_changes = [(units(20), units(FIRST_REPEAT_CAP_USD)),
                        (units(FIRST_REPEAT_CAP_USD), units(EIGHT_GIB_CAP_USD))]
    if after_raise:
        expected_changes.append((units(EIGHT_GIB_CAP_USD), units(CACHE_REPEAT_CAP_USD)))
    if changes != expected_changes:
        problems.append("cached repeat requires the original audited cap raise history; never use a replacement ledger")
    if _pilot_calls_sha256(db) != CACHE_REPEAT_PRIOR_CALLS_SHA256:
        problems.append("cached repeat prior request history differs from the approved ledger snapshot")
    return problems


def cache_repeat_prior_problems(path):
    """Read-only launch check after the one approved raise; never releases holds."""
    if not Path(path).exists():
        return ["cached repeat requires the existing cumulative pilot ledger; never create a replacement ledger"]
    with closing(_read_only(path)) as db:
        return _cache_repeat_prior_problems(db, after_raise=True)


def raise_cap(path, scope, new_cap, approval_note):
    """Raise one scope of an EXISTING ledger to its approved cap, with an audit row.

    For a ledger created before a recorded user decision raised an allowance (the
    compatibility scope was $0 until the October 13 "Live compatibility check"
    decision made it $2), which ``BudgetLedger`` otherwise refuses to open. Only an
    increase to exactly ``DEFAULT_CAPS[scope]`` is accepted, ``approval_note`` must
    record the user's approval, and recorded spending is untouched (prior costs and
    holds stay counted against the raised cap). Never called automatically.
    """
    note = str(approval_note or "").strip()
    if not note:
        raise ValueError("A cap raise requires a note recording the user's approval")
    if scope not in DEFAULT_CAPS:
        raise ValueError(f"Unknown budget scope {scope!r}")
    target = units(new_cap)
    if target != units(DEFAULT_CAPS[scope]) and not (
            scope == "pilot" and target in (units(FIRST_REPEAT_CAP_USD), units(EIGHT_GIB_CAP_USD))):
        raise ValueError(f"A cap may only be raised to its approved value ({scope}: ${DEFAULT_CAPS[scope]})")
    path = Path(path)
    if not path.exists():
        raise ValueError(f"No ledger at {path}; a new ledger is created with DEFAULT_CAPS")
    with closing(sqlite3.connect(str(path), timeout=30)) as db, db:
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("BEGIN EXCLUSIVE")
        db.execute(_CAP_CHANGES)
        row = db.execute("SELECT cap FROM scopes WHERE name=?", (scope,)).fetchone()
        if row is None:
            raise ValueError(f"Ledger has no {scope} scope; opening BudgetLedger adds it at the approved cap")
        if row[0] >= target:
            raise ValueError(f"{scope} cap is already {usd(row[0])}; caps are never lowered or reset")
        if scope == "pilot" and target == units("119.636804044"):
            pilot = _snapshot(db)["pilot"]
            if (pilot["settled_usd"] != INITIAL_PILOT_SETTLED_USD
                    or pilot["unresolved_requests"] or pilot["violation"]
                    or row[0] != units(20)):
                raise ValueError("The additional $100 requires the original $20 pilot ledger with exactly "
                                 f"${INITIAL_PILOT_SETTLED_USD} settled and no unresolved holds or violations")
        if scope == "pilot" and target == units(EIGHT_GIB_CAP_USD):
            problems = _eight_gib_prior_problems(db, after_raise=False)
            if problems:
                raise ValueError("; ".join(problems))
        if scope == "pilot" and target == units(CACHE_REPEAT_CAP_USD):
            problems = _cache_repeat_prior_problems(db, after_raise=False)
            if problems:
                raise ValueError("; ".join(problems))
        db.execute("UPDATE scopes SET cap=? WHERE name=?", (target, scope))
        db.execute("INSERT INTO cap_changes (scope,previous_cap,new_cap,note) VALUES (?,?,?,?)",
                   (scope, row[0], target, note))
    return {"scope": scope, "previous_cap_usd": usd(row[0]), "new_cap_usd": usd(target), "note": note}


class BudgetLedger:
    """SQLite BEGIN IMMEDIATE admission is shared by all processes and agents.

    The ledger guards a scope, not a per-agent allowance. Actual provider billing
    can exceed an estimate: such a violation blocks further calls and is retained.
    """
    def __init__(self, path: str | Path, caps=None):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            # One exclusive transaction: concurrent first opens (or a migration of an
            # older ledger lacking basis/settled) cannot race on CREATE/ALTER.
            db.execute("BEGIN EXCLUSIVE")
            for statement in _SCHEMA:
                db.execute(statement)
            columns = {row[1] for row in db.execute("PRAGMA table_info(calls)")}
            for column in ("basis", "settled"):
                if column not in columns:
                    db.execute(f"ALTER TABLE calls ADD COLUMN {column} TEXT")
            for scope, cap in (caps or DEFAULT_CAPS).items():
                existing = db.execute("SELECT cap FROM scopes WHERE name=?", (scope,)).fetchone()
                if existing and existing[0] != units(cap):
                    raise ValueError(f"Existing ledger {scope} cap {usd(existing[0])} differs from {cap}; never reset "
                                     "spending (an approved raise uses budget.raise_cap, via scripts/preflight.py "
                                     "--raise-ledger-cap)")
                db.execute("INSERT OR IGNORE INTO scopes VALUES (?,?)", (scope, units(cap)))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def reserve(self, scope, agent, cost):
        amount = units(cost)
        if amount <= 0:
            raise ValueError("Paid requests require positive conservative reservation")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT cap FROM scopes WHERE name=?", (scope,)).fetchone()
            if row is None:
                raise ValueError("Unknown budget scope")
            if db.execute("SELECT 1 FROM calls WHERE scope=? AND state='estimate_violation' LIMIT 1", (scope,)).fetchone():
                raise BudgetExceeded("A reservation estimate was exceeded; reconcile before further requests")
            used = db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM calls WHERE scope=?", (scope,)).fetchone()[0]
            if used + amount > row[0]:
                raise BudgetExceeded(f"{scope} allowance cannot cover request")
            request_id = str(uuid.uuid4())
            db.execute("INSERT INTO calls (id,scope,agent,reserved,state) VALUES (?,?,?,?,?)", (request_id,scope,agent,amount,"reserved"))
        return request_id

    def settle(self, request_id, cost=None, basis="provider_reported_cost"):
        """Settle a request still ``reserved``. ``cost=None`` means uncertain billing
        and retains the entire reservation; only ``reconcile()`` can resolve it later.

        ``basis="unbilled_error"`` requires ``cost=0`` (provider rejected the request
        before generation).
        """
        if cost is not None or basis == "unbilled_error":
            _check_basis(cost, basis)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reserved,state FROM calls WHERE id=?", (request_id,)).fetchone()
            if row is None or row[1] != "reserved":
                raise ValueError("Unknown or already settled request (uncertain requests need reconcile())")
            actual = units(cost) if cost is not None else None
            state = "uncertain" if actual is None else ("estimate_violation" if actual > row[0] else "settled")
            db.execute("UPDATE calls SET actual=?,state=?,basis=?,settled=CURRENT_TIMESTAMP WHERE id=?",
                       (actual,state,None if actual is None else basis,request_id))

    def reconcile(self, request_id, cost, basis="provider_reported_cost", note="", *, allow_reserved=False):
        """Resolve an uncertain request once billing is known, with an audit row.

        ``cost`` is required (normally OpenRouter's reported generation cost, see
        scripts/reconcile_ledger.py; ``basis=MANUAL_BASIS`` for a user-approved
        amount entered by hand). A ``reserved`` hold is accepted only with
        ``allow_reserved=True``, after its process is known to have exited without
        settling. A cost above the reservation becomes an estimate_violation.
        """
        if not str(note).strip():
            raise ValueError("Reconciliation requires an audit note")
        if cost is None:
            raise ValueError("Reconciliation requires a known cost")
        _check_basis(cost, basis, RECONCILIATION_BASES)
        allowed = UNRESOLVED_STATES if allow_reserved else ("uncertain",)
        actual = units(cost)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reserved,state,actual FROM calls WHERE id=?", (request_id,)).fetchone()
            if row is None or row[1] not in allowed:
                raise ValueError(f"Request is not reconcilable (state {None if row is None else row[1]})")
            state = "estimate_violation" if actual > row[0] else "settled"
            db.execute("UPDATE calls SET actual=?,state=?,basis=?,settled=CURRENT_TIMESTAMP WHERE id=?",
                       (actual, state, basis, request_id))
            db.execute("INSERT INTO reconciliations (request_id,action,previous_state,previous_actual,new_state,"
                       "new_actual,basis,note) VALUES (?,?,?,?,?,?,?,?)",
                       (request_id, "reconcile", row[1], row[2], state, actual, basis, str(note)))
        return {"request_id": request_id, "previous_state": row[1], "state": state,
                "reserved_usd": usd(row[0]), "actual_usd": usd(actual), "basis": basis}

    def acknowledge_violation(self, request_id, note):
        """Record a researcher's review of an estimate_violation and unblock its scope.

        The billed amount stays counted. Use only after the run has stopped and the
        estimator has been corrected (see module docstring).
        """
        if not str(note).strip():
            raise ValueError("Acknowledgement requires an audit note")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,actual,basis FROM calls WHERE id=?", (request_id,)).fetchone()
            if row is None or row[0] != "estimate_violation":
                raise ValueError("Request is not an unacknowledged estimate violation")
            db.execute("UPDATE calls SET state='violation_acknowledged' WHERE id=?", (request_id,))
            db.execute("INSERT INTO reconciliations (request_id,action,previous_state,previous_actual,new_state,"
                       "new_actual,basis,note) VALUES (?,?,?,?,?,?,?,?)",
                       (request_id, "acknowledge_violation", row[0], row[1], "violation_acknowledged", row[1],
                        row[2], str(note)))

    def audit_log(self):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT id,request_id,action,previous_state,previous_actual,new_state,new_actual,"
                              "basis,note,created FROM reconciliations ORDER BY id").fetchall()
        return [{"id": r[0], "request_id": r[1], "action": r[2], "previous_state": r[3],
                 "previous_actual_usd": None if r[4] is None else usd(r[4]), "new_state": r[5],
                 "new_actual_usd": None if r[6] is None else usd(r[6]), "basis": r[7], "note": r[8],
                 "created": r[9]} for r in rows]

    def can_cover(self, scope, cost):
        """Advisory check (no reservation) used to stop a run once calls cannot be admitted."""
        snapshot = self.snapshot().get(scope)
        if snapshot is None:
            raise ValueError("Unknown budget scope")
        return not snapshot["violation"] and Decimal(snapshot["remaining_usd"]) >= Decimal(str(cost))

    def snapshot(self):
        with closing(self.connect()) as db:
            return _snapshot(db)
