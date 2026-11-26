"""CSV/JSON imports with idempotency (file hash + natural keys) and a review queue for anything uncertain."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ImportJob, ImportReview, Product, ProductIdentifier
from app.money import MoneyError, to_pence
from app.services import catalogue, deals, purchasing
from app.services import valuation as val
from app.services.safety import UnsafeURL

KINDS = ("products", "sold_observations", "listings", "purchases")


class DuplicateFile(Exception):
    def __init__(self, job: ImportJob):
        super().__init__(f"This file was already imported (import #{job.id}, {job.created_at:%d %b %Y %H:%M})")
        self.job = job


def parse_rows(filename: str, content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig")
    if filename.lower().endswith(".json"):
        data = json.loads(text)
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise ValueError("JSON import must be a list of objects")
        return [{str(k).strip().lower(): ("" if v is None else str(v)) for k, v in r.items()} for r in data]
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in reader]


def _bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def _date(v: str) -> date:
    v = v.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognised date {v!r}")


def _resolve_product(s: Session, ref: str) -> tuple[Product | None, str | None]:
    """Find a product by id, identifier or alias. Returns (product, issue)."""
    ref = (ref or "").strip()
    if not ref:
        return None, "No product given"
    if ref.isdigit() and s.get(Product, int(ref)):
        return s.get(Product, int(ref)), None
    ident = s.scalar(select(ProductIdentifier).where(ProductIdentifier.value == ref.replace("-", "").replace(" ", "")))
    if ident:
        return s.get(Product, ident.product_id), None
    n = catalogue.normalise(ref)
    from app.models import ProductAlias
    hits = {a.product_id for a in s.scalars(select(ProductAlias).where(ProductAlias.normalised == n))}
    if len(hits) == 1:
        return s.get(Product, hits.pop()), None
    if len(hits) > 1:
        return None, f"Ambiguous product name {ref!r}"
    m = catalogue.confident_match(catalogue.match_title(s, ref))
    if m:
        return None, f"No exact match for {ref!r}; closest is #{m.product.id} {m.product.name} ({m.score})"
    return None, f"Unknown product {ref!r}"


def run_import(s: Session, kind: str, filename: str, content: bytes) -> ImportJob:
    if kind not in KINDS:
        raise ValueError(f"Unknown import type {kind}")
    digest = hashlib.sha256(content).hexdigest()
    prior = s.scalar(select(ImportJob).where(ImportJob.kind == kind, ImportJob.file_hash == digest))
    if prior:
        raise DuplicateFile(prior)
    rows = parse_rows(filename, content)
    job = ImportJob(kind=kind, filename=filename[:300], file_hash=digest, rows_total=len(rows), summary=[])
    s.add(job)
    s.flush()
    handler = {"products": _import_products, "sold_observations": _import_observations,
               "listings": _import_listings, "purchases": _import_purchases}[kind]
    handler(s, job, rows)
    job.status = "needs_review" if job.rows_review else ("completed_with_errors" if job.rows_error else "completed")
    s.flush()
    return job


def _review(s: Session, job: ImportJob, n: int, issue: str, row: dict) -> None:
    s.add(ImportReview(import_job_id=job.id, row_number=n, issue=issue[:300], row=row))
    job.rows_review += 1


def _import_products(s: Session, job: ImportJob, rows: list[dict]) -> None:
    pending_contents = []
    for n, r in enumerate(rows, start=2):
        name = r.get("name", "").strip()
        if not name:
            job.rows_error += 1
            job.summary.append(f"Row {n}: missing name")
            continue
        ids = {k.upper(): r[k] for k in ("ean", "upc", "isbn", "gw_code") if r.get(k)}
        clash = None
        for k, v in ids.items():
            clash = clash or s.scalar(select(ProductIdentifier).where(ProductIdentifier.kind == k,
                                                                      ProductIdentifier.value == v.replace("-", "")))
        same_name = [p for p in catalogue.search(s, name) if catalogue.normalise(p.name) == catalogue.normalise(name)]
        if clash or same_name:
            job.rows_duplicate += 1
            existing = s.get(Product, clash.product_id) if clash else same_name[0]
            _review(s, job, n, f"Possible duplicate of #{existing.id} {existing.name}", r)
            continue
        with s.begin_nested():
            p = catalogue.create_product(
                s, name=name, category=r.get("category") or None,
                aliases=[a for a in r.get("aliases", "").split("|") if a.strip()], identifiers=ids,
                manufacturer=r.get("manufacturer") or None, game_system=r.get("game_system") or None,
                faction=r.get("faction") or None, product_range=r.get("product_range") or None,
                edition=r.get("edition") or None,
                release_year=int(r["release_year"]) if r.get("release_year", "").isdigit() else None,
                status=r.get("status") or "unknown", sku=r.get("sku") or None,
                rrp_p=to_pence(r.get("rrp")) if r.get("rrp") else None, author=r.get("author") or None,
                series=r.get("series") or None, book_format=r.get("book_format") or None,
                notes=r.get("notes") or None, provenance=f"import:{job.filename}")
        job.rows_created += 1
        if r.get("contents"):
            pending_contents.append((n, p, r["contents"], r))
    # Bundle contents after all rows exist: "Name*2|Other Name|Shared A@grp1|Shared B@grp1|Card!"
    for n, bundle, spec, r in pending_contents:
        comps, problems = [], []
        for part in [x.strip() for x in spec.split("|") if x.strip()]:
            sellable = not part.endswith("!")
            part = part.rstrip("!")
            group = None
            if "@" in part:
                part, group = part.rsplit("@", 1)
            qty = 1
            if "*" in part:
                part, q = part.rsplit("*", 1)
                qty = int(q)
            prod, issue = _resolve_product(s, part)
            if prod:
                comps.append({"product_id": prod.id, "quantity": qty, "separately_sellable": sellable,
                              "shared_sprue_group": group})
            else:
                problems.append(issue)
        if problems:
            _review(s, job, n, f"Bundle {bundle.name}: " + "; ".join(problems), r)
        else:
            catalogue.set_bundle_components(s, bundle, comps)


def _import_observations(s: Session, job: ImportJob, rows: list[dict]) -> None:
    touched: set[tuple[int, str]] = set()
    for n, r in enumerate(rows, start=2):
        label = (r.get("data_label") or "").lower()
        if label not in ("verified", "test"):
            job.rows_error += 1
            job.summary.append(f"Row {n}: data_label must be 'verified' or 'test' (no unlabelled price data)")
            continue
        product, issue = _resolve_product(s, r.get("product", ""))
        if not product:
            _review(s, job, n, issue, r)
            continue
        try:
            currency = (r.get("currency") or "GBP").upper()
            price = to_pence(r["price"]) if currency != "JPY" else int(r["price"])
            obs = val.add_observation(
                s, product_id=product.id, observation_type=(r.get("type") or "sold").lower(),
                observed_on=_date(r["date"]), price_minor=price, currency=currency, fx_rate=r.get("fx_rate") or "1",
                shipping_p=to_pence(r["shipping"]) if r.get("shipping") else None,
                platform=r.get("platform") or "unknown", provenance=r.get("provenance") or "csv_import",
                condition=r.get("condition") or "unknown", source_url=r.get("url") or None,
                title=r.get("title") or None, edition=r.get("edition") or None, data_label=label,
                is_multi_item_lot=_bool(r.get("multi_lot", "")),
                offer_accepted_unconfirmed=_bool(r.get("offer_unconfirmed", "")))
        except (ValueError, KeyError, MoneyError) as e:
            job.rows_error += 1
            job.summary.append(f"Row {n}: {e}")
            continue
        if obs is None:
            job.rows_duplicate += 1
        else:
            job.rows_created += 1
            touched.add((product.id, obs.condition))
    for pid, cond in touched:  # refresh valuations for affected products
        val.compute(s, s.get(Product, pid), cond)


def _import_listings(s: Session, job: ImportJob, rows: list[dict]) -> None:
    for n, r in enumerate(rows, start=2):
        try:
            product_id = None
            if r.get("product"):
                product, issue = _resolve_product(s, r["product"])
                if not product:
                    _review(s, job, n, issue + " (listing imported unmatched)", r)
                else:
                    product_id = product.id
            currency = (r.get("currency") or "GBP").upper()
            res = deals.ingest(s, deals.ListingIn(
                source_key=r.get("source") or "csv", title=r.get("title", ""),
                price_minor=to_pence(r["price"]) if currency != "JPY" else int(r["price"]), currency=currency,
                fx_rate=r.get("fx_rate") or "1", shipping_p=to_pence(r.get("shipping") or "0") or 0,
                external_id=r.get("external_id") or None, url=r.get("url") or None,
                description=r.get("description") or None, image_url=r.get("image_url") or None,
                condition=r.get("condition") or "unknown", listing_type=r.get("listing_type") or "fixed",
                quantity=int(r.get("quantity") or 1), seller=r.get("seller") or None,
                ends_at=datetime.fromisoformat(r["ends_at"]) if r.get("ends_at") else None,
                product_id=product_id, provenance=f"import:{job.filename}"))
        except (ValueError, KeyError, MoneyError, UnsafeURL) as e:
            job.rows_error += 1
            job.summary.append(f"Row {n}: {e}")
            continue
        if res.created:
            job.rows_created += 1
        else:
            job.rows_duplicate += 1


def _import_purchases(s: Session, job: ImportJob, rows: list[dict]) -> None:
    orders: dict[tuple[str, str], list[tuple[int, dict]]] = {}
    for n, r in enumerate(rows, start=2):
        key = ((r.get("source") or "manual").strip(), (r.get("order_ref") or "").strip())
        if not key[1]:
            job.rows_error += 1
            job.summary.append(f"Row {n}: missing order_ref (needed to prevent duplicates)")
            continue
        orders.setdefault(key, []).append((n, r))
    for (source, ref), lines in orders.items():
        if purchasing.find_existing(s, source, ref):
            job.rows_duplicate += len(lines)
            job.summary.append(f"Order {source}:{ref} already recorded — skipped")
            continue
        first = lines[0][1]
        try:
            line_objs = []
            for n, r in lines:
                product, issue = _resolve_product(s, r.get("product", "")) if r.get("product") else (None, None)
                if r.get("product") and not product:
                    _review(s, job, n, issue + " (inventory created without catalogue link)", r)
                line_objs.append(purchasing.LineIn(description=r.get("description") or (product.name if product else "Unknown item"),
                                                   unit_price_p=to_pence(r["unit_price"]), quantity=int(r.get("quantity") or 1),
                                                   product_id=product.id if product else None,
                                                   condition=r.get("condition") or "unknown"))
            with s.begin_nested():
                purchasing.record_purchase(s, purchasing.PurchaseIn(
                    source_key=source, order_ref=ref, purchased_on=_date(first["date"]), lines=line_objs,
                    shipping_p=to_pence(first.get("shipping") or "0") or 0, fees_p=to_pence(first.get("fees") or "0") or 0,
                    other_costs_p=to_pence(first.get("other_costs") or "0") or 0, seller=first.get("seller") or None,
                    listing_url=first.get("url") or None, provenance=f"import:{job.filename}"))
            job.rows_created += len(lines)
        except (ValueError, KeyError, MoneyError) as e:
            job.rows_error += len(lines)
            job.summary.append(f"Order {source}:{ref}: {e}")
