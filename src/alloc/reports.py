"""Data correction, evidence pack, executive summary, handover report and the end-to-end demo.

Evidence pack  – everything an auditor needs for one allocation, retrievable with one command.
Exec summary   – one page on performance, risks, issues and decisions needed.
Handover       – open items, owners and SLA clocks passed between regional teams.
Attestation    – periodic re-performance of key control tests from the records.
Fairness       – fair share vs actual per fund across the whole deal set, with reviewer sign-off.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from . import audit, ops, workflow
from .allocate import load_run, promote, rebalance_proportional, run_allocation
from .ratings import RATING_SCALE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- data correction (DQ remediation)
def correct_deal_rating(con: sqlite3.Connection, deal_id: str, rating: str, by: str, reason: str) -> list[str]:
    """Fix a missing/incorrect rating with a reason; resolve open DQ exceptions for that deal; audit before/after."""
    if rating not in RATING_SCALE:
        raise ValueError(f"rating '{rating}' not on rating scale")
    if not (reason and reason.strip()):
        raise ValueError("a data correction needs a reason (source of the corrected value)")
    row = con.execute("SELECT rating FROM deals WHERE deal_id=?", (deal_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown deal {deal_id}")
    before = row[0]
    con.execute("UPDATE deals SET rating=? WHERE deal_id=?", (rating, deal_id))
    open_dq = [r[0] for r in con.execute(
        "SELECT e.exc_id FROM exceptions e LEFT JOIN allocation_runs r ON r.run_id=e.run_id "
        "WHERE e.status='Open' AND e.rule_id='DQ-MISSING_RATING' AND (e.run_id IS NULL OR r.deal_id=?)", (deal_id,))]
    # intake-level DQ exceptions have no run; only resolve them if they are for this deal (detail/entity match)
    open_dq = [e for e in open_dq if _dq_is_for(con, e, deal_id)]
    for exc_id in open_dq:
        con.execute("UPDATE exceptions SET status='Resolved', resolved_at=?, resolution=? WHERE exc_id=?",
                    (_now(), f"rating corrected {before} -> {rating} by {by}: {reason}", exc_id))
    con.execute("UPDATE allocation_runs SET status='superseded' WHERE deal_id=? AND status='blocked'", (deal_id,))
    audit.append(con, by, "DATA_CORRECTED", deal_id, {"field": "rating", "before": before, "after": rating,
                                                       "reason": reason, "resolved_exceptions": open_dq})
    con.commit()
    return open_dq


def _dq_is_for(con, exc_id: str, deal_id: str) -> bool:
    run_id, detail = con.execute("SELECT run_id, detail FROM exceptions WHERE exc_id=?", (exc_id,)).fetchone()
    return bool(run_id) or (detail or "").startswith(f"{deal_id}:")   # run-level, or intake exception for this deal


# ---------------------------------------------------------------- evidence pack
def evidence_pack(con: sqlite3.Connection, run_id: str) -> str:
    """Everything an auditor needs for one allocation, in one markdown document."""
    run = load_run(con, run_id)
    lineage, cur = [run_id], run.source_run_id
    while cur:
        lineage.append(cur)
        cur = load_run(con, cur).source_run_id
    lineage.reverse()
    deal = dict(zip(["deal_id", "issuer", "sector", "rating", "asset_class", "instrument", "size", "price",
                     "expected_close", "qualifying"],
                    con.execute("SELECT * FROM deals WHERE deal_id=?", (run.deal_id,)).fetchone()))
    pv = con.execute("SELECT policy_version, created_by, created_at FROM allocation_runs WHERE run_id=?", (run_id,)).fetchone()
    ok, bad, n = audit.verify(con)
    L = [f"# Evidence pack – allocation run {run_id}", "",
         f"Generated {_now()} · synthetic data, illustrative policies", "",
         "## 1. Deal", "", "| Field | Value |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in deal.items()]
    L += ["", "## 2. Run and lineage", "",
          f"- Run: **{run_id}** · mode **{run.mode}** · status **{run.status}** · key `{run.key}` · maker {pv[1]} · created {pv[2]}",
          f"- Lineage: {' → '.join(lineage)}",
          f"- Policy version applied: `{pv[0]}`", "",
          "## 3. Eligibility and sizing per fund", "",
          "| Fund | Eligible | Reasons (if not) | Capacity | Final (USD m) | Sizing notes |", "|---|---|---|---|---|---|"]
    for ln in run.lines:
        cap = "" if ln["capacity"] is None else format(ln["capacity"], "g")
        elig = "yes" if ln["eligible"] else "no"
        L.append(f"| {ln['fund_id']} | {elig} | {ln['ineligible_reason'] or ''} | {cap} | "
                 f"{ln['final_amt']:g} | {', '.join(ln['sizing_notes'])} |")
    L += [f"", f"Total allocated {sum(ln['final_amt'] for ln in run.lines):g} + unallocated {run.unallocated:g} = deal size {deal['size']:g}"]
    pre = []
    for (payload,) in con.execute("SELECT payload FROM audit_log WHERE action='RUN_CREATED' AND entity=?", (run_id,)):
        pre = json.loads(payload).get("pretrade_checks", [])
    if pre:
        L += ["", "**Pre-trade BDC regulatory headroom (funding- and reservation-aware; illustrative control under stated "
                  "assumptions – not legal advice)**", "",
              "| Fund | Funding (basis) | Scenario applied | Headroom reserved by | Start qualifying / coverage | "
              "Debt case permitted | Cash case permitted | Permitted | Allocated | Qualifying after | Coverage after | Binding | Passes |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for c in pre:
            cov = lambda v: "–" if v is None else f"{v:.1%}"
            L.append(f"| {c['fund_id']} | {c['funding']['source']} ({c['funding']['basis']}) | {c['applied_scenario']} | "
                     f"{', '.join(r['run_id'] for r in c['reservations']) or '–'} | {c['start_ratio']:.2%} / {cov(c['start_coverage'])} | "
                     f"{c['scenarios']['debt']['permitted']} | {c['scenarios']['cash']['permitted']} | {c['permitted']} | "
                     f"{c['allocated']:g} | {c['ratio_after']:.3%} | {cov(c['coverage_after'])} | "
                     f"{'yes' if c['binding'] else 'no'} | {'yes' if c['passes'] else 'NO'} |")
    L += ["", "## 4. Exceptions (this run and its lineage)", "",
          "| Exception | Run | Fund | Rule | Severity | Status | Owner | Resolution |", "|---|---|---|---|---|---|---|---|"]
    q = f"SELECT exc_id, run_id, fund_id, rule_id, severity, status, owner, resolution FROM exceptions WHERE run_id IN ({','.join('?'*len(lineage))}) ORDER BY exc_id"
    for r in con.execute(q, lineage):
        L.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    L += ["", "## 5. Approvals", "", "| Maker | Checker | Decision | Rationale | Time |", "|---|---|---|---|---|"]
    for r in con.execute("SELECT maker, checker, decision, rationale, ts FROM approvals WHERE run_id=?", (run_id,)):
        L.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    L += ["", "## 6. Booking and reconciliation", "", "| Fund | Booked | Ledger |", "|---|---|---|"]
    ledger = dict(con.execute("SELECT fund_id, SUM(amount) FROM ledger_stub WHERE deal_id=? GROUP BY fund_id", (run.deal_id,)))
    for fid, amt in con.execute("SELECT fund_id, amount FROM bookings WHERE run_id=? ORDER BY fund_id", (run_id,)):
        L.append(f"| {fid} | {amt:g} | {ledger.get(fid, 'MISSING')} |")
    breaks = con.execute("SELECT break_id, fund_id, check_name, expected, actual, diff, status, resolution "
                         "FROM recon_breaks WHERE run_id=?", (run_id,)).fetchall()
    L += ["", f"Reconciliation breaks: **{len(breaks)}**"] + [
        f"- {b[0]} {b[2]} fund {b[1]}: expected {b[3]}, actual {b[4]}, diff {b[5]} – **{b[6]}**"
        + (f": {b[7]}" if b[7] else "") for b in breaks]
    entities = lineage + [run.deal_id]
    L += ["", "## 7. Audit trail (deal, this run and its lineage)", "", "| # | Time | Actor | Action | Entity |", "|---|---|---|---|---|"]
    for seq, ts, actor, action, entity in con.execute(
            f"SELECT seq, ts, actor, action, entity FROM audit_log WHERE entity IN ({','.join('?'*len(entities))}) ORDER BY seq", entities):
        L.append(f"| {seq} | {ts} | {actor} | {action} | {entity} |")
    L += ["", f"**Audit chain verification:** {'intact' if ok else f'BROKEN at entry {bad}'} ({n} entries checked)", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- executive summary
def exec_summary(con: sqlite3.Connection) -> str:
    m = ops.metrics(con)
    k, r = m["KPI"], m["KRI"]
    crit = con.execute("SELECT exc_id, rule_id, fund_id, owner, detail FROM exceptions WHERE status='Open' "
                       "AND severity IN ('Critical','High') ORDER BY CASE severity WHEN 'Critical' THEN 0 ELSE 1 END, exc_id").fetchall()
    cap = con.execute("SELECT fund_id, vehicle_type, available_capital FROM funds ORDER BY fund_id").fetchall()
    rag = "RED" if crit and r["open_recon_breaks"] else "AMBER" if crit or r["open_recon_breaks"] else "GREEN"
    L = [f"# Investment Allocations – executive update ({_now()[:10]})", "",
         f"**Status: {rag}** · synthetic data, illustrative policies", "",
         "**Performance**",
         f"- {k['live_runs_approved_or_later']} live allocations approved or later; runs by status {k['runs_by_status']}",
         f"- Unallocated on live runs: USD {k['unallocated_usd_m_on_live_runs']:g}m; exceptions resolved/superseded: {k['exceptions_resolved']}", "",
         "**Risks and issues**",
         f"- Open High/Critical exceptions: {len(crit)}; overdue: {r['overdue_open_exceptions']}",
         f"- Open reconciliation breaks: {r['open_recon_breaks']}; surveillance breaches: {r['surveillance_breaches_latest']}",
         f"- Approvals needing override rationale: {r['approvals_with_override_rationale']}; audit chain intact: {r['audit_chain_ok']}", "",
         "**Decisions / actions needed**"]
    L += [f"- {e[0]} {e[1]} {e[2] or ''} – owner {e[3]}: {e[4]}" for e in crit] or ["- None"]
    L += ["", "**Capacity (available capital, USD m)**", ""] + [f"- {c[0]} ({c[1]}): {c[2]:g}" for c in cap]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- handover
def handover(con: sqlite3.Connection, from_region: str, to_region: str) -> str:
    now = _now()
    open_exc = con.execute("SELECT exc_id, run_id, rule_id, severity, owner, due_at FROM exceptions WHERE status='Open' "
                           "ORDER BY due_at IS NULL, due_at").fetchall()
    pending = con.execute("SELECT run_id, deal_id, status, created_by FROM allocation_runs "
                          "WHERE status IN ('submitted','pending_escalation') AND mode='live' ORDER BY run_id").fetchall()
    unbooked = con.execute("SELECT run_id, deal_id FROM allocation_runs WHERE mode='live' AND status='approved'").fetchall()
    L = [f"# Handover {from_region} → {to_region} ({now})", "",
         f"## Open exceptions ({len(open_exc)})", "", "| Exception | Run | Rule | Severity | Owner | Due | SLA |", "|---|---|---|---|---|---|---|"]
    for e in open_exc:
        sla = "no SLA" if not e[5] else ("**OVERDUE**" if e[5] < now else "on time")
        L.append("| " + " | ".join("" if v is None else str(v) for v in e) + f" | {sla} |")
    L += ["", f"## Live runs awaiting a decision ({len(pending)})", ""] + \
         [f"- {p[0]} {p[1]} – {p[2]} (maker {p[3]})" for p in pending] + \
         ["", f"## Approved, not yet booked ({len(unbooked)})", ""] + [f"- {u[0]} {u[1]}" for u in unbooked] + \
         ["", "Receiving lead confirms: [ ] read  [ ] owners accepted  [ ] overdue items escalated", ""]
    audit.append(con, "system", "HANDOVER_GENERATED", f"{from_region}->{to_region}",
                 {"open_exceptions": len(open_exc), "pending_runs": len(pending), "unbooked": len(unbooked)})
    con.commit()
    return "\n".join(L)


# ---------------------------------------------------------------- allocation fairness (B06)
FINAL_STATUSES = ("approved", "booked", "reconciled")
DRIFT_REASONS = ("CAPACITY_SET_BY_QUALIFYING_ASSETS_HEADROOM", "CAPACITY_SET_BY_ASSET_COVERAGE_HEADROOM",
                 "CAPACITY_SET_BY_CASH_ON_HAND", "HEADROOM_REDUCED_BY_PENDING_RUNS", "CAPITAL_RESERVED_BY_PENDING_RUNS", "CAPACITY_SET_BY_CONCENTRATION_LIMIT", "CAPPED_AT_CAPACITY",
                 "SHARE_BELOW_MIN_TICKET", "CAPACITY_BELOW_MIN_TICKET", "NO_CAPACITY",
                 "PROPORTIONAL_REBALANCE", "RECEIVED_REBALANCE_EXCESS")
FAIRNESS_METHOD = (
    "Fair share = deal size x the fund's allocation-key weight / sum of key weights of the funds that passed every "
    "eligibility gate for that deal (the run's documented key: capacity by default = min(available capital, "
    "issuer-concentration headroom)). It is the key split before caps, minimum tickets, lot rounding, pre-trade "
    "regulatory caps and rebalancing. A fund that fails a gate has a fair share of 0 and is reported as excluded with "
    "its reason codes. Drift = actual - fair share. Deal set = live runs approved, booked or reconciled. "
    "Review threshold: |drift| > tolerance x fair share across the set (illustrative KRI).")


def fairness(con: sqlite3.Connection, tolerance: float = 0.05) -> dict:
    """Per-deal and per-fund fair share vs actual allocation across the final (approved or later) live runs."""
    from .allocate import LOT_EPS
    lot = json.loads(con.execute("SELECT params FROM rules WHERE rule_id='SIZE-LOT' ORDER BY version DESC").fetchone()[0])["lot"]
    deals, per_fund = [], {}
    for run_id, deal_id, key, unalloc in con.execute(
            "SELECT run_id, deal_id, allocation_key, unallocated FROM allocation_runs WHERE mode='live' "
            f"AND status IN ({','.join('?' * len(FINAL_STATUSES))}) ORDER BY run_id", FINAL_STATUSES).fetchall():
        size = con.execute("SELECT size FROM deals WHERE deal_id=?", (deal_id,)).fetchone()[0]
        lines = load_run(con, run_id).lines
        wsum = sum(ln["weight"] or 0 for ln in lines if ln["eligible"])
        for ln in lines:
            fid = ln["fund_id"]
            agg = per_fund.setdefault(fid, {"fund_id": fid, "deals_eligible": 0, "deals_excluded": 0, "fair": 0.0,
                                            "actual": 0.0, "exclusions": {}, "reasons": {}, "max_abs_deal_drift": 0.0})
            if not ln["eligible"]:
                agg["deals_excluded"] += 1
                for code in (ln["ineligible_reason"] or "").split(";"):
                    agg["exclusions"][code] = agg["exclusions"].get(code, 0) + 1
                deals.append({"run_id": run_id, "deal_id": deal_id, "key": key, "fund_id": fid, "fair": 0.0,
                              "actual": 0.0, "drift": 0.0, "reasons": "EXCLUDED: " + (ln["ineligible_reason"] or "")})
                continue
            fair = round(size * (ln["weight"] or 0) / wsum, 6) if wsum else 0.0
            actual = ln["final_amt"]
            drift = round(actual - fair, 6)
            codes = [n for n in ln["sizing_notes"] if n in DRIFT_REASONS]
            if "CAPACITY_SET_BY_CONCENTRATION_LIMIT" in codes and "CAPPED_AT_CAPACITY" not in codes:
                codes.remove("CAPACITY_SET_BY_CONCENTRATION_LIMIT")      # limit set capacity but did not bind here
            if not codes and abs(drift) > LOT_EPS:
                codes = ["LOT_ROUNDING"] if abs(drift) < lot else (["RECEIVED_RESPLIT_FROM_CAPPED_FUNDS"] if drift > 0
                                                                     else ["DEAL_UNDERSUBSCRIBED"] if unalloc > LOT_EPS
                                                                     else ["UNEXPLAINED"])
            for c in codes:
                agg["reasons"][c] = agg["reasons"].get(c, 0) + 1
            agg["deals_eligible"] += 1
            agg["fair"] += fair
            agg["actual"] += actual
            agg["max_abs_deal_drift"] = max(agg["max_abs_deal_drift"], abs(drift))
            deals.append({"run_id": run_id, "deal_id": deal_id, "key": key, "fund_id": fid, "fair": fair,
                          "actual": actual, "drift": drift, "reasons": ", ".join(codes)})
    funds = []
    for fid in sorted(per_fund):
        a = per_fund[fid]
        a["fair"], a["actual"] = round(a["fair"], 4), round(a["actual"], 4)
        a["drift"] = round(a["actual"] - a["fair"], 4)
        a["drift_pct"] = round(a["drift"] / a["fair"], 6) if a["fair"] else 0.0
        a["flag"] = "REVIEW" if abs(a["drift_pct"]) > tolerance else "OK"
        funds.append(a)
    total_fair, total_actual = round(sum(f["fair"] for f in funds), 4), round(sum(f["actual"] for f in funds), 4)
    run_ids = sorted({d["run_id"] for d in deals})
    pv = sorted({r[0] for r in con.execute(f"SELECT policy_version FROM allocation_runs WHERE run_id IN "
                                           f"({','.join('?' * len(run_ids))})", run_ids)}) if run_ids else []
    return {"method": FAIRNESS_METHOD, "tolerance": tolerance, "deals": deals, "funds": funds,
            "run_ids": run_ids, "policy_versions": pv,
            "total": {"fair": total_fair, "actual": total_actual, "drift": round(total_actual - total_fair, 4),
                      "runs": len({d["run_id"] for d in deals})}}


def fairness_report(con: sqlite3.Connection, tolerance: float | None = None) -> str:
    tolerance = review_tolerance(con) if tolerance is None else tolerance
    fr = fairness(con, tolerance)
    L = [f"# Allocation fairness – final live allocations ({_now()[:10]})", "",
         "Synthetic data, illustrative policies. Personal prototype, not a production compliance report.", "",
         "## Method", "", fr["method"], "",
         f"**Deal set:** {', '.join(fr['run_ids'])}  ·  **Policy versions in the set:** {len(fr['policy_versions'])}"
         + "".join(f"\n- `{v}`" for v in fr["policy_versions"]), "",
         f"## By fund (across {fr['total']['runs']} deals)", "",
         "| Fund | Deals eligible | Deals excluded (reasons) | Fair share | Actual | Drift | Drift % | Max single-deal drift | Drift reasons | Flag |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for f in fr["funds"]:
        ex = ", ".join(f"{k} x{v}" for k, v in sorted(f["exclusions"].items())) or "–"
        rs = ", ".join(f"{k} x{v}" for k, v in sorted(f["reasons"].items())) or "–"
        L.append(f"| {f['fund_id']} | {f['deals_eligible']} | {f['deals_excluded']} ({ex}) | {f['fair']:.2f} | {f['actual']:.2f} | "
                 f"{f['drift']:+.2f} | {f['drift_pct']:+.2%} | {f['max_abs_deal_drift']:.2f} | {rs} | {f['flag']} |")
    t = fr["total"]
    L += [f"| **Total** | | | {t['fair']:.2f} | {t['actual']:.2f} | {t['drift']:+.2f} | | | | |", "",
          "Total drift equals minus the amount left unallocated (every dollar of fair share is either allocated or reported).", "",
          "## By deal and fund", "", "| Run | Deal | Key | Fund | Fair share | Actual | Drift | Reason codes |", "|---|---|---|---|---|---|---|---|"]
    for d in fr["deals"]:
        L.append(f"| {d['run_id']} | {d['deal_id']} | {d['key']} | {d['fund_id']} | {d['fair']:.2f} | {d['actual']:.2f} | {d['drift']:+.2f} | {d['reasons']} |")
    so = con.execute("SELECT signoff_id, ts, reviewer, tolerance, flagged, commentary FROM fairness_signoffs "
                     "ORDER BY ts DESC, signoff_id DESC LIMIT 1").fetchone()
    st = signoff_status(con, tolerance)
    L += ["", "## Review sign-off", "",
          f"Report fingerprint: tolerance {st['current']['tolerance']:.2%}, {len(st['current']['run_ids'])} runs, "
          f"{len(st['current']['policy_versions'])} policy version(s), SHA-256 `{st['current']['report_hash'][:16]}…`", ""]
    if so:
        L += [f"Last sign-off **{so[0]}** by {so[2]} at {so[1]} (tolerance {so[3]:.2%}); flagged {json.loads(so[4]) or 'none'}."]
        L += [f"- {k}: {v}" for k, v in json.loads(so[5]).items()]
        L += ["", "**Status: VALID for this report.**" if st["fresh"] else
              f"**Status: NOT VALID for this report – {'; '.join(st['reasons'])}. A new sign-off is needed.**"]
    else:
        L += ["Not yet signed off. `python -m alloc fairness-signoff --by <reviewer> [--comment F2=\"...\"]`"]
    return "\n".join(L) + "\n"


def review_tolerance(con) -> float:
    """Fairness review tolerance from policy (CTRL-REVIEW), default 5%."""
    from .allocate import _rules
    return _rules(con).get("CTRL-REVIEW", {}).get("params", {}).get("fairness_tolerance", 0.05)


def fairness_fingerprint(fr: dict, tolerance: float) -> dict:
    """What a sign-off is bound to: run set, tolerance, policy versions and a SHA-256 of the canonical report
    (all four plus every per-fund and per-deal figure, JSON with sorted keys and fixed separators)."""
    import hashlib
    canon = json.dumps({"run_ids": fr["run_ids"], "tolerance": round(float(tolerance), 6),
                        "policy_versions": fr["policy_versions"], "funds": fr["funds"], "deals": fr["deals"]},
                       sort_keys=True, separators=(",", ":"), default=str)
    return {"run_ids": fr["run_ids"], "tolerance": round(float(tolerance), 6),
            "policy_versions": fr["policy_versions"], "report_hash": hashlib.sha256(canon.encode()).hexdigest()}


def signoff_status(con: sqlite3.Connection, tolerance: float | None = None) -> dict:
    """Is the latest sign-off still valid for the report as it stands now? Any change to the run set, tolerance,
    policy versions or report content means a new sign-off is needed; the reasons are listed."""
    tol = review_tolerance(con) if tolerance is None else tolerance
    fr = fairness(con, tol)
    cur = fairness_fingerprint(fr, tol)
    last = con.execute("SELECT signoff_id, reviewer, ts, run_ids, tolerance, policy_versions, report_hash "
                       "FROM fairness_signoffs ORDER BY ts DESC, signoff_id DESC LIMIT 1").fetchone()
    if not fr["run_ids"]:
        return {"fresh": True, "reasons": [], "last": None, "current": cur, "fr": fr}
    if last is None:
        return {"fresh": False, "reasons": ["never signed off"], "last": None, "current": cur, "fr": fr}
    reasons = []
    if json.loads(last[3]) != cur["run_ids"]:
        added = sorted(set(cur["run_ids"]) - set(json.loads(last[3])))
        gone = sorted(set(json.loads(last[3])) - set(cur["run_ids"]))
        reasons.append("run set changed" + (f" (+{', '.join(added)})" if added else "") + (f" (-{', '.join(gone)})" if gone else ""))
    if abs(last[4] - cur["tolerance"]) > 1e-9:
        reasons.append(f"tolerance changed ({last[4]:.2%} signed, {cur['tolerance']:.2%} now)")
    if json.loads(last[5]) != cur["policy_versions"]:
        reasons.append("policy versions changed")
    if last[6] != cur["report_hash"]:
        reasons.append(f"report content changed (hash {last[6][:12]} signed, {cur['report_hash'][:12]} now)")
    return {"fresh": not reasons, "reasons": reasons, "current": cur, "fr": fr,
            "last": {"signoff_id": last[0], "reviewer": last[1], "ts": last[2], "report_hash": last[6]}}


def fairness_signoff(con: sqlite3.Connection, reviewer: str, tolerance: float | None = None,
                     comments: dict | None = None) -> dict:
    """Reviewer sign-off: segregated from the makers of the runs; every flagged fund needs written commentary."""
    tolerance = review_tolerance(con) if tolerance is None else tolerance
    comments = {k: v.strip() for k, v in (comments or {}).items() if v and v.strip()}
    fr = fairness(con, tolerance)
    if not fr["run_ids"]:
        raise ValueError("no final allocations to review")
    makers = {r[0] for r in con.execute(f"SELECT created_by FROM allocation_runs WHERE run_id IN "
                                        f"({','.join('?' * len(fr['run_ids']))})", fr["run_ids"])}
    if reviewer in makers:
        raise ValueError(f"segregation of duties: {reviewer} created runs in this deal set and cannot sign off fairness")
    flagged = [f["fund_id"] for f in fr["funds"] if f["flag"] == "REVIEW"]
    missing = [f for f in flagged if f not in comments]
    if missing:
        raise ValueError(f"commentary required for funds over tolerance: {missing}")
    fp = fairness_fingerprint(fr, tolerance)
    rhash = fp["report_hash"]
    sid = f"FS{con.execute('SELECT COUNT(*) FROM fairness_signoffs').fetchone()[0] + 1:03d}"
    con.execute("INSERT INTO fairness_signoffs VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, _now(), reviewer, fp["tolerance"], json.dumps(fr["run_ids"]), json.dumps(fr["policy_versions"]),
                 json.dumps(flagged), json.dumps(comments), rhash))
    audit.append(con, reviewer, "FAIRNESS_SIGNED_OFF", sid, {"tolerance": tolerance, "runs": fr["run_ids"],
                                                              "flagged": flagged, "commentary": comments,
                                                              "report_hash": rhash})
    con.commit()
    return {"signoff_id": sid, "flagged": flagged, "report_hash": rhash}


# ---------------------------------------------------------------- operator decision pack
def decision_pack(con: sqlite3.Connection, tolerance: float | None = None, as_of: str | None = None) -> str:
    """One page: every decision waiting, who owns it, by when, and where the evidence is.
    `as_of` previews the pack at a later time (e.g. to see reservations that will need renewing)."""
    from . import workflow
    now = as_of or _now()
    items = []
    for exc_id, run_id, fund, rule, sev, owner, due, detail in con.execute(
            "SELECT exc_id, run_id, fund_id, rule_id, severity, owner, due_at, detail FROM exceptions "
            "WHERE status='Open' ORDER BY due_at IS NULL, due_at"):
        action = {"FUNDING-UNKNOWN": "HARD STOP – confirm funding (set-funding) or reject / release the run; "
                                     "an approval rationale cannot clear it",
                  "SIZE-UNDERSUBSCRIBED": "Accept partial allocation or find capacity"}.get(
            rule, "Resolve, or approve with rationale" if run_id else "Investigate and remediate")
        items.append((due, sev, f"{exc_id} {rule}{' ' + fund if fund else ''}: {action}", owner or "–",
                      f"evidence --run {run_id}" if run_id else "surveillance / exceptions", detail or ""))
    for run_id, deal_id, maker, sub_at, appr_due in con.execute(
            "SELECT run_id, deal_id, created_by, submitted_at, approval_due_at FROM allocation_runs "
            "WHERE mode='live' AND status='submitted' ORDER BY run_id"):
        hard = [r[0] for r in con.execute("SELECT exc_id FROM exceptions WHERE run_id=? AND status='Open' "
                                          "AND rule_id IN ('FUNDING-UNKNOWN')", (run_id,))]
        items.append((appr_due, "–", f"{run_id} {deal_id}: approve or reject", f"Checker (not {maker})",
                      f"evidence --run {run_id}",
                      f"submitted {sub_at or '–'}; approval SLA due {appr_due or '–'}"
                      + (f"; cannot be approved until {', '.join(hard)} is closed" if hard else "")))
    for run_id, deal_id in con.execute("SELECT run_id, deal_id FROM allocation_runs WHERE mode='live' "
                                       "AND status='approved' ORDER BY run_id"):
        items.append((None, "–", f"{run_id} {deal_id}: book", "Operations", f"evidence --run {run_id}",
                      "approved, not yet booked (holds capital and BDC headroom until booked)"))
    aging = workflow.reservation_aging(con, now)
    for r in aging:
        if r["state"] in ("OVERDUE", "DUE SOON", "NO REVIEW DATE"):
            items.append((r["review_by"], "–", f"{r['run_id']} {r['deal_id']}: renew or release reservation "
                          f"({r['amount']:g} held, age {r['age_hours']:g}h)", f"{r['owner']} (reservation owner)",
                          f"reservation-renew / reservation-release --run {r['run_id']}",
                          "not released automatically – still holds capital and headroom until a named decision"))
    so = signoff_status(con, tolerance)
    if not so["fresh"]:
        flagged = [f["fund_id"] for f in so["fr"]["funds"] if f["flag"] == "REVIEW"]
        last = so["last"]
        items.append((None, "–", "Fairness report: new sign-off needed" + (f" (commentary needed: {', '.join(flagged)})" if flagged else ""),
                      "Compliance reviewer (not a maker)", "fairness",
                      "; ".join(so["reasons"]) + (f" since {last['signoff_id']}" if last else "")))
    items.sort(key=lambda i: (i[0] is None, i[0] or ""))
    L = [f"# Decision pack ({now[:16]} UTC{', preview' if as_of else ''})", "", "Synthetic data, illustrative policies.", "",
         f"**{len(items)} decisions waiting.**", "",
         "| # | Decision needed | Owner | Due | Status | Evidence (python -m alloc ...) | Context |", "|---|---|---|---|---|---|---|"]
    for n, (due, sev, what, owner, ev, ctx) in enumerate(items, 1):
        st = "–" if not due else ("**OVERDUE**" if due < now else "on time")
        L.append(f"| {n} | {what} | {owner} | {due or '–'} | {st} | `{ev}` | {ctx} |")
    L += ["", f"## Reservations held ({len(aging)})", "",
          "Live, unbooked runs hold capital, issuer exposure and BDC headroom until booked, rejected or released. "
          "Each has an owner and a review-by date; past that date it stays held and is escalated here – never freed silently.", ""]
    if aging:
        L += ["| Run | Deal | Status | Owner | Amount held | Age (h) | Review by | State |", "|---|---|---|---|---|---|---|---|"]
        L += [f"| {r['run_id']} | {r['deal_id']} | {r['status']} | {r['owner']} | {r['amount']:g} | {r['age_hours']:g} | "
              f"{r['review_by'] or '–'} | {'**' + r['state'] + '**' if r['state'] != 'on time' else r['state']} |" for r in aging]
    else:
        L += ["None."]
    fs = "valid for the current report" if so["fresh"] and so["last"] else ("no final deals" if so["fresh"] else "; ".join(so["reasons"]))
    L += ["", f"**Fairness sign-off:** {fs}" + (f" (last {so['last']['signoff_id']} by {so['last']['reviewer']}, "
                                                 f"hash {so['last']['report_hash'][:12]})" if so["last"] else "")]
    audit.append(con, "system", "DECISION_PACK_GENERATED", "desk", {"items": len(items), "as_of": as_of})
    con.commit()
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- control attestation
def attestation(con: sqlite3.Connection, period: str, attested_by: str, tolerance: float | None = None) -> str:
    """Periodic control attestation: each key control re-tested from the records, with the evidence count.
    The attester signs the result; any failed test is listed rather than hidden."""
    from . import bdc
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    ok, bad, n = audit.verify(con)
    live_approved = q("SELECT COUNT(*) FROM allocation_runs WHERE mode='live' AND status IN ('approved','booked','reconciled')")
    tests = [
        ("Maker-checker: no approval by the run's maker",
         q("SELECT COUNT(*) FROM approvals a JOIN allocation_runs r ON r.run_id=a.run_id WHERE a.checker=r.created_by"),
         q("SELECT COUNT(*) FROM approvals")),
        ("Every approved live run has an approval record",
         q("SELECT COUNT(*) FROM allocation_runs r WHERE r.mode='live' AND r.status IN ('approved','booked','reconciled') "
           "AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.run_id=r.run_id AND a.decision='approve')"), live_approved),
        ("High/Critical exceptions closed only with a written resolution",
         q("SELECT COUNT(*) FROM exceptions WHERE severity IN ('High','Critical') AND status<>'Open' "
           "AND (resolution IS NULL OR resolution='')"),
         q("SELECT COUNT(*) FROM exceptions WHERE severity IN ('High','Critical') AND status<>'Open'")),
        ("Only approved live runs booked",
         q("SELECT COUNT(DISTINCT b.run_id) FROM bookings b JOIN allocation_runs r ON r.run_id=b.run_id "
           "WHERE r.mode<>'live' OR NOT EXISTS (SELECT 1 FROM approvals a WHERE a.run_id=r.run_id AND a.decision='approve')"),
         q("SELECT COUNT(DISTINCT run_id) FROM bookings")),
        ("Every booked run reconciled",
         q("SELECT COUNT(*) FROM allocation_runs WHERE status='booked'"),
         q("SELECT COUNT(DISTINCT run_id) FROM bookings")),
        ("Reconciliation breaks resolved with root cause",
         q("SELECT COUNT(*) FROM recon_breaks WHERE status<>'Resolved' OR resolution IS NULL"),
         q("SELECT COUNT(*) FROM recon_breaks")),
        ("No open exception past its SLA",
         q("SELECT COUNT(*) FROM exceptions WHERE status='Open' AND due_at IS NOT NULL AND due_at < ?", _now()),
         q("SELECT COUNT(*) FROM exceptions WHERE status='Open'")),
        ("Audit chain intact (append-only, hash-verified)", 0 if ok else 1, n),
    ]
    so = signoff_status(con, tolerance)
    tests.append(("Fairness sign-off valid for the current report (run set, tolerance, policy versions, report hash; "
                  "reviewer not a maker)", 0 if so["fresh"] else 1, len(so["current"]["run_ids"])))
    tests.append(("Funding-unknown hard stops closed only by funding confirmation, rejection or release",
                  q("SELECT COUNT(*) FROM exceptions WHERE rule_id='FUNDING-UNKNOWN' AND status<>'Open' AND status<>'Superseded' "
                    "AND resolution NOT LIKE 'funding confirmed%' AND resolution NOT LIKE 'run rejected%' "
                    "AND resolution NOT LIKE 'reservation released%'"),
                  q("SELECT COUNT(*) FROM exceptions WHERE rule_id='FUNDING-UNKNOWN' AND status<>'Open'")))
    tests.append(("No booked BDC line on unknown funding",
                  sum(1 for rid, fid, did in con.execute(
                      "SELECT b.run_id, b.fund_id, r.deal_id FROM bookings b JOIN allocation_runs r ON r.run_id=b.run_id "
                      "JOIN fund_portfolio p ON p.fund_id=b.fund_id WHERE b.amount>0").fetchall()
                      if bdc.resolve_funding(con, did, fid)["source"] == "unknown"),
                  q("SELECT COUNT(*) FROM bookings b JOIN fund_portfolio p ON p.fund_id=b.fund_id WHERE b.amount>0")))
    tests.append(("No submitted run past its approval SLA",
                  q("SELECT COUNT(*) FROM allocation_runs WHERE status='submitted' AND approval_due_at < ?", _now()),
                  q("SELECT COUNT(*) FROM allocation_runs WHERE status='submitted'")))
    tests.append(("No reservation past its review-by date without a renew / release decision",
                  q("SELECT COUNT(*) FROM allocation_runs WHERE mode='live' AND status IN "
                    "('proposed','partially_allocated','pending_escalation','submitted','approved') "
                    "AND (review_by IS NULL OR review_by < ?)", _now()),
                  q("SELECT COUNT(*) FROM allocation_runs WHERE mode='live' AND status IN "
                    "('proposed','partially_allocated','pending_escalation','submitted','approved')")))
    failed = [t for t in tests if t[1]]
    L = [f"# Control attestation – {period}", "",
         f"Generated {_now()} · synthetic data, illustrative policies", "",
         "| # | Control test | Population | Exceptions found | Result |", "|---|---|---|---|---|"]
    L += [f"| {i} | {name} | {pop} | {exc} | {'Effective' if not exc else '**Exception**'} |"
          for i, (name, exc, pop) in enumerate(tests, 1)]
    L += ["", f"**Overall: {'all controls effective' if not failed else f'{len(failed)} control(s) with exceptions – remediation required'}**",
          "", f"Attested by: {attested_by}  ·  Date: {_now()[:10]}  ·  Signature: ______________", ""]
    audit.append(con, attested_by, "ATTESTATION_GENERATED", period,
                 {"tests": len(tests), "failed": [t[0] for t in failed]})
    con.commit()
    return "\n".join(L)


# ---------------------------------------------------------------- demo: all mock cases end to end
def demo(con: sqlite3.Connection, escalation: dict) -> dict:
    """Reproducible end-to-end scenarios used for docs/FINDINGS.md (fresh database expected)."""
    out = {}
    # Case 1 – D003: concentration headroom + existing-investment condition -> compare keys, rebalance, approve, book with a
    # test ledger break, reconcile
    h_cap = run_allocation(con, "D003", key="capacity", escalation=escalation)
    h_tw = run_allocation(con, "D003", key="target_weight", escalation=escalation)
    rb = rebalance_proportional(con, h_cap.run_id, escalation=escalation)
    withdrawn = workflow.withdraw(con, h_tw.run_id, "maker1",
                                  "Capacity key with proportional rebalance chosen; target-weight variant not pursued")
    live = promote(con, rb.run_id, "maker1", escalation)
    workflow.submit(con, live.run_id, "maker1")
    try:
        workflow.approve(con, live.run_id, "maker1")
        self_approval_blocked = False
    except ValueError:
        self_approval_blocked = True
    workflow.approve(con, live.run_id, "checker1")
    ops.book(con, live.run_id, "ops1", {"F3": -0.5})
    rec = ops.reconcile(con, live.run_id, escalation=escalation)
    fixed = [ops.resolve_break(con, b["break_id"], "ops1",
                               "Downstream ledger posted 231.0 against 231.5 booked (test-injected posting error)")
             for b in rec["breaks"]]
    out["case1"] = {"hypo_capacity": {l["fund_id"]: l["final_amt"] for l in h_cap.lines},
                    "hypo_target_weight": {l["fund_id"]: l["final_amt"] for l in h_tw.lines},
                    "hypo_exceptions": [e["rule_id"] + ":" + str(e["fund_id"]) for e in h_cap.exceptions],
                    "rebalanced": {l["fund_id"]: l["final_amt"] for l in rb.lines},
                    "rebalanced_exceptions": len(rb.exceptions), "target_weight_hypo_withdrawn": h_tw.run_id,
                    "withdrawn_exceptions_superseded": withdrawn, "live_run": live.run_id,
                    "self_approval_blocked": self_approval_blocked,
                    "recon_breaks": [(b["break_id"], b["fund_id"], b["check"], b["diff"]) for b in rec["breaks"]],
                    "breaks_resolved": [(f["break_id"], f["adjustment"]) for f in fixed]}
    # Case 2 – D009: restricted issuer for the insurance mandate only
    r2 = run_allocation(con, "D009", mode="live", escalation=escalation)
    workflow.submit(con, r2.run_id, "maker1")
    workflow.approve(con, r2.run_id, "checker1")
    ops.book(con, r2.run_id, "ops1")
    rec2 = ops.reconcile(con, r2.run_id, escalation=escalation)
    out["case2"] = {"run": r2.run_id, "lines": {l["fund_id"]: (l["final_amt"], l["ineligible_reason"]) for l in r2.lines},
                    "recon_breaks": len(rec2["breaks"])}
    # Case 3 – D007: missing rating -> blocked -> data correction with reason -> allocate
    b = run_allocation(con, "D007", mode="live", escalation=escalation)
    resolved = correct_deal_rating(con, "D007", "B", "data_ops", "Rating confirmed from agency report dated 2026-10-15 (illustrative)")
    r3 = run_allocation(con, "D007", mode="live", escalation=escalation)
    workflow.submit(con, r3.run_id, "maker1")
    workflow.approve(con, r3.run_id, "checker1", None if not r3.exceptions else "Board step obtained (illustrative)")
    status_before_booking = load_run(con, r3.run_id).status
    ops.book(con, r3.run_id, "ops1")
    rec3 = ops.reconcile(con, r3.run_id, escalation=escalation)
    out["case3"] = {"blocked_run": b.run_id, "blocked_status": b.status, "dq_resolved": resolved, "run": r3.run_id,
                    "status_before_booking": status_before_booking,
                    "exceptions_on_corrected_run": [e["rule_id"] + ":" + str(e["fund_id"]) for e in r3.exceptions], "lines": {l["fund_id"]: l["final_amt"] for l in r3.lines},
                    "recon_breaks": len(rec3["breaks"])}
    # Case 4 – D008: non-qualifying asset into BDCs, funding-aware pre-trade check (F1 cash-funded, F2 debt-funded)
    r4 = run_allocation(con, "D008", mode="live", escalation=escalation)
    workflow.submit(con, r4.run_id, "maker1")
    workflow.approve(con, r4.run_id, "checker1", "Board step obtained for F1/F2 (illustrative)")
    # Case 6 – D013: second non-qualifying deal while D008 is approved but not booked -> headroom reservation;
    #          F1 funding unknown -> conservative scenario + High exception to Compliance
    r6 = run_allocation(con, "D013", mode="live", escalation=escalation)
    workflow.submit(con, r6.run_id, "maker1")
    ops.book(con, r4.run_id, "ops1")
    ops.reconcile(con, r4.run_id, escalation=escalation)
    surv = ops.surveil(con, escalation=escalation)
    pick = lambda chk: {"funding": chk["funding"]["source"], "scenario": chk["applied_scenario"],
                        "reserved_by": [r["run_id"] for r in chk["reservations"]],
                        "start_ratio": chk["start_ratio"], "start_coverage": chk["start_coverage"],
                        "debt_permitted": chk["scenarios"]["debt"]["permitted"],
                        "cash_permitted": chk["scenarios"]["cash"]["permitted"],
                        "permitted": chk["permitted"], "allocated": chk["allocated"], "binding": chk["binding"],
                        "ratio_after": chk["ratio_after"], "coverage_after": chk["coverage_after"], "passes": chk["passes"]}
    out["case4"] = {"run": r4.run_id, "lines": {l["fund_id"]: l["final_amt"] for l in r4.lines},
                    "pretrade": {c["fund_id"]: pick(c) for c in r4.pretrade_checks},
                    "breaches": [(s["fund_id"], s["metric"], s["value"]) for s in surv if s["breach"]],
                    "checks": len(surv)}
    out["case6"] = {"run": r6.run_id, "status": load_run(con, r6.run_id).status,
                    "lines": {l["fund_id"]: l["final_amt"] for l in r6.lines},
                    "pretrade": {c["fund_id"]: pick(c) for c in r6.pretrade_checks},
                    "exceptions": [(e["rule_id"], e["fund_id"], e["severity"], e["owner"]) for e in r6.exceptions]}
    # Case 5 – follow-the-sun: a live run submitted in Bengaluru, awaiting a checker when the desk hands over
    r5 = run_allocation(con, "D010", mode="live", escalation=escalation)
    workflow.submit(con, r5.run_id, "maker1")
    out["case5"] = {"run": r5.run_id, "status": load_run(con, r5.run_id).status,
                    "lines": {l["fund_id"]: l["final_amt"] for l in r5.lines}}
    out["fairness"] = {f["fund_id"]: (f["fair"], f["actual"], f["drift"], f["flag"]) for f in fairness(con)["funds"]}
    out["fairness_signoff"] = fairness_signoff(con, "cco1", 0.05)["signoff_id"]
    out["metrics"] = ops.metrics(con)
    out["audit"] = audit.verify(con)
    return out
