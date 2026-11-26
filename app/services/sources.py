"""Source registry (honest implementation status) and the scan job runner / adapter framework."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ScanJob, Source, utcnow

# status: working = usable now; partial = some methods work; planned = not built; restricted = no permitted automation
W, P, PL, R = "working", "partial", "planned", "restricted"
SOURCES = [
    # key, name, kind, country, methods, status, note, auth
    ("manual", "Manual entry", "manual", "GB", "manual", W, "Type in any listing or private deal.", None),
    ("csv", "CSV / JSON import", "manual", "GB", "csv", W, "Import listings exported or collected from any source.", None),
    ("ebay_uk", "eBay UK", "marketplace", "GB", "official_api,manual_url,email_alerts,csv", P,
     "Manual/CSV work now. Browse API adapter (active listings) is Phase 3 and needs your eBay developer keys. "
     "Sold-price API (Marketplace Insights) is Limited Release: not available without eBay approval.",
     "eBay developer app keys (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET)"),
    ("ebay_product_research", "eBay Product Research (Terapeak)", "research", "GB", "manual_verified", P,
     "Free in Seller Hub, up to 3 years of eBay sales. No public API found: record verified comps by hand or CSV.",
     "eBay seller account (in browser)"),
    ("facebook_marketplace", "Facebook Marketplace", "marketplace", "GB", "manual_url", R,
     "No public listing API; automated collection is not permitted. Use manual URL submission.", None),
    ("gumtree", "Gumtree", "marketplace", "GB", "manual_url,email_alerts", PL,
     "Manual now; saved-search email alert import planned (Phase 5).", None),
    ("vinted_uk", "Vinted UK", "marketplace", "GB", "manual_url", PL, "No public API known. Manual URL.", None),
    ("abebooks_uk", "AbeBooks UK", "marketplace", "GB", "manual_url", PL, "Investigating permitted feeds/APIs.", None),
    ("bookfinder", "BookFinder", "marketplace", "GB", "manual_url", PL, "Manual URL.", None),
    ("the_saleroom", "The Saleroom", "auction", "GB", "manual_url,email_alerts", PL,
     "Saved-search email alerts planned; fees vary per auctioneer.", None),
    ("easy_live", "Easy Live Auction", "auction", "GB", "manual_url,email_alerts", PL, "As The Saleroom.", None),
    ("vectis", "Vectis", "auction", "GB", "manual_url", PL, "Manual URL; check terms before any automation.", None),
    ("ewbanks", "Ewbank's", "auction", "GB", "manual_url", PL, "Manual URL.", None),
    ("excalibur", "Excalibur Auctions", "auction", "GB", "manual_url", PL, "Manual URL.", None),
    ("simon_charles", "Simon Charles", "auction", "GB", "manual_url", PL, "Manual URL.", None),
    ("john_pye", "John Pye", "auction", "GB", "manual_url", PL, "Manual URL.", None),
    ("southgate", "Southgate Auction Rooms", "auction", "GB", "manual_url", PL, "Manual URL.", None),
    ("pump_house", "Pump House Auctions", "auction", "GB", "manual_url", PL, "Manual URL.", None),
    ("andover", "Andover Auctions", "auction", "GB", "manual_url", PL, "Manual URL.", None),
    ("cullens", "Cullens", "auction", "GB", "manual_url", PL, "Manual URL.", None),
    ("the_outpost", "The Outpost", "retailer", "GB", "manual_url", PL, "Manual URL.", None),
    ("firestorm", "Firestorm Games", "retailer", "GB", "manual_url", PL, "Manual URL.", None),
    ("zatu", "Zatu Games", "retailer", "GB", "manual_url", PL, "Manual URL.", None),
    ("element_games", "Element Games", "retailer", "GB", "manual_url", PL, "Manual URL.", None),
    ("wayland", "Wayland Games", "retailer", "GB", "manual_url", PL, "Manual URL.", None),
    ("goblin_gaming", "Goblin Gaming", "retailer", "GB", "manual_url", PL, "Manual URL.", None),
    ("hobby_workshop", "Hobby Workshop", "retailer", "GB", "manual_url", PL, "Manual URL.", None),
    ("troll_trader", "Troll Trader", "retailer", "GB", "manual_url", PL, "Manual URL.", None),
    ("wargame_compare", "Wargame Compare", "retailer", "GB", "manual_url", PL, "Comparison site: check terms first.", None),
    ("warzilla", "Warzilla", "retailer", "GB", "manual_url", PL, "Comparison site: check terms first.", None),
    ("hachette", "Hachette Partworks", "retailer", "GB", "manual", PL, "Manual.", None),
    ("allegro", "Allegro", "marketplace", "PL", "official_api,manual_url", PL,
     "Official REST API exists; listing-search access for new apps to be confirmed (Phase 6).", "Allegro app registration"),
    ("olx_pl", "OLX Poland", "marketplace", "PL", "manual_url", PL, "Manual URL.", None),
    ("mandarake", "Mandarake", "marketplace", "JP", "manual_url", PL, "Manual URL + landed-cost calculator.", None),
    ("surugaya", "Suruga-ya", "marketplace", "JP", "manual_url", PL, "Manual URL.", None),
    ("buyee", "Buyee", "marketplace", "JP", "manual_url", PL, "Manual URL; add proxy fees as costs.", None),
    ("local", "Local / private / car boot / charity", "manual", "GB", "manual", W, "Manual entry.", None),
]


def seed_sources(s: Session) -> None:
    existing = {k for (k,) in s.execute(select(Source.key))}
    for key, name, kind, country, methods, status, note, auth in SOURCES:
        if key in existing:
            continue
        s.add(Source(key=key, name=name, kind=kind, country=country, methods=methods, status=status, status_note=note,
                     enabled=status == W, auth_required=auth))
    s.flush()


# ---- Adapter framework ------------------------------------------------------------------------
class SourceAdapter(Protocol):
    """A source adapter yields normalised ListingIn objects. It must only use permitted access methods."""
    source_key: str

    def fetch(self, params: dict) -> Iterable:  # -> Iterable[ListingIn]
        ...


ADAPTERS: dict[str, Callable[[], SourceAdapter]] = {}


def register(key: str, factory: Callable[[], SourceAdapter]) -> None:
    ADAPTERS[key] = factory


class TransientError(Exception):
    """Errors worth retrying (timeouts, 5xx, rate limits)."""


@dataclass
class ScanOutcome:
    job: ScanJob
    new: int
    seen: int


def run_scan(s: Session, source_key: str, params: dict | None = None, adapter: SourceAdapter | None = None,
             backoff_s: float = 0.0) -> ScanOutcome:
    """Run one scan job with retries. Failures are stored on the job and the source, never swallowed."""
    from app.services.deals import ingest

    source = s.scalar(select(Source).where(Source.key == source_key))
    if source is None:
        raise ValueError(f"Unknown source {source_key}")
    job = ScanJob(source_id=source.id, status="running", started_at=utcnow(), params=params or {})
    s.add(job)
    s.flush()
    source.last_attempt_at = utcnow()
    if adapter is None:
        factory = ADAPTERS.get(source_key)
        if factory is None:
            job.status, job.error, job.finished_at = "failed", "No adapter implemented for this source", utcnow()
            source.last_error = job.error
            s.commit()
            return ScanOutcome(job, 0, 0)
        adapter = factory()
    while True:
        job.attempts += 1
        try:
            new = seen = 0
            with s.begin_nested():
                for item in adapter.fetch(params or {}):
                    s.refresh(job)
                    if job.status == "cancelled":
                        break
                    seen += 1
                    if ingest(s, item).created:
                        new += 1
            if job.status != "cancelled":
                job.status = "succeeded"
            job.items_seen, job.items_new = seen, new
            source.last_success_at, source.last_error = utcnow(), None
            break
        except TransientError as e:
            job.error = f"Attempt {job.attempts}: {e}"
            if job.attempts >= job.max_attempts:
                job.status = "failed"
                source.last_error = job.error
                break
            time.sleep(backoff_s * (2 ** (job.attempts - 1)))
        except Exception as e:  # permanent failure: record and stop
            job.status, job.error = "failed", f"{type(e).__name__}: {e}"
            source.last_error = job.error
            break
    job.finished_at = utcnow()
    s.commit()
    return ScanOutcome(job, job.items_new, job.items_seen)


def cancel_scan(s: Session, job: ScanJob) -> None:
    if job.status in ("queued", "running"):
        job.status = "cancelled"
        job.finished_at = utcnow()
