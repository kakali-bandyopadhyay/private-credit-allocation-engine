-- Approved LIVE allocations by fund and vehicle type (what Investments / Capital Formation ask for).
SELECT f.fund_id, f.vehicle_type,
       COUNT(DISTINCT r.deal_id)        AS deals,
       ROUND(SUM(l.final_amt), 1)       AS allocated_usd_m,
       ROUND(f.available_capital, 1)    AS available_capital_usd_m
FROM allocation_lines l
JOIN allocation_runs r ON r.run_id = l.run_id
JOIN funds f           ON f.fund_id = l.fund_id
WHERE r.mode = 'live' AND r.status = 'approved' AND l.final_amt > 0
GROUP BY f.fund_id, f.vehicle_type, f.available_capital
ORDER BY allocated_usd_m DESC;
