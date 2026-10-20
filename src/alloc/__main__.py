"""CLI.

  python -m alloc load      [--reset]
  python -m alloc run       --deal D003 [--mode hypo|live] [--key capacity|target_weight] [--by maker1]
  python -m alloc compare   --deal D003
  python -m alloc rebalance --run R0001 [--by maker1]          (proportional-rebalance hypo)
  python -m alloc promote   --run R0003 --by maker1            (hypo -> live)
  python -m alloc submit    --run R0004 --by maker1
  python -m alloc approve   --run R0004 --by checker1 [--rationale "..."]
  python -m alloc reject    --run R0004 --by checker1 --rationale "..."
  python -m alloc exceptions [--open]
  python -m alloc audit     verify | trail --entity R0004
  python -m alloc sql       queries/allocations_by_fund.sql
  python -m alloc book      --run R0003 --by ops1 [--inject-break F3:-0.5 | F3:missing]
  python -m alloc reconcile --run R0003 --by ops1
  python -m alloc surveil
  python -m alloc metrics
  python -m alloc export    --out allocation_report.xlsx
  python -m alloc withdraw  --run R0002 --by maker1 --reason "..."   (close a hypo not pursued)
  python -m alloc resolve-break --break B0001 --by ops1 --explanation "root cause" [--no-adjustment]
  python -m alloc correct-deal --deal D007 --rating B --by data_ops --reason "source of the corrected value"
  python -m alloc evidence  --run R0003 [--out evidence_R0003.md]
  python -m alloc exec-summary [--out exec_summary.md]
  python -m alloc handover  --from Bengaluru --to "New York" [--out handover.md]
  python -m alloc fairness  [--tolerance 0.05] [--out fairness.md]   (fair share vs actual across final allocations)
  python -m alloc set-funding --deal D013 --fund F1 --source debt [--cash-share 0.4] --by treasury1 --reason "..."
  python -m alloc reservations                               (BDC headroom held by live, unbooked runs; aging)
  python -m alloc reservation-renew   --run R0009 --by maker1 --reason "..." [--hours 48]
  python -m alloc reservation-release --run R0009 --by cco1   --reason "..."  (frees capital and headroom)
  python -m alloc fairness-signoff --by cco1 [--tolerance 0.05] [--comment F2="reason"]
  python -m alloc decisions [--days-ahead 4] [--out decisions.md]   (operator decision pack)
  python -m alloc attest    --period 2026-10 --by cco1 [--out attestation.md]
  python -m alloc demo                                       (all mock cases end to end on a fresh database)
All commands accept --db (default alloc.db) and --data (default data).
"""
import argparse
import json
import os
from pathlib import Path

from . import audit, ops, reports, workflow
from .allocate import promote, rebalance_proportional, run_allocation
from .db import connect
from .intake import load_directory


def _escalation(data_dir: str) -> dict:
    p = Path(data_dir) / "escalation.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _print_run(r) -> None:
    src = f"  source={r.source_run_id}" if r.source_run_id else ""
    print(f"\nRun {r.run_id}  deal={r.deal_id}  mode={r.mode}  key={r.key}  status={r.status}  unallocated={r.unallocated:g}{src}")
    print(f"  {'fund':<5}{'eligible':<10}{'capacity':>10}{'final':>10}  reasons / sizing notes")
    for ln in r.lines:
        cap = "" if ln["capacity"] is None else f"{ln['capacity']:g}"
        why = ln["ineligible_reason"] or ", ".join(ln["sizing_notes"])
        print(f"  {ln['fund_id']:<5}{('yes' if ln['eligible'] else 'no'):<10}{cap:>10}{ln['final_amt']:>10g}  {why}")
    for e in r.exceptions:
        print(f"  EXC {e['exc_id']} {e['severity']:<8} {e['rule_id']:<26} owner={e['owner']}  due={e.get('due_at')}  {e['detail']}")


def _table(cur) -> None:
    cols = [c[0] for c in cur.description]
    rows = cur.fetchall()
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c)) for i, c in enumerate(cols)]
    print("  " + "  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  " + "  ".join(str(v).ljust(w) for v, w in zip(r, widths)))
    print(f"  ({len(rows)} rows)")


def main() -> None:
    p = argparse.ArgumentParser(prog="alloc", description="Private-credit allocation engine (prototype)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, **kw):
        sp = sub.add_parser(name, **kw)
        sp.add_argument("--db", default="alloc.db")
        sp.add_argument("--data", default="data")
        return sp

    add("load").add_argument("--reset", action="store_true", help="delete the database first")
    sp = add("run"); sp.add_argument("--deal", required=True)
    sp.add_argument("--mode", default="hypo", choices=["hypo", "live"])
    sp.add_argument("--key", choices=["capacity", "target_weight"]); sp.add_argument("--by", default="maker1")
    add("compare").add_argument("--deal", required=True)
    for name in ("rebalance", "promote", "submit", "approve", "reject"):
        sp = add(name); sp.add_argument("--run", required=True)
        sp.add_argument("--by", default="maker1" if name in ("rebalance", "promote", "submit") else None,
                        required=name in ("approve", "reject"))
        if name in ("approve", "reject"):
            sp.add_argument("--rationale", required=name == "reject")
    add("exceptions").add_argument("--open", action="store_true")
    sp = add("audit"); sp.add_argument("action", choices=["verify", "trail"]); sp.add_argument("--entity")
    add("sql").add_argument("file")
    sp = add("book"); sp.add_argument("--run", required=True); sp.add_argument("--by", default="ops1")
    sp.add_argument("--inject-break", help="TEST ONLY: F3:-0.5 or F3:missing (simulates a downstream ledger break)")
    sp = add("reconcile"); sp.add_argument("--run", required=True); sp.add_argument("--by", default="ops1")
    add("surveil")
    add("metrics")
    add("export").add_argument("--out", default="allocation_report.xlsx")
    sp = add("withdraw"); sp.add_argument("--run", required=True); sp.add_argument("--by", required=True)
    sp.add_argument("--reason", required=True)
    sp = add("resolve-break"); sp.add_argument("--break", dest="break_id", required=True)
    sp.add_argument("--by", required=True); sp.add_argument("--explanation", required=True)
    sp.add_argument("--no-adjustment", action="store_true")
    sp = add("correct-deal"); sp.add_argument("--deal", required=True); sp.add_argument("--rating", required=True)
    sp.add_argument("--by", required=True); sp.add_argument("--reason", required=True)
    sp = add("evidence"); sp.add_argument("--run", required=True); sp.add_argument("--out")
    add("exec-summary").add_argument("--out")
    sp = add("handover"); sp.add_argument("--from", dest="from_region", required=True)
    sp.add_argument("--to", dest="to_region", required=True); sp.add_argument("--out")
    sp = add("fairness"); sp.add_argument("--tolerance", type=float); sp.add_argument("--out")
    sp = add("set-funding"); sp.add_argument("--deal", required=True); sp.add_argument("--fund", required=True)
    sp.add_argument("--source", required=True, choices=["cash", "debt", "mixed"]); sp.add_argument("--cash-share", type=float)
    sp.add_argument("--by", required=True); sp.add_argument("--reason", required=True)
    add("reservations")
    sp = add("reservation-renew"); sp.add_argument("--run", required=True); sp.add_argument("--by", required=True)
    sp.add_argument("--reason", required=True); sp.add_argument("--hours", type=float)
    sp = add("reservation-release"); sp.add_argument("--run", required=True); sp.add_argument("--by", required=True)
    sp.add_argument("--reason", required=True)
    sp = add("fairness-signoff"); sp.add_argument("--by", required=True)
    sp.add_argument("--tolerance", type=float); sp.add_argument("--comment", action="append", default=[])
    sp = add("decisions"); sp.add_argument("--out"); sp.add_argument("--tolerance", type=float)
    sp.add_argument("--days-ahead", type=float, help="preview the pack this many days from now")
    sp = add("attest"); sp.add_argument("--period", required=True); sp.add_argument("--by", required=True)
    sp.add_argument("--tolerance", type=float); sp.add_argument("--out")
    add("demo")
    a = p.parse_args()

    if a.cmd == "load":
        if a.reset and os.path.exists(a.db):
            os.remove(a.db)
        con = connect(a.db)
        issues = load_directory(con, a.data)
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("funds", "deals", "holdings", "restricted_list", "rules", "exceptions")}
        print("Loaded:", ", ".join(f"{k}={v}" for k, v in counts.items()))
        for i in issues:
            print(f"  DQ {i.severity:<8} {i.entity} {i.entity_id}: {i.code} – {i.message}")
        return

    con, esc = connect(a.db), _escalation(a.data)
    try:
        if a.cmd == "run":
            _print_run(run_allocation(con, a.deal, a.mode, a.key, created_by=a.by, escalation=esc))
        elif a.cmd == "compare":
            runs = [run_allocation(con, a.deal, "hypo", k, escalation=esc) for k in ("capacity", "target_weight")]
            for r in runs:
                _print_run(r)
            print(f"\nHypo comparison for {a.deal} (final amounts):")
            print(f"  {'fund':<5}{'capacity key':>14}{'target-weight key':>20}")
            for l1, l2 in zip(runs[0].lines, runs[1].lines):
                print(f"  {l1['fund_id']:<5}{l1['final_amt']:>14g}{l2['final_amt']:>20g}")
        elif a.cmd == "rebalance":
            _print_run(rebalance_proportional(con, a.run, a.by, esc))
        elif a.cmd == "promote":
            _print_run(promote(con, a.run, a.by, esc))
        elif a.cmd == "submit":
            workflow.submit(con, a.run, a.by); print(f"{a.run} submitted by {a.by}")
        elif a.cmd == "approve":
            resolved = workflow.approve(con, a.run, a.by, a.rationale)
            print(f"{a.run} approved by {a.by}" + (f"; resolved exceptions {resolved}" if resolved else ""))
        elif a.cmd == "reject":
            workflow.reject(con, a.run, a.by, a.rationale); print(f"{a.run} rejected by {a.by}")
        elif a.cmd == "exceptions":
            q = ("SELECT exc_id, run_id, fund_id, rule_id, severity, status, owner, due_at FROM exceptions"
                 + (" WHERE status='Open'" if a.open else "") + " ORDER BY exc_id")
            _table(con.execute(q))
        elif a.cmd == "audit":
            if a.action == "verify":
                ok, bad, n = audit.verify(con)
                print(f"Audit chain OK – {n} entries verified" if ok else f"AUDIT CHAIN BROKEN at entry {bad} ({n} checked)")
            else:
                for seq, ts, actor, action, payload in audit.trail(con, a.entity):
                    print(f"  #{seq} {ts} {actor:<10} {action:<12} {payload}")
        elif a.cmd == "sql":
            _table(con.execute(Path(a.file).read_text(encoding="utf-8")))
        elif a.cmd == "book":
            booked = ops.book(con, a.run, a.by, ops.parse_break(a.inject_break))
            print(f"{a.run} booked by {a.by}: " + ", ".join(f"{k}={v:g}" for k, v in booked.items()))
            if a.inject_break:
                print(f"  TEST INJECTION: ledger posting altered for {a.inject_break}")
        elif a.cmd == "reconcile":
            res = ops.reconcile(con, a.run, a.by, esc)
            print(f"{a.run} reconciled: {res['funds_checked']} funds x 3 checks -> {len(res['breaks'])} break(s)")
            for b in res["breaks"]:
                print(f"  BREAK {b['break_id']} {b['check']:<20} fund={b['fund_id']} expected={b['expected']} "
                      f"actual={b['actual']} diff={b['diff']} -> {b['exc_id']} (High, Operations)")
        elif a.cmd == "surveil":
            for r in ops.surveil(con, escalation=esc):
                flag = "BREACH" if r["breach"] else "ok"
                print(f"  {r['check_id']} {r['fund_id']:<3} {flag:<6} {r['detail']}" + (f" -> {r['exc_id']}" if r["breach"] else ""))
        elif a.cmd == "metrics":
            print(json.dumps(ops.metrics(con), indent=2))
        elif a.cmd == "export":
            print(f"Wrote {a.out}: sheets {ops.export_excel(con, a.out)}")
        elif a.cmd == "withdraw":
            print(f"{a.run} withdrawn; superseded exceptions {workflow.withdraw(con, a.run, a.by, a.reason)}")
        elif a.cmd == "resolve-break":
            print(ops.resolve_break(con, a.break_id, a.by, a.explanation, not a.no_adjustment))
        elif a.cmd == "correct-deal":
            res = reports.correct_deal_rating(con, a.deal, a.rating, a.by, a.reason)
            print(f"{a.deal} rating set to {a.rating} by {a.by}; resolved DQ exceptions {res}")
        elif a.cmd == "set-funding":
            from . import bdc
            res = bdc.set_funding(con, a.deal, a.fund, a.source, a.cash_share, a.by, a.reason)
            print(f"{a.fund} on {a.deal}: funding confirmed as {a.source}; resolved exceptions {res}")
        elif a.cmd == "reservations":
            from . import bdc
            from .allocate import _rules
            st = _rules(con)["SIZE-BDC-QUALIFYING-HEADROOM"]["params"].get("reserve_statuses", [])
            for f in bdc.reservation_report(con, st):
                print(f"{f['fund_id']}: qualifying {f['booked_ratio']:.3%} booked -> {f['pro_forma_ratio']:.3%} pro forma; "
                      f"coverage {f['booked_coverage']:.1%} -> {f['pro_forma_coverage']:.1%}")
                for r in f["reservations"]:
                    print(f"    {r['run_id']} {r['deal_id']} {r['status']:<20} {r['amount']:>8g}  funding {r['funding']}"
                          f"  {'qualifying' if r['qualifying'] else 'NON-qualifying'}")
            from . import workflow as wf
            print("\nReservation aging (all live, unbooked runs):")
            for r in wf.reservation_aging(con):
                print(f"    {r['run_id']} {r['deal_id']} {r['status']:<20} owner {r['owner']:<8} held {r['amount']:>7g}  "
                      f"age {r['age_hours']:>5g}h  review by {r['review_by']}  {r['state']}")
        elif a.cmd == "reservation-renew":
            print(f"{a.run} reservation renewed; review by {workflow.renew_reservation(con, a.run, a.by, a.reason, a.hours)}")
        elif a.cmd == "reservation-release":
            print(f"{a.run} released (capital and headroom freed); closed exceptions "
                  f"{workflow.release_reservation(con, a.run, a.by, a.reason)}")
        elif a.cmd == "fairness-signoff":
            comments = dict(c.split("=", 1) for c in a.comment)
            print(reports.fairness_signoff(con, a.by, a.tolerance, comments))
        elif a.cmd == "decisions":
            as_of = None
            if a.days_ahead:
                from datetime import datetime, timedelta, timezone
                as_of = (datetime.now(timezone.utc) + timedelta(days=a.days_ahead)).isoformat(timespec="seconds")
            text = reports.decision_pack(con, a.tolerance, as_of)
            if a.out:
                Path(a.out).write_text(text, encoding="utf-8"); print(f"Wrote {a.out}")
            else:
                print(text)
        elif a.cmd == "fairness":
            text = reports.fairness_report(con, a.tolerance)
            if a.out:
                Path(a.out).write_text(text, encoding="utf-8"); print(f"Wrote {a.out}")
            else:
                print(text)
        elif a.cmd in ("evidence", "exec-summary", "handover", "attest"):
            text = (reports.evidence_pack(con, a.run) if a.cmd == "evidence" else
                    reports.exec_summary(con) if a.cmd == "exec-summary" else
                    reports.attestation(con, a.period, a.by, a.tolerance) if a.cmd == "attest" else
                    reports.handover(con, a.from_region, a.to_region))
            if a.out:
                Path(a.out).write_text(text, encoding="utf-8")
                print(f"Wrote {a.out}")
            else:
                print(text)
        elif a.cmd == "demo":
            print(json.dumps(reports.demo(con, esc), indent=2, default=str))
    except ValueError as e:
        print(f"REFUSED: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
