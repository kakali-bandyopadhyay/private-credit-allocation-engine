# ADR 0007 – Hard stops, sign-offs bound to content, and reservation aging

**Status:** Accepted (20 Oct 2026) · **Builds:** B07.1

## Context
A review of B07 found three ways a control could look closed while it was not:
1. **Unknown funding** raised an exception only when the deal was non-qualifying or the cap bound, yet booking blocks *every* BDC line on unknown funding. A qualifying deal could reach "approved" with nothing visible, then fail at booking. And an approval rationale resolved the exception, which recorded it as closed while the underlying fact (funding) was still unknown.
2. **Fairness sign-off** was treated as current when the run IDs matched. The same runs at a different tolerance, under a different policy version, or with corrected figures would still show as signed off.
3. **Reservations** had no owner or end date, so an abandoned run could hold capital and headroom indefinitely, and nothing said who should decide.

## Decision
1. **Hard stops.** `FUNDING-UNKNOWN` (High, Compliance) is raised for every BDC line with an amount and unknown funding, on every path (run, rebalance, promote). An approval is **refused** while it is open; it closes only through `set-funding` (funding confirmed, with a reason) or when the run is **rejected** or its reservation **released**. The attestation re-tests both: no hard stop closed any other way, and no BDC line booked on unknown funding.
2. **Sign-off bound to content.** A sign-off stores the run set, tolerance, policy versions and a SHA-256 of the canonical report (those three plus every per-fund and per-deal figure, sorted-key JSON). It is valid only while all four match the report as it stands; the decision pack and the attestation say which changed. The review tolerance is policy (`CTRL-REVIEW`), not a default in code.
3. **Reservation aging.** Every live run gets a review-by date (created + `reservation_review_hours`). The decision pack lists every reservation with owner (the maker), amount, age and review-by, and raises a "renew or release" decision when it is due within `reservation_warn_hours` or overdue. **Nothing is released automatically**: past the date it stays held and is escalated. `reservation-renew` (reason; at most the policy period ahead) and `reservation-release` (reason; for an approved run, someone other than the maker) are audited and recorded in `reservation_decisions`.
4. **Approval SLA.** Submitting a run sets `approval_due_at` (`approval_sla_hours`); the decision pack shows it and the attestation tests for overdue approvals.

## Consequences
- A control that blocks an action is visible before the action is attempted, and cannot be "approved away".
- Any change to what was reviewed – including a silent correction – invalidates the sign-off, at the cost of more re-reviews.
- Capital is never freed without a named decision; the trade-off is that stale runs keep holding capacity until someone acts, which is why they are escalated.
- Parameters are illustrative policy values in `data/rules.json`.
