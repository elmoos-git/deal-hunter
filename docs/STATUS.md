# Feature implementation status

As of 26 Nov 2026 (Phase 1 vertical slice). ✅ working · 🟡 partial · ⬜ not built (the UI says so where it appears).

## Spec §24 acceptance tests

All 15 have automated tests and pass (`python -m pytest`: 56 tests).

| # | Criterion | Test |
|---|---|---|
| 1 | Data persists across restarts | `test_01_…` |
| 2 | Duplicate purchase imports don't duplicate inventory | `test_02_…` (file hash + order reference) |
| 3 | Auction fees calculated correctly | `test_03_…` (marginal/whole tiers, minimum premium, VAT, surcharge) |
| 4 | Max bid respects target profit/ROI | `test_04_…` (exact to the penny) |
| 5 | Alternative-name matching | `test_05_…` (aliases, abbreviations, identifiers, ambiguity left for review) |
| 6 | Bundle split without duplicating cost | `test_06_…` (shared sprues counted once, non-sellable parts dropped) |
| 7 | Sale creates correct inventory + financial records | `test_07_…` (oversell blocked) |
| 8 | Refunds/returns reverse correctly | `test_08_…` (partial, restock, damaged, cancellation) |
| 9 | Repeated scans don't duplicate opportunities | `test_09_…` (price-change snapshots, cross-source duplicate flag) |
| 10 | Missing evidence ≠ confidence | `test_10_…` (<3 comps → insufficient → score 0) |
| 11 | Scanner failures visible + recoverable | `test_11_…` (retry, permanent failure, recovery, job history) |
| 12 | Dashboard reconciles with records | `test_12_…` + reconciliation check shown on the dashboard |
| 13 | Demo data separated | `test_13_…` (separate DB file; refuses to seed live) |
| 14 | Restricted integrations identified | `test_14_…` |
| 15 | External URLs / untrusted content safe | `test_15_…` (SSRF guard, XSS escaping, CSP, CSV formula injection) |

## By section

| Area | Status | Notes |
|---|---|---|
| Command Centre | ✅ | KPIs and 7 charts from real records; reconciliation badge. Forecast cash flow ⬜. |
| Product Database | ✅ | Create, search (name/alias/identifier/faction), aliases, EAN/UPC/ISBN, box contents with shared sprues, change history. Bulk edit ⬜, images ⬜, retail price history UI ⬜. |
| Market Intelligence | ✅ | Sold/hammer/unsold/asking/retail observations, 30/90/180/365/all windows, condition groups, exclusions (edition, lots, unconfirmed offers, outliers), confidence with explanation, overrides that keep the computed value. |
| Deal Scanner | ✅ | Feed with filters/sort, scoring with explanation, budget warnings, save/watch/dismiss-with-reason/re-identify/mark purchased, price snapshots, dedupe. Listings come from the eBay scanner, manual entry, URL capture and CSV/JSON. |
| eBay scanner | 🟡 | Official Browse API adapter: app token (cached, refreshed on 401), EBAY_GB marketplace, paging, polite pacing, retries on 429/5xx, clear errors for missing/bad keys. Search profiles with terms, exclusions, price range, listing types, new-only, UK delivery, frequency; background scheduler; run now; cancel; interrupted-job recovery. Tested against mocked responses only until real keys are added. |
| Deal scoring | ✅ | Configurable weights and thresholds in Settings; caps for low/insufficient evidence and no profit. |
| Auction & Listing Analyser | 🟡 | Itemised fee calculator (tiers, minimum premium, VAT, surcharge, lot fee, payment fee, shipping, FX), break-even / max safe / recommended max bid for profit, ROI or margin targets. Saved per-auction-house fee profiles (linked to a source, with terms URL and checked date) feed auction opportunity costs and a recommended max bid on the deal page. Auto-extraction of lot pages ⬜. |
| Box Split Calculator | ✅ | Component values from the catalogue, editable prices, unsold %, copies, break-even units, whole vs split comparison. Real splits create inventory with an auditable cost allocation. |
| Watchlist | 🟡 | Products with target cost → one-time alerts when a listing lands below target. Saved searches, quiet hours, auction-closing reminders ⬜. |
| Inventory | ✅ | Statuses, cost basis with allocated costs, splits, adjustments/write-offs, ageing colours and suggestions (user decides), CSV export, item history. Photos ⬜. |
| Sales | ✅ | Multi-line sales, fee estimate from profile or actual fees, refunds, returns (restock or damaged), cancellation, cost corrections posted as ledger adjustments. eBay orders report import (match by Custom Label = SKU, then title). |
| Finance & Analytics | ✅ | Ledger, capital in/withdrawals, overheads, transaction vs business profit, monthly table, reconciliation. Tax-year reports/accounting export ⬜. |
| Imports | 🟡 | CSV/JSON for products (+contents), sold prices (label required), listings, purchases, eBay orders report; review queue; duplicate protection. EML/email/mailbox ⬜. |
| Source Manager | 🟡 | All requested sources listed with honest status, buyer-fee profile per source, eBay search profiles, scan job history with cancel. eBay is “partial” (active listings only, no sold data); only manual/CSV/local are “working”. |
| Settings | ✅ | Budget rules, selling-fee profiles (unverified defaults), scoring, valuation window, VAT flag, backup download. |
| Global search, notifications, quick actions | ✅ | |
| JSON API | 🟡 | `/api/dashboard`, `/api/products`, `/api/products/{id}/valuation`, `/api/opportunities`, `POST /api/listings` (for a future browser extension). Docs at `/api/docs`. |
| AI features, image recognition | ⬜ | Deliberately not started (Phase 6, local/free only). |

## Next (proposed order)

1. ~~eBay Browse API adapter + scheduled scans~~ (built; verify with real keys).
2. ~~eBay orders report import~~ (built); saved-search email (EML) import.
3. ~~Per-auctioneer fee profiles~~ (built); lot-page extraction where terms allow.
4. Seed the real catalogue (start with the Combat Patrols / Battleforces Seb actually buys).
