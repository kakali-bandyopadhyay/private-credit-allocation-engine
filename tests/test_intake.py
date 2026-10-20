from pathlib import Path

from alloc.db import connect
from alloc.intake import load_directory, validate_deals, validate_funds
from alloc.ratings import meets_floor

DATA = Path(__file__).resolve().parents[1] / "data"

GOOD_DEAL = {"deal_id": "X1", "issuer": "Test Co", "sector": "Software", "rating": "B",
             "asset_class": "direct_lending", "instrument": "first_lien", "size": 100,
             "price": 99.0, "expected_close": "2026-12-01"}
GOOD_FUND = {"fund_id": "FX", "name": "Test Fund", "vehicle_type": "BDC", "available_capital": 100,
             "target_weight": 0.1, "min_ticket": 5, "max_issuer_pct": 0.05, "rating_floor": "B",
             "excluded_sectors": [], "nav": 1000}


def test_seed_loads_expected_counts_and_flags_only_missing_rating():
    con = connect()
    issues = load_directory(con, DATA)
    count = lambda t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    assert (count("funds"), count("deals"), count("holdings"), count("restricted_list")) == (6, 13, 5, 1)
    assert [(i.entity_id, i.code, i.blocking) for i in issues] == [("D007", "MISSING_RATING", False)]
    assert count("exceptions") == 1
    assert con.execute("SELECT rule_id, severity, status FROM exceptions").fetchone() == ("DQ-MISSING_RATING", "High", "Open")


def test_duplicate_deal_id_rejected():
    valid, issues = validate_deals([GOOD_DEAL, dict(GOOD_DEAL)])
    assert len(valid) == 1
    assert [(i.code, i.blocking) for i in issues] == [("DUPLICATE_ID", True)]


def test_non_positive_size_rejected():
    valid, issues = validate_deals([{**GOOD_DEAL, "size": 0}])
    assert valid == [] and issues[0].code == "SCHEMA_INVALID" and "size" in issues[0].message


def test_rating_off_scale_rejected():
    valid, issues = validate_deals([{**GOOD_DEAL, "rating": "Z"}])
    assert valid == [] and "rating" in issues[0].message


def test_unknown_vehicle_type_rejected():
    valid, issues = validate_funds([{**GOOD_FUND, "vehicle_type": "hedge"}])
    assert valid == [] and "vehicle_type" in issues[0].message


def test_issuer_cap_above_100pct_rejected():
    valid, issues = validate_funds([{**GOOD_FUND, "max_issuer_pct": 1.5}])
    assert valid == [] and "max_issuer_pct" in issues[0].message


def test_rating_floor_comparison():
    assert meets_floor("BB-", "B")       # better than floor
    assert meets_floor("B", "B")         # equal to floor
    assert not meets_floor("CCC+", "B-")  # worse than floor
