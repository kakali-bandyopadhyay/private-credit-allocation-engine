import json
import sqlite3
from pathlib import Path

import pytest

from alloc import audit, workflow
from alloc.allocate import load_run, promote, rebalance_proportional, run_allocation
from alloc.db import connect
from alloc.intake import load_directory

DATA = Path(__file__).resolve().parents[1] / "data"
ESC = json.loads((DATA / "escalation.json").read_text())


@pytest.fixture
def con():
    c = connect()
    load_directory(c, DATA)
    return c


def finals(res):
    return {ln["fund_id"]: ln["final_amt"] for ln in res.lines}


# ---------------------------------------------------------------- audit log
def test_audit_chain_verifies_after_activity(con):
    r = run_allocation(con, "D010", mode="live", escalation=ESC)
    workflow.submit(con, r.run_id, "maker1")
    workflow.approve(con, r.run_id, "checker1")
    ok, bad, n = audit.verify(con)
    assert ok and bad is None and n == 4          # DATA_LOADED, RUN_CREATED, SUBMITTED, APPROVED


def test_audit_log_refuses_update_and_delete(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE audit_log SET actor='intruder' WHERE seq=1")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM audit_log WHERE seq=1")


def test_tampering_is_detected_even_if_triggers_are_bypassed(con):
    run_allocation(con, "D010", escalation=ESC)
    con.execute("DROP TRIGGER audit_log_no_update")                 # simulate a privileged bypass
    con.execute("UPDATE audit_log SET payload=replace(payload, '\"F3\"', '\"FX\"') WHERE seq=2")
    ok, bad, _ = audit.verify(con)
    assert not ok and bad == 2


# ---------------------------------------------------------------- maker-checker
def test_checker_cannot_be_maker(con):
    r = run_allocation(con, "D010", mode="live", escalation=ESC)
    workflow.submit(con, r.run_id, "maker1")
    with pytest.raises(ValueError, match="checker cannot be the maker"):
        workflow.approve(con, r.run_id, "maker1")


def test_hypo_cannot_be_submitted(con):
    r = run_allocation(con, "D010", mode="hypo", escalation=ESC)
    with pytest.raises(ValueError, match="promote"):
        workflow.submit(con, r.run_id, "maker1")


def test_only_the_maker_can_submit(con):
    r = run_allocation(con, "D010", mode="live", created_by="maker1", escalation=ESC)
    with pytest.raises(ValueError, match="only the maker"):
        workflow.submit(con, r.run_id, "maker2")


def test_approval_with_open_critical_needs_rationale_and_resolves_them(con):
    r = run_allocation(con, "D001", mode="live", escalation=ESC)   # existing-investment condition fails
    workflow.submit(con, r.run_id, "maker1")
    with pytest.raises(ValueError, match="needs a rationale"):
        workflow.approve(con, r.run_id, "checker1")
    resolved = workflow.approve(con, r.run_id, "checker1", "Board step obtained (illustrative ref)")
    assert len(resolved) == 2
    assert con.execute("SELECT COUNT(*) FROM exceptions WHERE run_id=? AND status='Open'", (r.run_id,)).fetchone()[0] == 0
    assert con.execute("SELECT status FROM allocation_runs WHERE run_id=?", (r.run_id,)).fetchone()[0] == "approved"


def test_reject_needs_rationale(con):
    r = run_allocation(con, "D010", mode="live", escalation=ESC)
    workflow.submit(con, r.run_id, "maker1")
    with pytest.raises(ValueError, match="rationale"):
        workflow.reject(con, r.run_id, "checker1", "")
    workflow.reject(con, r.run_id, "checker1", "Wrong key for this mandate")
    assert con.execute("SELECT status FROM allocation_runs WHERE run_id=?", (r.run_id,)).fetchone()[0] == "rejected"


def test_cannot_approve_twice(con):
    r = run_allocation(con, "D010", mode="live", escalation=ESC)
    workflow.submit(con, r.run_id, "maker1")
    workflow.approve(con, r.run_id, "checker1")
    with pytest.raises(ValueError, match="must be 'submitted'"):
        workflow.approve(con, r.run_id, "checker2")


# ---------------------------------------------------------------- rebalance and promote
@pytest.mark.parametrize("key", ["capacity", "target_weight"])
def test_d003_proportional_rebalance_clears_board_step(con, key):
    hypo = run_allocation(con, "D003", key=key, escalation=ESC)
    assert hypo.status == "pending_escalation"
    rb = rebalance_proportional(con, hypo.run_id, escalation=ESC)
    f = finals(rb)
    assert rb.status == "proposed" and rb.exceptions == [] and rb.source_run_id == hypo.run_id
    assert abs(f["F1"] / (f["F1"] + f["F2"]) - 280 / 390) <= 0.10        # in proportion to 280 : 110
    assert abs(sum(f.values()) + rb.unallocated - 400) < 1e-9             # every dollar still accounted for
    assert f["F1"] <= 20 and f["F2"] <= 15 and f["F2"] >= 5               # capacity and min ticket respected
    # source hypo's open exceptions are superseded, not left dangling
    assert con.execute("SELECT COUNT(*) FROM exceptions WHERE run_id=? AND status='Open'", (hypo.run_id,)).fetchone()[0] == 0
    assert load_run(con, hypo.run_id).status == "superseded"


def test_promote_copies_amounts_into_live_run(con):
    rb = rebalance_proportional(con, run_allocation(con, "D003", escalation=ESC).run_id, escalation=ESC)
    live = promote(con, rb.run_id, "maker1", ESC)
    assert live.mode == "live" and live.source_run_id == rb.run_id and finals(live) == finals(rb)
    workflow.submit(con, live.run_id, "maker1")
    workflow.approve(con, live.run_id, "checker1")
    assert load_run(con, live.run_id).status == "approved"


def test_promote_refuses_live_run(con):
    r = run_allocation(con, "D010", mode="live", escalation=ESC)
    with pytest.raises(ValueError, match="already live"):
        promote(con, r.run_id, "maker1", ESC)


def test_exceptions_carry_sla_due_time(con):
    r = run_allocation(con, "D001", escalation=ESC)
    due = [e["due_at"] for e in r.exceptions]
    assert due and all(d is not None for d in due)
