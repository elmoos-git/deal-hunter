"""eBay Browse API adapter (official API, active listings only).

Docs checked 26 Nov 2026: https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search
- GET /buy/browse/v1/item_summary/search, application token from the client credentials grant,
  scope https://api.ebay.com/oauth/api_scope, header X-EBAY-C-MARKETPLACE-ID: EBAY_GB.
- limit 1..200 (default 50); page with the `next` URI, not `total`; at most 10,000 results.
- By default only FIXED_PRICE listings are returned; buyingOptions:{AUCTION|FIXED_PRICE} adds auctions.

This adapter never buys, bids or watches. It only reads public listing summaries.
Sold prices are NOT available here (that is Marketplace Insights, a Limited Release API).
"""
from __future__ import annotations

import base64
import os
import threading
import time
from datetime import datetime, timezone
from typing import Iterator

import httpx

from app.money import to_pence
from app.services.sources import TransientError

API_BASE = {"production": "https://api.ebay.com", "sandbox": "https://api.sandbox.ebay.com"}
SCOPE = "https://api.ebay.com/oauth/api_scope"
MARKETPLACE = "EBAY_GB"

# eBay condition IDs → our conditions. eBay's "Used" can mean anything from opened to painted, so it maps to unknown.
CONDITION_MAP = {"1000": "new_in_box", "1500": "complete_opened", "1750": "complete_opened",
                 "3000": "unknown", "7000": "incomplete"}
BUYING_OPTIONS = {"FIXED_PRICE", "AUCTION", "BEST_OFFER"}


class EbayConfigError(Exception):
    pass


class _TokenCache:
    """App tokens last ~2 hours; reuse until 5 minutes before expiry. Shared across scans in this process."""
    lock = threading.Lock()
    token: str | None = None
    expires_at: float = 0
    key: str | None = None


def credentials() -> tuple[str, str, str]:
    cid, secret = os.environ.get("EBAY_CLIENT_ID", "").strip(), os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    env = os.environ.get("EBAY_ENVIRONMENT", "production").strip().lower()
    if not cid or not secret:
        raise EbayConfigError("eBay keys are not configured. Add EBAY_CLIENT_ID and EBAY_CLIENT_SECRET to the .env "
                              "file next to run.py, then restart the app.")
    if env not in API_BASE:
        raise EbayConfigError("EBAY_ENVIRONMENT must be 'production' or 'sandbox'")
    return cid, secret, env


def is_configured() -> bool:
    try:
        credentials()
        return True
    except EbayConfigError:
        return False


class EbayBrowseAdapter:
    source_key = "ebay_uk"

    def __init__(self, client: httpx.Client | None = None, min_interval_s: float = 1.0, timeout_s: float = 15.0):
        self.client_id, self.client_secret, self.env = credentials()
        self.base = API_BASE[self.env]
        self.client = client or httpx.Client(timeout=timeout_s)
        self.min_interval_s = min_interval_s
        self._last_call = 0.0

    # ---- auth --------------------------------------------------------------------------------
    def _token(self, force: bool = False) -> str:
        key = f"{self.env}:{self.client_id}"
        with _TokenCache.lock:
            if not force and _TokenCache.token and _TokenCache.key == key and time.time() < _TokenCache.expires_at:
                return _TokenCache.token
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            try:
                r = self.client.post(f"{self.base}/identity/v1/oauth2/token",
                                     headers={"Authorization": f"Basic {basic}",
                                              "Content-Type": "application/x-www-form-urlencoded"},
                                     data={"grant_type": "client_credentials", "scope": SCOPE})
            except httpx.TransportError as e:
                raise TransientError(f"eBay auth network error: {e}") from e
            if r.status_code >= 500 or r.status_code == 429:
                raise TransientError(f"eBay auth HTTP {r.status_code}")
            if r.status_code != 200:
                # Never echo the secret; eBay's error body does not contain it.
                raise EbayConfigError(f"eBay rejected the keys (HTTP {r.status_code}: {_err(r)}). Check the Production "
                                      "App ID and Cert ID, and that the keyset is enabled.")
            body = r.json()
            _TokenCache.token, _TokenCache.key = body["access_token"], key
            _TokenCache.expires_at = time.time() + int(body.get("expires_in", 7200)) - 300
            return _TokenCache.token

    # ---- search ------------------------------------------------------------------------------
    def _get(self, url: str, params: dict | None) -> dict:
        wait = self.min_interval_s - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)  # polite pacing between calls
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._token(force=attempt == 2)}",
                       "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE, "Accept": "application/json"}
            try:
                r = self.client.get(url, params=params, headers=headers)
            except httpx.TransportError as e:
                raise TransientError(f"eBay network error: {e}") from e
            finally:
                self._last_call = time.time()
            if r.status_code == 401 and attempt == 1:
                continue  # token expired early: refresh once
            if r.status_code == 429 or r.status_code >= 500:
                raise TransientError(f"eBay HTTP {r.status_code} ({_err(r)})")
            if r.status_code != 200:
                raise RuntimeError(f"eBay search failed: HTTP {r.status_code} ({_err(r)})")
            return r.json()
        raise RuntimeError("eBay kept rejecting the access token")

    def fetch(self, params: dict) -> Iterator:
        from app.services.deals import ListingIn

        query = (params.get("query") or "").strip()
        if not query:
            raise ValueError("A search profile needs search terms")
        max_results = max(1, min(int(params.get("max_results", 100)), 1000))
        excluded = [w.strip().lower() for w in (params.get("excluded_keywords") or "").split(",") if w.strip()]
        req = {"q": query, "limit": str(min(200, max_results)), "filter": build_filter(params)}
        url, sent = f"{self.base}/buy/browse/v1/item_summary/search", 0
        while url and sent < max_results:
            data = self._get(url, req)
            for it in data.get("itemSummaries") or []:
                title = it.get("title") or ""
                if any(w in title.lower() for w in excluded):
                    continue
                listing = to_listing(it, ListingIn, params.get("product_id"))
                if listing:
                    yield listing
                    sent += 1
                    if sent >= max_results:
                        return
            url, req = data.get("next"), None  # `next` already carries the query string


def build_filter(params: dict) -> str:
    parts = []
    opts = [o for o in (params.get("listing_types") or "FIXED_PRICE,AUCTION").split(",") if o in BUYING_OPTIONS]
    if opts:
        parts.append("buyingOptions:{" + "|".join(opts) + "}")
    lo, hi = params.get("min_price_p"), params.get("max_price_p")
    if lo or hi:
        lo_s = f"{lo / 100:.2f}" if lo else ""
        hi_s = f"{hi / 100:.2f}" if hi else ""
        parts.append(f"price:[{lo_s}..{hi_s}]")
        parts.append("priceCurrency:GBP")
    if params.get("new_only"):
        parts.append("conditions:{NEW}")
    if params.get("uk_delivery", True):
        parts.append("deliveryCountry:GB")
    return ",".join(parts)


def to_listing(it: dict, ListingIn, product_id: int | None = None):
    """Map one itemSummary to our ListingIn. Returns None for summaries missing the essentials."""
    item_id, title, url = it.get("itemId"), it.get("title"), it.get("itemWebUrl")
    if not (item_id and title):
        return None
    opts = set(it.get("buyingOptions") or [])
    is_auction = "AUCTION" in opts
    price_obj = (it.get("currentBidPrice") if is_auction and it.get("currentBidPrice") else it.get("price")) or {}
    currency = price_obj.get("currency") or "GBP"
    if currency != "GBP" or price_obj.get("value") is None:
        return None  # EBAY_GB should be GBP; skip anything we can't price without an FX rate
    shipping = 0
    for opt in it.get("shippingOptions") or []:
        cost = opt.get("shippingCost") or {}
        if cost.get("currency") == "GBP" and cost.get("value") is not None:
            shipping = to_pence(cost["value"])
            break
    ends_at = None
    if is_auction and it.get("itemEndDate"):
        ends_at = datetime.fromisoformat(it["itemEndDate"].replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    loc = it.get("itemLocation") or {}
    return ListingIn(
        source_key="ebay_uk", external_id=f"ebay:{item_id}", title=title, url=url,
        price_minor=to_pence(price_obj["value"]), currency="GBP", shipping_p=shipping,
        image_url=(it.get("image") or {}).get("imageUrl"), condition=CONDITION_MAP.get(str(it.get("conditionId")), "unknown"),
        listing_type="auction" if is_auction else ("offer" if "BEST_OFFER" in opts else "fixed"),
        seller=(it.get("seller") or {}).get("username"), location=", ".join(x for x in (loc.get("city"), loc.get("country")) if x) or None,
        ends_at=ends_at, product_id=product_id, provenance="ebay_browse_api")


def _err(r: httpx.Response) -> str:
    try:
        errs = r.json().get("errors") or []
        if errs:
            return "; ".join(f"{e.get('errorId')}: {e.get('message')}" for e in errs)[:300]
        return (r.json().get("error_description") or r.json().get("error") or "")[:300]
    except Exception:
        return r.text[:200]


def register() -> None:
    from app.services.sources import register as reg
    reg("ebay_uk", EbayBrowseAdapter)
