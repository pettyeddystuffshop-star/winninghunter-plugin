#!/usr/bin/env python3
"""Cross-platform landed-cost, shipping-fee and delivery-SLA comparator.

Platforms compared on every product (Criterion 2):
  aliexpress.us      1st choice   (AliExpress DS API, or quotes collected via DSers/AliExpress MCP)
  cjdropshipping.us  2nd choice   (CJ API 2.0, US warehouses)
  dsers              price check  (AliExpress suppliers mapped in DSers; collected via DSers MCP)
  autods             LAST RESORT  (only when none of the three above has a quote that is in stock,
                                   ships for < $10, arrives in <= 10 days and fits the 3x margin)

Selection: lowest landed cost (unit + US shipping) among qualifying quotes on
the three price-check platforms. Ties (within suppliers.tie_tolerance_usd) go to
aliexpress.us, then cjdropshipping.us, then dsers; then free shipping; then
fastest delivery. AutoDS is considered only if no price-check quote qualifies.

Quote record (one per platform x listing x variant; see schemas/product_evidence.schema.json):
  {"platform": "aliexpress.us", "status": "QUOTED" | "NOT_FOUND" | "OUT_OF_STOCK" | "ERROR",
   "listing_id": "...", "listing_url": "...", "supplier_name": "...", "variant_key": null | "white-3l",
   "unit_price_usd": 8.12, "shipping_cost_usd": 0.0, "shipping_method": "...",
   "delivery_min_days": 4, "delivery_max_days": 8, "ship_from": "US", "stock": 532,
   "checked_at": "2026-11-27T09:00:00Z", "source": "script:cj-api"}

Sub-commands:
  compare --quotes FILE [--variant-key K] [--max-viable-landed X]   -> JSON comparison on stdout
  fetch-cj --pid PID | --keyword KW [--out FILE]                     -> CJ quotes (US warehouse -> US)
  fetch-aliexpress --product-id ID [--out FILE]                      -> AliExpress DS API quotes (ship to US)
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    RateLimiter, get_logger, http_json, load_env, load_rules, money, project_root,
    read_json, require_env, utc_now_iso, write_json,
)

log = get_logger("supplier-check")

PLATFORMS = ("aliexpress.us", "cjdropshipping.us", "dsers", "autods")


# ------------------------------------------------------------------ comparison

def _evaluate_quote(q: dict[str, Any], c: dict[str, Any], max_viable_landed: float | None) -> dict[str, Any]:
    unit = money(q.get("unit_price_usd"))
    ship = money(q.get("shipping_cost_usd"))
    max_days = q.get("delivery_max_days")
    stock = q.get("stock")
    reasons = []
    if unit is None:
        reasons.append("missing unit price")
    if ship is None:
        reasons.append("missing shipping cost")
    if max_days is None:
        reasons.append("missing delivery window")
    landed = round(unit + ship, 2) if unit is not None and ship is not None else None
    if stock is not None and int(stock) <= 0:
        reasons.append("out of stock")
    if stock is None and q.get("in_stock") is not True:
        reasons.append("stock not verified")
    if ship is not None and not ship < c["c03_shipping_cost_max_exclusive_usd"]:
        reasons.append(f"shipping ${ship:.2f} is not < ${c['c03_shipping_cost_max_exclusive_usd']:.2f}")
    if max_days is not None and int(max_days) > c["c04_delivery_max_days"]:
        reasons.append(f"delivery max {max_days}d > {c['c04_delivery_max_days']}d")
    if max_viable_landed is not None and landed is not None and landed > max_viable_landed + 1e-9:
        reasons.append(f"landed ${landed:.2f} exceeds max viable ${max_viable_landed:.2f} for 3x at a competitive price")
    out = dict(q)
    out.update({"unit_price_usd": unit, "shipping_cost_usd": ship, "landed_cost_usd": landed,
                "free_shipping": ship == 0.0, "qualifies": not reasons, "reject_reasons": reasons})
    return out


def _sort_key(q, priority):
    plat = q["platform"]
    prio = priority.index(plat) if plat in priority else len(priority)
    return (q["landed_cost_usd"], prio, 0 if q["free_shipping"] else 1, q.get("delivery_max_days") or 99)


def compare_quotes(quotes: list[dict[str, Any]], rules: dict[str, Any] | None = None,
                   variant_key: str | None = None, max_viable_landed: float | None = None) -> dict[str, Any]:
    rules = rules or load_rules()
    c = rules["criteria"]
    sup = rules["suppliers"]
    tol = float(sup["tie_tolerance_usd"])
    relevant = [q for q in quotes if q.get("variant_key") in (None, variant_key)]

    platforms: dict[str, Any] = {}
    for plat in PLATFORMS:
        recs = [q for q in relevant if q.get("platform") == plat]
        evaluated = [_evaluate_quote(q, c, max_viable_landed) for q in recs if q.get("status") == "QUOTED"]
        qualifying = [q for q in evaluated if q["qualifies"]]
        if evaluated:
            status = "QUOTED"
        elif recs:
            status = recs[0].get("status", "ERROR")
        else:
            status = "NOT_CHECKED"
        best = sorted(qualifying, key=lambda q: _sort_key(q, sup["priority"]))[0] if qualifying else None
        cheapest_any = min((q["landed_cost_usd"] for q in evaluated if q["landed_cost_usd"] is not None), default=None)
        platforms[plat] = {"status": status, "quotes_evaluated": evaluated, "best_qualifying": best,
                           "cheapest_landed_any_usd": cheapest_any}

    missing = [p for p in sup["price_check_platforms"] if platforms[p]["status"] in ("NOT_CHECKED", "ERROR")]
    complete = not missing
    pool = [platforms[p]["best_qualifying"] for p in sup["price_check_platforms"] if platforms[p]["best_qualifying"]]

    selected = None
    justification = []
    if pool:
        pool.sort(key=lambda q: _sort_key(q, sup["priority"]))
        floor = pool[0]["landed_cost_usd"]
        tied = [q for q in pool if q["landed_cost_usd"] - floor <= tol + 1e-9]
        tied.sort(key=lambda q: (sup["priority"].index(q["platform"]), 0 if q["free_shipping"] else 1,
                                 q.get("delivery_max_days") or 99))
        selected = tied[0]
        justification.append(f"Lowest qualifying landed cost ${selected['landed_cost_usd']:.2f} on {selected['platform']}")
        if len(tied) > 1:
            justification.append("Tie broken by priority aliexpress.us > cjdropshipping.us > dsers: "
                                 + ", ".join(f"{q['platform']} ${q['landed_cost_usd']:.2f}" for q in tied))
        for p in sup["price_check_platforms"]:
            if p != selected["platform"]:
                b = platforms[p]["best_qualifying"]
                justification.append(f"{p}: " + (f"${b['landed_cost_usd']:.2f}" if b else platforms[p]["status"].lower()
                                                   + ("" if platforms[p]["status"] != "QUOTED" else " (no qualifying quote)")))
    else:
        auto = platforms["autods"]["best_qualifying"]
        if auto:
            selected = auto
            justification.append("LAST RESORT: no aliexpress.us / cjdropshipping.us / dsers quote met stock, <$10 shipping, "
                                 "<=10 day delivery and 3x margin rules")
            for p in sup["price_check_platforms"]:
                reasons = sorted({r for q in platforms[p]["quotes_evaluated"] for r in q["reject_reasons"]})
                justification.append(f"{p}: {platforms[p]['status'].lower()}" + (f" ({'; '.join(reasons)})" if reasons else ""))
        else:
            justification.append("No qualifying quote on any platform (including AutoDS fallback).")

    if selected:
        selected = dict(selected)
        selected["supplier_tier"] = sup["tier_labels"][selected["platform"]]
    all_qualifying = [q for p in sup["price_check_platforms"] for q in platforms[p]["quotes_evaluated"] if q["qualifies"]]
    is_lowest = bool(selected) and all(selected["landed_cost_usd"] <= q["landed_cost_usd"] + 1e-9 for q in all_qualifying)

    return {
        "variant_key": variant_key,
        "platforms": platforms,
        "comparison_complete": complete,
        "missing_platforms": missing,
        "selected": selected,
        "is_lowest_cross_platform": is_lowest,
        "used_last_resort": bool(selected) and selected["platform"] == sup["last_resort"],
        "justification": justification,
    }


# ------------------------------------------------------------------ CJ API 2.0

class CJClient:
    BASE = "https://developers.cjdropshipping.com/api2.0/v1"

    def __init__(self):
        load_env()
        self.api_key = require_env("CJ_API_KEY")["CJ_API_KEY"]
        self.limiter = RateLimiter(load_rules()["rate_limits"]["cj_per_second"], 1.0)
        self.cache = project_root() / ".wpc" / "cj_token.json"

    def token(self) -> str:
        if self.cache.exists():
            data = read_json(self.cache)
            if data.get("expires_at", 0) > time.time() + 3600:
                return data["accessToken"]
        body = {"apiKey": self.api_key}
        if os.environ.get("CJ_EMAIL"):
            body["email"] = os.environ["CJ_EMAIL"]
        resp = http_json("POST", f"{self.BASE}/authentication/getAccessToken", body=body, limiter=self.limiter)
        if not resp.get("result"):
            raise SystemExit(f"CJ auth failed: {resp.get('message')}")
        tok = resp["data"]["accessToken"]
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.cache, {"accessToken": tok, "expires_at": time.time() + 14 * 86400})
        os.chmod(self.cache, 0o600)
        return tok

    def _get(self, path, params=None):
        resp = http_json("GET", f"{self.BASE}{path}", headers={"CJ-Access-Token": self.token()}, params=params, limiter=self.limiter)
        if not resp.get("result"):
            raise RuntimeError(f"CJ {path} failed: {resp.get('message')}")
        return resp.get("data")

    def _post(self, path, body):
        resp = http_json("POST", f"{self.BASE}{path}", headers={"CJ-Access-Token": self.token()}, body=body, limiter=self.limiter)
        if not resp.get("result"):
            raise RuntimeError(f"CJ {path} failed: {resp.get('message')}")
        return resp.get("data")

    def search(self, keyword, page_size=20):
        data = self._get("/product/list", {"productNameEn": keyword, "pageNum": 1, "pageSize": page_size})
        return (data or {}).get("list") or []

    def product(self, pid):
        return self._get("/product/query", {"pid": pid})

    def us_stock(self, vid) -> int | None:
        rows = self._get("/product/stock/queryByVid", {"vid": vid}) or []
        total = None
        for r in rows:
            if (r.get("countryCode") or "").upper() != "US":
                continue
            for k in ("totalInventoryNum", "storageNum", "cjInventoryNum", "totalInventory"):
                if r.get(k) is not None:
                    total = (total or 0) + int(float(r[k]))
                    break
        return total

    def freight_us(self, vid):
        return self._post("/logistic/freightCalculate", {
            "startCountryCode": "US", "endCountryCode": "US", "products": [{"quantity": 1, "vid": vid}]}) or []

    def quotes_for_product(self, pid) -> list[dict[str, Any]]:
        prod = self.product(pid)
        out = []
        for v in prod.get("variants") or []:
            vid = v.get("vid")
            stock = self.us_stock(vid)
            base = {"platform": "cjdropshipping.us", "listing_id": pid, "supplier_name": "CJdropshipping",
                    "listing_url": f"https://cjdropshipping.com/product/-p-{pid}.html",
                    "variant_key": None, "supplier_variant_id": vid,
                    "supplier_variant_name": v.get("variantNameEn") or v.get("variantKey"),
                    "unit_price_usd": money(v.get("variantSellPrice")), "ship_from": "US",
                    "stock": stock, "checked_at": utc_now_iso(), "source": "script:cj-api"}
            if stock == 0 or stock is None:
                out.append({**base, "status": "OUT_OF_STOCK" if stock == 0 else "NOT_FOUND",
                            "note": "no US-warehouse inventory reported"})
                continue
            options = self.freight_us(vid)
            if not options:
                out.append({**base, "status": "NOT_FOUND", "note": "no US->US logistics option"})
            for o in options:
                aging = str(o.get("logisticAging") or "")
                parts = [int(x) for x in aging.replace(" ", "").split("-") if x.isdigit()]
                out.append({**base, "status": "QUOTED", "shipping_cost_usd": money(o.get("logisticPrice")),
                            "shipping_method": o.get("logisticName"),
                            "delivery_min_days": min(parts) if parts else None,
                            "delivery_max_days": max(parts) if parts else None})
        return out


# --------------------------------------------------------- AliExpress DS (IOP) API

class AliExpressDS:
    """AliExpress Open Platform dropshipping API (api-sg.aliexpress.com/sync, HMAC-SHA256)."""

    URL = "https://api-sg.aliexpress.com/sync"

    def __init__(self):
        load_env()
        env = require_env("ALIEXPRESS_APP_KEY", "ALIEXPRESS_APP_SECRET", "ALIEXPRESS_ACCESS_TOKEN")
        self.app_key, self.secret, self.session = env["ALIEXPRESS_APP_KEY"], env["ALIEXPRESS_APP_SECRET"], env["ALIEXPRESS_ACCESS_TOKEN"]
        self.limiter = RateLimiter(load_rules()["rate_limits"]["aliexpress_per_second"], 1.0)

    def _sign(self, params: dict[str, str]) -> str:
        base = "".join(f"{k}{params[k]}" for k in sorted(params))
        return hmac.new(self.secret.encode(), base.encode(), hashlib.sha256).hexdigest().upper()

    def call(self, method: str, **api_params) -> dict[str, Any]:
        params = {"app_key": self.app_key, "method": method, "session": self.session,
                  "timestamp": str(int(time.time() * 1000)), "sign_method": "sha256"}
        params.update({k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in api_params.items()})
        params["sign"] = self._sign(params)
        resp = http_json("POST", self.URL, params=params, limiter=self.limiter)
        if "error_response" in resp:
            raise RuntimeError(f"AliExpress {method}: {resp['error_response']}")
        return resp

    def quotes_for_product(self, product_id: str) -> list[dict[str, Any]]:
        prod = self.call("aliexpress.ds.product.get", product_id=product_id, ship_to_country="US",
                         target_currency="USD", target_language="en")
        result = (prod.get("aliexpress_ds_product_get_response") or {}).get("result") or {}
        skus = ((result.get("ae_item_sku_info_dtos") or {}).get("ae_item_sku_info_d_t_o")) or []
        title = (result.get("ae_item_base_info_dto") or {}).get("subject")
        out = []
        for sku in skus:
            props = ((sku.get("ae_sku_property_dtos") or {}).get("ae_sku_property_d_t_o")) or []
            ships_from = next((p.get("property_value_definition_name") or p.get("sku_property_value")
                               for p in props if (p.get("sku_property_name") or "").lower().startswith("ships from")), None)
            name = " / ".join(str(p.get("property_value_definition_name") or p.get("sku_property_value"))
                              for p in props if not (p.get("sku_property_name") or "").lower().startswith("ships from"))
            stock = sku.get("sku_available_stock")
            base = {"platform": "aliexpress.us", "listing_id": str(product_id), "supplier_name": title,
                    "listing_url": f"https://www.aliexpress.us/item/{product_id}.html", "variant_key": None,
                    "supplier_variant_id": str(sku.get("sku_id") or sku.get("id")), "supplier_variant_name": name,
                    "unit_price_usd": money(sku.get("offer_sale_price") or sku.get("sku_price")),
                    "stock": int(stock) if stock is not None else None, "checked_at": utc_now_iso(),
                    "source": "script:aliexpress-ds-api"}
            req = {"quantity": 1, "shipToCountry": "US", "productId": str(product_id),
                   "selectedSkuId": str(sku.get("sku_id") or sku.get("id")), "language": "en_US",
                   "currency": "USD", "locale": "en_US"}
            fr = self.call("aliexpress.ds.freight.query", queryDeliveryReq=req)
            fres = (fr.get("aliexpress_ds_freight_query_response") or {}).get("result") or {}
            opts = ((fres.get("delivery_options") or {}).get("delivery_option_d_t_o")) or []
            if not opts:
                out.append({**base, "status": "NOT_FOUND", "note": fres.get("msg") or "no delivery option to US"})
            for o in opts:
                if o.get("free_shipping") in (True, "true"):
                    fee = 0.0
                elif o.get("shipping_fee_format"):
                    fee = money(o["shipping_fee_format"])
                else:
                    fee = money(o.get("shipping_fee_cent"))
                out.append({**base, "status": "QUOTED", "shipping_cost_usd": fee,
                            "shipping_method": o.get("company") or o.get("code"),
                            "delivery_min_days": int(o["min_delivery_days"]) if o.get("min_delivery_days") is not None else None,
                            "delivery_max_days": int(o["max_delivery_days"]) if o.get("max_delivery_days") is not None else None,
                            "ship_from": o.get("ship_from_country") or ships_from,
                            "note": "verify fee against DSers quote (fee field formats vary by API version)"})
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("compare")
    p.add_argument("--quotes", required=True, help="JSON file: list of quote records")
    p.add_argument("--variant-key", default=None)
    p.add_argument("--max-viable-landed", type=float, default=None)
    p = sub.add_parser("fetch-cj")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pid"); g.add_argument("--keyword")
    p.add_argument("--out")
    p = sub.add_parser("fetch-aliexpress")
    p.add_argument("--product-id", required=True)
    p.add_argument("--out")
    args = ap.parse_args()

    if args.cmd == "compare":
        quotes = read_json(args.quotes)
        if isinstance(quotes, dict):
            quotes = quotes.get("supplier_quotes") or quotes.get("quotes") or []
        out = compare_quotes(quotes, variant_key=args.variant_key, max_viable_landed=args.max_viable_landed)
    elif args.cmd == "fetch-cj":
        cj = CJClient()
        if args.keyword:
            out = [{"pid": r.get("pid"), "name": r.get("productNameEn"), "sell_price": r.get("sellPrice"),
                    "image": r.get("productImage")} for r in cj.search(args.keyword)]
        else:
            out = cj.quotes_for_product(args.pid)
    else:
        out = AliExpressDS().quotes_for_product(args.product_id)

    if getattr(args, "out", None):
        write_json(args.out, out)
        log.info("Wrote %s", args.out)
    else:
        json.dump(out, sys.stdout, indent=2)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
