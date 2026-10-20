-- Open exceptions past their SLA due time (escalation trigger).
SELECT exc_id, run_id, rule_id, severity, owner, opened_at, due_at
FROM exceptions
WHERE status = 'Open' AND due_at IS NOT NULL AND due_at < strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
ORDER BY due_at;
