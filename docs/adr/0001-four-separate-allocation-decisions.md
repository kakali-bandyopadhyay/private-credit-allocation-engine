# ADR 0001 – Allocation is four separate decisions, not one pro-rata rule

**Status:** Accepted (19 Oct 2026, spec v0.2) · **Builds:** B02

## Context
The first draft (spec v0.1) allocated every deal pro rata by available capital and treated a board step as required whenever an affiliate held the issuer. Reading a large BDC's FY2025 annual report and the related SEC co-investment exemptive application showed both were too simple:
- allocation is described as **multi-factor and in good faith** (mandate, capital, existing positions and concentration, liquidity, minimum sizes, legal limits), not universal pro rata;
- mandate and legal limits **exclude** a fund; capital and concentration only **reduce** its share;
- the board step for a regulated fund is needed when an affiliate already holds the issuer **unless** the fund already holds it too and every affiliate holder participates in approximate proportion.

## Decision
Model each deal as four decisions with different owners and failure modes:

| Decision | Question | Fails as | Owner |
|---|---|---|---|
| 1. Eligibility gates | Can this fund take it at all? | Fund excluded, every reason code recorded | Allocations |
| 2. Sizing | How much? | Capped, re-split, below minimum ticket, unallocated | Allocations |
| 3. Co-investment conditions | Does it need extra sign-off? | Critical exception | CCO / Board |
| 4. Escalation | Who decides, by when? | Exception with owner and SLA | Per severity |

Sizing uses capacity = min(available capital, concentration headroom) and a **documented allocation key** (`capacity` default, `target_weight` as a hypo variant), stored on every run with the policy version.

## Consequences
- Each allocated dollar is explainable: gate results, capacity, key, sizing notes and policy version are on the run.
- The two keys can be compared side by side (`compare`), so the key is a visible choice, not a hidden default.
- Condition checks **flag**; they never change amounts (see ADR 0004).
- Simplification: holdings are issuer-level, so "same security" is approximated by "same issuer".
