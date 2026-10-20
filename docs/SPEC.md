# Allocation Engine – Specification v0.7.1

Private-credit investment allocation prototype: hypos → eligibility → pro-rata → constraints → exceptions → maker-checker → booking → reconciliation → surveillance, with audit evidence.

**Status:** v0.7.1, 20 Oct 2026 (B07.1 control fixes: funding-unknown hard stop that approval cannot clear; fairness sign-off bound to run set, tolerance, policy versions and report hash; reservation aging with renew/release decisions; approval SLA – ADR 0007). v0.7 (B07: funding-aware BDC headroom – cash/debt/mixed, unknown → conservative + escalation, booking blocked; reservations of capital, issuer exposure and BDC headroom for live unbooked runs; pre-booking re-check; fairness sign-off with segregation and commentary; operator decision pack). v0.6 (B06: pre-trade BDC qualifying-asset headroom cap; allocation fairness report across the deal set). v0.5, 19 Oct 2026 (B05: five mock cases end to end, data correction, hypo withdrawal, break resolution, evidence pack, executive summary, handover, control attestation, README, ADRs, FINDINGS, RUNBOOK). v0.4 added booking, reconciliation, surveillance, KPIs/KRIs and the Excel report. v0.1 approved earlier the same day; v0.2 adds the BCRED 10-K and co-investment application findings (section 4 and section 12).
**Labelling rule:** all data is synthetic. Policies are *illustrative*, modelled on public information (BDC co-investment exemptive-relief conditions, BCRED 10-K disclosures). Public filings are cited only as sources for illustrative rules. Nothing here represents any firm's actual policies, systems, data or views.

---

## 1. Purpose

Show, end to end, how a multi-fund private-credit allocation process can be run with:
- strong controls (maker-checker, audit trail, escalation, attestation)
- clean data (intake checks, reconciliation)
- measurable performance (KPIs/KRIs/SLAs)
- operating-model thinking (handover, evidence on demand)

**Audience:** people who run, review or audit allocation controls – allocations, compliance, operations and technology.

---

## 2. Requirements traceability – every responsibility mapped to a feature or artefact

| # | Responsibility of a typical investment-allocations function | Engine feature / artefact |
|---|---|---|
| R1 | Run hypothetical allocation scenarios (hypos); evaluate outcomes; finalise accurately | **Scenario mode.** `run --mode hypo` and `compare` (the same deal under both sizing keys, side by side), then a `live` run |
| R2 | End-to-end process: intake, eligibility, constraint monitoring, exceptions, approvals, booking, post-allocation review | **Pipeline stages** (section 4), each logged |
| R3 | Policies, guidelines, client/fund restrictions, conflicts protocols, regulatory requirements | **Versioned rule registry**: fund guidelines, restricted list, conflicts / co-investment conditions, illustrative BDC regulatory check |
| R4 | Maker-checker, documented approvals, audit trails, escalation protocols, periodic control attestations | **Controls core**: maker ≠ checker enforced, approval rationale mandatory on overrides, hash-chained audit log, severity-based escalation, `attest` control attestation (13 control tests re-performed from the records) |
| R5 | KRIs, KPIs, service-level metrics, operational trends; remediation | **Metrics module**: cycle time, exception rate, overrides, breaches, reconciliation breaks, SLA %, trend across runs |
| R6 | Periodic allocation surveillance, portfolio reviews, management reporting | **Surveillance job**: re-checks every fund against its guidelines after booking (e.g. concentration drift); **fairness report** (fair share vs actual per fund, with reason codes) across all final allocations; management report |
| R7 | Data quality; reconcile inputs/outputs across source systems, models, downstream records | **DQ checks at intake** and a **three-way reconciliation** (allocation run ↔ booking file ↔ downstream ledger stub), with a breaks register |
| R8 | Senior point of contact across Investments, Tech, Finance, Ops, Data, Legal & Compliance, Capital Formation, Insurance | Out of scope for code (stakeholder RACI belongs in the operating model) |
| R9 | Modernisation, workflow digitisation, automation, standardisation, scalability | The engine itself, plus a *labelled estimate* of manual steps removed (no invented savings figures) |
| R10 | Change impact, business requirements, UAT, post-go-live stabilisation | This spec = requirements; build guides B01–B07 = change log; 80 pytest tests = regression/UAT evidence; limitations in FINDINGS.md |
| R11 | Follow-the-sun handover, procedures, BCP, cross-training | **Handover report**: open exceptions, pending approvals and SLA clocks by region; `docs/RUNBOOK.md` (daily procedure, BCP, escalation) |
| R12 | Build, coach, retain the team | Out of scope for code (operating-model document, not in this repo) |
| R13 | Concise executive-level updates | **Auto-generated one-page executive summary** (status, risks, decisions needed) from the metrics |
| R14 | Ad hoc analysis for Investment / Capital Formation / Insurance | **SQL query pack** for typical asks (allocation by fund and vehicle, issuer exposure, exceptions by rule) |
| R15 | Support audits and regulatory reviews with readily retrievable evidence | **Evidence pack export**: for any allocation ID, one command returns inputs, rule versions, decisions, approvals, bookings and reconciliation result |

---

## 3. Data model (SQLite)

| Table | Key columns | Purpose |
|---|---|---|
| `funds` | fund_id, name, vehicle_type (BDC / drawdown / insurance_SMA / evergreen), available_capital, target_weight, min_ticket, max_issuer_pct, rating_floor, excluded_sectors, nav | Fund and mandate master |
| `deals` | deal_id, issuer, sector, rating, asset_class, instrument (first lien / unitranche / second lien), size, price, expected_close | Opportunities to allocate |
| `rules` | rule_id, version, scope (global / fund), rule_type, params (JSON), effective_from | Versioned policy and guideline registry |
| `restricted_list` | issuer, fund_id (nullable = all), reason, valid_from, valid_to | Client and fund restrictions |
| `holdings` | fund_id, issuer, exposure | Existing positions for concentration checks |
| `allocation_runs` | run_id, deal_id, mode (hypo / live), policy_version, created_by, created_at, status | One row per hypo or live run |
| `allocation_lines` | run_id, fund_id, eligible, ineligible_reason, proposed_amt, final_amt | Per-fund result |
| `exceptions` | exc_id, run_id, fund_id, rule_id, severity, status, owner, opened_at, resolved_at, resolution | Exception queue |
| `approvals` | run_id, maker, checker, decision, rationale, ts | Maker-checker record |
| `bookings` | run_id, fund_id, amount, booked_at | Booking file |
| `ledger_stub` | fund_id, deal_id, amount, posted_at | Simulated downstream record, for reconciliation |
| `audit_log` | seq, ts, actor, action, entity, payload (JSON), prev_hash, hash | Append-only, hash-chained (tamper-evident) |

---

## 4. Pipeline – four separate decisions

Source findings (19 Oct): the BCRED FY2025 10-K describes a **multi-factor, good-faith allocation**, not a universal pro-rata split. The 2025 co-investment application sets specific **conditions** (same terms; an existing-investment condition with an approximate-proportion exception). So eligibility, sizing, co-investment checks and escalation are kept as **separate decisions**.

1. **Intake and DQ:** schema validation (pydantic), completeness, valid IDs, duplicates. A missing rating blocks the deal from allocation (owner data_ops).
2. **Eligibility – hard gates only:** mandate (asset class), permitted instruments, rating floor, sector exclusions, restricted list (per fund, date-valid). Failure excludes the fund, with coded reasons.
3. **Sizing – multi-factor, documented:** capacity = min(available capital, concentration headroom under the issuer cap). Capital and concentration **reduce a share; they are not eligibility gates**. A documented allocation key, recorded on every run, splits the deal: `capacity` (default) or `target_weight` (hypo variant). Shares above capacity are capped and the excess re-split; shares below the minimum ticket are dropped and re-split; amounts are rounded to a 0.5 lot. Any shortfall is reported as unallocated.
4. **Co-investment conditions (illustrative):**
   - *Same terms:* all participants on the same instrument and price, with settlement within 10 business days. A violation is a **Critical** exception.
   - *Existing investment:* if an affiliate already holds the issuer, each participating **regulated fund (BDC)** needs the board step **unless** it already holds the issuer **and** it and every affiliate holder participate in approximate proportion (±10%) to existing holdings. This is an issuer-level proxy for "same security". Not met = **Critical** exception, owner CCO / Board.
5. **Escalation:** each exception gets a severity, an owner and an SLA from `data/escalation.json`. A run with a Critical exception is `pending_escalation`.
6. **Maker-checker** (B03): only LIVE runs can be submitted, and only by their maker. The checker must be a different user (enforced in code and in the schema). Approving with open High/Critical exceptions needs a rationale, which is recorded as their resolution; a rejection needs a rationale. A hypo can be **rebalanced** (holders brought into proportion with existing holdings) and **promoted** to live; superseded exceptions are closed with lineage. Every step goes to a **hash-chained, append-only audit log** (DB triggers block edits; `audit verify` detects tampering).
7. **Booking** (B04): approved live runs only; booking file plus ledger stub (test-only injectable break, logged); holdings, available capital and BDC balance sheet updated; double booking refused.
8. **Post-allocation review** (B04): three-way reconciliation (approved ↔ booked ↔ ledger, plus control total) with a breaks register and High exceptions to Operations; surveillance, including the BDC **70% qualifying-assets** test and the **150% asset-coverage** check as portfolio-level regulatory monitoring (not per-deal allocation rules); metrics.

## 5. Outputs

- **Excel workbook** (openpyxl): Allocation, Hypo comparison, Exceptions, Reconciliation, KPI/KRI dashboard, Audit evidence.
- **Executive summary** (markdown, one page).
- **Handover report**, **monthly attestation report**, **evidence pack** (per allocation ID).
- **SQL query pack** (`queries/*.sql`).

---

## 6. Synthetic dataset (seeded, reproducible)

- **6 funds:** 2 BDC, 2 drawdown, 1 insurance SMA, 1 evergreen, each with different guidelines.
- **12 deals** (13 from B07, with D013 added): mixed sectors, ratings and instruments.
- **Holdings:** set so that concentration limits can be hit.
- **Restricted list:** 1 issuer restricted for the insurance mandate only.

---

## 7. Mock cases (run as scenarios; documented in FINDINGS.md)

Implemented in `reports.demo` (`python -m alloc demo`); results in FINDINGS.md.
1. **D003 – concentration + existing-investment condition:** two keys compared, proportional rebalance, target-weight hypo withdrawn, self-approval refused, test ledger break detected and resolved.
2. **D009 – restricted issuer** for the insurance SMA only.
3. **D007 – missing rating:** blocked, corrected with a sourced reason, then allocated, approved, booked.
4. **D008 – non-qualifying asset into a BDC:** surveillance breach on the 70% test.
5. **D010 – follow-the-sun:** submitted run and open exceptions carried in the handover.

---

## 8. Tech

Python 3.11+, pydantic, sqlite3, openpyxl, pytest (pandas dropped in v0.5 – not needed). CLI:

```
python -m alloc run --deal D001 --mode hypo
python -m alloc evidence --run R0001
```

No LLM in the core. **Stretch (only if everything else meets the bar):** an LLM drafts exception summaries for the checker, with human approval.

---

## 9. Definition of done (gate before the repo goes public)

- [x] Runs end to end from one command on a clean checkout; README steps verified
- [x] pytest passes, covering the allocation maths and every rule type; no hard-coded results
- [x] Data labelled synthetic; policies labelled illustrative; no claim to be any firm's policy
- [x] Every allocation traceable; audit chain verifies; maker ≠ checker enforced
- [x] Reconciliation: zero unexplained breaks (injected breaks are detected and explained)
- [x] Diagram, architecture decision record (ADR), FINDINGS.md with numbers from actual runs
- [x] No secrets, no client or NDA material; full git routine done; history rewritten and every commit scanned for application-specific terms
- [x] Independent review passed

---

## 10. Build sequence

| Build | Scope |
|---|---|
| B01 | Data model, synthetic data, intake + DQ |
| B02 | Eligibility, documented sizing, co-investment conditions, exceptions, tests |
| B03 | Maker-checker, hash-chained audit log, escalation |
| B04 | Reconciliation, surveillance, KPIs/KRIs, Excel output |
| B05 | Mock cases, exec summary, handover / attestation / evidence pack, README, diagram, ADRs, FINDINGS |
| B06 | Pre-trade BDC qualifying-asset cap; fairness report |
| B07 | Funding-aware headroom, reservations, fairness sign-off, decision pack |
| B07.1 | Control fixes after review: funding hard stop, sign-off bound to report content, reservation aging, approval SLA |

## 11. Out of scope

Any firm's real data or policies, pricing/valuation, trade execution, a UI, and live system integrations.

## 12. Decisions

1. **Sizing (revised in v0.2):** no universal pro-rata rule. Sizing is capacity-constrained with a documented allocation key; `capacity` is the default and `target_weight` a hypo variant. The key and policy version are stored on every run.
2. **Dataset size:** 6 funds (all four vehicle types) and 12 deals (13 from B07).
3. **Repo:** standalone `private-credit-allocation-engine`.
4. **Regulated funds:** the BDCs (F1, F2) are treated as regulated funds for the existing-investment condition. The other vehicles are affiliates.
5. **Escalation owners:** Low = analyst; Medium = Allocations VP; High = VP + CCO; Critical = CCO, plus the Board where required. DQ exceptions are owned by data_ops.
