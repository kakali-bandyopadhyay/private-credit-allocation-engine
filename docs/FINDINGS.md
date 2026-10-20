# FINDINGS – mock cases with numbers from actual runs

**Run:** `python -m alloc load --reset` then `python -m alloc demo` on a fresh database (20 Oct 2026, v0.7.0). Every figure below is printed by that command and asserted in `tests/test_reports.py`.
**v0.6 (B06):** the pre-trade BDC qualifying-asset check prevents the case 4 breach that v0.5 caught only after booking; allocation fairness report.
**v0.7 (B07):** the check is **funding-aware** (cash, debt or mixed; unknown → most conservative case + escalation) and **reservation-aware** (capital, issuer exposure and BDC headroom held by live, unbooked runs); fairness sign-off; operator decision pack. New case 6 (D013).
**Units:** USD millions. **Data:** synthetic. **Policies:** illustrative.

The cases run in sequence on one database, so each later case sees the bookings of the earlier ones (holdings, available capital, BDC balance sheets).

---

## Case 1 – D003 Atlas ($400m): concentration, existing-investment condition, ledger break

**Set-up:** F1 already holds $280m of Atlas (headroom $20m), F2 holds $110m (headroom $15m).

| Fund | Capacity | Hypo, capacity key (R0001) | Hypo, target-weight key (R0002) | Rebalanced (R0003 → live R0004) |
|---|---|---|---|---|
| F1 (BDC, holder) | 20 | 11.5 | 20.0 (capped) | **14.0** |
| F2 (BDC, holder) | 15 | 8.5 | 15.0 (capped) | **5.5** |
| F3 | 400 | 231.0 | 182.5 | 231.5 |
| F4 | 90 | 52.0 | 61.0 | 52.0 |
| F5 | 120 | 69.5 | 91.0 | 69.5 |
| F6 | 48 | 27.5 | 30.5 | 27.5 |
| **Total** | | 400 | 400 | 400 |
| F1 : F2 | existing 280 : 110 = **72 : 28** | 57 : 43 | 57 : 43 | **72 : 28** |
| Board-step exceptions | | 2 Critical | 2 Critical | **0** |

**Findings**
1. Under both keys the two BDC holders would participate out of proportion to their existing holdings, so the existing-investment condition requires a board step (2 Critical exceptions, owner CCO / Board).
2. The proportional rebalance moves F1 up and F2 down to 72 : 28 and gives the freed $0.5m to F3, clearing the condition with every dollar still placed. The target-weight hypo was **withdrawn** with a reason; its 2 exceptions became *Superseded*.
3. maker1 trying to approve their own run was **refused**; checker1 approved.
4. A test-injected posting error made the ledger show F3 at 231.0 against 231.5 booked. Reconciliation raised break **B0001** (BOOKED_vs_LEDGER, −0.5, High, Operations). It was resolved with a root cause and a +0.5 adjusting entry, and the fund re-checked and tied out.
5. After booking, F1's Atlas exposure is 294 = **4.90% of NAV** against a 5% limit: sizing kept it inside the limit and surveillance confirms it.

## Case 2 – D009 Crescent ($220m): restricted issuer for one mandate

| Fund | F1 | F2 | F3 | F4 | F5 | F6 | Total |
|---|---|---|---|---|---|---|---|
| Final | 90.5 | 39.5 | 53.5 | 21.5 | **0 – RESTRICTED_ISSUER** | 15.0 | 220 |

**Finding:** the restriction applies to the insurance SMA only. The other five funds take the deal; F2 and F6 are limited by their concentration headroom. Booked and reconciled with zero breaks.

## Case 3 – D007 Bluebell ($110m): missing rating → correction → allocation

| Step | Result |
|---|---|
| Intake | Loaded, High DQ exception E0001 (owner data_ops) |
| Live run R0006 | **Blocked** – no allocation; run-level exception E0007 |
| `correct-deal --rating B` with a sourced reason | E0001 and E0007 resolved; R0006 superseded; `DATA_CORRECTED` logged with before = null, after = B |
| Live run R0007 | F1 43.0 · F2 23.0 · F3 25.5 · F4 10.0 · F5 0 (unitranche and below BB- floor) · F6 8.5 = 110 |
| Approval, booking, reconciliation | Approved by checker1, booked, zero breaks |

**Finding:** the bad record was never dropped or silently fixed. The correction carries a reason and an owner, and the evidence pack for R0007 shows the `DATA_CORRECTED` entry.

## Case 4 – D008 Summit ($300m): non-qualifying asset into a BDC

F5 (insurance SMA) already holds $100m of Summit and the BDCs do not, so F1 and F2 needed the board step (`AFFILIATE_HOLDS_ISSUER_FUND_DOES_NOT`); approved with a recorded rationale (illustrative). Summit is a **non-qualifying** asset for the BDCs' 70% test.

**v0.5 (detective control only):** F2 was allocated $60m. Surveillance after booking found F2 at **69.94%** (minimum 70%) and raised a High exception to Compliance.

**v0.6/v0.7 – pre-trade check:** before sizing, each BDC's capacity is capped at its regulatory headroom **under the fund's funding plan for the deal** (deal plan, else the fund's default, else unknown). Funding plans here: F1 pays cash, F2 draws on its facility (debt).

| Pre-trade check (D008) | F1 – cash-funded | F2 – debt-funded |
|---|---|---|
| Qualifying / total / senior / cash before | 4,647.5 / 6,147.5 / 3,647.5 / 250 | 1,838 / 2,568 / 1,668 / 90 |
| Starting qualifying ratio / asset coverage | 75.60% / 168.5% | **71.57%** / 153.96% |
| Debt case: 70% limit = Q / 0.70 − T; coverage limit = (T − 1.5 S) / 0.5 | 491.79; 1,352.5 → **491.5** | **57.71**; 132 → **57.5** |
| Cash case: 70% limit = Q − 0.70 T; cash on hand | 344.25; **250** → 250 | 40.4; 90 → **40.0** |
| Scenario applied (from the funding plan) | cash → 250 | debt → **57.5** |
| Key share without the cap (fair share) | ~111.7 | ~60.1 |
| Allocated | 113.0 (cap not binding) | **57.5** (cap binding) |
| After the purchase: qualifying / coverage | 73.76% / 168.5% | **70.006%** / 152.2% – passes both |

| Fund | F1 | F2 | F3 | F4 | F5 | F6 | Total |
|---|---|---|---|---|---|---|---|
| v0.5 (no pre-trade check) | 111.5 | 60.0 | 65.5 | 27.0 | 14.5 | 21.5 | 300 |
| **v0.6 / v0.7** | **113.0** | **57.5** | **66.0** | 27.0 | **15.0** | 21.5 | 300 |

**Why 57.5 and not 60 for F2:** at 60, the ratio would be 1,838 / 2,628 = 69.94%. The debt-funded maximum is 57.71; rounding to the lot goes down to 57.5 (rounding up to 58.0 gives 69.99%). The $2.5m excess is re-split (F1 +1.5, F3 +0.5, F5 +0.5).
**Why funding matters:** had F2 paid cash, the purchase lowers qualifying assets instead of raising total assets, and the limit would be **40.0**, not 57.5. If F2's funding were unknown, the engine applies the **most conservative case** (here cash, 40.0) and raises a **High exception to Compliance**; the run cannot be booked until funding is confirmed (`set-funding`). The conservative amount is within the limit for any cash/debt mix. Debt also adds to senior securities, so the 150% asset-coverage test is checked too (132 for F2 – not binding here).
**Design choice:** the regulatory headroom **caps the amount but does not change the fund's weight** (ADR 0005); the shortfall shows as a reasoned drift in the fairness report.
**Assumptions (illustrative, not legal advice or a production compliance determination):** applied at acquisition on a simplified balance sheet at book value; cash is assumed to count in the 70% basket; no valuation changes. Classification and the calculation basis must be validated against authoritative guidance and the fund's Legal/Compliance interpretation. A **pre-booking re-check** refuses to book if the balance sheet has moved since sizing (tested). Surveillance after booking: **0 breaches** (28 checks); the detective control still catches later changes (tested).

## Case 6 – D013 Orion ($120m, non-qualifying): two deals in flight, funding unknown (B07)

D013 is run while D008 is **approved but not yet booked**. D008's allocations are reserved first, so D013 cannot reuse the same room.

| Fund | Reserved by pending D008 | Effect on D013 | Allocated |
|---|---|---|---|
| F1 | 113 capital (cash-funded) | free capital 152.5 − 113 = **39.5**; funding for D013 **unknown** → conservative cash case (137) applied, **High exception to Compliance** | 25.5 |
| F2 | 57.5 of qualifying-asset headroom | pro-forma ratio already 70.006% → permitted **0** | **0** |
| F3 / F4 / F5 / F6 | capital | F4 below its 10 minimum ticket → dropped | 15.0 / 0 / 74.5 / 5.0 = 120 |

**Finding:** without reservations, D013 would have sized F2 at up to 57.5 again against the same headroom, and the two bookings together would have taken F2 below 70%. The reservation is released when a run is booked (the balance sheet then carries it), rejected or superseded (tested). D013 stays **submitted**, waiting for the funding confirmation and a checker.

**B07.1:** the funding exception is a **hard stop** – `approve` is refused while it is open, even with a rationale; it closes only by `set-funding` or by rejecting the run or releasing its reservation. D013 and D010 each hold a reservation with owner maker1, a review-by date 72h after creation and an approval due time 8h after submission.

## Case 5 – D010 Meridian ($75m): follow-the-sun handover

Run R0010 was created and submitted by maker1 in Bengaluru and left for a New York checker. Pending D013 (case 6) still reserves capital.

| Fund | F1 | F2 | F3 | F4 | F5 | F6 | Unallocated |
|---|---|---|---|---|---|---|---|
| Final | 14.0 (39.5 free − 25.5 reserved) | 0 – below B floor | 0 – 8.5 free, below 10 minimum ticket | 0 – below B floor | 0 – instrument, floor | 2.5 (7.5 − 5.0 reserved) | **58.5** |

**Finding:** after the earlier bookings and the capital held by pending D013, the deal is **under-subscribed by $58.5m**, raised as a Medium exception to the Allocations VP rather than hidden. The handover report and the decision pack list both pending runs with owners and SLA status.

## Allocation fairness across the deal set (B06)

`python -m alloc fairness` (also in the Excel report, sheets *Fairness* and *Fairness by deal*, where drift and flags are live formulas).

**Method:** fair share = deal size × the fund's allocation-key weight ÷ sum of key weights of the funds that passed every eligibility gate for that deal (default key: capacity = min(available capital, issuer-concentration headroom)). It is the key split **before** caps, minimum tickets, lot rounding, pre-trade regulatory caps and rebalancing. A fund that fails a gate has a fair share of 0 and is listed as excluded with its reason codes. Drift = actual − fair share. Deal set = the 4 live runs approved or later (D003, D009, D007, D008); the report also lists the policy version(s) in force for the set. Review flag: |drift| > 5% of fair share (illustrative KRI).

| Fund | Deals eligible | Excluded (reasons) | Fair share | Actual | Drift | Drift % | Drift reasons | Flag |
|---|---|---|---|---|---|---|---|---|
| F1 | 4 | 0 | 256.79 | 260.50 | +3.71 | +1.45% | proportional rebalance (D003), re-split from capped F2 (D008), lot rounding | OK |
| F2 | 4 | 0 | 131.39 | 125.50 | **−5.89** | **−4.49%** | proportional rebalance (D003, −3.16), **qualifying-asset cap (D008, −2.57)**, lot rounding | OK |
| F3 | 4 | 0 | 375.07 | 376.50 | +1.43 | +0.38% | received rebalance excess, lot rounding | OK |
| F4 | 4 | 0 | 110.44 | 110.50 | +0.06 | +0.06% | lot rounding | OK |
| F5 | 2 | 2 (restricted issuer; instrument + rating floor) | 83.92 | 84.50 | +0.58 | +0.70% | lot rounding | OK |
| F6 | 4 | 0 | 72.39 | 72.50 | +0.11 | +0.15% | lot rounding | OK |
| **Total** | | | **1,030.00** | **1,030.00** | **0.00** | | | |

**Findings**
1. Every non-zero drift carries a reason code; none is unexplained. Total drift equals minus the unallocated amount (0 here), so the report reconciles to the deals.
2. **F2 is the fund to watch:** −4.49% across the set, driven by two deliberate, documented constraints (the D003 board-step rebalance and the D008 regulatory cap). It is inside the 5% tolerance; at a 2% tolerance it is flagged REVIEW (tested).
3. Funds that gain from re-splits (F1, F3) are the mirror image of F2's drift, which is what a Compliance reviewer would check for systematic favouritism.
4. **Sign-off (B07):** `fairness-signoff` records reviewer, tolerance, deal set, policy versions, a hash of the report and commentary. The reviewer must not have created any run in the set, and every fund over tolerance needs written commentary (at 2%, F2 does). The demo signs off FS001 (cco1, 5%, nothing flagged). **B07.1:** the sign-off is bound to the run set, tolerance, policy versions and a SHA-256 of the canonical report; changing any of them – e.g. reviewing at 2%, or correcting one final amount by 0.5 under the same run IDs – makes it not valid until signed again (tested).

---

## End state (after all six cases, v0.7 + B07.1)

| KPI | Value | KRI | Value |
|---|---|---|---|
| Runs | 10 (4 reconciled, 2 submitted, 3 superseded, 1 withdrawn) | Runs with exceptions | 70% |
| Live runs approved or later | 4 | Open exceptions | 1 High (D013 funding not confirmed), 1 Medium (D010 under-subscribed) |
| Exceptions resolved or superseded | 9 | Overdue exceptions | 0 |
| Unallocated on live runs | $58.5m (D010) | Open reconciliation breaks | 0 |
| | | Surveillance breaches | **0** (v0.5: 1) |
| | | Audit chain | intact (34 entries) |

- **Executive summary:** status **AMBER** – one open High (D013 F1 funding, owner Compliance); no breaks or surveillance breaches.
- **Decision pack (`decisions`):** 4 decisions – the D013 funding hard stop (Compliance), approve/reject D013 and D010 (a checker who is not maker1, due within the 8h approval SLA; D013 cannot be approved until funding is closed), accept or fill D010's shortfall (Allocations VP) – each with owner, due time, SLA status and the evidence command. Plus a reservations table (2 held, owner, age, review-by) and the sign-off status. Previewed 4 days ahead, both reservations become OVERDUE renew-or-release decisions and still hold their capacity.
- **Control attestation (2026-10):** 13 of 13 control tests effective (9: sign-off valid for the current report; 10–11: funding hard stops; 12: approval SLA; 13: reservation review dates).
- Create-to-approval time shows 0.0 minutes because the demo is scripted; in use it is measured from the audit log timestamps.

## Decision trace (v0.7.2)

`python -m alloc trace --run <id>` follows a run from input to evidence: the run-time input snapshot (recorded in the RUN_CREATED audit entry), every eligibility rule with the value it tested, the limit and BDC calculations for both funding cases, the allocation arithmetic, the outcome, reviewer actions, the audit entries with their hashes, and the provenance of every rule used (basis, assumption, public source, who would have to approve it – data/rule_sources.json). Per fund: gates, capacity and what set it, key share, caps with reasons, the BDC pre-trade check, the final amount and outcome (ALLOCATE, CAP, REBALANCE, DROPPED, EXCLUDE, plus ESCALATE where a High or Critical exception is open or was raised), then exceptions across the lineage, approval, booking and the audit chain. The four outcomes are worked through in [DECISION_PATH.md](DECISION_PATH.md): D009 F5 excluded (restricted issuer), D008 F2 capped at 57.5 (debt-funded BDC, 70.006% after), D003 F1/F2 rebalanced to clear the board-step condition, D013 F1 escalated on unknown funding and F2 at zero because D008 reserves its headroom. Tests check each outcome, the snapshot against the input files, that the trace ties out to the deal size, that every stored reason code has a plain-English meaning, and that every rule and exception type has documented provenance.

## Limitations and next steps

| Limitation | Effect | Next step |
|---|---|---|
| Holdings are issuer-level | "Same security" approximated by "same issuer" for the existing-investment condition | Hold positions at instrument level |
| BDC balance sheet simplified | Book values; cash assumed qualifying; no valuation changes | Model the balance sheet properly; Legal/Compliance validation of classification and basis |
| Funding plans are inputs | Wrong plan → wrong scenario | Source plans from Treasury; unknown already escalates, blocks approval and booking |
| No role model | Segregation is by user name (maker ≠ checker, reviewer not a maker, release of an approved run not by its maker) | Role-based entitlements |
| Reservations cover capital, issuer exposure and BDC headroom | Other limits (e.g. sector) not reserved | Extend the same mechanism if those limits are added |
| Fairness benchmark is the documented key | Measures drift from policy, not "fairness" in any absolute sense | Add rotation / time-weighted views if the policy uses them |
| Single currency, no pricing or valuation | Out of scope | – |
| Audit chain anchored only in the database | Tamper-evident, not tamper-proof | Anchor the latest hash in write-once storage |
| CLI only | Fine for a prototype | Workflow UI with role-based access |
