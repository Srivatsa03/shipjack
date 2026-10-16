"""Onboarding a merchant: read its catalog the way merchants actually have it, and its policy pages.

Two catalog formats:

* **Shopify's product export CSV** (Admin > Products > Export). One row per variant: the first row of a
  ``Handle`` carries the title and description, the next rows only the variant (options, SKU, price).
  Each variant becomes a product; a gift card, or a variant that doesn't require shipping, is digital.
  Draft and archived products are skipped.
* **A simple CSV**: ``id,title,price`` plus optional ``description`` and ``kind`` (price in dollars).

Bad rows are skipped with a reason instead of failing the whole import, because a real export always
has a few.
"""

from __future__ import annotations

import csv
import html
import io
import re
from dataclasses import dataclass, field

from .catalog import Product

MAX_PRODUCTS = 5000
MAX_BYTES = 2_000_000


@dataclass
class CatalogImport:
    products: list[Product] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    format: str = ""


def _slug(text: str, limit: int = 64) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


def _cents(value: str) -> int | None:
    value = (value or "").strip().replace("$", "").replace(",", "")
    try:
        cents = round(float(value) * 100)
    except ValueError:
        return None
    return cents if 0 < cents < 10_000_000 else None


def _text(body: str, limit: int = 1200) -> str:
    """Shopify's ``Body (HTML)`` as plain text: tags removed, entities decoded, whitespace collapsed."""
    plain = re.sub(r"<(script|style)\b.*?</\1>", " ", body or "", flags=re.DOTALL | re.IGNORECASE)
    plain = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", ". ", plain, flags=re.IGNORECASE)
    plain = html.unescape(re.sub(r"<[^>]+>", " ", plain))
    plain = re.sub(r"\s+", " ", plain)
    plain = re.sub(r"\s+([,.;:!?])", r"\1", plain)
    return re.sub(r"(\.\s*){2,}", ". ", plain).strip()[:limit]


def parse_catalog(data: str | bytes) -> CatalogImport:
    """Read a Shopify product export or a simple CSV. Raises ValueError when it is neither."""
    if isinstance(data, bytes):
        if len(data) > MAX_BYTES:
            raise ValueError(f"the file is larger than {MAX_BYTES // 1_000_000} MB")
        data = data.decode("utf-8-sig", errors="replace")
    elif len(data.encode()) > MAX_BYTES:
        raise ValueError(f"the file is larger than {MAX_BYTES // 1_000_000} MB")
    reader = csv.DictReader(io.StringIO(data.lstrip("﻿")))
    columns = {c.strip() for c in reader.fieldnames or []}
    rows = [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]
    if {"Handle", "Title", "Variant Price"} <= columns:
        out = _shopify(rows)
    elif {"id", "title", "price"} <= {c.lower() for c in columns}:
        out = _simple(rows)
    else:
        raise ValueError("not a Shopify product export (Handle, Title, Variant Price) or a simple catalog "
                         "(id, title, price)")
    if len(out.products) > MAX_PRODUCTS:
        out.skipped.append(f"{len(out.products) - MAX_PRODUCTS} products over the {MAX_PRODUCTS} limit")
        out.products = out.products[:MAX_PRODUCTS]
    ids = [p.id for p in out.products]
    if len(set(ids)) != len(ids):
        seen, unique = set(), []
        for p in out.products:
            if p.id in seen:
                out.skipped.append(f"{p.id}: duplicate id")
            else:
                seen.add(p.id)
                unique.append(p)
        out.products = unique
    return out


def _shopify(rows: list[dict]) -> CatalogImport:
    out = CatalogImport(format="shopify")
    head: dict[str, dict] = {}
    variants: dict[str, list[dict]] = {}
    for n, row in enumerate(rows, 2):
        handle = row.get("Handle", "")
        if not handle:
            out.skipped.append(f"row {n}: no Handle")
            continue
        if row.get("Title"):
            head.setdefault(handle, row)
        if row.get("Variant Price"):
            variants.setdefault(handle, []).append(row)
    for handle, first in head.items():
        status = first.get("Status", "active").lower() or "active"
        if status != "active" or first.get("Published", "TRUE").upper() == "FALSE":
            out.skipped.append(f"{handle}: {status if status != 'active' else 'not published'}")
            continue
        found = variants.get(handle, [])
        if not found:
            out.skipped.append(f"{handle}: no variant with a price")
            continue
        gift = first.get("Gift Card", "").upper() == "TRUE"
        description = " ".join(x for x in (_text(first.get("Body (HTML)", "")),
                                           f"By {first['Vendor']}." if first.get("Vendor") else "",
                                           f"Category: {first.get('Type')}." if first.get("Type") else "") if x)
        for v in found:
            options = [v.get(f"Option{i} Value", "") for i in (1, 2, 3)]
            options = [o for o in options if o and o.lower() != "default title"]
            price = _cents(v.get("Variant Price", ""))
            pid = _slug(handle if len(found) == 1 else f"{handle}-{v.get('Variant SKU') or '-'.join(options)}")
            if price is None:
                out.skipped.append(f"{pid or handle}: price {v.get('Variant Price')!r} is not a price")
                continue
            if not pid:
                out.skipped.append(f"{handle}: no usable id")
                continue
            digital = gift or v.get("Variant Requires Shipping", "TRUE").upper() == "FALSE"
            title = first["Title"] + (f" ({' / '.join(options)})" if options and len(found) > 1 else "")
            out.products.append(Product(pid, title[:200], price, kind="digital" if digital else "physical",
                                        description=description))
    return out


def _simple(rows: list[dict]) -> CatalogImport:
    out = CatalogImport(format="simple")
    for n, raw in enumerate(rows, 2):
        row = {k.lower(): v for k, v in raw.items()}
        pid, title, price = _slug(row.get("id", "")), row.get("title", ""), _cents(row.get("price", ""))
        if not pid or not title:
            out.skipped.append(f"row {n}: id and title are required")
            continue
        if price is None:
            out.skipped.append(f"{pid}: price {row.get('price')!r} is not a price")
            continue
        kind = "digital" if row.get("kind", "").lower() == "digital" else "physical"
        out.products.append(Product(pid, title[:200], price, kind=kind, description=row.get("description", "")[:1200]))
    return out
