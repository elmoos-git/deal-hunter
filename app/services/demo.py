"""Demo workspace: synthetic, clearly labelled data in a SEPARATE database file. Never touches live data."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from app.config import get_settings
from app.db import _engines, migrate, session_factory
from app.services import catalogue, deals, ledger, purchasing, sales
from app.services import valuation as val
from app.services.sources import seed_sources

DEMO_NOTE = "Synthetic demo data — not real market prices."


def _require_demo():
    cfg = get_settings()
    if not cfg.is_demo:
        raise RuntimeError("Demo data can only be loaded into the demo workspace (DEAL_HUNTER_WORKSPACE=demo)")
    return cfg


def seed() -> None:
    cfg = _require_demo()
    migrate(cfg.database_url)
    s = session_factory(cfg.database_url)()
    try:
        seed_sources(s)
        if s.query(val.Product).count():
            return
        today = date.today()
        ledger.add_capital(s, 40000, today - timedelta(days=60), "Initial capital (demo)")
        mk = lambda name, **kw: catalogue.create_product(s, name=f"[DEMO] {name}", notes=DEMO_NOTE, provenance="demo", **kw)
        trooper = mk("Trooper Squad", category="Warhammer miniatures", game_system="Demo System", faction="Demo Faction A",
                     aliases=["demo troopers"])
        walker = mk("Assault Walker", category="Warhammer miniatures", game_system="Demo System", faction="Demo Faction A",
                    aliases=["demo walker"])
        hero_a = mk("Commander (build A)", category="Warhammer miniatures", game_system="Demo System")
        hero_b = mk("Commander (build B)", category="Warhammer miniatures", game_system="Demo System")
        box = mk("Patrol Box", category="Warhammer miniatures", game_system="Demo System", faction="Demo Faction A",
                 aliases=["demo patrol box", "demo patrol"])
        book = mk("Collector Hardback Novel", category="Warhammer books", author="Demo Author", book_format="Hardback",
                  aliases=["demo hardback"])
        catalogue.set_bundle_components(s, box, [
            {"product_id": trooper.id, "quantity": 2}, {"product_id": walker.id, "quantity": 1},
            {"product_id": hero_a.id, "quantity": 1, "shared_sprue_group": "commander"},
            {"product_id": hero_b.id, "quantity": 1, "shared_sprue_group": "commander"}])
        prices = {trooper: [2200, 2400, 2300, 2500, 2350, 2450, 2250, 2600], walker: [3100, 3300, 3000, 3400, 3200, 3150],
                  hero_a: [1400, 1500, 1350, 1600], hero_b: [1700, 1800, 1650, 1750, 1900],
                  box: [7200, 7500, 6900, 7800, 7400, 7100, 7600, 7300, 7000],
                  book: [4500, 6000, 5200, 4800]}
        for p, vals in prices.items():
            for i, price in enumerate(vals):
                val.add_observation(s, product_id=p.id, observation_type="sold", observed_on=today - timedelta(days=3 + i * 7),
                                    price_minor=price, platform="ebay_uk", provenance="demo", condition="sealed",
                                    data_label="test", title=f"DEMO sale {i}")
            val.compute(s, p, "sealed")
        po = purchasing.record_purchase(s, purchasing.PurchaseIn(
            source_key="manual", order_ref="DEMO-PO-1", purchased_on=today - timedelta(days=40), shipping_p=450,
            lines=[purchasing.LineIn("[DEMO] Patrol Box", 5200, 1, box.id, "sealed"),
                   purchasing.LineIn("[DEMO] Collector Hardback Novel", 2500, 1, book.id, "sealed")],
            notes=DEMO_NOTE, status="received"))
        book_item = [i for i in s.query(purchasing.InventoryItem).all() if i.product_id == book.id][0]
        sales.record_sale(s, sales.SaleIn("ebay_uk", "DEMO-SALE-1", today - timedelta(days=12),
                                          [sales.SaleLineIn(book_item.id, 1, 5200)], buyer_shipping_p=399,
                                          platform_fees_p=720, postage_p=399, packaging_p=60))
        for title, price, cond, kind in [("[DEMO] Patrol Box sealed", 4800, "sealed", "fixed"),
                                         ("[DEMO] Trooper Squad NoS", 1100, "new_on_sprue", "fixed"),
                                         ("[DEMO] Assault Walker sealed", 1500, "sealed", "auction"),
                                         ("[DEMO] demo hardback signed", 3900, "sealed", "fixed")]:
            deals.ingest(s, deals.ListingIn(source_key="manual", title=title, price_minor=price, shipping_p=350,
                                            condition=cond, listing_type=kind, provenance="demo",
                                            description=DEMO_NOTE))
        s.commit()
        _ = po
    finally:
        s.close()


def wipe() -> None:
    """Delete the demo database file. Refuses to run outside the demo workspace."""
    cfg = _require_demo()
    eng = _engines.pop(cfg.database_url, None)
    if eng:
        eng.dispose()
    if cfg.database_url.startswith("sqlite:///"):
        path = Path(cfg.database_url.removeprefix("sqlite:///"))
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
