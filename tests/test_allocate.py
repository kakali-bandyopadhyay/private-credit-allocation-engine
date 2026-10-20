import json
from datetime import date
from pathlib import Path

import pytest

from alloc.allocate import (check_existing_investment, check_same_terms, run_allocation,
                            size_allocation)
from alloc.db import connect
from alloc.intake import load_directory

DATA = Path(__file__).resolve().parents[1] / "data"
ESC = json.loads((DATA / "escalation.json").read_text())


@pytest.fixture
def con():
    c = connect()
    load_directory(c, DATA)
    return c


def lines(res):
    return {ln["fund_id"]: ln for ln in res.lines}


def coinv(res):
    return {e["fund_id"]: e["detail"] for e in res.exceptions if e["rule_id"] == "COINV-EXISTING-INVESTMENT"}


# ---------------------------------------------------------------- eligibility gates
def test_d011_ccc_second_lien_only_f3_eligible_and_takes_whole_deal(con):
    L = lines(run_allocation(con, "D011", escalation=ESC))
    assert [f for f, ln in L.items() if ln["eligible"]] == ["F3"]
    assert L["F3"]["final_amt"] == 40
    assert "INSTRUMENT_NOT_PERMITTED" in L["F5"]["ineligible_reason"]


def test_d009_restricted_for_f5_only(con):
    L = lines(run_allocation(con, "D009", escalation=ESC))
    assert L["F5"]["ineligible_reason"] == "RESTRICTED_ISSUER" and L["F5"]["final_amt"] == 0
    assert all(L[f]["eligible"] for f in ("F1", "F2", "F3", "F4", "F6"))


def test_d004_energy_excluded_for_f2_and_f5(con):
    L = lines(run_allocation(con, "D004", escalation=ESC))
    assert "SECTOR_EXCLUDED" in L["F2"]["ineligible_reason"]
    assert "SECTOR_EXCLUDED" in L["F5"]["ineligible_reason"]


def test_d007_missing_rating_blocks_whole_deal(con):
    r = run_allocation(con, "D007", escalation=ESC)
    assert r.status == "blocked"
    assert all(ln["final_amt"] == 0 for ln in r.lines)
    assert [(e["rule_id"], e["owner"]) for e in r.exceptions] == [("DQ-MISSING_RATING", "data_ops")]


# ---------------------------------------------------------------- co-investment conditions
def test_d003_holders_not_in_proportion_need_board_step(con):
    r = run_allocation(con, "D003", escalation=ESC)
    L = lines(r)
    assert L["F1"]["capacity"] == 20 and L["F2"]["capacity"] == 15      # concentration headroom
    assert L["F1"]["final_amt"] <= 20 and L["F2"]["final_amt"] <= 15
    c = coinv(r)
    assert set(c) == {"F1", "F2"} and all("NOT_IN_PROPORTION" in v for v in c.values())
    assert r.status == "pending_escalation"


def test_d001_affiliate_holds_issuer_regulated_fund_does_not(con):
    c = coinv(run_allocation(con, "D001", escalation=ESC))
    assert set(c) == {"F1", "F2"}                        # BDCs only; F3 (holder) and F6 (evergreen) not flagged
    assert all("AFFILIATE_HOLDS_ISSUER_FUND_DOES_NOT" in v for v in c.values())


def test_d010_no_affiliate_holders_no_coinvestment_exception(con):
    r = run_allocation(con, "D010", escalation=ESC)
    assert coinv(r) == {} and r.status == "proposed"


def test_proportional_participation_by_holders_needs_no_board_step():
    vehicle = {"A": "BDC", "B": "BDC", "C": "BDC", "D": "drawdown"}
    holders = {"A": 300, "B": 100}
    assert check_existing_investment({"A": 30, "B": 10, "D": 20}, holders, vehicle, 0.10) == {}
    out = check_existing_investment({"A": 30, "B": 10, "C": 20}, holders, vehicle, 0.10)
    assert out == {"C": "AFFILIATE_HOLDS_ISSUER_FUND_DOES_NOT"}
    out = check_existing_investment({"A": 30, "B": 0, "D": 20}, holders, vehicle, 0.10)
    assert out == {"A": "NOT_IN_PROPORTION_TO_EXISTING_HOLDINGS"}   # a holder sitting out breaks proportion


def test_same_terms_settlement_window():
    base = {"instrument": "first_lien", "price": 99.0}
    ok = {"A": {**base, "settlement": date(2026, 11, 2)}, "B": {**base, "settlement": date(2026, 11, 16)}}
    late = {"A": {**base, "settlement": date(2026, 11, 2)}, "B": {**base, "settlement": date(2026, 11, 23)}}
    assert check_same_terms(ok, 10) == []                  # 10 business days
    assert check_same_terms(late, 10) == ["SETTLEMENT_OUTSIDE_WINDOW"]   # 15 business days
    assert check_same_terms({"A": {**base, "settlement": date(2026, 11, 2)},
                             "B": {"instrument": "first_lien", "price": 98.5, "settlement": date(2026, 11, 2)}}, 10) == ["DIFFERENT_PRICE"]


def test_settlement_override_raises_critical_same_terms_exception(con):
    r = run_allocation(con, "D010", escalation=ESC, settlement_overrides={"F3": "2026-12-31"})
    assert any(e["rule_id"] == "COINV-SAME-TERMS" and e["severity"] == "Critical" for e in r.exceptions)
    assert r.status == "pending_escalation"


# ---------------------------------------------------------------- sizing
def test_capacity_cap_redistributes_excess():
    final, notes, un = size_allocation(60, {"A": {"capacity": 10, "weight": 1, "min_ticket": 0},
                                            "B": {"capacity": 100, "weight": 1, "min_ticket": 0}}, 0.5)
    assert final == {"A": 10, "B": 50} and un == 0 and "CAPPED_AT_CAPACITY" in notes["A"]


def test_share_below_min_ticket_is_dropped_and_redistributed():
    final, notes, un = size_allocation(100, {"A": {"capacity": 500, "weight": 97, "min_ticket": 5},
                                             "B": {"capacity": 500, "weight": 3, "min_ticket": 5}}, 0.5)
    assert final == {"A": 100, "B": 0} and "SHARE_BELOW_MIN_TICKET" in notes["B"]


def test_undersubscribed_reports_unallocated():
    final, notes, un = size_allocation(300, {"A": {"capacity": 100, "weight": 1, "min_ticket": 0},
                                             "B": {"capacity": 50, "weight": 1, "min_ticket": 0}}, 0.5)
    assert final == {"A": 100, "B": 50} and un == 150


def test_two_keys_give_different_hypos_for_d001(con):
    a = lines(run_allocation(con, "D001", key="capacity", escalation=ESC))
    b = lines(run_allocation(con, "D001", key="target_weight", escalation=ESC))
    assert any(a[f]["final_amt"] != b[f]["final_amt"] for f in a)


@pytest.mark.parametrize("key", ["capacity", "target_weight"])
def test_invariants_hold_for_every_deal(con, key):
    funds = {r[0]: r[1] for r in con.execute("SELECT fund_id, min_ticket FROM funds")}
    for (deal_id, size) in con.execute("SELECT deal_id, size FROM deals WHERE rating IS NOT NULL").fetchall():
        r = run_allocation(con, deal_id, key=key, escalation=ESC)
        total = sum(ln["final_amt"] for ln in r.lines)
        assert abs(total + r.unallocated - size) < 1e-6, deal_id                 # every dollar accounted for
        for ln in r.lines:
            amt = ln["final_amt"]
            if not ln["eligible"]:
                assert amt == 0, (deal_id, ln)                                   # never allocate to ineligible
            else:
                assert amt <= ln["capacity"] + 1e-9, (deal_id, ln)               # never exceed capacity
                assert amt == 0 or amt >= funds[ln["fund_id"]], (deal_id, ln)   # respect minimum ticket
                assert abs(amt * 2 - round(amt * 2)) < 1e-9, (deal_id, ln)      # 0.5 lot size


def test_run_is_persisted_with_policy_version(con):
    r = run_allocation(con, "D002", mode="live", escalation=ESC)
    row = con.execute("SELECT mode, allocation_key, policy_version FROM allocation_runs WHERE run_id=?", (r.run_id,)).fetchone()
    assert row[0] == "live" and row[1] == "capacity" and "COINV-EXISTING-INVESTMENT@v1" in row[2]
    assert con.execute("SELECT COUNT(*) FROM allocation_lines WHERE run_id=?", (r.run_id,)).fetchone()[0] == 6
