# @created 2026-04-21
# @file    seed.sql

-- ============================================================
-- FinFlux — Demo Seed Data
-- @version 1.0.0 | April 2026
-- ============================================================
USE finflux;

-- ============================================================
-- USERS
-- ============================================================
-- Identity is captured by first_name + last_name (both NOT NULL).
-- The display name is computed at the application layer; no `name`
-- column is stored.
INSERT INTO users (id, first_name, last_name, initials, color, role, email) VALUES
(1, 'Priya', 'Sharma', 'PS', '#dc2626', 'analyst',    'priya@tickandtie.com'),
(2, 'Raj',   'Patel',  'RP', '#7c3aed', 'analyst',    'raj@tickandtie.com'),
(3, 'Sara',  'Chen',   'SC', '#0891b2', 'analyst',    'sara@tickandtie.com'),
(4, 'David', 'Kim',    'DK', '#d97706', 'analyst',    'david@tickandtie.com'),
(5, 'Jane',  'Smith',  'JS', '#2563eb', 'supervisor', 'jane@tickandtie.com'),
(6, 'Mark',  'Lee',    'ML', '#059669', 'supervisor', 'mark@tickandtie.com'),
(7, 'Lisa',  'Wang',   'LW', '#be185d', 'director',   'lisa@tickandtie.com');

-- ============================================================
-- ENTITIES
-- ============================================================
INSERT INTO entities (id, name, region, supervisor_id, status, tags, period_type, period_from, period_to, sub_due_date, rev_due_date, approval_level) VALUES
('ACM-US', 'Acme Corp (US)',  'North America', 5, 'under_review', '["tech","priority"]',  'YoY', 'Mar 2026', 'Mar 2025', '2026-04-18', '2026-04-22', 'supervisor'),
('ACM-CA', 'Acme Canada',     'North America', 5, 'wip',          '["tech"]',             'MoM', 'Mar 2026', 'Feb 2026', '2026-04-22', '2026-04-26', 'supervisor'),
('BET-US', 'Beta Holdings',   'North America', 5, 'approved',     '["finance","priority"]','YoY', 'Mar 2026', 'Mar 2025', '2026-04-14', '2026-04-18', 'director'),
('ACM-UK', 'Acme UK',         'Europe',        5, 'under_review', '["tech","emea"]',      'QoQ', 'Q1 2026',  'Q4 2025',  '2026-04-20', '2026-04-24', 'supervisor'),
('ACM-DE', 'Acme GmbH',       'Europe',        5, 'rejected',     '["tech","emea"]',      'YoY', 'Mar 2026', 'Mar 2025', '2026-04-25', '2026-04-28', 'supervisor'),
('ACM-IN', 'Acme India',      'APAC',          5, 'assigned',     '["tech","apac"]',      'MoM', 'Mar 2026', 'Feb 2026', '2026-04-16', '2026-04-20', 'supervisor');

-- ============================================================
-- ENTITY ASSIGNMENTS
-- ============================================================
INSERT INTO entity_assignments (entity_id, caption, assignee_id) VALUES
('ACM-US', 'ALL', 1),
('ACM-CA', 'ALL', 2),
('BET-US', 'ALL', 3),
('BET-US', 'ALL', 2),
('ACM-UK', 'ALL', 1),
('ACM-DE', 'ALL', 4),
('ACM-IN', 'ALL', 1),
('ACM-IN', 'ALL', 3);

-- ============================================================
-- COMPANY PROFILES
-- ============================================================
INSERT INTO company_profiles (entity_id, industry, geography, revenue_range, entity_type, brief) VALUES
('ACM-US', 'Technology / SaaS',            'United States', '$50M–$100M',  'Private',    'B2B SaaS platform for accounting firms. Mid-market focus with 200+ clients. Primary revenue from annual subscriptions and implementation services.'),
('ACM-CA', 'Technology / SaaS',            'Canada',        '$10M–$50M',   'Subsidiary', 'Canadian subsidiary of Acme Corp. Serves North American clients outside the US. Revenue in CAD, reported in USD.'),
('BET-US', 'Financial Services / Holdings','United States', '$100M–$500M', 'Private',    'Diversified holding company with 6 portfolio companies across fintech, insurance, and real estate.'),
('ACM-UK', 'Technology / SaaS',            'United Kingdom','$10M–$50M',   'Subsidiary', 'UK subsidiary of Acme Corp. Serves EMEA clients. Revenue in GBP, reported in USD.'),
('ACM-DE', 'Technology / SaaS',            'Germany',       '$5M–$10M',    'Subsidiary', 'German subsidiary serving DACH region enterprise clients. Revenue in EUR.'),
('ACM-IN', 'Technology / IT Services',     'India',         '$10M–$50M',   'Subsidiary', 'India development center and APAC sales office. Wage costs in INR, significant headcount growth.');

-- ============================================================
-- THRESHOLDS
-- ============================================================
INSERT INTO thresholds (entity_id, statement, mode, pct_value, abs_value, logic, apply_all) VALUES
('ACM-US','pl','both',10.00,100000.00,'or',1), ('ACM-US','bs','both', 5.00, 75000.00,'or',1),
('ACM-CA','pl','both',10.00,100000.00,'or',1), ('ACM-CA','bs','both', 5.00, 75000.00,'or',1),
('BET-US','pl','both',10.00,100000.00,'or',1), ('BET-US','bs','both', 5.00, 75000.00,'or',1),
('ACM-UK','pl','both',10.00,100000.00,'or',1), ('ACM-UK','bs','both', 5.00, 75000.00,'or',1),
('ACM-DE','pl','both',10.00,100000.00,'or',1), ('ACM-DE','bs','both', 5.00, 75000.00,'or',1),
('ACM-IN','pl','both',10.00,100000.00,'or',1), ('ACM-IN','bs','both', 5.00, 75000.00,'or',1);

-- ============================================================
-- FLUX DATA — ACM-US P&L
-- ============================================================
INSERT INTO flux_data (entity_id, statement, period_from, period_to, caption, current_value, comp_value, pct_variance, is_material, is_bold, sort_order) VALUES
('ACM-US','pl','Mar 2026','Mar 2025','Revenue',              1590000, 1345000,  18.22, 1, 0,  1),
('ACM-US','pl','Mar 2026','Mar 2025','Consulting Revenue',    530000,  490000,   8.16, 0, 0,  2),
('ACM-US','pl','Mar 2026','Mar 2025','Product Revenue',      1060000,  855000,  23.98, 1, 0,  3),
('ACM-US','pl','Mar 2026','Mar 2025','Total Revenue',        1590000, 1345000,  18.22, 1, 1,  4),
('ACM-US','pl','Mar 2026','Mar 2025','Cost of Revenue',       477000,  376600,  26.66, 1, 0,  5),
('ACM-US','pl','Mar 2026','Mar 2025','Gross Profit',         1113000,  968400,  14.93, 1, 1,  6),
('ACM-US','pl','Mar 2026','Mar 2025','Salaries & Wages',      450000,  420000,   7.14, 0, 0,  7),
('ACM-US','pl','Mar 2026','Mar 2025','Professional Services',  95000,   88000,   7.95, 0, 0,  8),
('ACM-US','pl','Mar 2026','Mar 2025','Software & Technology', 146000,   80000,  82.50, 1, 0,  9),
('ACM-US','pl','Mar 2026','Mar 2025','Travel & Entertainment',  32000,  29000,  10.34, 0, 0, 10),
('ACM-US','pl','Mar 2026','Mar 2025','Depreciation',           28000,  26000,   7.69, 0, 0, 11),
('ACM-US','pl','Mar 2026','Mar 2025','Total OpEx',            751000,  643000,  16.80, 1, 1, 12),
('ACM-US','pl','Mar 2026','Mar 2025','Operating Income',      362000,  325400,  11.25, 1, 1, 13),
('ACM-US','pl','Mar 2026','Mar 2025','Interest Expense',       18000,   15000,  20.00, 0, 0, 14),
('ACM-US','pl','Mar 2026','Mar 2025','Income Tax',             86000,   77000,  11.69, 0, 0, 15),
('ACM-US','pl','Mar 2026','Mar 2025','Net Income',            258000,  233400,  10.54, 1, 1, 16);

-- ============================================================
-- FLUX DATA — ACM-US B/S
-- ============================================================
INSERT INTO flux_data (entity_id, statement, period_from, period_to, caption, current_value, comp_value, pct_variance, is_material, is_bold, sort_order) VALUES
('ACM-US','bs','Mar 2026','Mar 2025','Cash',                    820000,  690000,  18.84, 1, 0,  1),
('ACM-US','bs','Mar 2026','Mar 2025','Accounts Receivable',     345000,  290000,  18.97, 1, 0,  2),
('ACM-US','bs','Mar 2026','Mar 2025','Prepaid Expenses',         42000,   38000,  10.53, 0, 0,  3),
('ACM-US','bs','Mar 2026','Mar 2025','Total Current Assets',   1207000, 1018000,  18.56, 1, 1,  4),
('ACM-US','bs','Mar 2026','Mar 2025','PP&E',                    310000,  295000,   5.08, 0, 0,  5),
('ACM-US','bs','Mar 2026','Mar 2025','Intangibles',             125000,  130000,  -3.85, 0, 0,  6),
('ACM-US','bs','Mar 2026','Mar 2025','Total Assets',           1642000, 1443000,  13.79, 1, 1,  7),
('ACM-US','bs','Mar 2026','Mar 2025','Accounts Payable',        198000,  175000,  13.14, 1, 0,  8),
('ACM-US','bs','Mar 2026','Mar 2025','Accrued Liabilities',      87000,   80000,   8.75, 0, 0,  9),
('ACM-US','bs','Mar 2026','Mar 2025','Total Current Liabilities',285000,  255000,  11.76, 1, 1, 10),
('ACM-US','bs','Mar 2026','Mar 2025','Long-Term Debt',          200000,  220000,  -9.09, 0, 0, 11),
('ACM-US','bs','Mar 2026','Mar 2025','Total Liabilities',       485000,  475000,   2.11, 0, 1, 12),
('ACM-US','bs','Mar 2026','Mar 2025','Equity',                 1157000,  968000,  19.52, 1, 1, 13);

-- ============================================================
-- MACRO FACTORS
-- ============================================================
INSERT INTO macro_factors (entity_id, category, icon, text, is_custom, created_by) VALUES
('ACM-US','Geopolitical','🌍','US-China trade tensions: 15% tariff on tech imports effective Feb 2026 — impacts COGS for hardware-bundled SaaS.',0,5),
('ACM-US','Energy',      '⛽','Brent crude at $82/bbl (+8% YoY) — cloud hosting energy surcharges up 3.2%, impacting infrastructure costs.',0,5),
('ACM-US','Monetary',    '💰','Fed holds rates at 4.25% — SaaS valuations stabilize, favorable for fundraising and M&A exit multiples.',0,5),
('ACM-US','Regulatory',  '📋','SEC cybersecurity disclosure rules (Dec 2025) — SOC 2 compliance costs projected +12% for mid-market SaaS.',0,5),
('ACM-US','Industry',    '📈','Global SaaS market grew 18.5% in Q1 2026 — AI-native platforms capturing disproportionate market share.',0,5),
('ACM-US','FX',          '💱','USD strengthened 3.1% vs EUR, 2.4% vs GBP — headwind for international ARR reported in USD.',0,5),
('BET-US','Conflict',    '⚔️','Russia-Ukraine conflict ongoing — European portfolio exposure creates supply chain and energy cost volatility.',0,5),
('BET-US','Energy',      '⛽','Natural gas futures up 22% QoQ — impacts operating costs for manufacturing portfolio companies.',0,5),
('BET-US','Monetary',    '💰','Interest rate environment pressuring leveraged acquisitions — LBO financing costs +180bps vs 2024.',0,5),
('BET-US','Regulatory',  '📋','New FASB intercompany elimination guidance (ASU 2025-08) — increases consolidation complexity and audit hours.',0,5),
('ACM-UK','Geopolitical','🌍','Post-Brexit trade friction: UK-EU services passporting gaps impacting cross-border SaaS delivery.',0,5),
('ACM-UK','Energy',      '⛽','UK electricity prices 40% above pre-2022 baseline — data center costs elevated, margin pressure.',0,5),
('ACM-UK','FX',          '💱','GBP/USD at 1.24 (-2.4% YoY) — favorable for USD-reporting parent but reduces GBP purchasing power.',0,5),
('ACM-DE','Conflict',    '⚔️','European defense spending surge (+18%) redirecting government IT budgets — potential crowding-out of enterprise software.',0,5),
('ACM-DE','Energy',      '⛽','German industrial electricity at €0.28/kWh — highest in G7, significant margin impact for data-intensive operations.',0,5),
('ACM-IN','Geopolitical','🌍','India-China border tensions — minimal direct impact but IT services talent competition intensifying.',0,5),
('ACM-IN','Industry',    '📈','India IT services sector growing 14% — strong talent pipeline but wage inflation at 11% for senior engineers.',0,5);

-- ============================================================
-- CHAT MESSAGES & REPLIES
-- ============================================================
INSERT INTO chat_messages (id, entity_id, user_id, text, created_at) VALUES
(1,'ACM-US',5,'@Priya Sharma Revenue variance at 18% — please verify ASC 606 adjustment before submission.','2026-04-05 10:30:00'),
(2,'ACM-US',1,'Software & Technology spend up 82% — flagging for CTO sign-off. @Jane Smith should we escalate?','2026-04-06 09:00:00'),
(3,'ACM-US',5,'Macro alert: USD strengthened 3.1% vs EUR — check if international ARR is properly translated.','2026-04-07 08:30:00'),
(4,'ACM-UK',1,'FX impact on Operating Income is -$34K. GBP/USD at 1.24. @Jane Smith is this within tolerance?','2026-04-04 11:00:00'),
(5,'ACM-UK',5,'UK electricity costs still elevated — check if data center hosting surcharges hit this quarter.','2026-04-05 15:00:00'),
(6,'BET-US',3,'Intercompany eliminations reconciled. 3 new subs added this quarter — consolidation entries posted.','2026-04-03 14:00:00'),
(7,'BET-US',5,'@Sara Chen @Raj Patel this needs Lisa Wang''s sign-off before final approval. Flagging for director review.','2026-04-04 10:00:00'),
(8,'ACM-IN',1,'Wage inflation at 11% for senior engineers impacting Salaries line. @Jane Smith should we add this to macro factors?','2026-04-06 10:30:00');

INSERT INTO chat_replies (message_id, user_id, text, created_at) VALUES
(1,1,'Checked — adjustment is correct per ASC 606-10-25. Memo attached.','2026-04-05 11:15:00'),
(1,5,'Confirmed. Moving to review.','2026-04-05 14:00:00'),
(2,5,'Yes, need CTO memo before approval. Flag it.','2026-04-06 09:45:00'),
(4,5,'Above threshold. Add a note in the commentary and flag for review.','2026-04-04 11:30:00'),
(6,2,'Confirmed balances tie. Ready for supervisor review.','2026-04-03 16:15:00'),
(8,5,'Good catch. Add it as a custom Industry factor in AI Insights.','2026-04-06 11:00:00');

-- ============================================================
-- COMMENTS ON CAPTIONS
-- ============================================================
INSERT INTO comments (id, entity_id, caption, user_id, text, is_resolved, created_at) VALUES
(1,'ACM-US','Revenue',             5,'Verify ASC 606 adjustment for Q1 recognition',       0,'2026-04-05 10:30:00'),
(2,'ACM-US','Revenue',             5,'Cross-check with deferred revenue schedule',          1,'2026-04-06 09:00:00'),
(3,'ACM-US','Software & Technology',5,'82% increase is material — need CTO approval memo', 0,'2026-04-04 15:45:00'),
(4,'ACM-UK','Operating Income',    5,'Verify FX impact on operating margins',               0,'2026-04-03 11:00:00');

INSERT INTO comments (entity_id, caption, user_id, text, parent_id, created_at) VALUES
('ACM-US','Revenue',            1,'Memo attached — adjustment verified per ASC 606-10-25.',1,'2026-04-05 11:15:00'),
('ACM-US','Revenue',            1,'@Jane Smith confirmed with audit team.',                 1,'2026-04-05 14:00:00'),
('ACM-US','Revenue',            1,'Reconciled — $12K timing difference resolved.',          2,'2026-04-06 10:30:00'),
('ACM-UK','Operating Income',   1,'GBP/USD impact calculated at -$34K.',                   4,'2026-04-03 14:30:00');

-- ============================================================
-- SIGN-OFFS
-- ============================================================
INSERT INTO signoffs (entity_id, caption, signed_by, version, signed_at) VALUES
('ACM-US','Revenue',              5, 0,'2026-04-07 15:00:00'),
('ACM-US','Total Revenue',        5, 0,'2026-04-07 15:05:00'),
('ACM-US','Gross Profit',         5, 0,'2026-04-07 15:10:00'),
('ACM-US','Software & Technology',5, 0,'2026-04-05 16:00:00');

-- ============================================================
-- CAPTION VERSIONS
-- ============================================================
INSERT INTO caption_versions (entity_id, caption, version) VALUES
('ACM-US','Revenue',              1),
('ACM-US','Software & Technology',1),
('ACM-UK','Operating Income',     1);

-- ============================================================
-- TASKS
-- ============================================================
INSERT INTO tasks (entity_id, title, description, assignee_id, due_date, status, priority, phase, created_at) VALUES
('ACM-US','Post journal entries',   'All JEs for month-end accruals, prepaid amortization, and intercompany',1,'2026-04-08','done',       'high',    'preparation','2026-04-01 09:00:00'),
('ACM-US','Run TB & reconciliations','Trial balance extraction, bank recs, AR/AP aging, fixed assets',       1,'2026-04-10','done',       'high',    'preparation','2026-04-01 09:00:00'),
('ACM-US','Prepare flux — P&L',     'P&L variance analysis with AI commentary generation',                   1,'2026-04-14','in_progress','high',    'preparation','2026-04-01 09:00:00'),
('ACM-US','Prepare flux — BS',      'Balance sheet flux with materiality review',                            1,'2026-04-15','in_progress','medium',  'preparation','2026-04-01 09:00:00'),
('ACM-US','AI commentary review',   'Review all AI-generated variance commentary for accuracy',              5,'2026-04-18','pending',    'high',    'review',     '2026-04-01 09:00:00'),
('ACM-US','Supervisor sign-off',    'Final review and sign-off on flux package',                             5,'2026-04-18','pending',    'critical','review',     '2026-04-01 09:00:00'),
('ACM-UK','Post journal entries',   'All month-end JEs including FX revaluation',                            1,'2026-04-09','done',       'high',    'preparation','2026-04-01 09:00:00'),
('ACM-UK','FX revaluation adjustments','GBP/USD translation entries per closing rate',                      1,'2026-04-12','in_progress','critical','preparation','2026-04-01 09:00:00'),
('ACM-UK','Prepare flux — P&L',     'P&L analysis with FX impact segregation',                              1,'2026-04-16','todo',       'medium',  'preparation','2026-04-01 09:00:00'),
('ACM-UK','Supervisor review',      'Review flux with macro factor overlay',                                 5,'2026-04-22','todo',       'high',    'review',     '2026-04-01 09:00:00'),
('BET-US','Intercompany eliminations','Review and post IC elimination entries for 6 subs',                   3,'2026-04-10','done',       'critical','preparation','2026-04-01 09:00:00'),
('BET-US','Prepare consolidation flux','Consolidated P&L and BS flux analysis',                             3,'2026-04-12','done',       'high',    'preparation','2026-04-01 09:00:00'),
('BET-US','Supervisor review',      'Review flux and prepare for director',                                  5,'2026-04-16','in_progress','high',    'review',     '2026-04-01 09:00:00'),
('BET-US','Director approval',      'Lisa Wang final sign-off on consolidation',                             7,'2026-04-20','pending',    'critical','approval',   '2026-04-01 09:00:00'),
('ACM-DE','Post entries & TB',      '',                                                                       4,'2026-04-14','in_progress','medium',  'preparation','2026-04-01 09:00:00'),
('ACM-DE','Prepare flux analysis',  '',                                                                       4,'2026-04-20','todo',       'medium',  'preparation','2026-04-01 09:00:00'),
('ACM-CA','Journal entries',        '',                                                                       2,'2026-04-10','done',       'medium',  'preparation','2026-04-01 09:00:00'),
('ACM-CA','Prepare flux — P&L',     '',                                                                       2,'2026-04-18','in_progress','high',    'preparation','2026-04-01 09:00:00'),
('ACM-IN','Post entries',           '',                                                                       3,'2026-04-08','done',       'medium',  'preparation','2026-04-01 09:00:00'),
('ACM-IN','Run reconciliations',    '',                                                                       1,'2026-04-12','in_progress','high',    'preparation','2026-04-01 09:00:00'),
('ACM-IN','Prepare flux',           '',                                                                       3,'2026-04-16','todo',       'medium',  'preparation','2026-04-01 09:00:00');

-- ============================================================
-- TASK REMINDERS
-- ============================================================
INSERT INTO task_reminders (task_id, days_before, remind_time, email, is_sent) VALUES
(1, 1,'09:00:00','priya@tickandtie.com',1),
(2, 1,'09:00:00','priya@tickandtie.com',1),
(3, 2,'08:30:00','priya@tickandtie.com',0),
(4, 1,'09:00:00','priya@tickandtie.com',0),
(5, 2,'08:00:00','jane@tickandtie.com', 0),
(6, 1,'08:00:00','jane@tickandtie.com', 0);

-- ============================================================
-- TASK RECURRENCE
-- ============================================================
INSERT INTO task_recurrence (task_id, frequency, day_of_month) VALUES
(2, 'monthly', 10),
(6, 'monthly', 18),
(7, 'monthly',  9),
(17,'monthly', 10);

-- ============================================================
-- PERSONAL REMINDERS
-- ============================================================
INSERT INTO personal_reminders (user_id, text, due_date, remind_time, is_done, created_at) VALUES
(1,'Review ASC 606 memo before submission',           '2026-04-17','09:00:00',0,'2026-04-10 09:00:00'),
(1,'Follow up on CTO approval for Software variance', '2026-04-16','14:00:00',0,'2026-04-10 09:00:00'),
(1,'Verify FX rates with Treasury for ACM-UK',        '2026-04-19','10:00:00',1,'2026-04-10 09:00:00');

-- ============================================================
-- AI COMMENTARY
-- ============================================================
INSERT INTO ai_commentary (entity_id, caption, concise_text, detailed_text, ai_score) VALUES
('ACM-US','Revenue',
 'Revenue increased 18% ($245,000) driven by subscription and product growth.',
 'Revenue increased 18% — $245,000 favorable.\n\nDrivers:\n• Volume/mix: ~$147,000 (new subscription additions and upsells)\n• Rate/price: ~$98,000 (annual price increase of 4.5%)\n\n⚠️ Material — review required. Cross-check ASC 606 recognition schedule.',
 92),
('ACM-US','Software & Technology',
 'Software & Technology increased 83% ($66,000) due to new SaaS tool subscriptions.',
 'Software & Technology increased 83% — $66,000 unfavorable.\n\nDrivers:\n• Volume/mix: ~$40,000 (3 new SaaS platforms onboarded in Q1)\n• Rate/price: ~$26,000 (vendor price increases across Infra stack)\n\n⚠️ Material — CTO approval memo required before sign-off.',
 87),
('ACM-US','Gross Profit',
 'Gross Profit increased 15% ($144,600) reflecting improved revenue mix.',
 'Gross Profit increased 15% — $144,600 favorable.\n\nDrivers:\n• Volume/mix: ~$86,760 (higher-margin subscription revenue growth)\n• Rate/price: ~$57,840 (reduced CoR per unit at scale)\n\nWithin expected range given revenue growth.',
 90),
('ACM-US','Net Income',
 'Net Income increased 11% ($24,600) after absorbing elevated OpEx growth.',
 'Net Income increased 11% — $24,600 favorable.\n\nRevenue growth of 18% partially offset by OpEx growth of 17% (primarily S&T and headcount). Effective tax rate stable at 25%.\n\nWithin thresholds. No escalation required.',
 85),
('ACM-UK','Operating Income',
 'Operating Income impacted by GBP/USD FX headwind of -$34,000.',
 'Operating Income declined 4% — $34,000 unfavorable.\n\nPrimary driver: GBP/USD rate moved from 1.27 to 1.24 (-2.4% YoY)\n• FX translation impact: ~$34,000\n• Underlying operating performance: flat to slightly positive\n\n⚠️ Material — FX memo recommended.',
 88);

-- ============================================================
-- EXPORT LOG (samples)
-- ============================================================
INSERT INTO export_log (entity_id, exported_by, mode, statement) VALUES
('ACM-US', 5, 'detailed', 'pl'),
('BET-US', 5, 'concise',  'both');
