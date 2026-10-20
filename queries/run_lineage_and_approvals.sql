-- Lineage of every run (hypo -> rebalance -> live) with its maker-checker decision.
SELECT r.run_id, r.deal_id, r.mode, r.allocation_key, r.source_run_id, r.status,
       r.created_by AS maker, a.checker, a.decision, a.rationale
FROM allocation_runs r
LEFT JOIN approvals a ON a.run_id = r.run_id
ORDER BY r.run_id;
