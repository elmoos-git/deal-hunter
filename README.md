# Gaming Collectibles Deal Hunter

Sourcing, valuation, inventory and resale-accounting tool for a one-person UK collectibles business (Warhammer first).
Runs locally on your computer. Single user, no login, binds to `127.0.0.1`.

- Design, architecture, ER model, roadmap, source feasibility, assumptions: [`docs/DESIGN.md`](docs/DESIGN.md)
- What works today and what doesn't: [`docs/STATUS.md`](docs/STATUS.md)

## Quick start (no typing)

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/) (on Windows, tick **Add python.exe to PATH**).
2. Download this repository (Code → Download ZIP) and unzip it.
3. Double-click **`start-windows.bat`** (Windows) or **`start-mac.command`** (Mac; the first time, right-click → Open).
   The first run installs what it needs; then your browser opens at http://127.0.0.1:8000. Close the window to stop.

Your data lives in the `data/` folder inside that directory. Keep it when you download a newer version.

## Setup (manual)

Needs Python 3.11+ (tested on 3.13).

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py                        # live workspace → http://127.0.0.1:8000
```

The database (`data/live.sqlite`) and its schema are created automatically on first start (Alembic migrations run at startup).
On first visit, the Command Centre asks you to record your starting capital (prefilled with £400).

### Demo workspace

Demo data lives in a **separate** database file (`data/demo.sqlite`), never in your live books, and every page shows a DEMO banner.

```bash
DEAL_HUNTER_WORKSPACE=demo python -c "from app.services import demo; demo.seed()"
DEAL_HUNTER_WORKSPACE=demo python run.py
# remove it:
DEAL_HUNTER_WORKSPACE=demo python -c "from app.services import demo; demo.wipe()"
```

Demo products are named `[DEMO] …` and all demo sold prices carry `data_label=test`.

### Configuration (environment variables or a `.env` file)

| Variable | Default | Purpose |
|---|---|---|
| `DEAL_HUNTER_WORKSPACE` | `live` | `live` or `demo` |
| `DATABASE_URL` | `sqlite:///data/live.sqlite` | Live DB. A PostgreSQL URL works with a driver installed (`pip install psycopg[binary]`). |
| `DATABASE_URL_DEMO` | `sqlite:///data/demo.sqlite` | Demo DB |
| `DEAL_HUNTER_HOST` / `DEAL_HUNTER_PORT` | `127.0.0.1` / `8000` | Bind address. Do not expose publicly: there is no authentication yet. |
| `DEAL_HUNTER_HTTP_TIMEOUT` | `8` | Seconds, for user-initiated URL capture |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | — | eBay Production keyset (App ID / Cert ID). Enables the eBay scanner. |
| `EBAY_ENVIRONMENT` | `production` | `sandbox` to use eBay's test data |
| `DEAL_HUNTER_SCHEDULER` | `1` | `0` disables scheduled scans |

Secrets go in `.env` (git-ignored); see `.env.example`.

### eBay scanner

1. Get a Production keyset at [developer.ebay.com](https://developer.ebay.com) (Application Keysets). For the
   "Marketplace Account Deletion" step, the app stores no eBay user accounts, only public listing summaries.
2. Copy `.env.example` to `.env`, fill in `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`, restart the app.
3. Source Manager → **eBay search profiles**: add search terms, exclusions, price range, listing types and a
   frequency. Use **Run now** for a first test. Results appear in the Deal Scanner, matched and scored.

The scanner uses the official Browse API (active listings only). It never buys, bids or watches, and it cannot
see sold prices: eBay's sold-data API (Marketplace Insights) is Limited Release. Scans run only while the app is open.

### Backups

Settings → **Download backup** produces a consistent SQLite copy. To restore, stop the app and copy the file over `data/live.sqlite`.

### Tests

```bash
python -m pytest
```

Tests cover the 15 acceptance criteria in spec §24 (named `test_01_…` to `test_15_…`) plus an HTTP end-to-end workflow
(`tests/test_web.py::test_end_to_end_workflow`).

## Layout

```
app/
  models.py          SQLAlchemy models (money in integer pence)
  services/          domain logic: catalogue, valuation, fees, deals, purchasing, inventory, sales, ledger, finance,
                     imports, sources (registry + scan runner), safety (URL/SSRF), demo
  main.py            FastAPI routes (HTML pages + /api/*)
  templates/, static/  UI (Jinja, vanilla JS, vendored Chart.js)
alembic/             migrations
samples/             CSV import templates
tests/
```
