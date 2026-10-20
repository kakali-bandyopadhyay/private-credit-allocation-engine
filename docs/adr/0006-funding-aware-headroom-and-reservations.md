# ADR 0006 – Funding-aware BDC headroom, reservations for pending runs, and a pre-booking re-check

**Status:** Accepted (20 Oct 2026) · **Builds:** B07

## Context
B06 capped a BDC's allocation to a non-qualifying deal at qualifying ÷ 0.70 − total. That formula silently assumes the purchase is **debt-funded** (total assets grow, qualifying assets unchanged). A review pointed out two gaps:
1. A **cash-funded** purchase lowers qualifying assets instead (cash, assumed to count in the 70% basket, becomes a non-qualifying loan), giving a tighter limit; debt also raises senior securities, so the **150% asset-coverage** test matters.
2. Each run looked only at the booked balance sheet, so two deals **in flight** could each use the same headroom (and the same capital).

## Decision
1. **Funding plan per fund per deal** (`funding_plans`): cash, debt, mixed (cash share) or unknown; else the fund's default; else unknown.
2. For each BDC, compute the limit under the debt and cash cases (and the plan's mix): 70% test (non-qualifying deals), cash on hand, 150% asset coverage. Apply the plan's case; **unknown → the most conservative case** (the minimum is within the limit for any cash/debt mix) and a **High exception to Compliance**.
3. **Reservations:** capital, issuer exposure and BDC balance-sheet headroom committed to live runs that are proposed, pending, submitted or approved (not yet booked) are deducted before sizing another deal. Booking moves the reservation into the balance sheet; rejection or supersession releases it.
4. **Booking controls:** a BDC line cannot be booked on unknown funding (`set-funding` first, with a reason; audited), and a **pre-booking re-check** refuses the booking if the projected ratios fail because the balance sheet moved since sizing.

## Consequences
- The control now encodes a judgement call explicitly: when an input is missing it takes the safe case and makes someone accountable, instead of assuming.
- Results depend on the funding inputs; plans should come from Treasury. The capping still changes amounts, not weights (ADR 0005).
- Illustrative and simplified (book values, cash treated as qualifying, no valuation changes). Classification and calculation basis must be validated against authoritative guidance and the fund's Legal/Compliance interpretation; not legal advice.
