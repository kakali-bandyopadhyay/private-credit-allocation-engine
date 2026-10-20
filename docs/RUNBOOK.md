# RUNBOOK – daily allocation operations (illustrative)

Procedure for running the engine as a desk would. Commands assume `--db alloc.db --data data` (defaults).

## 1. Start of day (each region)
| Step | Command | Check |
|---|---|---|
| Verify the audit trail | `python -m alloc audit verify` | "intact"; if broken → stop, escalate to CCO and Technology (do not run allocations) |
| Read the incoming handover | `handover.md` from the previous region | Accept owners; escalate anything OVERDUE |
| Open exceptions | `python -m alloc exceptions --open` | Each has an owner and due time |
| Overdue items | `python -m alloc sql queries/overdue_exceptions.sql` | Escalate per `data/escalation.json` |
| Decisions and reservations | `python -m alloc decisions` | Every reservation due or overdue gets `reservation-renew` (reason, ≤72h) or `reservation-release` (reason) from its owner; nothing is freed automatically |

## 2. Per deal
1. `run --deal <id>` (hypo) and `compare --deal <id>` – review gates, capacity, key and sizing notes.
2. If a co-investment condition fires: `rebalance --run <hypo>` **or** keep the hypo and record the board step at approval. Withdraw hypos not pursued: `withdraw --run <id> --by <you> --reason "..."`.
3. `promote --run <hypo> --by <maker>` → `submit` (approval due within 8h) → a different person runs `approve` (rationale required over High/Critical) or `reject`. A `FUNDING-UNKNOWN` hard stop cannot be approved: Treasury confirms funding (`set-funding ... --reason`) or the checker rejects the run.
4. `book --run <id>` → `reconcile --run <id>`. Any break: investigate, then `resolve-break --break <id> --by <you> --explanation "root cause"`.
5. Data problems: never edit data directly – `correct-deal ... --reason "<source>"`.

## 3. End of day
| Step | Command |
|---|---|
| Surveillance | `python -m alloc surveil` |
| Management summary | `python -m alloc exec-summary --out exec_summary.md` |
| Handover to next region | `python -m alloc handover --from <region> --to <region> --out handover.md` |
| MI pack | `python -m alloc export --out allocation_report.xlsx` |

## 4. Periodic
- Fairness review: `python -m alloc fairness` – if the sign-off is NOT VALID (run set, tolerance, policy or figures changed), a reviewer who made none of the runs signs again (`fairness-signoff`, commentary for flagged funds).
- Monthly control attestation: `python -m alloc attest --period YYYY-MM --by <attester>` (13 tests); any "Exception" row needs a remediation owner and date.
- Audit or regulatory request: `python -m alloc evidence --run <id> --out evidence_<id>.md`.

## 5. Business continuity
- The database is one file: back it up at end of day with the handover. Restore = copy file + `audit verify`.
- If a region is unavailable, the receiving region works from the last handover and `exceptions --open`; nothing depends on a named individual.
- Inputs are fingerprinted at load (`DATA_LOADED` audit entry), so a reload from the wrong files is detectable.

## 6. Escalation matrix (illustrative, `data/escalation.json`)
| Severity | Owner | SLA |
|---|---|---|
| Low | Analyst | 24h |
| Medium | VP | 8h |
| High | VP + CCO | 4h |
| Critical | CCO / Board | 1h |
