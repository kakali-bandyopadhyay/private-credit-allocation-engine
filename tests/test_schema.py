import sqlite3
import pytest
from alloc.db import connect

EXPECTED = {"funds", "deals", "rules", "restricted_list", "holdings", "allocation_runs",
            "allocation_lines", "exceptions", "approvals", "bookings", "ledger_stub", "audit_log",
            "fund_portfolio", "recon_breaks", "surveillance_results"}


def test_schema_creates_all_tables():
    con = connect()
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED <= names


def test_maker_cannot_be_checker():
    con = connect()
    con.execute("INSERT INTO deals (deal_id, issuer, sector, rating, asset_class, instrument, size, price, expected_close) "
                "VALUES ('D001','IssuerA','Software','B','direct_lending','unitranche',100,99.0,'2026-11-01')")
    con.execute("INSERT INTO allocation_runs (run_id, deal_id, mode, policy_version, created_by, created_at, status) "
                "VALUES ('R0001','D001','live','v1','maker1','2026-10-19T00:00:00','proposed')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO approvals VALUES ('R0001','userA','userA','approve',NULL,'2026-10-19T00:00:00')")
