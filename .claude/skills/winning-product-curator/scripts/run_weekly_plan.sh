#!/usr/bin/env bash
# Headless weekly runner: Thursday 04:00 (store-local) Phase A only.
#
# Invoked by crontab / launchd / CI (see install_thursday_4am_cron.sh).
#   run_weekly_plan.sh              run now
#   run_weekly_plan.sh --slot-guard run only if it is Thursday 04:xx in STORE_TIMEZONE and today's plan
#                                   has not run yet (used when the OS clock zone differs from the store's)
#   run_weekly_plan.sh --dry-run    check configuration and print the exact claude command; run nothing
#
# Safety: forces WPC_MODE=plan and WPC_HEADLESS=1, never loads .env.apply, unsets any write token,
# passes an explicit read-only --allowedTools list and a --disallowedTools list of every write tool.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
cd "$PROJECT_DIR"

MODE="run"
case "${1:-}" in
  --slot-guard) MODE="guard" ;;
  --dry-run) MODE="dry" ;;
  "") ;;
  *) echo "usage: $0 [--slot-guard|--dry-run]" >&2; exit 64 ;;
esac

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
export PATH="$HOME/.local/bin:$HOME/.claude/local:/usr/local/bin:/opt/homebrew/bin:$PATH"
export WPC_MODE=plan WPC_HEADLESS=1 WPC_PROJECT_ROOT="$PROJECT_DIR"
unset SHOPIFY_ADMIN_WRITE_TOKEN
STORE_TZ="${STORE_TIMEZONE:-UTC}"
PY="${PYTHON_BIN:-python3}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
DATE="$(TZ="$STORE_TZ" date +%F)"
mkdir -p logs .wpc "work/$DATE"
LOG="logs/weekly-audit-$DATE.log"
STAMP=".wpc/last-weekly-run"

if [[ "$MODE" == "guard" ]]; then
  if [[ "$(TZ="$STORE_TZ" date +%u)" != "4" || "$(TZ="$STORE_TZ" date +%H)" != "04" ]]; then
    exit 0
  fi
  if [[ -f "$STAMP" && "$(cat "$STAMP")" == "$DATE" ]]; then
    exit 0
  fi
fi

S="python3 .claude/skills/winning-product-curator/scripts"
ALLOWED=(
  "Read" "Glob" "Grep" "Write(work/**)" "Edit(work/**)" "Bash(mkdir -p work/*)" "Bash(date:*)" "Bash(TZ=* date:*)"
  "Bash($S/shopify_readonly_audit.py:*)" "Bash($S/winninghunter_client.py:*)" "Bash($S/supplier_cross_check.py:*)"
  "Bash($S/qualify_product.py:*)" "Bash($S/generate_change_plan.py:*)"
  "mcp__winninghunter__find_winning_products" "mcp__winninghunter__search_facebook_ads" "mcp__winninghunter__search_shopify_stores"
  "mcp__winninghunter__search_shops" "mcp__winninghunter__find_similar_shops" "mcp__winninghunter__find_similar_stores_by_image"
  "mcp__winninghunter__list_shopify_store_filter_options" "mcp__winninghunter__search_tiktok_products" "mcp__winninghunter__get_tiktok_product"
  "mcp__winninghunter__search_tiktok_shops" "mcp__winninghunter__get_tiktok_shop" "mcp__winninghunter__search_tiktok_videos"
  "mcp__winninghunter__get_tiktok_video" "mcp__winninghunter__get_tiktok_trending_products" "mcp__winninghunter__list_tiktok_category_top"
  "mcp__winninghunter__browse_tiktok_categories" "mcp__winninghunter__search_exploding_topics" "mcp__winninghunter__get_exploding_topic_detail"
  "mcp__winninghunter__autocomplete_exploding_topics" "mcp__winninghunter__creative_inspiration_pack" "mcp__winninghunter__scan_ad"
  "mcp__winninghunter__brief_competitor" "mcp__winninghunter__get_ad_transcript" "mcp__winninghunter__search_landers"
  "mcp__winninghunter__daily_radar" "mcp__winninghunter__check_credits"
  "mcp__dsers__list_stores" "mcp__dsers__search_supplier_products" "mcp__dsers__get_supplier_product"
  "mcp__dsers__get_supplier_product_with_freight" "mcp__dsers__batch_get_supplier_products" "mcp__dsers__batch_quote_supplier_freight"
  "mcp__dsers__list_supplier_product_shipping_methods_and_cost" "mcp__dsers__list_supplier_search_filters"
  "mcp__dsers__get_store_product_mapping" "mcp__dsers__list_managed_store_products" "mcp__dsers__get_managed_store_product"
  "mcp__dsers__search_store_products" "mcp__dsers__search_ai_mapping_candidates" "mcp__dsers__list_supplier_product_change_notifications"
  "mcp__dsers__search_tiktok_ads"
  "mcp__autods__list_stores_api" "mcp__autods__get_user_subscription" "mcp__autods__list_products" "mcp__autods__get_product_by_id"
  "mcp__autods__search_products" "mcp__autods__get_similar_products" "mcp__autods__get_winning_products"
  "mcp__autods__get_recommended_products" "mcp__autods__get_tiktok_products" "mcp__autods__get_tiktok_product_details"
  "mcp__autods__get_tiktok_product_overview" "mcp__autods__get_store_quote" "mcp__autods__list_store_quotes"
  "mcp__autods__list_store_quote_shipping_options" "mcp__autods__get_product_regions"
)
DISALLOWED=(
  "Bash($S/apply_approved_plan.py:*)" "Bash(WPC_MODE=apply:*)" "Bash(curl:*)" "Bash(wget:*)" "Write(change-plans/**)" "Edit(change-plans/**)"
  "mcp__shopify" "mcp__dsers__apply_product_mapping" "mcp__dsers__apply_ai_mapping" "mcp__dsers__publish_products_to_stores"
  "mcp__dsers__update_managed_store_product" "mcp__dsers__update_store_product_price_in_bulk" "mcp__dsers__import_store_products_to_dsers"
  "mcp__dsers__place_orders_to_suppliers" "mcp__dsers__update_pricing_rule" "mcp__autods__upload_products" "mcp__autods__delete_product"
  "mcp__autods__publish_drafts_to_marketplace" "mcp__autods__link_quoted_product" "mcp__autods__set_store_quote_shipping_option"
  "mcp__autods__request_manual_sourcing" "mcp__autods__create_1688_sourcing_request"
)
ALLOWED_CSV="$(IFS=,; echo "${ALLOWED[*]}")"
DISALLOWED_CSV="$(IFS=,; echo "${DISALLOWED[*]}")"
PROMPT="/winning-product-curator --mode=plan"

if [[ "$MODE" == "dry" ]]; then
  echo "Project:        $PROJECT_DIR"
  echo "Store TZ:       $STORE_TZ (now $(TZ="$STORE_TZ" date '+%a %F %H:%M'))"
  echo "Log file:       $LOG"
  printf 'claude binary:  '; command -v "$CLAUDE_BIN" || echo "NOT FOUND (set CLAUDE_BIN)"
  "$CLAUDE_BIN" --version 2>/dev/null || true
  for v in SHOPIFY_SHOP_DOMAIN SHOPIFY_ADMIN_READ_TOKEN WH_API_KEY STORE_TIMEZONE; do
    if [[ -n "${!v:-}" ]]; then echo "env $v: set"; else echo "env $v: MISSING"; fi
  done
  for v in CJ_API_KEY ALIEXPRESS_APP_KEY NOTIFY_CHANNELS; do
    if [[ -n "${!v:-}" ]]; then echo "env $v: set"; else echo "env $v: not set (optional)"; fi
  done
  [[ -f .env.apply ]] && echo ".env.apply: present (never loaded by this runner)"
  "$PY" -c "import jsonschema" 2>/dev/null && echo "jsonschema: OK" || echo "jsonschema: MISSING (pip install -r .claude/skills/winning-product-curator/requirements.txt)"
  echo
  echo "Would run:"
  printf '  %q -p %q --allowedTools %q --disallowedTools %q --output-format text\n' "$CLAUDE_BIN" "$PROMPT" "$ALLOWED_CSV" "$DISALLOWED_CSV"
  exit 0
fi

LOCKDIR=".wpc/weekly-run.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  echo "$(date -u +%FT%TZ) another weekly run is in progress; exiting" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT
echo "$DATE" > "$STAMP"

notify() { "$PY" .claude/skills/winning-product-curator/scripts/notify.py --title "$1" --message "$2" --level "$3" >> "$LOG" 2>&1 || true; }

{
  echo "=== winning-product-curator weekly plan run $(date -u +%FT%TZ) (store date $DATE, TZ $STORE_TZ) ==="
  echo "mode=plan headless=1 claude=$(command -v "$CLAUDE_BIN" || echo missing)"
} >> "$LOG"

if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  echo "claude CLI not found" >> "$LOG"
  notify "Weekly Change Plan FAILED" "claude CLI not found on PATH for the scheduler. See $PROJECT_DIR/$LOG" error
  exit 1
fi

TIMEOUT_CMD=()
if command -v timeout >/dev/null 2>&1; then TIMEOUT_CMD=(timeout "${WPC_RUN_TIMEOUT:-14400}");
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT_CMD=(gtimeout "${WPC_RUN_TIMEOUT:-14400}"); fi

set +e
${TIMEOUT_CMD[@]+"${TIMEOUT_CMD[@]}"} "$CLAUDE_BIN" -p "$PROMPT" \
  --allowedTools "$ALLOWED_CSV" --disallowedTools "$DISALLOWED_CSV" --output-format text >> "$LOG" 2>&1
RC=$?
set -e
echo "=== claude exited with $RC at $(date -u +%FT%TZ) ===" >> "$LOG"

PLAN_JSON="$(ls -1t "change-plans/$DATE"/change-plan-"$DATE"*.json 2>/dev/null | head -n1 || true)"
if [[ $RC -eq 0 && -n "$PLAN_JSON" ]] && ! grep -q "PLAN_FAILED:" "$LOG"; then
  read -r HASH SUMMARY < <("$PY" - "$PLAN_JSON" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
s = p["summary"]
print(p["integrity"]["sha256"],
      f"Audited {s['total_active_audited']}, passed {s['passed_10_of_10']}, replace {s['slated_for_replacement']} "
      f"({s['replacements_found']} found), add {s['net_new_additions']}, new sub-collections {s['sub_collections_proposed']}.")
PY
)
  notify "Weekly Change Plan ready for approval ($DATE)" "$SUMMARY
Review: $PROJECT_DIR/${PLAN_JSON%.json}.md
Approve in Claude Code: /winning-product-curator --apply ./$PLAN_JSON --confirm-hash $HASH" ok
  exit 0
fi
notify "Weekly Change Plan FAILED ($DATE)" "Exit code $RC. No plan was produced or Claude reported PLAN_FAILED. Log: $PROJECT_DIR/$LOG" error
exit 1
