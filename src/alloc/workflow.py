"""Maker-checker workflow for LIVE allocation runs.

  proposed / pending_escalation / partially_allocated --submit(maker)--> submitted
  submitted --approve(checker != maker)--> approved      (open High/Critical exceptions need a rationale,
                                                           which is recorded as their resolution)
  submitted --reject(checker != maker, rationale)--> rejected      (open exceptions on the run are closed)
  any live, unbooked run --release(reason)--> released              (B07.1: frees reserved capital and headroom)
  any live, unbooked run --renew(reason)--> same status, new review-by date

B07.1 controls:
  * FUNDING-UNKNOWN is never cleared by an approval rationale. Approval is refused while it is open; it closes only
    when funding is confirmed (set-funding) or the run is rejected or released.
  * A submitted run has an approval due time (CTRL-REVIEW approval_sla_hours).
  * What a live, unbooked run reserves is never freed silently: it is renewed or released by a named person.
Every transition is written to the hash-chained audit log.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import audit

SUBMITTABLE = {"proposed", "pending_escalation", "partially_allocated"}
HOLDING = ("proposed", "partially_allocated", "pending_escalation", "submitted", "approved")   # live runs that reserve
HARD_STOPS = ("FUNDING-UNKNOWN",)        # exceptions an approval rationale cannot resolve


def _ctrl(con) -> dict:
    from .allocate import _rules
    return _rules(con).get("CTRL-REVIEW", {}).get("params", {})


def _plus_hours(iso: str, hours: float) -> str:
    return (datetime.fromisoformat(iso) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _close_open_exceptions(con, run_id: str, status: str, resolution: str) -> list[str]:
    exc = [e[0] for e in con.execute("SELECT exc_id FROM exceptions WHERE run_id=? AND status='Open'", (run_id,))]
    for e in exc:
        con.execute("UPDATE exceptions SET status=?, resolved_at=?, resolution=? WHERE exc_id=?",
                    (status, _now(), resolution, e))
    return exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run(con, run_id):
    r = con.execute("SELECT mode, status, created_by FROM allocation_runs WHERE run_id=?", (run_id,)).fetchone()
    if r is None:
        raise ValueError(f"unknown run {run_id}")
    return r


def _set_status(con, run_id, status):
    con.execute("UPDATE allocation_runs SET status=? WHERE run_id=?", (status, run_id))


def open_exceptions(con, run_id, severities=("High", "Critical")):
    q = f"SELECT exc_id, rule_id, severity FROM exceptions WHERE run_id=? AND status='Open' AND severity IN ({','.join('?'*len(severities))})"
    return con.execute(q, (run_id, *severities)).fetchall()


def submit(con: sqlite3.Connection, run_id: str, maker: str) -> None:
    mode, status, created_by = _run(con, run_id)
    if mode != "live":
        raise ValueError("hypo runs are for evaluation only – promote to live before submitting")
    if status not in SUBMITTABLE:
        raise ValueError(f"run {run_id} has status '{status}' and cannot be submitted")
    if maker != created_by:
        raise ValueError(f"only the maker who created {run_id} ({created_by}) can submit it")
    ts = _now()
    sla = _ctrl(con).get("approval_sla_hours")
    due = _plus_hours(ts, sla) if sla else None
    con.execute("UPDATE allocation_runs SET status='submitted', submitted_at=?, approval_due_at=? WHERE run_id=?",
                (ts, due, run_id))
    audit.append(con, maker, "SUBMITTED", run_id, {"previous_status": status, "approval_due_at": due,
                                                    "open_high_critical": [e[0] for e in open_exceptions(con, run_id)]})
    con.commit()


def approve(con: sqlite3.Connection, run_id: str, checker: str, rationale: str | None = None) -> list[str]:
    mode, status, maker = _run(con, run_id)
    if status != "submitted":
        raise ValueError(f"run {run_id} must be 'submitted' to approve (is '{status}')")
    if checker == maker:
        raise ValueError("maker-checker: the checker cannot be the maker")
    hard = [e[0] for e in open_exceptions(con, run_id) if e[1] in HARD_STOPS]
    if hard:
        raise ValueError(f"hard stop {hard}: funding is not confirmed – an approval rationale cannot clear this. "
                         "Confirm funding (set-funding) or reject the run")
    blocking = open_exceptions(con, run_id)
    if blocking and not (rationale and rationale.strip()):
        raise ValueError(f"open High/Critical exceptions {[e[0] for e in blocking]} – approval needs a rationale "
                         "(e.g. board step obtained, reference)")
    ts = _now()
    for exc_id, _, _ in blocking:
        con.execute("UPDATE exceptions SET status='Resolved', resolved_at=?, resolution=? WHERE exc_id=?",
                    (ts, f"approved by {checker}: {rationale}", exc_id))
    con.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?)", (run_id, maker, checker, "approve", rationale, ts))
    _set_status(con, run_id, "approved")
    audit.append(con, checker, "APPROVED", run_id, {"maker": maker, "rationale": rationale,
                                                    "resolved_exceptions": [e[0] for e in blocking]})
    con.commit()
    return [e[0] for e in blocking]


def reject(con: sqlite3.Connection, run_id: str, checker: str, rationale: str) -> None:
    mode, status, maker = _run(con, run_id)
    if status != "submitted":
        raise ValueError(f"run {run_id} must be 'submitted' to reject (is '{status}')")
    if checker == maker:
        raise ValueError("maker-checker: the checker cannot be the maker")
    if not (rationale and rationale.strip()):
        raise ValueError("a rejection needs a rationale")
    con.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?)", (run_id, maker, checker, "reject", rationale, _now()))
    _set_status(con, run_id, "rejected")
    closed = _close_open_exceptions(con, run_id, "Closed", f"run rejected by {checker}: {rationale}")
    audit.append(con, checker, "REJECTED", run_id, {"maker": maker, "rationale": rationale, "closed_exceptions": closed,
                                                    "released": "capital, issuer exposure and BDC headroom"})
    con.commit()


def withdraw(con: sqlite3.Connection, run_id: str, by: str, reason: str) -> list[str]:
    """Close a hypo that will not be pursued; its open exceptions become Superseded with the reason."""
    if not (reason and reason.strip()):
        raise ValueError("withdrawing a hypo needs a reason")
    mode, status, _maker = _run(con, run_id)
    if mode != "hypo":
        raise ValueError(f"only hypos can be withdrawn ({run_id} is {mode}); live runs are rejected by a checker")
    if status in ("superseded", "withdrawn"):
        raise ValueError(f"run {run_id} is already {status}")
    exc = [e[0] for e in con.execute("SELECT exc_id FROM exceptions WHERE run_id=? AND status='Open'", (run_id,))]
    for e in exc:
        con.execute("UPDATE exceptions SET status='Superseded', resolved_at=?, resolution=? WHERE exc_id=?",
                    (_now(), f"hypo withdrawn by {by}: {reason}", e))
    _set_status(con, run_id, "withdrawn")
    audit.append(con, by, "HYPO_WITHDRAWN", run_id, {"previous_status": status, "reason": reason,
                                                      "superseded_exceptions": exc})
    con.commit()
    return exc


# ---------------------------------------------------------------- B07.1 reservation aging
def _holding_run(con, run_id):
    r = con.execute("SELECT mode, status, created_by, review_by FROM allocation_runs WHERE run_id=?", (run_id,)).fetchone()
    if r is None:
        raise ValueError(f"unknown run {run_id}")
    if r[0] != "live" or r[1] not in HOLDING:
        raise ValueError(f"run {run_id} ({r[0]}, {r[1]}) holds no reservation")
    return r


def _decision_id(con) -> str:
    return f"RD{con.execute('SELECT COUNT(*) FROM reservation_decisions').fetchone()[0] + 1:03d}"


def renew_reservation(con: sqlite3.Connection, run_id: str, by: str, reason: str, hours: float | None = None) -> str:
    """Keep holding the capital and headroom; sets a new review-by date (at most the policy period ahead)."""
    if not (reason and reason.strip()):
        raise ValueError("a renewal needs a reason (why the deal is still live, expected close)")
    _, status, maker, prev = _holding_run(con, run_id)
    limit = _ctrl(con).get("reservation_review_hours", 72)
    hours = limit if hours is None else hours
    if not 0 < hours <= limit:
        raise ValueError(f"renewal period must be between 0 and {limit} hours (CTRL-REVIEW reservation_review_hours)")
    ts = _now()
    new = _plus_hours(ts, hours)
    con.execute("UPDATE allocation_runs SET review_by=? WHERE run_id=?", (new, run_id))
    did = _decision_id(con)
    con.execute("INSERT INTO reservation_decisions VALUES (?,?,?,?,?,?,?,?)", (did, run_id, "renew", by, reason, ts, prev, new))
    audit.append(con, by, "RESERVATION_RENEWED", run_id, {"decision_id": did, "status": status, "owner": maker,
                                                           "previous_review_by": prev, "new_review_by": new, "reason": reason})
    con.commit()
    return new


def release_reservation(con: sqlite3.Connection, run_id: str, by: str, reason: str) -> list[str]:
    """Stop holding: the run is released (it will not be booked), its capital and headroom become free,
    and its open exceptions are closed with the reason. Approved runs need someone other than the maker."""
    if not (reason and reason.strip()):
        raise ValueError("a release needs a reason (deal lost, terms changed, superseded)")
    _, status, maker, prev = _holding_run(con, run_id)
    if status == "approved" and by == maker:
        raise ValueError("an approved run's reservation is released by someone other than its maker")
    ts = _now()
    closed = _close_open_exceptions(con, run_id, "Closed", f"reservation released by {by}: {reason}")
    _set_status(con, run_id, "released")
    did = _decision_id(con)
    con.execute("INSERT INTO reservation_decisions VALUES (?,?,?,?,?,?,?,?)", (did, run_id, "release", by, reason, ts, prev, None))
    audit.append(con, by, "RESERVATION_RELEASED", run_id, {"decision_id": did, "previous_status": status, "owner": maker,
                                                            "reason": reason, "closed_exceptions": closed})
    con.commit()
    return closed


def reservation_aging(con: sqlite3.Connection, as_of: str | None = None, statuses=HOLDING) -> list[dict]:
    """Every live, unbooked run holding capital or headroom: owner, amount, age, review-by, state."""
    now = as_of or _now()
    warn = _ctrl(con).get("reservation_warn_hours", 24)
    out = []
    for run_id, deal_id, status, maker, created, review_by, amt in con.execute(
            f"SELECT r.run_id, r.deal_id, r.status, r.created_by, r.created_at, r.review_by, "
            f"COALESCE(SUM(l.final_amt),0) FROM allocation_runs r LEFT JOIN allocation_lines l ON l.run_id=r.run_id "
            f"WHERE r.mode='live' AND r.status IN ({','.join('?' * len(statuses))}) GROUP BY r.run_id ORDER BY r.run_id",
            statuses):
        age_h = (datetime.fromisoformat(now) - datetime.fromisoformat(created)).total_seconds() / 3600
        if review_by is None:
            state = "NO REVIEW DATE"
        elif review_by < now:
            state = "OVERDUE"
        elif review_by < _plus_hours(now, warn):
            state = "DUE SOON"
        else:
            state = "on time"
        out.append({"run_id": run_id, "deal_id": deal_id, "status": status, "owner": maker, "amount": amt,
                    "created_at": created, "age_hours": round(age_h, 1), "review_by": review_by, "state": state})
    return out
