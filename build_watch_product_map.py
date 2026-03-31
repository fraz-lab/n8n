#!/usr/bin/env python3
"""
Build a starter watch-product mapping CSV from WooCommerce export.

Input:
  - woocommerce_products_flat.csv
  - woocommerce_variations_flat.csv (optional, produced by fetch_woocommerce_products.py --fetch-variations)

Output:
  - watch_product_map.csv

Usage:
  python build_watch_product_map.py
  python build_watch_product_map.py --input woocommerce_products_flat.csv --output watch_product_map.csv
  python build_watch_product_map.py --include-variations
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import urllib.parse
from typing import Dict, List, Optional, Tuple


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create starter watch_product_map.csv from WooCommerce export.")
    parser.add_argument("--input", default="woocommerce_variations_flat.csv", help="Path to WooCommerce flat CSV")
    parser.add_argument(
        "--variations",
        default="woocommerce_variations_flat.csv",
        help="Path to WooCommerce variations flat CSV (optional)",
    )
    parser.add_argument(
        "--include-variations",
        action="store_true",
        help="If set, populate quality-specific variation id columns from variations CSV",
    )
    parser.add_argument("--output", default="watch_product_map.csv", help="Output mapping CSV")
    parser.add_argument(
        "--only-published",
        action="store_true",
        help="Include only rows where status=publish",
    )
    parser.add_argument(
        "--only-instock",
        action="store_true",
        help="Include only rows where stock_status=instock",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print detailed debug counters and sample rows",
    )
    return parser.parse_args()


def norm_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def slugify(text: str) -> str:
    text = norm_space(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def clean_slug(raw_slug: str) -> str:
    # Woo slugs in export may be percent-encoded Hebrew; keep decoded text and fallback
    decoded = urllib.parse.unquote(raw_slug or "")
    decoded = decoded.replace("__trashed", "")
    return norm_space(decoded)


def extract_reference(name: str, slug: str) -> str:
    # Try common "model/ref" tokens from name or slug:
    # examples: 5168G-001, 220.92.43.22.99.001, 98803353265, WHSA0015
    text = f"{name} {slug}"
    patterns = [
        r"\b[A-Z]{2,}\d{3,}(?:[-/]\d+)?\b",               # WHSA0015 / PAM01232 style
        r"\b\d{2,}(?:[.-]\d{2,}){2,}\b",                  # 220.92.43.22.99.001 style
        r"\b\d{5,}(?:[-/]\d+)?\b",                        # long numeric refs
        r"\b\d{4,}[A-Z](?:[-/]\d+)?\b",                   # 5168G-001 style
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return m.group(0).upper()
    return ""


def infer_brand(name: str, categories: str) -> str:
    source = f"{name} {categories}".lower()
    brands = [
        "rolex", "patek philippe", "audemars piguet", "omega", "cartier",
        "panerai", "breitling", "hublot", "iwc", "tag heuer", "richard mille",
    ]
    for b in brands:
        if b in source:
            return b
    return "unknown"


def make_watch_key(name: str, slug: str, categories: str) -> str:
    ref = extract_reference(name, slug)
    brand = infer_brand(name, categories)
    # keep key short and stable for lookup from vision outputs
    base = f"{brand} {ref}" if ref else f"{brand} {name}"
    return slugify(base)


def make_watch_id(watch_key: str) -> str:
    """Create a starter internal watch_id from watch_key."""
    if not watch_key:
        return "UNKNOWN_WATCH"
    return watch_key.upper().replace("-", "_")


def read_rows(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)

def _to_price_number(value: str) -> Optional[float]:
    raw = (value or "").strip()
    if not raw:
        return None
    # keep digits and dot only
    raw = re.sub(r"[^0-9.]+", "", raw)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def detect_quality_label(text: str) -> str:
    """
    Try to detect quality tier from variation attributes / title text.

    In your data this usually appears in Hebrew attributes like:
      רמת גימור=דרגה-aaa-...
      רמת גימור=דרגה-aaaa-שוויצרי-...
      רמת גימור=דרגה-superclone-aaaaa-...
    """
    t = (text or "").lower()
    t = t.replace("+", "")  # normalize AAA+ -> aaa

    # Parse exact Hebrew quality phrase when present:
    #   "רמת גימור=דרגה-superclone-aaaaa-ב-3996₪"
    #   "רמת גימור=דרגה-aaaa-שוויצרי-ב-2789₪"
    #   "רמת גימור=דרגה-aaa-ב-1842₪"
    m = re.search(r"רמת\s*גימור\s*=\s*דרגה-([^,]+)", t)
    quality_chunk = m.group(1) if m else t

    # Prefer most specific first to avoid AAA matching inside AAAAA.
    if "aaaaa" in quality_chunk:
        return "AAAAA+"
    if re.search(r"(?<!a)aaaa(?!a)", quality_chunk):
        return "AAAA+"
    if re.search(r"(?<!a)aaa(?!a)", quality_chunk):
        return "AAA+"
    return ""


def build_variation_map(variation_rows: List[Dict[str, str]]) -> Dict[str, Dict[str, Tuple[str, Optional[float]]]]:
    """
    Returns:
      parent_product_id -> { "AAA+": (variation_id, price), "AAAA+": (...), "AAAAA+": (...) }
    Chooses the lowest numeric price per quality when duplicates exist.
    """
    out: Dict[str, Dict[str, Tuple[str, Optional[float]]]] = {}
    for r in variation_rows:
        parent_id = str(r.get("parent_product_id") or "").strip()
        var_id = str(r.get("variation_id") or "").strip()
        if not parent_id or not var_id:
            continue

        hint = " ".join(
            [
                str(r.get("variation_attributes") or ""),
                str(r.get("parent_product_name") or ""),
                str(r.get("parent_product_slug") or ""),
            ]
        )
        q = detect_quality_label(hint)
        if not q:
            continue

        price = _to_price_number(str(r.get("variation_price") or ""))
        bucket = out.setdefault(parent_id, {})
        if q not in bucket:
            bucket[q] = (var_id, price)
            continue

        # If we already have a variation for this quality, prefer the cheaper one when prices exist.
        existing_id, existing_price = bucket[q]
        if existing_price is None and price is not None:
            bucket[q] = (var_id, price)
        elif existing_price is not None and price is not None and price < existing_price:
            bucket[q] = (var_id, price)
        else:
            # keep existing
            bucket[q] = (existing_id, existing_price)
    return out


def write_rows(path: str, rows: List[Dict[str, str]]) -> None:
    fieldnames = [
        "watch_key",
        "watch_id",
        "woo_product_id",
        "woo_product_type",
        "woo_variation_id",
        "woo_variation_id_aaa_plus",
        "woo_variation_id_aaaa_plus",
        "woo_variation_id_aaaaa_plus",
        "quality_categories",
        "display_name",
        "product_slug",
        "reference_hint",
        "brand_hint",
        "status",
        "stock_status",
        "price",
        "comparison_image_url",
        "notes",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    if not os.path.exists(args.input):
        raise FileNotFoundError(f"Input CSV not found: {args.input}")

    raw_rows = read_rows(args.input)
    if args.debug:
        print(f"DEBUG: input file = {os.path.abspath(args.input)}")
        print(f"DEBUG: total raw product rows = {len(raw_rows)}")
        if raw_rows:
            print(f"DEBUG: product headers = {list(raw_rows[0].keys())}")
            for i, sample in enumerate(raw_rows[:3], start=1):
                print(
                    "DEBUG: sample row "
                    f"{i}: id={sample.get('id')!r}, "
                    f"status={sample.get('status')!r}, "
                    f"stock_status={sample.get('stock_status')!r}, "
                    f"name={str(sample.get('name') or '')[:80]!r}"
                )
    variation_map: Dict[str, Dict[str, Tuple[str, Optional[float]]]] = {}
    if args.include_variations:
        if not os.path.exists(args.variations):
            raise FileNotFoundError(f"Variations CSV not found: {args.variations}")
        variation_rows = read_rows(args.variations)
        variation_map = build_variation_map(variation_rows)
        print(f"Loaded variations: {len(variation_rows)} rows; mapped parents: {len(variation_map)}")
        if args.debug and variation_rows:
            print(f"DEBUG: variations file = {os.path.abspath(args.variations)}")
            print(f"DEBUG: variations headers = {list(variation_rows[0].keys())}")
            for i, sample in enumerate(variation_rows[:3], start=1):
                print(
                    "DEBUG: variation sample "
                    f"{i}: parent_product_id={sample.get('parent_product_id')!r}, "
                    f"variation_id={sample.get('variation_id')!r}, "
                    f"variation_attributes={str(sample.get('variation_attributes') or '')[:120]!r}"
                )

    out_rows: List[Dict[str, str]] = []
    used_watch_ids: Dict[str, int] = {}
    skipped_status = 0
    skipped_stock = 0
    skipped_empty_name = 0
    matched_variation_parents = 0

    for r in raw_rows:
        status = (r.get("status") or "").strip().lower()
        stock_status = (r.get("stock_status") or "").strip().lower()
        if args.only_published and status != "publish":
            skipped_status += 1
            continue
        if args.only_instock and stock_status != "instock":
            skipped_stock += 1
            continue

        name = norm_space(r.get("name") or "")
        if not name:
            skipped_empty_name += 1
            continue

        product_slug = clean_slug(r.get("slug") or "")
        brand_hint = infer_brand(name, r.get("categories") or "")
        reference_hint = extract_reference(name, product_slug)
        watch_key = make_watch_key(name, product_slug, r.get("categories") or "")
        base_watch_id = make_watch_id(watch_key)
        count = used_watch_ids.get(base_watch_id, 0) + 1
        used_watch_ids[base_watch_id] = count
        watch_id = base_watch_id if count == 1 else f"{base_watch_id}_{count}"

        woo_product_id = str(r.get("id") or "").strip()
        var_for_parent = variation_map.get(woo_product_id, {}) if variation_map else {}
        if var_for_parent:
            matched_variation_parents += 1
        v_aaa = var_for_parent.get("AAA+", ("", None))[0] if var_for_parent else ""
        v_aaaa = var_for_parent.get("AAAA+", ("", None))[0] if var_for_parent else ""
        v_aaaaa = var_for_parent.get("AAAAA+", ("", None))[0] if var_for_parent else ""
        quality_categories = []
        if v_aaa:
            quality_categories.append("AAA+")
        if v_aaaa:
            quality_categories.append("AAAA+")
        if v_aaaaa:
            quality_categories.append("AAAAA+")

        out_rows.append(
            {
                "watch_key": watch_key,
                "watch_id": watch_id,  # auto-generated starter ID; review/edit if needed
                "woo_product_id": woo_product_id,
                "woo_product_type": str(r.get("type") or "").strip(),
                "woo_variation_id": "",  # legacy single-variation field (kept for compatibility)
                "woo_variation_id_aaa_plus": v_aaa,
                "woo_variation_id_aaaa_plus": v_aaaa,
                "woo_variation_id_aaaaa_plus": v_aaaaa,
                "quality_categories": "|".join(quality_categories),
                "display_name": name,
                "product_slug": product_slug,
                "reference_hint": reference_hint,
                "brand_hint": brand_hint,
                "status": status,
                "stock_status": stock_status,
                "price": str(r.get("price") or "").strip(),
                "comparison_image_url": "",  # optional fill
                "notes": "",
            }
        )

    write_rows(args.output, out_rows)
    print(f"Done. Wrote {len(out_rows)} rows to: {os.path.abspath(args.output)}")
    if args.debug:
        print(
            "DEBUG: counters -> "
            f"skipped_status={skipped_status}, "
            f"skipped_stock={skipped_stock}, "
            f"skipped_empty_name={skipped_empty_name}, "
            f"matched_variation_parents={matched_variation_parents}"
        )
        if args.only_published or args.only_instock:
            print(
                "DEBUG: filters enabled -> "
                f"only_published={args.only_published}, "
                f"only_instock={args.only_instock}"
            )
    if args.include_variations:
        print("Next: review variation quality mapping columns (AAA+/AAAA+/AAAAA+) and ensure they match your Woo attributes.")
    else:
        print("Next: fill watch_id and (if variable product) woo_variation_id as needed.")


if __name__ == "__main__":
    main()

