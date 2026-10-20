# ADR 0002 – Preventive and detective controls, both evidenced

**Status:** Accepted · **Builds:** B01–B05

## Context
An allocation can go wrong before booking (wrong fund, wrong size, missing approval) or after it (downstream posting differs, portfolio-level limits drift). Controls that only run before booking cannot see the second kind.

## Decision
| Layer | Controls | Where |
|---|---|---|
| Preventive (before booking) | Intake DQ (reject or flag), eligibility gates, sizing within capacity, co-investment conditions, maker-checker, "only approved live runs can be booked", no double booking | `intake.py`, `allocate.py`, `workflow.py`, `ops.book` |
| Detective (after booking) | Three-way reconciliation (approved ↔ booked ↔ ledger, plus control total), surveillance (issuer concentration, BDC 70% qualifying assets, 150% asset coverage), KPIs/KRIs, periodic control attestation | `ops.reconcile`, `ops.surveil`, `ops.metrics`, `reports.attestation` |

Critical controls are enforced twice where possible: maker ≠ checker in code **and** as a database `CHECK` constraint; audit log immutability by trigger **and** hash chain (ADR 0003).
Every exception gets a severity, an owner and an SLA due time from `data/escalation.json`.

## Consequences
- Case 4 shows why both layers matter: in v0.5 the qualifying-asset breach was caught only by surveillance. B06 turned that detective finding into a preventive control (a pre-trade headroom cap in sizing, ADR 0005), and surveillance stays in place to catch later changes.
- The attestation re-tests each control from the records and shows exceptions rather than hiding them.
