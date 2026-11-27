#!/usr/bin/env python3
"""Phase B: gated executor for a human-approved Change Plan.

Nothing in this file runs a mutation unless ALL of these hold:
  1. The plan validates against schemas/change_plan.schema.json.
  2. The recomputed SHA-256 equals integrity.sha256 AND the --confirm-hash you typed.
  3. The plan's shop domain equals SHOPIFY_SHOP_DOMAIN and the plan is not older than
     plan.max_age_days_for_apply (default 14 days).
  4. WPC_MODE=apply and SHOPIFY_ADMIN_WRITE_TOKEN are set (the weekly runner never sets either).
  5. A human recorded decisions (`review`), approved_by is set, and at least one item is APPROVED.
  6. The confirmation phrase `APPLY <first 12 hash chars>` is typed at the prompt
     (or passed with --confirm-phrase after the owner says it in chat).
Only APPROVED, executable items run. Decisions live in the plan JSON but are outside
the hash scope, so approving/rejecting items never invalidates the hash.

Lifecycle
  review     record per-item APPROVED/REJECTED (interactive, flags, or synced from the .md checklist)
  verify     run gates 1-3 and print a summary (no writes)
  execute    stage 1: create sub-collections, create new products as DRAFT, prune variants,
             reprice; write supplier work orders (DSers / CJ / AutoDS linking, re-routes).
             Failing products are NOT touched in this stage.
  mark-linked  record that Claude linked the supplier for an item via DSers/CJ/AutoDS MCP
  finalize   stage 2: for linked items, activate + publish the new product, then ARCHIVE the
             replaced product (status ARCHIVED, order history kept) and create a URL redirect
             old handle -> new handle. Nothing is ever hard-deleted.
  status     print the execution journal

Usage
  apply_approved_plan.py review   --plan P --confirm-hash H [--approve all|ITEM-001,..] [--reject ..] [--from-markdown MD] --approved-by NAME
  apply_approved_plan.py verify   --plan P --confirm-hash H
  WPC_MODE=apply apply_approved_plan.py execute  --plan P --confirm-hash H [--confirm-phrase "APPLY abcdef123456"] [--dry-run]
  apply_approved_plan.py mark-linked --plan P --confirm-hash H --item ITEM-003 --platform dsers --reference "DSers mapping 123"
  WPC_MODE=apply apply_approved_plan.py finalize --plan P --confirm-hash H [--confirm-phrase ...] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    get_logger, load_env, load_rules, parse_date, read_json, shopify_client_from_env, store_today,
    utc_now_iso, validate_against_schema, write_json,
)
from generate_change_plan import plan_hash  # noqa: E402
from shopify_readonly_audit import _normalize_sources  # noqa: E402

log = get_logger("apply")

EXEC_ORDER = ["CREATE_SUB_COLLECTION", "REPLACE_PRODUCT", "ADD_PRODUCT", "PRUNE_VARIANTS", "REPRICE_PRODUCT", "REROUTE_SUPPLIER"]


class GateError(SystemExit):
    def __init__(self, msg: str):
        super().__init__(f"BLOCKED: {msg}")


# ------------------------------------------------------------------- gates

def load_plan(path: str, confirm_hash: str) -> dict[str, Any]:
    plan = read_json(path)
    errs = validate_against_schema(plan, "change_plan.schema.json")
    if errs:
        raise GateError("plan fails schema validation:\n  " + "\n  ".join(errs[:20]))
    actual = plan_hash(plan)
    if actual != plan["integrity"]["sha256"]:
        raise GateError(f"plan content was modified after generation (recomputed {actual} != recorded {plan['integrity']['sha256']})")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", confirm_hash or "") or confirm_hash.lower() != actual:
        raise GateError("--confirm-hash does not match the plan's SHA-256 (full 64-hex hash required)")
    return plan


def gate_store_and_age(plan: dict[str, Any]) -> None:
    load_env()
    domain = os.environ.get("SHOPIFY_SHOP_DOMAIN")
    if domain != plan["store"]["shop_domain"]:
        raise GateError(f"plan is for {plan['store']['shop_domain']} but SHOPIFY_SHOP_DOMAIN={domain}")
    max_age = load_rules()["plan"]["max_age_days_for_apply"]
    age = (store_today() - parse_date(plan["plan_date"])).days
    if age > max_age:
        raise GateError(f"plan is {age} days old (max {max_age}); generate a fresh plan - supplier prices and stock drift")
    if plan["status"] in ("EXPIRED", "REJECTED", "APPLIED"):
        raise GateError(f"plan status is {plan['status']}")


def gate_write_mode(plan: dict[str, Any], phrase: str | None, dry_run: bool) -> None:
    if dry_run:
        return
    if os.environ.get("WPC_MODE") != "apply":
        raise GateError("WPC_MODE must be 'apply' for Phase B (the scheduled runner forces WPC_MODE=plan)")
    if os.environ.get("WPC_HEADLESS") == "1":
        raise GateError("Phase B never runs headless/scheduled")
    from common import project_root
    load_env(project_root() / ".env.apply")
    if not os.environ.get("SHOPIFY_ADMIN_WRITE_TOKEN"):
        raise GateError("SHOPIFY_ADMIN_WRITE_TOKEN is not set (put it in .env.apply, never in .env)")
    appr = plan["approval"]
    if not appr.get("approved_by"):
        raise GateError("no human approval recorded - run `review` first")
    if not any(i["approval"]["decision"] == "APPROVED" and i["executable"] for i in plan["items"]):
        raise GateError("no executable item is APPROVED")
    expected = f"APPLY {plan['integrity']['sha256'][:12]}"
    if phrase is None and sys.stdin.isatty():
        print(f"\nAbout to modify {plan['store']['shop_domain']} using plan {plan['plan_id']}.")
        phrase = input(f"Type exactly '{expected}' to continue: ").strip()
    if phrase != expected:
        raise GateError(f"confirmation phrase mismatch (expected '{expected}')")


# --------------------------------------------------------------- journal

def journal_path(plan_path: str, plan: dict[str, Any]) -> Path:
    return Path(plan_path).parent / f"execution-journal-{plan['plan_id']}.json"


def load_journal(plan_path: str, plan: dict[str, Any]) -> dict[str, Any]:
    jp = journal_path(plan_path, plan)
    if jp.exists():
        j = read_json(jp)
        if j["plan_sha256"] != plan["integrity"]["sha256"]:
            raise GateError("execution journal belongs to a different plan hash")
        return j
    return {"plan_id": plan["plan_id"], "plan_sha256": plan["integrity"]["sha256"], "items": {}, "events": []}


def save_journal(plan_path, plan, journal) -> None:
    write_json(journal_path(plan_path, plan), journal)


def jlog(journal, item_id, state, **data):
    rec = journal["items"].setdefault(item_id, {"state": "PENDING"})
    rec["state"] = state
    rec.update(data)
    rec["updated_at"] = utc_now_iso()
    journal["events"].append({"at": rec["updated_at"], "item_id": item_id, "state": state,
                              **{k: v for k, v in data.items() if k in ("error", "note")}})


# ------------------------------------------------------------- Shopify writes

def user_errors(payload: dict[str, Any], name: str) -> None:
    errs = payload.get("userErrors") or []
    if errs:
        raise RuntimeError(f"{name}: " + "; ".join(f"{e.get('field')}: {e.get('message')}" for e in errs))


class ShopifyWriter:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.read = shopify_client_from_env(read_only=True)
        self.write = None if dry_run else shopify_client_from_env(read_only=False)
        self._publication = None

    def mutate(self, name: str, doc: str, variables: dict[str, Any]) -> dict[str, Any]:
        if self.dry_run:
            log.info("[dry-run] %s %s", name, {k: (v if len(str(v)) < 300 else str(v)[:300] + "...") for k, v in variables.items()})
            return {}
        data = self.write.execute(doc, variables)[name]
        user_errors(data, name)
        return data

    def online_store_publication(self) -> str | None:
        if self._publication is None:
            pubs = self.read.execute("query { publications(first: 25) { edges { node { id name } } } }")["publications"]["edges"]
            match = [e["node"]["id"] for e in pubs if e["node"]["name"].lower() == "online store"]
            self._publication = match[0] if match else ""
        return self._publication or None

    def collection_info(self, cid: str) -> dict[str, Any]:
        info = self.read.execute("""query($id: ID!) { collection(id: $id) { id handle
            sources {
              __typename
              ... on CollectionConditionsSource {
                inclusion { matchType conditions {
                  __typename
                  ... on CollectionSourceInclusionConditionProductTag { relation values }
                } }
              }
              ... on CollectionSubCollectionsSource { collections { id handle } }
            } } }""", {"id": cid})["collection"]
        info["ruleSet"], _ = _normalize_sources(info.pop("sources", None))  # see shopify_readonly_audit._normalize_sources
        return info

    def publish(self, gid: str) -> None:
        pub = self.online_store_publication()
        if not pub:
            log.warning("Online Store publication not found; %s left unpublished", gid)
            return
        self.mutate("publishablePublish", """
            mutation($id: ID!, $pub: ID!) { publishablePublish(id: $id, input: [{publicationId: $pub}]) { userErrors { field message } } }""",
                    {"id": gid, "pub": pub})


def _menu_items_to_input(items):
    out = []
    for it in items or []:
        node = {"id": it["id"], "title": it["title"], "type": it["type"], "items": _menu_items_to_input(it.get("items"))}
        if it.get("resourceId"):
            node["resourceId"] = it["resourceId"]
        if it.get("url") and not it.get("resourceId"):
            node["url"] = it["url"]
        if it.get("tags"):
            node["tags"] = it["tags"]
        out.append(node)
    return out


def add_menu_child(sw: ShopifyWriter, parent_id: str, child_id: str, title: str) -> str:
    handle = os.environ.get("NAVIGATION_MENU_HANDLE", "main-menu")
    menus = sw.read.execute("""query { menus(first: 25) { edges { node { id handle title
        items { id title type resourceId url tags items { id title type resourceId url tags items { id title type resourceId url tags } } } } } } }""")
    menu = next((e["node"] for e in menus["menus"]["edges"] if e["node"]["handle"] == handle), None)
    if not menu:
        return f"menu {handle!r} not found; navigation unchanged"
    items = _menu_items_to_input(menu["items"])

    def attach(nodes) -> bool:
        for n in nodes:
            if n.get("resourceId") == parent_id:
                if not any(c.get("resourceId") == child_id for c in n["items"]):
                    n["items"].append({"title": title, "type": "COLLECTION", "resourceId": child_id, "items": []})
                return True
            if attach(n["items"]):
                return True
        return False

    if not attach(items):
        return f"parent collection not present in menu {handle!r}; navigation unchanged"
    sw.mutate("menuUpdate", """mutation($id: ID!, $title: String!, $items: [MenuItemUpdateInput!]!) {
        menuUpdate(id: $id, title: $title, items: $items) { menu { id } userErrors { field message } } }""",
              {"id": menu["id"], "title": menu["title"], "items": items})
    return f"nested under parent in menu {handle!r}"


def create_sub_collection(sw, item, journal) -> None:
    # Shopify's flexible-collections model (API 2026-07+) removed CollectionInput.products and the old
    # `input: CollectionInput` argument from collectionCreate; manual products are now a conditions
    # source with explicit `selections`. See docs/apps/build/product-merchandising/.../migrate-to-flexible-collections.
    p = item["payload"]
    ns, _, key = os.environ.get("SUBCOLLECTION_PARENT_METAFIELD", "custom.parent_collection").partition(".")
    initial = p.get("initial_product_ids") or []
    collection_input = {"title": p["title"], "handle": p["handle"], "descriptionHtml": p["description_html"],
                        "seo": {"title": p["seo_title"], "description": p["meta_description"]},
                        "metafields": [{"namespace": ns, "key": key, "type": "collection_reference",
                                        "value": p["parent_collection_id"]}]}
    if initial:
        collection_input["sources"] = [{"source": {"targetType": "PRODUCTS", "title": p["title"],
                                                    "inclusion": {"selections": [{"productId": pid} for pid in initial]}}}]
    data = sw.mutate("collectionCreate", """mutation($input: CollectionCreateInput!) {
        collectionCreate(collection: $input) { collection { id handle } userErrors { field message } } }""",
                     {"input": collection_input})
    cid = (data.get("collection") or {}).get("id", f"dry-run:{p['handle']}")
    if not sw.dry_run:
        sw.publish(cid)
        nav = add_menu_child(sw, p["parent_collection_id"], cid, p["title"])
    else:
        nav = "dry-run"
    jlog(journal, item["item_id"], "DONE", collection_id=cid, handle=p["handle"], navigation=nav)


def resolve_collection_ids(sw, item, journal, plan) -> tuple[list[str], list[str]]:
    """Return (manual collection ids to join, extra tags for smart collections)."""
    by_handle = {c["handle"]: c["collection_id"] for c in plan["collections"]}
    for c in plan["collections"]:
        for s in c["sub_collections"]:
            if s["collection_id"]:
                by_handle[s["handle"]] = s["collection_id"]
    for i in plan["items"]:
        if i["type"] == "CREATE_SUB_COLLECTION":
            rec = journal["items"].get(i["item_id"], {})
            if rec.get("collection_id"):
                by_handle[i["payload"]["handle"]] = rec["collection_id"]
    p = item["payload"]
    handles = [p["collection_handle"], *p.get("sub_collection_handles", [])]
    ids, tags = [], []
    for h in handles:
        cid = by_handle.get(h)
        if not cid:
            raise RuntimeError(f"collection {h!r} has no id (was its CREATE_SUB_COLLECTION item approved and executed?)")
        if cid.startswith("dry-run:"):
            continue
        info = sw.collection_info(cid)
        if info.get("ruleSet"):
            for r in info["ruleSet"]["rules"]:
                if r["column"] == "TAG" and r["relation"] == "EQUALS":
                    tags.append(r["condition"])
                else:
                    log.warning("Smart collection %s has non-tag rule %s; product must satisfy it via its own fields", h, r)
        else:
            ids.append(cid)
    parent_tag = f"parent:{p['collection_handle']}"
    return ids, sorted(set(tags + [parent_tag]))


def create_draft_product(sw, item, journal, plan) -> None:
    p = item["payload"]
    prod = p.get("replacement") or p["product"]
    coll_ids, extra_tags = resolve_collection_ids(sw, item, journal, plan)
    option_names: list[str] = []
    for v in prod["variants"]:
        for name in v["options"]:
            if name not in option_names:
                option_names.append(name)
    if not option_names:
        options = [{"name": "Title", "values": [{"name": "Default Title"}]}]
        variants = [{"optionValues": [{"optionName": "Title", "name": "Default Title"}], "price": f"{prod['variants'][0]['price_usd']:.2f}",
                     "sku": prod["variants"][0].get("sku"), "inventoryPolicy": "CONTINUE",
                     "inventoryItem": {"cost": f"{prod['variants'][0]['landed_cost_usd']:.2f}", "tracked": False}}]
    else:
        options = [{"name": n, "values": [{"name": val} for val in dict.fromkeys(v["options"][n] for v in prod["variants"] if n in v["options"])]}
                   for n in option_names]
        variants = [{"optionValues": [{"optionName": n, "name": v["options"][n]} for n in option_names if n in v["options"]],
                     "price": f"{v['price_usd']:.2f}", "sku": v.get("sku"), "inventoryPolicy": "CONTINUE",
                     "inventoryItem": {"cost": f"{v['landed_cost_usd']:.2f}", "tracked": False}} for v in prod["variants"]]
    tags = sorted(set(prod.get("tags", []) + extra_tags + ["wpc:" + plan["plan_id"]]))
    inp = {"title": prod["title"], "handle": prod["handle"], "descriptionHtml": prod.get("description_html", ""),
           "productType": prod.get("product_type", ""), "vendor": prod.get("vendor", ""), "tags": tags,
           "status": load_rules()["plan"]["new_product_initial_status"], "productOptions": options, "variants": variants,
           "files": [{"originalSource": u, "contentType": "IMAGE"} for u in prod.get("image_urls", [])],
           "collections": coll_ids}
    if prod.get("seo_title") or prod.get("seo_description"):
        inp["seo"] = {"title": prod.get("seo_title") or prod["title"], "description": prod.get("seo_description") or ""}
    data = sw.mutate("productSet", """mutation($input: ProductSetInput!) {
        productSet(input: $input, synchronous: true) { product { id handle variants(first: 50) { edges { node { id sku } } } } userErrors { field message code } } }""",
                     {"input": inp})
    product = data.get("product") or {"id": f"dry-run:{prod['handle']}", "handle": prod["handle"]}
    jlog(journal, item["item_id"], "AWAITING_SUPPLIER_LINK", new_product_id=product["id"], new_handle=product["handle"],
         supplier=prod["supplier"], variant_map=[{"variant_key": v["variant_key"], "supplier_variant_id": v.get("supplier_variant_id"),
                                                  "sku": v.get("sku")} for v in prod["variants"]])


def prune_variants(sw, item, journal) -> None:
    p = item["payload"]
    current = sw.read.execute("query($id: ID!) { product(id: $id) { status variants(first: 100) { edges { node { id } } } } }",
                              {"id": p["shopify_product_id"]})["product"]
    if not current:
        raise RuntimeError("product no longer exists")
    live = {e["node"]["id"] for e in current["variants"]["edges"]}
    remove = [v for v in p["remove_variant_ids"] if v in live]
    if not set(p["keep_variant_ids"]) & live:
        raise RuntimeError("none of the variants to keep still exist; refusing to prune")
    if remove:
        sw.mutate("productVariantsBulkDelete", """mutation($pid: ID!, $ids: [ID!]!) {
            productVariantsBulkDelete(productId: $pid, variantsIds: $ids) { product { id } userErrors { field message } } }""",
                  {"pid": p["shopify_product_id"], "ids": remove})
    jlog(journal, item["item_id"], "DONE", removed=remove, already_gone=sorted(set(p["remove_variant_ids"]) - set(remove)))


def reprice(sw, item, journal) -> None:
    p = item["payload"]
    current = sw.read.execute("query($id: ID!) { product(id: $id) { variants(first: 100) { edges { node { id price } } } } }",
                              {"id": p["shopify_product_id"]})["product"]
    live = {e["node"]["id"]: float(e["node"]["price"]) for e in current["variants"]["edges"]}
    updates, drift = [], []
    for v in p["variants"]:
        cur = live.get(v["shopify_variant_id"])
        if cur is None:
            drift.append(f"{v['shopify_variant_id']} missing")
        elif v["from_price_usd"] is not None and abs(cur - v["from_price_usd"]) > 0.005:
            drift.append(f"{v['shopify_variant_id']} price changed to {cur} since plan")
        else:
            updates.append({"id": v["shopify_variant_id"], "price": f"{v['to_price_usd']:.2f}"})
    if drift:
        raise RuntimeError("price drift, nothing changed: " + "; ".join(drift))
    sw.mutate("productVariantsBulkUpdate", """mutation($pid: ID!, $variants: [ProductVariantsBulkInput!]!) {
        productVariantsBulkUpdate(productId: $pid, variants: $variants) { product { id } userErrors { field message } } }""",
              {"pid": p["shopify_product_id"], "variants": updates})
    jlog(journal, item["item_id"], "DONE", updated=updates)


def write_work_orders(plan_path, plan, journal) -> Path:
    orders = []
    for item in plan["items"]:
        rec = journal["items"].get(item["item_id"], {})
        if rec.get("state") != "AWAITING_SUPPLIER_LINK":
            continue
        if item["type"] == "REROUTE_SUPPLIER":
            p = item["payload"]
            orders.append({"item_id": item["item_id"], "action": "REROUTE_SUPPLIER", "shopify_product_id": p["shopify_product_id"],
                           "from": p["from"], "to": p["to"], "variant_mapping": p.get("variant_mapping", [])})
        else:
            orders.append({"item_id": item["item_id"], "action": "LINK_NEW_PRODUCT", "shopify_product_id": rec.get("new_product_id"),
                           "supplier": rec.get("supplier"), "variant_mapping": rec.get("variant_map")})
    path = Path(plan_path).parent / f"supplier-work-orders-{plan['plan_id']}.json"
    write_json(path, {"plan_id": plan["plan_id"], "plan_sha256": plan["integrity"]["sha256"], "generated_at": utc_now_iso(),
                      "instructions": ("For each order: link the Shopify product/variants to the named supplier listing in DSers "
                                       "(aliexpress.us / dsers), CJ (cjdropshipping.us) or AutoDS (last resort). Verify landed cost "
                                       "and delivery still match; if they broke 3x / <$10 / <=10 days, do NOT link - leave it. "
                                       "Then run `apply_approved_plan.py mark-linked --item <id> ...`."),
                      "orders": orders})
    return path


# ------------------------------------------------------- apply session (hook)

def _session_path() -> Path:
    from common import project_root
    return project_root() / ".wpc" / "apply-session.json"


def open_apply_session(plan: dict[str, Any], hours: int = 4) -> None:
    """Lets the PreToolUse guard allow DSers/AutoDS write tools for supplier linking.

    Written only after every Phase B gate passed; bound to the plan hash; expires.
    Each supplier write still triggers a Claude Code permission prompt (settings.json `ask`).
    """
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)
    path = _session_path()
    write_json(path, {"plan_id": plan["plan_id"], "plan_sha256": plan["integrity"]["sha256"],
                      "opened_at": utc_now_iso(), "expires_at": expires.replace(microsecond=0).isoformat().replace("+00:00", "Z")})
    os.chmod(path, 0o600)


def close_apply_session() -> None:
    try:
        _session_path().unlink()
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------ commands

def cmd_review(args) -> int:
    plan = load_plan(args.plan, args.confirm_hash)
    ids = {i["item_id"] for i in plan["items"]}
    decisions: dict[str, str] = {}
    if args.from_markdown:
        for line in Path(args.from_markdown).read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*-\s*\[([ xX\-rR])\]\s*(ITEM-\d{3,4})\b", line)
            if m and m.group(2) in ids:
                mark = m.group(1).lower()
                decisions[m.group(2)] = "APPROVED" if mark == "x" else ("REJECTED" if mark in ("-", "r") else "PENDING")
    if args.approve:
        for i in (ids if args.approve.strip().lower() == "all" else {x.strip() for x in args.approve.split(",")}):
            if i not in ids:
                raise GateError(f"unknown item {i}")
            decisions[i] = "APPROVED"
    if args.reject:
        for i in {x.strip() for x in args.reject.split(",")}:
            if i not in ids:
                raise GateError(f"unknown item {i}")
            decisions[i] = "REJECTED"
    if not decisions:
        if not sys.stdin.isatty():
            raise GateError("no decisions given (use --approve/--reject/--from-markdown, or run in a terminal)")
        for item in plan["items"]:
            if not item["executable"]:
                continue
            ans = input(f"{item['item_id']} {item['type']}: {item['title']}\n  approve? [y/n/s=skip] ").strip().lower()
            if ans in ("y", "n"):
                decisions[item["item_id"]] = "APPROVED" if ans == "y" else "REJECTED"
    if not args.approved_by:
        raise GateError("--approved-by is required (who approved this plan)")
    now = utc_now_iso()
    for item in plan["items"]:
        d = decisions.get(item["item_id"])
        if d and d != "PENDING":
            if d == "APPROVED" and not item["executable"]:
                log.warning("%s is informational; ignoring approval", item["item_id"])
                continue
            item["approval"] = {"decision": d, "decided_by": args.approved_by, "decided_at": now, "note": args.note}
    # dependency check: an approved item cannot depend on a non-approved one
    by_id = {i["item_id"]: i for i in plan["items"]}
    for item in plan["items"]:
        if item["approval"]["decision"] == "APPROVED":
            for dep in item["depends_on"]:
                if by_id[dep]["approval"]["decision"] != "APPROVED":
                    raise GateError(f"{item['item_id']} depends on {dep}, which is not approved - approve both or neither")
    approved = [i["item_id"] for i in plan["items"] if i["approval"]["decision"] == "APPROVED"]
    rejected = [i["item_id"] for i in plan["items"] if i["approval"]["decision"] == "REJECTED"]
    executable = [i["item_id"] for i in plan["items"] if i["executable"]]
    status = ("APPROVED" if approved and set(approved) == set(executable) else
              "PARTIALLY_APPROVED" if approved else ("REJECTED" if rejected and len(rejected) == len(executable) else "PENDING_APPROVAL"))
    plan["status"] = status
    plan["approval"] = {"status": status, "approved_by": args.approved_by if approved else None,
                        "approved_at": now if approved else None, "confirm_hash_verified": True,
                        "approved_item_ids": approved, "rejected_item_ids": rejected}
    assert plan_hash(plan) == plan["integrity"]["sha256"]
    write_json(args.plan, plan)
    print(f"Plan status: {status}. Approved: {', '.join(approved) or 'none'}. Rejected: {', '.join(rejected) or 'none'}.")
    return 0


def cmd_verify(args) -> int:
    plan = load_plan(args.plan, args.confirm_hash)
    gate_store_and_age(plan)
    print(f"OK: {plan['plan_id']} hash verified, store {plan['store']['shop_domain']}, status {plan['status']}")
    for i in plan["items"]:
        print(f"  {i['item_id']} [{i['approval']['decision']:<8}] {i['type']:<28} {i['title']}")
    return 0


def _approved_in_order(plan):
    items = [i for i in plan["items"] if i["executable"] and i["approval"]["decision"] == "APPROVED"]
    return sorted(items, key=lambda i: (EXEC_ORDER.index(i["type"]), i["item_id"]))


def cmd_execute(args) -> int:
    plan = load_plan(args.plan, args.confirm_hash)
    gate_store_and_age(plan)
    gate_write_mode(plan, args.confirm_phrase, args.dry_run)
    journal = load_journal(args.plan, plan)
    sw = ShopifyWriter(args.dry_run)
    if not args.dry_run:
        plan["status"] = "APPLYING"
        write_json(args.plan, plan)
    failures = 0
    for item in _approved_in_order(plan):
        iid = item["item_id"]
        state = journal["items"].get(iid, {}).get("state")
        if state in ("DONE", "AWAITING_SUPPLIER_LINK", "SUPPLIER_LINKED"):
            log.info("%s already %s; skipping", iid, state)
            continue
        blocked = [d for d in item["depends_on"] if journal["items"].get(d, {}).get("state") != "DONE"]
        if blocked and not args.dry_run:
            jlog(journal, iid, "BLOCKED", error=f"dependencies not done: {blocked}")
            failures += 1
            continue
        try:
            if item["type"] == "CREATE_SUB_COLLECTION":
                create_sub_collection(sw, item, journal)
            elif item["type"] in ("REPLACE_PRODUCT", "ADD_PRODUCT"):
                create_draft_product(sw, item, journal, plan)
            elif item["type"] == "PRUNE_VARIANTS":
                prune_variants(sw, item, journal)
            elif item["type"] == "REPRICE_PRODUCT":
                reprice(sw, item, journal)
            elif item["type"] == "REROUTE_SUPPLIER":
                jlog(journal, iid, "AWAITING_SUPPLIER_LINK")
        except Exception as exc:  # keep going; every failure is journaled
            failures += 1
            jlog(journal, iid, "FAILED", error=str(exc))
            log.error("%s failed: %s", iid, exc)
        if not args.dry_run:
            save_journal(args.plan, plan, journal)
    if args.dry_run:
        print("Dry run complete; no changes were made and no journal was written.")
        return 0
    wo = write_work_orders(args.plan, plan, journal)
    save_journal(args.plan, plan, journal)
    open_apply_session(plan)
    print(f"Stage 1 done with {failures} failure(s). Supplier work orders: {wo}")
    print("Next: link suppliers per the work orders, `mark-linked` each item, then run `finalize`.")
    return 1 if failures else 0


def cmd_mark_linked(args) -> int:
    plan = load_plan(args.plan, args.confirm_hash)
    journal = load_journal(args.plan, plan)
    rec = journal["items"].get(args.item)
    if not rec or rec.get("state") != "AWAITING_SUPPLIER_LINK":
        raise GateError(f"{args.item} is not awaiting a supplier link (state: {(rec or {}).get('state')})")
    item = next(i for i in plan["items"] if i["item_id"] == args.item)
    expected = (item["payload"].get("to") or (item["payload"].get("replacement") or item["payload"].get("product") or {}).get("supplier") or {}).get("platform")
    if args.platform != expected:
        raise GateError(f"{args.item} was approved for platform {expected}, not {args.platform}")
    state = "DONE" if item["type"] == "REROUTE_SUPPLIER" else "SUPPLIER_LINKED"
    jlog(journal, args.item, state, supplier_link={"platform": args.platform, "reference": args.reference, "at": utc_now_iso()},
         note=args.reference)
    save_journal(args.plan, plan, journal)
    print(f"{args.item}: {state}")
    return 0


def cmd_finalize(args) -> int:
    plan = load_plan(args.plan, args.confirm_hash)
    gate_store_and_age(plan)
    gate_write_mode(plan, args.confirm_phrase, args.dry_run)
    journal = load_journal(args.plan, plan)
    sw = ShopifyWriter(args.dry_run)
    failures = 0
    for item in _approved_in_order(plan):
        if item["type"] not in ("REPLACE_PRODUCT", "ADD_PRODUCT"):
            continue
        rec = journal["items"].get(item["item_id"], {})
        if rec.get("state") != "SUPPLIER_LINKED":
            continue
        try:
            new_id = rec["new_product_id"]
            sw.mutate("productUpdate", """mutation($p: ProductUpdateInput!) { productUpdate(product: $p) { product { id status } userErrors { field message } } }""",
                      {"p": {"id": new_id, "status": "ACTIVE"}})
            if not args.dry_run:
                sw.publish(new_id)
            extra = {}
            if item["type"] == "REPLACE_PRODUCT":
                p = item["payload"]
                old = sw.read.execute("query($id: ID!) { product(id: $id) { id handle status } }", {"id": p["failing_shopify_product_id"]})["product"]
                if old and old["status"] == "ACTIVE":
                    sw.mutate("productUpdate", """mutation($p: ProductUpdateInput!) { productUpdate(product: $p) { product { id status } userErrors { field message } } }""",
                              {"p": {"id": old["id"], "status": "ARCHIVED"}})
                    sw.mutate("urlRedirectCreate", """mutation($r: UrlRedirectInput!) { urlRedirectCreate(urlRedirect: $r) { urlRedirect { id } userErrors { field message } } }""",
                              {"r": {"path": f"/products/{old['handle']}", "target": f"/products/{rec['new_handle']}"}})
                    extra = {"archived_product_id": old["id"], "redirect": f"/products/{old['handle']} -> /products/{rec['new_handle']}"}
                else:
                    extra = {"note": f"failing product already {old['status'] if old else 'missing'}; not touched"}
            if not args.dry_run:
                jlog(journal, item["item_id"], "DONE", activated=new_id, **extra)
        except Exception as exc:
            failures += 1
            if not args.dry_run:
                jlog(journal, item["item_id"], "FAILED", error=str(exc))
            log.error("%s failed: %s", item["item_id"], exc)
        if not args.dry_run:
            save_journal(args.plan, plan, journal)
    if args.dry_run:
        print("Dry run complete; no changes were made.")
        return 0
    approved = [i["item_id"] for i in _approved_in_order(plan)]
    done = [i for i in approved if journal["items"].get(i, {}).get("state") == "DONE"]
    plan["status"] = "APPLIED" if approved and len(done) == len(approved) else "PARTIALLY_APPLIED"
    write_json(args.plan, plan)
    if plan["status"] == "APPLIED":
        close_apply_session()
    print(f"Finalize done ({failures} failure(s)). Plan status: {plan['status']} ({len(done)}/{len(approved)} approved items done).")
    return 1 if failures else 0


def cmd_status(args) -> int:
    plan = read_json(args.plan)
    jp = journal_path(args.plan, plan)
    journal = read_json(jp) if jp.exists() else {"items": {}}
    print(f"{plan['plan_id']} status={plan['status']} sha256={plan['integrity']['sha256']}")
    for i in plan["items"]:
        st = journal["items"].get(i["item_id"], {})
        print(f"  {i['item_id']} [{i['approval']['decision']:<8}] {st.get('state', '-'):<24} {i['type']:<28} {st.get('error', '')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def base(p, need_hash=True):
        p.add_argument("--plan", required=True)
        if need_hash:
            p.add_argument("--confirm-hash", required=True)
        return p

    p = base(sub.add_parser("review"))
    p.add_argument("--approve"); p.add_argument("--reject"); p.add_argument("--from-markdown")
    p.add_argument("--approved-by"); p.add_argument("--note")
    base(sub.add_parser("verify"))
    for name in ("execute", "finalize"):
        p = base(sub.add_parser(name))
        p.add_argument("--confirm-phrase")
        p.add_argument("--dry-run", action="store_true")
    p = base(sub.add_parser("mark-linked"))
    p.add_argument("--item", required=True)
    p.add_argument("--platform", required=True, choices=["aliexpress.us", "cjdropshipping.us", "dsers", "autods"])
    p.add_argument("--reference", required=True, help="DSers mapping id / CJ product id / AutoDS product id")
    base(sub.add_parser("status"), need_hash=False)
    args = ap.parse_args()
    load_env()
    return {"review": cmd_review, "verify": cmd_verify, "execute": cmd_execute, "mark-linked": cmd_mark_linked,
            "finalize": cmd_finalize, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
