#!/usr/bin/env python3
"""WinningHunter REST client (batch market data) with safe rate limiting.

REST limit is 60 req/min per billing account; this client caps itself at
`rate_limits.winninghunter_rest_per_minute` (default 55) and stops immediately
on credit exhaustion (a 429 carrying a `credits` object), plan gates (403) or
auth failures (401). MCP calls (300/min) are made by Claude directly, not here.

Endpoints (docs: https://app.winninghunter.com/docs):
  GET  /api/v1/credits
  GET  /api/v1/tiktok-shop/products/explore   (period = day count, sold_count, revenue)
  GET  /api/v1/adlibrary                       (Meta ads; Standard plan)
  POST /api/v1/store-explorer                  (store 30d revenue estimates)
  POST /api/v1/trends/search                   (Exploding Topics)

Sub-commands:
  credits
  tiktok-products --name KW [--period 30]
  adlibrary --keyword KW [--last-seen-days 14]
  store-explorer --search KW
  trends --query KW
  market-snapshot --keyword KW [--listing-ids ID,ID] --product-key KEY --out FILE
      Builds the `market`, `competitors` and `trend_90d` evidence blocks for
      one product. Monthly buckets come from differencing the 30/60/90/120-day
      TikTok Shop windows for the SAME listing ids (product-level, all listings).
      Run once without --listing-ids to see candidate listings, then re-run with
      the ids that are truly the same product.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    FatalApiError, RateLimiter, get_logger, http_json, load_env, load_rules, money,
    require_env, store_today, utc_now_iso, write_json,
)

log = get_logger("winninghunter")


class WinningHunter:
    def __init__(self):
        load_env()
        self.key = require_env("WH_API_KEY")["WH_API_KEY"]
        self.origin = os.environ.get("WH_API_ORIGIN", "https://app.winninghunter.com").rstrip("/")
        self.limiter = RateLimiter(load_rules()["rate_limits"]["winninghunter_rest_per_minute"], 60.0)

    @staticmethod
    def _fatal(status, body):
        if status == 429 and isinstance(body, dict) and body.get("credits"):
            raise FatalApiError(f"WinningHunter API credits exhausted: {body.get('error')}")
        if status in (401, 403):
            raise FatalApiError(f"WinningHunter HTTP {status}: {(body or {}).get('error') if isinstance(body, dict) else body}")

    def _req(self, method, path, *, params=None, body=None):
        return http_json(method, f"{self.origin}{path}", headers={"X-API-Key": self.key}, params=params,
                         body=body, limiter=self.limiter, fatal_check=self._fatal)

    def credits(self):
        return self._req("GET", "/api/v1/credits")

    def tiktok_products(self, name, period=30, limit=50, country="US", sort="revenue"):
        return self._req("GET", "/api/v1/tiktok-shop/products/explore",
                         params={"country": country, "period": period, "limit": limit, "page": 1,
                                 "sort": sort, "order": "desc", "name": name})

    def adlibrary(self, keyword, last_seen_days=14, limit=50, countries="US"):
        today = store_today()
        return self._req("GET", "/api/v1/adlibrary", params={
            "keyword": keyword, "countries": countries, "sorting": "lastseen", "sortdirection": "desc",
            "page": 0, "limit": limit, "technology": "SH",
            "fromlastseen": (today - dt.timedelta(days=last_seen_days)).isoformat(),
            "tolastseen": today.isoformat(),
        })

    def store_explorer(self, search, country="US", page_size=20):
        return self._req("POST", "/api/v1/store-explorer", body={
            "search": search, "country": country, "sortingKey": "revenue_30d",
            "sortingDirection": "desc", "page": 1, "pageSize": page_size, "includeWlads": 0})

    def trends(self, query):
        return self._req("POST", "/api/v1/trends/search", body={"query": query})


def _int(v):
    if v is None or v == "":
        return 0
    try:
        return int(float(str(v).replace(",", "")))
    except ValueError:
        return 0


def _host(url):
    try:
        return urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return ""


def build_market_snapshot(wh: WinningHunter, keyword: str, listing_ids: list[str] | None, product_key: str):
    windows = [30, 60, 90, 120]
    rows_by_window = {}
    for w in windows:
        resp = wh.tiktok_products(keyword, period=w)
        rows_by_window[w] = {str(r.get("id")): r for r in resp.get("data") or []}

    candidates = [
        {"listing_id": lid, "title": r.get("product_title"), "unit_price": r.get("unit_price"),
         "sold_30d": _int(r.get("sold_count")), "revenue_30d": money(r.get("revenue")),
         "url": r.get("winninghunter_product_url")}
        for lid, r in rows_by_window[30].items()
    ]
    if not listing_ids:
        return {"product_key": product_key, "keyword": keyword, "needs_listing_selection": True,
                "candidate_listings": candidates,
                "instructions": "Re-run with --listing-ids listing,listing for listings that are the SAME product."}

    cumulative = {w: {"units": 0, "revenue": 0.0} for w in windows}
    units_sources = []
    missing = []
    for lid in listing_ids:
        for w in windows:
            row = rows_by_window[w].get(lid)
            if row is None:
                missing.append(f"{lid}@{w}d")
                continue
            cumulative[w]["units"] += _int(row.get("sold_count"))
            cumulative[w]["revenue"] += money(row.get("revenue")) or 0.0
        r30 = rows_by_window[30].get(lid)
        if r30:
            units_sources.append({"source": "winninghunter:tiktok-shop", "listing_id": lid,
                                  "units": _int(r30.get("sold_count")), "url": r30.get("winninghunter_product_url")})

    today = store_today()
    monthly = []
    prev = {"units": 0, "revenue": 0.0}
    notes = []
    for idx, w in enumerate(windows):
        units = cumulative[w]["units"] - prev["units"]
        revenue = cumulative[w]["revenue"] - prev["revenue"]
        if units < 0 or revenue < 0:
            notes.append(f"Negative bucket for days {w-30}-{w}; clamped to 0 (window data inconsistency).")
        monthly.append({
            "period_start": (today - dt.timedelta(days=w)).isoformat(),
            "period_end": (today - dt.timedelta(days=w - 30)).isoformat(),
            "units": max(units, 0), "revenue_usd": round(max(revenue, 0.0), 2),
            "sources": ["winninghunter:tiktok-shop"],
        })
        prev = cumulative[w]
    monthly.reverse()  # oldest -> newest

    ads = wh.adlibrary(keyword).get("data") or []
    stores = {}
    for a in ads:
        dom = a.get("shopify_shopifydomain") or _host(a.get("urlStore") or "") or a.get("pageName") or "unknown"
        s = stores.setdefault(dom, {"store": dom, "price": money(a.get("shopify_productprice")),
                                    "url": a.get("urlStore") or a.get("product_url"), "ads": 0,
                                    "last_seen": "", "page_id": a.get("page_id"),
                                    "active_ads_curve": {
                                        "d90": a.get("total_active_ads_on_page_growth_3m"),
                                        "d30": a.get("total_active_ads_on_page_growth_1m"),
                                        "d14": a.get("total_active_ads_on_page_growth_14d"),
                                        "d7": a.get("total_active_ads_on_page_growth_1w"),
                                        "d0": a.get("total_active_ads_on_page")}})
        s["ads"] += 1
        s["last_seen"] = max(s["last_seen"], (a.get("lastSeen") or "")[:10])
    competitors = [
        {"store": s["store"], "price_usd": s["price"], "url": s["url"], "active_ads_sampled": s["ads"],
         "last_seen": s["last_seen"] or None, "source": "winninghunter:adlibrary"}
        for s in stores.values()
    ]

    # Aggregate active-ads curve across distinct pages as a 90-day scaling series.
    pages = {}
    for s in stores.values():
        if s["page_id"]:
            pages[s["page_id"]] = s["active_ads_curve"]
    series = []
    for label, days_ago in (("d90", 90), ("d30", 30), ("d14", 14), ("d7", 7), ("d0", 0)):
        vals = [_int(c.get(label)) for c in pages.values() if c.get(label) is not None]
        if vals:
            series.append({"date": (today - dt.timedelta(days=days_ago)).isoformat(), "value": sum(vals)})

    priced = [c["price_usd"] for c in competitors if c["price_usd"]]
    return {
        "product_key": product_key,
        "keyword": keyword,
        "collected_at": utc_now_iso(),
        "listing_ids": listing_ids,
        "missing_window_rows": missing,
        "market": {"units_sold_30d_sources": units_sources, "monthly": monthly, "notes": notes},
        "competitors": competitors,
        "competitor_price_median_usd": round(statistics.median(priced), 2) if priced else None,
        "trend_90d_series": {"source": "winninghunter:meta-active-ads", "points": series},
        "review_required": "Drop competitors that are not the same kind of product before merging into evidence.",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("credits")
    p = sub.add_parser("tiktok-products"); p.add_argument("--name", required=True); p.add_argument("--period", type=int, default=30)
    p = sub.add_parser("adlibrary"); p.add_argument("--keyword", required=True); p.add_argument("--last-seen-days", type=int, default=14)
    p = sub.add_parser("store-explorer"); p.add_argument("--search", required=True)
    p = sub.add_parser("trends"); p.add_argument("--query", required=True)
    p = sub.add_parser("market-snapshot")
    p.add_argument("--keyword", required=True)
    p.add_argument("--listing-ids", default="")
    p.add_argument("--product-key", required=True)
    p.add_argument("--out", required=True)
    args = ap.parse_args()

    wh = WinningHunter()
    try:
        if args.cmd == "credits":
            out = wh.credits()
        elif args.cmd == "tiktok-products":
            out = wh.tiktok_products(args.name, period=args.period)
        elif args.cmd == "adlibrary":
            out = wh.adlibrary(args.keyword, last_seen_days=args.last_seen_days)
        elif args.cmd == "store-explorer":
            out = wh.store_explorer(args.search)
        elif args.cmd == "trends":
            out = wh.trends(args.query)
        else:
            ids = [i.strip() for i in args.listing_ids.split(",") if i.strip()]
            out = build_market_snapshot(wh, args.keyword, ids, args.product_key)
            write_json(args.out, out)
            log.info("Wrote %s", args.out)
            return 0
    except FatalApiError as exc:
        log.error("%s", exc)
        return 3
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
