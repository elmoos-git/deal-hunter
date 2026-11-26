"""Product catalogue: creation, aliases, identifiers, search and title matching (deterministic)."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import BundleComponent, Category, Product, ProductAlias, ProductIdentifier
from app.services.audit import audit

# Words that carry no identity; removed before comparing.
STOPWORDS = {
    "the", "a", "an", "and", "of", "for", "with", "games", "workshop", "gw", "warhammer", "new", "box", "boxed",
    "sealed", "nib", "nos", "on", "sprue", "sprues", "unopened", "unassembled", "set", "kit", "miniatures",
    "miniature", "models", "model", "uk", "free", "postage", "p&p", "bnib", "oop", "rare",
}
# Common abbreviations expanded before matching.
EXPANSIONS = {
    "40k": "40000", "wh40k": "40000", "aos": "age of sigmar", "sm": "space marines", "cp": "combat patrol",
    "bl": "black library", "hh": "horus heresy", "hb": "hardback", "le": "limited edition", "csm": "chaos space marines",
    "kt": "kill team", "tow": "the old world",
}


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = text.replace("40,000", "40000").replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [EXPANSIONS.get(t, t) for t in text.split()]
    return " ".join(" ".join(tokens).split())


def tokens(text: str) -> set[str]:
    return {t for t in normalise(text).split() if t not in STOPWORDS and len(t) > 1}


def get_or_create_category(s: Session, name: str | None) -> Category | None:
    if not name:
        return None
    cat = s.scalar(select(Category).where(Category.name == name))
    if not cat:
        cat = Category(name=name)
        s.add(cat)
        s.flush()
    return cat


def create_product(s: Session, *, name: str, category: str | None = None, aliases: list[str] | None = None,
                   identifiers: dict[str, str] | None = None, **fields) -> Product:
    name = name.strip()
    if not name:
        raise ValueError("Product name is required")
    p = Product(name=name, category=get_or_create_category(s, category), **fields)
    s.add(p)
    s.flush()
    add_alias(s, p, name)
    for a in aliases or []:
        add_alias(s, p, a)
    for kind, value in (identifiers or {}).items():
        if value:
            add_identifier(s, p, kind, value)
    audit(s, "product", p.id, "create", {"name": name})
    return p


def add_alias(s: Session, p: Product, alias: str) -> ProductAlias | None:
    n = normalise(alias)
    if not n:
        return None
    existing = s.scalar(select(ProductAlias).where(ProductAlias.product_id == p.id, ProductAlias.normalised == n))
    if existing:
        return existing
    a = ProductAlias(product_id=p.id, alias=alias.strip(), normalised=n)
    s.add(a)
    s.flush()
    return a


def add_identifier(s: Session, p: Product, kind: str, value: str) -> ProductIdentifier:
    kind = kind.upper().strip()
    value = re.sub(r"[\s-]", "", value)
    clash = s.scalar(select(ProductIdentifier).where(ProductIdentifier.kind == kind, ProductIdentifier.value == value))
    if clash and clash.product_id != p.id:
        raise ValueError(f"{kind} {value} already belongs to product #{clash.product_id}")
    if clash:
        return clash
    ident = ProductIdentifier(product_id=p.id, kind=kind, value=value)
    s.add(ident)
    s.flush()
    return ident


def set_bundle_components(s: Session, bundle: Product, components: list[dict]) -> None:
    """components: [{product_id, quantity, separately_sellable, shared_sprue_group, notes}]"""
    seen = set()
    for c in components:
        if c["product_id"] == bundle.id:
            raise ValueError("A bundle cannot contain itself")
        if c["product_id"] in seen:
            raise ValueError("Duplicate component in bundle")
        seen.add(c["product_id"])
    for old in list(bundle.components):
        s.delete(old)
    s.flush()
    for c in components:
        s.add(BundleComponent(bundle_id=bundle.id, component_id=c["product_id"], quantity=int(c.get("quantity", 1)),
                              separately_sellable=bool(c.get("separately_sellable", True)),
                              shared_sprue_group=c.get("shared_sprue_group") or None, notes=c.get("notes")))
    bundle.is_bundle = bool(components)
    s.flush()
    s.refresh(bundle)
    audit(s, "product", bundle.id, "set_components", {"components": components})


def search(s: Session, q: str, limit: int = 50) -> list[Product]:
    q = (q or "").strip()
    stmt = select(Product).order_by(Product.name).limit(limit)
    if q:
        n = normalise(q)
        like = f"%{n}%"
        ids = select(ProductAlias.product_id).where(ProductAlias.normalised.like(like))
        ident = select(ProductIdentifier.product_id).where(ProductIdentifier.value == re.sub(r"[\s-]", "", q))
        stmt = stmt.where(or_(Product.id.in_(ids), Product.id.in_(ident), Product.sku == q,
                              Product.faction.ilike(f"%{q}%"), Product.game_system.ilike(f"%{q}%")))
    return list(s.scalars(stmt))


@dataclass
class Match:
    product: Product
    score: int  # 0-100
    method: str


def match_title(s: Session, title: str, limit: int = 5) -> list[Match]:
    """Rank catalogue products for a free-text listing title.

    Identifier hit = 100. Otherwise the best alias is scored by how much of the alias's tokens appear in the title
    (containment), with a small bonus for overall overlap. Ambiguous results (two close scores) are left for human review.
    """
    raw = re.sub(r"[\s-]", "", title)
    for ident in s.scalars(select(ProductIdentifier)):
        if len(ident.value) >= 8 and ident.value in raw:
            return [Match(s.get(Product, ident.product_id), 100, f"identifier:{ident.kind}")]
    t_tokens = tokens(title)
    if not t_tokens:
        return []
    best: dict[int, Match] = {}
    for alias in s.scalars(select(ProductAlias)):
        a_tokens = {t for t in alias.normalised.split() if t not in STOPWORDS and len(t) > 1}
        if not a_tokens:
            continue
        inter = len(a_tokens & t_tokens)
        if inter == 0:
            continue
        containment = inter / len(a_tokens)
        jaccard = inter / len(a_tokens | t_tokens)
        score = round(100 * (0.8 * containment + 0.2 * jaccard))
        if containment < 1:
            score = min(score, 79)  # partial alias match is never a confident match
        if score >= 40 and (alias.product_id not in best or best[alias.product_id].score < score):
            best[alias.product_id] = Match(alias.product, score, "alias")
    return sorted(best.values(), key=lambda m: m.score, reverse=True)[:limit]


def confident_match(matches: list[Match], threshold: int = 80, margin: int = 10) -> Match | None:
    if not matches or matches[0].score < threshold:
        return None
    if len(matches) > 1 and matches[0].score - matches[1].score < margin:
        return None  # ambiguous: needs human review
    return matches[0]
