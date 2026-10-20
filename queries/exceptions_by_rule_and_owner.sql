-- Exception volumes by rule, severity and owner, split open vs resolved (KRI view).
SELECT rule_id, severity, owner,
       SUM(CASE WHEN status = 'Open' THEN 1 ELSE 0 END)     AS open_count,
       SUM(CASE WHEN status = 'Resolved' THEN 1 ELSE 0 END) AS resolved_count
FROM exceptions
GROUP BY rule_id, severity, owner
ORDER BY CASE severity WHEN 'Critical' THEN 1 WHEN 'High' THEN 2 WHEN 'Medium' THEN 3 ELSE 4 END, rule_id;
