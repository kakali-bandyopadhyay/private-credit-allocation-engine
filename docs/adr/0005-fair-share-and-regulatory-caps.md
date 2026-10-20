# ADR 0005 – Fair share is the documented key; regulatory limits cap amounts, not weights

**Status:** Accepted (20 Oct 2026) · **Builds:** B06

## Context
B05 found that a non-qualifying deal could push a BDC below the 70% qualifying-assets minimum, caught only after booking. B06 adds a pre-trade cap. That raises two questions an allocations function must answer explicitly: how is a fund's *fair share* defined, and when a legal or regulatory limit binds, does the fund's share shrink, or does the fund simply take less while its entitlement is unchanged?

## Decision
1. **Fair share** = deal size × the fund's weight under the run's documented allocation key ÷ the sum of weights of the funds that passed every eligibility gate. Default key: capacity = min(available capital, issuer-concentration headroom). Ineligible funds have a fair share of 0.
2. **Regulatory headroom is a cap on the amount, not an input to the weight.** The BDC 70% pre-trade check (illustrative) limits capacity to qualifying ÷ 0.70 − total, rounded down to the lot; the fund's weight is unchanged. The excess is re-split by the normal sizing rules.
3. **Drift** (actual − fair share) is reported per deal and per fund with reason codes from the sizing notes (caps, regulatory headroom, minimum tickets, rebalancing, lot rounding). Total drift = − unallocated.

## Consequences
- The cost of a constraint to a fund's investors is visible as a reasoned drift (F2 −2.57 on D008) rather than absorbed into a smaller "fair" number.
- The report is reproducible from stored run data (weights, eligibility, final amounts, sizing notes) – no recomputation from live data.
- Concentration headroom *is* part of the capacity weight under the default key (a fund that cannot take more of an issuer is not entitled to more). That is a policy choice, documented in the SIZE-KEY rule and comparable with the target-weight key.
- The 70% check is illustrative and simplified (book values, applied at acquisition, one deal at a time); it is not legal advice or a production compliance determination.
