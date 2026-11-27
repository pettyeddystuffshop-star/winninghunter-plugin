"""Synthetic but realistic fixtures (pet store: litter boxes & water fountains).

`python3 tests/fixtures.py` regenerates examples/*.example.json, which double
as documentation of every input format Claude must produce.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
TODAY = NOW.date()
TS = NOW.isoformat().replace("+00:00", "Z")


def d(days_ago: int) -> str:
    return (TODAY - dt.timedelta(days=days_ago)).isoformat()


def gid(kind, n):
    return f"gid://shopify/{kind}/{n}"


def catalog():
    products = []
    # 22 healthy litter-box products, 1 failing fountain, 19 fountains (fountains collection short)
    for n in range(1, 23):
        products.append({"id": gid("Product", 1000 + n), "title": f"Self-Cleaning Litter Box #{n}", "handle": f"litter-box-{n}",
                         "status": "ACTIVE", "variants": [], "collections": [], "sales": {"units_30d": 10}})
    for n in range(1, 21):
        products.append({"id": gid("Product", 2000 + n), "title": f"Cat Water Fountain #{n}", "handle": f"fountain-{n}",
                         "status": "ACTIVE", "variants": [], "collections": [], "sales": {"units_30d": 5}})
    lb = [p["id"] for p in products if "Litter" in p["title"]]
    wf = [p["id"] for p in products if "Fountain" in p["title"]]
    cols = [
        {"id": gid("Collection", 1), "handle": "automatic-litter-boxes", "title": "Automatic Litter Boxes", "active_product_ids": lb, "product_ids": lb, "metafields": [], "ruleSet": None},
        {"id": gid("Collection", 11), "handle": "multi-cat-litter-boxes", "title": "Multi-Cat Litter Boxes", "active_product_ids": lb[:20], "product_ids": lb[:20], "metafields": [], "ruleSet": None},
        {"id": gid("Collection", 12), "handle": "small-space-litter-boxes", "title": "Small-Space Litter Boxes", "active_product_ids": lb[:20], "product_ids": lb[:20], "metafields": [], "ruleSet": None},
        {"id": gid("Collection", 13), "handle": "quiet-litter-boxes", "title": "Quiet Litter Boxes", "active_product_ids": lb[2:22], "product_ids": lb[2:22], "metafields": [], "ruleSet": None},
        {"id": gid("Collection", 14), "handle": "odor-control-litter-boxes", "title": "Odor-Control Litter Boxes", "active_product_ids": lb[1:21], "product_ids": lb[1:21], "metafields": [], "ruleSet": None},
        {"id": gid("Collection", 2), "handle": "cat-water-fountains", "title": "Cat Water Fountains", "active_product_ids": wf, "product_ids": wf, "metafields": [], "ruleSet": None},
        {"id": gid("Collection", 21), "handle": "large-capacity-fountains", "title": "Large-Capacity Fountains (4L+)", "active_product_ids": wf[:5], "product_ids": wf[:5], "metafields": [], "ruleSet": None},
    ]
    hierarchy = [
        {"handle": "automatic-litter-boxes", "collection_id": gid("Collection", 1), "sub_collections": [
            {"handle": h, "collection_id": gid("Collection", n), "link_sources": ["menu:main-menu"]}
            for h, n in (("multi-cat-litter-boxes", 11), ("small-space-litter-boxes", 12), ("quiet-litter-boxes", 13), ("odor-control-litter-boxes", 14))]},
        {"handle": "cat-water-fountains", "collection_id": gid("Collection", 2), "sub_collections": [
            {"handle": "large-capacity-fountains", "collection_id": gid("Collection", 21), "link_sources": ["metafield"]}]},
    ]
    return {"catalog_version": "1.0", "generated_at": TS, "store_date": TODAY.isoformat(),
            "shop": {"name": "Purrfect Home Co", "myshopifyDomain": "purrfect-home.myshopify.com", "ianaTimezone": "America/New_York", "currencyCode": "USD"},
            "read_only": True, "products": products, "collections": cols, "menus": [], "hierarchy": hierarchy, "warnings": []}


def quote(platform, unit, ship, dmax, listing, variant_key=None, stock=500, status="QUOTED"):
    q = {"platform": platform, "status": status, "listing_id": listing, "listing_url": f"https://example.invalid/{platform}/{listing}",
         "supplier_name": f"{platform} seller", "variant_key": variant_key, "unit_price_usd": unit, "shipping_cost_usd": ship,
         "shipping_method": "US warehouse standard", "delivery_min_days": max(2, dmax - 4), "delivery_max_days": dmax,
         "ship_from": "US", "stock": stock, "checked_at": TS, "source": f"mcp:{platform}"}
    if status != "QUOTED":
        for k in ("unit_price_usd", "shipping_cost_usd", "delivery_min_days", "delivery_max_days", "stock"):
            q[k] = None
    return q


def good_product(key, title, role, *, price=None, collections=("cat-water-fountains",), subs=(), tier=None,
                 variants=None, current_supplier=None, shopify_id=None, listing=None):
    p = {
        "product_key": key, "role": role, "title": title, "collected_at": TS,
        "shopify_product_id": shopify_id, "shopify_handle": key.split(":")[-1] if role == "ACTIVE" else None,
        "collection_handles": list(collections), "sub_collection_handles": list(subs),
        "claims": ["Keeps water fresh and filtered for cats"],
        "tier": tier or {"dimensions": {"capacity": "3L"}, "manufacturer": "Aquapaw", "price_tier": "mid"},
        "current_supplier": current_supplier,
        "supplier_quotes": [
            quote("aliexpress.us", 9.10, 0.0, 8, "3256801"),
            quote("cjdropshipping.us", 9.60, 1.50, 6, "CJ-77"),
            quote("dsers", 9.10, 0.0, 9, "3256801-ds"),
            quote("autods", 12.00, 0.0, 7, "ADS-1"),
        ],
        "competitors": [
            {"store": "aquacat.com", "price_usd": 32.99, "source": "winninghunter:adlibrary"},
            {"store": "meowflow.com", "price_usd": 29.99, "source": "winninghunter:adlibrary"},
            {"store": "petsip.co", "price_usd": 34.95, "source": "winninghunter:adlibrary"},
        ],
        "market": {
            "units_sold_30d_sources": [
                {"source": "winninghunter:tiktok-shop", "listing_id": "17290001", "units": 1450},
                {"source": "winninghunter:tiktok-shop", "listing_id": "17290002", "units": 380},
            ],
            "monthly": [
                {"period_start": d(120), "period_end": d(90), "units": 3100, "revenue_usd": 86000, "sources": ["winninghunter:tiktok-shop"]},
                {"period_start": d(90), "period_end": d(60), "units": 3500, "revenue_usd": 97000, "sources": ["winninghunter:tiktok-shop"]},
                {"period_start": d(60), "period_end": d(30), "units": 3900, "revenue_usd": 108000, "sources": ["winninghunter:tiktok-shop"]},
                {"period_start": d(30), "period_end": d(0), "units": 4300, "revenue_usd": 121000, "sources": ["winninghunter:tiktok-shop"]},
            ],
        },
        "rating": {"verified_avg": 4.7, "verified_count": 1840, "source": "aliexpress.us verified purchases",
                   "negative_review_scan": {"sample_size": 60, "top_complaints": ["pump noise after 3 months", "filter cost"],
                                            "fatal_defect_found": False, "fatal_defect_detail": None,
                                            "improvement_opportunity": "Choose wireless-pump version that fixes noise complaints"}},
        "trend_90d": {
            "primary": {"source": "winninghunter:meta-active-ads", "points": [
                {"date": d(90), "value": 40}, {"date": d(60), "value": 52}, {"date": d(30), "value": 61},
                {"date": d(14), "value": 66}, {"date": d(7), "value": 70}, {"date": d(0), "value": 72}]},
            "corroborating": [{"source": "google-trends", "points": [
                {"date": d(90), "value": 70}, {"date": d(60), "value": 74}, {"date": d(30), "value": 78}, {"date": d(0), "value": 80}]}],
        },
        "problem_solver": {
            "audience": "cat owners whose cats drink too little water", "problem": "cats avoid stale bowl water and risk urinary issues",
            "recurrence": "daily", "solution": "circulating filtered water that cats prefer",
            "statement": "For cat owners whose cats drink too little water, this filtered fountain keeps water moving so cats drink more every day.",
            "niche_fit": "multi-pet and small-apartment households", "review_gap_upgrade": "silent wireless pump fixes the #1 complaint",
            "claude_verdict": "PASS", "rationale": "Hydration is a daily, vet-recognised problem; fountains are a proven category with repeat demand."},
        "visual_hook": {
            "hook_3s": "Cat ignores a stale bowl, then runs to the fountain and drinks",
            "demo_format": "before_after", "media_evidence": ["https://example.invalid/ad/123"],
            "ad_policy": {"meta": True, "tiktok": True, "google": True, "notes": "no health claims"},
            "claude_verdict": "PASS", "rationale": "The before/after of a cat refusing a bowl then drinking is instantly readable without sound."},
        "variants": variants or [
            {"variant_key": "white-3l", "options": {"Color": "White", "Capacity": "3L"}, "supplier_order_count": 5200, "review_count": 900, "sku_velocity_30d": 700, "in_stock": True, "current_price_usd": price},
            {"variant_key": "grey-3l", "options": {"Color": "Grey", "Capacity": "3L"}, "supplier_order_count": 2100, "review_count": 300, "sku_velocity_30d": 260, "in_stock": True, "current_price_usd": price},
            {"variant_key": "pink-3l", "options": {"Color": "Pink", "Capacity": "3L"}, "supplier_order_count": 120, "review_count": 10, "sku_velocity_30d": 8, "in_stock": True, "current_price_usd": price},
        ],
        "listing": listing,
        "seo": {"long_tail_keywords": ["quiet cat water fountain for multiple cats"], "proposed_title": title},
    }
    return p


def evidence():
    active_ok = good_product("shopify:gid://shopify/Product/2001", "Cat Water Fountain #1", "ACTIVE", price=31.99,
                             shopify_id=gid("Product", 2001), current_supplier={"platform": "cjdropshipping.us", "listing_id": "CJ-77", "source": "dsers mapping"})
    for i, v in enumerate(active_ok["variants"]):
        v["shopify_variant_id"] = gid("ProductVariant", 50010 + i)
    active_fail = good_product("shopify:gid://shopify/Product/2002", "Cat Water Fountain #2", "ACTIVE", price=24.99, shopify_id=gid("Product", 2002))
    active_fail["rating"]["verified_avg"] = 4.1
    active_fail["trend_90d"]["primary"]["points"] = [{"date": d(90), "value": 90}, {"date": d(60), "value": 70}, {"date": d(30), "value": 40},
                                                     {"date": d(7), "value": 30}, {"date": d(0), "value": 25}]
    for i, v in enumerate(active_fail["variants"]):
        v["shopify_variant_id"] = gid("ProductVariant", 50020 + i)

    listing = {"title": "Silent 3L Wireless Cat Water Fountain", "handle": "silent-3l-wireless-cat-water-fountain",
               "description_html": "<p>Ultra-quiet wireless pump keeps water flowing so cats drink more.</p>",
               "product_type": "Cat Water Fountain", "vendor": "Purrfect Home", "tags": ["fountain", "3l"],
               "image_urls": ["https://example.invalid/img/fountain.jpg"], "seo_title": "Silent 3L Wireless Cat Water Fountain",
               "seo_description": "Quiet filtered water fountain for cats and multi-cat homes."}
    replacement = good_product("candidate:silent-3l-wireless-fountain", "Silent 3L Wireless Cat Water Fountain", "REPLACEMENT_CANDIDATE",
                               subs=(), listing=listing)
    addition = good_product("candidate:stainless-4l-fountain", "Stainless 4L Cat Water Fountain", "GAP_FILL_CANDIDATE",
                            subs=("large-capacity-fountains",),
                            tier={"dimensions": {"capacity": "4L", "material": "stainless"}, "manufacturer": "Petlibro", "price_tier": "premium"},
                            listing={**listing, "title": "Stainless Steel 4L Cat Water Fountain", "handle": "stainless-4l-cat-water-fountain"})
    addition["variants"] = [{"variant_key": "steel-4l", "options": {"Capacity": "4L"}, "supplier_order_count": 3000, "review_count": 400, "sku_velocity_30d": 300, "in_stock": True}]
    addition["supplier_quotes"] = [quote("aliexpress.us", 10.5, 0.0, 9, "AE-4L"), quote("cjdropshipping.us", 10.5, 0.0, 6, "CJ-4L"),
                                   quote("dsers", 11.0, 0.0, 9, "AE-4L-ds")]
    rejected = good_product("candidate:led-fountain-fad", "LED Light-Up Fountain", "GAP_FILL_CANDIDATE")
    rejected["supplier_quotes"] = [quote("aliexpress.us", 9.0, 12.0, 15, "AE-LED"), quote("cjdropshipping.us", 0, 0, 0, "x", status="NOT_FOUND"),
                                   quote("dsers", 9.0, 11.0, 18, "DS-LED")]
    return {"evidence_version": "1.0", "collected_at": TS,
            "tier_requirements": {"cat-water-fountains": {"capacity": ["2.2L", "3L", "4L"]}},
            "products": [active_ok, active_fail, replacement, addition, rejected]}


def proposals():
    return {"proposals_version": "1.0",
            "tier_requirements": {"cat-water-fountains": {"capacity": ["2.2L", "3L", "4L"]}},
            "replacements": [{"failing_product_key": "shopify:gid://shopify/Product/2002",
                              "replacement_product_key": "candidate:silent-3l-wireless-fountain",
                              "collection_handle": "cat-water-fountains", "sub_collection_handles": ["quiet-cat-water-fountains"],
                              "tier_note": "same 3L mid-price slot", "rationale": "Upgraded silent pump fixes review complaints."}],
            "additions": [{"product_key": "candidate:stainless-4l-fountain", "collection_handle": "cat-water-fountains",
                           "sub_collection_handles": ["large-capacity-fountains"], "tier_slot": "capacity 4L / premium / stainless"}],
            "new_sub_collections": [
                {"parent_collection_handle": "cat-water-fountains", "handle": "quiet-cat-water-fountains", "title": "Quiet Cat Water Fountains",
                 "seo_title": "Quiet Cat Water Fountains | Silent Pumps", "meta_description": "Whisper-quiet filtered fountains for bedrooms and apartments.",
                 "description_html": "<p>Silent pumps for light sleepers.</p>", "long_tail_keywords": ["quiet cat water fountain for bedroom"],
                 "avg_margin_multiple": 3.4, "existing_product_keys": ["shopify:gid://shopify/Product/2001"],
                 "rationale": "Noise is the #1 complaint in fountain reviews; a quiet-pump sub-collection captures that intent."}],
            "unfilled_gap_notes": ["cat-water-fountains needs 2 more sub-collections and more 2.2L options next week"]}


def write_examples():
    out = SKILL / "examples"
    out.mkdir(exist_ok=True)
    for name, data in (("catalog.example.json", catalog()), ("evidence.example.json", evidence()), ("proposals.example.json", proposals())):
        (out / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote examples to {out}")


if __name__ == "__main__":
    write_examples()
    sys.exit(0)
