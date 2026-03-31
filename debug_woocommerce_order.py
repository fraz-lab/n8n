#!/usr/bin/env python3
"""
Read-only WooCommerce order debugger.

Purpose:
- Inspect an order and explain why total might be 0.00
- Validate product_id/variation_id pairs in line items
- Check variation pricing directly from Woo API

This script performs GET requests only. It does NOT modify WooCommerce data.

Usage examples:
  python debug_woocommerce_order.py --order-id 138577
  python debug_woocommerce_order.py --order-id 138577 --include-credentials-in-query
  python debug_woocommerce_order.py --order-id 138577 --base-url https://example.com --consumer-key ck_xxx --consumer-secret cs_xxx
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, Optional

import requests
from requests.auth import HTTPBasicAuth


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def require(value: Optional[str], name: str, env_name: str) -> str:
    if value:
        return value
    raise ValueError(f"Missing {name}. Pass --{name.replace('_', '-')} or set {env_name} in .env/environment.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Debug WooCommerce order totals and line-item pricing.")
    p.add_argument("--order-id", required=True, type=int, help="Woo order ID to inspect")
    p.add_argument("--base-url", default=None, help="Store URL (or WC_BASE_URL)")
    p.add_argument("--consumer-key", default=None, help="Woo key (or WC_KEY)")
    p.add_argument("--consumer-secret", default=None, help="Woo secret (or WC_SECRET)")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--include-credentials-in-query", action="store_true")
    return p.parse_args()


def auth_and_params(
    key: str,
    secret: str,
    include_credentials_in_query: bool,
) -> tuple[Optional[HTTPBasicAuth], Dict[str, Any]]:
    if include_credentials_in_query:
        return None, {"consumer_key": key, "consumer_secret": secret}
    return HTTPBasicAuth(key, secret), {}


def get_json(
    url: str,
    key: str,
    secret: str,
    include_credentials_in_query: bool,
    timeout: int,
) -> Dict[str, Any]:
    auth, params = auth_and_params(key, secret, include_credentials_in_query)
    r = requests.get(url, auth=auth, params=params, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"GET {url} -> HTTP {r.status_code}\n{r.text[:1200]}")
    return r.json()


def to_num(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def main() -> None:
    load_dotenv()
    args = parse_args()

    base_url = require(args.base_url or os.getenv("WC_BASE_URL"), "base_url", "WC_BASE_URL")
    key = require(args.consumer_key or os.getenv("WC_KEY"), "consumer_key", "WC_KEY")
    secret = require(args.consumer_secret or os.getenv("WC_SECRET"), "consumer_secret", "WC_SECRET")

    order_url = f"{base_url.rstrip('/')}/wp-json/wc/v3/orders/{args.order_id}"
    order = get_json(order_url, key, secret, args.include_credentials_in_query, args.timeout)

    print(f"Order ID: {order.get('id')}")
    print(f"Status: {order.get('status')}")
    print(f"Currency: {order.get('currency')}")
    print(f"Order total: {order.get('total')}")
    print(f"Order cart_tax: {order.get('cart_tax')}, total_tax: {order.get('total_tax')}, shipping_total: {order.get('shipping_total')}")
    print("-" * 80)

    line_items = order.get("line_items") or []
    if not line_items:
        print("No line_items found on this order.")
        return

    zero_lines = 0
    for i, li in enumerate(line_items, start=1):
        product_id = li.get("product_id")
        variation_id = li.get("variation_id")
        qty = li.get("quantity")
        subtotal = li.get("subtotal")
        total = li.get("total")

        print(f"Line #{i}: product_id={product_id}, variation_id={variation_id}, qty={qty}, subtotal={subtotal}, total={total}")
        li_total = to_num(total)
        if li_total is not None and li_total == 0:
            zero_lines += 1

        # Validate product endpoint
        if product_id:
            try:
                p_url = f"{base_url.rstrip('/')}/wp-json/wc/v3/products/{product_id}"
                product = get_json(p_url, key, secret, args.include_credentials_in_query, args.timeout)
                print(
                    "  Product check:"
                    f" type={product.get('type')},"
                    f" price={product.get('price')},"
                    f" regular_price={product.get('regular_price')},"
                    f" stock_status={product.get('stock_status')}"
                )
            except Exception as e:
                print(f"  Product check failed: {e}")

        # Validate variation endpoint + parent consistency
        if variation_id:
            try:
                v_url = f"{base_url.rstrip('/')}/wp-json/wc/v3/products/{product_id}/variations/{variation_id}"
                variation = get_json(v_url, key, secret, args.include_credentials_in_query, args.timeout)
                print(
                    "  Variation check:"
                    f" id={variation.get('id')},"
                    f" parent_id={variation.get('parent_id')},"
                    f" price={variation.get('price')},"
                    f" regular_price={variation.get('regular_price')},"
                    f" stock_status={variation.get('stock_status')}"
                )
                if str(variation.get("parent_id")) != str(product_id):
                    print("  WARNING: variation parent_id does not match line-item product_id.")
            except Exception as e:
                print(f"  Variation check failed (likely mismatch product/variation): {e}")
        else:
            print("  Variation check: no variation_id on this line item.")

        print("-" * 80)

    if to_num(order.get("total")) == 0:
        print("DIAGNOSIS: Order total is 0.00.")
        print("Most common causes:")
        print("1) variation_id is missing/invalid for a variable product")
        print("2) variation price is empty/0 in WooCommerce")
        print("3) line item totals/subtotals were overridden as blank/zero in request")
    elif zero_lines > 0:
        print("DIAGNOSIS: Some line items are 0.00 even if order total is not 0.")
    else:
        print("DIAGNOSIS: Pricing appears non-zero on line items and order total.")


if __name__ == "__main__":
    main()

