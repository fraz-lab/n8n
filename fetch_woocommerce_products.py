#!/usr/bin/env python3
"""
Fetch WooCommerce products and save full + flat exports.

Usage examples:
  python fetch_woocommerce_products.py --base-url https://aluxurywatches.com --consumer-key ck_xxx --consumer-secret cs_xxx
  python fetch_woocommerce_products.py --base-url https://aluxurywatches.com --consumer-key ck_xxx --consumer-secret cs_xxx --include-credentials-in-query
  python fetch_woocommerce_products.py  # reads .env / environment variables
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from typing import Any, Dict, List, Tuple

import requests
from requests.auth import HTTPBasicAuth


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE entries from .env into os.environ if not already set."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def require(value: str | None, field_name: str, env_name: str) -> str:
    if value:
        return value
    raise ValueError(
        f"Missing required value for {field_name}. "
        f"Pass --{field_name.replace('_', '-')} or set {env_name} in environment/.env."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export WooCommerce products to JSON/CSV.")
    parser.add_argument(
        "--base-url",
        default=None,
        help="Store URL, e.g. https://aluxurywatches.com (or WC_BASE_URL env var)",
    )
    parser.add_argument(
        "--consumer-key",
        default=None,
        help="WooCommerce consumer key (or WC_KEY env var)",
    )
    parser.add_argument(
        "--consumer-secret",
        default=None,
        help="WooCommerce consumer secret (or WC_SECRET env var)",
    )
    parser.add_argument("--per-page", type=int, default=100, help="Products per page (default: 100)")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds (default: 30)")
    parser.add_argument(
        "--include-credentials-in-query",
        action="store_true",
        help="Pass credentials as query params instead of Authorization header.",
    )
    parser.add_argument(
        "--status",
        default="any",
        help="WooCommerce product status filter (default: any). Example: publish",
    )
    parser.add_argument(
        "--out-json",
        default="woocommerce_products_full.json",
        help="Output file for full JSON export",
    )
    parser.add_argument(
        "--out-csv",
        default="woocommerce_products_flat.csv",
        help="Output file for flat CSV export",
    )
    parser.add_argument(
        "--out-variations-json",
        default="woocommerce_variations_full.json",
        help="Output file for full variations JSON export (variable products only)",
    )
    parser.add_argument(
        "--out-variations-csv",
        default="woocommerce_variations_flat.csv",
        help="Output file for flat variations CSV export (variable products only)",
    )
    parser.add_argument(
        "--fetch-variations",
        action="store_true",
        help="Also fetch variations for variable products and export them",
    )
    parser.add_argument(
        "--variation-retries",
        type=int,
        default=6,
        help="Retry attempts for variation fetch failures (default: 6)",
    )
    parser.add_argument(
        "--variation-retry-sleep",
        type=float,
        default=2.0,
        help="Base sleep seconds between variation retries (default: 2.0). Backoff is applied.",
    )
    return parser.parse_args()


def build_request_auth_and_params(
    key: str,
    secret: str,
    include_credentials_in_query: bool,
    status: str,
    per_page: int,
    page: int,
) -> Tuple[HTTPBasicAuth | None, Dict[str, Any]]:
    params: Dict[str, Any] = {
        "per_page": per_page,
        "page": page,
        "status": status,
    }
    if include_credentials_in_query:
        params["consumer_key"] = key
        params["consumer_secret"] = secret
        return None, params
    return HTTPBasicAuth(key, secret), params


def fetch_products_page(
    base_url: str,
    key: str,
    secret: str,
    include_credentials_in_query: bool,
    status: str,
    per_page: int,
    page: int,
    timeout: int,
) -> Tuple[List[Dict[str, Any]], int, int]:
    url = f"{base_url.rstrip('/')}/wp-json/wc/v3/products"
    auth, params = build_request_auth_and_params(
        key, secret, include_credentials_in_query, status, per_page, page
    )

    response = requests.get(url, params=params, auth=auth, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(
            f"HTTP {response.status_code} while fetching page {page}\n"
            f"URL: {response.url}\n"
            f"Response: {response.text[:1500]}"
        )

    products = response.json()
    total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
    total_items = int(response.headers.get("X-WP-Total", str(len(products))))
    return products, total_pages, total_items


def fetch_variations_page(
    base_url: str,
    product_id: int,
    key: str,
    secret: str,
    include_credentials_in_query: bool,
    per_page: int,
    page: int,
    timeout: int,
) -> Tuple[List[Dict[str, Any]], int, int]:
    url = f"{base_url.rstrip('/')}/wp-json/wc/v3/products/{product_id}/variations"
    auth, params = build_request_auth_and_params(
        key, secret, include_credentials_in_query, status="any", per_page=per_page, page=page
    )

    response = requests.get(url, params=params, auth=auth, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(
            f"HTTP {response.status_code} while fetching variations page {page} for product {product_id}\n"
            f"URL: {response.url}\n"
            f"Response: {response.text[:1500]}"
        )

    variations = response.json()
    total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
    total_items = int(response.headers.get("X-WP-Total", str(len(variations))))
    return variations, total_pages, total_items


def flatten_product(product: Dict[str, Any]) -> Dict[str, Any]:
    def join_names(items: List[Dict[str, Any]], field: str = "name") -> str:
        return ", ".join(str(x.get(field, "")).strip() for x in items if str(x.get(field, "")).strip())

    image_urls = ", ".join(
        str(img.get("src", "")).strip()
        for img in product.get("images", [])
        if str(img.get("src", "")).strip()
    )

    return {
        "id": product.get("id"),
        "name": product.get("name"),
        "slug": product.get("slug"),
        "type": product.get("type"),
        "status": product.get("status"),
        "sku": product.get("sku"),
        "price": product.get("price"),
        "regular_price": product.get("regular_price"),
        "sale_price": product.get("sale_price"),
        "stock_status": product.get("stock_status"),
        "stock_quantity": product.get("stock_quantity"),
        "manage_stock": product.get("manage_stock"),
        "catalog_visibility": product.get("catalog_visibility"),
        "permalink": product.get("permalink"),
        "categories": join_names(product.get("categories", [])),
        "tags": join_names(product.get("tags", [])),
        "images": image_urls,
    }


def flatten_variation(variation: Dict[str, Any], parent_product: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Flatten a variation to a single CSV row.
    Includes parent identifiers to make joins easy.
    """

    def join_attrs(attrs: List[Dict[str, Any]]) -> str:
        parts: List[str] = []
        for a in attrs or []:
            name = str(a.get("name") or a.get("attribute") or "").strip()
            option = str(a.get("option") or "").strip()
            if name and option:
                parts.append(f"{name}={option}")
            elif option:
                parts.append(option)
        return ", ".join(parts)

    parent_id = variation.get("parent_id") or (parent_product.get("id") if parent_product else None)
    parent_name = parent_product.get("name") if parent_product else None
    parent_slug = parent_product.get("slug") if parent_product else None

    attrs = variation.get("attributes") or []
    return {
        "parent_product_id": parent_id,
        "parent_product_name": parent_name,
        "parent_product_slug": parent_slug,
        "variation_id": variation.get("id"),
        "variation_sku": variation.get("sku"),
        "variation_price": variation.get("price"),
        "variation_regular_price": variation.get("regular_price"),
        "variation_sale_price": variation.get("sale_price"),
        "variation_stock_status": variation.get("stock_status"),
        "variation_stock_quantity": variation.get("stock_quantity"),
        "variation_manage_stock": variation.get("manage_stock"),
        "variation_attributes": join_attrs(attrs),
    }


def write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        fieldnames = [
            "id",
            "name",
            "slug",
            "type",
            "status",
            "sku",
            "price",
            "regular_price",
            "sale_price",
            "stock_status",
            "stock_quantity",
            "manage_stock",
            "catalog_visibility",
            "permalink",
            "categories",
            "tags",
            "images",
        ]
    else:
        fieldnames = list(rows[0].keys())

    with open(path, "w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    load_dotenv()
    args = parse_args()
    base_url = args.base_url or os.getenv("WC_BASE_URL")
    consumer_key = args.consumer_key or os.getenv("WC_KEY")
    consumer_secret = args.consumer_secret or os.getenv("WC_SECRET")

    base_url = require(base_url, "base_url", "WC_BASE_URL")
    consumer_key = require(consumer_key, "consumer_key", "WC_KEY")
    consumer_secret = require(consumer_secret, "consumer_secret", "WC_SECRET")

    all_products: List[Dict[str, Any]] = []
    page = 1

    products, total_pages, total_items = fetch_products_page(
        base_url=base_url,
        key=consumer_key,
        secret=consumer_secret,
        include_credentials_in_query=args.include_credentials_in_query,
        status=args.status,
        per_page=args.per_page,
        page=page,
        timeout=args.timeout,
    )
    all_products.extend(products)

    print(f"Total products reported by API: {total_items}")
    print(f"Total pages: {total_pages}")
    print(f"Fetched page 1/{total_pages} (running total: {len(all_products)})")

    for page in range(2, total_pages + 1):
        products, _, _ = fetch_products_page(
            base_url=base_url,
            key=consumer_key,
            secret=consumer_secret,
            include_credentials_in_query=args.include_credentials_in_query,
            status=args.status,
            per_page=args.per_page,
            page=page,
            timeout=args.timeout,
        )
        all_products.extend(products)
        print(f"Fetched page {page}/{total_pages} (running total: {len(all_products)})")

    flat_rows = [flatten_product(product) for product in all_products]
    write_json(args.out_json, all_products)
    write_csv(args.out_csv, flat_rows)

    print("\nExport complete.")
    print(f"JSON: {os.path.abspath(args.out_json)}")
    print(f"CSV:  {os.path.abspath(args.out_csv)}")
    print(f"Rows: {len(all_products)}")

    if args.fetch_variations:
        variable_products = [p for p in all_products if str(p.get("type") or "").lower() == "variable"]
        print(f"\nFetching variations for {len(variable_products)} variable products...")

        all_variations: List[Dict[str, Any]] = []
        flat_variations: List[Dict[str, Any]] = []
        variation_errors: List[Dict[str, Any]] = []

        def write_variations_exports() -> None:
            write_json(args.out_variations_json, all_variations)
            write_csv(args.out_variations_csv, flat_variations)
            err_path = os.path.splitext(args.out_variations_json)[0] + "_errors.json"
            write_json(err_path, variation_errors)

        for idx, p in enumerate(variable_products, start=1):
            pid = p.get("id")
            if not pid:
                continue
            try:
                pid_int = int(pid)
            except Exception:
                continue

            # Fetch all pages of variations for this product with retry/backoff on transient failures.
            try:
                v_page = 1
                variations, v_total_pages, v_total_items = None, 1, 0

                while v_page <= v_total_pages:
                    attempt = 0
                    while True:
                        try:
                            variations, v_total_pages, v_total_items = fetch_variations_page(
                                base_url=base_url,
                                product_id=pid_int,
                                key=consumer_key,
                                secret=consumer_secret,
                                include_credentials_in_query=args.include_credentials_in_query,
                                per_page=args.per_page,
                                page=v_page,
                                timeout=args.timeout,
                            )
                            break
                        except Exception as e:
                            attempt += 1
                            msg = str(e)
                            transient = any(code in msg for code in ["HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504"])
                            if (not transient) or attempt > max(1, args.variation_retries):
                                raise
                            sleep_s = args.variation_retry_sleep * (2 ** (attempt - 1))
                            print(f"  retry {attempt}/{args.variation_retries} product={pid_int} page={v_page} after error; sleeping {sleep_s:.1f}s")
                            time.sleep(sleep_s)

                    all_variations.extend(variations or [])
                    flat_variations.extend(flatten_variation(v, parent_product=p) for v in (variations or []))
                    v_page += 1

            except Exception as e:
                variation_errors.append(
                    {
                        "parent_product_id": pid_int,
                        "parent_product_name": p.get("name"),
                        "error": str(e),
                    }
                )
                print(f"  skipping product {pid_int} due to variations fetch failure: {e}")

            if idx % 25 == 0:
                print(f"  processed {idx}/{len(variable_products)} variable products (variations so far: {len(all_variations)})")
                # Save progress periodically so a later failure doesn't lose work.
                try:
                    write_variations_exports()
                except Exception as e:
                    print(f"  WARNING: failed to write intermediate variations exports: {e}")

        # Final write
        write_variations_exports()

        print("\nVariations export complete.")
        print(f"Variations JSON: {os.path.abspath(args.out_variations_json)}")
        print(f"Variations CSV:  {os.path.abspath(args.out_variations_csv)}")
        print(f"Variations rows: {len(all_variations)}")


if __name__ == "__main__":
    main()
