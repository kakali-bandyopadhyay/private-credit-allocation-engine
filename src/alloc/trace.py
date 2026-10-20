"""Decision trace: follow one allocation from input through rule and calculation to outcome, reviewer action and audit record.

For each fund: the run-time inputs (deal and fund terms, holdings, reservations), every eligibility rule with the value it
tested, the limit calculations (issuer limit, BDC pre-trade check), the allocation arithmetic, and the outcome; then the
exceptions with owner and SLA, the reviewer actions, booking and reconciliation, the audit entries with their hashes, and
the provenance of every rule used (public source or illustrative choice, the assumption, and who would have to approve it).

Read-only. Built from the stored run, the run-time input snapshot in its RUN_CREATED audit entry, and the records written
after it – the same records as the evidence pack. Personal prototype; synthetic data; illustrative policies. The BDC
checks are a model under stated assumptions, not legal advice.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import audit
from .allocate import load_run
from .ratings import meets_floor

CODES = {
    "ASSET_CLASS_NOT_PERMITTED": "asset class outside the fund's mandate",
    "INSTRUMENT_NOT_PERMITTED": "instrument not permitted by the mandate",
    "BELOW_RATING_FLOOR": "deal rating below the fund's rating floor",
    "SECTOR_EXCLUDED": "sector excluded by the fund's guidelines",
    "RESTRICTED_ISSUER": "issuer on the restricted list for this fund",
    "DEAL_BLOCKED_DQ": "deal blocked at intake by a data-quality exception",
    "CAPACITY_SET_BY_CONCENTRATION_LIMIT": "capacity set by the issuer limit",
    "CAPACITY_SET_BY_CASH_ON_HAND": "capacity set by cash on hand",
    "CAPACITY_SET_BY_QUALIFYING_ASSETS_HEADROOM": "capacity set by BDC 70% qualifying-asset headroom",
    "CAPACITY_SET_BY_ASSET_COVERAGE_HEADROOM": "capacity set by BDC 150% asset-coverage headroom",
    "CAPITAL_RESERVED_BY_PENDING_RUNS": "capital already reserved by live, unbooked runs",
    "HEADROOM_REDUCED_BY_PENDING_RUNS": "regulatory headroom already reserved by live, unbooked runs",
    "NO_CAPACITY": "no capacity left, so no allocation",
    "ZERO_WEIGHT": "zero weight under the allocation key",
    "CAPPED_AT_CAPACITY": "key share above capacity: capped, excess re-split to other eligible funds",
    "BDC_REGULATORY_HEADROOM_PRETRADE": "capped by the pre-trade BDC check, rounded down to the lot",
    "SHARE_BELOW_MIN_TICKET": "share below the minimum ticket: dropped and re-split",
    "CAPACITY_BELOW_MIN_TICKET": "capacity below the minimum ticket: dropped",
    "PROPORTIONAL_REBALANCE": "rebalanced into proportion with existing holdings (co-investment condition)",
    "RECEIVED_REBALANCE_EXCESS": "received excess freed by a cap or rebalance",
}
TEST_NAMES = {"qualifying_70": "70% qualifying assets: Q / 0.70 - T",
              "asset_coverage_150": "150% asset coverage (assumes the s.61(a)(2) approval; otherwise 200%): (T - 1.5 S) / 0.5",
              "cash_on_hand": "cash on hand"}


def _explain(code: str) -> str:
    return f"`{code}` – {CODES.get(code, 'see data/rule_sources.json')}"


def _sources(path: str | Path | None) -> dict:
    p = Path(path) if path else Path(__file__).resolve().parents[2] / "data" / "rule_sources.json"
    return json.loads(p.read_text(encoding="utf-8"))["rules"] if p.exists() else {}


def _rules_at(con, policy_version: str) -> dict[str, dict]:
    out = {}
    for item in policy_version.split(";"):
        rid, ver = item.split("@v")
        row = con.execute("SELECT version, scope, rule_type, params FROM rules WHERE rule_id=? AND version=?", (rid, int(ver))).fetchone()
        if row:
            out[rid] = {"version": row[0], "scope": row[1], "type": row[2], "params": json.loads(row[3])}
    return out


def _payload(con, run_id: str) -> dict:
    row = con.execute("SELECT payload FROM audit_log WHERE action='RUN_CREATED' AND entity=?", (run_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def _gate(rule: dict, deal: dict, f: dict) -> tuple[str, bool]:
    p, chk = rule["params"], rule["params"]["check"]
    if chk == "allowed_asset_classes":
        return f"asset class {deal['asset_class']} in {p['allowed']}", deal["asset_class"] in p["allowed"]
    if chk == "allowed_instruments":
        return f"instrument {deal['instrument']} in {p['allowed']}", deal["instrument"] in p["allowed"]
    if chk == "rating_floor":
        return f"rating {deal['rating']} at or above floor {f['rating_floor']}", bool(deal["rating"]) and meets_floor(deal["rating"], f["rating_floor"])
    if chk == "sector_exclusion":
        return f"sector {deal['sector']} not in {f['excluded_sectors'] or '[]'}", deal["sector"] not in (f["excluded_sectors"] or [])
    if chk == "restricted_list":
        return f"issuer {deal['issuer']} not restricted for this fund", not f["restricted"]
    return chk, True


def trace(con: sqlite3.Connection, run_id: str, fund: str | None = None, sources: str | Path | None = None) -> str:
    run = load_run(con, run_id)
    lineage, cur = [run_id], run.source_run_id
    while cur:
        lineage.append(cur)
        cur = load_run(con, cur).source_run_id
    lineage.reverse()
    pv = con.execute("SELECT policy_version FROM allocation_runs WHERE run_id=?", (run_id,)).fetchone()[0]
    rules, src = _rules_at(con, pv), _sources(sources)
    used: set[str] = set()
    snap, snap_from = {}, None
    for rid in reversed(lineage):                      # a rebalanced or promoted run uses its source's input snapshot
        snap = _payload(con, rid).get("inputs") or {}
        if snap:
            snap_from = rid
            break
    pre = {c["fund_id"]: c for c in _payload(con, run_id).get("pretrade_checks", [])}
    if not pre:
        for rid in reversed(lineage):
            pre = {c["fund_id"]: c for c in _payload(con, rid).get("pretrade_checks", [])}
            if pre:
                break
    lines = [ln for ln in run.lines if fund is None or ln["fund_id"] == fund]
    if fund and not lines:
        raise ValueError(f"fund {fund} not in run {run_id}")
    exc_all = con.execute(f"SELECT exc_id, fund_id, rule_id, severity, status, owner, opened_at, due_at, resolved_at, resolution, detail, run_id "
                          f"FROM exceptions WHERE run_id IN ({','.join('?' * len(lineage))}) ORDER BY exc_id", lineage).fetchall()
    deal = (snap or {}).get("deal") or dict(zip(
        ["deal_id", "issuer", "sector", "rating", "asset_class", "instrument", "size", "price", "expected_close", "qualifying"],
        con.execute("SELECT deal_id, issuer, sector, rating, asset_class, instrument, size, price, expected_close, qualifying "
                    "FROM deals WHERE deal_id=?", (run.deal_id,)).fetchone()))
    elig_w = sum(ln["weight"] or 0 for ln in run.lines if ln["eligible"])

    L = [f"# Decision trace – run {run_id}, deal {deal['deal_id']} {deal['issuer']}", "",
         "> Personal prototype. Synthetic data; illustrative policies. Regulatory checks are a model under stated assumptions – "
         "not legal advice and not validated requirements; see *Rules used and their provenance* at the end.", "",
         f"Run: mode **{run.mode}**, status **{run.status}**, allocation key `{run.key}`, lineage {' → '.join(lineage)}.", "",
         "## 1. Deal inputs (data/deals.json, synthetic)", "",
         f"{deal['issuer']} · sector {deal['sector']} · rating **{deal['rating'] or 'MISSING'}** · {deal['asset_class']} · "
         f"{deal['instrument'].replace('_', ' ')} · size **{deal['size']:g}** (USD m) · price {deal['price']} · expected close "
         f"{deal['expected_close']} · **{'qualifying' if deal['qualifying'] else 'non-qualifying'}** asset for the BDC 70% test "
         f"(illustrative classification)", ""]
    if snap_from and snap_from != run_id:
        L += [f"Inputs below are the run-time snapshot recorded when {snap_from} was created (this run was derived from it).", ""]
    L += ["## 2. Per fund: inputs → rules → calculation → outcome", ""]

    for ln in lines:
        fid = ln["fund_id"]
        f = (snap.get("funds") or {}).get(fid)
        fexc = [e for e in exc_all if e[1] == fid]
        esc = [e[0] for e in fexc if e[3] in ("High", "Critical") and not (e[2] or "").startswith("RECON")]
        notes = ln["sizing_notes"]
        outcome = ("EXCLUDE" if not ln["eligible"] else
                   "REBALANCE" if "PROPORTIONAL_REBALANCE" in notes else
                   "CAP" if any(x in notes for x in ("CAPPED_AT_CAPACITY", "BDC_REGULATORY_HEADROOM_PRETRADE")) else
                   "DROPPED" if ln["final_amt"] == 0 else "ALLOCATE")
        L.append(f"### {fid} → {ln['final_amt']:g} · **{outcome}**" + (f" + **ESCALATE** ({', '.join(esc)})" if esc else ""))
        if not f:
            L += ["", "Run-time input snapshot not recorded for this run.", ""]
        else:
            L += ["", f"**Inputs** (data/funds.json, holdings.json, live runs; synthetic): {f['vehicle_type']} · NAV {f['nav']:g} · "
                  f"available capital {f['available_capital']:g}, reserved by live unbooked runs {f['reserved_capital']:g} · issuer limit "
                  f"{f['max_issuer_pct']:.0%} of NAV, already held {f['existing_exposure']:g}, reserved {f['reserved_issuer']:g} · rating floor "
                  f"{f['rating_floor']} · minimum ticket {f['min_ticket']:g}", "", "**Eligibility rules**", ""]
            for rid, r in sorted(rules.items()):
                if r["type"] != "eligibility_gate" or r["scope"] not in ("global", fid):
                    continue
                used.add(rid)
                text, ok = _gate(r, deal, f)
                L.append(f"- `{rid}@v{r['version']}`: {text} → {'pass' if ok else '**FAIL**'}")
        if not ln["eligible"]:
            L += [f"- Result: excluded – " + "; ".join(_explain(c) for c in (ln["ineligible_reason"] or "").split(";") if c), ""]
            continue
        L += ["", "**Limits and calculation**", ""]
        if f:
            room = f["max_issuer_pct"] * f["nav"] - f["existing_exposure"] - f["reserved_issuer"]
            free = f["available_capital"] - f["reserved_capital"]
            used.add("SIZE-CONC-HEADROOM")
            L.append(f"- `SIZE-CONC-HEADROOM`: issuer room = {f['max_issuer_pct']:.0%} × {f['nav']:g} − {f['existing_exposure']:g} − "
                     f"{f['reserved_issuer']:g} = **{room:g}**; free capital = {f['available_capital']:g} − {f['reserved_capital']:g} = **{free:g}**; "
                     f"capacity = min = **{max(0.0, min(room, free)):g}**")
        c = pre.get(fid)
        if c:
            used.add("SIZE-BDC-QUALIFYING-HEADROOM")
            pf = c["pro_forma"]
            L.append(f"- `SIZE-BDC-QUALIFYING-HEADROOM`: pro forma Q {pf['Q']:g}, T {pf['T']:g}, S {pf['S']:g} (after reservations"
                     + (f" by {', '.join(r['run_id'] for r in c['reservations'])}" if c["reservations"] else "") + f"); start ratio "
                     f"{c['start_ratio']:.2%}; funding **{c['funding']['source']}** ({c['funding']['basis']}) → scenario **{c['applied_scenario']}**")
            for name, sc in c["scenarios"].items():
                lim = "; ".join(f"{TEST_NAMES.get(k, k)} = {('no limit' if v is None else format(v, 'g'))}" for k, v in sc["limits"].items())
                L.append(f"  - {name} case: {lim} → permitted (rounded down to the lot) **{sc['permitted']}**")
            L.append(f"  - capacity before {c['capacity_before']:g} → permitted {c['permitted']} → "
                     f"{'**binding**' if c['binding'] else 'not binding'}; ratio after allocation {c['ratio_after']:.3%}; "
                     f"{'passes' if c['passes'] else '**FAILS**'}")
        L.append(f"- Capacity used for sizing: **{ln['capacity']:g}**" + "".join(
            f"; {_explain(x)}" for x in notes if x.startswith("CAPACITY_SET_BY") or x.endswith("BY_PENDING_RUNS")))
        used.update({"SIZE-KEY", "SIZE-MIN-TICKET", "SIZE-LOT"})
        share = deal["size"] * (ln["weight"] or 0) / elig_w if elig_w else 0.0
        L += ["", "**Allocation**", "",
              f"- `SIZE-KEY` ({run.key}): key share = {deal['size']:g} × {ln['weight'] or 0:g} ÷ {elig_w:g} = **{share:.2f}**"]
        L += [f"- {_explain(x)}" for x in notes if not (x.startswith("CAPACITY_SET_BY") or x.endswith("BY_PENDING_RUNS"))]
        L.append(f"- `SIZE-MIN-TICKET` {f['min_ticket']:g} and `SIZE-LOT` 0.5 applied → final **{ln['final_amt']:g}**" if f
                 else f"- final **{ln['final_amt']:g}**")
        for e in fexc:
            used.add("RECON" if (e[2] or "").startswith("RECON") else e[2])
            L.append(f"- Exception **{e[0]}** ({e[11]}): {e[3]} `{e[2]}`, owner {e[5]}, due {e[7] or '–'} – {e[10]}")
        L.append("")

    run_exc = [e for e in exc_all if e[1] is None]
    if run_exc:
        L += ["**Run-level exceptions**", ""] + [f"- **{e[0]}** ({e[11]}): {e[3]} `{e[2]}`, owner {e[5]} – {e[10]}" for e in run_exc] + [""]
        used.update(e[2] for e in run_exc if e[2])

    L += ["## 3. Reviewer actions", ""]
    ap = con.execute("SELECT maker, checker, decision, rationale, ts FROM approvals WHERE run_id=?", (run_id,)).fetchall()
    L += [f"- {a[2].upper()} by **{a[1]}** (maker {a[0]}, segregation enforced) at {a[4]}" + (f" – rationale: “{a[3]}”" if a[3] else "") for a in ap] \
        or [f"- No approval recorded (status {run.status})."]
    for e in exc_all:
        if fund and e[1] not in (fund, None):
            continue
        L.append(f"- {e[0]} {e[3]} `{e[2]}`: **{e[4]}**" + (f" at {e[8]} – {e[9]}" if e[9] else " – open, awaiting its owner"))
    if run.status == "submitted" and any(e[2] == "FUNDING-UNKNOWN" and e[4] == "Open" for e in exc_all):
        L.append("- Hard stop: approval is refused while funding is unknown; only `set-funding`, rejection or release closes it.")
    L.append("")

    L += ["## 4. Booking and reconciliation", ""]
    bk = dict(con.execute("SELECT fund_id, amount FROM bookings WHERE run_id=?", (run_id,)))
    led = dict(con.execute("SELECT fund_id, SUM(amount) FROM ledger_stub WHERE deal_id=? GROUP BY fund_id", (run.deal_id,)))
    br = con.execute("SELECT break_id, fund_id, check_name, expected, actual, status, resolution FROM recon_breaks WHERE run_id=?", (run_id,)).fetchall()
    if bk:
        L += [f"- {x}: approved {next(l['final_amt'] for l in run.lines if l['fund_id'] == x):g} · booked {bk[x]:g} · ledger {led.get(x, 'MISSING')}"
              for x in sorted(bk) if fund is None or x == fund]
        L += [f"- Break {b[0]} {b[2]} {b[1]}: expected {b[3]}, actual {b[4]} – **{b[5]}**" + (f": {b[6]}" if b[6] else "")
              for b in br if fund is None or b[1] == fund] or ["- No reconciliation breaks."]
    else:
        L.append("- Not booked.")
    L.append("")

    ents = lineage + [run.deal_id] + [b[0] for b in br]
    L += ["## 5. Audit record (hash-chained, append-only)", "", "| # | Time | Actor | Action | Entity | Hash |", "|---|---|---|---|---|---|"]
    for seq, ts, actor, action, entity, h in con.execute(
            f"SELECT seq, ts, actor, action, entity, hash FROM audit_log WHERE entity IN ({','.join('?' * len(ents))}) ORDER BY seq", ents):
        L.append(f"| {seq} | {ts} | {actor} | {action} | {entity} | `{h[:12]}…` |")
    ok, bad, n = audit.verify(con)
    L += ["", f"Chain verification: **{'intact' if ok else f'BROKEN at entry {bad}'}** ({n} entries checked). "
              f"`python -m alloc evidence --run {run_id}` gives the full evidence pack.", ""]

    L += ["## 6. Rules used and their provenance", "",
          "| Rule | Basis | Assumption | Sources | Would need approval from |", "|---|---|---|---|---|"]
    for rid in sorted(used):
        ver = f"@v{rules[rid]['version']}" if rid in rules else ""
        s = src.get(f"{rid}{ver}") or src.get(rid)
        if not s:
            L.append(f"| `{rid}{ver}` | not documented | | | |")
            continue
        links = "; ".join(f"[{x['title']}]({x['url']})" for x in s["sources"]) or "–"
        L.append(f"| `{rid}{ver}` | {s['basis']} | {s['assumption']} | {links} | {s['approval_needed']} |")
    return "\n".join(L) + "\n"
