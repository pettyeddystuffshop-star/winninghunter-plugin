#!/usr/bin/env python3
"""Phase A, Step 1: read-only Shopify catalog, collection & sub-collection fetcher.

Pulls every ACTIVE product (variants, prices, unit cost, inventory, metafields,
collection membership), every collection (rules, SEO, metafields, member
products), navigation menus, and 30/90-day sales per product and variant.
Then derives the primary-collection -> sub-collection hierarchy.

Every GraphQL document passes through `assert_read_only_graphql`; the client is
built with the read-only token. There is no code path in this file that can
mutate the store.

Usage:
  python3 shopify_readonly_audit.py --out work/2026-11-27/catalog.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    get_logger, load_env, money, shopify_client_from_env, store_today, utc_now_iso, write_json,
)

log = get_logger("shopify-audit")

SHOP_Q = """
query Shop { shop { name myshopifyDomain ianaTimezone currencyCode primaryDomain { host } } }
"""

PRODUCTS_Q = """
query ActiveProducts($cursor: String, $pageSize: Int!) {
  products(first: $pageSize, after: $cursor, query: "status:active", sortKey: ID) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id title handle status vendor productType tags createdAt updatedAt totalInventory onlineStoreUrl
      seo { title description }
      options { name values }
      priceRangeV2 { minVariantPrice { amount } maxVariantPrice { amount } }
      variants(first: 100) { edges { node {
        id title sku price compareAtPrice inventoryQuantity availableForSale
        selectedOptions { name value }
        inventoryItem { unitCost { amount } }
      } } }
      metafields(first: 20) { edges { node { namespace key type value } } }
      collections(first: 25) { edges { node { id handle title } } }
    } }
  }
}
"""

COLLECTIONS_Q = """
query Collections($cursor: String) {
  collections(first: 50, after: $cursor, sortKey: ID) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id handle title updatedAt sortOrder
      productsCount { count }
      seo { title description }
      ruleSet { appliedDisjunctively rules { column relation condition } }
      metafields(first: 20) { edges { node { namespace key type value } } }
    } }
  }
}
"""

COLLECTION_PRODUCTS_Q = """
query CollectionProducts($id: ID!, $cursor: String) {
  collection(id: $id) {
    products(first: 250, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      edges { node { id status } }
    }
  }
}
"""

MENUS_Q = """
query Menus($cursor: String) {
  menus(first: 25, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id handle title
      items { id title type resourceId url
        items { id title type resourceId url
          items { id title type resourceId url } } }
    } }
  }
}
"""

ORDERS_Q = """
query RecentOrders($cursor: String, $q: String!) {
  orders(first: 20, after: $cursor, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id createdAt cancelledAt
      lineItems(first: 25) { edges { node {
        quantity
        product { id }
        variant { id }
        originalTotalSet { shopMoney { amount } }
      } } }
    } }
  }
}
"""


def flatten_edges(conn):
    return [e["node"] for e in (conn or {}).get("edges", [])]


def fetch_products(gql, page_size: int):
    raw = gql.paginate(PRODUCTS_Q, ["products"], {"pageSize": page_size})
    products = []
    for p in raw:
        variants = []
        for v in flatten_edges(p.pop("variants")):
            variants.append({
                "id": v["id"], "title": v["title"], "sku": v.get("sku"),
                "price": money(v.get("price")), "compare_at_price": money(v.get("compareAtPrice")),
                "inventory_quantity": v.get("inventoryQuantity"), "available_for_sale": v.get("availableForSale"),
                "options": {o["name"]: o["value"] for o in v.get("selectedOptions") or []},
                "unit_cost": money(((v.get("inventoryItem") or {}).get("unitCost") or {}).get("amount")),
            })
        p["variants"] = variants
        p["metafields"] = flatten_edges(p.pop("metafields"))
        p["collections"] = flatten_edges(p.pop("collections"))
        pr = p.pop("priceRangeV2") or {}
        p["price_min"] = money((pr.get("minVariantPrice") or {}).get("amount"))
        p["price_max"] = money((pr.get("maxVariantPrice") or {}).get("amount"))
        products.append(p)
    return products


def fetch_collections(gql):
    cols = gql.paginate(COLLECTIONS_Q, ["collections"])
    for c in cols:
        c["metafields"] = flatten_edges(c.pop("metafields"))
        c["products_count"] = (c.pop("productsCount") or {}).get("count")
        c["is_smart"] = bool(c.get("ruleSet"))
        members = gql.paginate(COLLECTION_PRODUCTS_Q, ["collection", "products"], {"id": c["id"]})
        c["product_ids"] = [m["id"] for m in members]
        c["active_product_ids"] = [m["id"] for m in members if m["status"] == "ACTIVE"]
    return cols


def fetch_menus(gql, warnings):
    try:
        return gql.paginate(MENUS_Q, ["menus"])
    except Exception as exc:  # missing read_online_store_navigation scope is common
        warnings.append(f"Menus not readable (needs read_online_store_navigation scope): {exc}")
        return []


def fetch_sales(gql, days: int, warnings):
    since = (store_today() - dt.timedelta(days=days)).isoformat()
    cutoff_30 = store_today() - dt.timedelta(days=30)
    try:
        orders = gql.paginate(ORDERS_Q, ["orders"], {"q": f"created_at:>={since} -status:cancelled"})
    except Exception as exc:
        warnings.append(f"Orders not readable (needs read_orders; >60 days needs read_all_orders): {exc}")
        return {}, {}
    per_product = defaultdict(lambda: {"units_30d": 0, "revenue_30d": 0.0, "units_90d": 0, "revenue_90d": 0.0, "orders_90d": 0})
    per_variant = defaultdict(lambda: {"units_30d": 0, "units_90d": 0})
    for o in orders:
        if o.get("cancelledAt"):
            continue
        created = dt.date.fromisoformat(o["createdAt"][:10])
        seen = set()
        for li in flatten_edges(o["lineItems"]):
            pid = (li.get("product") or {}).get("id")
            vid = (li.get("variant") or {}).get("id")
            if not pid:
                continue
            qty = int(li.get("quantity") or 0)
            amount = money(((li.get("originalTotalSet") or {}).get("shopMoney") or {}).get("amount")) or 0.0
            rec = per_product[pid]
            rec["units_90d"] += qty
            rec["revenue_90d"] += amount
            if pid not in seen:
                rec["orders_90d"] += 1
                seen.add(pid)
            if vid:
                per_variant[vid]["units_90d"] += qty
            if created >= cutoff_30:
                rec["units_30d"] += qty
                rec["revenue_30d"] += amount
                if vid:
                    per_variant[vid]["units_30d"] += qty
    for rec in per_product.values():
        rec["revenue_30d"] = round(rec["revenue_30d"], 2)
        rec["revenue_90d"] = round(rec["revenue_90d"], 2)
    return dict(per_product), dict(per_variant)


def derive_hierarchy(collections, menus, warnings):
    """Map primary collections to sub-collections using three signals:
    1. collection metafield (SUBCOLLECTION_PARENT_METAFIELD, default custom.parent_collection)
    2. nested COLLECTION items in navigation menus
    3. smart-collection tag rules of the form `parent:<parent-handle>`
    """
    by_id = {c["id"]: c for c in collections}
    by_handle = {c["handle"]: c for c in collections}
    excluded = {h.strip() for h in os.environ.get("EXCLUDED_COLLECTION_HANDLES", "frontpage,all").split(",") if h.strip()}
    ns, _, key = os.environ.get("SUBCOLLECTION_PARENT_METAFIELD", "custom.parent_collection").partition(".")
    children: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    top_level_menu: set[str] = set()

    for c in collections:
        for mf in c["metafields"]:
            if mf["namespace"] == ns and mf["key"] == key and mf["value"]:
                parent = by_id.get(mf["value"]) or by_handle.get(mf["value"])
                if parent:
                    children[parent["handle"]][c["handle"]].add("metafield")
        for rule in ((c.get("ruleSet") or {}).get("rules") or []):
            cond = (rule.get("condition") or "").strip()
            if rule.get("column") == "TAG" and rule.get("relation") == "EQUALS" and cond.lower().startswith("parent:"):
                parent_handle = cond.split(":", 1)[1].strip()
                if parent_handle in by_handle:
                    children[parent_handle][c["handle"]].add("tag_rule")

    def walk(items, parent_handle, depth, menu_handle):
        for it in items or []:
            handle = by_id[it["resourceId"]]["handle"] if it.get("type") == "COLLECTION" and it.get("resourceId") in by_id else None
            if handle and parent_handle:
                children[parent_handle][handle].add(f"menu:{menu_handle}")
            if handle and depth == 0:
                top_level_menu.add(handle)
            walk(it.get("items"), handle or parent_handle, depth + 1, menu_handle)

    for m in menus:
        walk(m.get("items"), None, 0, m["handle"])

    override = [h.strip() for h in os.environ.get("PRIMARY_COLLECTION_HANDLES", "").split(",") if h.strip()]
    child_handles = {ch for kids in children.values() for ch in kids}
    if override:
        primaries = override
    else:
        primaries = sorted((set(children) | top_level_menu) - child_handles - excluded)
        if not primaries:
            primaries = sorted(set(by_handle) - child_handles - excluded)
            warnings.append("No navigation/metafield hierarchy found; every non-child collection is treated as primary.")
    hierarchy = []
    for h in primaries:
        if h not in by_handle:
            warnings.append(f"PRIMARY_COLLECTION_HANDLES lists unknown handle {h!r}")
            continue
        kids = children.get(h, {})
        hierarchy.append({
            "handle": h,
            "collection_id": by_handle[h]["id"],
            "sub_collections": [{"handle": k, "collection_id": by_handle[k]["id"], "link_sources": sorted(v)} for k, v in sorted(kids.items())],
        })
    return hierarchy


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="Output catalog JSON path")
    ap.add_argument("--sales-days", type=int, default=90)
    ap.add_argument("--page-size", type=int, default=int(os.environ.get("SHOPIFY_PRODUCT_PAGE_SIZE", "6")))
    args = ap.parse_args()

    load_env()
    if os.environ.get("WPC_MODE", "plan") != "plan":
        log.info("WPC_MODE=%s; this script is read-only regardless.", os.environ.get("WPC_MODE"))
    gql = shopify_client_from_env(read_only=True)
    warnings: list[str] = []

    shop = gql.execute(SHOP_Q)["shop"]
    log.info("Auditing %s", shop["myshopifyDomain"])
    products = fetch_products(gql, args.page_size)
    log.info("Fetched %d active products", len(products))
    collections = fetch_collections(gql)
    log.info("Fetched %d collections", len(collections))
    menus = fetch_menus(gql, warnings)
    sales_p, sales_v = fetch_sales(gql, args.sales_days, warnings)
    for p in products:
        p["sales"] = sales_p.get(p["id"], {"units_30d": 0, "revenue_30d": 0.0, "units_90d": 0, "revenue_90d": 0.0, "orders_90d": 0})
        for v in p["variants"]:
            v["sales"] = sales_v.get(v["id"], {"units_30d": 0, "units_90d": 0})
    hierarchy = derive_hierarchy(collections, menus, warnings)

    catalog = {
        "catalog_version": "1.0",
        "generated_at": utc_now_iso(),
        "store_date": store_today().isoformat(),
        "shop": shop,
        "read_only": True,
        "products": products,
        "collections": collections,
        "menus": menus,
        "hierarchy": hierarchy,
        "warnings": warnings,
    }
    write_json(args.out, catalog)
    for w in warnings:
        log.warning(w)
    log.info("Wrote %s (%d products, %d primary collections)", args.out, len(products), len(hierarchy))
    return 0


if __name__ == "__main__":
    sys.exit(main())
