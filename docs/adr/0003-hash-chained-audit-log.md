# ADR 0003 – Hash-chained, append-only audit log

**Status:** Accepted · **Builds:** B03

## Context
Audit and regulatory reviews need evidence that can be trusted without trusting whoever administers the database.

## Decision
Every action appends one entry: timestamp, actor, action, entity, JSON payload, previous entry's hash, and its own SHA-256 hash over all of these.
1. Database triggers refuse `UPDATE` and `DELETE` on `audit_log`.
2. `audit verify` recomputes the chain and reports the first broken entry, so tampering is detected even if the triggers are bypassed.

Actions logged include `DATA_LOADED` (with input file fingerprints), `DATA_CORRECTED` (before/after/reason), `RUN_CREATED`, `HYPO_WITHDRAWN`, `SUBMITTED`, `APPROVED`, `REJECTED`, `BOOKED`, `RECONCILED`, `BREAK_RESOLVED`, `SURVEILLANCE_RUN`, `HANDOVER_GENERATED`, `ATTESTATION_GENERATED`.

## Consequences
- The evidence pack, KPIs (e.g. create-to-approval time) and the attestation are generated from the same trail an auditor would use.
- Tamper-evident, not tamper-proof: a production system would also anchor the latest hash externally (e.g. write-once storage).
