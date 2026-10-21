"""Atomic host-only cost admission: the per-run budget, the one spending control.

Owner decisions (docs/plan.md, "One per-run budget, no manual approvals (October 20, 2026)"):
every paid invocation (a pilot run on any track, the live compatibility check, the behavior
judge) is one *run* with its own budget, specified before it starts. A request is admitted
only if THIS run's committed spend (its own in-flight reservations, settled costs and
uncertain holds, tagged by run id) plus the request's conservative reservation fits the
run's budget (``BudgetLedger.reserve``); otherwise ``BudgetExceeded`` is raised and the run
stops cleanly. There is no cumulative cap, no cap raise, no top-up, no approval note, no
typed confirmation and no snapshot binding.

Budgets: pilot runs and the compatibility check read ``run_budget_usd`` from their config
(``run_budget``; ``DEFAULT_RUN_BUDGET_USD`` = $100 when absent, and the launch prints the
budget it uses). The behavior judge's budget is its fixed run limit (``--run-limit``, else the
config's ``behavior_judge_max_usd``, else ``DEFAULT_JUDGE_RUN_BUDGET_USD`` = $50).
``start_run`` records the run (id, scope, budget, where the budget came from, owning process)
in the ledger's ``runs`` table before its first request.

The ledger stays the shared record of every request (cumulative history per scope,
reconciliation). Request states (one row per admitted request in ``calls``; ``run_id`` names
the run, ``owner`` the reserving process as ``host:pid``):

* ``reserved`` -> ``settle()`` -> ``settled`` | ``uncertain`` | ``estimate_violation``.
* ``uncertain`` -> ``reconcile()`` -> ``settled`` | ``estimate_violation`` (an audit row each;
  ``settle()`` never touches an uncertain request, except that the owning process may still
  settle a hold that was marked as left over while it was alive after all). Unresolved holds
  count against their own run at their full reservation and stay explicit (``uncertain``)
  until a provider record settles them (scripts/reconcile_ledger.py).
* ``estimate_violation`` (billed more than reserved) is recorded and the run continues: it
  needs no acknowledgement. The run's ``reservation_factor`` is raised automatically to the
  observed ``actual / requested`` ratio, so every later reservation of that run is scaled up
  and is an upper bound again; the billed amount counts against the run's budget.

Leftovers of earlier processes never block a new run (they are not part of its committed
spend). ``start_run`` also marks every ``reserved`` hold whose process is provably gone
(``owner_gone``) as ``uncertain``: still counted against its own run, now explicit as unknown
billing and reconcilable. A live process's rows are never touched.

Older ledgers keep their ``scopes`` caps and ``cap_changes`` rows as history
(``legacy_cap_usd`` in the snapshot); nothing reads them for admission. The authoritative
ledger lives outside run scratch (``DEFAULT_LEDGER_PATH``).
"""
from __future__ import annotations

import datetime
import os
import socket
import sqlite3
import urllib.parse
import uuid
from contextlib import closing
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path

SCALE = Decimal(1_000_000_000)

# Owner decision, October 20, 2026: "The per run budget should always be specified before
# each run; default to $100 a run" (pilot runs on any track and the compatibility check).
RUN_BUDGET_KEY = "run_budget_usd"
DEFAULT_RUN_BUDGET_USD = Decimal("100")
# The behavior judge's existing fixed run limit (docs/plan.md, "No cost estimates for judge
# runs, October 20, 2026"): its budget when neither --run-limit nor behavior_judge_max_usd is set.
DEFAULT_JUDGE_RUN_BUDGET_USD = Decimal("50")

# How a settled amount was established. Only these bases release a reservation.
# ``unbilled_error``: the provider rejected the request before any generation
# began (an HTTP error status such as 400/401/402/403/429 with no generation id);
# its cost must be exactly 0. Timeouts, disconnects, 5xx and cancellations are
# not certain to be unbilled and stay uncertain until reconciled.
SETTLEMENT_BASES = ("provider_reported_cost", "usage_priced_upper_bound", "unbilled_error")

# ``manual_reconciliation``: an amount entered by hand from a billing record
# (scripts/reconcile_ledger.py --manual), e.g. for an exception-path hold without a
# generation id. Accepted only by ``reconcile()``, never by the runtime's ``settle()``;
# the audit row carries the note naming the source.
MANUAL_BASIS = "manual_reconciliation"
RECONCILIATION_BASES = (*SETTLEMENT_BASES, MANUAL_BASIS)

UNRESOLVED_STATES = ("reserved", "uncertain")
VIOLATION_STATES = ("estimate_violation", "violation_acknowledged")  # the second only in older ledgers
LEDGER_STATES = (*UNRESOLVED_STATES, "settled", *VIOLATION_STATES)
# Marks an ``uncertain`` hold whose process was found gone (``start_run``) or whose run ended
# without settling it (``end_run``). Not a settlement basis: the hold stays counted.
ORPHANED_BASIS = "orphaned_hold"
# A row without an owner predates owner tracking; it counts as left over only once it is at
# least this old (a process started with older code is never mistaken for a dead one).
ORPHAN_MIN_AGE_HOURS = 24

# Pre-October-13 location inside git-ignored run scratch. Its history is not in the
# authoritative ledger; the launch gates warn about it (it never affects admission).
LEGACY_LEDGER_RELATIVE = Path("runs") / "budget.sqlite"

_SCHEMA = (
    # Legacy per-scope caps (history only; never read for admission).
    "CREATE TABLE IF NOT EXISTS scopes (name TEXT PRIMARY KEY, cap INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, scope TEXT NOT NULL, agent TEXT NOT NULL,"
    " reserved INTEGER NOT NULL, actual INTEGER, state TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP,"
    " basis TEXT, settled TEXT, run_id TEXT, requested INTEGER, owner TEXT)",
    "CREATE TABLE IF NOT EXISTS reconciliations (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,"
    " action TEXT NOT NULL, previous_state TEXT NOT NULL, previous_actual INTEGER, new_state TEXT NOT NULL,"
    " new_actual INTEGER, basis TEXT, note TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP)",
    # Legacy audited cap changes (history only).
    "CREATE TABLE IF NOT EXISTS cap_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,"
    " previous_cap INTEGER NOT NULL, new_cap INTEGER NOT NULL, note TEXT NOT NULL,"
    " created TEXT DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, scope TEXT NOT NULL, budget INTEGER NOT NULL,"
    " source TEXT, reservation_factor TEXT NOT NULL DEFAULT '1', owner TEXT, note TEXT,"
    " created TEXT DEFAULT CURRENT_TIMESTAMP, ended TEXT)",
)
# Columns added to older ledgers on open: (table, column, type).
_MIGRATIONS = (("calls", "basis", "TEXT"), ("calls", "settled", "TEXT"), ("calls", "run_id", "TEXT"),
               ("calls", "requested", "INTEGER"), ("calls", "owner", "TEXT"))


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


def budget_amount(value, name="budget"):
    """A run budget as a positive, finite Decimal (``$`` and thousands commas allowed); ValueError otherwise."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} {value!r} is not an amount in USD")
    try:
        amount = Decimal(str(value).strip().lstrip("$").replace(",", ""))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} {value!r} is not an amount in USD") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{name} {value!r} must be a positive, finite amount in USD")
    return amount


def run_budget(config, key=RUN_BUDGET_KEY, default=DEFAULT_RUN_BUDGET_USD):
    """``(budget, source)`` of a run from its config: ``config[key]`` (source ``"config <key>"``),
    or ``default`` when the key is absent or null (source ``"default"``). An invalid value raises
    ValueError (it is never replaced by the default)."""
    value = (config or {}).get(key)
    if value is None:
        return Decimal(default), "default"
    return budget_amount(value, key), f"config {key}"


def describe_budget(amount, source, key=RUN_BUDGET_KEY):
    """The line a launch prints about the budget it uses."""
    text = f"Run budget: ${Decimal(amount)}"
    if source == "default":
        return text + f" (default; set {key} in the config to specify it)"
    return text + f" ({source})"


def _check_basis(cost, basis, allowed=SETTLEMENT_BASES):
    if basis not in allowed:
        raise ValueError("Unknown settlement basis")
    if basis == "unbilled_error" and (cost is None or units(cost) != 0):
        raise ValueError("unbilled_error settles with cost exactly 0")


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _factor(text):
    try:
        value = Decimal(str(text))
    except (InvalidOperation, ValueError):
        return Decimal(1)
    return value if value.is_finite() and value > 1 else Decimal(1)


_TOTALS = ("COALESCE(SUM(COALESCE(actual,reserved)),0),"
           "COALESCE(SUM(state IN ('reserved','uncertain')),0),"
           "COALESCE(SUM(state='uncertain'),0),"
           "COALESCE(SUM(state='reserved'),0),"
           "COALESCE(SUM(state='estimate_violation'),0),"
           "COALESCE(SUM(state='violation_acknowledged'),0),"
           "COALESCE(SUM(state='settled'),0),"
           "COALESCE(SUM(CASE WHEN actual IS NOT NULL THEN actual ELSE 0 END),0),"
           "COALESCE(SUM(CASE WHEN state IN ('reserved','uncertain') THEN reserved ELSE 0 END),0),"
           "COUNT(*)")


def _totals(db, where, params):
    columns = _columns(db, "calls")
    unbilled = "COALESCE(SUM(state='settled' AND basis='unbilled_error'),0)" if "basis" in columns else "0"
    manual = f"COALESCE(SUM(basis='{MANUAL_BASIS}'),0)" if "basis" in columns else "0"
    (total, unresolved, uncertain, in_flight, violation, acknowledged, settled, settled_amount, held, count,
     unbilled_count, manual_count) = db.execute(
        f"SELECT {_TOTALS},{unbilled},{manual} FROM calls WHERE {where}", params).fetchone()
    return {
        # committed = known costs (settled_usd: every recorded actual, violations included)
        # + full reservations still held by unresolved requests (held_usd, unknown billing).
        "committed": total, "committed_usd": usd(total), "settled_usd": usd(settled_amount), "held_usd": usd(held),
        "requests": count, "unresolved_requests": unresolved, "uncertain_requests": uncertain,
        "in_flight_requests": in_flight, "settled_requests": settled, "unbilled_error_requests": unbilled_count,
        "estimate_violations": violation + acknowledged, "violation": bool(violation),
        "acknowledged_violations": acknowledged, "manual_reconciliations": manual_count}


def _snapshot(db):
    """Cumulative history per scope (every request ever recorded). No cap: admission is per run."""
    tables = _tables(db)
    legacy_caps = dict(db.execute("SELECT name,cap FROM scopes").fetchall()) if "scopes" in tables else {}
    scopes = set(legacy_caps) | {row[0] for row in db.execute("SELECT DISTINCT scope FROM calls")}
    has_runs = "runs" in tables and "run_id" in _columns(db, "calls")
    if has_runs:
        scopes |= {row[0] for row in db.execute("SELECT DISTINCT scope FROM runs")}
    result = {}
    for scope in sorted(scopes):
        data = _totals(db, "scope=?", (scope,))
        data.pop("committed")
        audits = changes = runs = 0
        if "reconciliations" in tables:
            audits = db.execute("SELECT COUNT(*) FROM reconciliations r JOIN calls c ON c.id=r.request_id "
                                "WHERE c.scope=? AND r.action IN ('reconcile','late_settlement')", (scope,)).fetchone()[0]
        if "cap_changes" in tables:
            changes = db.execute("SELECT COUNT(*) FROM cap_changes WHERE scope=?", (scope,)).fetchone()[0]
        if has_runs:
            runs = db.execute("SELECT COUNT(*) FROM runs WHERE scope=?", (scope,)).fetchone()[0]
        result[scope] = {**data, "reconciliations": audits, "runs": runs,
                         "legacy_cap_usd": None if scope not in legacy_caps else usd(legacy_caps[scope]),
                         "legacy_cap_changes": changes}
    return result


def _run_record(db, run_id):
    if "runs" not in _tables(db) or "run_id" not in _columns(db, "calls"):
        return None
    row = db.execute("SELECT id,scope,budget,source,reservation_factor,owner,note,created,ended FROM runs WHERE id=?",
                     (run_id,)).fetchone()
    if row is None:
        return None
    data = _totals(db, "run_id=?", (run_id,))
    committed = data.pop("committed")
    return {"run_id": row[0], "scope": row[1], "budget_usd": usd(row[2]), "budget_source": row[3],
            "remaining_usd": usd(max(0, row[2] - committed)), **data,
            "reservation_factor": str(_factor(row[4])), "owner": row[5], "note": row[6], "created": row[7],
            "ended": row[8]}


def _read_only(path):
    db = sqlite3.connect(f"file:{urllib.parse.quote(str(Path(path).resolve()))}?mode=ro", uri=True, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    return db


def read_snapshot(path):
    """Read-only per-scope history of an existing ledger (None when absent). Never creates or migrates."""
    path = Path(path)
    if not path.exists():
        return None
    with closing(_read_only(path)) as db:
        if not {"calls"} <= _tables(db):
            return None
        return _snapshot(db)


def read_run(path, run_id):
    """Read-only state of one run (budget, committed, remaining, holds, violations), or None."""
    path = Path(path)
    if not path.exists():
        return None
    with closing(_read_only(path)) as db:
        return _run_record(db, run_id)


def list_runs(path, scope=None):
    """Read-only list of the recorded runs (oldest first), each as ``read_run`` returns it."""
    path = Path(path)
    if not path.exists():
        return []
    with closing(_read_only(path)) as db:
        if "runs" not in _tables(db):
            return []
        query, params = "SELECT id FROM runs", ()
        if scope is not None:
            query, params = query + " WHERE scope=?", (scope,)
        ids = [row[0] for row in db.execute(query + " ORDER BY created,id", params).fetchall()]
        return [_run_record(db, run_id) for run_id in ids]


def list_requests(path, states=UNRESOLVED_STATES, scope=None, run_id=None):
    """Read-only listing of requests in ``states`` (default: unresolved holds)."""
    path = Path(path)
    if not path.exists():
        return []
    marks = ",".join("?" * len(states))
    with closing(_read_only(path)) as db:
        columns = _columns(db, "calls")
        extra = ",".join(name if name in columns else "NULL" for name in ("run_id", "owner", "basis"))
        query = f"SELECT id,scope,agent,reserved,actual,state,created,{extra} FROM calls WHERE state IN ({marks})"
        params = list(states)
        if scope is not None:
            query += " AND scope=?"
            params.append(scope)
        if run_id is not None:
            if "run_id" not in columns:
                return []
            query += " AND run_id=?"
            params.append(run_id)
        rows = db.execute(query + " ORDER BY created,id", params).fetchall()
    return [{"request_id": r[0], "scope": r[1], "agent": r[2], "reserved_usd": usd(r[3]),
             "actual_usd": None if r[4] is None else usd(r[4]), "state": r[5], "created": r[6], "run_id": r[7],
             "owner": r[8], "basis": r[9]} for r in rows]


def cap_changes(path):
    """Read-only legacy log of cap changes from older ledgers ([] when absent)."""
    path = Path(path)
    if not path.exists():
        return []
    with closing(_read_only(path)) as db:
        if "cap_changes" not in _tables(db):
            return []
        rows = db.execute("SELECT id,scope,previous_cap,new_cap,note,created FROM cap_changes ORDER BY id").fetchall()
    return [{"id": r[0], "scope": r[1], "previous_cap_usd": usd(r[2]), "new_cap_usd": usd(r[3]), "note": r[4],
             "created": r[5]} for r in rows]


def current_owner():
    """``host:pid`` of this process (recorded on every reservation and run)."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # exists (e.g. owned by another user) or cannot be checked: alive
        return True
    return True


def owner_gone(owner, created=None, *, host=None, now=None):
    """True only when the process that wrote a row provably no longer exists: same host and no
    such pid, or another host (the ledger file is local, so a process elsewhere cannot be writing
    it), or (an ownerless row from before owner tracking) created at least ORPHAN_MIN_AGE_HOURS
    ago. Anything uncertain counts as alive."""
    if not owner:
        if not created:
            return False
        try:
            moment = datetime.datetime.fromisoformat(str(created).replace(" ", "T"))
        except ValueError:
            return False
        moment = moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)
        now = now or datetime.datetime.now(datetime.timezone.utc)
        return (now - moment).total_seconds() >= ORPHAN_MIN_AGE_HOURS * 3600
    name, _, pid = str(owner).rpartition(":")
    if name != (host or socket.gethostname()):
        return True
    try:
        return not _pid_alive(int(pid))
    except ValueError:
        return False


def _audit(db, request_id, action, previous_state, previous_actual, new_state, new_actual, basis, note):
    db.execute("INSERT INTO reconciliations (request_id,action,previous_state,previous_actual,new_state,new_actual,"
               "basis,note) VALUES (?,?,?,?,?,?,?,?)",
               (request_id, action, previous_state, previous_actual, new_state, new_actual, basis, str(note)))


def _record_violation(db, request_id, run_id, requested, reserved, actual):
    """After a request billed above its reservation: raise its run's reservation factor to the
    observed ``actual / requested`` ratio (rounded up to 0.01) so later reservations of the run
    are upper bounds again. Recorded with an audit row; never blocks anything."""
    if not run_id:
        return
    row = db.execute("SELECT reservation_factor FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return
    previous = _factor(row[0])
    needed = (Decimal(actual) / Decimal(max(requested or reserved, 1))).quantize(Decimal("0.01"),
                                                                                  rounding=ROUND_CEILING)
    if needed > previous:
        db.execute("UPDATE runs SET reservation_factor=? WHERE id=?", (str(needed), run_id))
        _audit(db, request_id, "reservation_factor", "estimate_violation", actual, "estimate_violation", actual, None,
               f"run {run_id}: billed {usd(actual)} against reserved {usd(reserved)}; reservation factor "
               f"{previous} -> {needed} for the rest of the run")


class BudgetLedger:
    """SQLite BEGIN IMMEDIATE admission shared by all processes and agents.

    ``BudgetLedger(path)`` opens the ledger, creating the file and its tables when absent and
    adding newer columns to an older ledger. ``start_run`` registers the run whose budget every
    ``reserve`` of this object is admitted against.
    """
    def __init__(self, path: str | Path, *, owner=None):
        self.path = str(path)
        self.owner = owner or current_owner()
        self.run_id = None
        self.run_scope = None
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            # One exclusive transaction: concurrent first opens (or a migration of an
            # older ledger) cannot race on CREATE/ALTER.
            db.execute("BEGIN EXCLUSIVE")
            for statement in _SCHEMA:
                db.execute(statement)
            for table, column, kind in _MIGRATIONS:
                if column not in _columns(db, table):
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    # -- runs ------------------------------------------------------------------
    def start_run(self, run_id, scope, budget_usd, *, source=None, note=None):
        """Register run ``run_id`` in ``scope`` with its budget (positive USD) and bind this object
        to it. Registering an existing run again (e.g. the judge's three clients) needs the same
        scope and budget. Also marks every ``reserved`` hold of a process that is provably gone as
        ``uncertain`` (still counted against its own run). Never blocks on other runs. Returns
        {"run": the run's state, "orphaned_holds": [request ids]}."""
        if not str(run_id or "").strip():
            raise ValueError("A run needs a nonempty run id")
        if not str(scope or "").strip():
            raise ValueError("A run needs a scope")
        budget = units(budget_amount(budget_usd))
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT scope,budget FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO runs (id,scope,budget,source,owner,note) VALUES (?,?,?,?,?,?)",
                           (run_id, scope, budget, source, self.owner, note))
            elif (row[0], row[1]) != (scope, budget):
                raise ValueError(f"run {run_id} is already registered with scope {row[0]!r} and budget "
                                 f"${usd(row[1])}; a run's budget is fixed when it starts")
            orphaned = self._orphan_holds(db, f"run {run_id} started")
        self.run_id, self.run_scope = run_id, scope
        return {"run": self.run_snapshot(), "orphaned_holds": orphaned}

    def _orphan_holds(self, db, reason):
        orphaned = []
        rows = db.execute("SELECT id,owner,created,run_id FROM calls WHERE state='reserved' ORDER BY created,id").fetchall()
        for request_id, owner, created, run_id in rows:
            if owner == self.owner or not owner_gone(owner, created):
                continue
            db.execute("UPDATE calls SET state='uncertain',basis=? WHERE id=? AND state='reserved'",
                       (ORPHANED_BASIS, request_id))
            _audit(db, request_id, "orphaned_hold", "reserved", None, "uncertain", None, None,
                   f"{reason}: process {owner or 'unknown'} of run {run_id or 'unknown'} is gone; the hold stays "
                   "counted against its own run until reconciled")
            orphaned.append(request_id)
        return orphaned

    def end_run(self):
        """Record the bound run's end. A hold of this run still ``reserved`` (its settlement never
        arrived) becomes ``uncertain``: counted at its reservation, explicit, reconcilable."""
        if self.run_id is None:
            return None
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE runs SET ended=CURRENT_TIMESTAMP WHERE id=? AND ended IS NULL", (self.run_id,))
            for (request_id,) in db.execute("SELECT id FROM calls WHERE run_id=? AND state='reserved'",
                                            (self.run_id,)).fetchall():
                db.execute("UPDATE calls SET state='uncertain',basis=? WHERE id=?", (ORPHANED_BASIS, request_id))
                _audit(db, request_id, "orphaned_hold", "reserved", None, "uncertain", None, None,
                       f"run {self.run_id} ended without a settlement; the hold stays counted until reconciled")
        return self.run_snapshot()

    def run_snapshot(self, run_id=None):
        with closing(self.connect()) as db:
            return _run_record(db, run_id or self.run_id)

    # -- requests ----------------------------------------------------------------
    def reserve(self, scope, agent, cost):
        """The one spending control: admit a request only if the run's committed spend plus this
        conservative reservation (scaled by the run's ``reservation_factor``) fits the run's
        budget; else ``BudgetExceeded``."""
        if self.run_id is None:
            raise ValueError("No run budget: call start_run(run_id, scope, budget_usd) before reserving")
        if scope != self.run_scope:
            raise ValueError(f"Run {self.run_id} reserves in scope {self.run_scope!r}, not {scope!r}")
        requested = units(cost)
        if requested <= 0:
            raise ValueError("Paid requests require positive conservative reservation")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT budget,reservation_factor FROM runs WHERE id=?", (self.run_id,)).fetchone()
            if row is None:
                raise ValueError(f"Run {self.run_id} is not registered in {self.path}")
            budget, factor = row[0], _factor(row[1])
            amount = requested
            if factor > 1:
                amount = int((Decimal(requested) * factor).to_integral_value(rounding=ROUND_CEILING))
            used = db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM calls WHERE run_id=?",
                              (self.run_id,)).fetchone()[0]
            if used + amount > budget:
                raise BudgetExceeded(f"run {self.run_id} budget ${usd(budget)} cannot cover the request "
                                     f"(committed ${usd(used)}, reservation ${usd(amount)})")
            request_id = str(uuid.uuid4())
            db.execute("INSERT INTO calls (id,scope,agent,reserved,state,run_id,requested,owner) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (request_id, scope, agent, amount, "reserved", self.run_id, requested, self.owner))
        return request_id

    def settle(self, request_id, cost=None, basis="provider_reported_cost"):
        """Settle a request still ``reserved``. ``cost=None`` means uncertain billing
        and retains the entire reservation; only ``reconcile()`` can resolve it later.

        ``basis="unbilled_error"`` requires ``cost=0`` (provider rejected the request
        before generation). A cost above the reservation is recorded as an
        ``estimate_violation`` and raises the run's reservation factor (never blocks).
        """
        if cost is not None or basis == "unbilled_error":
            _check_basis(cost, basis)
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reserved,state,basis,owner,run_id,requested FROM calls WHERE id=?",
                             (request_id,)).fetchone()
            orphaned = (row is not None and row[1] == "uncertain" and row[2] == ORPHANED_BASIS
                        and row[3] == self.owner)
            if row is None or (row[1] != "reserved" and not orphaned):
                raise ValueError("Unknown or already settled request (uncertain requests need reconcile())")
            reserved, _, _, _, run_id, requested = row
            actual = units(cost) if cost is not None else None
            if orphaned:
                # Marked as left over although its own process was still alive: it settles after
                # all, so the billed amount is never lost.
                if actual is None:
                    return
                state = "estimate_violation" if actual > reserved else "settled"
                db.execute("UPDATE calls SET actual=?,state=?,basis=?,settled=CURRENT_TIMESTAMP WHERE id=?",
                           (actual, state, basis, request_id))
                _audit(db, request_id, "late_settlement", "uncertain", None, state, actual, basis,
                       "settled by its own process after it was marked as a left-over hold")
            else:
                state = "uncertain" if actual is None else ("estimate_violation" if actual > reserved else "settled")
                db.execute("UPDATE calls SET actual=?,state=?,basis=?,settled=CURRENT_TIMESTAMP WHERE id=?",
                           (actual, state, None if actual is None else basis, request_id))
            if state == "estimate_violation":
                _record_violation(db, request_id, run_id, requested, reserved, actual)

    def reconcile(self, request_id, cost, basis="provider_reported_cost", note="", *, allow_reserved=False):
        """Resolve an uncertain request once billing is known, with an audit row.

        ``cost`` is required (normally OpenRouter's reported generation cost, see
        scripts/reconcile_ledger.py; ``basis=MANUAL_BASIS`` for an amount entered by hand).
        A ``reserved`` hold is accepted only with ``allow_reserved=True``, after its
        process is known to have exited without settling. A cost above the reservation
        becomes an estimate_violation (recorded, never blocking).
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
            row = db.execute("SELECT reserved,state,actual,run_id,requested FROM calls WHERE id=?",
                             (request_id,)).fetchone()
            if row is None or row[1] not in allowed:
                raise ValueError(f"Request is not reconcilable (state {None if row is None else row[1]})")
            state = "estimate_violation" if actual > row[0] else "settled"
            db.execute("UPDATE calls SET actual=?,state=?,basis=?,settled=CURRENT_TIMESTAMP WHERE id=?",
                       (actual, state, basis, request_id))
            _audit(db, request_id, "reconcile", row[1], row[2], state, actual, basis, note)
            if state == "estimate_violation":
                _record_violation(db, request_id, row[3], row[4], row[0], actual)
        return {"request_id": request_id, "previous_state": row[1], "state": state,
                "reserved_usd": usd(row[0]), "actual_usd": usd(actual), "basis": basis}

    def audit_log(self):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT id,request_id,action,previous_state,previous_actual,new_state,new_actual,"
                              "basis,note,created FROM reconciliations ORDER BY id").fetchall()
        return [{"id": r[0], "request_id": r[1], "action": r[2], "previous_state": r[3],
                 "previous_actual_usd": None if r[4] is None else usd(r[4]), "new_state": r[5],
                 "new_actual_usd": None if r[6] is None else usd(r[6]), "basis": r[7], "note": r[8],
                 "created": r[9]} for r in rows]

    def can_cover(self, cost):
        """Advisory check (no reservation): can the bound run still admit a request reserving ``cost``?"""
        run = self.run_snapshot()
        if run is None:
            raise ValueError("No run budget: call start_run first")
        needed = Decimal(str(cost)) * Decimal(run["reservation_factor"])
        return Decimal(run["remaining_usd"]) >= needed

    def snapshot(self):
        with closing(self.connect()) as db:
            return _snapshot(db)
