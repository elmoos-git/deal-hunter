# Gaming Collectibles Deal Hunter: design (deliverables 1–6)

Status as of 26 Nov 2026. Covers spec section 26, items 1–6. Item 7 (the working app) is in this repository; see `docs/STATUS.md` for what is built.

## 1. What the app is for

A one-person UK resale business starts with £400, buys Warhammer (then Black Library, then other collectibles) below market value, and resells mainly on eBay. The app is the business's decision system and books of record:

- **Know what things are worth**: valuations come only from recorded evidence (sold prices, hammer prices), split by condition and edition, with a stated confidence. Asking prices are never treated as sold prices.
- **Decide what to buy and how much to pay**: every opportunity gets a landed cost, conservative/expected profit, ROI, a 0–100 score with a written explanation, and a check against the budget rules (cash, reserve, per-deal cap).
- **Run the stock and the money**: purchases become inventory with a cost basis; boxes can be split with an auditable cost allocation; sales and refunds post to a cash ledger. Dashboard figures are computed from those records only.
- **Learn**: realised profit by source and category feeds the next buying decision.

Non-goals: auto-buying, auto-bidding or auto-listing (never without explicit user action), scraping that breaks site terms, and tax advice.

## 2. Technical architecture

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.13, FastAPI | One language for web, jobs, data work; typed request validation. |
| Persistence | SQLAlchemy 2 ORM + Alembic migrations, SQLite file by default | Zero-ops for one user. All SQL goes through SQLAlchemy, so moving to PostgreSQL is a `DATABASE_URL` change plus a migration run. |
| Money | Integer pence (`*_p` columns); FX amounts keep original minor units, currency, rate (decimal string) and date beside the GBP value | No floating-point money. |
| Domain logic | Plain service modules (`app/services/*`): catalogue, valuation, fees, deals, purchasing, inventory, sales, finance, imports, sources/scans | Deterministic, unit-tested, no web dependency. |
| UI | Server-rendered Jinja templates + small vanilla JS + Chart.js (vendored, no CDN) | Fast, dense, no build step. A JSON API (`/api/*`) exposes the same services for a future SPA or browser extension. |
| Jobs | `scan_jobs` / `import_jobs` tables + an in-process runner with retry, cancel and history; APScheduler-style scheduling is a Phase 3 add | No Redis/Celery needed at this scale. |
| Config | Environment variables (`.env` supported), no secrets in code | |
| Demo data | Separate SQLite file (`DEAL_HUNTER_WORKSPACE=demo`), banner on every page, one command to wipe | Demo numbers can never mix with live books. |

Request flow: browser → FastAPI route → service function (one DB transaction for each financial/inventory operation) → models. External content (listing titles, descriptions, URLs) is validated on the way in and HTML-escaped on the way out; outbound fetches go through one SSRF-guarded client.

## 3. Entity relationship model

```
categories ─┐
            ▼
products ──< product_aliases
   │  ├────< product_identifiers (EAN/UPC/ISBN/SKU, unique per kind)
   │  ├────< retail_prices (history)
   │  ├────< bundle_components >── products (component, qty, separately_sellable, shared_sprue_group)
   │  ├────< product_relationships >── products (edition_of, repack_of, related)
   │  ├────< sold_observations (sold / hammer / unsold / asking; provenance, data_label, exclusion flags)
   │  └────< valuations (per condition group + window; computed values kept, override + reason separate)
sources ──< market_listings ──< listing_snapshots
   │              └──1 deal_opportunities (score, explanation, status, reject reason)
   └────< scan_jobs
watchlist_entries ─> products | market_listings ;  alerts (unique dedupe_key)
purchase_orders ──< purchase_order_items ──> products
                         └──< inventory_items ──< inventory_movements
                                   └── cost_allocations (split parent → child items)
sales_orders ──< sales_order_items ──> inventory_items ;  sales_orders ──< refunds
financial_transactions (signed cash ledger; ref_type/ref_id back to the record that caused it)
expenses (overhead vs transaction) ; import_jobs ──< import_reviews ; settings ; audit_events
```

Key integrity rules:

- `inventory_items.quantity_on_hand >= 0` (DB check) and every quantity change writes an `inventory_movements` row in the same transaction.
- Each item carries `remaining_cost_p`. A sale or split takes cost pro rata, and the last unit takes the exact remainder, so cost is never created or lost through rounding.
- Purchases are unique on `(source, order_ref)`; import files are unique on content hash; listings are unique on `(source, external_id)`; sold observations on a dedupe key; alerts on a dedupe key.
- Valuations are append-only snapshots (`is_current` flag); sales store their own price, fees and COGS, so later valuation changes never rewrite history.

## 4. Implementation roadmap

| Phase | Scope | State |
|---|---|---|
| 1 Foundation + vertical slice | Schema/migrations, design system, navigation, catalogue + alias search, labelled sold-data import, valuation engine, manual/CSV opportunity import + scoring, purchases, inventory, box split, sales, refunds, ledger, dashboard, demo workspace, tests | **This delivery** |
| 2 Product & market intelligence | Product import UI with review queue, bulk edit, change history UI, valuation override UI polish, trend charts per product | Partly in Phase 1 (import, overrides) |
| 3 Deal discovery | eBay Browse API adapter (first real integration, needs developer keys), scheduled scans, search profiles, watchlist alerts, cross-market dedupe | Adapter framework + scan job runner already in place |
| 4 Profitability tools | URL-first analyser with per-auctioneer fee profiles, max-bid calculator UI, multi-copy box scenarios | Fee engine + max bid + split calculator done in Phase 1 |
| 5 Admin automation | EML import (purchase/sale confirmations, saved-search alerts), eBay order report import, ageing suggestions, tax-year exports | |
| 6 Expansion | Allegro, Japanese proxies, forecasting, optional local-AI matching, optional image recognition | |

## 5. Source integration feasibility matrix

"Verified" means checked against a current source on 26 Nov 2026; everything else is marked as needing confirmation before build.

| Source | Best legitimate method | Feasibility | Notes |
|---|---|---|---|
| eBay: active listings | **Browse API** (official REST, app OAuth token) | High | Needs a free eBay developer account and keys. Current call quota to be read from the developer portal at setup. First Phase 3 integration. |
| eBay: sold prices via API | Marketplace Insights API | **Not assumed** | eBay lists it as a *Limited Release* (verified, developer.ebay.com research APIs page). Access needs eBay's approval; small projects report difficulty getting it. |
| eBay: Finding API `findCompletedItems` | n/a | **Gone** | Finding and Shopping APIs were scheduled for decommission in 2025 (eBay developer announcements). Not used. |
| eBay Product Research (ex-Terapeak) | User researches in Seller Hub, records verified comps via the app's CSV template or quick-entry form | Medium (manual) | Free for all sellers, up to 3 years of eBay sales data (verified, official eBay UK announcement). No public API found, and sellers report no native export (community threads, not an official statement). A business eBay account does not grant programmatic access. |
| eBay: own orders | Seller Hub order report CSV import; later Sell Fulfillment API with user OAuth | High | Phase 5. |
| eBay: saved-search alerts | Email alert → EML import | High | Phase 5. |
| Facebook Marketplace | Manual URL submission / browser-assisted capture of a listing the user is viewing | Low automation | No public search API; automated collection is against Meta's terms. |
| Gumtree, Vinted UK | Saved-search email alerts → EML; manual URL | Low–Medium | No public search API known (to confirm). |
| AbeBooks UK, BookFinder | Manual URL; investigate AbeBooks affiliate/search programmes | Medium (to confirm) | |
| The Saleroom, Easy Live Auction (host many listed houses: Vectis, Ewbank's, etc.) | Saved-search email alerts → EML; user-initiated URL analyser reading public lot pages if terms allow | Medium (to confirm per site) | Fees differ per auctioneer: per-house fee profiles, never one universal rate. |
| John Pye, other independent auction houses | Same as above | Medium (to confirm) | |
| UK Warhammer retailers (Element, Wayland, Firestorm, Outpost, Zatu, Goblin Gaming, etc.) | Public product pages via user-initiated analyser; investigate affiliate product feeds | Medium (to confirm) | Retail price history is useful for "discounted retail" deals. |
| Wargame Compare, Warzilla | Price comparison sites: check terms before any automation; manual otherwise | Unknown | |
| Hachette Partworks | Manual | Low | Subscription product. |
| Allegro | Official REST API exists | Medium (to confirm) | Listing-search access for new apps needs confirming. |
| OLX Poland | Manual URL | Low | Partner API is for own ads, not search (to confirm). |
| Mandarake, Suruga-ya, Buyee | Manual URL + landed-cost calculator (FX, proxy fees, shipping, import VAT/duty) | Low automation | |
| Local / private / car boot / charity | Manual entry, mobile-friendly quick add | n/a | |
| Any source | CSV/JSON import of listings | High | **Working in Phase 1.** |

## 6. Limitations and assumptions

1. **No sold data ships with the app.** Valuations stay "insufficient evidence" until the user records verified sold prices (manual or CSV). Test/demo observations must carry `data_label=test` or live in the demo workspace.
2. **Catalogue starts empty.** There is no licensed, complete Warhammer database; products are added manually or imported, and missing fields stay blank, never guessed.
3. **Fees are configuration, not facts.** eBay selling fees (private vs business), buyer protection fees and auctioneer premiums change and differ. The app ships editable fee profiles marked "verify against current terms". As a reseller buying stock to sell, Seb should expect business-seller treatment, not private "personal possessions" treatment.
4. **Valuation uses item price, not buyer-paid shipping**; postage is a separate selling cost. Conservative = 25th percentile, expected = median, optimistic = 75th percentile of retained comparables in the chosen window.
5. **Confidence rules**: fewer than 3 comparables → "insufficient evidence" and no score above "Low priority". Confidence also falls with stale data, wide price spread and less reliable provenance.
6. **Box splits** allocate cost by each component's expected value (falling back to RRP), and refuse to split when a component has neither, so the user sets weights explicitly. Components on a shared sprue are treated as one sellable unit.
7. **Tax** figures (later phases) are estimates for record-keeping and are not professional tax advice. VAT registration is a setting; default is not registered.
8. **Single user, local.** No login; the server binds to 127.0.0.1. Hosting online later needs authentication and CSRF protection added first (documented, not built).
9. **Auction max bids** ignore bid increments and reserve prices; they are a ceiling, not a bidding strategy.
10. **AI** is not used in Phase 1. Matching is deterministic (normalised aliases + token similarity). Any future AI must be free/local unless Seb approves a paid service.
