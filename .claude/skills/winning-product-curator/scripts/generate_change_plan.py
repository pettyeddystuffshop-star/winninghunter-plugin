#!/usr/bin/env python3
"""Phase A, Step 4: build the immutable, SHA-256-protected Change Plan bundle.

Inputs
  --catalog    catalog.json from shopify_readonly_audit.py
  --scorecards scorecards.json from qualify_product.py (active products AND candidates)
  --proposals  proposals.json written by Claude (replacements, additions, new sub-collections,
               tier requirements) - see examples/proposals.example.json

Hard gates (the generator exits non-zero instead of writing a plan):
  * every proposed replacement / addition must be a QUALIFIED (10/10) scorecard
  * a replacement must target a product whose scorecard is FAIL_NEEDS_REPLACEMENT
  * each failing product gets at most one replacement; each candidate is used once
  * the final plan must validate against schemas/change_plan.schema.json

Outputs (never overwritten; a same-day re-run gets a -2, -3 ... suffix):
  change-plans/YYYY-MM-DD/change-plan-YYYY-MM-DD.json   status PENDING_APPROVAL + integrity.sha256
  change-plans/YYYY-MM-DD/change-plan-YYYY-MM-DD.md     human review document + approval checklist
  change-plans/YYYY-MM-DD/change-plan-YYYY-MM-DD.sha256 "<hash>  <json file>"
"""
from __future__ import annotations

import argparse
import copy
import os
import re
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    canonical_json, get_logger, load_rules, project_root, read_json, sha256_hex, store_today,
    utc_now_iso, validate_against_schema, write_json,
)

log = get_logger("change-plan")

HASH_SCOPE = ("canonical JSON (sorted keys, compact separators, UTF-8) of the plan with "
              "/integrity, /status, /approval and /items/*/approval removed")
QUALIFYING_ACTIVE = {"PASS_10_OF_10", "NEEDS_OPTIMIZATION"}


def plan_hash(plan: dict[str, Any]) -> str:
    """The single definition of the plan hash (also imported by apply_approved_plan.py)."""
    body = copy.deepcopy(plan)
    for key in ("integrity", "status", "approval"):
        body.pop(key, None)
    for item in body.get("items", []):
        item.pop("approval", None)
    return sha256_hex(canonical_json(body))


def file_sha(path: str) -> str:
    return sha256_hex(Path(path).read_text(encoding="utf-8"))


def slugify(text: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")[:80] or "product"


class PlanError(SystemExit):
    pass


def proposed_product(card: dict[str, Any]) -> dict[str, Any]:
    listing = card.get("listing") or {}
    kept = {v["variant_key"]: v for v in card["variants"]["kept"]}
    variants = []
    for row in card["pricing"]["variants"]:
        v = kept.get(row["variant_key"], {})
        variants.append({
            "variant_key": row["variant_key"], "options": v.get("options") or {},
            "price_usd": row["target_retail_usd"], "landed_cost_usd": row["landed_cost_usd"],
            "margin_multiple": row["margin_multiple"], "sku": v.get("sku"),
            "supplier_variant_id": v.get("supplier_variant_id"), "popularity_share": v.get("popularity_share"),
        })
    sel = card["supplier"]["selected"]
    crit = card["criteria"]
    return {
        "product_key": card["product_key"],
        "title": listing.get("title") or card["title"],
        "handle": listing.get("handle") or slugify(listing.get("title") or card["title"]),
        "description_html": listing.get("description_html", ""),
        "product_type": listing.get("product_type", ""),
        "vendor": listing.get("vendor", ""),
        "tags": listing.get("tags", []),
        "image_urls": listing.get("image_urls", []),
        "seo_title": listing.get("seo_title"),
        "seo_description": listing.get("seo_description"),
        "long_tail_keywords": (card.get("seo") or {}).get("long_tail_keywords", []),
        "criteria_passed": card["criteria_passed"],
        "criteria_status": {k: v["status"] for k, v in crit.items()},
        "supplier": {**sel, "supplier_tier": card["supplier"]["supplier_tier"],
                     "used_last_resort": card["supplier"]["used_last_resort"],
                     "justification": card["supplier"]["justification"]},
        "price_comparison": card["supplier"]["price_comparison"],
        "variants": variants,
        "pruned_variants": card["variants"]["pruned"],
        "tier": card.get("tier") or {},
        "evidence_summary": {
            "units_30d": card["market_summary"]["units_30d"],
            "c06_path": card["market_summary"]["c06_path"],
            "latest_month_revenue_usd": card["market_summary"]["latest_month_revenue_usd"],
            "trend_90d": card["market_summary"]["trend_90d"],
            "verified_rating": crit["c07_verified_rating_4_5"]["evidence"].get("verified_avg"),
            "verified_reviews": crit["c07_verified_rating_4_5"]["evidence"].get("verified_count"),
            "pain_point": crit["c09_everyday_problem_solver"]["evidence"].get("statement") or "",
            "visual_hook": crit["c10_visual_pain_point_demo"]["evidence"].get("hook_3s") or "",
        },
    }


def build_plan(catalog, scorecards_doc, proposals, rules, plan_id, plan_date, input_hashes):
    cards = {c["product_key"]: c for c in scorecards_doc["scorecards"]}
    errors: list[str] = []
    warnings: list[str] = list(catalog.get("warnings") or [])
    coll_rules = rules["collections"]
    by_handle = {c["handle"]: c for c in catalog["collections"]}
    hierarchy = {h["handle"]: h for h in catalog["hierarchy"]}
    min_subs, min_products = coll_rules["min_sub_collections"], coll_rules["min_products"]

    active_cards = [c for c in cards.values() if c["role"] == "ACTIVE"]
    scored_ids = {c["shopify_product_id"] for c in active_cards}
    unscored = [p for p in catalog["products"] if f"shopify:{p['id']}" not in cards and p["id"] not in scored_ids]
    if unscored:
        sample = "; ".join(f"{p['title']} ({p['id'].rsplit('/', 1)[-1]})" for p in unscored[:10])
        warnings.append(f"{len(unscored)} active product(s) have no scorecard and are counted as NON-qualifying "
                        f"(score them before approving): {sample}{' ...' if len(unscored) > 10 else ''}")
    qualifying_ids = {c["shopify_product_id"] for c in active_cards if c["status"] in QUALIFYING_ACTIVE}

    items: list[dict[str, Any]] = []

    def add_item(type_, title, rationale, payload, executable=True, depends_on=None):
        item = {"item_id": f"ITEM-{len(items) + 1:03d}", "type": type_, "title": title, "executable": executable,
                "depends_on": depends_on or [], "rationale": rationale, "payload": payload,
                "approval": {"decision": "PENDING", "decided_by": None, "decided_at": None, "note": None}}
        items.append(item)
        return item["item_id"]

    # ---- new sub-collections ------------------------------------------------
    new_sub_items: dict[str, str] = {}
    proposed_subs: dict[str, list[dict[str, Any]]] = {}
    for sub in proposals.get("new_sub_collections", []):
        parent = sub["parent_collection_handle"]
        if parent not in hierarchy:
            errors.append(f"new sub-collection {sub['handle']}: parent {parent!r} is not a primary collection")
            continue
        if sub["handle"] in by_handle:
            errors.append(f"new sub-collection handle {sub['handle']!r} already exists in Shopify")
            continue
        payload = {
            "parent_collection_handle": parent, "parent_collection_id": hierarchy[parent]["collection_id"],
            "handle": sub["handle"], "title": sub["title"], "seo_title": sub["seo_title"],
            "meta_description": sub["meta_description"], "description_html": sub["description_html"],
            "long_tail_keywords": sub["long_tail_keywords"],
            "link_methods": ["metafield:" + os.environ.get("SUBCOLLECTION_PARENT_METAFIELD", "custom.parent_collection"),
                             "menu:" + os.environ.get("NAVIGATION_MENU_HANDLE", "main-menu")],
            "parent_tag": f"parent:{parent}",
            "initial_product_ids": [],
        }
        parent_members = set(by_handle[parent]["active_product_ids"])
        for key in sub.get("existing_product_keys", []):
            card = cards.get(key)
            if not card or card["role"] != "ACTIVE" or card["status"] not in QUALIFYING_ACTIVE:
                errors.append(f"new sub-collection {sub['handle']}: {key} is not a qualifying (10/10) active product")
            elif card["shopify_product_id"] not in parent_members:
                errors.append(f"new sub-collection {sub['handle']}: {key} is not in parent collection {parent!r}")
            else:
                payload["initial_product_ids"].append(card["shopify_product_id"])
        iid = add_item("CREATE_SUB_COLLECTION", f"Create sub-collection '{sub['title']}' under {parent}",
                       sub.get("rationale", "Brings parent to the minimum sub-collection count with a high-margin, long-tail theme."), payload)
        new_sub_items[sub["handle"]] = iid
        proposed_subs.setdefault(parent, []).append({**sub, "item_id": iid})

    used_candidates: set[str] = set()

    def check_candidate(key: str, ctx: str) -> dict[str, Any] | None:
        card = cards.get(key)
        if not card:
            errors.append(f"{ctx}: no scorecard for {key}")
            return None
        if card["status"] != "QUALIFIED" or card["criteria_passed"] != 10:
            errors.append(f"{ctx}: {key} is {card['status']} ({card['criteria_passed']}/10) - only 10/10 QUALIFIED products may be proposed")
            return None
        if key in used_candidates:
            errors.append(f"{ctx}: {key} proposed more than once")
            return None
        if not card["variants"]["kept"]:
            errors.append(f"{ctx}: {key} has no retained variants")
            return None
        used_candidates.add(key)
        return card

    def deps_for(handles):
        return [new_sub_items[h] for h in handles if h in new_sub_items]

    def check_targets(ctx, coll, subs):
        if coll not in hierarchy:
            errors.append(f"{ctx}: target collection {coll!r} is not a primary collection")
        known_subs = {s["handle"] for s in hierarchy.get(coll, {}).get("sub_collections", [])} | \
                     {s["handle"] for s in proposed_subs.get(coll, [])}
        for s in subs:
            if s not in known_subs:
                errors.append(f"{ctx}: sub-collection {s!r} is neither an existing nor a proposed child of {coll!r}")

    # ---- replacements --------------------------------------------------------
    replaced: set[str] = set()
    adds_by_collection: dict[str, int] = {}
    adds_by_sub: dict[str, int] = {}
    planned_cards: dict[str, list[dict[str, Any]]] = {}
    for rep in proposals.get("replacements", []):
        fkey = rep["failing_product_key"]
        fcard = cards.get(fkey)
        ctx = f"replacement for {fkey}"
        if not fcard or fcard["status"] != "FAIL_NEEDS_REPLACEMENT":
            errors.append(f"{ctx}: failing product scorecard missing or not FAIL_NEEDS_REPLACEMENT")
            continue
        if fkey in replaced:
            errors.append(f"{ctx}: more than one replacement proposed")
            continue
        coll, subs = rep["collection_handle"], rep.get("sub_collection_handles", [])
        check_targets(ctx, coll, subs)
        if coll not in (fcard.get("collection_handles") or []):
            errors.append(f"{ctx}: replacement collection {coll!r} is not one of the failing product's collections (1-for-1 rule)")
        card = check_candidate(rep["replacement_product_key"], ctx)
        if not card:
            continue
        replaced.add(fkey)
        product = proposed_product(card)
        add_item("REPLACE_PRODUCT", f"Replace '{fcard['title']}' with '{product['title']}'",
                 rep.get("rationale", "Failing product replaced 1-for-1 by a 10/10 qualified product in the same collection and tier."),
                 {"failing_product_key": fkey, "failing_shopify_product_id": fcard["shopify_product_id"],
                  "failing_title": fcard["title"], "failing_handle": fcard.get("shopify_handle") or "",
                  "failed_criteria": fcard["failed_criteria"],
                  "failed_criteria_detail": {k: v["summary"] for k, v in fcard["criteria"].items() if v["status"] == "FAIL"},
                  "collection_handle": coll, "sub_collection_handles": subs, "tier_note": rep.get("tier_note", ""),
                  "archive_mode": "ARCHIVE_WITH_REDIRECT", "replacement": product},
                 depends_on=deps_for(subs))
        adds_by_collection[coll] = adds_by_collection.get(coll, 0) + 1
        for s in subs:
            adds_by_sub[s] = adds_by_sub.get(s, 0) + 1
        planned_cards.setdefault(coll, []).append(card)

    for fcard in active_cards:
        if fcard["status"] == "FAIL_NEEDS_REPLACEMENT" and fcard["product_key"] not in replaced:
            add_item("REVIEW_FAILING_NO_REPLACEMENT", f"Review failing '{fcard['title']}' (no 10/10 replacement found)",
                     "No qualified replacement was found this week. Nothing will be archived; decide manually or wait for next run.",
                     {"product_key": fcard["product_key"], "shopify_product_id": fcard["shopify_product_id"],
                      "failed_criteria": fcard["failed_criteria"],
                      "failed_criteria_detail": {k: v["summary"] for k, v in fcard["criteria"].items() if v["status"] == "FAIL"}},
                     executable=False)

    # ---- gap-fill additions -------------------------------------------------
    for add in proposals.get("additions", []):
        coll, subs = add["collection_handle"], add.get("sub_collection_handles", [])
        ctx = f"addition {add['product_key']}"
        check_targets(ctx, coll, subs)
        card = check_candidate(add["product_key"], ctx)
        if not card:
            continue
        product = proposed_product(card)
        add_item("ADD_PRODUCT", f"Add '{product['title']}' to {coll}" + (f" / {', '.join(subs)}" if subs else ""),
                 add.get("rationale", "Fills a collection / sub-collection / tier gap with a 10/10 qualified product."),
                 {"collection_handle": coll, "sub_collection_handles": subs, "tier_slot": add.get("tier_slot", ""),
                  "product": product}, depends_on=deps_for(subs))
        adds_by_collection[coll] = adds_by_collection.get(coll, 0) + 1
        for s in subs:
            adds_by_sub[s] = adds_by_sub.get(s, 0) + 1
        planned_cards.setdefault(coll, []).append(card)

    # ---- optimisation items for retained products ------------------------------
    prunes = reroutes = reprices = 0
    for card in active_cards:
        if card["status"] != "NEEDS_OPTIMIZATION":
            continue
        pid = card["shopify_product_id"]
        remove = [v["shopify_variant_id"] for v in card["variants"]["pruned"] if v.get("shopify_variant_id")]
        keep = [v["shopify_variant_id"] for v in card["variants"]["kept"] if v.get("shopify_variant_id")]
        if remove and keep:
            prunes += 1
            add_item("PRUNE_VARIANTS", f"Prune {len(remove)} slow/out-of-spec variant(s) on '{card['title']}'",
                     "Popular-variant-only rule: keep the top variants by supplier order share, review share and SKU velocity.",
                     {"product_key": card["product_key"], "shopify_product_id": pid, "remove_variant_ids": remove,
                      "keep_variant_ids": keep,
                      "reasons": {v["shopify_variant_id"]: v.get("prune_reason") for v in card["variants"]["pruned"] if v.get("shopify_variant_id")},
                      "kept_variants": card["variants"]["kept"]})
        elif remove and not keep:
            warnings.append(f"{card['title']}: all variants would be pruned; no prune item created (product should be re-scored).")
        sel = card["supplier"]["selected"]
        cur = card["supplier"].get("current") or {}
        if any(o.startswith("re-route") for o in card["optimizations"]):
            reroutes += 1
            add_item("REROUTE_SUPPLIER", f"Re-route '{card['title']}' to {sel['platform']} ({card['supplier']['supplier_tier']})",
                     "; ".join(card["supplier"]["justification"]),
                     {"product_key": card["product_key"], "shopify_product_id": pid, "from": cur,
                      "to": {**sel, "supplier_tier": card["supplier"]["supplier_tier"],
                             "used_last_resort": card["supplier"]["used_last_resort"],
                             "justification": card["supplier"]["justification"]},
                      "price_comparison": card["supplier"]["price_comparison"],
                      "variant_mapping": [{"variant_key": v["variant_key"], "shopify_variant_id": v.get("shopify_variant_id"),
                                           "supplier_variant_id": v.get("supplier_variant_id")} for v in card["variants"]["kept"]]})
        kept_ids = {v["variant_key"]: v.get("shopify_variant_id") for v in card["variants"]["kept"]}
        rows = [r for r in card["pricing"]["variants"] if r["current_price_usd"] != r["target_retail_usd"] and kept_ids.get(r["variant_key"])]
        if rows:
            reprices += 1
            add_item("REPRICE_PRODUCT", f"Reprice '{card['title']}' to >= 3x landed cost",
                     f"Competitor median ${card['pricing']['competitor_median_usd']}, cap ${card['pricing']['competitive_cap_usd']}.",
                     {"product_key": card["product_key"], "shopify_product_id": pid,
                      "variants": [{"shopify_variant_id": kept_ids[r["variant_key"]], "variant_key": r["variant_key"],
                                    "from_price_usd": r["current_price_usd"], "to_price_usd": r["target_retail_usd"],
                                    "landed_cost_usd": r["landed_cost_usd"], "margin_multiple": r["margin_multiple"]} for r in rows]})

    if errors:
        raise PlanError("Change Plan refused:\n  - " + "\n  - ".join(errors))

    # ---- collection audit -----------------------------------------------------
    tier_reqs = proposals.get("tier_requirements", {})
    collections_out = []
    for handle, h in hierarchy.items():
        col = by_handle[handle]
        members = set(col["active_product_ids"])
        current = len(members & qualifying_ids)
        after = current + adds_by_collection.get(handle, 0)
        subs_out = []
        for s in h["sub_collections"]:
            sc = by_handle[s["handle"]]
            s_cur = len(set(sc["active_product_ids"]) & qualifying_ids)
            s_after = s_cur + adds_by_sub.get(s["handle"], 0)
            subs_out.append({"handle": s["handle"], "title": sc["title"], "collection_id": sc["id"], "exists": True,
                             "proposed_item_id": None, "qualifying_count_current": s_cur,
                             "qualifying_count_after_plan": s_after, "target_min_products": min_products,
                             "deficit_after_plan": max(0, min_products - s_after), "avg_margin_multiple": None})
        for s in proposed_subs.get(handle, []):
            s_after = adds_by_sub.get(s["handle"], 0) + len(s.get("existing_product_keys", []))
            subs_out.append({"handle": s["handle"], "title": s["title"], "collection_id": None, "exists": False,
                             "proposed_item_id": s["item_id"], "qualifying_count_current": 0,
                             "qualifying_count_after_plan": s_after, "target_min_products": min_products,
                             "deficit_after_plan": max(0, min_products - s_after),
                             "avg_margin_multiple": s.get("avg_margin_multiple")})
        # tier matrix over qualifying active members + planned additions
        tier_cards = [c for c in active_cards if c["status"] in QUALIFYING_ACTIVE and c["shopify_product_id"] in members]
        tier_cards += planned_cards.get(handle, [])
        prices = [c["pricing"]["suggested_retail_usd"] for c in tier_cards if c["pricing"].get("suggested_retail_usd")]
        median = statistics.median(prices) if prices else None
        price_tiers = {t: 0 for t in coll_rules["required_price_tiers"]}
        dims: dict[str, set[str]] = {}
        makers: set[str] = set()
        for c in tier_cards:
            t = c.get("tier") or {}
            pt = t.get("price_tier")
            if not pt and median and c["pricing"].get("suggested_retail_usd"):
                r = c["pricing"]["suggested_retail_usd"] / median
                pt = "budget" if r <= coll_rules["price_tier_budget_max_ratio"] else ("premium" if r >= coll_rules["price_tier_premium_min_ratio"] else "mid")
            if pt:
                price_tiers[pt] = price_tiers.get(pt, 0) + 1
            for k, v in (t.get("dimensions") or {}).items():
                dims.setdefault(k, set()).add(v)
            if t.get("manufacturer"):
                makers.add(t["manufacturer"])
        gaps = [f"price tier '{t}' not covered" for t, n in price_tiers.items() if n == 0]
        for dim, values in (tier_reqs.get(handle) or {}).items():
            missing = [v for v in values if v not in dims.get(dim, set())]
            if missing:
                gaps.append(f"{dim}: missing {', '.join(missing)}")
        sub_after = len(subs_out)
        meets = after >= min_products and sub_after >= min_subs and all(s["qualifying_count_after_plan"] >= min_products for s in subs_out)
        if not meets:
            warnings.append(f"Collection '{handle}' still below rules after this plan: {after}/{min_products} products, "
                            f"{sub_after}/{min_subs} sub-collections; sub-collection deficits "
                            + ", ".join(f"{s['handle']}={s['deficit_after_plan']}" for s in subs_out if s["deficit_after_plan"]))
        collections_out.append({
            "handle": handle, "title": col["title"], "collection_id": col["id"], "sub_collections": subs_out,
            "sub_collection_count_current": len(h["sub_collections"]), "sub_collection_count_after_plan": sub_after,
            "min_sub_collections": min_subs, "qualifying_count_current": current, "qualifying_count_after_plan": after,
            "target_min_products": min_products, "deficit_current": max(0, min_products - current),
            "deficit_after_plan": max(0, min_products - after),
            "tier_matrix": {"price_tiers": price_tiers, "dimensions": {k: sorted(v) for k, v in dims.items()},
                            "manufacturers": sorted(makers)},
            "tier_gaps": gaps, "meets_rules_after_plan": meets,
        })

    summary = {
        "total_active_audited": len(active_cards),
        "passed_10_of_10": sum(1 for c in active_cards if c["status"] in QUALIFYING_ACTIVE),
        "needs_optimization": sum(1 for c in active_cards if c["status"] == "NEEDS_OPTIMIZATION"),
        "slated_for_replacement": sum(1 for c in active_cards if c["status"] == "FAIL_NEEDS_REPLACEMENT"),
        "replacements_found": len(replaced),
        "failing_without_replacement": sum(1 for c in active_cards if c["status"] == "FAIL_NEEDS_REPLACEMENT") - len(replaced),
        "net_new_additions": sum(1 for i in items if i["type"] == "ADD_PRODUCT"),
        "sub_collections_proposed": len(new_sub_items),
        "variant_prunes": prunes, "supplier_reroutes": reroutes, "reprices": reprices,
        "collections_total": len(collections_out),
        "collections_meeting_rules_after_plan": sum(1 for c in collections_out if c["meets_rules_after_plan"]),
    }
    referenced = {c["product_key"] for c in active_cards} | used_candidates
    shop = catalog["shop"]
    plan = {
        "plan_version": "1.0", "plan_id": plan_id, "plan_date": plan_date, "generated_at": utc_now_iso(),
        "mode": "plan", "status": "PENDING_APPROVAL",
        "store": {"shop_domain": shop["myshopifyDomain"], "name": shop.get("name", ""),
                  "timezone": os.environ.get("STORE_TIMEZONE") or shop.get("ianaTimezone", "UTC"),
                  "currency": shop.get("currencyCode", "USD")},
        "rules_snapshot": {k: rules[k] for k in ("rules_version", "criteria", "collections", "suppliers", "variants")},
        "inputs": input_hashes, "summary": summary,
        "scorecards": [c for k, c in cards.items() if k in referenced],
        "collections": collections_out, "items": items, "warnings": warnings,
        "integrity": {"algorithm": "SHA-256", "hash_scope": HASH_SCOPE, "sha256": "0" * 64},
        "approval": {"status": "PENDING_APPROVAL", "approved_by": None, "approved_at": None,
                     "confirm_hash_verified": False, "approved_item_ids": [], "rejected_item_ids": []},
    }
    plan["integrity"]["sha256"] = plan_hash(plan)
    return plan


# ------------------------------------------------------------------ markdown

def _m(v, prefix="$"):
    return "n/a" if v is None else f"{prefix}{v:,.2f}"


def _pc(pc, plat):
    d = pc.get(plat) or {}
    if d.get("landed_cost_usd") is not None:
        return f"{_m(d['landed_cost_usd'])}"
    return (d.get("status") or "NOT_CHECKED").lower().replace("_", " ")


def _esc(text) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def render_markdown(plan: dict[str, Any], json_name: str) -> str:
    s = plan["summary"]
    h = plan["integrity"]["sha256"]
    rel_json = f"./change-plans/{plan['plan_date']}/{json_name}"
    L = [f"# Weekly Change Plan — {plan['store']['name'] or plan['store']['shop_domain']} — {plan['plan_date']}", "",
         f"> **Status: {plan['status']}** · Nothing has been changed in Shopify, DSers, CJ or AutoDS. "
         "Every item below needs your explicit approval before Phase B can run.", "",
         f"- Plan ID: `{plan['plan_id']}`  ", f"- Generated: {plan['generated_at']} (store TZ {plan['store']['timezone']})  ",
         f"- SHA-256: `{h}`  ", f"- Rules version: {plan['rules_snapshot']['rules_version']}", "",
         "## Store Health Summary", "",
         "| Metric | Value |", "|---|---|",
         f"| Total active products audited | {s['total_active_audited']} |",
         f"| Passed 10/10 (kept) | {s['passed_10_of_10']} (of which {s['needs_optimization']} need optimisation) |",
         f"| Slated for replacement | {s['slated_for_replacement']} ({s['replacements_found']} replacements found, {s['failing_without_replacement']} without) |",
         f"| Net new additions | {s['net_new_additions']} |",
         f"| Sub-collections proposed | {s['sub_collections_proposed']} |",
         f"| Variant prunes / supplier re-routes / reprices | {s['variant_prunes']} / {s['supplier_reroutes']} / {s['reprices']} |",
         f"| Collections meeting ≥{plan['rules_snapshot']['collections']['min_sub_collections']} sub-collections & ≥{plan['rules_snapshot']['collections']['min_products']} products after plan | {s['collections_meeting_rules_after_plan']} / {s['collections_total']} |",
         ""]
    if plan["warnings"]:
        L += ["### Warnings", ""] + [f"- {_esc(w)}" for w in plan["warnings"]] + [""]

    # Table 1
    L += ["## Table 1 — Failing Products & Proposed 1-for-1 Replacements", ""]
    reps = [i for i in plan["items"] if i["type"] in ("REPLACE_PRODUCT", "REVIEW_FAILING_NO_REPLACEMENT")]
    if not reps:
        L += ["_No failing products this week._", ""]
    else:
        L += ["| Item | Current product (ID / title) | Failed criteria | Replacement | Supplier (tier) | aliexpress.us | cjdropshipping.us | DSers | Unit + Ship = Landed | Ship days | Retail (×) | 30d units | 90d rev / trend | Rating (verified) | Pain point · visual hook | Kept variants |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for i in reps:
            p = i["payload"]
            if i["type"] == "REVIEW_FAILING_NO_REPLACEMENT":
                L.append(f"| {i['item_id']} | `{p['shopify_product_id']}` | {', '.join(map(str, p['failed_criteria']))} | **none found** — manual review | | | | | | | | | | | | |")
                continue
            r = p["replacement"]
            sup, ev, pc = r["supplier"], r["evidence_summary"], r["price_comparison"]
            v0 = r["variants"][0]
            L.append(
                f"| {i['item_id']} | `{p['failing_shopify_product_id']}` {_esc(p['failing_title'])} | {', '.join(map(str, p['failed_criteria']))} "
                f"| {_esc(r['title'])} | {sup['platform']} ({sup['supplier_tier']}) | {_pc(pc, 'aliexpress.us')} | {_pc(pc, 'cjdropshipping.us')} | {_pc(pc, 'dsers')} "
                f"| {_m(sup['unit_price_usd'])} + {_m(sup['shipping_cost_usd'])} = **{_m(sup['landed_cost_usd'])}** | {sup.get('delivery_min_days')}–{sup['delivery_max_days']} "
                f"| {_m(v0['price_usd'])} ({v0['margin_multiple']}×) | {ev['units_30d']:,} | {ev['c06_path']} · {_m(ev.get('latest_month_revenue_usd'))}/mo · {ev['trend_90d']} "
                f"| {ev['verified_rating']} ({ev['verified_reviews']:,}) | {_esc(ev['pain_point'])} · _{_esc(ev['visual_hook'])}_ "
                f"| {', '.join(v['variant_key'] for v in r['variants'])} |")
        L.append("")
        for i in reps:
            if i["type"] == "REPLACE_PRODUCT":
                L += [f"<details><summary>{i['item_id']} failure detail — {_esc(i['payload']['failing_title'])}</summary>", ""]
                L += [f"- **{k}**: {_esc(v)}" for k, v in i["payload"]["failed_criteria_detail"].items()]
                L += [f"- Supplier justification: {_esc('; '.join(i['payload']['replacement']['supplier']['justification']))}", "", "</details>", ""]

    # Table 2
    L += ["## Table 2 — Collection & Sub-Collection Gap-Fill Plan", "",
          "| Primary collection | Sub-collections (existing · **proposed**) | Products now → after (target) | Sub-collections now → after (target) | Sub-collection counts after | Tier matrix covered | Tier gaps | Meets rules after plan |",
          "|---|---|---|---|---|---|---|---|"]
    for c in plan["collections"]:
        subs = ", ".join((f"**{s['title']}** ({s['proposed_item_id']})" if not s["exists"] else s["title"]) for s in c["sub_collections"]) or "—"
        counts = ", ".join(f"{s['handle']}: {s['qualifying_count_after_plan']}/{s['target_min_products']}" for s in c["sub_collections"]) or "—"
        tm = c["tier_matrix"]
        tiers = "price " + "/".join(f"{k}:{v}" for k, v in tm["price_tiers"].items())
        tiers += "".join(f"; {k}: {', '.join(v)}" for k, v in tm["dimensions"].items())
        if tm["manufacturers"]:
            tiers += f"; makers: {', '.join(tm['manufacturers'])}"
        L.append(f"| {c['title']} (`{c['handle']}`) | {subs} | {c['qualifying_count_current']} → {c['qualifying_count_after_plan']} ({c['target_min_products']}) "
                 f"| {c['sub_collection_count_current']} → {c['sub_collection_count_after_plan']} ({c['min_sub_collections']}) | {counts} | {_esc(tiers)} "
                 f"| {_esc('; '.join(c['tier_gaps'])) or '—'} | {'✅' if c['meets_rules_after_plan'] else '❌ deficit remains'} |")
    L.append("")
    adds = [i for i in plan["items"] if i["type"] == "ADD_PRODUCT"]
    if adds:
        L += ["### Queued additions", "",
              "| Item | Product | Collection / sub-collections | Tier slot | Supplier (tier) | Landed | Retail (×) | 30d units | Trend | Rating |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for i in adds:
            p, r = i["payload"], i["payload"]["product"]
            L.append(f"| {i['item_id']} | {_esc(r['title'])} | {p['collection_handle']} / {', '.join(p['sub_collection_handles']) or '—'} | {_esc(p['tier_slot'])} "
                     f"| {r['supplier']['platform']} ({r['supplier']['supplier_tier']}) | {_m(r['supplier']['landed_cost_usd'])} "
                     f"| {_m(r['variants'][0]['price_usd'])} ({r['variants'][0]['margin_multiple']}×) | {r['evidence_summary']['units_30d']:,} "
                     f"| {r['evidence_summary']['trend_90d']} | {r['evidence_summary']['verified_rating']} |")
        L.append("")
    subs_items = [i for i in plan["items"] if i["type"] == "CREATE_SUB_COLLECTION"]
    if subs_items:
        L += ["### Proposed sub-collections", "", "| Item | Parent | Handle | Title | SEO title | Long-tail keywords |", "|---|---|---|---|---|---|"]
        for i in subs_items:
            p = i["payload"]
            L.append(f"| {i['item_id']} | {p['parent_collection_handle']} | `{p['handle']}` | {_esc(p['title'])} | {_esc(p['seo_title'])} | {_esc(', '.join(p['long_tail_keywords']))} |")
        L.append("")

    # Scorecards for every proposed product
    proposed_keys = [i["payload"].get("replacement", i["payload"].get("product", {})).get("product_key")
                     for i in plan["items"] if i["type"] in ("REPLACE_PRODUCT", "ADD_PRODUCT")]
    cards = {c["product_key"]: c for c in plan["scorecards"]}
    if proposed_keys:
        L += ["### 10-point scorecards for every queued replacement / addition", ""]
        for key in proposed_keys:
            c = cards[key]
            L += [f"<details><summary>{_esc(c['title'])} — {c['criteria_passed']}/10</summary>", "", "| # | Criterion | Result | Evidence |", "|---|---|---|---|"]
            for crit in c["criteria"].values():
                L.append(f"| {crit['criterion']} | {_esc(crit['name'])} | {crit['status']} | {_esc(crit['summary'])} |")
            L += ["", "</details>", ""]

    # Table 3
    L += ["## Table 3 — Variant Pruning, Supplier Re-Routing & Repricing (retained products)", ""]
    opt = [i for i in plan["items"] if i["type"] in ("PRUNE_VARIANTS", "REROUTE_SUPPLIER", "REPRICE_PRODUCT")]
    if not opt:
        L += ["_No optimisation items._", ""]
    else:
        L += ["| Item | Type | Product | Change | Evidence |", "|---|---|---|---|---|"]
        for i in opt:
            p = i["payload"]
            if i["type"] == "PRUNE_VARIANTS":
                change = f"remove {len(p['remove_variant_ids'])}, keep {len(p['keep_variant_ids'])}"
                evid = "; ".join(f"{k.split('/')[-1]}: {v}" for k, v in p["reasons"].items())
            elif i["type"] == "REROUTE_SUPPLIER":
                change = f"{p['from'].get('platform')}:{p['from'].get('listing_id')} → {p['to']['platform']}:{p['to'].get('listing_id')} ({_m(p['to']['landed_cost_usd'])} landed, {p['to']['delivery_max_days']}d)"
                evid = f"ali {_pc(p['price_comparison'], 'aliexpress.us')} · cj {_pc(p['price_comparison'], 'cjdropshipping.us')} · dsers {_pc(p['price_comparison'], 'dsers')}"
            else:
                change = "; ".join(f"{v['variant_key']}: {_m(v['from_price_usd'])} → {_m(v['to_price_usd'])}" for v in p["variants"])
                evid = "; ".join(f"{v['variant_key']} landed {_m(v['landed_cost_usd'])} ({v['margin_multiple']}×)" for v in p["variants"])
            L.append(f"| {i['item_id']} | {i['type']} | `{p['shopify_product_id']}` | {_esc(change)} | {_esc(evid)} |")
        L.append("")

    # Approval checklist
    L += ["## Approval checklist", "",
          "Tick `[x]` to approve or write `[-]` to reject each item, then sync the decisions into the JSON "
          "(the hash is unaffected by decisions):", "",
          "```bash",
          f"python3 .claude/skills/winning-product-curator/scripts/apply_approved_plan.py review --plan {rel_json} --confirm-hash {h} --from-markdown ./change-plans/{plan['plan_date']}/{json_name[:-5]}.md --approved-by \"<your name>\"",
          "```", ""]
    for i in plan["items"]:
        flag = "" if i["executable"] else " _(informational, not executable)_"
        L.append(f"- [ ] {i['item_id']} — {i['type']} — {_esc(i['title'])}{flag}")
    L += ["", "## Apply (Phase B)", "", "Only after approving items, run in Claude Code:", "",
          f"```\n/winning-product-curator --apply {rel_json} --confirm-hash {h}\n```", "",
          "Phase B creates new products as DRAFT, links suppliers, and only archives a failing product (with a URL redirect) "
          "after its replacement is live. Nothing is ever hard-deleted.", ""]
    return "\n".join(L)


def next_free_paths(out_dir: Path, date: str) -> tuple[str, Path, Path, Path]:
    n = 1
    while True:
        suffix = "" if n == 1 else f"-{n}"
        base = f"change-plan-{date}{suffix}"
        jp, mp, hp = out_dir / f"{base}.json", out_dir / f"{base}.md", out_dir / f"{base}.sha256"
        if not jp.exists() and not mp.exists():
            return f"wpc-{date}{suffix}", jp, mp, hp
        n += 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--scorecards", required=True)
    ap.add_argument("--proposals", required=True)
    ap.add_argument("--date", default=None, help="Plan date YYYY-MM-DD (default: today in STORE_TIMEZONE)")
    ap.add_argument("--out-root", default=None, help="Default: <project>/change-plans")
    args = ap.parse_args()

    rules = load_rules()
    date = args.date or store_today().isoformat()
    out_dir = Path(args.out_root or project_root() / "change-plans") / date
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_id, jp, mp, hp = next_free_paths(out_dir, date)

    catalog, scorecards, proposals = read_json(args.catalog), read_json(args.scorecards), read_json(args.proposals)
    perrs = validate_against_schema(proposals, "proposals.schema.json")
    if perrs:
        for e in perrs[:50]:
            log.error("proposals schema: %s", e)
        return 2
    hashes = {"catalog_sha256": file_sha(args.catalog), "scorecards_sha256": file_sha(args.scorecards),
              "proposals_sha256": file_sha(args.proposals), "catalog_generated_at": catalog.get("generated_at", ""),
              "scorecards_generated_at": scorecards.get("generated_at", "")}
    try:
        plan = build_plan(catalog, scorecards, proposals, rules, plan_id, date, hashes)
    except PlanError as exc:
        log.error("%s", exc)
        return 2
    errs = validate_against_schema(plan, "change_plan.schema.json")
    if errs:
        for e in errs[:50]:
            log.error("change plan schema: %s", e)
        return 2

    write_json(jp, plan)
    mp.write_text(render_markdown(plan, jp.name), encoding="utf-8")
    hp.write_text(f"{plan['integrity']['sha256']}  {jp.name}\n", encoding="utf-8")
    os.chmod(hp, 0o444)  # the .md stays writable so the owner can tick approval boxes
    log.info("Change Plan written: %s", jp)
    print(f"PLAN_JSON={jp}")
    print(f"PLAN_MD={mp}")
    print(f"PLAN_SHA256={plan['integrity']['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
