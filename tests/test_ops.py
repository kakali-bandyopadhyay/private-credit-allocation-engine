import json
from pathlib import Path

import pytest

from alloc import ops, workflow
from alloc.allocate import promote, rebalance_proportional, run_allocation
from alloc.db import connect
from alloc.intake import load_directory

DATA = Path(__file__).resolve().parents[1] / "data"
ESC = json.loads((DATA / "escalation.json").read_text())


@pytest.fixture
def con():
    c = connect()
    load_directory(c, DATA)
    return c


def approved_live(con, deal="D010", rationale=None):
    r = run_allocation(con, deal, mode="live", escalation=ESC)
    workflow.submit(con, r.run_id, "maker1")
    workflow.approve(con, r.run_id, "checker1", rationale)
    return r


def d003_approved(con):
    rb = rebalance_proportional(con, run_allocation(con, "D003", escalation=ESC).run_id, escalation=ESC)
    live = promote(con, rb.run_id, "maker1", ESC)
    workflow.submit(con, live.run_id, "maker1")
    workflow.approve(con, live.run_id, "checker1")
    return live


# ---------------------------------------------------------------- booking
def test_only_approved_live_runs_can_be_booked(con):
    hypo = run_allocation(con, "D010", escalation=ESC)
    with pytest.raises(ValueError, match="only approved live"):
        ops.book(con, hypo.run_id, "ops1")


def test_booking_updates_holdings_capital_and_ledger(con):
    live = d003_approved(con)
    f1_before = con.execute("SELECT available_capital FROM funds WHERE fund_id='F1'").fetchone()[0]
    booked = ops.book(con, live.run_id, "ops1")
    assert abs(sum(booked.values()) - 400) < 1e-9
    assert con.execute("SELECT exposure FROM holdings WHERE fund_id='F1' AND issuer='Atlas Business Services'").fetchone()[0] == 280 + booked["F1"]
    assert con.execute("SELECT available_capital FROM funds WHERE fund_id='F1'").fetchone()[0] == f1_before - booked["F1"]
    assert con.execute("SELECT COUNT(*) FROM ledger_stub WHERE deal_id='D003'").fetchone()[0] == len(booked)


def test_double_booking_of_a_deal_is_refused(con):
    first = approved_live(con)
    ops.book(con, first.run_id, "ops1")
    second = approved_live(con)
    with pytest.raises(ValueError, match="double booking"):
        ops.book(con, second.run_id, "ops1")


# ---------------------------------------------------------------- reconciliation
def test_clean_three_way_reconciliation_has_no_breaks(con):
    live = d003_approved(con)
    ops.book(con, live.run_id, "ops1")
    res = ops.reconcile(con, live.run_id, escalation=ESC)
    assert res["breaks"] == [] and res["funds_checked"] == 6


def test_injected_amount_break_is_detected_and_raised(con):
    live = d003_approved(con)
    ops.book(con, live.run_id, "ops1", {"F3": -0.5})
    res = ops.reconcile(con, live.run_id, escalation=ESC)
    assert [(b["fund_id"], b["check"], b["diff"]) for b in res["breaks"]] == [("F3", "BOOKED_vs_LEDGER", -0.5)]
    row = con.execute("SELECT severity, owner, status FROM exceptions WHERE exc_id=?", (res["breaks"][0]["exc_id"],)).fetchone()
    assert row == ("High", "Operations", "Open")


def test_missing_ledger_posting_is_detected(con):
    live = d003_approved(con)
    ops.book(con, live.run_id, "ops1", {"F6": "missing"})
    res = ops.reconcile(con, live.run_id, escalation=ESC)
    assert [(b["fund_id"], b["check"], b["actual"]) for b in res["breaks"]] == [("F6", "BOOKED_vs_LEDGER", None)]


def test_cannot_reconcile_before_booking(con):
    live = approved_live(con)
    with pytest.raises(ValueError, match="booked before"):
        ops.reconcile(con, live.run_id)


# ---------------------------------------------------------------- surveillance
def test_surveillance_clean_after_d003_booking(con):
    live = d003_approved(con)
    ops.book(con, live.run_id, "ops1")
    res = ops.surveil(con, escalation=ESC)
    assert not any(r["breach"] for r in res)                      # preventive controls held
    atlas_f1 = next(r for r in res if r["fund_id"] == "F1" and r["metric"] == "ISSUER_CONCENTRATION:Atlas Business Services")
    assert atlas_f1["value"] <= 0.05


def test_pretrade_check_caps_bdc_so_qualifying_ratio_stays_at_or_above_70pct(con):
    """B06/B07 preventive control. F2 on D008 is debt-funded (deal plan): T and S grow, Q unchanged."""
    live = approved_live(con, "D008", rationale="Board step obtained (illustrative)")   # Summit: qualifying = false
    f2 = next(c for c in live.pretrade_checks if c["fund_id"] == "F2")
    # F2 starts at 1770 / 2500 = 70.80%; debt case: max = 1770 / 0.70 - 2500 = 28.57 -> 28.5 at the 0.5 lot
    assert f2["funding"]["source"] == "debt" and f2["applied_scenario"] == "debt"
    assert f2["start_ratio"] == 0.708 and round(f2["scenarios"]["debt"]["max"], 2) == 28.57 and f2["permitted"] == 28.5
    # cash case would be tighter: 1770 - 0.70 x 2500 = 20 (and cash on hand 90 does not bind)
    assert f2["scenarios"]["cash"]["permitted"] == 20.0
    assert f2["binding"] and f2["allocated"] == 28.5 and f2["passes"] and f2["ratio_after"] >= 0.70
    line = next(l for l in live.lines if l["fund_id"] == "F2")
    assert "CAPACITY_SET_BY_QUALIFYING_ASSETS_HEADROOM" in line["sizing_notes"]
    assert sum(l["final_amt"] for l in live.lines) + live.unallocated == 300      # excess re-split, every dollar accounted
    ops.book(con, live.run_id, "ops1")
    assert not any(r["breach"] for r in ops.surveil(con, escalation=ESC))


def test_funding_math_cash_debt_mixed():
    from alloc import bdc
    st = {"Q": 1838.0, "T": 2568.0, "S": 1668.0, "C": 90.0}
    debt = bdc.limits(st, 0.0, False, 0.70, 1.5)
    cash = bdc.limits(st, 1.0, False, 0.70, 1.5)
    mixed = bdc.limits(st, 0.5, False, 0.70, 1.5)
    assert round(debt["qualifying_70"], 4) == round(1838 / 0.70 - 2568, 4)            # 57.71
    assert round(debt["asset_coverage_150"], 4) == round((2568 - 1.5 * 1668) / 0.5, 4)  # 132
    assert round(cash["qualifying_70"], 4) == round(1838 - 0.70 * 2568, 4)             # 40.4
    assert cash["cash_on_hand"] == 90 and cash["asset_coverage_150"] == float("inf")
    assert cash["qualifying_70"] < mixed["qualifying_70"] < debt["qualifying_70"]      # cash is the tightest 70% case
    after = bdc.apply_purchase(st, 40.0, 1.0, False)                                    # cash: Q down, T unchanged
    assert after == {"Q": 1798.0, "T": 2568.0, "S": 1668.0, "C": 50.0} and after["Q"] / after["T"] >= 0.70


def test_unknown_funding_uses_conservative_case_and_escalates(con):
    con.execute("UPDATE fund_portfolio SET default_funding=NULL WHERE fund_id='F2'")
    con.execute("DELETE FROM funding_plans WHERE deal_id='D008' AND fund_id='F2'")
    r = run_allocation(con, "D008", mode="live", escalation=ESC)
    f2 = next(c for c in r.pretrade_checks if c["fund_id"] == "F2")
    assert f2["funding"]["source"] == "unknown" and f2["applied_scenario"] == "cash" and f2["permitted"] == 20.0
    assert any(e["rule_id"] == "FUNDING-UNKNOWN" and e["fund_id"] == "F2" and e["severity"] == "High"
               and e["owner"] == "Compliance" for e in r.exceptions)
    workflow.submit(con, r.run_id, "maker1")
    with pytest.raises(ValueError, match="hard stop"):                               # B07.1: a rationale cannot clear it
        workflow.approve(con, r.run_id, "checker1", "Board step obtained; funding to be confirmed (illustrative)")
    exc = con.execute("SELECT status FROM exceptions WHERE run_id=? AND rule_id='FUNDING-UNKNOWN'", (r.run_id,)).fetchone()
    assert exc[0] == "Open"
    from alloc import bdc
    bdc.set_funding(con, "D008", "F2", "debt", None, "treasury1", "facility draw confirmed (illustrative)")
    workflow.approve(con, r.run_id, "checker1", "Board step obtained (illustrative)")
    ops.book(con, r.run_id, "ops1")
    assert not any(x["breach"] for x in ops.surveil(con, escalation=ESC))


def test_pending_runs_reserve_headroom_and_capital(con):
    """Two non-qualifying deals in flight cannot both use F2's headroom; the reservation is released on rejection."""
    d8 = run_allocation(con, "D008", mode="live", escalation=ESC)
    workflow.submit(con, d8.run_id, "maker1")                                      # submitted, not booked: holds headroom
    r13 = run_allocation(con, "D013", mode="hypo", escalation=ESC)
    f2 = next(c for c in r13.pretrade_checks if c["fund_id"] == "F2")
    assert [x["run_id"] for x in f2["reservations"]] == [d8.run_id] and f2["permitted"] == 0.0
    f1_line = next(l for l in r13.lines if l["fund_id"] == "F1")
    f1_d8 = next(l for l in d8.lines if l["fund_id"] == "F1")["final_amt"]
    assert f1_line["capacity"] <= 300 - f1_d8 + 1e-9                                  # F1 capital reserved by D008
    workflow.reject(con, d8.run_id, "checker1", "terms changed (illustrative)")     # reservation released
    r13b = run_allocation(con, "D013", mode="hypo", escalation=ESC)
    f2b = next(c for c in r13b.pretrade_checks if c["fund_id"] == "F2")
    assert f2b["reservations"] == [] and f2b["permitted"] > 0


def test_pre_booking_recheck_blocks_a_stale_allocation(con):
    d8 = approved_live(con, "D008", rationale="Board step obtained (illustrative)")
    con.execute("UPDATE fund_portfolio SET qualifying_assets = qualifying_assets - 30 WHERE fund_id='F2'")  # changed since sizing
    with pytest.raises(ValueError, match="pre-booking re-check failed for F2"):
        ops.book(con, d8.run_id, "ops1")
    assert con.execute("SELECT COUNT(*) FROM bookings WHERE run_id=?", (d8.run_id,)).fetchone()[0] == 0


def test_pretrade_check_runs_for_bdcs_only(con):
    r = run_allocation(con, "D003", escalation=ESC)                                   # qualifying deal: coverage still checked
    assert {c["fund_id"] for c in r.pretrade_checks} == {"F1", "F2"}
    assert all(c["scenarios"]["debt"]["limits"]["qualifying_70"] is None for c in r.pretrade_checks)
    r8 = run_allocation(con, "D008", escalation=ESC)
    assert {c["fund_id"] for c in r8.pretrade_checks} == {"F1", "F2"}                 # BDCs only


def test_surveillance_still_detects_breach_after_portfolio_change(con):
    """Detective control remains: a later change (e.g. a qualifying loan reclassified) is caught post-trade."""
    live = approved_live(con, "D008", rationale="Board step obtained (illustrative)")
    ops.book(con, live.run_id, "ops1")
    con.execute("UPDATE fund_portfolio SET qualifying_assets = qualifying_assets - 20 WHERE fund_id='F2'")  # test-only
    breach = [r for r in ops.surveil(con, escalation=ESC) if r["breach"]]
    assert [(r["fund_id"], r["metric"]) for r in breach] == [("F2", "QUALIFYING_ASSETS_RATIO")]
    assert breach[0]["value"] < 0.70 and "exc_id" in breach[0]


def test_asset_coverage_is_computed_for_bdcs(con):
    res = ops.surveil(con, escalation=ESC)
    cov = {r["fund_id"]: r["value"] for r in res if r["metric"] == "ASSET_COVERAGE"}
    assert set(cov) == {"F1", "F2"} and all(v >= 1.5 for v in cov.values())


# ---------------------------------------------------------------- KPIs / Excel
def test_metrics_report_kpis_and_kris(con):
    live = d003_approved(con)
    ops.book(con, live.run_id, "ops1", {"F3": -0.5})
    ops.reconcile(con, live.run_id, escalation=ESC)
    m = ops.metrics(con)
    assert m["KPI"]["live_runs_approved_or_later"] == 1
    assert m["KRI"]["open_recon_breaks"] == 1 and m["KRI"]["audit_chain_ok"] is True


def test_excel_report_has_all_sheets(con, tmp_path):
    live = d003_approved(con)
    ops.book(con, live.run_id, "ops1")
    ops.reconcile(con, live.run_id, escalation=ESC)
    ops.surveil(con, escalation=ESC)
    out = tmp_path / "report.xlsx"
    sheets = ops.export_excel(con, str(out))
    assert out.exists() and sheets == ["Summary", "Allocations", "Fairness", "Fairness by deal", "Exceptions", "Reconciliation",
                                       "Surveillance", "Approvals", "Audit trail"]


# ---------------------------------------------------------------- B07.1 control fixes
def test_funding_unknown_is_raised_for_every_funded_bdc_line_even_when_not_binding(con):
    """Qualifying deal, cap not binding: booking would still be blocked, so the hard stop must be visible."""
    con.execute("UPDATE fund_portfolio SET default_funding=NULL, cash=1000 WHERE fund_id='F1'")   # ample cash: no cap
    r = run_allocation(con, "D004", mode="live", escalation=ESC)                  # qualifying deal
    f1 = next(l for l in r.lines if l["fund_id"] == "F1")["final_amt"]
    chk = next(c for c in r.pretrade_checks if c["fund_id"] == "F1")
    assert f1 > 0 and not chk["binding"] and chk["deal_qualifying"]               # B07 raised nothing here
    assert [e["fund_id"] for e in r.exceptions if e["rule_id"] == "FUNDING-UNKNOWN"] == ["F1"]


def test_funding_unknown_closes_only_by_confirmation_rejection_or_release(con):
    from alloc import reports
    r = run_allocation(con, "D013", mode="live", escalation=ESC)                  # F1 funding unknown (deal plan)
    workflow.submit(con, r.run_id, "maker1")
    with pytest.raises(ValueError, match="hard stop"):
        workflow.approve(con, r.run_id, "checker1", "approved pending treasury (illustrative)")
    workflow.reject(con, r.run_id, "checker1", "funding not available this week (illustrative)")
    st, res = con.execute("SELECT status, resolution FROM exceptions WHERE run_id=? AND rule_id='FUNDING-UNKNOWN'",
                          (r.run_id,)).fetchone()
    assert st == "Closed" and res.startswith("run rejected by checker1")
    # a hard stop closed any other way is caught by the attestation
    con.execute("UPDATE exceptions SET resolution='approved by checker1: fine' WHERE run_id=? AND rule_id='FUNDING-UNKNOWN'",
                (r.run_id,))
    assert "Funding-unknown hard stops closed only by funding confirmation, rejection or release | 1 | 1 | **Exception**" \
        in reports.attestation(con, "2026-10", "cco1")


def test_submitted_run_gets_an_approval_sla(con):
    from datetime import datetime
    from alloc import reports
    r = run_allocation(con, "D010", mode="live", escalation=ESC)
    workflow.submit(con, r.run_id, "maker1")
    sub, due = con.execute("SELECT submitted_at, approval_due_at FROM allocation_runs WHERE run_id=?", (r.run_id,)).fetchone()
    assert (datetime.fromisoformat(due) - datetime.fromisoformat(sub)).total_seconds() == 8 * 3600
    assert f"approval SLA due {due}" in reports.decision_pack(con)
    con.execute("UPDATE allocation_runs SET approval_due_at='2026-01-01T00:00:00+00:00' WHERE run_id=?", (r.run_id,))
    assert "No submitted run past its approval SLA | 1 | 1 | **Exception**" in reports.attestation(con, "2026-10", "cco1")


def test_reservation_aging_is_explicit_renew_or_release_never_silent(con):
    from datetime import datetime, timedelta, timezone
    from alloc import bdc, reports
    d8 = run_allocation(con, "D008", mode="live", escalation=ESC)
    workflow.submit(con, d8.run_id, "maker1")
    aging = workflow.reservation_aging(con)
    assert [(a["run_id"], a["owner"], a["state"]) for a in aging] == [(d8.run_id, "maker1", "on time")]
    later = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat(timespec="seconds")
    pack = reports.decision_pack(con, as_of=later)
    assert f"{d8.run_id} D008: renew or release reservation" in pack and "**OVERDUE**" in pack
    # still held: past review-by is escalated, not freed
    assert [x["run_id"] for x in bdc.reservations(con, "F2", None, list(workflow.HOLDING))] == [d8.run_id]
    with pytest.raises(ValueError, match="between 0 and 72"):
        workflow.renew_reservation(con, d8.run_id, "maker1", "close slipped a week", hours=200)
    with pytest.raises(ValueError, match="reason"):
        workflow.release_reservation(con, d8.run_id, "maker1", " ")
    new = workflow.renew_reservation(con, d8.run_id, "maker1", "close moved to Friday (illustrative)", hours=48)
    assert con.execute("SELECT review_by FROM allocation_runs WHERE run_id=?", (d8.run_id,)).fetchone()[0] == new
    workflow.release_reservation(con, d8.run_id, "vp1", "deal lost (illustrative)")
    assert con.execute("SELECT status FROM allocation_runs WHERE run_id=?", (d8.run_id,)).fetchone()[0] == "released"
    assert bdc.reservations(con, "F2", None, list(workflow.HOLDING)) == []
    acts = [r[0] for r in con.execute("SELECT action FROM audit_log WHERE entity=? ORDER BY seq", (d8.run_id,))]
    assert acts[-2:] == ["RESERVATION_RENEWED", "RESERVATION_RELEASED"]
    assert [r[0] for r in con.execute("SELECT decision FROM reservation_decisions ORDER BY decision_id")] == ["renew", "release"]


def test_approved_reservation_release_needs_someone_other_than_the_maker(con):
    d10 = approved_live(con, "D010")
    with pytest.raises(ValueError, match="other than its maker"):
        workflow.release_reservation(con, d10.run_id, "maker1", "changed my mind")
