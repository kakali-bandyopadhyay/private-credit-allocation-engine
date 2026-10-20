"""Decision trace: input -> rule -> calculation -> outcome -> reviewer action -> audit record, with rule provenance."""
import json
from pathlib import Path

import pytest

from alloc import reports, trace
from alloc.db import connect
from alloc.intake import load_directory

DATA = Path(__file__).resolve().parents[1] / "data"
ESC = json.loads((DATA / "escalation.json").read_text())
SRC = DATA / "rule_sources.json"


@pytest.fixture(scope="module")
def con():
    c = connect()
    load_directory(c, DATA)
    reports.demo(c, ESC)
    return c


def live_run(con, deal):
    return con.execute("SELECT run_id FROM allocation_runs WHERE deal_id=? AND mode='live' AND status NOT IN "
                       "('superseded','blocked','withdrawn') ORDER BY run_id DESC", (deal,)).fetchone()[0]


def section(text, fund):
    start = text.index(f"### {fund} ")
    nxt = [i for i in (text.find("\n### ", start + 1), text.find("\n## ", start + 1)) if i > 0]
    return text[start:min(nxt)]


def test_exclude_shows_the_failing_rule_and_input(con):
    t = trace.trace(con, live_run(con, "D009"), sources=SRC)
    f5 = section(t, "F5")
    assert "**EXCLUDE**" in f5 and "`ELIG-RESTRICTED@v1`" in f5 and "**FAIL**" in f5 and "RESTRICTED_ISSUER" in f5
    assert "**FAIL**" not in section(t, "F1")


def test_cap_shows_inputs_calculation_and_both_funding_cases(con):
    t = trace.trace(con, live_run(con, "D008"), "F2", sources=SRC)
    assert "non-qualifying" in t and "NAV 2500" in t
    assert "Q / 0.70 - T = 57.7143" in t and "permitted (rounded down to the lot) **57.5**" in t
    assert "Q / 0.70 - T = 40.4" in t and "**40.0**" in t                       # cash case is tighter
    assert "funding **debt**" in t and "**binding**" in t and "ratio after allocation 70.006%" in t
    assert "### F2 → 57.5 · **CAP**" in t


def test_reviewer_action_and_audit_record(con):
    t = trace.trace(con, live_run(con, "D008"), "F2", sources=SRC)
    assert "APPROVE by **checker1** (maker maker1" in t and "Board step" in t
    assert "| RUN_CREATED | R0008 |" in t.replace(live_run(con, "D008"), "R0008") and "Chain verification: **intact**" in t
    assert "booked 57.5 · ledger 57.5" in t


def test_rebalance_uses_lineage_snapshot_and_shows_board_step_exceptions(con):
    t = trace.trace(con, live_run(con, "D003"), sources=SRC)
    assert "run-time snapshot recorded when" in t and " → " in t
    assert "**REBALANCE**" in section(t, "F1")
    assert "COINV-EXISTING-INVESTMENT" in t and "Superseded" in t


def test_hard_stop_on_unknown_funding(con):
    t = trace.trace(con, live_run(con, "D013"), sources=SRC)
    assert "funding **unknown**" in section(t, "F1") and "**ESCALATE**" in section(t, "F1")
    assert "reservations by R" in section(t, "F2") and "### F2 → 0" in t
    assert "No approval recorded (status submitted)" in t and "Hard stop" in t and "Not booked." in t


def test_every_rule_used_has_provenance_and_regulatory_rules_cite_sources(con):
    src = json.loads(SRC.read_text())["rules"]
    versions = {}
    for rid, ver in con.execute("SELECT rule_id, version FROM rules"):
        versions.setdefault(rid, []).append(ver)
    for rid, vs in versions.items():
        for v in vs:                                    # multi-version rules need one entry per version
            assert f"{rid}@v{v}" in src or (len(vs) == 1 and rid in src), f"{rid}@v{v}"
    for (rid,) in con.execute("SELECT DISTINCT rule_id FROM exceptions"):
        assert rid in src or rid.startswith("RECON"), rid
    for rid in ("SIZE-BDC-QUALIFYING-HEADROOM@v1", "SIZE-BDC-QUALIFYING-HEADROOM@v2", "COINV-SAME-TERMS", "COINV-EXISTING-INVESTMENT"):
        assert src[rid]["sources"] and "Legal and Compliance" in src[rid]["approval_needed"]
    t = trace.trace(con, live_run(con, "D008"), sources=SRC)
    assert "not documented" not in t and "law.cornell.edu/uscode/text/15/80a-54" in t
    assert "`SIZE-BDC-QUALIFYING-HEADROOM@v2` | public statute" in t and "section 61(a)(2)" in t
    assert all(x["url"] != "https://www.sec.gov/edgar/search/" for e in src.values() for x in e["sources"])   # direct links only


def test_bdc_asset_coverage_election_is_explicit():
    for f in json.loads((DATA / "fund_portfolio.json").read_text()):
        assert f["asset_coverage_minimum"].startswith("150%") and "otherwise 200%" in f["asset_coverage_minimum"]


def test_snapshot_matches_inputs_and_totals_tie_out(con):
    first = con.execute("SELECT payload FROM audit_log WHERE action='RUN_CREATED' ORDER BY seq").fetchone()[0]
    snap = json.loads(first)["inputs"]["funds"]
    funds = {f["fund_id"]: f for f in json.loads((DATA / "funds.json").read_text())}
    assert all(snap[k]["nav"] == funds[k]["nav"] and snap[k]["available_capital"] == funds[k]["available_capital"] for k in funds)
    rid = live_run(con, "D008")
    t = trace.trace(con, rid, sources=SRC)
    finals = [float(x.split(" → ")[1].split(" ")[0]) for x in t.splitlines() if x.startswith("### F")]
    size, = con.execute("SELECT size FROM deals WHERE deal_id='D008'").fetchone()
    unalloc, = con.execute("SELECT unallocated FROM allocation_runs WHERE run_id=?", (rid,)).fetchone()
    assert sum(finals) + unalloc == size
    codes = {c for (n,) in con.execute("SELECT sizing_notes FROM allocation_lines") for c in json.loads(n or "[]")}
    codes |= {c for (r,) in con.execute("SELECT ineligible_reason FROM allocation_lines WHERE ineligible_reason IS NOT NULL")
              for c in r.split(";")}
    assert codes <= set(trace.CODES)


def test_unknown_fund_refused(con):
    with pytest.raises(ValueError, match="not in run"):
        trace.trace(con, live_run(con, "D008"), "F9", sources=SRC)
