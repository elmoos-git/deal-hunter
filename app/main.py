"""FastAPI application: server-rendered pages + JSON API over the same services."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import ROOT, get_settings
from app.db import migrate, session_factory
from app.models import (
    CONDITION_LABELS, CONDITIONS, INVENTORY_STATUSES, Alert, AuditEvent, CostAllocation, DealOpportunity, Expense,
    FinancialTransaction, ImportJob, ImportReview, InventoryItem, MarketListing, Product, PurchaseOrder, SalesOrder,
    ScanJob, SearchProfile, SoldObservation, Source, Valuation, WatchlistEntry,
)
from app.money import MoneyError, fmt, to_pence
from app.services import catalogue, deals, ebay, finance, imports, inventory, ledger, purchasing, sales, scanning
from app.services.scanning import Scheduler
from app.services import valuation as val
from app.services.audit import DEFAULT_SETTINGS, all_settings, set_setting
from app.services.fees import AuctionFeeSchedule, PremiumTier, auction_cost, max_hammer, selling_costs
from app.services.safety import UnsafeURL, fetch_listing_metadata
from app.services.sources import seed_sources

log = logging.getLogger("deal_hunter")
logging.basicConfig(level=logging.INFO, format='{"t":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}')

NAV = [
    ("Overview", [("/", "Command Centre", "home"), ("/deals", "Deal Scanner", "radar"), ("/watchlist", "Watchlist", "eye")]),
    ("Intelligence", [("/products", "Product Database", "box"), ("/market", "Market Intelligence", "chart"),
                      ("/analyser", "Auction & Listing Analyser", "gavel"), ("/split", "Box Split Calculator", "split")]),
    ("Business", [("/inventory", "Inventory", "stack"), ("/sales", "Sales", "tag"), ("/finance", "Finance & Analytics", "pound")]),
    ("System", [("/imports", "Imports", "upload"), ("/sources", "Source Manager", "plug"), ("/settings", "Settings", "cog")]),
]


def create_app(database_url: str | None = None, scheduler: bool | None = None) -> FastAPI:
    cfg = get_settings()
    url = database_url or cfg.database_url
    migrate(url)
    factory = session_factory(url)
    with factory() as s:
        seed_sources(s)
        s.commit()
    is_demo = cfg.is_demo if database_url is None else ("demo" in Path(url).name)
    ebay.register()
    if scheduler is None:
        scheduler = database_url is None and os.environ.get("DEAL_HUNTER_SCHEDULER", "1") != "0"
    if scheduler:
        Scheduler(factory).start()

    app = FastAPI(title="Gaming Collectibles Deal Hunter", docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "app" / "templates")
    templates.env.filters["gbp"] = fmt
    templates.env.filters["cond"] = lambda c: CONDITION_LABELS.get(c, c or "—")
    templates.env.filters["d"] = lambda d: d.strftime("%d %b %Y") if d else "—"
    templates.env.filters["dt"] = lambda d: d.strftime("%d %b %H:%M") if d else "—"
    templates.env.filters["tojson_safe"] = lambda v: json.dumps(v).replace("<", "\\u003c")

    def db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    def render(request: Request, s: Session, name: str, **ctx) -> HTMLResponse:
        ctx.update(request=request, nav=NAV, demo=is_demo, conditions=CONDITIONS,
                   msg=request.query_params.get("msg"), err=request.query_params.get("err"),
                   alert_count=s.scalar(select(func.count(Alert.id)).where(Alert.read_at.is_(None))),
                   active=ctx.get("active", "/" + request.url.path.strip("/").split("/")[0] if request.url.path != "/" else "/"),
                   today=date.today())
        return templates.TemplateResponse(request, name, ctx)

    def go(path: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
        q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
        path, _, frag = path.partition("#")
        return RedirectResponse(path + ("?" + urlencode(q) if q else "") + ("#" + frag if frag else ""), status_code=303)

    async def form(request: Request) -> dict:
        f = await request.form()
        return {k: (v.strip() if isinstance(v, str) else v) for k, v in f.multi_items()}

    def p(v: str | None, default: int | None = None) -> int | None:
        return to_pence(v) if v not in (None, "") else default

    def d(v: str | None) -> date:
        return date.fromisoformat(v) if v else date.today()

    @app.exception_handler(MoneyError)
    async def money_err(request: Request, exc: MoneyError):
        return go(request.headers.get("referer", "/").split("?")[0].replace(str(request.base_url).rstrip("/"), "") or "/", err=str(exc))

    # ---------------- Command Centre ----------------
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, s: Session = Depends(db)):
        dash = finance.compute(s)
        opps = list(s.scalars(select(DealOpportunity).where(DealOpportunity.status.in_(("new", "saved")))
                              .order_by(desc(DealOpportunity.score)).limit(8)))
        auctions = list(s.scalars(select(MarketListing).where(MarketListing.listing_type == "auction",
                                                              MarketListing.ends_at >= datetime.now())
                                  .order_by(MarketListing.ends_at).limit(6)))
        alerts = list(s.scalars(select(Alert).where(Alert.read_at.is_(None)).order_by(desc(Alert.id)).limit(6)))
        m = dash.monthly
        charts = {
            "c_cash": {"type": "line", "labels": [x["date"] for x in dash.capital_series],
                       "series": [{"label": "Cash balance", "data": [x["value_p"] for x in dash.capital_series]}]},
            "c_revexp": {"type": "bar", "labels": [x["month"] for x in m],
                         "series": [{"label": "Revenue", "data": [x["revenue_p"] for x in m], "color": 0},
                                    {"label": "Stock purchases", "data": [x["purchases_p"] for x in m], "color": 1},
                                    {"label": "Overheads", "data": [x["expenses_p"] for x in m], "color": 4}]},
            "c_profit": {"type": "bar", "labels": [x["month"] for x in m],
                         "series": [{"label": "Realised profit", "data": [x["profit_p"] for x in m], "color": 2}]},
            "c_cat": {"type": "doughnut", "labels": [x["label"] for x in dash.by_category],
                      "series": [{"label": "Cost", "data": [x["cost_p"] for x in dash.by_category]}]},
            "c_age": {"type": "bar", "labels": [x["label"] for x in dash.ageing] if dash.units_on_hand else [],
                      "series": [{"label": "Cost held", "data": [x["cost_p"] for x in dash.ageing], "color": 1}]},
            "c_src": {"type": "bar", "labels": [x["label"] for x in dash.by_source],
                      "series": [{"label": "Profit", "data": [x["profit_p"] for x in dash.by_source], "color": 3}]},
            "c_pcat": {"type": "bar", "labels": [x["label"] for x in dash.profit_by_category],
                       "series": [{"label": "Profit", "data": [x["profit_p"] for x in dash.profit_by_category], "color": 2}]},
        }
        needs_capital = dash.capital_in_p == 0
        return render(request, s, "home.html", title="Command Centre", dash=dash, opps=opps, auctions=auctions,
                      alerts=alerts, charts=charts, needs_capital=needs_capital,
                      budget=all_settings(s)["budget"], initial=all_settings(s)["initial_capital_p"])

    # ---------------- Search & alerts ----------------
    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = "", s: Session = Depends(db)):
        prods = catalogue.search(s, q, 30) if q else []
        items = list(s.scalars(select(InventoryItem).where(or_(InventoryItem.sku.ilike(f"%{q}%"),
                                                                InventoryItem.name.ilike(f"%{q}%"))).limit(30))) if q else []
        listings = list(s.scalars(select(MarketListing).where(MarketListing.title.ilike(f"%{q}%")).limit(30))) if q else []
        return render(request, s, "search.html", title="Search", q=q, prods=prods, items=items, listings=listings)

    @app.get("/alerts", response_class=HTMLResponse)
    def alerts_page(request: Request, s: Session = Depends(db)):
        rows = list(s.scalars(select(Alert).order_by(desc(Alert.id)).limit(200)))
        return render(request, s, "alerts.html", title="Notifications", rows=rows)

    @app.post("/alerts/read")
    def alerts_read(s: Session = Depends(db)):
        for a in s.scalars(select(Alert).where(Alert.read_at.is_(None))):
            a.read_at = datetime.now()
        s.commit()
        return go("/alerts", "All notifications marked read")

    # ---------------- Deal Scanner ----------------
    SORTS = {"score": desc(DealOpportunity.score), "profit": desc(DealOpportunity.profit_expected_p),
             "roi": desc(DealOpportunity.roi_pct), "cost": DealOpportunity.acquisition_p,
             "new": desc(DealOpportunity.created_at)}

    @app.get("/deals", response_class=HTMLResponse)
    def deals_page(request: Request, status: str = "open", sort: str = "score", source: str = "", min_score: int = 0,
                   confidence: str = "", s: Session = Depends(db)):
        stmt = select(DealOpportunity).join(MarketListing).options(
            selectinload(DealOpportunity.listing).selectinload(MarketListing.source),
            selectinload(DealOpportunity.product).selectinload(Product.category))
        if status == "open":
            stmt = stmt.where(DealOpportunity.status.in_(("new", "saved", "watching")))
        elif status != "all":
            stmt = stmt.where(DealOpportunity.status == status)
        if source:
            stmt = stmt.join(Source).where(Source.key == source)
        if min_score:
            stmt = stmt.where(DealOpportunity.score >= min_score)
        if confidence:
            stmt = stmt.where(DealOpportunity.confidence == confidence)
        rows = list(s.scalars(stmt.order_by(SORTS.get(sort, SORTS["score"])).limit(300)))
        srcs = list(s.scalars(select(Source).order_by(Source.name)))
        return render(request, s, "deals.html", title="Deal Scanner", rows=rows, srcs=srcs,
                      f={"status": status, "sort": sort, "source": source, "min_score": min_score, "confidence": confidence},
                      products=list(s.scalars(select(Product).order_by(Product.name))))

    @app.post("/deals/add")
    async def deals_add(request: Request, s: Session = Depends(db)):
        f = await form(request)
        try:
            r = deals.ingest(s, deals.ListingIn(
                source_key=f.get("source") or "manual", title=f.get("title", ""), price_minor=p(f.get("price"), 0),
                shipping_p=p(f.get("shipping"), 0), url=f.get("url") or None, description=f.get("description") or None,
                condition=f.get("condition") or "unknown", listing_type=f.get("listing_type") or "fixed",
                quantity=int(f.get("quantity") or 1), seller=f.get("seller") or None,
                ends_at=datetime.fromisoformat(f["ends_at"]) if f.get("ends_at") else None,
                product_id=int(f["product_id"]) if f.get("product_id") else None, image_url=f.get("image_url") or None))
            s.commit()
        except (ValueError, UnsafeURL) as e:
            s.rollback()
            return go("/deals", err=str(e))
        return go(f"/deals/{r.opportunity.id}", "Listing added" if r.created else "Listing already known: updated, no duplicate created")

    @app.get("/deals/{oid}", response_class=HTMLResponse)
    def deal_detail(oid: int, request: Request, s: Session = Depends(db)):
        o = s.get(DealOpportunity, oid) or _404()
        pf = deals.profitability(s, o.listing)
        candidates = catalogue.match_title(s, o.listing.title)
        from app.models import ListingSnapshot
        snaps = list(s.scalars(select(ListingSnapshot).where(ListingSnapshot.listing_id == o.listing_id).order_by(ListingSnapshot.id)))
        dup = s.get(MarketListing, o.listing.possible_duplicate_of) if o.listing.possible_duplicate_of else None
        return render(request, s, "deal_detail.html", title="Opportunity", o=o, pf=pf, candidates=candidates, snaps=snaps,
                      dup=dup, active="/deals", products=list(s.scalars(select(Product).order_by(Product.name))))

    @app.post("/deals/{oid}/status")
    async def deal_status(oid: int, request: Request, s: Session = Depends(db)):
        f = await form(request)
        o = s.get(DealOpportunity, oid) or _404()
        try:
            deals.set_status(s, o, f.get("status", ""), f.get("reason"))
            if f.get("status") == "watching" and o.product_id:
                s.add(WatchlistEntry(listing_id=o.listing_id, product_id=o.product_id, notes="From deal feed"))
            s.commit()
        except ValueError as e:
            return go(f"/deals/{oid}", err=str(e))
        return go(f"/deals/{oid}", f"Marked {o.status}")

    @app.post("/deals/{oid}/identify")
    async def deal_identify(oid: int, request: Request, s: Session = Depends(db)):
        f = await form(request)
        o = s.get(DealOpportunity, oid) or _404()
        deals.reidentify(s, o, int(f["product_id"]) if f.get("product_id") else None)
        s.commit()
        return go(f"/deals/{oid}", "Product identification updated and re-scored")

    @app.post("/deals/rescore")
    def deals_rescore(s: Session = Depends(db)):
        n = 0
        for o in s.scalars(select(DealOpportunity).where(DealOpportunity.status.in_(("new", "saved", "watching")))):
            deals.evaluate(s, o.listing)
            n += 1
        s.commit()
        return go("/deals", f"Re-scored {n} open opportunities with current valuations and settings")

    @app.post("/deals/fetch")
    async def deals_fetch(request: Request, s: Session = Depends(db)):
        """URL-first: try to read public page metadata, then prefill the add form. Falls back to manual entry."""
        f = await form(request)
        try:
            meta = fetch_listing_metadata(f.get("url", ""))
            q = {"prefill_title": meta.get("title") or "", "prefill_url": meta.get("final_url") or "",
                 "prefill_price": meta.get("price") or "", "prefill_image": meta.get("image_url") or "",
                 "msg": "Fetched what the page exposes publicly. Check every field before saving."}
        except Exception as e:  # network, blocked, not HTML, unsafe
            q = {"prefill_url": f.get("url", ""), "err": f"Could not read that page automatically ({e}). Enter the details manually."}
        return RedirectResponse("/deals?" + urlencode(q) + "#add", status_code=303)

    # ---------------- Watchlist ----------------
    @app.get("/watchlist", response_class=HTMLResponse)
    def watchlist(request: Request, s: Session = Depends(db)):
        rows = list(s.scalars(select(WatchlistEntry).order_by(desc(WatchlistEntry.id))))
        return render(request, s, "watchlist.html", title="Watchlist", rows=rows,
                      products=list(s.scalars(select(Product).order_by(Product.name))))

    @app.post("/watchlist/add")
    async def watch_add(request: Request, s: Session = Depends(db)):
        f = await form(request)
        if not f.get("product_id"):
            return go("/watchlist", err="Choose a product")
        s.add(WatchlistEntry(product_id=int(f["product_id"]), target_price_p=p(f.get("target")),
                             priority=f.get("priority") or "normal", notes=f.get("notes") or None))
        s.commit()
        return go("/watchlist", "Added to watchlist")

    @app.post("/watchlist/{wid}/delete")
    def watch_del(wid: int, s: Session = Depends(db)):
        w = s.get(WatchlistEntry, wid)
        if w:
            s.delete(w)
            s.commit()
        return go("/watchlist", "Removed")

    # ---------------- Products ----------------
    @app.get("/products", response_class=HTMLResponse)
    def products(request: Request, q: str = "", s: Session = Depends(db)):
        rows = catalogue.search(s, q, 500)
        current = {(v.product_id): v for v in s.scalars(select(Valuation).where(Valuation.is_current.is_(True),
                                                                                  Valuation.window_days == 90))}
        return render(request, s, "products.html", title="Product Database", rows=rows, q=q, current=current)

    @app.post("/products/new")
    async def product_new(request: Request, s: Session = Depends(db)):
        f = await form(request)
        try:
            prod = catalogue.create_product(
                s, name=f.get("name", ""), category=f.get("category") or None,
                aliases=[a.strip() for a in f.get("aliases", "").split("|") if a.strip()],
                identifiers={k: f[k.lower()] for k in ("EAN", "UPC", "ISBN") if f.get(k.lower())},
                manufacturer=f.get("manufacturer") or None, game_system=f.get("game_system") or None,
                faction=f.get("faction") or None, edition=f.get("edition") or None, sku=f.get("sku") or None,
                rrp_p=p(f.get("rrp")), status=f.get("status") or "unknown",
                release_year=int(f["release_year"]) if f.get("release_year") else None,
                author=f.get("author") or None, series=f.get("series") or None, book_format=f.get("book_format") or None,
                official_url=f.get("official_url") or None, notes=f.get("notes") or None)
            s.commit()
        except ValueError as e:
            s.rollback()
            return go("/products", err=str(e))
        return go(f"/products/{prod.id}", "Product created")

    @app.get("/products/{pid}", response_class=HTMLResponse)
    def product_detail(pid: int, request: Request, window: int = 90, s: Session = Depends(db)):
        prod = s.get(Product, pid) or _404()
        obs = list(s.scalars(select(SoldObservation).where(SoldObservation.product_id == pid).order_by(desc(SoldObservation.observed_on))))
        groups = sorted({val.condition_group(o.condition) for o in obs}) or ["new"]
        vals = {}
        for g in groups:
            rep = next(k for k, v in val.CONDITION_GROUPS.items() if v == g)
            vals[g] = {w: (val.current(s, pid, rep, w) or val.compute(s, prod, rep, w)) for w in val.WINDOWS}
        s.commit()
        used_in = [c for c in s.scalars(select(catalogue.BundleComponent).where(catalogue.BundleComponent.component_id == pid))]
        history = list(s.scalars(select(AuditEvent).where(AuditEvent.entity == "product", AuditEvent.entity_id == pid)
                                 .order_by(desc(AuditEvent.id)).limit(20)))
        chart = {"type": "line", "labels": [o.observed_on.isoformat() for o in reversed(obs) if o.observation_type in val.COMPLETED_TYPES],
                 "series": [{"label": "Sold price", "data": [o.price_p for o in reversed(obs) if o.observation_type in val.COMPLETED_TYPES]}]}
        return render(request, s, "product_detail.html", title=prod.name, prod=prod, obs=obs, vals=vals, window=window,
                      group_labels=val.GROUP_LABELS, used_in=used_in, history=history, chart=chart, active="/products",
                      all_products=list(s.scalars(select(Product).where(Product.id != pid).order_by(Product.name))))

    @app.post("/products/{pid}/alias")
    async def product_alias(pid: int, request: Request, s: Session = Depends(db)):
        f = await form(request)
        prod = s.get(Product, pid) or _404()
        try:
            if f.get("alias"):
                catalogue.add_alias(s, prod, f["alias"])
            if f.get("id_value"):
                catalogue.add_identifier(s, prod, f.get("id_kind") or "EAN", f["id_value"])
            s.commit()
        except ValueError as e:
            s.rollback()
            return go(f"/products/{pid}", err=str(e))
        return go(f"/products/{pid}", "Saved")

    @app.post("/products/{pid}/components")
    async def product_components(pid: int, request: Request, s: Session = Depends(db)):
        f = await form(request)
        prod = s.get(Product, pid) or _404()
        comps = []
        ids = (await request.form()).getlist("comp_id")
        qtys = (await request.form()).getlist("comp_qty")
        groups = (await request.form()).getlist("comp_group")
        sellable = set((await request.form()).getlist("comp_sellable"))
        for i, cid in enumerate(ids):
            if cid:
                comps.append({"product_id": int(cid), "quantity": int(qtys[i] or 1), "shared_sprue_group": groups[i] or None,
                              "separately_sellable": str(i) in sellable})
        try:
            catalogue.set_bundle_components(s, prod, comps)
            s.commit()
        except ValueError as e:
            s.rollback()
            return go(f"/products/{pid}", err=str(e))
        _ = f
        return go(f"/products/{pid}", "Box contents saved")

    @app.post("/products/{pid}/observation")
    async def product_obs(pid: int, request: Request, s: Session = Depends(db)):
        f = await form(request)
        prod = s.get(Product, pid) or _404()
        back = f.get("back") or f"/products/{pid}"
        try:
            o = val.add_observation(
                s, product_id=pid, observation_type=f.get("type") or "sold", observed_on=d(f.get("date")),
                price_minor=p(f.get("price")), shipping_p=p(f.get("shipping")), platform=f.get("platform") or "ebay_uk",
                provenance=f.get("provenance") or "manual_verified", condition=f.get("condition") or "unknown",
                source_url=f.get("url") or None, title=f.get("title") or None, edition=f.get("edition") or None,
                data_label=f.get("data_label") or "verified", is_multi_item_lot=bool(f.get("multi_lot")),
                offer_accepted_unconfirmed=bool(f.get("offer_unconfirmed")))
            if o is None:
                return go(back, err="That observation is already recorded")
            for w in val.WINDOWS:
                val.compute(s, prod, o.condition, w)
            s.commit()
        except (ValueError, TypeError) as e:
            s.rollback()
            return go(back, err=str(e))
        return go(back, "Observation recorded and valuation recalculated")

    @app.post("/observations/{oid}/exclude")
    async def obs_exclude(oid: int, request: Request, s: Session = Depends(db)):
        f = await form(request)
        o = s.get(SoldObservation, oid) or _404()
        o.excluded = not o.excluded
        o.exclude_reason = (f.get("reason") or "manually excluded") if o.excluded else None
        prod = s.get(Product, o.product_id)
        for w in val.WINDOWS:
            val.compute(s, prod, o.condition, w)
        s.commit()
        return go(f"/products/{o.product_id}", "Comparable updated")

    @app.post("/valuations/{vid}/override")
    async def val_override(vid: int, request: Request, s: Session = Depends(db)):
        f = await form(request)
        v = s.get(Valuation, vid) or _404()
        try:
            val.set_override(s, v, p(f.get("override")), f.get("reason", ""))
            s.commit()
        except ValueError as e:
            return go(f"/products/{v.product_id}", err=str(e))
        return go(f"/products/{v.product_id}", "Override saved (calculated value kept)")

    # ---------------- Market intelligence ----------------
    @app.get("/market", response_class=HTMLResponse)
    def market(request: Request, window: int = 90, s: Session = Depends(db)):
        rows = list(s.scalars(select(Valuation).where(Valuation.is_current.is_(True), Valuation.window_days == window)
                              .order_by(desc(Valuation.confidence_score))))
        prods = {pr.id: pr for pr in s.scalars(select(Product))}
        recent = list(s.scalars(select(SoldObservation).order_by(desc(SoldObservation.id)).limit(25)))
        return render(request, s, "market.html", title="Market Intelligence", rows=rows, prods=prods, window=window,
                      recent=recent, group_labels=val.GROUP_LABELS, products=sorted(prods.values(), key=lambda x: x.name))

    # ---------------- Analyser ----------------
    @app.get("/analyser", response_class=HTMLResponse)
    def analyser(request: Request, s: Session = Depends(db)):
        qp = request.query_params
        result = None
        if qp.get("resale"):
            try:
                tiers = []
                for i in range(3):
                    rate = qp.get(f"t{i}_rate")
                    if rate:
                        upto = qp.get(f"t{i}_upto")
                        tiers.append(PremiumTier(p(upto) if upto else None, int(round(float(rate) * 100))))
                sch = AuctionFeeSchedule(
                    name=qp.get("house") or "Custom", premium_tiers=tiers or [PremiumTier(None, 0)],
                    premium_marginal=qp.get("marginal") == "1", min_premium_p=p(qp.get("min_premium"), 0),
                    vat_on_premium=qp.get("vat_premium") == "1", vat_on_hammer=qp.get("vat_hammer") == "1",
                    online_surcharge_bp=int(round(float(qp.get("surcharge") or 0) * 100)),
                    online_surcharge_fixed_p=p(qp.get("surcharge_fixed"), 0), vat_on_surcharge=qp.get("vat_surcharge") == "1",
                    per_lot_fee_p=p(qp.get("lot_fee"), 0), payment_fee_bp=int(round(float(qp.get("payment_fee") or 0) * 100)),
                    shipping_p=p(qp.get("shipping"), 0), collection_p=p(qp.get("collection"), 0),
                    fx_and_import_p=p(qp.get("fx_import"), 0))
                st = all_settings(s)
                prof = st["selling_profiles"][st["selling_profile"]]
                post, pack = st["default_postage_p"], st["default_packaging_p"]
                scen = {}
                for k in ("conservative", "resale", "optimistic"):
                    gross = p(qp.get(k)) or p(qp.get("resale"))
                    scen[k] = (gross, gross - selling_costs(gross, prof, post, pack).total_p)
                bid = p(qp.get("bid"), 0)
                cost = auction_cost(bid, sch)
                tp, troi, tm = p(qp.get("target_profit"), 0), qp.get("target_roi"), qp.get("target_margin")
                net_c = scen["conservative"][1]
                result = {
                    "cost": cost, "scen": {k: (g, n, n - cost.total_p) for k, (g, n) in scen.items()},
                    "break_even": max_hammer(scen["resale"][1], sch, target_profit_p=0),
                    "max_safe": max_hammer(net_c, sch, target_profit_p=0),
                    "recommended": max_hammer(net_c, sch, target_profit_p=tp,
                                              target_roi_pct=float(troi) if troi else None,
                                              target_margin_pct=float(tm) if tm else None, gross_resale_p=scen["conservative"][0]),
                    "roi": round((scen["resale"][1] - cost.total_p) * 100 / cost.total_p, 1) if cost.total_p else None,
                    "margin": round((scen["resale"][1] - cost.total_p) * 100 / scen["resale"][0], 1) if scen["resale"][0] else None,
                }
            except (ValueError, MoneyError) as e:
                return render(request, s, "analyser.html", title="Auction & Listing Analyser", q=qp, result=None, error=str(e))
        return render(request, s, "analyser.html", title="Auction & Listing Analyser", q=qp, result=result, error=None)

    # ---------------- Box split ----------------
    @app.get("/split", response_class=HTMLResponse)
    def split(request: Request, product_id: int | None = None, s: Session = Depends(db)):
        bundles = list(s.scalars(select(Product).where(Product.is_bundle.is_(True)).order_by(Product.name)))
        calc = None
        qp = request.query_params
        if product_id:
            prod = s.get(Product, product_id) or _404()
            st = all_settings(s)
            prof = st["selling_profiles"][st["selling_profile"]]
            post, pack = p(qp.get("postage"), st["default_postage_p"]), p(qp.get("packaging"), st["default_packaging_p"])
            copies = int(qp.get("copies") or 1)
            cost = p(qp.get("cost"), prod.rrp_p or 0) * copies + p(qp.get("acq_costs"), 0)
            cond = qp.get("condition") or "sealed"
            fake_item = InventoryItem(product_id=prod.id, condition=cond)
            parts = inventory.plan_split(s, fake_item)
            lines = []
            for i, part in enumerate(parts):
                comp_v = val.current(s, part.product_id, cond) if part.product_id else None
                comp = s.get(Product, part.product_id) if part.product_id else None
                exp = p(qp.get(f"price_{i}"), part.unit_value_p if part.basis in ("expected_value", "manual_weight") else None)
                cons = comp_v.conservative_p if comp_v and comp_v.confidence != "insufficient" else None
                opt = comp_v.optimistic_p if comp_v and comp_v.confidence != "insufficient" else None
                units = part.quantity * copies
                unsold_pct = int(qp.get("unsold_pct") or 0)
                fee = selling_costs(exp or 0, prof, 0, 0).total_p if exp else None
                net_unit = (exp - fee - post - pack) if exp else None
                sold_units = units - (units * unsold_pct // 100)
                lines.append({"part": part, "units": units, "rrp": comp.rrp_p if comp else None, "cons": cons, "exp": exp,
                              "opt": opt, "fee": fee, "net_unit": net_unit, "net_total": net_unit * sold_units if net_unit is not None else None,
                              "confidence": comp_v.confidence if comp_v else "insufficient",
                              "spm": comp_v.sales_per_30d if comp_v else None, "sold_units": sold_units})
            known = [ln for ln in lines if ln["net_total"] is not None]
            total_net = sum(ln["net_total"] for ln in known)
            box_v = val.current(s, prod.id, cond) or val.compute(s, prod, cond)
            box_net = None
            if box_v.effective_expected_p and box_v.confidence != "insufficient":
                e = box_v.effective_expected_p
                box_net = (e - selling_costs(e, prof, post, pack).total_p) * copies
            # break-even: sort by net per unit desc, count units needed to recover cost
            units_sorted = sorted([ln["net_unit"] for ln in known for _ in range(ln["units"]) if ln["net_unit"] and ln["net_unit"] > 0], reverse=True)
            acc, need = 0, None
            for k, u in enumerate(units_sorted, 1):
                acc += u
                if acc >= cost:
                    need = k
                    break
            total_units = sum(ln["units"] for ln in lines)
            calc = {"prod": prod, "lines": lines, "cost": cost, "total_net": total_net, "profit": total_net - cost,
                    "roi": round((total_net - cost) * 100 / cost, 1) if cost else None,
                    "gross": sum((ln["exp"] or 0) * ln["sold_units"] for ln in lines),
                    "missing": [ln["part"].name for ln in lines if ln["exp"] is None], "box_v": box_v, "box_net": box_net,
                    "box_profit": box_net - cost if box_net is not None else None, "need_units": need,
                    "total_units": total_units, "copies": copies, "cond": cond, "post": post, "pack": pack}
            s.commit()
        return render(request, s, "split.html", title="Box Split Calculator", bundles=bundles, calc=calc, q=qp)

    # ---------------- Inventory ----------------
    @app.get("/inventory", response_class=HTMLResponse)
    def inventory_page(request: Request, status: str = "active", q: str = "", s: Session = Depends(db)):
        stmt = select(InventoryItem).options(selectinload(InventoryItem.product)).order_by(desc(InventoryItem.id))
        if status == "active":
            stmt = stmt.where(InventoryItem.quantity_on_hand > 0)
        elif status != "all":
            stmt = stmt.where(InventoryItem.status == status)
        if q:
            stmt = stmt.where(or_(InventoryItem.name.ilike(f"%{q}%"), InventoryItem.sku.ilike(f"%{q}%")))
        rows = list(s.scalars(stmt.limit(1000)))
        thresholds = all_settings(s)["ageing_thresholds_days"]
        return render(request, s, "inventory.html", title="Inventory", rows=rows, status=status, q=q, statuses=INVENTORY_STATUSES,
                      thresholds=thresholds)

    @app.get("/inventory/export.csv")
    def inventory_csv(s: Session = Depends(db)):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["sku", "name", "condition", "status", "qty_on_hand", "qty_initial", "cost_basis_gbp", "remaining_cost_gbp",
                    "acquired_on", "source", "order_ref", "storage", "asking_gbp"])
        for i in s.scalars(select(InventoryItem).order_by(InventoryItem.id)):
            w.writerow([i.sku, _csv_safe(i.name), i.condition, i.status, i.quantity_on_hand, i.quantity_initial,
                        f"{i.cost_basis_p/100:.2f}", f"{i.remaining_cost_p/100:.2f}", i.acquired_on, i.source_key,
                        _csv_safe(i.order_ref or ""), _csv_safe(i.storage_location or ""),
                        f"{i.asking_price_p/100:.2f}" if i.asking_price_p else ""])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=inventory-{date.today()}.csv"})

    @app.get("/inventory/purchase", response_class=HTMLResponse)
    def purchase_form(request: Request, opportunity: int | None = None, s: Session = Depends(db)):
        o = s.get(DealOpportunity, opportunity) if opportunity else None
        return render(request, s, "purchase.html", title="Record purchase", o=o, active="/inventory",
                      products=list(s.scalars(select(Product).order_by(Product.name))),
                      srcs=list(s.scalars(select(Source).order_by(Source.name))), cash=ledger.cash_balance(s))

    @app.post("/inventory/purchase")
    async def purchase_post(request: Request, s: Session = Depends(db)):
        raw = await request.form()
        f = {k: v.strip() for k, v in raw.items() if isinstance(v, str)}
        lines = []
        for desc_, pid, qty, price, cond in zip(raw.getlist("l_desc"), raw.getlist("l_product"), raw.getlist("l_qty"),
                                                raw.getlist("l_price"), raw.getlist("l_cond")):
            if not (desc_ or pid) or not price:
                continue
            prod = s.get(Product, int(pid)) if pid else None
            lines.append(purchasing.LineIn(desc_ or prod.name, to_pence(price), int(qty or 1), prod.id if prod else None, cond))
        try:
            po = purchasing.record_purchase(s, purchasing.PurchaseIn(
                source_key=f.get("source") or "manual", order_ref=f.get("order_ref", ""), purchased_on=d(f.get("date")),
                lines=lines, shipping_p=p(f.get("shipping"), 0), fees_p=p(f.get("fees"), 0),
                other_costs_p=p(f.get("other"), 0), seller=f.get("seller") or None, listing_url=f.get("url") or None,
                opportunity_id=int(f["opportunity_id"]) if f.get("opportunity_id") else None, notes=f.get("notes") or None,
                status=f.get("status") or "awaiting_delivery"))
            s.commit()
        except purchasing.DuplicatePurchase as e:
            s.rollback()
            return go("/inventory/purchase", err=str(e) + " — nothing was created.")
        except (ValueError, MoneyError) as e:
            s.rollback()
            return go("/inventory/purchase", err=str(e))
        return go("/inventory", f"Purchase recorded: {fmt(po.total_p)} landed cost across {len(lines)} line(s)")

    @app.get("/inventory/{iid}", response_class=HTMLResponse)
    def item_detail(iid: int, request: Request, s: Session = Depends(db)):
        item = s.get(InventoryItem, iid) or _404()
        allocs_out = list(s.scalars(select(CostAllocation).where(CostAllocation.source_item_id == iid)))
        alloc_in = s.scalar(select(CostAllocation).where(CostAllocation.target_item_id == iid))
        can_split = bool(item.product and item.product.is_bundle and item.quantity_on_hand > 0)
        plan, plan_err = None, None
        if can_split:
            try:
                plan = inventory.plan_split(s, item)
            except ValueError as e:
                plan_err = str(e)
        v = val.current(s, item.product_id, item.condition) if item.product_id else None
        children = {a.target_item_id: s.get(InventoryItem, a.target_item_id) for a in allocs_out}
        parent = s.get(InventoryItem, item.parent_item_id) if item.parent_item_id else None
        age = (date.today() - item.acquired_on).days
        suggestion = _ageing_suggestion(item, v, age)
        sale_lines = list(s.scalars(select(sales.SalesOrderItem).where(sales.SalesOrderItem.inventory_item_id == iid)))
        s.commit()
        return render(request, s, "item_detail.html", title=item.sku, item=item, allocs_out=allocs_out, alloc_in=alloc_in,
                      plan=plan, plan_err=plan_err, can_split=can_split, v=v, children=children, parent=parent, age=age,
                      suggestion=suggestion, statuses=INVENTORY_STATUSES, sale_lines=sale_lines, active="/inventory")

    @app.post("/inventory/{iid}/update")
    async def item_update(iid: int, request: Request, s: Session = Depends(db)):
        f = await form(request)
        item = s.get(InventoryItem, iid) or _404()
        try:
            if f.get("status") and f["status"] != item.status:
                inventory.set_status(s, item, f["status"])
            item.storage_location = f.get("storage") or None
            item.target_price_p = p(f.get("target"))
            item.asking_price_p = p(f.get("asking"))
            item.notes = f.get("notes") or None
            s.commit()
        except (ValueError, MoneyError) as e:
            s.rollback()
            return go(f"/inventory/{iid}", err=str(e))
        return go(f"/inventory/{iid}", "Saved")

    @app.post("/inventory/{iid}/split")
    async def item_split(iid: int, request: Request, s: Session = Depends(db)):
        f = await form(request)
        item = s.get(InventoryItem, iid) or _404()
        weights = {int(k[2:]): to_pence(v) for k, v in f.items() if k.startswith("w_") and v}
        try:
            kids = inventory.split_bundle(s, item, weights or None)
            s.commit()
        except ValueError as e:
            s.rollback()
            return go(f"/inventory/{iid}", err=str(e))
        return go(f"/inventory/{iid}", f"Split into {len(kids)} inventory items; cost allocated exactly")

    @app.post("/inventory/{iid}/adjust")
    async def item_adjust(iid: int, request: Request, s: Session = Depends(db)):
        f = await form(request)
        item = s.get(InventoryItem, iid) or _404()
        try:
            inventory.adjust(s, item, int(f.get("qty") or 0), f.get("reason", ""), write_off=f.get("write_off") == "1")
            s.commit()
        except ValueError as e:
            s.rollback()
            return go(f"/inventory/{iid}", err=str(e))
        return go(f"/inventory/{iid}", "Adjustment recorded")

    # ---------------- Sales ----------------
    @app.get("/sales", response_class=HTMLResponse)
    def sales_page(request: Request, s: Session = Depends(db)):
        rows = list(s.scalars(select(SalesOrder).options(selectinload(SalesOrder.items), selectinload(SalesOrder.refunds))
                              .order_by(desc(SalesOrder.sold_on), desc(SalesOrder.id))))
        return render(request, s, "sales.html", title="Sales", rows=[(r, sales.figures(r)) for r in rows])

    @app.get("/sales/new", response_class=HTMLResponse)
    def sale_new(request: Request, item: int | None = None, s: Session = Depends(db)):
        items = list(s.scalars(select(InventoryItem).where(InventoryItem.quantity_on_hand > 0).order_by(InventoryItem.name)))
        st = all_settings(s)
        return render(request, s, "sale_new.html", title="Record sale", items=items, pre=item, active="/sales",
                      profile=st["selling_profiles"][st["selling_profile"]], st=st)

    @app.post("/sales/new")
    async def sale_post(request: Request, s: Session = Depends(db)):
        raw = await request.form()
        f = {k: v.strip() for k, v in raw.items() if isinstance(v, str)}
        lines = [sales.SaleLineIn(int(i), int(q or 1), to_pence(pr)) for i, q, pr in
                 zip(raw.getlist("l_item"), raw.getlist("l_qty"), raw.getlist("l_price")) if i and pr]
        try:
            fees = p(f.get("fees"))
            if fees is None:  # estimate from the configured selling profile
                st = all_settings(s)
                gross = sum(ln.unit_price_p * ln.quantity for ln in lines)
                fees = selling_costs(gross, st["selling_profiles"][st["selling_profile"]], 0, 0,
                                     p(f.get("buyer_shipping"), 0)).total_p
            so = sales.record_sale(s, sales.SaleIn(
                platform=f.get("platform") or "ebay_uk", order_ref=f.get("order_ref", ""), sold_on=d(f.get("date")),
                lines=lines, buyer_shipping_p=p(f.get("buyer_shipping"), 0), platform_fees_p=fees,
                ad_fees_p=p(f.get("ad_fees"), 0), postage_p=p(f.get("postage"), 0), packaging_p=p(f.get("packaging"), 0),
                other_costs_p=p(f.get("other"), 0), notes=f.get("notes") or None))
            s.commit()
        except (ValueError, MoneyError, sales.DuplicateSale) as e:
            s.rollback()
            return go("/sales/new", err=str(e))
        return go(f"/sales/{so.id}", "Sale recorded")

    @app.get("/sales/{sid}", response_class=HTMLResponse)
    def sale_detail(sid: int, request: Request, s: Session = Depends(db)):
        so = s.get(SalesOrder, sid) or _404()
        return render(request, s, "sale_detail.html", title=f"Sale {so.order_ref}", so=so, fig=sales.figures(so), active="/sales")

    @app.post("/sales/{sid}/refund")
    async def sale_refund(sid: int, request: Request, s: Session = Depends(db)):
        raw = await request.form()
        so = s.get(SalesOrder, sid) or _404()
        returns = {int(k[4:]): int(v) for k, v in raw.items() if k.startswith("ret_") and v and int(v) > 0}
        try:
            sales.refund(s, so, to_pence(raw.get("amount") or "0"), returns=returns, restock=raw.get("restock") == "1",
                         fee_credit_p=to_pence(raw.get("fee_credit") or "0"), reason=raw.get("reason") or "")
            s.commit()
        except (ValueError, MoneyError) as e:
            s.rollback()
            return go(f"/sales/{sid}", err=str(e))
        return go(f"/sales/{sid}", "Refund recorded")

    @app.post("/sales/{sid}/cancel")
    def sale_cancel(sid: int, s: Session = Depends(db)):
        so = s.get(SalesOrder, sid) or _404()
        try:
            sales.cancel(s, so)
            s.commit()
        except ValueError as e:
            s.rollback()
            return go(f"/sales/{sid}", err=str(e))
        return go(f"/sales/{sid}", "Order cancelled: money, fees and stock reversed")

    # ---------------- Finance ----------------
    @app.get("/finance", response_class=HTMLResponse)
    def finance_page(request: Request, s: Session = Depends(db)):
        dash = finance.compute(s)
        txs = list(s.scalars(select(FinancialTransaction).order_by(desc(FinancialTransaction.occurred_on), desc(FinancialTransaction.id)).limit(300)))
        exps = list(s.scalars(select(Expense).order_by(desc(Expense.spent_on))))
        return render(request, s, "finance.html", title="Finance & Analytics", dash=dash, txs=txs, exps=exps,
                      st=all_settings(s))

    @app.post("/finance/capital")
    async def fin_capital(request: Request, s: Session = Depends(db)):
        f = await form(request)
        try:
            amt = p(f.get("amount"))
            if f.get("kind") == "withdrawal":
                ledger.withdraw(s, amt or 0, d(f.get("date")), f.get("note") or "Owner withdrawal")
            else:
                ledger.add_capital(s, amt or 0, d(f.get("date")), f.get("note") or "Capital contribution")
            s.commit()
        except (ValueError, MoneyError) as e:
            s.rollback()
            return go(request.headers.get("x-back", "/finance"), err=str(e))
        return go(f.get("back") or "/finance", "Recorded")

    @app.post("/finance/expense")
    async def fin_expense(request: Request, s: Session = Depends(db)):
        f = await form(request)
        try:
            ledger.record_expense(s, p(f.get("amount")) or 0, d(f.get("date")), f.get("category") or "general",
                                  f.get("description") or "Expense")
            s.commit()
        except (ValueError, MoneyError) as e:
            s.rollback()
            return go("/finance", err=str(e))
        return go("/finance", "Expense recorded")

    # ---------------- Imports ----------------
    @app.get("/imports", response_class=HTMLResponse)
    def imports_page(request: Request, s: Session = Depends(db)):
        jobs = list(s.scalars(select(ImportJob).order_by(desc(ImportJob.id)).limit(50)))
        reviews = list(s.scalars(select(ImportReview).where(ImportReview.status == "open").order_by(desc(ImportReview.id)).limit(200)))
        return render(request, s, "imports.html", title="Imports", jobs=jobs, reviews=reviews)

    @app.post("/imports")
    async def imports_post(request: Request, s: Session = Depends(db)):
        raw = await request.form()
        up = raw.get("file")
        if up is None or not getattr(up, "filename", None):
            return go("/imports", err="Choose a file")
        content = await up.read()
        if len(content) > 5_000_000:
            return go("/imports", err="File too large (5 MB max)")
        try:
            job = imports.run_import(s, raw.get("kind", ""), up.filename, content)
            s.commit()
        except imports.DuplicateFile as e:
            s.rollback()
            return go("/imports", err=str(e) + " — nothing imported.")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as e:
            s.rollback()
            return go("/imports", err=f"Import failed: {e}")
        return go("/imports", f"Import #{job.id}: {job.rows_created} created, {job.rows_duplicate} duplicates skipped, "
                              f"{job.rows_review} for review, {job.rows_error} errors")

    @app.post("/imports/review/{rid}")
    async def review_resolve(rid: int, request: Request, s: Session = Depends(db)):
        r = s.get(ImportReview, rid) or _404()
        r.status = "dismissed"
        s.commit()
        return go("/imports", "Review item dismissed")

    @app.get("/imports/template/{kind}.csv")
    def import_template(kind: str):
        path = ROOT / "samples" / f"{kind}_template.csv"
        if kind not in imports.KINDS or not path.exists():
            _404()
        return FileResponse(path, media_type="text/csv", filename=f"{kind}_template.csv")

    # ---------------- Sources ----------------
    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request, s: Session = Depends(db)):
        srcs = list(s.scalars(select(Source).order_by(Source.status.desc(), Source.kind, Source.name)))
        jobs = list(s.scalars(select(ScanJob).order_by(desc(ScanJob.id)).limit(30)))
        profiles = list(s.scalars(select(SearchProfile).order_by(SearchProfile.name)))
        names = {x.id: x.name for x in srcs}
        return render(request, s, "sources.html", title="Source Manager", srcs=srcs, jobs=jobs, profiles=profiles,
                      src_names=names, ebay_ready=ebay.is_configured(), suggestions=scanning.SUGGESTED_QUERIES,
                      products=list(s.scalars(select(Product).order_by(Product.name))),
                      running={j.profile_id for j in jobs if j.status == "running"})

    @app.post("/profiles/new")
    async def profile_new(request: Request, s: Session = Depends(db)):
        raw = await request.form()
        f = {k: v.strip() for k, v in raw.items() if isinstance(v, str)}
        if not f.get("query"):
            return go("/sources#profiles", err="Search terms are required")
        types = ",".join(raw.getlist("listing_types")) or "FIXED_PRICE"
        try:
            s.add(SearchProfile(name=f.get("name") or f["query"][:120], source_key="ebay_uk", query=f["query"][:300],
                                excluded_keywords=f.get("excluded") or None, max_price_p=p(f.get("max_price")),
                                min_price_p=p(f.get("min_price")), listing_types=types, new_only=f.get("new_only") == "1",
                                uk_delivery=f.get("uk_delivery", "1") == "1",
                                product_id=int(f["product_id"]) if f.get("product_id") else None,
                                max_results=max(1, min(int(f.get("max_results") or 100), 1000)),
                                frequency_min=max(15, int(f.get("frequency_min") or 360))))
            s.commit()
        except (ValueError, MoneyError) as e:
            s.rollback()
            return go("/sources#profiles", err=str(e))
        return go("/sources#profiles", "Search profile saved")

    @app.post("/profiles/{pid}/toggle")
    def profile_toggle(pid: int, s: Session = Depends(db)):
        pr = s.get(SearchProfile, pid) or _404()
        pr.enabled = not pr.enabled
        s.commit()
        return go("/sources#profiles", f"{pr.name}: {'enabled' if pr.enabled else 'paused'}")

    @app.post("/profiles/{pid}/delete")
    def profile_delete(pid: int, s: Session = Depends(db)):
        pr = s.get(SearchProfile, pid) or _404()
        s.delete(pr)
        s.commit()
        return go("/sources#profiles", "Search profile deleted (its past scans and listings are kept)")

    @app.post("/profiles/{pid}/run")
    def profile_run(pid: int, s: Session = Depends(db)):
        pr = s.get(SearchProfile, pid) or _404()
        if scanning.is_running(s, pid):
            return go("/sources#jobs", err="That profile is already scanning")

        def work():
            with factory() as s2:
                try:
                    scanning.run_profile(s2, s2.get(SearchProfile, pid))
                except Exception as e:
                    log.error(f"manual scan {pid} crashed: {e}")
        threading.Thread(target=work, daemon=True).start()
        return go("/sources#jobs", f"Scan started for “{pr.name}”. Refresh this page to see progress.")

    @app.post("/scans/{jid}/cancel")
    def scan_cancel(jid: int, s: Session = Depends(db)):
        from app.services.sources import cancel_scan
        job = s.get(ScanJob, jid) or _404()
        cancel_scan(s, job)
        s.commit()
        return go("/sources#jobs", "Cancellation requested; the scan stops before its next listing")

    @app.post("/sources/{sid}/fees")
    async def source_fees(sid: int, request: Request, s: Session = Depends(db)):
        f = await form(request)
        src = s.get(Source, sid) or _404()
        src.buyer_fee_bp = int(round(float(f.get("bp") or 0) * 100))
        src.buyer_fee_fixed_p = p(f.get("fixed"), 0)
        s.commit()
        return go("/sources", f"Buyer fee profile saved for {src.name}")

    # ---------------- Settings ----------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, s: Session = Depends(db)):
        return render(request, s, "settings.html", title="Settings", st=all_settings(s), defaults=DEFAULT_SETTINGS,
                      db_url=url.split("///")[-1])

    @app.post("/settings")
    async def settings_post(request: Request, s: Session = Depends(db)):
        f = await form(request)
        st = all_settings(s)
        try:
            budget = dict(st["budget"])
            for k in ("max_per_deal_p", "min_cash_reserve_p", "min_profit_p", "target_monthly_profit_p"):
                if f.get(k):
                    budget[k] = to_pence(f[k])
            for k in ("min_roi_pct", "reinvest_pct", "max_category_exposure_pct"):
                if f.get(k):
                    budget[k] = int(f[k])
            set_setting(s, "budget", budget)
            for k in ("default_postage_p", "default_packaging_p"):
                if f.get(k):
                    set_setting(s, k, to_pence(f[k]))
            if f.get("selling_profile") in st["selling_profiles"]:
                set_setting(s, "selling_profile", f["selling_profile"])
            profiles = dict(st["selling_profiles"])
            for key in profiles:
                if f.get(f"fvf_{key}"):
                    profiles[key] = {**profiles[key], "final_value_bp": int(round(float(f[f"fvf_{key}"]) * 100)),
                                     "per_order_fixed_p": to_pence(f.get(f"fixed_{key}") or "0")}
            set_setting(s, "selling_profiles", profiles)
            scoring = json.loads(json.dumps(st["scoring"]))
            for k in scoring["weights"]:
                if f.get(f"w_{k}"):
                    scoring["weights"][k] = int(f[f"w_{k}"])
            for k in scoring["thresholds"]:
                if f.get(f"t_{k}"):
                    scoring["thresholds"][k] = int(f[f"t_{k}"])
            set_setting(s, "scoring", scoring)
            if f.get("valuation_window_days"):
                set_setting(s, "valuation_window_days", int(f["valuation_window_days"]))
            set_setting(s, "vat_registered", f.get("vat_registered") == "1")
            s.commit()
        except (ValueError, MoneyError) as e:
            s.rollback()
            return go("/settings", err=str(e))
        return go("/settings", "Settings saved. Use 'Re-score' on the Deal Scanner to apply them to open opportunities.")

    @app.get("/settings/backup")
    def backup():
        if not url.startswith("sqlite:///"):
            raise HTTPException(400, "Backup download supports SQLite only; use pg_dump for PostgreSQL")
        src = sqlite3.connect(url.removeprefix("sqlite:///"))
        tmp = Path(tempfile.mkdtemp()) / f"deal-hunter-{'demo' if is_demo else 'live'}-{datetime.now():%Y%m%d-%H%M}.sqlite"
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        src.close()
        dst.close()
        return FileResponse(tmp, filename=tmp.name, media_type="application/octet-stream")

    # ---------------- JSON API ----------------
    @app.get("/api/dashboard")
    def api_dashboard(s: Session = Depends(db)):
        from dataclasses import asdict
        return asdict(finance.compute(s))

    @app.get("/api/products")
    def api_products(q: str = "", s: Session = Depends(db)):
        return [{"id": x.id, "name": x.name, "category": x.category.name if x.category else None, "sku": x.sku,
                 "rrp_p": x.rrp_p, "is_bundle": x.is_bundle} for x in catalogue.search(s, q, 200)]

    @app.get("/api/products/{pid}/valuation")
    def api_valuation(pid: int, condition: str = "sealed", window: int = 90, s: Session = Depends(db)):
        prod = s.get(Product, pid) or _404()
        v = val.compute(s, prod, condition, window, persist=False)
        return {k: getattr(v, k) for k in ("condition_group", "window_days", "n_comparables", "n_excluded", "median_p",
                                           "conservative_p", "expected_p", "optimistic_p", "sales_per_30d", "trend_pct",
                                           "confidence", "confidence_score", "explanation")}

    @app.get("/api/opportunities")
    def api_opps(s: Session = Depends(db)):
        return [{"id": o.id, "title": o.listing.title, "score": o.score, "classification": o.classification,
                 "acquisition_p": o.acquisition_p, "profit_expected_p": o.profit_expected_p, "roi_pct": o.roi_pct,
                 "confidence": o.confidence, "status": o.status, "url": o.listing.url, "explanation": o.explanation}
                for o in s.scalars(select(DealOpportunity).order_by(desc(DealOpportunity.score)).limit(500))]

    @app.post("/api/listings")
    async def api_listing(request: Request, s: Session = Depends(db)):
        """For a future browser extension: submit a listing the user is viewing."""
        body = await request.json()
        try:
            r = deals.ingest(s, deals.ListingIn(**{k: v for k, v in body.items() if k in deals.ListingIn.__dataclass_fields__}))
            s.commit()
        except (ValueError, UnsafeURL, TypeError) as e:
            s.rollback()
            return JSONResponse({"error": str(e)}, status_code=422)
        return {"opportunity_id": r.opportunity.id, "created": r.created, "score": r.opportunity.score}

    @app.get("/healthz")
    def health():
        return {"ok": True, "workspace": "demo" if is_demo else "live"}

    return app


def _404():
    raise HTTPException(404, "Not found")


def _csv_safe(v: str) -> str:
    """Prevent spreadsheet formula injection in exports."""
    return "'" + v if v and v[0] in "=+-@\t\r" else v


def _ageing_suggestion(item: InventoryItem, v, age: int) -> str | None:
    if item.quantity_on_hand == 0:
        return None
    if age >= 180:
        return ("Held 180+ days. Consider pricing at or below the conservative valuation"
                + (f" ({fmt(v.conservative_p)})" if v and v.conservative_p else "") + ", bundling, or another channel. Your call.")
    if age >= 90:
        return ("Held 90+ days. Check the listing price against the expected valuation"
                + (f" ({fmt(v.effective_expected_p)})" if v and v.effective_expected_p else "") + " and consider a 5–10% reduction.")
    if age >= 30 and item.status not in ("listed", "reserved"):
        return "Held 30+ days and not listed yet: listing it releases capital."
    return None

