"""Allocation run: eligibility gates -> sizing -> co-investment condition checks -> escalation.

These are four SEPARATE decisions (design choice, see docs/SPEC.md §4):
1. Eligibility  – hard gates from mandate, guidelines, restrictions. Fail = fund excluded, with coded reasons.
2. Sizing       – multi-factor, documented. Capacity = min(available capital, concentration headroom).
                  A documented allocation key splits the deal among eligible funds, capped at capacity,
                  then minimum ticket and lot size apply. Capital and concentration REDUCE shares;
                  they do not make a fund ineligible.
3. Co-investment conditions (illustrative, modelled on the 2025 relief framework):
                  same terms (instrument, price, settlement within 10 business days) and the
                  existing-investment condition for regulated funds (BDCs).
4. Escalation   – each exception gets a severity and an owner from data/escalation.json.
All policy is illustrative. Data is synthetic.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .ratings import meets_floor

LOT_EPS = 1e-9
REGULATED_VEHICLES = {"BDC"}


# ---------------------------------------------------------------- data access
def _rules(con) -> dict[str, dict]:
    """Latest version of each rule, keyed by rule_id."""
    out = {}
    for rid, ver, scope, rtype, params in con.execute(
            "SELECT rule_id, version, scope, rule_type, params FROM rules ORDER BY rule_id, version"):
        out[rid] = {"version": ver, "scope": scope, "type": rtype, "params": json.loads(params)}
    return out


def policy_version(rules: dict) -> str:
    return ";".join(f"{k}@v{v['version']}" for k, v in sorted(rules.items()))


def _funds(con) -> list[dict]:
    cols = ["fund_id", "name", "vehicle_type", "available_capital", "target_weight", "min_ticket",
            "max_issuer_pct", "rating_floor", "excluded_sectors", "nav"]
    rows = con.execute(f"SELECT {', '.join(cols)} FROM funds ORDER BY fund_id").fetchall()
    funds = [dict(zip(cols, r)) for r in rows]
    for f in funds:
        f["excluded_sectors"] = json.loads(f["excluded_sectors"])
    return funds


def _deal(con, deal_id: str) -> dict:
    cols = ["deal_id", "issuer", "sector", "rating", "asset_class", "instrument", "size", "price", "expected_close",
            "qualifying"]
    r = con.execute(f"SELECT {', '.join(cols)} FROM deals WHERE deal_id=?", (deal_id,)).fetchone()
    if r is None:
        raise ValueError(f"unknown deal {deal_id}")
    return dict(zip(cols, r))


def _holdings(con, issuer: str) -> dict[str, float]:
    return dict(con.execute("SELECT fund_id, exposure FROM holdings WHERE issuer=?", (issuer,)).fetchall())


def _restricted(con, issuer: str, as_of: str) -> set[str | None]:
    """Fund IDs for which the issuer is restricted (None = restricted for all funds)."""
    rows = con.execute(
        "SELECT fund_id FROM restricted_list WHERE issuer=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)",
        (issuer, as_of, as_of)).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------- 1. eligibility
def check_eligibility(deal: dict, fund: dict, rules: dict, restricted: set) -> list[str]:
    """Return a list of failed-gate reason codes (empty list = eligible)."""
    reasons = []
    for rid, r in rules.items():
        if r["type"] != "eligibility_gate" or r["scope"] not in ("global", fund["fund_id"]):
            continue
        check = r["params"]["check"]
        if check == "allowed_asset_classes" and deal["asset_class"] not in r["params"]["allowed"]:
            reasons.append("ASSET_CLASS_NOT_PERMITTED")
        elif check == "allowed_instruments" and deal["instrument"] not in r["params"]["allowed"]:
            reasons.append("INSTRUMENT_NOT_PERMITTED")
        elif check == "rating_floor" and not meets_floor(deal["rating"], fund["rating_floor"]):
            reasons.append("BELOW_RATING_FLOOR")
        elif check == "sector_exclusion" and deal["sector"] in fund["excluded_sectors"]:
            reasons.append("SECTOR_EXCLUDED")
        elif check == "restricted_list" and (None in restricted or fund["fund_id"] in restricted):
            reasons.append("RESTRICTED_ISSUER")
    return reasons


# ---------------------------------------------------------------- 2. sizing
def _floor_lot(x: float, lot: float) -> float:
    return math.floor(x / lot + LOT_EPS) * lot


def size_allocation(size: float, cands: dict[str, dict], lot: float) -> tuple[dict[str, float], dict[str, list[str]], float]:
    """Split `size` across candidates.

    cands: fund_id -> {"capacity", "weight", "min_ticket"}
    Returns (final amounts, sizing notes per fund, unallocated amount).
    Algorithm: weight-proportional split; any fund whose share exceeds capacity is fixed at capacity
    and the remainder is re-split among the others (repeat until stable); funds whose share falls
    below their minimum ticket are dropped and the split is re-run; amounts are rounded down to the
    lot size and rounding remainders go, one lot at a time, to the fund with the largest rounding
    residual that still has capacity.
    """
    notes = {f: [] for f in cands}
    active = {f for f, c in cands.items() if c["capacity"] > 0 and c["weight"] > 0}
    for f in set(cands) - active:
        notes[f].append("NO_CAPACITY" if cands[f]["capacity"] <= 0 else "ZERO_WEIGHT")
    for f in list(active):
        if cands[f]["capacity"] + LOT_EPS < cands[f]["min_ticket"]:
            active.discard(f)
            notes[f].append("CAPACITY_BELOW_MIN_TICKET")

    while True:
        raw, free, remaining = {}, set(active), size
        while free and remaining > LOT_EPS:
            w = sum(cands[f]["weight"] for f in free)
            shares = {f: remaining * cands[f]["weight"] / w for f in free}
            over = [f for f in free if shares[f] >= cands[f]["capacity"] - LOT_EPS]
            if not over:
                raw.update(shares)
                remaining = 0.0
                break
            for f in over:
                raw[f] = cands[f]["capacity"]
                remaining -= cands[f]["capacity"]
                free.discard(f)
                notes[f].append("CAPPED_AT_CAPACITY")
        below = [f for f, a in raw.items() if LOT_EPS < a < cands[f]["min_ticket"] - LOT_EPS]
        if not below:
            break
        for f in below:
            active.discard(f)
            notes[f] = [n for n in notes[f] if n != "CAPPED_AT_CAPACITY"] + ["SHARE_BELOW_MIN_TICKET"]
        for f in active:
            notes[f] = [n for n in notes[f] if n != "CAPPED_AT_CAPACITY"]

    final = {f: _floor_lot(a, lot) for f, a in raw.items()}
    target = min(size, sum(raw.values()))
    leftover = round(target - sum(final.values()), 9)
    while leftover >= lot - LOT_EPS:
        room = [f for f in final if final[f] + lot <= cands[f]["capacity"] + LOT_EPS]
        if not room:
            break
        f = max(room, key=lambda k: (raw[k] - final[k], raw[k]))
        final[f] += lot
        leftover = round(leftover - lot, 9)
    for f in cands:
        final.setdefault(f, 0.0)
    unallocated = round(size - sum(final.values()), 9)
    return final, notes, unallocated


# ---------------------------------------------------------------- 3. co-investment conditions
def business_days_between(a: date, b: date) -> int:
    lo, hi = sorted((a, b))
    days, d = 0, lo
    while d < hi:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def check_same_terms(participants: dict[str, dict], window_bd: int) -> list[str]:
    """participants: fund_id -> {"instrument", "price", "settlement"(date)}. Returns violation codes."""
    if len(participants) < 2:
        return []
    out = []
    vals = list(participants.values())
    if len({v["instrument"] for v in vals}) > 1:
        out.append("DIFFERENT_INSTRUMENT")
    if len({v["price"] for v in vals}) > 1:
        out.append("DIFFERENT_PRICE")
    dates = [v["settlement"] for v in vals]
    if business_days_between(min(dates), max(dates)) > window_bd:
        out.append("SETTLEMENT_OUTSIDE_WINDOW")
    return out


def check_existing_investment(final: dict[str, float], holders: dict[str, float],
                              vehicle: dict[str, str], tolerance: float) -> dict[str, str]:
    """Return {regulated fund_id: reason} for regulated participants that need the board step."""
    if not holders:
        return {}
    exp_total = sum(holders.values())
    alloc_holders = {h: final.get(h, 0.0) for h in holders}
    alloc_total = sum(alloc_holders.values())
    proportional = alloc_total > 0 and all(a > 0 for a in alloc_holders.values()) and all(
        abs(alloc_holders[h] / alloc_total - holders[h] / exp_total) <= tolerance for h in holders)
    needs = {}
    for f, amt in final.items():
        if amt <= 0 or vehicle[f] not in REGULATED_VEHICLES:
            continue
        if f not in holders:
            needs[f] = "AFFILIATE_HOLDS_ISSUER_FUND_DOES_NOT"
        elif not proportional:
            needs[f] = "NOT_IN_PROPORTION_TO_EXISTING_HOLDINGS"
    return needs


# ---------------------------------------------------------------- run
@dataclass
class RunResult:
    run_id: str
    deal_id: str
    mode: str
    key: str
    status: str
    lines: list[dict] = field(default_factory=list)
    exceptions: list[dict] = field(default_factory=list)
    unallocated: float = 0.0
    source_run_id: str | None = None
    pretrade_checks: list[dict] = field(default_factory=list)   # B06: evidence of pre-trade regulatory caps


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _next_id(con, table: str, col: str, prefix: str) -> str:
    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] + 1
    while con.execute(f"SELECT 1 FROM {table} WHERE {col}=?", (f"{prefix}{n:04d}",)).fetchone():
        n += 1
    return f"{prefix}{n:04d}"


def _due(opened_iso: str, severity: str, escalation: dict) -> str | None:
    hrs = escalation.get(severity, {}).get("sla_hours")
    if hrs is None:
        return None
    return (datetime.fromisoformat(opened_iso) + timedelta(hours=hrs)).isoformat(timespec="seconds")


def _finalise(con, deal: dict, funds: list[dict], rules: dict, res: RunResult, holders: dict,
              escalation: dict, created_by: str, settlement_overrides: dict | None, exc: list[dict]) -> RunResult:
    """Co-investment checks on the final amounts, status, persistence and audit entry."""
    from . import audit

    def raise_exc(fund_id, rule_id, severity, detail, owner=None):
        exc.append({"fund_id": fund_id, "rule_id": rule_id, "severity": severity,
                    "owner": owner or escalation.get(severity, {}).get("owner"), "detail": detail})

    final = {ln["fund_id"]: ln["final_amt"] for ln in res.lines}
    if res.status not in ("blocked", "no_eligible_funds"):
        participants = {fid: {"instrument": deal["instrument"], "price": deal["price"],
                              "settlement": date.fromisoformat((settlement_overrides or {}).get(fid, deal["expected_close"]))}
                        for fid, amt in final.items() if amt > 0}
        st = rules["COINV-SAME-TERMS"]["params"]
        for code in check_same_terms(participants, st["settlement_window_business_days"]):
            raise_exc(None, "COINV-SAME-TERMS", "Critical", f"same-terms condition failed: {code}")
        ei = rules["COINV-EXISTING-INVESTMENT"]["params"]
        vehicle = {f["fund_id"]: f["vehicle_type"] for f in funds}
        for fid, why in check_existing_investment(final, holders, vehicle, ei["proportion_tolerance"]).items():
            raise_exc(fid, "COINV-EXISTING-INVESTMENT", "Critical",
                      f"board step required before {fid} co-invests: {why} (affiliate holders: {', '.join(sorted(holders))})")
        if any(e["severity"] == "Critical" for e in exc) and res.status == "proposed":
            res.status = "pending_escalation"
        # B07.1 – unknown funding is a hard stop on every BDC line that receives an amount (booking refuses it), so it
        # is always a visible High exception, on every path (run, rebalance, promote). Only set-funding, or rejecting
        # / releasing the run, closes it – an approval rationale cannot.
        from . import bdc
        checks = {c["fund_id"]: c for c in res.pretrade_checks}
        for fid, amt in sorted(final.items()):
            if amt > 0 and bdc.portfolio(con, fid) is not None and \
                    bdc.resolve_funding(con, deal["deal_id"], fid)["source"] == "unknown":
                c = checks.get(fid)
                basis = (f"conservative {c['applied_scenario']} scenario applied (permitted {c['permitted']}, allocated {amt:g})"
                         if c else f"allocated {amt:g}")
                raise_exc(fid, "FUNDING-UNKNOWN", "High",
                          f"{fid}: funding for {deal['deal_id']} not confirmed; {basis}; booking blocked until "
                          f"set-funding – an approval rationale cannot clear this", owner="Compliance")

    created = _now()
    review_hrs = rules.get("CTRL-REVIEW", {}).get("params", {}).get("reservation_review_hours")
    review_by = ((datetime.fromisoformat(created) + timedelta(hours=review_hrs)).isoformat(timespec="seconds")
                 if res.mode == "live" and review_hrs else None)
    con.execute("INSERT INTO allocation_runs (run_id, deal_id, mode, policy_version, allocation_key, source_run_id, "
                "unallocated, created_by, created_at, status, review_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (res.run_id, res.deal_id, res.mode, policy_version(rules), res.key, res.source_run_id,
                 res.unallocated, created_by, created, res.status, review_by))
    for ln in res.lines:
        con.execute("INSERT INTO allocation_lines VALUES (?,?,?,?,?,?,?,?,?)",
                    (res.run_id, ln["fund_id"], ln["eligible"], ln["ineligible_reason"], ln["capacity"], ln["weight"],
                     ln["proposed_amt"], ln["final_amt"], json.dumps(ln["sizing_notes"])))
    for e in exc:
        e["exc_id"] = _next_id(con, "exceptions", "exc_id", "E")
        opened = _now()
        e["due_at"] = _due(opened, e["severity"], escalation)
        con.execute("INSERT INTO exceptions (exc_id, run_id, fund_id, rule_id, severity, status, owner, opened_at, detail, due_at) "
                    "VALUES (?,?,?,?,?,'Open',?,?,?,?)",
                    (e["exc_id"], res.run_id, e["fund_id"], e["rule_id"], e["severity"], e["owner"], opened, e["detail"], e["due_at"]))
    res.exceptions = exc
    superseded = []
    if res.source_run_id:
        superseded = [r[0] for r in con.execute(
            "SELECT exc_id FROM exceptions WHERE run_id=? AND status='Open'", (res.source_run_id,))]
        con.execute("UPDATE exceptions SET status='Superseded', resolved_at=?, resolution=? "
                    "WHERE run_id=? AND status='Open'", (_now(), f"superseded by {res.run_id}", res.source_run_id))
        con.execute("UPDATE allocation_runs SET status='superseded' WHERE run_id=? AND mode='hypo'", (res.source_run_id,))
    audit.append(con, created_by, "RUN_CREATED", res.run_id, {
        "superseded_exceptions": superseded,
        "deal_id": res.deal_id, "mode": res.mode, "key": res.key, "status": res.status,
        "source_run_id": res.source_run_id, "policy_version": policy_version(rules),
        "final": {ln["fund_id"]: ln["final_amt"] for ln in res.lines},
        "pretrade_checks": res.pretrade_checks,
        "exceptions": [e["exc_id"] for e in exc]})
    con.commit()
    return res


def run_allocation(con: sqlite3.Connection, deal_id: str, mode: str = "hypo", key: str | None = None,
                   created_by: str = "maker1", settlement_overrides: dict[str, str] | None = None,
                   escalation: dict | None = None) -> RunResult:
    rules = _rules(con)
    key = key or rules["SIZE-KEY"]["params"]["default_key"]
    if key not in {"capacity", "target_weight"}:
        raise ValueError(f"unknown allocation key {key}")
    escalation = escalation or {}
    deal, funds = _deal(con, deal_id), _funds(con)
    res = RunResult(_next_id(con, "allocation_runs", "run_id", "R"), deal_id, mode, key, "proposed")
    exc: list[dict] = []
    holders: dict = {}

    if deal["rating"] is None:                                    # DQ block: never allocate on incomplete data
        res.status = "blocked"
        exc.append({"fund_id": None, "rule_id": "DQ-MISSING_RATING", "severity": "High", "owner": "data_ops",
                    "detail": f"{deal_id} has no rating; allocation blocked until resolved"})
        for f in funds:
            res.lines.append({"fund_id": f["fund_id"], "eligible": 0, "ineligible_reason": "DEAL_BLOCKED_DQ",
                              "capacity": None, "weight": None, "proposed_amt": 0.0, "final_amt": 0.0, "sizing_notes": []})
        return _finalise(con, deal, funds, rules, res, holders, escalation, created_by, settlement_overrides, exc)

    restricted = _restricted(con, deal["issuer"], date.today().isoformat())
    holders = _holdings(con, deal["issuer"])
    cands, gates = {}, {}
    # B07 – capital and issuer exposure already committed to other live, unbooked runs are reserved
    qh_rule = rules.get("SIZE-BDC-QUALIFYING-HEADROOM", {}).get("params", {})
    rs = qh_rule.get("reserve_statuses", ["proposed", "partially_allocated", "pending_escalation", "submitted", "approved"])
    reserved_cap, reserved_iss = {}, {}
    for fid, amt, iss in con.execute(
            f"SELECT l.fund_id, l.final_amt, d.issuer FROM allocation_runs r JOIN allocation_lines l ON l.run_id=r.run_id "
            f"JOIN deals d ON d.deal_id=r.deal_id WHERE r.mode='live' AND r.status IN ({','.join('?' * len(rs))}) "
            f"AND r.deal_id<>? AND l.final_amt>0", (*rs, deal_id)):
        reserved_cap[fid] = reserved_cap.get(fid, 0.0) + amt
        if iss == deal["issuer"]:
            reserved_iss[fid] = reserved_iss.get(fid, 0.0) + amt
    for f in funds:
        reasons = check_eligibility(deal, f, rules, restricted)
        gates[f["fund_id"]] = reasons
        if reasons:
            continue
        headroom = f["max_issuer_pct"] * f["nav"] - holders.get(f["fund_id"], 0.0) - reserved_iss.get(f["fund_id"], 0.0)
        free_capital = f["available_capital"] - reserved_cap.get(f["fund_id"], 0.0)
        capacity = max(0.0, min(free_capital, headroom))
        weight = capacity if key == "capacity" else f["target_weight"]
        cands[f["fund_id"]] = {"capacity": capacity, "weight": weight, "min_ticket": f["min_ticket"],
                               "headroom": headroom, "free_capital": free_capital,
                               "reserved": reserved_cap.get(f["fund_id"], 0.0)}
    lot = rules["SIZE-LOT"]["params"]["lot"]
    # B06/B07 – pre-trade BDC regulatory headroom (funding-aware, reservation-aware): caps the amount, never the weight
    qh = rules.get("SIZE-BDC-QUALIFYING-HEADROOM")
    reg_capped: dict[str, str] = {}
    if qh:
        from . import bdc
        vtypes = {f["fund_id"]: f["vehicle_type"] for f in funds}
        for fid, c in cands.items():
            if vtypes[fid] not in qh["params"]["applies_to_vehicle_types"]:
                continue
            chk = bdc.pretrade_check(con, fid, deal, qh["params"], lot)
            if chk is None:
                continue
            chk["capacity_before"] = round(c["capacity"], 6)
            sc = chk["scenarios"][chk["applied_scenario"]]
            binding = chk["permitted"] is not None and chk["permitted"] < c["capacity"]
            if binding:
                c["capacity"] = chk["permitted"]
                reg_capped[fid] = sc["binding_test"]
            chk["binding"] = binding
            res.pretrade_checks.append(chk)
    final, notes, unalloc = size_allocation(deal["size"], cands, lot) if cands else ({}, {}, deal["size"])
    avail = {f["fund_id"]: f["available_capital"] for f in funds}
    for fid, c in cands.items():
        if c["headroom"] < min(avail[fid], c["free_capital"]):
            notes[fid].insert(0, "CAPACITY_SET_BY_CONCENTRATION_LIMIT")
        elif c["reserved"] > 0 and c["free_capital"] < avail[fid] and c["free_capital"] <= c["headroom"]:
            notes[fid].insert(0, "CAPITAL_RESERVED_BY_PENDING_RUNS")
        if fid in reg_capped:
            code = {"qualifying_70": "CAPACITY_SET_BY_QUALIFYING_ASSETS_HEADROOM",
                    "asset_coverage_150": "CAPACITY_SET_BY_ASSET_COVERAGE_HEADROOM",
                    "cash_on_hand": "CAPACITY_SET_BY_CASH_ON_HAND"}[reg_capped[fid]]
            notes[fid].insert(0, code)
            chk = next(k for k in res.pretrade_checks if k["fund_id"] == fid)
            if chk["reservations"]:
                notes[fid].insert(1, "HEADROOM_REDUCED_BY_PENDING_RUNS")
    from . import bdc as _bdc
    for chk in res.pretrade_checks:                       # record the outcome against the final amount
        amt = final.get(chk["fund_id"], 0.0)
        chk["allocated"] = amt
        chk.update(_bdc.ratios_after(chk, amt))
        chk["passes"] = (chk["ratio_after"] >= chk["min_ratio"] - 1e-9 or chk["deal_qualifying"]) and \
                        (chk["coverage_after"] is None or chk["coverage_after"] >= chk["min_asset_coverage"] - 1e-9)
    res.unallocated = unalloc
    for f in funds:
        fid = f["fund_id"]
        elig = not gates[fid]
        res.lines.append({"fund_id": fid, "eligible": int(elig),
                          "ineligible_reason": ";".join(gates[fid]) or None,
                          "capacity": round(cands[fid]["capacity"], 6) if elig else None,
                          "weight": round(cands[fid]["weight"], 6) if elig else None,
                          "proposed_amt": final.get(fid, 0.0), "final_amt": final.get(fid, 0.0),
                          "sizing_notes": notes.get(fid, [])})
    if not cands:
        res.status = "no_eligible_funds"
        exc.append({"fund_id": None, "rule_id": "ELIG-NONE", "severity": "Medium",
                    "owner": escalation.get("Medium", {}).get("owner"), "detail": f"no eligible fund for {deal_id}"})
    elif unalloc > LOT_EPS:
        res.status = "partially_allocated"
        exc.append({"fund_id": None, "rule_id": "SIZE-UNDERSUBSCRIBED", "severity": "Medium",
                    "owner": escalation.get("Medium", {}).get("owner"),
                    "detail": f"{unalloc:g} of {deal['size']:g} not placed (all eligible funds at capacity)"})
    return _finalise(con, deal, funds, rules, res, holders, escalation, created_by, settlement_overrides, exc)


# ---------------------------------------------------------------- derived runs
def load_run(con, run_id: str) -> RunResult:
    r = con.execute("SELECT run_id, deal_id, mode, allocation_key, status, unallocated, source_run_id "
                    "FROM allocation_runs WHERE run_id=?", (run_id,)).fetchone()
    if r is None:
        raise ValueError(f"unknown run {run_id}")
    res = RunResult(r[0], r[1], r[2], r[3], r[4], unallocated=r[5], source_run_id=r[6])
    for fid, el, why, cap, w, prop, fin, notes in con.execute(
            "SELECT fund_id, eligible, ineligible_reason, capacity, weight, proposed_amt, final_amt, sizing_notes "
            "FROM allocation_lines WHERE run_id=? ORDER BY fund_id", (run_id,)):
        res.lines.append({"fund_id": fid, "eligible": el, "ineligible_reason": why, "capacity": cap, "weight": w,
                          "proposed_amt": prop, "final_amt": fin, "sizing_notes": json.loads(notes or "[]")})
    return res


def rebalance_proportional(con, run_id: str, created_by: str = "maker1", escalation: dict | None = None) -> RunResult:
    """New hypo: bring affiliate holders into proportion with their existing holdings (within capacity),
    and re-split the freed amount across the other participating funds by the run's key.

    Holder amounts become s × existing exposure, with s = min(capacity_h / exposure_h, holders' current total /
    total exposure), rounded down to the lot. This never increases the holders' combined share.
    """
    src = load_run(con, run_id)
    if src.status in ("blocked", "no_eligible_funds"):
        raise ValueError(f"run {run_id} has no allocation to rebalance")
    rules, deal, funds = _rules(con), _deal(con, src.deal_id), _funds(con)
    holders = _holdings(con, deal["issuer"])
    lines = {ln["fund_id"]: dict(ln, sizing_notes=list(ln["sizing_notes"])) for ln in src.lines}
    held = {h: e for h, e in holders.items() if lines.get(h, {}).get("eligible")}
    if not held:
        raise ValueError(f"no eligible affiliate holder of {deal['issuer']} in run {run_id}")
    lot = rules["SIZE-LOT"]["params"]["lot"]
    current = sum(lines[h]["final_amt"] for h in held)
    s = min([lines[h]["capacity"] / e for h, e in held.items()] + [current / sum(held.values())])
    mins = {f["fund_id"]: f["min_ticket"] for f in funds}
    new_amt = {h: _floor_lot(s * e, lot) for h, e in held.items()}
    if any(0 < a < mins[h] for h, a in new_amt.items()) or any(a == 0 for a in new_amt.values()):
        raise ValueError("proportional amounts fall below a holder's minimum ticket; rebalance not possible – escalate")
    freed = round(current - sum(new_amt.values()), 9)
    for h, a in new_amt.items():
        lines[h]["final_amt"] = a
        lines[h]["sizing_notes"].append("PROPORTIONAL_REBALANCE")
    others = {fid: ln for fid, ln in lines.items() if ln["eligible"] and fid not in held and ln["final_amt"] > 0}
    if freed > LOT_EPS and others:
        cands = {fid: {"capacity": ln["capacity"] - ln["final_amt"], "weight": ln["weight"], "min_ticket": 0}
                 for fid, ln in others.items()}
        inc, _, left = size_allocation(freed, cands, lot)
        for fid, a in inc.items():
            if a > 0:
                lines[fid]["final_amt"] = round(lines[fid]["final_amt"] + a, 9)
                lines[fid]["sizing_notes"].append("RECEIVED_REBALANCE_EXCESS")
        freed = left
    res = RunResult(_next_id(con, "allocation_runs", "run_id", "R"), src.deal_id, "hypo",
                    f"{src.key}+proportional_rebalance", "proposed", list(lines.values()),
                    unallocated=round(src.unallocated + max(freed, 0.0), 9), source_run_id=run_id)
    for ln in res.lines:
        ln["proposed_amt"] = ln["final_amt"]
    return _finalise(con, deal, funds, rules, res, holders, escalation or {}, created_by, None, [])


def promote(con, run_id: str, created_by: str, escalation: dict | None = None) -> RunResult:
    """Copy a hypo's amounts into a new LIVE run (re-checked), ready for maker-checker."""
    src = load_run(con, run_id)
    if src.mode != "hypo":
        raise ValueError(f"run {run_id} is already {src.mode}")
    if src.status in ("blocked", "no_eligible_funds"):
        raise ValueError(f"run {run_id} has status {src.status}; nothing to promote")
    rules, deal, funds = _rules(con), _deal(con, src.deal_id), _funds(con)
    res = RunResult(_next_id(con, "allocation_runs", "run_id", "R"), src.deal_id, "live", src.key,
                    "partially_allocated" if src.unallocated > LOT_EPS else "proposed",
                    [dict(ln) for ln in src.lines], unallocated=src.unallocated, source_run_id=run_id)
    return _finalise(con, deal, funds, rules, res, _holdings(con, deal["issuer"]), escalation or {},
                     created_by, None, [])
