"""Search profiles and the background scheduler that runs them."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import ScanJob, SearchProfile, utcnow
from app.services.sources import ScanOutcome, run_scan

log = logging.getLogger("deal_hunter.scheduler")

# Starter search terms from the spec. Users add their own; these are just suggestions in the UI.
SUGGESTED_QUERIES = [
    "warhammer new on sprue", "warhammer (nos, \"new on sprue\")", "warhammer combat patrol sealed",
    "warhammer battleforce", "games workshop job lot", "warhammer collection unassembled",
    "black library hardback limited edition", "black library out of print", "kill team box sealed",
]


def params_for(p: SearchProfile) -> dict:
    return {"query": p.query, "excluded_keywords": p.excluded_keywords or "", "max_price_p": p.max_price_p,
            "min_price_p": p.min_price_p, "listing_types": p.listing_types, "new_only": p.new_only,
            "uk_delivery": p.uk_delivery, "max_results": p.max_results, "product_id": p.product_id}


def is_running(s: Session, profile_id: int) -> bool:
    return bool(s.scalar(select(ScanJob.id).where(ScanJob.profile_id == profile_id, ScanJob.status == "running")))


def run_profile(s: Session, p: SearchProfile, adapter=None, backoff_s: float = 2.0) -> ScanOutcome | None:
    if is_running(s, p.id):
        return None
    p.last_run_at = utcnow()
    s.commit()
    return run_scan(s, p.source_key, params_for(p), adapter=adapter, backoff_s=backoff_s, profile_id=p.id)


def due(s: Session, now: datetime | None = None) -> list[SearchProfile]:
    now = now or utcnow()
    out = []
    for p in s.scalars(select(SearchProfile).where(SearchProfile.enabled.is_(True))):
        if p.last_run_at is None or p.last_run_at + timedelta(minutes=max(p.frequency_min, 15)) <= now:
            out.append(p)
    return out


def recover_interrupted(s: Session) -> int:
    """Jobs left 'running' by a crash or restart are marked failed so they are visible and the profile can rerun."""
    n = 0
    for j in s.scalars(select(ScanJob).where(ScanJob.status == "running")):
        j.status, j.error, j.finished_at = "failed", "Interrupted (app stopped while scanning)", utcnow()
        n += 1
    s.commit()
    return n


class Scheduler:
    """One background thread; checks for due profiles every `tick_s` seconds. Scans run one at a time."""

    def __init__(self, factory: sessionmaker, tick_s: float = 60.0):
        self.factory, self.tick_s = factory, tick_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self.factory() as s:
            recover_interrupted(s)
        self._thread = threading.Thread(target=self._loop, name="deal-hunter-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> int:
        ran = 0
        with self.factory() as s:
            for p in due(s):
                try:
                    out = run_profile(s, p)
                    if out:
                        ran += 1
                        log.info(f"scan profile={p.id} status={out.job.status} seen={out.seen} new={out.new}")
                except Exception as e:  # never let one profile kill the scheduler
                    s.rollback()
                    log.error(f"scan profile={p.id} crashed: {e}")
        return ran

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            self.tick()
