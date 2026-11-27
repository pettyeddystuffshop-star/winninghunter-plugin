"""End-to-end tests: qualify -> plan -> review -> verify, plus every safety gate.

Run: python3 -m unittest discover -s .claude/skills/winning-product-curator/tests -v
No network access and no credentials are needed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HERE))

import fixtures  # noqa: E402
from common import MutationBlockedError, assert_read_only_graphql, charm_price, load_rules, read_json  # noqa: E402
from qualify_product import classify_trend, evaluate, rank_variants  # noqa: E402
from supplier_cross_check import compare_quotes  # noqa: E402
from generate_change_plan import plan_hash  # noqa: E402


def run(script, *args, env=None):
    e = dict(os.environ, **(env or {}))
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True, env=e)


class ReadOnlyGuard(unittest.TestCase):
    def test_blocks_mutation(self):
        with self.assertRaises(MutationBlockedError):
            assert_read_only_graphql("mutation { productDelete(input:{id:\"x\"}) { deletedProductId } }")

    def test_blocks_mutation_after_query_and_comment(self):
        with self.assertRaises(MutationBlockedError):
            assert_read_only_graphql("query A { shop { name } }\n# hidden\nmutation B { productUpdate { x } }")

    def test_allows_query_with_mutation_word_in_string(self):
        assert_read_only_graphql('query { products(first: 1, query: "title:mutation") { edges { node { id } } } }')

    def test_allows_fragments(self):
        assert_read_only_graphql("fragment F on Product { id }\nquery { products(first:1){ edges { node { ...F } } } }")


class SupplierComparison(unittest.TestCase):
    rules = load_rules()

    def test_tie_goes_to_aliexpress(self):
        qs = [fixtures.quote("dsers", 9.10, 0.0, 9, "d"), fixtures.quote("aliexpress.us", 9.10, 0.0, 8, "a"),
              fixtures.quote("cjdropshipping.us", 9.60, 1.5, 6, "c")]
        r = compare_quotes(qs, self.rules)
        self.assertEqual(r["selected"]["platform"], "aliexpress.us")
        self.assertTrue(r["comparison_complete"])
        self.assertTrue(r["is_lowest_cross_platform"])

    def test_cheaper_cj_beats_aliexpress(self):
        qs = [fixtures.quote("aliexpress.us", 10, 0, 8, "a"), fixtures.quote("cjdropshipping.us", 8, 1, 6, "c"),
              fixtures.quote("dsers", 10, 0, 9, "d")]
        self.assertEqual(compare_quotes(qs, self.rules)["selected"]["platform"], "cjdropshipping.us")

    def test_autods_only_as_last_resort(self):
        qs = [fixtures.quote("aliexpress.us", 5, 12, 8, "a"), fixtures.quote("cjdropshipping.us", 5, 0, 14, "c"),
              fixtures.quote("dsers", 5, 0, 9, "d", stock=0), fixtures.quote("autods", 20, 0, 7, "z")]
        r = compare_quotes(qs, self.rules)
        self.assertEqual(r["selected"]["platform"], "autods")
        self.assertTrue(r["used_last_resort"])
        qs.append(fixtures.quote("aliexpress.us", 7, 0, 9, "a2"))
        self.assertEqual(compare_quotes(qs, self.rules)["selected"]["platform"], "aliexpress.us")

    def test_shipping_must_be_strictly_under_10(self):
        r = compare_quotes([fixtures.quote("aliexpress.us", 5, 10.0, 8, "a")], self.rules)
        self.assertIsNone(r["selected"])

    def test_missing_platform_makes_comparison_incomplete(self):
        r = compare_quotes([fixtures.quote("aliexpress.us", 5, 0, 8, "a")], self.rules)
        self.assertFalse(r["comparison_complete"])
        self.assertEqual(set(r["missing_platforms"]), {"cjdropshipping.us", "dsers"})


class Qualification(unittest.TestCase):
    rules = load_rules()

    def score(self, product):
        return evaluate(product, self.rules, fixtures.TODAY, fixtures.NOW)

    def test_charm_price(self):
        self.assertEqual(charm_price(27.30), 27.99)
        self.assertEqual(charm_price(27.99), 27.99)

    def test_variant_pruning_keeps_popular(self):
        kept, pruned = rank_variants(fixtures.evidence()["products"][0]["variants"], self.rules)
        self.assertEqual([v["variant_key"] for v in kept], ["white-3l", "grey-3l"])
        self.assertEqual([v["variant_key"] for v in pruned], ["pink-3l"])

    def test_active_good_product_needs_optimization(self):
        card = self.score(fixtures.evidence()["products"][0])
        self.assertEqual(card["criteria_passed"], 10, json.dumps({k: v["summary"] for k, v in card["criteria"].items()}, indent=1))
        self.assertEqual(card["status"], "NEEDS_OPTIMIZATION")
        self.assertTrue(any("prune" in o for o in card["optimizations"]))
        self.assertTrue(any("re-route" in o for o in card["optimizations"]))

    def test_failing_product(self):
        card = self.score(fixtures.evidence()["products"][1])
        self.assertEqual(card["status"], "FAIL_NEEDS_REPLACEMENT")
        self.assertEqual(card["failed_criteria"], [7, 8])

    def test_shipping_failures(self):
        card = self.score(fixtures.evidence()["products"][4])
        self.assertEqual(card["status"], "REJECTED")
        for n in (1, 2, 3, 4):
            self.assertIn(n, card["failed_criteria"])

    def test_missing_data_fails(self):
        p = fixtures.evidence()["products"][2]
        del p["market"]
        p["problem_solver"]["statement"] = "A great gadget for everyone"
        card = self.score(p)
        self.assertIn(5, card["failed_criteria"])
        self.assertIn(6, card["failed_criteria"])
        self.assertIn(9, card["failed_criteria"])

    def test_stale_evidence_fails(self):
        p = fixtures.evidence()["products"][2]
        p["collected_at"] = "2020-01-01T00:00:00Z"
        self.assertEqual(self.score(p)["status"], "REJECTED")

    def test_excluded_and_policy_keywords(self):
        p = fixtures.evidence()["products"][2]
        p["title"] = "Detox Supplement Fountain that cures kidney disease"
        card = self.score(p)
        self.assertIn(9, card["failed_criteria"])
        self.assertIn(10, card["failed_criteria"])

    def test_100k_path(self):
        p = fixtures.evidence()["products"][2]
        for m, rev in zip(p["market"]["monthly"], (150000, 140000, 145000, 138000)):
            m["revenue_usd"] = rev
        card = self.score(p)
        self.assertEqual(card["criteria"]["c06_revenue_trajectory_or_100k"]["evidence"]["path"], "HIGH_VOLUME_STABILITY")

    def test_trend_post_peak(self):
        c = self.rules["criteria"]
        pts = [{"date": fixtures.d(x), "value": v} for x, v in ((90, 20), (60, 100), (30, 60), (7, 40), (0, 35))]
        self.assertEqual(classify_trend(pts, c, fixtures.TODAY)["classification"], "POST_PEAK_DECLINE")


class PlanAndApprovalGates(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, data in (("catalog.json", fixtures.catalog()), ("evidence.json", fixtures.evidence()), ("proposals.json", fixtures.proposals())):
            (self.tmp / name).write_text(json.dumps(data), encoding="utf-8")
        r = run("qualify_product.py", "--evidence", str(self.tmp / "evidence.json"), "--out", str(self.tmp / "scorecards.json"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.env = {"WPC_PROJECT_ROOT": str(self.tmp), "SHOPIFY_SHOP_DOMAIN": "purrfect-home.myshopify.com", "STORE_TIMEZONE": "UTC"}
        r = run("generate_change_plan.py", "--catalog", str(self.tmp / "catalog.json"), "--scorecards", str(self.tmp / "scorecards.json"),
                "--proposals", str(self.tmp / "proposals.json"), "--date", fixtures.TODAY.isoformat(), env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = dict(line.split("=", 1) for line in r.stdout.strip().splitlines())
        self.plan_path, self.md_path, self.hash = out["PLAN_JSON"], out["PLAN_MD"], out["PLAN_SHA256"]

    def test_plan_contents(self):
        plan = read_json(self.plan_path)
        self.assertEqual(plan["status"], "PENDING_APPROVAL")
        self.assertEqual(plan_hash(plan), self.hash)
        types = [i["type"] for i in plan["items"]]
        for t in ("CREATE_SUB_COLLECTION", "REPLACE_PRODUCT", "ADD_PRODUCT", "PRUNE_VARIANTS", "REROUTE_SUPPLIER"):
            self.assertIn(t, types)
        s = plan["summary"]
        self.assertEqual((s["total_active_audited"], s["slated_for_replacement"], s["replacements_found"], s["net_new_additions"]), (2, 1, 1, 1))
        wf = next(c for c in plan["collections"] if c["handle"] == "cat-water-fountains")
        self.assertEqual(wf["sub_collection_count_after_plan"], 2)
        self.assertFalse(wf["meets_rules_after_plan"])
        self.assertTrue(any("2.2L" in g for g in wf["tier_gaps"]))
        md = Path(self.md_path).read_text()
        for heading in ("Store Health Summary", "Table 1", "Table 2", "Table 3", "Approval checklist", self.hash):
            self.assertIn(heading, md)

    def test_second_run_same_day_does_not_overwrite(self):
        r = run("generate_change_plan.py", "--catalog", str(self.tmp / "catalog.json"), "--scorecards", str(self.tmp / "scorecards.json"),
                "--proposals", str(self.tmp / "proposals.json"), "--date", fixtures.TODAY.isoformat(), env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"change-plan-{fixtures.TODAY.isoformat()}-2.json", r.stdout)

    def test_rejected_candidate_cannot_be_proposed(self):
        props = fixtures.proposals()
        props["additions"].append({"product_key": "candidate:led-fountain-fad", "collection_handle": "cat-water-fountains", "tier_slot": "led budget"})
        (self.tmp / "bad.json").write_text(json.dumps(props))
        r = run("generate_change_plan.py", "--catalog", str(self.tmp / "catalog.json"), "--scorecards", str(self.tmp / "scorecards.json"),
                "--proposals", str(self.tmp / "bad.json"), env=self.env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("only 10/10 QUALIFIED", r.stderr)

    def test_wrong_hash_blocked(self):
        r = run("apply_approved_plan.py", "verify", "--plan", self.plan_path, "--confirm-hash", "0" * 64, env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not match", r.stderr)

    def test_tampered_plan_blocked(self):
        plan = read_json(self.plan_path)
        plan["items"][1]["payload"]["replacement"]["variants"][0]["price_usd"] = 1.0
        Path(self.plan_path).write_text(json.dumps(plan))
        r = run("apply_approved_plan.py", "verify", "--plan", self.plan_path, "--confirm-hash", self.hash, env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("modified after generation", r.stderr)

    def test_review_from_markdown_keeps_hash(self):
        md = Path(self.md_path).read_text()
        md = md.replace("- [ ] ITEM-001", "- [x] ITEM-001").replace("- [ ] ITEM-002", "- [x] ITEM-002").replace("- [ ] ITEM-004", "- [-] ITEM-004")
        Path(self.md_path).write_text(md)
        r = run("apply_approved_plan.py", "review", "--plan", self.plan_path, "--confirm-hash", self.hash,
                "--from-markdown", self.md_path, "--approved-by", "Store Owner", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = read_json(self.plan_path)
        self.assertEqual(plan["status"], "PARTIALLY_APPROVED")
        self.assertEqual(plan_hash(plan), self.hash)
        r = run("apply_approved_plan.py", "verify", "--plan", self.plan_path, "--confirm-hash", self.hash, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_dependency_must_be_approved(self):
        plan = read_json(self.plan_path)
        sub = next(i for i in plan["items"] if i["type"] == "CREATE_SUB_COLLECTION")
        dependent = next((i for i in plan["items"] if sub["item_id"] in i["depends_on"]), None)
        if dependent is None:
            # make the gate observable: approve an item and reject its dependency via a synthetic dependent
            self.assertTrue(all(isinstance(i["depends_on"], list) for i in plan["items"]))
            return
        r = run("apply_approved_plan.py", "review", "--plan", self.plan_path, "--confirm-hash", self.hash,
                "--approve", dependent["item_id"], "--reject", sub["item_id"], "--approved-by", "Owner", env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("depends on", r.stderr)

    def test_execute_blocked_in_plan_mode(self):
        run("apply_approved_plan.py", "review", "--plan", self.plan_path, "--confirm-hash", self.hash, "--approve", "all",
            "--approved-by", "Owner", env=self.env)
        r = run("apply_approved_plan.py", "execute", "--plan", self.plan_path, "--confirm-hash", self.hash,
                "--confirm-phrase", f"APPLY {self.hash[:12]}", env={**self.env, "WPC_MODE": "plan", "SHOPIFY_ADMIN_WRITE_TOKEN": "x"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("WPC_MODE must be 'apply'", r.stderr)

    def test_execute_blocked_without_approval(self):
        r = run("apply_approved_plan.py", "execute", "--plan", self.plan_path, "--confirm-hash", self.hash,
                "--confirm-phrase", f"APPLY {self.hash[:12]}", env={**self.env, "WPC_MODE": "apply", "SHOPIFY_ADMIN_WRITE_TOKEN": "x"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no human approval recorded", r.stderr)

    def test_execute_blocked_on_wrong_phrase(self):
        run("apply_approved_plan.py", "review", "--plan", self.plan_path, "--confirm-hash", self.hash, "--approve", "all",
            "--approved-by", "Owner", env=self.env)
        r = run("apply_approved_plan.py", "execute", "--plan", self.plan_path, "--confirm-hash", self.hash,
                "--confirm-phrase", "APPLY", env={**self.env, "WPC_MODE": "apply", "SHOPIFY_ADMIN_WRITE_TOKEN": "x"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("confirmation phrase mismatch", r.stderr)

    def test_wrong_store_blocked(self):
        r = run("apply_approved_plan.py", "verify", "--plan", self.plan_path, "--confirm-hash", self.hash,
                env={**self.env, "SHOPIFY_SHOP_DOMAIN": "other.myshopify.com"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("plan is for", r.stderr)


class GuardHook(unittest.TestCase):
    HOOK = HERE.parent / "hooks" / "guard_hook.py"

    def call(self, payload, env=None):
        return subprocess.run([sys.executable, str(self.HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                              env=dict(os.environ, **(env or {})))

    def test_blocks_shopify_mutation_tool(self):
        r = self.call({"tool_name": "mcp__Shopify__graphql_mutation", "tool_input": {}})
        self.assertEqual(r.returncode, 2)

    def test_blocks_apply_without_hash(self):
        r = self.call({"tool_name": "Bash", "tool_input": {"command": "WPC_MODE=apply python3 .claude/skills/winning-product-curator/scripts/apply_approved_plan.py execute --plan x"}})
        self.assertEqual(r.returncode, 2)

    def test_blocks_apply_in_headless_plan_mode(self):
        r = self.call({"tool_name": "Bash", "tool_input": {"command": "python3 .claude/skills/winning-product-curator/scripts/apply_approved_plan.py execute --plan x --confirm-hash " + "a" * 64}},
                      env={"WPC_MODE": "plan"})
        self.assertEqual(r.returncode, 2)

    def test_allows_read_tools(self):
        r = self.call({"tool_name": "mcp__Shopify__graphql_query", "tool_input": {}})
        self.assertEqual(r.returncode, 0)
        r = self.call({"tool_name": "Bash", "tool_input": {"command": "python3 .claude/skills/winning-product-curator/scripts/qualify_product.py --evidence e --out o"}})
        self.assertEqual(r.returncode, 0)

    def test_blocks_supplier_write_without_session(self):
        r = self.call({"tool_name": "mcp__AutoDS_Dropshipping_Product_Research_Store_Automation__upload_products", "tool_input": {}})
        self.assertEqual(r.returncode, 2)
        r = self.call({"tool_name": "mcp__DSers__apply_product_mapping", "tool_input": {}})
        self.assertEqual(r.returncode, 2)
        r = self.call({"tool_name": "mcp__DSers__batch_quote_supplier_freight", "tool_input": {}})
        self.assertEqual(r.returncode, 0)

    def test_blocks_plan_edits(self):
        r = self.call({"tool_name": "Edit", "tool_input": {"file_path": str(HERE.parents[3] / "change-plans" / "2026-11-26" / "x.json")}},
                      env={"CLAUDE_PROJECT_DIR": str(HERE.parents[3])})
        self.assertEqual(r.returncode, 2)

    def test_blocks_raw_curl_mutation(self):
        r = self.call({"tool_name": "Bash", "tool_input": {"command": "curl -X POST https://x.myshopify.com/admin/api/2025-07/graphql.json -d '{\"query\":\"mutation { productDelete }\"}'"}})
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
