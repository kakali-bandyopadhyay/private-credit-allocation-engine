# Synthetic data (illustrative only)

All funds, issuers, deals, holdings and restrictions are invented. Amounts are in USD millions.
Policies and rule parameters are illustrative and do not represent any firm's actual policies.

Planted test conditions (used by the mock cases in docs/SPEC.md §7):
- **D003 Atlas Business Services**: F1 and F2 (BDCs) already hold this issuer, so concentration headroom limits their capacity (20 and 15), and their participation is not in proportion to existing holdings, which triggers the co-investment board step (mock case 1).
- **D009 Crescent Data Centres**: restricted for insurance mandate F5 (mock case 2).
- **D007 Bluebell Consumer Brands**: rating missing → data-quality exception at intake (mock case 3).
- **D008 Summit Logistics**: flagged `qualifying: false` (not a qualifying asset for BDC purposes) – without the pre-trade check (v0.5) booking it into BDC F2 pushed F2's qualifying-asset ratio below 70%; from v0.6 the pre-trade check caps F2 so the ratio stays at or above 70%.
- **fund_portfolio.json**: illustrative balance-sheet figures for the two BDCs (total assets, qualifying assets, senior securities) used by surveillance. Simplified model: a booking adds its amount to total assets (and to qualifying assets if the deal qualifies); senior securities unchanged.

## B07 additions
- **fund_portfolio.json**: adds `cash` (cash and equivalents, assumed inside qualifying assets) and `default_funding` (`debt` for both BDCs) – illustrative.
- **funding_plans.json**: how each BDC pays for its share of a deal. D008: F1 cash, F2 debt. D013: F1 **unknown** (planted: triggers the conservative case, a High exception to Compliance, and blocks booking), F2 debt.
- **D013 Orion Fleet Leasing** ($120m, first lien, BB-, `qualifying: false`): a second non-qualifying deal used to show headroom reservation while D008 is approved but not booked.
- **rules.json**: `SIZE-BDC-QUALIFYING-HEADROOM` v2 (funding-aware, 150% asset coverage, reservation statuses); v1 kept for history.

## B07.1 additions
- **rules.json**: `CTRL-REVIEW` v1 (illustrative operating controls) – fairness review tolerance 5%, approval SLA 8h for submitted runs, reservation review period 72h with a 24h warning. Changing any of these changes the policy version of later runs.
