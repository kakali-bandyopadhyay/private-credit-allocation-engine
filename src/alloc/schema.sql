-- Data model v0.4 (docs/SPEC.md section 3). Synthetic data only.
CREATE TABLE funds (
  fund_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  vehicle_type TEXT NOT NULL CHECK (vehicle_type IN ('BDC','drawdown','insurance_SMA','evergreen')),
  available_capital REAL NOT NULL CHECK (available_capital >= 0),
  target_weight REAL NOT NULL CHECK (target_weight >= 0),
  min_ticket REAL NOT NULL CHECK (min_ticket >= 0),
  max_issuer_pct REAL NOT NULL CHECK (max_issuer_pct > 0 AND max_issuer_pct <= 1),
  rating_floor TEXT NOT NULL,
  excluded_sectors TEXT NOT NULL DEFAULT '[]',   -- JSON array
  nav REAL NOT NULL CHECK (nav > 0)
);
CREATE TABLE deals (
  deal_id TEXT PRIMARY KEY,
  issuer TEXT NOT NULL,
  sector TEXT NOT NULL,
  rating TEXT,                                   -- nullable: missing rating is a DQ exception
  asset_class TEXT NOT NULL,
  instrument TEXT NOT NULL CHECK (instrument IN ('first_lien','unitranche','second_lien')),
  size REAL NOT NULL CHECK (size > 0),
  price REAL NOT NULL,
  expected_close TEXT NOT NULL,
  qualifying INTEGER NOT NULL DEFAULT 1          -- BDC qualifying asset (illustrative flag)
);
CREATE TABLE rules (
  rule_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  scope TEXT NOT NULL,                           -- 'global' or a fund_id
  rule_type TEXT NOT NULL,
  params TEXT NOT NULL,                          -- JSON
  effective_from TEXT NOT NULL,
  PRIMARY KEY (rule_id, version)
);
CREATE TABLE restricted_list (
  issuer TEXT NOT NULL,
  fund_id TEXT,                                  -- NULL = restricted for all funds
  reason TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT
);
CREATE TABLE holdings (
  fund_id TEXT NOT NULL REFERENCES funds(fund_id),
  issuer TEXT NOT NULL,
  exposure REAL NOT NULL,
  PRIMARY KEY (fund_id, issuer)
);
CREATE TABLE allocation_runs (
  run_id TEXT PRIMARY KEY,
  deal_id TEXT NOT NULL REFERENCES deals(deal_id),
  mode TEXT NOT NULL CHECK (mode IN ('hypo','live')),
  policy_version TEXT NOT NULL,
  allocation_key TEXT,                           -- documented sizing key used for this run
  source_run_id TEXT,                            -- lineage: hypo this run was rebalanced from / promoted from
  unallocated REAL NOT NULL DEFAULT 0,           -- deal amount not placed (undersubscribed / all funds capped)
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,
  submitted_at TEXT,                             -- B07.1: approval SLA runs from here
  approval_due_at TEXT,                          -- submitted_at + CTRL-REVIEW approval_sla_hours
  review_by TEXT                                 -- B07.1: live runs hold capital/headroom until renewed or released by this time
);
CREATE TABLE reservation_decisions (           -- B07.1: explicit renew / release of what a live, unbooked run holds
  decision_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES allocation_runs(run_id),
  decision TEXT NOT NULL CHECK (decision IN ('renew','release')),
  decided_by TEXT NOT NULL,
  reason TEXT NOT NULL,
  ts TEXT NOT NULL,
  previous_review_by TEXT,
  new_review_by TEXT
);
CREATE TABLE allocation_lines (
  run_id TEXT NOT NULL REFERENCES allocation_runs(run_id),
  fund_id TEXT NOT NULL REFERENCES funds(fund_id),
  eligible INTEGER NOT NULL,
  ineligible_reason TEXT,
  capacity REAL,                                 -- min(available capital, concentration headroom)
  weight REAL,                                   -- value of the allocation key for this fund
  proposed_amt REAL NOT NULL DEFAULT 0,
  final_amt REAL NOT NULL DEFAULT 0,
  sizing_notes TEXT,                             -- JSON list of sizing factor codes
  PRIMARY KEY (run_id, fund_id)
);
CREATE TABLE exceptions (
  exc_id TEXT PRIMARY KEY,
  run_id TEXT REFERENCES allocation_runs(run_id),
  fund_id TEXT,
  rule_id TEXT,
  severity TEXT NOT NULL CHECK (severity IN ('Low','Medium','High','Critical')),
  status TEXT NOT NULL,
  owner TEXT,
  opened_at TEXT NOT NULL,
  resolved_at TEXT,
  resolution TEXT,
  detail TEXT,
  due_at TEXT                                    -- opened_at + SLA hours for the severity
);
CREATE TABLE approvals (
  run_id TEXT NOT NULL REFERENCES allocation_runs(run_id),
  maker TEXT NOT NULL,
  checker TEXT NOT NULL,
  decision TEXT NOT NULL,
  rationale TEXT,
  ts TEXT NOT NULL,
  CHECK (maker <> checker)
);
CREATE TABLE bookings (
  run_id TEXT NOT NULL REFERENCES allocation_runs(run_id),
  fund_id TEXT NOT NULL REFERENCES funds(fund_id),
  amount REAL NOT NULL,
  booked_at TEXT NOT NULL
);
CREATE TABLE ledger_stub (
  fund_id TEXT NOT NULL,
  deal_id TEXT NOT NULL,
  amount REAL NOT NULL,
  posted_at TEXT NOT NULL
);
CREATE TABLE fund_portfolio (                  -- illustrative BDC balance-sheet figures for surveillance
  fund_id TEXT PRIMARY KEY REFERENCES funds(fund_id),
  total_assets REAL NOT NULL,
  qualifying_assets REAL NOT NULL,
  senior_securities REAL NOT NULL,
  cash REAL NOT NULL DEFAULT 0,                  -- cash and equivalents, assumed inside qualifying_assets (B07)
  default_funding TEXT                           -- 'cash' | 'debt' | NULL (unknown) – used when a deal has no funding plan
);
CREATE TABLE funding_plans (                   -- B07: how each fund pays for its share of a deal (illustrative)
  deal_id TEXT NOT NULL REFERENCES deals(deal_id),
  fund_id TEXT NOT NULL REFERENCES funds(fund_id),
  source TEXT NOT NULL CHECK (source IN ('cash','debt','mixed','unknown')),
  cash_share REAL CHECK (cash_share IS NULL OR (cash_share >= 0 AND cash_share <= 1)),
  note TEXT,
  updated_by TEXT,
  updated_at TEXT,
  PRIMARY KEY (deal_id, fund_id)
);
CREATE TABLE fairness_signoffs (               -- B07: reviewer sign-off of the fairness report
  signoff_id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  tolerance REAL NOT NULL,
  run_ids TEXT NOT NULL,                         -- JSON list of runs in the deal set
  policy_versions TEXT NOT NULL,                 -- JSON list of distinct policy versions in the set
  flagged TEXT NOT NULL,                         -- JSON list of funds over tolerance
  commentary TEXT NOT NULL,                      -- JSON {fund: comment}
  report_hash TEXT NOT NULL                      -- SHA-256 of the canonical report (run set, tolerance, policy versions, figures)
);
CREATE TABLE recon_breaks (
  break_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES allocation_runs(run_id),
  fund_id TEXT,
  check_name TEXT NOT NULL,                      -- APPROVED_vs_BOOKED, BOOKED_vs_LEDGER, CONTROL_TOTAL
  expected REAL,
  actual REAL,
  diff REAL,
  status TEXT NOT NULL,                          -- Open, Resolved
  opened_at TEXT NOT NULL,
  resolved_at TEXT,
  resolution TEXT                                -- root cause + action (B05)
);
CREATE TABLE surveillance_results (
  check_id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  fund_id TEXT NOT NULL,
  metric TEXT NOT NULL,                          -- ISSUER_CONCENTRATION:<issuer>, QUALIFYING_ASSETS_RATIO, ASSET_COVERAGE
  value REAL NOT NULL,
  threshold REAL NOT NULL,
  breach INTEGER NOT NULL,
  detail TEXT
);
CREATE TABLE audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  entity TEXT NOT NULL,
  payload TEXT NOT NULL,                         -- JSON
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);

-- Append-only audit log: the database itself refuses edits and deletes.
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
