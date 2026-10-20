"""BDC regulatory headroom – funding-aware and reservation-aware (B07).

Illustrative control under stated assumptions; NOT legal advice or a production compliance determination.
Classification of qualifying assets and the calculation basis must be validated against authoritative guidance
and the fund's own Legal/Compliance interpretation.

Simplified balance sheet per BDC (table fund_portfolio):
    Q = qualifying assets (includes cash and equivalents – assumed to count in the 70% basket)
    T = total assets,  S = senior securities (borrowings),  C = cash on hand

Buying x of a loan, paid with cash share c (0 = all debt, 1 = all cash):
    cash part  c*x : C -= c*x and Q -= c*x (cash leaves the basket), T unchanged
    debt part (1-c)*x : T += (1-c)*x and S += (1-c)*x
    the loan itself  : T is already counted above; Q += x only if the loan is a qualifying asset
"""
from __future__ import annotations

import math
import sqlite3

INF = float("inf")


def resolve_funding(con: sqlite3.Connection, deal_id: str, fund_id: str) -> dict:
    """Deal-level plan first, else the fund's default, else unknown."""
    r = con.execute("SELECT source, cash_share FROM funding_plans WHERE deal_id=? AND fund_id=?",
                    (deal_id, fund_id)).fetchone()
    if r and r[0] != "unknown":
        src, c = r
        c = 1.0 if src == "cash" else 0.0 if src == "debt" else c
        return {"source": src, "cash_share": c, "basis": "deal plan"}
    if r is None:
        d = con.execute("SELECT default_funding FROM fund_portfolio WHERE fund_id=?", (fund_id,)).fetchone()
        if d and d[0] in ("cash", "debt"):
            return {"source": d[0], "cash_share": 1.0 if d[0] == "cash" else 0.0, "basis": "fund default"}
    return {"source": "unknown", "cash_share": None, "basis": "deal plan" if r else "none"}


def apply_purchase(state: dict, x: float, c: float, qualifying: bool) -> dict:
    """Balance sheet after buying x with cash share c (pure function)."""
    cash_part, debt_part = c * x, (1 - c) * x
    return {"Q": state["Q"] - cash_part + (x if qualifying else 0.0),
            "T": state["T"] + debt_part,
            "S": state["S"] + debt_part,
            "C": state["C"] - cash_part}


def limits(state: dict, c: float, qualifying: bool, min_ratio: float, min_cov: float) -> dict:
    """Largest x allowed by each test for cash share c (before lot rounding)."""
    Q, T, S, C = state["Q"], state["T"], state["S"], state["C"]
    out = {}
    if qualifying:
        out["qualifying_70"] = INF                       # a qualifying loan never lowers the ratio below where it is
    else:                                              # (Q - c x) / (T + (1-c) x) >= m
        out["qualifying_70"] = max(0.0, (Q - min_ratio * T) / (c + min_ratio * (1 - c)))
    out["cash_on_hand"] = INF if c == 0 else max(0.0, C / c)
    if c >= 1:                                         # (T + d) / (S + d) >= a,  d = (1-c) x
        out["asset_coverage_150"] = INF
    else:
        out["asset_coverage_150"] = max(0.0, (T - min_cov * S) / ((min_cov - 1) * (1 - c)))
    return out


def floor_lot(x: float, lot: float) -> float:
    return INF if x == INF else math.floor(x / lot + 1e-9) * lot


def portfolio(con: sqlite3.Connection, fund_id: str) -> dict | None:
    r = con.execute("SELECT qualifying_assets, total_assets, senior_securities, cash FROM fund_portfolio "
                    "WHERE fund_id=?", (fund_id,)).fetchone()
    return None if r is None else {"Q": r[0], "T": r[1], "S": r[2], "C": r[3]}


def reservations(con: sqlite3.Connection, fund_id: str, exclude_deal: str | None, statuses: list[str]) -> list[dict]:
    """Allocations in live runs that are not yet booked and would still be booked (they hold headroom)."""
    q = (f"SELECT r.run_id, r.deal_id, r.status, l.final_amt, d.qualifying FROM allocation_runs r "
         f"JOIN allocation_lines l ON l.run_id=r.run_id JOIN deals d ON d.deal_id=r.deal_id "
         f"WHERE r.mode='live' AND r.status IN ({','.join('?' * len(statuses))}) AND l.fund_id=? AND l.final_amt>0 "
         f"AND (? IS NULL OR r.deal_id<>?) ORDER BY r.run_id")
    out = []
    for run_id, deal_id, status, amt, qual in con.execute(q, (*statuses, fund_id, exclude_deal, exclude_deal)):
        f = resolve_funding(con, deal_id, fund_id)
        c = f["cash_share"] if f["cash_share"] is not None else 1.0 if not qual else 0.0
        out.append({"run_id": run_id, "deal_id": deal_id, "status": status, "amount": amt, "qualifying": bool(qual),
                    "cash_share_assumed": c, "funding": f["source"]})
    return out


def pretrade_check(con: sqlite3.Connection, fund_id: str, deal: dict, params: dict, lot: float) -> dict | None:
    """Funding-aware, reservation-aware headroom for one BDC on one deal."""
    base = portfolio(con, fund_id)
    if base is None:
        return None
    qualifying = bool(deal["qualifying"])
    m, a = params["min_ratio"], params.get("min_asset_coverage", 1.5)
    res = reservations(con, fund_id, deal["deal_id"], params.get("reserve_statuses", []))
    state = dict(base)
    for r in res:                                      # pro-forma: pending deals already take their room
        state = apply_purchase(state, r["amount"], r["cash_share_assumed"], r["qualifying"])
    funding = resolve_funding(con, deal["deal_id"], fund_id)
    scenarios = {}
    for name, c in (("debt", 0.0), ("cash", 1.0)) + ((("plan", funding["cash_share"]),)
                                                       if funding["cash_share"] not in (None, 0.0, 1.0) else ()):
        lim = limits(state, c, qualifying, m, a)
        binding = min(lim, key=lim.get)
        mx = lim[binding]
        permit = floor_lot(mx, lot)
        after = apply_purchase(state, permit, c, qualifying) if permit != INF else None
        scenarios[name] = {"cash_share": c, "limits": {k: (None if v == INF else round(v, 6)) for k, v in lim.items()},
                           "binding_test": binding if mx != INF else None,
                           "max": None if mx == INF else round(mx, 6), "permitted": None if permit == INF else permit,
                           "ratio_at_permitted": None if after is None else round(after["Q"] / after["T"], 6),
                           "coverage_at_permitted": None if after is None or after["S"] == 0 else round(after["T"] / after["S"], 6)}
    if funding["source"] == "unknown":
        applied = min(scenarios.values(), key=lambda s: INF if s["permitted"] is None else s["permitted"])
        applied_name = next(k for k, v in scenarios.items() if v is applied)
    else:
        applied_name = "debt" if funding["cash_share"] == 0 else "cash" if funding["cash_share"] == 1 else "plan"
        applied = scenarios[applied_name]
    return {"fund_id": fund_id, "check": "BDC_REGULATORY_HEADROOM_PRETRADE", "deal_qualifying": qualifying,
            "min_ratio": m, "min_asset_coverage": a, "funding": funding, "applied_scenario": applied_name,
            "balance_sheet": {k: round(v, 6) for k, v in base.items()},
            "reservations": res, "pro_forma": {k: round(v, 6) for k, v in state.items()},
            "start_ratio": round(state["Q"] / state["T"], 6),
            "start_coverage": round(state["T"] / state["S"], 6) if state["S"] else None,
            "scenarios": scenarios, "permitted": applied["permitted"]}


def ratios_after(check: dict, amount: float) -> dict:
    sc = check["scenarios"][check["applied_scenario"]]
    after = apply_purchase(check["pro_forma"], amount, sc["cash_share"], check["deal_qualifying"])
    return {"ratio_after": round(after["Q"] / after["T"], 6),
            "coverage_after": round(after["T"] / after["S"], 6) if after["S"] else None}


def set_funding(con: sqlite3.Connection, deal_id: str, fund_id: str, source: str, cash_share: float | None,
                by: str, reason: str) -> list[str]:
    """Record confirmed funding for one fund on one deal; resolves the matching FUNDING-UNKNOWN exceptions."""
    from datetime import datetime, timezone
    from . import audit
    if source not in ("cash", "debt", "mixed"):
        raise ValueError("source must be cash, debt or mixed")
    if source == "mixed" and (cash_share is None or not 0 < cash_share < 1):
        raise ValueError("mixed funding needs a cash share strictly between 0 and 1")
    if not (reason and reason.strip()):
        raise ValueError("a funding confirmation needs a reason (who confirmed it, on what basis)")
    c = 1.0 if source == "cash" else 0.0 if source == "debt" else cash_share
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    before = resolve_funding(con, deal_id, fund_id)
    con.execute("INSERT INTO funding_plans (deal_id, fund_id, source, cash_share, note, updated_by, updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(deal_id, fund_id) DO UPDATE SET source=excluded.source, "
                "cash_share=excluded.cash_share, note=excluded.note, updated_by=excluded.updated_by, "
                "updated_at=excluded.updated_at", (deal_id, fund_id, source, c, reason, by, now))
    open_exc = [r[0] for r in con.execute(
        "SELECT e.exc_id FROM exceptions e JOIN allocation_runs r ON r.run_id=e.run_id WHERE e.status='Open' "
        "AND e.rule_id='FUNDING-UNKNOWN' AND e.fund_id=? AND r.deal_id=?", (fund_id, deal_id))]
    for e in open_exc:
        con.execute("UPDATE exceptions SET status='Resolved', resolved_at=?, resolution=? WHERE exc_id=?",
                    (now, f"funding confirmed as {source} (cash share {c}) by {by}: {reason}. The conservative amount "
                          f"already sized is within the limit for any funding mix.", e))
    audit.append(con, by, "FUNDING_CONFIRMED", deal_id, {"fund_id": fund_id, "before": before["source"],
                                                          "after": source, "cash_share": c, "reason": reason,
                                                          "resolved_exceptions": open_exc})
    con.commit()
    return open_exc


def reservation_report(con: sqlite3.Connection, statuses: list[str]) -> list[dict]:
    """Current headroom held by live, unbooked runs, per BDC."""
    out = []
    for (fid,) in con.execute("SELECT fund_id FROM fund_portfolio ORDER BY fund_id").fetchall():
        base = portfolio(con, fid)
        res = reservations(con, fid, None, statuses)
        state = dict(base)
        for r in res:
            state = apply_purchase(state, r["amount"], r["cash_share_assumed"], r["qualifying"])
        out.append({"fund_id": fid, "reservations": res,
                    "booked_ratio": round(base["Q"] / base["T"], 6), "pro_forma_ratio": round(state["Q"] / state["T"], 6),
                    "booked_coverage": round(base["T"] / base["S"], 6), "pro_forma_coverage": round(state["T"] / state["S"], 6)})
    return out
