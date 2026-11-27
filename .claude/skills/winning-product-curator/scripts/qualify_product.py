#!/usr/bin/env python3
"""Deterministic 10-point Winning Product Qualification Gate + popular-variant filter.

Input : evidence JSON (schemas/product_evidence.schema.json) gathered by Claude from
        Shopify, WinningHunter (MCP + REST), DSers, CJ, AliExpress and AutoDS.
Output: scorecards JSON, one scorecard per product, every criterion PASS/FAIL
        with the raw evidence that decided it.

Principles
  * Missing or unverifiable data is a FAIL, never a PASS.
  * Evidence older than plan.evidence_max_age_days fails the data criteria (1-8).
  * Criteria 9 and 10 need Claude's written judgement, but the validator still
    enforces structure: a concrete "For <audience> who <problem>, this <solution>"
    statement, an allowed recurrence, a demo format, media evidence, ad-policy
    sign-off, and keyword screens for excluded / policy-risk products.

Statuses
  ACTIVE products     : PASS_10_OF_10 | NEEDS_OPTIMIZATION | FAIL_NEEDS_REPLACEMENT
  Candidate products  : QUALIFIED | REJECTED

Usage:
  python3 qualify_product.py --evidence work/2026-11-27/evidence.json --out work/2026-11-27/scorecards.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    charm_price, get_logger, load_rules, money, parse_date, parse_ts, read_json, utc_now_iso,
    validate_against_schema, write_json,
)
from supplier_cross_check import compare_quotes  # noqa: E402

log = get_logger("qualify")

CRITERIA = [
    (1, "c01_margin_3x", "3x landed-cost margin at a market-competitive price"),
    (2, "c02_lowest_cross_platform_cost", "Lowest landed cost across aliexpress.us / cjdropshipping.us / DSers"),
    (3, "c03_shipping_cost_under_10", "US shipping < $10.00 (free preferred)"),
    (4, "c04_delivery_within_10_days", "US delivery <= 10 days"),
    (5, "c05_units_30d_1000", "1,000+ units sold market-wide in 30 days"),
    (6, "c06_revenue_trajectory_or_100k", "Rising 3+ months OR steady $100k+/month"),
    (7, "c07_verified_rating_4_5", "Verified-buyer rating >= 4.5 with no fatal defect"),
    (8, "c08_trend_90d_healthy", "90-day trend rising or high plateau"),
    (9, "c09_everyday_problem_solver", "Solves a real recurring everyday problem"),
    (10, "c10_visual_pain_point_demo", "Instantly demonstrable pain-point visual, ad-policy compliant"),
]


def res(status: bool, summary: str, **evidence) -> dict[str, Any]:
    return {"status": "PASS" if status else "FAIL", "summary": summary, "evidence": evidence}


# --------------------------------------------------------------- variant filter

def rank_variants(variants: list[dict[str, Any]], rules: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Return (kept, pruned) using order share, review share and SKU velocity.

    Raw metrics are normalised within the product. A metric missing for every
    variant is dropped and its weight redistributed. Variants are kept in
    descending popularity until the cumulative share reaches the target, capped
    at max_variants and never keeping a variant below min_individual_share.
    """
    vr = rules["variants"]
    if not variants:
        return [], []
    metrics = {"order_share": "supplier_order_count", "review_share": "review_count", "sku_velocity_share": "sku_velocity_30d"}
    weights = {}
    totals = {}
    for share_key, raw_key in metrics.items():
        vals = [v.get(raw_key) for v in variants]
        if all(x is None for x in vals):
            continue
        total = sum(float(x or 0) for x in vals)
        if total <= 0:
            continue
        weights[share_key] = vr["weights"][share_key]
        totals[share_key] = total
    wsum = sum(weights.values())
    scored = []
    for v in variants:
        s = dict(v)
        if wsum:
            s["popularity_share"] = round(sum(
                weights[k] / wsum * float(v.get(metrics[k]) or 0) / totals[k] for k in weights), 4)
        else:
            s["popularity_share"] = None
        scored.append(s)
    if not wsum:
        for s in scored:
            s["prune_reason"] = "no popularity data (supplier orders, reviews or SKU velocity) to justify keeping"
        return [], scored
    scored.sort(key=lambda s: s["popularity_share"], reverse=True)
    kept, pruned, cum = [], [], 0.0
    for s in scored:
        if s.get("in_stock") is False:
            s["prune_reason"] = "out of stock at supplier"
            pruned.append(s)
        elif len(kept) >= vr["max_variants"]:
            s["prune_reason"] = f"beyond max {vr['max_variants']} variants"
            pruned.append(s)
        elif kept and cum >= vr["cumulative_share_target"]:
            s["prune_reason"] = f"popularity target {vr['cumulative_share_target']:.0%} already covered"
            pruned.append(s)
        elif kept and s["popularity_share"] < vr["min_individual_share"]:
            s["prune_reason"] = f"share {s['popularity_share']:.1%} below {vr['min_individual_share']:.0%} minimum"
            pruned.append(s)
        else:
            kept.append(s)
            cum += s["popularity_share"]
    return kept, pruned


# ------------------------------------------------------------------ trend math

def classify_trend(points: list[dict[str, Any]], c: dict[str, Any], as_of: dt.date) -> dict[str, Any]:
    pts = sorted(((parse_date(p["date"]), float(p["value"])) for p in points if p.get("value") is not None), key=lambda x: x[0])
    window_start = as_of - dt.timedelta(days=c["c08_window_days"])
    pts = [p for p in pts if p[0] >= window_start - dt.timedelta(days=3)]
    if len(pts) < c["c08_min_series_points"]:
        return {"classification": "INSUFFICIENT_DATA", "points": len(pts)}
    span = (pts[-1][0] - pts[0][0]).days
    if span < c["c08_min_span_days"]:
        return {"classification": "INSUFFICIENT_DATA", "points": len(pts), "span_days": span}
    xs = [(d - pts[0][0]).days for d, _ in pts]
    ys = [v for _, v in pts]
    mean_y = statistics.fmean(ys)
    if mean_y <= 0:
        return {"classification": "NO_ACTIVITY", "points": len(pts)}
    mean_x = statistics.fmean(xs)
    denom = sum((x - mean_x) ** 2 for x in xs) or 1.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    rel_change = slope * span / mean_y
    peak = max(ys)
    tail_n = max(1, len(ys) // 4)
    tail_mean = statistics.fmean(ys[-tail_n:])
    ratio = tail_mean / peak if peak else 0.0
    if rel_change >= c["c08_rising_min_relative_change"] and ratio >= c["c08_plateau_min_ratio_of_peak"]:
        cls = "RISING"
    elif ratio >= c["c08_plateau_min_ratio_of_peak"] and rel_change >= -c["c08_plateau_max_relative_drop"]:
        cls = "PLATEAU_HIGH"
    elif ys.index(peak) < len(ys) - tail_n and ratio < c["c08_plateau_min_ratio_of_peak"]:
        cls = "POST_PEAK_DECLINE"
    else:
        cls = "DECLINING"
    return {"classification": cls, "points": len(pts), "span_days": span, "relative_change": round(rel_change, 3),
            "tail_to_peak_ratio": round(ratio, 3), "peak": peak, "latest": ys[-1]}


# ------------------------------------------------------------------- criteria

def keyword_hits(text: str, words: list[str]) -> list[str]:
    low = text.lower()
    return [w for w in words if re.search(r"\b" + re.escape(w.lower()) + r"\b", low)]


def evaluate(product: dict[str, Any], rules: dict[str, Any], as_of: dt.date, now: dt.datetime) -> dict[str, Any]:
    c = rules["criteria"]
    crit: dict[str, dict[str, Any]] = {}
    role = product["role"]

    collected = product.get("collected_at")
    age_days = (now - parse_ts(collected)).days if collected else None
    fresh = age_days is not None and age_days <= rules["plan"]["evidence_max_age_days"]
    stale_note = None if fresh else f"evidence age {age_days if age_days is not None else 'unknown'}d exceeds {rules['plan']['evidence_max_age_days']}d"

    # --- variants -----------------------------------------------------------
    kept, pruned = rank_variants(product.get("variants") or [], rules)
    reference_key = kept[0]["variant_key"] if kept else None

    # --- competitor benchmark ----------------------------------------------
    comp_prices = sorted(p for p in (money(x.get("price_usd")) for x in product.get("competitors") or []) if p)
    comp_median = round(statistics.median(comp_prices), 2) if comp_prices else None
    enough_comp = len(comp_prices) >= c["c01_min_competitor_benchmarks"]
    max_retail = round(comp_median * (1 + c["c01_max_premium_over_competitor_median"]), 2) if comp_median else None
    max_viable_landed = round(max_retail / c["c01_margin_multiple_min"], 2) if max_retail else None

    # --- supplier comparison -------------------------------------------------
    quotes = product.get("supplier_quotes") or []
    comparison = compare_quotes(quotes, rules, variant_key=reference_key, max_viable_landed=max_viable_landed)
    selected = comparison["selected"]

    # per-variant landed cost & out-of-spec pruning on the selected listing
    variant_rows = []
    if selected:
        for v in list(kept):
            vq = [q for q in quotes if q.get("variant_key") == v["variant_key"] and q.get("platform") == selected["platform"]
                  and q.get("listing_id") == selected.get("listing_id") and q.get("status") == "QUOTED"]
            if vq:
                vcmp = compare_quotes(vq, rules, variant_key=v["variant_key"], max_viable_landed=max_viable_landed)
                best = vcmp["platforms"][selected["platform"]]["best_qualifying"]
                if not best:
                    reasons = sorted({r for q in vcmp["platforms"][selected["platform"]]["quotes_evaluated"] for r in q["reject_reasons"]})
                    v["prune_reason"] = "out-of-spec at selected supplier: " + "; ".join(reasons)
                    kept.remove(v)
                    pruned.append(v)
                    continue
                landed = best["landed_cost_usd"]
            else:
                landed = selected["landed_cost_usd"]
            variant_rows.append((v, landed))
        reference_key = kept[0]["variant_key"] if kept else None

    # --- C1 margin ------------------------------------------------------------
    pricing_rows = []
    reprice_needed = False
    c1_ok = bool(selected) and enough_comp and bool(variant_rows) and fresh
    for v, landed in variant_rows:
        required = charm_price(c["c01_margin_multiple_min"] * landed)
        current = money(v.get("current_price_usd"))
        target = current if current is not None and current >= c["c01_margin_multiple_min"] * landed else required
        if role == "ACTIVE" and (current is None or target != current):
            reprice_needed = True
        competitive = max_retail is not None and target <= max_retail
        multiple = round(target / landed, 2) if landed else None
        ok = competitive and multiple is not None and multiple >= c["c01_margin_multiple_min"]
        c1_ok = c1_ok and ok
        pricing_rows.append({"variant_key": v["variant_key"], "landed_cost_usd": landed, "current_price_usd": current,
                             "required_min_retail_usd": required, "target_retail_usd": target,
                             "margin_multiple": multiple, "competitive": competitive})
    reasons = []
    if not selected:
        reasons.append("no qualifying supplier quote")
    if not enough_comp:
        reasons.append(f"only {len(comp_prices)} competitor price benchmarks (< {c['c01_min_competitor_benchmarks']})")
    if not variant_rows:
        reasons.append("no retained variants")
    if stale_note:
        reasons.append(stale_note)
    reasons += [f"{r['variant_key']}: target ${r['target_retail_usd']:.2f} > competitive cap ${max_retail:.2f}"
                for r in pricing_rows if max_retail and not r["competitive"]]
    crit["c01_margin_3x"] = res(c1_ok, "OK" if c1_ok else "; ".join(reasons),
                                formula="Target_Retail_Price >= 3.0 * (Supplier_Unit_Price + US_Shipping_Price)",
                                competitor_prices_usd=comp_prices, competitor_median_usd=comp_median,
                                competitive_cap_usd=max_retail, variants=pricing_rows)

    # --- C2 lowest cross-platform -------------------------------------------
    c2_ok = bool(selected) and comparison["comparison_complete"] and comparison["is_lowest_cross_platform"] and fresh
    why = []
    if not comparison["comparison_complete"]:
        why.append("comparison incomplete; not checked: " + ", ".join(comparison["missing_platforms"]))
    if not selected:
        why.append("no qualifying quote")
    if stale_note:
        why.append(stale_note)
    crit["c02_lowest_cross_platform_cost"] = res(
        c2_ok, "; ".join(comparison["justification"]) if c2_ok else "; ".join(why + comparison["justification"]),
        platform_summary={p: {"status": d["status"],
                              "best_qualifying_landed_usd": (d["best_qualifying"] or {}).get("landed_cost_usd"),
                              "cheapest_landed_any_usd": d["cheapest_landed_any_usd"]}
                          for p, d in comparison["platforms"].items()},
        selected_platform=(selected or {}).get("platform"), used_last_resort=comparison["used_last_resort"])

    # --- C3 / C4 shipping ------------------------------------------------------
    all_eval = [q for d in comparison["platforms"].values() for q in d["quotes_evaluated"]]
    cheapest_ship = min((q["shipping_cost_usd"] for q in all_eval if q["shipping_cost_usd"] is not None), default=None)
    fastest = min((q["delivery_max_days"] for q in all_eval if q.get("delivery_max_days") is not None), default=None)
    if selected:
        ship = selected["shipping_cost_usd"]
        c3 = ship < c["c03_shipping_cost_max_exclusive_usd"] and fresh
        crit["c03_shipping_cost_under_10"] = res(c3, ("FREE shipping" if ship == 0 else f"${ship:.2f} shipping (free not available)") + (f"; {stale_note}" if stale_note else ""),
                                                 shipping_cost_usd=ship, free_shipping=ship == 0, method=selected.get("shipping_method"))
        dmax = selected["delivery_max_days"]
        c4 = dmax <= c["c04_delivery_max_days"] and fresh
        crit["c04_delivery_within_10_days"] = res(c4, f"{selected.get('delivery_min_days')}-{dmax} days from {selected.get('ship_from')}" + (f"; {stale_note}" if stale_note else ""),
                                                  delivery_min_days=selected.get("delivery_min_days"), delivery_max_days=dmax,
                                                  ship_from=selected.get("ship_from"))
    else:
        crit["c03_shipping_cost_under_10"] = res(False, "no qualifying quote; cheapest shipping seen "
                                                 + (f"${cheapest_ship:.2f}" if cheapest_ship is not None else "n/a"),
                                                 cheapest_shipping_seen_usd=cheapest_ship)
        crit["c04_delivery_within_10_days"] = res(False, "no qualifying quote; fastest delivery seen "
                                                  + (f"{fastest}d" if fastest is not None else "n/a"),
                                                  fastest_delivery_seen_days=fastest)

    # --- C5 units 30d ----------------------------------------------------------
    market = product.get("market") or {}
    seen, units = set(), 0
    for s in market.get("units_sold_30d_sources") or []:
        k = (s.get("source"), str(s.get("listing_id")))
        if k in seen:
            continue
        seen.add(k)
        units += int(s.get("units") or 0)
    c5 = units >= c["c05_units_sold_30d_min"] and fresh
    crit["c05_units_30d_1000"] = res(c5, f"{units:,} units across {len(seen)} listings" + (f"; {stale_note}" if stale_note else ""),
                                     units_30d=units, listings_counted=len(seen), sources=market.get("units_sold_30d_sources") or [])

    # --- C6 trajectory or $100k/mo ----------------------------------------------
    monthly = market.get("monthly") or []
    n_rise = c["c06_rising_min_consecutive_months"]
    rising = False
    consecutive = 0
    for prev, cur in zip(monthly, monthly[1:]):
        if cur.get("units", 0) > prev.get("units", 0) and cur.get("revenue_usd", 0) > prev.get("revenue_usd", 0):
            consecutive += 1
        else:
            consecutive = 0
    rising = consecutive >= n_rise
    last = monthly[-c["c06_high_volume_min_months"]:] if len(monthly) >= c["c06_high_volume_min_months"] else []
    revs = [m.get("revenue_usd", 0) for m in last]
    high_volume = bool(revs) and min(revs) >= c["c06_high_volume_monthly_revenue_min_usd"] and \
        (min(revs) / max(revs) if max(revs) else 0) >= c["c06_high_volume_stability_min_ratio"]
    c6 = (rising or high_volume) and fresh
    path = "RISING_TRAJECTORY" if rising else ("HIGH_VOLUME_STABILITY" if high_volume else None)
    crit["c06_revenue_trajectory_or_100k"] = res(
        c6, (f"{path}: " if path else "neither path met: ") + f"{consecutive} consecutive rising months; last {len(revs)} months revenue "
        + ", ".join(f"${r:,.0f}" for r in revs) + (f"; {stale_note}" if stale_note else ""),
        path=path, consecutive_rising_months=consecutive, monthly=monthly)

    # --- C7 rating ----------------------------------------------------------------
    rating = product.get("rating") or {}
    scan = rating.get("negative_review_scan") or {}
    avg, cnt = rating.get("verified_avg"), rating.get("verified_count") or 0
    r_reasons = []
    if avg is None or avg < c["c07_rating_min"]:
        r_reasons.append(f"verified rating {avg} < {c['c07_rating_min']}")
    if cnt < c["c07_min_verified_reviews"]:
        r_reasons.append(f"only {cnt} verified reviews (< {c['c07_min_verified_reviews']})")
    if (scan.get("sample_size") or 0) < c["c07_min_negative_review_sample"]:
        r_reasons.append("negative-review scan sample too small (3% rule)")
    if scan.get("fatal_defect_found") is not False:
        r_reasons.append("fatal defect found or not assessed: " + str(scan.get("fatal_defect_detail") or "unassessed"))
    if stale_note:
        r_reasons.append(stale_note)
    crit["c07_verified_rating_4_5"] = res(not r_reasons, f"{avg} stars from {cnt} verified buyers" if not r_reasons else "; ".join(r_reasons),
                                          verified_avg=avg, verified_count=cnt, source=rating.get("source"),
                                          top_complaints=scan.get("top_complaints"), improvement_opportunity=scan.get("improvement_opportunity"))

    # --- C8 trend -----------------------------------------------------------------
    trend = product.get("trend_90d") or {}
    primary = classify_trend((trend.get("primary") or {}).get("points") or [], c, as_of)
    corroborating = [{"source": s.get("source"), **classify_trend(s.get("points") or [], c, as_of)} for s in trend.get("corroborating") or []]
    bad_corro = [x for x in corroborating if x["classification"] in ("POST_PEAK_DECLINE", "DECLINING")]
    c8 = primary["classification"] in ("RISING", "PLATEAU_HIGH") and not bad_corro and fresh
    crit["c08_trend_90d_healthy"] = res(
        c8, f"primary {primary['classification']}" + (f"; corroborating declines: {[x['source'] for x in bad_corro]}" if bad_corro else "")
        + (f"; {stale_note}" if stale_note else ""),
        primary_source=(trend.get("primary") or {}).get("source"), primary=primary, corroborating=corroborating)

    # --- C9 problem solver -------------------------------------------------------------
    ps = product.get("problem_solver") or {}
    text_blob = " ".join([product.get("title") or "", " ".join(product.get("claims") or [])])
    excluded = keyword_hits(text_blob, rules["excluded_product_keywords"])
    p_reasons = []
    stmt = ps.get("statement") or ""
    if not re.match(r"^\s*for\s+.+?\s+(who|whose|that|with)\s+.+,\s*this\s+.+", stmt, re.IGNORECASE):
        p_reasons.append("statement must read 'For <audience> who <problem>, this <solution>'")
    aud = (ps.get("audience") or "").strip().lower()
    if not aud or aud in rules["vague_audience_terms"]:
        p_reasons.append("audience missing or vague")
    if ps.get("recurrence") not in rules["allowed_recurrence"]:
        p_reasons.append(f"recurrence must be one of {rules['allowed_recurrence']}")
    if len(ps.get("rationale") or "") < c["c09_min_rationale_chars"]:
        p_reasons.append("rationale too short")
    if ps.get("claude_verdict") != "PASS":
        p_reasons.append("Claude verdict is not PASS")
    if excluded:
        p_reasons.append(f"excluded category keywords: {excluded}")
    crit["c09_everyday_problem_solver"] = res(not p_reasons, stmt if not p_reasons else "; ".join(p_reasons),
                                              statement=stmt, audience=ps.get("audience"), problem=ps.get("problem"),
                                              recurrence=ps.get("recurrence"), niche_fit=ps.get("niche_fit"),
                                              review_gap_upgrade=ps.get("review_gap_upgrade"), rationale=ps.get("rationale"))

    # --- C10 visual hook -------------------------------------------------------------------
    vh = product.get("visual_hook") or {}
    policy = vh.get("ad_policy") or {}
    risk = keyword_hits(" ".join([text_blob, vh.get("hook_3s") or ""]), rules["ad_policy_risk_keywords"])
    v_reasons = []
    if not (vh.get("hook_3s") or "").strip():
        v_reasons.append("missing 3-second hook")
    if vh.get("demo_format") not in rules["allowed_demo_formats"]:
        v_reasons.append(f"demo_format must be one of {rules['allowed_demo_formats']}")
    if not vh.get("media_evidence"):
        v_reasons.append("no media/ad evidence URLs")
    for net in ("meta", "tiktok", "google"):
        if policy.get(net) is not True:
            v_reasons.append(f"{net} ad-policy compliance not confirmed")
    if risk:
        v_reasons.append(f"ad-policy risk keywords: {risk}")
    if len(vh.get("rationale") or "") < c["c10_min_rationale_chars"]:
        v_reasons.append("rationale too short")
    if vh.get("claude_verdict") != "PASS":
        v_reasons.append("Claude verdict is not PASS")
    crit["c10_visual_pain_point_demo"] = res(not v_reasons, vh.get("hook_3s") if not v_reasons else "; ".join(v_reasons),
                                             hook_3s=vh.get("hook_3s"), demo_format=vh.get("demo_format"),
                                             media_evidence=vh.get("media_evidence"), ad_policy=policy, rationale=vh.get("rationale"))

    # --- status ----------------------------------------------------------------------------
    failed = [n for n, key, _ in CRITERIA if crit[key]["status"] == "FAIL"]
    optimizations = []
    if role == "ACTIVE":
        existing_pruned = [p for p in pruned if p.get("shopify_variant_id")]
        if existing_pruned:
            optimizations.append(f"prune {len(existing_pruned)} variant(s)")
        cur = product.get("current_supplier") or {}
        if selected and (cur.get("platform") != selected["platform"] or str(cur.get("listing_id")) != str(selected.get("listing_id"))):
            optimizations.append(f"re-route supplier {cur.get('platform')}:{cur.get('listing_id')} -> {selected['platform']}:{selected.get('listing_id')}")
        if reprice_needed:
            optimizations.append("reprice to >= 3x landed cost")
        status = "FAIL_NEEDS_REPLACEMENT" if failed else ("NEEDS_OPTIMIZATION" if optimizations else "PASS_10_OF_10")
    else:
        status = "REJECTED" if failed else "QUALIFIED"

    return {
        "product_key": product["product_key"],
        "role": role,
        "title": product.get("title"),
        "shopify_product_id": product.get("shopify_product_id"),
        "shopify_handle": product.get("shopify_handle"),
        "collection_handles": product.get("collection_handles") or [],
        "sub_collection_handles": product.get("sub_collection_handles") or [],
        "tier": product.get("tier") or {},
        "status": status,
        "criteria_passed": 10 - len(failed),
        "failed_criteria": failed,
        "optimizations": optimizations,
        "evidence_age_days": age_days,
        "criteria": {key: {"criterion": n, "name": name, **crit[key]} for n, key, name in CRITERIA},
        "supplier": {
            "selected": _slim_quote(selected),
            "selected_platform": (selected or {}).get("platform"),
            "supplier_tier": (selected or {}).get("supplier_tier"),
            "used_last_resort": comparison["used_last_resort"],
            "comparison_complete": comparison["comparison_complete"],
            "justification": comparison["justification"],
            "price_comparison": {p: _slim_quote(d["best_qualifying"]) | {"status": d["status"], "cheapest_landed_any_usd": d["cheapest_landed_any_usd"]}
                                 for p, d in comparison["platforms"].items()},
            "current": product.get("current_supplier"),
        },
        "pricing": {"competitor_median_usd": comp_median, "competitive_cap_usd": max_retail, "variants": pricing_rows,
                    "suggested_retail_usd": pricing_rows[0]["target_retail_usd"] if pricing_rows else None,
                    "landed_cost_usd": (selected or {}).get("landed_cost_usd")},
        "market_summary": {"units_30d": units, "c06_path": path,
                           "latest_month_revenue_usd": monthly[-1].get("revenue_usd") if monthly else None,
                           "trend_90d": primary["classification"]},
        "variants": {"kept": [_slim_variant(v) for v in kept], "pruned": [_slim_variant(v) for v in pruned]},
        "listing": product.get("listing"),
        "seo": product.get("seo"),
    }


def _slim_quote(q):
    if not q:
        return {}
    keys = ("platform", "listing_id", "listing_url", "supplier_name", "supplier_variant_id", "unit_price_usd",
            "shipping_cost_usd", "landed_cost_usd", "shipping_method", "delivery_min_days", "delivery_max_days",
            "ship_from", "stock", "checked_at", "source", "supplier_tier")
    return {k: q.get(k) for k in keys if k in q}


def _slim_variant(v):
    keys = ("variant_key", "shopify_variant_id", "options", "sku", "popularity_share", "supplier_order_count",
            "review_count", "sku_velocity_30d", "current_price_usd", "prune_reason")
    return {k: v.get(k) for k in keys if k in v}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--as-of", help="YYYY-MM-DD (default: evidence collected date / today)")
    args = ap.parse_args()

    evidence = read_json(args.evidence)
    errors = validate_against_schema(evidence, "product_evidence.schema.json")
    if errors:
        for e in errors[:50]:
            log.error("evidence schema: %s", e)
        return 2
    rules = load_rules()
    now = dt.datetime.now(dt.timezone.utc)
    as_of = parse_date(args.as_of) if args.as_of else now.date()
    cards = [evaluate(p, rules, as_of, now) for p in evidence["products"]]
    out = {"scorecards_version": "1.0", "generated_at": utc_now_iso(), "rules_version": rules["rules_version"],
           "as_of": as_of.isoformat(), "scorecards": cards}
    write_json(args.out, out)
    counts: dict[str, int] = {}
    for card in cards:
        counts[card["status"]] = counts.get(card["status"], 0) + 1
    log.info("Scored %d products: %s -> %s", len(cards), counts, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
