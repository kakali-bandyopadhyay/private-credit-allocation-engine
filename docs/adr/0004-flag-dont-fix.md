# ADR 0004 – Flag, don't silently fix; offer remediation as a documented option

**Status:** Accepted · **Builds:** B01–B05

## Context
Automation can quietly "correct" problems – drop a bad record, re-size a deal into compliance, adjust a ledger. That hides judgement calls from the people accountable for them and breaks the audit trail.

## Decision
- **Data:** structurally broken records are rejected; business gaps (e.g. missing rating) are loaded, flagged as an owned exception and blocked. A correction needs a reason and is logged with before/after (`correct-deal`).
- **Allocation:** a co-investment condition breach raises a Critical exception; amounts are unchanged. The maker chooses between documented options – a proportional rebalance (new hypo with lineage) or approval with a rationale recording the board step.
- **Hypos not pursued** are withdrawn with a reason; their exceptions become *Superseded*, not deleted.
- **Breaks:** resolved only with a root cause; an adjusting entry is posted and the fund re-checked before the break closes.

## Consequences
Every number and every status change has a person, a reason and a timestamp. The cost is more steps for the user, which is the point: those steps are where accountability sits.
