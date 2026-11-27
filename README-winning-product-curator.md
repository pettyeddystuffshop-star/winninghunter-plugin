# winning-product-curator

A Claude Code skill that audits a Shopify dropshipping store every Thursday at 04:00. It scores every active product against a strict 10-point winning-product gate, sources 1-for-1 replacements, and fills each collection to at least 4 sub-collections and at least 20 products with tiered assortments. The result is a **SHA-256-signed Change Plan**. Nothing in your store changes until you approve the plan, item by item.

```
Thursday 04:00 (store TZ)                                  You, any time after
┌──────────────────────── Phase A: read-only ───────────────────────┐   ┌──────── Phase B: gated ─────────┐
shopify_readonly_audit ─► evidence (WinningHunter MCP+REST, DSers,  │   │ review (tick items) ► verify    │
                          CJ, AliExpress, AutoDS) ─► qualify_product │   │ ► execute (DRAFTs, prune, price)│
                          ─► proposals ─► generate_change_plan ──────┼──►│ ► link suppliers ► mark-linked  │
                          change-plans/DATE/*.md|json|sha256 + notify│   │ ► finalize (activate, archive   │
└────────────────────────────────────────────────────────────────────┘   │   old + redirect)               │
                                                                          └─────────────────────────────────┘
```

## Files

| Path | Purpose |
|---|---|
| `.claude/skills/winning-product-curator/SKILL.md` | Agent instructions: workflow, MCP tool rules, 10-point rubric, tiering and variant rules, guardrails |
| `…/config/rules.json` | Every threshold (3×, < $10, ≤ 10 days, 1,000 units, $100k, 4.5★, 20 products, 4 sub-collections, variant shares) |
| `…/schemas/change_plan.schema.json` | Strict Change Plan schema (10 criteria, 3-platform price comparison, supplier justification, counts, tiers, approval state) |
| `…/schemas/product_evidence.schema.json`, `proposals.schema.json` | Inputs Claude produces, validated before use |
| `…/scripts/shopify_readonly_audit.py` | Read-only GraphQL fetch of products, variants, collections, menus, metafields, 30/90-day sales; derives the sub-collection hierarchy |
| `…/scripts/winninghunter_client.py` | WinningHunter REST (store explorer, ad library, TikTok Shop, trends), self-limited to 55 requests/min, `market-snapshot` builds evidence |
| `…/scripts/supplier_cross_check.py` | Landed-cost / < $10 / ≤ 10-day comparator (aliexpress.us → cjdropshipping.us → DSers, AutoDS last resort); CJ API 2.0 and AliExpress DS API fetchers |
| `…/scripts/qualify_product.py` | Deterministic 10/10 validator and popular-variant filter |
| `…/scripts/generate_change_plan.py` | Builds the Markdown and SHA-256-signed JSON plan; refuses non-10/10 proposals |
| `…/scripts/apply_approved_plan.py` | Gated Phase B executor (`review`, `verify`, `execute`, `mark-linked`, `finalize`, `status`) |
| `…/scripts/install_thursday_4am_cron.sh`, `run_weekly_plan.sh`, `notify.py` | Scheduler installer, headless runner, notifications |
| `…/hooks/guard_hook.py` | PreToolUse hook that blocks store and supplier writes outside the gated path |
| `…/tests/` | 37 offline tests (pipeline and every safety gate); `examples/` holds complete sample inputs |
| `.claude/settings.json` | Registers the hook; `ask` / `deny` permission rules |
| `.mcp.json.example`, `.env.example`, `.env.apply.example` | Configuration templates |

## 1. Setup

Requirements: Python 3.10+, Claude Code CLI (`claude`), Node (only for the optional Shopify dev MCP).

```bash
pip install -r .claude/skills/winning-product-curator/requirements.txt
cp .env.example .env              && chmod 600 .env
cp .env.apply.example .env.apply  && chmod 600 .env.apply     # Phase B only
cp .mcp.json.example .mcp.json
```

### Credentials

1. **Shopify, two custom apps.** Create them under Settings → Apps and sales channels → Develop apps.
   - `curator-readonly`: `read_products`, `read_inventory`, `read_orders`, `read_online_store_navigation` (add `read_all_orders` for history older than 60 days). Its token goes in `.env` as `SHOPIFY_ADMIN_READ_TOKEN`.
   - `curator-apply`: `write_products`, `write_inventory`, `write_online_store_navigation`, `read_publications`, `write_publications`. Its token goes in `.env.apply` only.
2. **WinningHunter.** Create a key at app.winninghunter.com/api and set `WH_API_KEY`. The same key authenticates the MCP server (`X-API-Key` header in `.mcp.json`) and REST. You need the Basic plan or higher for the API, and Standard for Meta ad-library data. Limits are 60 REST and 300 MCP calls per minute.
3. **CJdropshipping.** Generate an API key in the CJ dashboard and set `CJ_API_KEY`. The access token is cached in `.wpc/cj_token.json` (mode 0600).
4. **DSers / AutoDS.** Paste each account's MCP server URL into `DSERS_MCP_URL` / `AUTODS_MCP_URL`, then run `/mcp` in Claude Code to finish OAuth. If you connect them as claude.ai connectors instead, delete those entries from `.mcp.json`.
5. **AliExpress DS API (optional).** Set the `ALIEXPRESS_*` keys. Without them, `aliexpress.us` quotes come from DSers freight quotes, which is the normal path.
6. **Notifications.** Set `NOTIFY_CHANNELS` (`slack`, `ntfy`, `email`, `desktop`) and the matching variables.

Load the variables into your shell before launching Claude Code, so `.mcp.json` can expand `${WH_API_KEY}` and the MCP URLs:

```bash
set -a; source .env; set +a; claude
```

> The weekly runner, headless allow-list and hook assume the MCP server names `winninghunter`, `dsers` and `autods`, as in `.mcp.json.example`. If you rename them, update `scripts/run_weekly_plan.sh`.

## 2. Dry-run testing

```bash
# offline test suite (no credentials needed)
python3 -m unittest discover -s .claude/skills/winning-product-curator/tests -v

# end-to-end on the bundled sample data, without touching any API
python3 .claude/skills/winning-product-curator/scripts/qualify_product.py \
  --evidence .claude/skills/winning-product-curator/examples/evidence.example.json --out /tmp/scorecards.json
WPC_PROJECT_ROOT=/tmp/wpc-demo python3 .claude/skills/winning-product-curator/scripts/generate_change_plan.py \
  --catalog .claude/skills/winning-product-curator/examples/catalog.example.json \
  --scorecards /tmp/scorecards.json \
  --proposals .claude/skills/winning-product-curator/examples/proposals.example.json

# live read-only checks
python3 .claude/skills/winning-product-curator/scripts/winninghunter_client.py credits
python3 .claude/skills/winning-product-curator/scripts/shopify_readonly_audit.py --out work/test/catalog.json

# scheduler dry run: checks config and prints the exact headless command
bash .claude/skills/winning-product-curator/scripts/install_thursday_4am_cron.sh --test
```

Interactive run: `/winning-product-curator` (the same as `--mode=plan`).

## 3. Weekly Thursday 04:00 schedule

```bash
bash .claude/skills/winning-product-curator/scripts/install_thursday_4am_cron.sh            # install (idempotent)
bash .claude/skills/winning-product-curator/scripts/install_thursday_4am_cron.sh --print    # preview
bash .claude/skills/winning-product-curator/scripts/install_thursday_4am_cron.sh --run-now  # run Phase A now
bash .claude/skills/winning-product-curator/scripts/install_thursday_4am_cron.sh --uninstall
bash .claude/skills/winning-product-curator/scripts/install_thursday_4am_cron.sh --github-actions  # optional cloud runner
```

- **Linux / WSL:** installs a `crontab` line `0 4 * * 4 …` tagged `# winning-product-curator weekly plan`. Your other entries are preserved, and re-running the installer replaces the line rather than adding a second one. On WSL, cron only runs while WSL is up and `sudo service cron start` has been run.
- **macOS:** installs `~/Library/LaunchAgents/com.winning-product-curator.weekly.plist` (Weekday 4, Hour 4). launchd catches up a missed run when the Mac wakes.
- **Machine clock zone differs from `STORE_TIMEZONE`:** the installer schedules an hourly trigger on Wednesday to Friday instead. `run_weekly_plan.sh --slot-guard` then fires once, when it is Thursday 04:xx in the store's zone, including across DST changes.
- **GitHub Actions:** writes a workflow covering both DST-dependent UTC hours, slot-guarded. It uses read-only secrets only, and uploads the plan and log as an artifact.
- Claude Code's `/loop` timers are not used, because they die when the terminal closes and expire after 3 days.

What the runner does: it loads `.env` (never `.env.apply`), forces `WPC_MODE=plan` and `WPC_HEADLESS=1`, and runs `claude -p "/winning-product-curator --mode=plan"` with an explicit read-only `--allowedTools` list and a `--disallowedTools` list of every write tool. It logs to `logs/weekly-audit-YYYY-MM-DD.log`, uses a lock to prevent overlapping runs, then sends a notification with the summary, the plan path and the exact approval command.

## 4. Approving and applying a Change Plan

1. Open `change-plans/YYYY-MM-DD/change-plan-YYYY-MM-DD.md`. It contains:
   - the Store Health Summary;
   - **Table 1**: failing products and their 1-for-1 replacements, with prices on aliexpress.us, CJ and DSers, landed cost, shipping days, the ≥ 3× retail price, 30-day units, revenue path and trend, verified rating, pain point and hook, and the kept variants;
   - **Table 2**: collection and sub-collection gaps, with tier matrix and a full scorecard for each addition;
   - **Table 3**: variant pruning, supplier re-routing and repricing.
2. In the **Approval checklist**, mark each item `[x]` to approve or `[-]` to reject. Unmarked items stay pending and are never executed.
3. In Claude Code run:
   ```
   /winning-product-curator --apply ./change-plans/YYYY-MM-DD/change-plan-YYYY-MM-DD.json --confirm-hash <sha256 from the plan>
   ```
   Claude verifies the hash, syncs your ticks (`review --from-markdown`) and shows a dry run. It then asks you to type `APPLY <first 12 hash characters>`. Every write command also triggers a Claude Code permission prompt.
4. **Stage 1** (`execute`):
   - creates approved sub-collections (metafield link, publication, nested menu item);
   - creates new products as **DRAFT** with only the popular variants, ≥ 3× prices and the landed cost recorded;
   - prunes variants and reprices;
   - writes `supplier-work-orders-<plan>.json`.
5. **Supplier linking:** Claude links each new product in DSers (aliexpress.us), CJ, or AutoDS (last resort only). It re-checks cost, shipping and delivery first, and refuses if any rule is now broken. Each link is recorded with `mark-linked`.
6. **Stage 2** (`finalize`, same confirmation phrase): activates and publishes each new product, then **archives** the product it replaces and creates a `/products/old → /products/new` redirect. Order history is kept. Nothing is ever hard-deleted. If you really want something deleted, do it yourself in Shopify admin.

Manual equivalents:

```bash
P=./change-plans/2026-12-03/change-plan-2026-12-03.json; H=<sha256>
S=.claude/skills/winning-product-curator/scripts
python3 $S/apply_approved_plan.py review  --plan $P --confirm-hash $H --from-markdown ${P%.json}.md --approved-by "Owner"
python3 $S/apply_approved_plan.py verify  --plan $P --confirm-hash $H
WPC_MODE=apply python3 $S/apply_approved_plan.py execute  --plan $P --confirm-hash $H --dry-run
WPC_MODE=apply python3 $S/apply_approved_plan.py execute  --plan $P --confirm-hash $H      # prompts for APPLY <hash12>
python3 $S/apply_approved_plan.py mark-linked --plan $P --confirm-hash $H --item ITEM-002 --platform aliexpress.us --reference "DSers mapping 123"
WPC_MODE=apply python3 $S/apply_approved_plan.py finalize --plan $P --confirm-hash $H
python3 $S/apply_approved_plan.py status  --plan $P
```

## 5. Safety model (defense in depth)

| Layer | What it stops |
|---|---|
| Separate read and write Shopify apps; write token only in `.env.apply` | Phase A cannot write, even through a bug: it never has a write-capable token |
| `assert_read_only_graphql` in every Phase A request | Any GraphQL `mutation` or `subscription`, including one hidden after comments |
| Headless runner: `WPC_MODE=plan`, `WPC_HEADLESS=1`, explicit `--allowedTools` / `--disallowedTools` | Writes during the scheduled run |
| `hooks/guard_hook.py` (PreToolUse) | Shopify MCP write tools; DSers/AutoDS writes outside an active apply session; apply commands without the full hash, or during headless runs; raw `curl` mutations; edits to `change-plans/**` |
| `.claude/settings.json` `ask` / `deny` | Human prompt for every apply or supplier-write call; never reads `.env`, `.env.apply` |
| Plan integrity | The SHA-256 covers everything except approval decisions. A tampered plan, wrong hash, wrong store or a plan older than 14 days is refused |
| Approval | Only items marked `APPROVED` run; dependencies must be approved together; the `APPLY <hash12>` phrase is required |
| Two-stage apply | New products go live and old ones are archived only after the supplier link is confirmed; price drift aborts a reprice |

Limits: the hook and permission rules are Claude Code safeguards. They are not a sandbox against someone with shell access to your machine. Running Claude Code with `--dangerously-skip-permissions` disables the `ask` prompts, but the hook, the separate token, hash, phrase and approval gates still apply.

## 6. Data-source notes and limitations

- **Product-level 30-day units and monthly revenue** come from WinningHunter TikTok Shop listings that you confirm are the same product. Monthly buckets come from differencing the 30/60/90/120-day windows. Add other listings (Store Explorer, AliExpress order counts) as extra sources in the evidence. Sources missing from the evidence count as unverified, which means FAIL.
- **AliExpress DS freight fees:** the fee field format varies between API versions. The script prefers `shipping_fee_format` and flags each quote to be cross-checked against the DSers quote.
- **Smart collections** can't take manual product adds. Phase B adds the collection's `TAG EQUALS` rule tags to new products instead, and logs a warning for any other rule type.
- **Menu nesting** needs the parent collection to already appear in `NAVIGATION_MENU_HANDLE`. If it doesn't, the sub-collection is still created and linked by metafield, and the journal notes that the menu was unchanged.
- Thresholds live in `config/rules.json`, and every plan stores a snapshot of them. The plan schema refuses rules looser than the specification (for example, a margin below 3×).
