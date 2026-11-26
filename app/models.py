"""Domain models. Money columns end in `_p` and hold integer pence (GBP) unless named `*_minor`."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- Reference values -------------------------------------------------------------------------
CONDITIONS = [
    ("sealed", "Sealed"), ("new_in_box", "New in box"), ("new_on_sprue", "New on sprue"),
    ("complete_opened", "Complete but opened"), ("assembled", "Assembled"), ("primed", "Primed"),
    ("painted", "Painted"), ("incomplete", "Incomplete"), ("damaged", "Damaged"), ("unknown", "Condition unknown"),
]
CONDITION_LABELS = dict(CONDITIONS)
INVENTORY_STATUSES = [
    "purchased", "awaiting_delivery", "received", "requires_inspection", "ready_to_list", "listed",
    "reserved", "sold", "dispatched", "returned", "damaged", "written_off", "split",
]
PRODUCT_STATUSES = ["current", "discontinued", "limited", "made_to_order", "unknown"]


# ---- Catalogue --------------------------------------------------------------------------------
class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    priority: Mapped[int] = mapped_column(Integer, default=3)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    manufacturer: Mapped[str | None] = mapped_column(String(120))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), index=True)
    game_system: Mapped[str | None] = mapped_column(String(120), index=True)
    faction: Mapped[str | None] = mapped_column(String(120), index=True)
    product_range: Mapped[str | None] = mapped_column(String(120))
    edition: Mapped[str | None] = mapped_column(String(120))
    release_year: Mapped[int | None] = mapped_column(Integer)
    discontinued_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="unknown")
    sku: Mapped[str | None] = mapped_column(String(60), index=True)
    rrp_p: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    official_url: Mapped[str | None] = mapped_column(String(500))
    image_url: Mapped[str | None] = mapped_column(String(500))
    is_bundle: Mapped[bool] = mapped_column(Boolean, default=False)
    # Book attributes (nullable; only used for books)
    author: Mapped[str | None] = mapped_column(String(200))
    series: Mapped[str | None] = mapped_column(String(200))
    book_format: Mapped[str | None] = mapped_column(String(60))
    printing: Mapped[str | None] = mapped_column(String(120))
    signed: Mapped[bool | None] = mapped_column(Boolean)
    limited_numbering: Mapped[str | None] = mapped_column(String(60))
    notes: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(String(200), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    category: Mapped[Category | None] = relationship()
    aliases: Mapped[list[ProductAlias]] = relationship(back_populates="product", cascade="all, delete-orphan")
    identifiers: Mapped[list[ProductIdentifier]] = relationship(cascade="all, delete-orphan")
    components: Mapped[list[BundleComponent]] = relationship(
        foreign_keys="BundleComponent.bundle_id", cascade="all, delete-orphan", back_populates="bundle")


class ProductAlias(Base):
    __tablename__ = "product_aliases"
    __table_args__ = (UniqueConstraint("product_id", "normalised"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(300))
    normalised: Mapped[str] = mapped_column(String(300), index=True)
    product: Mapped[Product] = relationship(back_populates="aliases")


class ProductIdentifier(Base):
    __tablename__ = "product_identifiers"
    __table_args__ = (UniqueConstraint("kind", "value"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # EAN, UPC, ISBN, GW_CODE
    value: Mapped[str] = mapped_column(String(40))


class RetailPrice(Base):
    __tablename__ = "retail_prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    price_p: Mapped[int] = mapped_column(Integer)
    effective_on: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(200))


class BundleComponent(Base):
    __tablename__ = "bundle_components"
    __table_args__ = (UniqueConstraint("bundle_id", "component_id"), CheckConstraint("quantity > 0"),
                      CheckConstraint("bundle_id <> component_id"))
    id: Mapped[int] = mapped_column(primary_key=True)
    bundle_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    separately_sellable: Mapped[bool] = mapped_column(Boolean, default=True)
    shared_sprue_group: Mapped[str | None] = mapped_column(String(60))  # same group = one physical sprue
    is_exclusive: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(String(300))
    bundle: Mapped[Product] = relationship(foreign_keys=[bundle_id], back_populates="components")
    component: Mapped[Product] = relationship(foreign_keys=[component_id])


class ProductRelationship(Base):
    __tablename__ = "product_relationships"
    __table_args__ = (UniqueConstraint("from_id", "to_id", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    from_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    to_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30))  # edition_of, repack_of, related, upgrade_for


# ---- Market data ------------------------------------------------------------------------------
class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(30))  # marketplace, auction, retailer, manual
    country: Mapped[str] = mapped_column(String(2), default="GB")
    methods: Mapped[str] = mapped_column(String(200))  # comma list
    status: Mapped[str] = mapped_column(String(20))  # working, partial, planned, restricted
    status_note: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auth_required: Mapped[str | None] = mapped_column(String(200))
    rate_limit: Mapped[str | None] = mapped_column(String(120))
    scan_frequency_min: Mapped[int | None] = mapped_column(Integer)
    buyer_fee_bp: Mapped[int] = mapped_column(Integer, default=0)
    buyer_fee_fixed_p: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    listings_found: Mapped[int] = mapped_column(Integer, default=0)


class ScanJob(Base):
    __tablename__ = "scan_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued running succeeded failed cancelled
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict | None] = mapped_column(JSON)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("search_profiles.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SearchProfile(Base):
    """A saved search run against a source on a schedule."""
    __tablename__ = "search_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    source_key: Mapped[str] = mapped_column(String(60), default="ebay_uk")
    query: Mapped[str] = mapped_column(String(300))  # eBay syntax: space = AND, (a, b) = OR
    excluded_keywords: Mapped[str | None] = mapped_column(String(500))  # comma separated
    max_price_p: Mapped[int | None] = mapped_column(Integer)
    min_price_p: Mapped[int | None] = mapped_column(Integer)
    listing_types: Mapped[str] = mapped_column(String(60), default="FIXED_PRICE,AUCTION")
    new_only: Mapped[bool] = mapped_column(Boolean, default=False)
    uk_delivery: Mapped[bool] = mapped_column(Boolean, default=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    max_results: Mapped[int] = mapped_column(Integer, default=100)
    frequency_min: Mapped[int] = mapped_column(Integer, default=360)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuctionFeeProfile(Base):
    """Buyer-side fee terms for one auction house (or one sale). Entered from the auctioneer's own terms."""
    __tablename__ = "auction_fee_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    source_key: Mapped[str | None] = mapped_column(String(60), index=True)
    schedule: Mapped[dict] = mapped_column(JSON)  # AuctionFeeSchedule fields
    terms_url: Mapped[str | None] = mapped_column(String(1000))
    checked_on: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MarketListing(Base):
    __tablename__ = "market_listings"
    __table_args__ = (UniqueConstraint("source_id", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    url: Mapped[str | None] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)  # untrusted; always escaped on output
    image_url: Mapped[str | None] = mapped_column(String(1000))
    condition: Mapped[str] = mapped_column(String(30), default="unknown")
    listing_type: Mapped[str] = mapped_column(String(20), default="fixed")  # fixed, auction, offer
    price_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="GBP")
    fx_rate: Mapped[str] = mapped_column(String(30), default="1")
    fx_date: Mapped[date | None] = mapped_column(Date)
    price_p: Mapped[int] = mapped_column(Integer)
    shipping_p: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    seller: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), index=True)
    match_score: Mapped[int | None] = mapped_column(Integer)
    match_method: Mapped[str | None] = mapped_column(String(40))
    possible_duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("market_listings.id"))
    provenance: Mapped[str] = mapped_column(String(200))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    source: Mapped[Source] = relationship()
    product: Mapped[Product | None] = relationship()


class ListingSnapshot(Base):
    __tablename__ = "listing_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("market_listings.id", ondelete="CASCADE"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    price_p: Mapped[int] = mapped_column(Integer)
    shipping_p: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(String(200))


class SoldObservation(Base):
    __tablename__ = "sold_observations"
    __table_args__ = (Index("ix_obs_product_date", "product_id", "observed_on"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    observation_type: Mapped[str] = mapped_column(String(20))  # sold, auction_hammer, unsold, asking, retail
    condition: Mapped[str] = mapped_column(String(30), default="unknown")
    edition: Mapped[str | None] = mapped_column(String(120))
    observed_on: Mapped[date] = mapped_column(Date)
    price_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="GBP")
    fx_rate: Mapped[str] = mapped_column(String(30), default="1")
    price_p: Mapped[int] = mapped_column(Integer)
    shipping_p: Mapped[int | None] = mapped_column(Integer)
    platform: Mapped[str] = mapped_column(String(60))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    title: Mapped[str | None] = mapped_column(String(500))
    provenance: Mapped[str] = mapped_column(String(60))  # manual_verified, product_research_manual, csv_import, demo
    data_label: Mapped[str] = mapped_column(String(20), default="verified")  # verified | test
    is_multi_item_lot: Mapped[bool] = mapped_column(Boolean, default=False)
    offer_accepted_unconfirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    exclude_reason: Mapped[str | None] = mapped_column(String(200))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Valuation(Base):
    __tablename__ = "valuations"
    __table_args__ = (Index("ix_val_current", "product_id", "condition_group", "window_days", "is_current"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    condition_group: Mapped[str] = mapped_column(String(30))
    window_days: Mapped[int] = mapped_column(Integer)  # 0 = all history
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    n_comparables: Mapped[int] = mapped_column(Integer)
    n_excluded: Mapped[int] = mapped_column(Integer, default=0)
    median_p: Mapped[int | None] = mapped_column(Integer)
    mean_p: Mapped[int | None] = mapped_column(Integer)
    low_p: Mapped[int | None] = mapped_column(Integer)
    high_p: Mapped[int | None] = mapped_column(Integer)
    conservative_p: Mapped[int | None] = mapped_column(Integer)
    expected_p: Mapped[int | None] = mapped_column(Integer)
    optimistic_p: Mapped[int | None] = mapped_column(Integer)
    sales_per_30d: Mapped[float | None] = mapped_column()
    sell_through_pct: Mapped[float | None] = mapped_column()
    trend_pct: Mapped[float | None] = mapped_column()
    est_days_to_sell: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[str] = mapped_column(String(20))  # high medium low insufficient
    confidence_score: Mapped[int] = mapped_column(Integer)
    explanation: Mapped[list | None] = mapped_column(JSON)
    override_p: Mapped[int | None] = mapped_column(Integer)
    override_reason: Mapped[str | None] = mapped_column(String(300))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def effective_expected_p(self) -> int | None:
        return self.override_p if self.override_p is not None else self.expected_p


class DealOpportunity(Base):
    __tablename__ = "deal_opportunities"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("market_listings.id", ondelete="CASCADE"), unique=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    acquisition_p: Mapped[int] = mapped_column(Integer)
    resale_conservative_p: Mapped[int | None] = mapped_column(Integer)
    resale_expected_p: Mapped[int | None] = mapped_column(Integer)
    selling_costs_p: Mapped[int | None] = mapped_column(Integer)
    profit_conservative_p: Mapped[int | None] = mapped_column(Integer)
    profit_expected_p: Mapped[int | None] = mapped_column(Integer)
    roi_pct: Mapped[float | None] = mapped_column()
    score: Mapped[int] = mapped_column(Integer)
    classification: Mapped[str] = mapped_column(String(40))
    confidence: Mapped[str] = mapped_column(String(20))
    explanation: Mapped[list] = mapped_column(JSON)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="new")  # new saved watching dismissed purchased
    reject_reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    listing: Mapped[MarketListing] = relationship()
    product: Mapped[Product | None] = relationship()


class WatchlistEntry(Base):
    __tablename__ = "watchlist_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("market_listings.id", ondelete="CASCADE"))
    target_price_p: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[str] = mapped_column(String(10), default="normal")
    notes: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    product: Mapped[Product | None] = relationship()
    listing: Mapped[MarketListing | None] = relationship()


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    priority: Mapped[str] = mapped_column(String(10), default="normal")
    message: Mapped[str] = mapped_column(String(500))
    link: Mapped[str | None] = mapped_column(String(300))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


# ---- Purchasing & inventory -------------------------------------------------------------------
class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (UniqueConstraint("source_key", "order_ref"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(60))
    order_ref: Mapped[str] = mapped_column(String(120))
    purchased_on: Mapped[date] = mapped_column(Date)
    seller: Mapped[str | None] = mapped_column(String(200))
    listing_url: Mapped[str | None] = mapped_column(String(1000))
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("deal_opportunities.id"))
    items_total_p: Mapped[int] = mapped_column(Integer)
    shipping_p: Mapped[int] = mapped_column(Integer, default=0)
    fees_p: Mapped[int] = mapped_column(Integer, default=0)  # buyer premium, protection fee, payment fee
    other_costs_p: Mapped[int] = mapped_column(Integer, default=0)  # FX, import VAT/duty, travel
    total_p: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="GBP")
    fx_rate: Mapped[str] = mapped_column(String(30), default="1")
    notes: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(String(200), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    items: Mapped[list[PurchaseOrderItem]] = relationship(back_populates="order", cascade="all, delete-orphan")


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"
    __table_args__ = (CheckConstraint("quantity > 0"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    description: Mapped[str] = mapped_column(String(300))
    condition: Mapped[str] = mapped_column(String(30), default="unknown")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price_p: Mapped[int] = mapped_column(Integer)
    allocated_costs_p: Mapped[int] = mapped_column(Integer, default=0)
    order: Mapped[PurchaseOrder] = relationship(back_populates="items")


class InventoryItem(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (CheckConstraint("quantity_on_hand >= 0"), CheckConstraint("remaining_cost_p >= 0"))
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(40), unique=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), index=True)
    po_item_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_order_items.id"))
    parent_item_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_items.id"))
    name: Mapped[str] = mapped_column(String(300))
    condition: Mapped[str] = mapped_column(String(30), default="unknown")
    quantity_initial: Mapped[int] = mapped_column(Integer)
    quantity_on_hand: Mapped[int] = mapped_column(Integer)
    purchase_cost_p: Mapped[int] = mapped_column(Integer)  # item price share
    allocated_costs_p: Mapped[int] = mapped_column(Integer, default=0)  # shipping/fees share
    cost_basis_p: Mapped[int] = mapped_column(Integer)  # = purchase + allocated, for quantity_initial
    remaining_cost_p: Mapped[int] = mapped_column(Integer)  # cost of units still on hand
    status: Mapped[str] = mapped_column(String(30), default="purchased", index=True)
    acquired_on: Mapped[date] = mapped_column(Date)
    source_key: Mapped[str | None] = mapped_column(String(60))
    listing_url: Mapped[str | None] = mapped_column(String(1000))
    order_ref: Mapped[str | None] = mapped_column(String(120))
    storage_location: Mapped[str | None] = mapped_column(String(120))
    est_resale_p: Mapped[int | None] = mapped_column(Integer)  # per unit, informational
    target_price_p: Mapped[int | None] = mapped_column(Integer)
    asking_price_p: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    product: Mapped[Product | None] = relationship()
    movements: Mapped[list[InventoryMovement]] = relationship(order_by="InventoryMovement.id")


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # purchase split_out split_in sale return adjust write_off cancel
    quantity_delta: Mapped[int] = mapped_column(Integer)
    cost_delta_p: Mapped[int] = mapped_column(Integer)
    ref_type: Mapped[str | None] = mapped_column(String(40))
    ref_id: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CostAllocation(Base):
    __tablename__ = "cost_allocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), index=True)
    target_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), index=True)
    method: Mapped[str] = mapped_column(String(40))  # expected_value, rrp, manual_weight
    basis_value_p: Mapped[int] = mapped_column(Integer)
    allocated_p: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---- Sales ------------------------------------------------------------------------------------
class SalesOrder(Base):
    __tablename__ = "sales_orders"
    __table_args__ = (UniqueConstraint("platform", "order_ref"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(60))
    order_ref: Mapped[str] = mapped_column(String(120))
    sold_on: Mapped[date] = mapped_column(Date)
    buyer_shipping_p: Mapped[int] = mapped_column(Integer, default=0)
    platform_fees_p: Mapped[int] = mapped_column(Integer, default=0)
    ad_fees_p: Mapped[int] = mapped_column(Integer, default=0)
    postage_p: Mapped[int] = mapped_column(Integer, default=0)
    packaging_p: Mapped[int] = mapped_column(Integer, default=0)
    other_costs_p: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="completed")  # completed cancelled refunded partially_refunded
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    items: Mapped[list[SalesOrderItem]] = relationship(back_populates="order", cascade="all, delete-orphan")
    refunds: Mapped[list[Refund]] = relationship(back_populates="order", cascade="all, delete-orphan")


class SalesOrderItem(Base):
    __tablename__ = "sales_order_items"
    __table_args__ = (CheckConstraint("quantity > 0"), CheckConstraint("returned_quantity <= quantity"))
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id", ondelete="CASCADE"), index=True)
    inventory_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_p: Mapped[int] = mapped_column(Integer)
    cogs_p: Mapped[int] = mapped_column(Integer)  # cost basis released for this line
    returned_quantity: Mapped[int] = mapped_column(Integer, default=0)
    returned_cogs_p: Mapped[int] = mapped_column(Integer, default=0)
    order: Mapped[SalesOrder] = relationship(back_populates="items")
    item: Mapped[InventoryItem] = relationship()


class Refund(Base):
    __tablename__ = "refunds"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id", ondelete="CASCADE"), index=True)
    amount_p: Mapped[int] = mapped_column(Integer)
    fee_credit_p: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    order: Mapped[SalesOrder] = relationship(back_populates="refunds")


# ---- Finance ----------------------------------------------------------------------------------
class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    spent_on: Mapped[date] = mapped_column(Date)
    category: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(String(300))
    amount_p: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FinancialTransaction(Base):
    """Signed cash ledger. Sum of amount_p = cash available."""
    __tablename__ = "financial_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_on: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    # capital_in withdrawal purchase sale_receipt selling_cost refund fee_credit expense
    amount_p: Mapped[int] = mapped_column(Integer)
    ref_type: Mapped[str | None] = mapped_column(String(40))
    ref_id: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---- Imports, settings, audit -----------------------------------------------------------------
class ImportJob(Base):
    __tablename__ = "import_jobs"
    __table_args__ = (UniqueConstraint("kind", "file_hash"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    filename: Mapped[str] = mapped_column(String(300))
    file_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="running")
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_created: Mapped[int] = mapped_column(Integer, default=0)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    rows_review: Mapped[int] = mapped_column(Integer, default=0)
    rows_error: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ImportReview(Base):
    __tablename__ = "import_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_job_id: Mapped[int] = mapped_column(ForeignKey("import_jobs.id", ondelete="CASCADE"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    issue: Mapped[str] = mapped_column(String(300))
    row: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="open")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict | list | int | str | None] = mapped_column(JSON)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict | None] = mapped_column(JSON)
