import json
from pathlib import Path

import pytest

from alloc import audit, ops, reports, workflow
from alloc.allocate import load_run, rebalance_proportional, run_allocation
from alloc.db import connect
from alloc.intake import load_directory

DATA = Path(__file__).resolve().parents[1] / "data"
ESC = json.loads((DATA / "escalation.json").read_text())


@pytest.fixture
def con():
    c = connect()
    load_directory(c, DATA)
    return c


def status(con, exc_id):
    return con.execute("SELECT status FROM exceptions WHERE exc_id=?", (exc_id,)).fetchone()[0]


# ---------------------------------------------------------------- data correction (D007)
def test_rating_correction_resolves_dq_unblocks_deal_and_is_audited(con):
    blocked = run_allocation(con, "D007", mode="live", escalation=ESC)
    assert blocked.status == "blocked"
    resolved = reports.correct_deal_rating(con, "D007", "B", "data_ops", "agency report (illustrative)")
    assert len(resolved) == 2                                   # intake exception + blocked-run exception
    assert all(status(con, e) == "Resolved" for e in resolved)
    assert load_run(con, blocked.run_id).status == "superseded"
    entry = con.execute("SELECT payload FROM audit_log WHERE action='DATA_CORRECTED'").fetchone()
    assert json.loads(entry[0])["before"] is None and json.loads(entry[0])["after"] == "B"
    r = run_allocation(con, "D007", mode="live", escalation=ESC)
    assert r.status == "proposed" and sum(l["final_amt"] for l in r.lines) == 110
    assert next(l for l in r.lines if l["fund_id"] == "F5")["ineligible_reason"] == \
        "INSTRUMENT_NOT_PERMITTED;BELOW_RATING_FLOOR"                # unitranche and B, both gates fail for F5


def test_rating_correction_needs_reason_and_valid_rating(con):
    with pytest.raises(ValueError, match="reason"):
        reports.correct_deal_rating(con, "D007", "B", "data_ops", " ")
    with pytest.raises(ValueError, match="rating scale"):
        reports.correct_deal_rating(con, "D007", "Z", "data_ops", "x")


def test_correction_does_not_touch_other_deals_dq_exceptions(con):
    con.execute("INSERT INTO exceptions (exc_id, severity, status, rule_id, opened_at, detail) "
                "VALUES ('E9999','High','Open','DQ-MISSING_RATING','2026-10-19','D999: rating missing')")
    reports.correct_deal_rating(con, "D007", "B", "data_ops", "agency report (illustrative)")
    assert status(con, "E9999") == "Open"


# ---------------------------------------------------------------- withdraw hypo / resolve break
def test_withdrawn_hypo_supersedes_its_exceptions_and_live_runs_cannot_be_withdrawn(con):
    tw = run_allocation(con, "D003", key="target_weight", escalation=ESC)
    assert len(tw.exceptions) == 2
    with pytest.raises(ValueError, match="needs a reason"):
        workflow.withdraw(con, tw.run_id, "maker1", "")
    sup = workflow.withdraw(con, tw.run_id, "maker1", "capacity key chosen")
    assert all(status(con, e) == "Superseded" for e in sup)
    assert load_run(con, tw.run_id).status == "withdrawn"
    live = run_allocation(con, "D010", mode="live", escalation=ESC)
    with pytest.raises(ValueError, match="only hypos"):
        workflow.withdraw(con, live.run_id, "maker1", "x")


def test_break_resolution_posts_adjustment_and_reconciles_clean(con):
    r = run_allocation(con, "D010", mode="live", escalation=ESC)
    workflow.submit(con, r.run_id, "maker1")
    workflow.approve(con, r.run_id, "checker1")
    ops.book(con, r.run_id, "ops1", {"F1": -0.5})
    brk = ops.reconcile(con, r.run_id, escalation=ESC)["breaks"]
    assert len(brk) == 1
    with pytest.raises(ValueError, match="explanation"):
        ops.resolve_break(con, brk[0]["break_id"], "ops1", "")
    res = ops.resolve_break(con, brk[0]["break_id"], "ops1", "posting error")
    assert res["adjustment"] == 0.5 and status(con, brk[0]["exc_id"]) == "Resolved"
    assert ops.reconcile(con, r.run_id, escalation=ESC)["breaks"] == []
    with pytest.raises(ValueError, match="already Resolved"):
        ops.resolve_break(con, brk[0]["break_id"], "ops1", "again")


# ---------------------------------------------------------------- reports
@pytest.fixture
def demo(con):
    return con, reports.demo(con, ESC)


def test_evidence_pack_has_every_section_and_ties_out(demo):
    con, out = demo
    text = reports.evidence_pack(con, out["case1"]["live_run"])
    for section in ("## 1. Deal", "## 2. Run and lineage", "## 3. Eligibility", "## 4. Exceptions",
                    "## 5. Approvals", "## 6. Booking", "## 7. Audit trail"):
        assert section in text
    assert "Lineage: R0001 → R0003 → R0004" in text
    assert "Total allocated 400 + unallocated 0 = deal size 400" in text
    assert "Resolved" in text and "intact" in text


def test_evidence_pack_for_corrected_deal_shows_data_correction(demo):
    con, out = demo
    assert "DATA_CORRECTED" in reports.evidence_pack(con, out["case3"]["run"])


def test_exec_summary_status_and_actions(demo):
    con, _ = demo
    text = reports.exec_summary(con)
    assert "**Status: AMBER**" in text                          # B07: one open High – D013 funding not confirmed
    assert "FUNDING-UNKNOWN F1" in text and "surveillance breaches: 0" in text


def test_handover_lists_open_items_and_is_audited(demo):
    con, out = demo
    text = reports.handover(con, "Bengaluru", "New York")
    assert f"{out['case5']['run']} D010 – submitted" in text
    assert "SIZE-UNDERSUBSCRIBED" in text
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='HANDOVER_GENERATED'").fetchone()[0] == 1


def test_demo_end_to_end_numbers(demo):
    con, out = demo
    c1 = out["case1"]
    assert c1["rebalanced"]["F1"] == 14.0 and c1["rebalanced"]["F2"] == 5.5 and c1["rebalanced_exceptions"] == 0
    assert c1["self_approval_blocked"] is True
    assert tuple(c1["recon_breaks"][0][1:]) == ("F3", "BOOKED_vs_LEDGER", -0.5)
    assert c1["breaks_resolved"][0][1] == 0.5
    assert out["case2"]["lines"]["F5"] == (0.0, "RESTRICTED_ISSUER")
    assert out["case3"]["blocked_status"] == "blocked" and out["case3"]["status_before_booking"] == "approved"
    assert out["case4"]["breaches"] == []                                         # B06 preventive control
    f2 = out["case4"]["pretrade"]["F2"]
    assert (f2["funding"], f2["start_ratio"], f2["debt_permitted"], f2["cash_permitted"], f2["allocated"], f2["binding"]) == \
        ("debt", 0.715732, 57.5, 40.0, 57.5, True)
    assert f2["ratio_after"] >= 0.70 and f2["coverage_after"] >= 1.5
    f1 = out["case4"]["pretrade"]["F1"]
    assert (f1["funding"], f1["cash_permitted"], f1["allocated"], f1["binding"]) == ("cash", 250.0, 113.0, False)
    assert out["case4"]["lines"]["F2"] == 57.5 and sum(out["case4"]["lines"].values()) == 300
    c6 = out["case6"]
    assert c6["pretrade"]["F2"]["reserved_by"] == [out["case4"]["run"]] and c6["lines"]["F2"] == 0.0
    assert c6["pretrade"]["F1"]["funding"] == "unknown" and c6["pretrade"]["F1"]["scenario"] == "cash"
    assert ("FUNDING-UNKNOWN", "F1", "High", "Compliance") in [tuple(x) for x in c6["exceptions"]]
    assert out["fairness_signoff"] == "FS001"
    assert out["case5"]["status"] == "submitted"
    assert out["metrics"]["KRI"]["open_recon_breaks"] == 0
    assert audit.verify(con)[0] is True


def test_attestation_all_effective_after_demo_and_flags_a_violation(demo):
    con, _ = demo
    text = reports.attestation(con, "2026-10", "cco1")
    assert "all controls effective" in text and text.count("| Effective |") == 13
    # simulate an unreconciled booking -> attestation must show an exception, not hide it
    con.execute("UPDATE allocation_runs SET status='booked' WHERE run_id='R0005'")
    assert "Every booked run reconciled | 4 | 1 | **Exception**" in reports.attestation(con, "2026-10", "cco1")


# ---------------------------------------------------------------- fairness (B06)
def test_fairness_method_reconciles_and_explains_every_drift(demo):
    con, _ = demo
    fr = reports.fairness(con)
    assert fr["total"]["runs"] == 4
    unalloc = con.execute("SELECT COALESCE(SUM(unallocated),0) FROM allocation_runs WHERE mode='live' "
                          "AND status IN ('approved','booked','reconciled')").fetchone()[0]
    assert abs(fr["total"]["drift"] + unalloc) < 1e-6                            # drift = - unallocated
    for d in fr["deals"]:
        if abs(d["drift"]) > 1e-9:
            assert d["reasons"] and "UNEXPLAINED" not in d["reasons"]             # every drift carries a reason code
    f2 = next(d for d in fr["deals"] if d["deal_id"] == "D008" and d["fund_id"] == "F2")
    assert "CAPACITY_SET_BY_QUALIFYING_ASSETS_HEADROOM" in f2["reasons"]
    assert f2["actual"] == 57.5 and f2["fair"] > 57.5


def test_fairness_fair_share_is_the_key_split_across_eligible_funds(demo):
    con, out = demo
    fr = reports.fairness(con)
    d009 = {d["fund_id"]: d for d in fr["deals"] if d["deal_id"] == "D009"}
    assert d009["F5"]["fair"] == 0 and d009["F5"]["reasons"].startswith("EXCLUDED: RESTRICTED_ISSUER")
    assert abs(sum(d["fair"] for d in d009.values()) - 220) < 1e-4               # fair shares add up to the deal size
    f5 = next(f for f in fr["funds"] if f["fund_id"] == "F5")
    assert f5["exclusions"]["RESTRICTED_ISSUER"] == 1


def test_fairness_flags_drift_above_tolerance(demo):
    con, _ = demo
    flags = {f["fund_id"]: f["flag"] for f in reports.fairness(con, tolerance=0.02)["funds"]}
    assert flags["F2"] == "REVIEW" and flags["F4"] == "OK"                        # F2 -4.49% vs 2% threshold
    assert "## Method" in reports.fairness_report(con)


# ---------------------------------------------------------------- B07: sign-off and decision pack
def test_fairness_signoff_requires_segregation_and_commentary(demo):
    con, _ = demo
    with pytest.raises(ValueError, match="segregation of duties"):
        reports.fairness_signoff(con, "maker1", 0.02)
    with pytest.raises(ValueError, match="commentary required"):
        reports.fairness_signoff(con, "cco2", 0.02)                                 # F2 over 2% tolerance
    res = reports.fairness_signoff(con, "cco2", 0.02, {"F2": "Board-step rebalance on D003 and 70% cap on D008; both documented"})
    assert res["flagged"] == ["F2"] and len(res["report_hash"]) == 64
    text = reports.fairness_report(con, 0.02)
    assert res["signoff_id"] in text and "Policy versions in the set" in text
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='FAIRNESS_SIGNED_OFF'").fetchone()[0] == 2


def test_decision_pack_lists_owner_deadline_and_evidence(demo):
    con, out = demo
    text = reports.decision_pack(con)
    assert "FUNDING-UNKNOWN F1" in text and "Compliance" in text
    assert f"{out['case6']['run']} D013: approve or reject" in text and "Checker (not maker1)" in text
    assert f"evidence --run {out['case6']['run']}" in text
    assert "Fairness report: review and sign off" not in text                        # signed off in the demo


# ---------------------------------------------------------------- B07.1 sign-off freshness
def test_signoff_is_bound_to_run_set_tolerance_policy_versions_and_report_hash(demo):
    con, _ = demo
    assert reports.signoff_status(con)["fresh"]                                     # FS001 at policy tolerance 5%
    st = reports.signoff_status(con, 0.02)
    assert not st["fresh"] and st["reasons"][0].startswith("tolerance changed")
    assert "Fairness report: new sign-off needed" in reports.decision_pack(con, 0.02)
    # same run IDs, different figures (e.g. a correction to a final amount): the hash catches it
    con.execute("UPDATE allocation_lines SET final_amt=final_amt+0.5 WHERE run_id='R0004' AND fund_id='F3'")
    st = reports.signoff_status(con)
    assert not st["fresh"] and any(r.startswith("report content changed") for r in st["reasons"])
    assert not any(r.startswith("run set") for r in st["reasons"])
    assert "Fairness sign-off valid for the current report" in reports.attestation(con, "2026-10", "cco1") and \
        "**Exception**" in reports.attestation(con, "2026-10", "cco1")
    con.execute("UPDATE allocation_lines SET final_amt=final_amt-0.5 WHERE run_id='R0004' AND fund_id='F3'")
    assert reports.signoff_status(con)["fresh"]                                    # back to what was signed
    con.execute("UPDATE allocation_runs SET policy_version=policy_version||';X@v9' WHERE run_id='R0004'")
    assert "policy versions changed" in reports.signoff_status(con)["reasons"]
    reports.fairness_signoff(con, "cco2", None, {"F2": "D003 rebalance and D008 regulatory cap (illustrative)"})
    assert reports.signoff_status(con)["fresh"]
