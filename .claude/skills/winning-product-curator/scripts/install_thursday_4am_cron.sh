#!/usr/bin/env bash
# Idempotent installer for the weekly Thursday 04:00 (store-local) Change Plan run.
#
#   install_thursday_4am_cron.sh                install (crontab on Linux/WSL, launchd on macOS)
#   install_thursday_4am_cron.sh --test         dry-run test: check config, print the exact headless command
#   install_thursday_4am_cron.sh --run-now      run the plan job once now (read-only Phase A), then exit
#   install_thursday_4am_cron.sh --print        show what would be installed, change nothing
#   install_thursday_4am_cron.sh --uninstall    remove the schedule
#   install_thursday_4am_cron.sh --github-actions  also write .github/workflows/winning-product-curator-weekly.yml
#
# Why not Claude Code /loop? Interactive loop timers die when the terminal closes (and expire after
# 3 days), so a persistent OS scheduler is used instead.
#
# Time zone handling: the schedule is `0 4 * * 4` in STORE_TIMEZONE. If the machine's clock zone equals
# STORE_TIMEZONE, the exact `0 4 * * 4` line (launchd: Weekday 4 Hour 4) is installed. If it differs,
# an hourly Wed-Fri trigger is installed and run_weekly_plan.sh --slot-guard fires only when it is
# Thursday 04:xx in STORE_TIMEZONE (handles DST in both zones), once per day.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
RUNNER="$SCRIPT_DIR/run_weekly_plan.sh"
MARKER="# winning-product-curator weekly plan"
LABEL="com.winning-product-curator.weekly"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ACTION="install"

case "${1:-}" in
  "") ;;
  --test) ACTION="test" ;;
  --run-now) ACTION="run" ;;
  --print) ACTION="print" ;;
  --uninstall) ACTION="uninstall" ;;
  --github-actions) ACTION="gha" ;;
  -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
  *) echo "unknown option $1" >&2; exit 64 ;;
esac

chmod +x "$RUNNER"
STORE_TZ="UTC"
if [[ -f "$PROJECT_DIR/.env" ]]; then
  STORE_TZ="$(grep -E '^[[:space:]]*(export[[:space:]]+)?STORE_TIMEZONE=' "$PROJECT_DIR/.env" | tail -n1 | sed -E 's/^[^=]*=//; s/^["'\'']//; s/["'\'']$//' || true)"
  STORE_TZ="${STORE_TZ:-UTC}"
fi

system_tz() {
  if [[ -n "${TZ:-}" ]]; then echo "$TZ"; return; fi
  if command -v timedatectl >/dev/null 2>&1 && timedatectl show -p Timezone --value >/dev/null 2>&1; then
    timedatectl show -p Timezone --value; return
  fi
  if [[ -L /etc/localtime ]]; then readlink /etc/localtime | sed -E 's#.*/zoneinfo/##'; return; fi
  if [[ -f /etc/timezone ]]; then cat /etc/timezone; return; fi
  echo "UTC"
}
SYS_TZ="$(system_tz)"
if [[ "$SYS_TZ" == "$STORE_TZ" ]]; then
  EXACT=1; CRON_SPEC="0 4 * * 4"; RUN_ARGS=""
else
  EXACT=0; CRON_SPEC="0 * * * 3-5"; RUN_ARGS=" --slot-guard"
fi
CRON_LINE="$CRON_SPEC cd \"$PROJECT_DIR\" && /usr/bin/env bash \"$RUNNER\"$RUN_ARGS >/dev/null 2>&1 $MARKER"

case "$ACTION" in
  test) exec "$RUNNER" --dry-run ;;
  run) exec "$RUNNER" ;;
esac

if [[ "$ACTION" == "gha" ]]; then
  mkdir -p "$PROJECT_DIR/.github/workflows"
  read -r UTC_A UTC_B < <(python3 - "$STORE_TZ" <<'PY'
import datetime as dt, sys
from zoneinfo import ZoneInfo
tz = ZoneInfo(sys.argv[1])
hours = set()
for month in (1, 7):  # winter and summer offsets
    local = dt.datetime(2026, month, 15, 4, 0, tzinfo=tz)
    hours.add(local.astimezone(dt.timezone.utc).hour)
hs = sorted(hours)
print(hs[0], hs[-1])
PY
)
  WF="$PROJECT_DIR/.github/workflows/winning-product-curator-weekly.yml"
  cat > "$WF" <<YAML
# Optional cloud runner for the weekly Thursday 04:00 ($STORE_TZ) Change Plan (Phase A, read-only).
# GitHub cron is UTC, so both DST-dependent UTC hours are scheduled and run_weekly_plan.sh --slot-guard
# skips the one that is not 04:xx store-local. Thursday 04:00 local can be Wednesday or Friday in UTC,
# so days 3-5 are listed and the guard does the exact check.
# Required repository secrets: ANTHROPIC_API_KEY, SHOPIFY_SHOP_DOMAIN, SHOPIFY_ADMIN_READ_TOKEN, WH_API_KEY.
# Optional: CJ_API_KEY, ALIEXPRESS_APP_KEY/SECRET/ACCESS_TOKEN, SLACK_WEBHOOK_URL, NTFY_TOPIC_URL.
# Never add SHOPIFY_ADMIN_WRITE_TOKEN here: Phase B is interactive only.
name: winning-product-curator weekly plan
on:
  schedule:
    - cron: "0 $UTC_A * * 3-5"
    - cron: "0 $UTC_B * * 3-5"
  workflow_dispatch: {}
permissions:
  contents: read
concurrency:
  group: winning-product-curator-weekly
  cancel-in-progress: false
jobs:
  plan:
    runs-on: ubuntu-latest
    timeout-minutes: 240
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - run: pip install -r .claude/skills/winning-product-curator/requirements.txt
      - run: npm install -g @anthropic-ai/claude-code
      - name: Write .env (read-only credentials only)
        run: |
          {
            echo "SHOPIFY_SHOP_DOMAIN=\${{ secrets.SHOPIFY_SHOP_DOMAIN }}"
            echo "SHOPIFY_ADMIN_READ_TOKEN=\${{ secrets.SHOPIFY_ADMIN_READ_TOKEN }}"
            echo "WH_API_KEY=\${{ secrets.WH_API_KEY }}"
            echo "CJ_API_KEY=\${{ secrets.CJ_API_KEY }}"
            echo "ALIEXPRESS_APP_KEY=\${{ secrets.ALIEXPRESS_APP_KEY }}"
            echo "ALIEXPRESS_APP_SECRET=\${{ secrets.ALIEXPRESS_APP_SECRET }}"
            echo "ALIEXPRESS_ACCESS_TOKEN=\${{ secrets.ALIEXPRESS_ACCESS_TOKEN }}"
            echo "SLACK_WEBHOOK_URL=\${{ secrets.SLACK_WEBHOOK_URL }}"
            echo "NTFY_TOPIC_URL=\${{ secrets.NTFY_TOPIC_URL }}"
            echo "STORE_TIMEZONE=$STORE_TZ"
            echo "NOTIFY_CHANNELS=slack,ntfy,stdout"
          } > .env
          cp .mcp.json.example .mcp.json
      - name: Run Phase A (slot-guarded unless manually dispatched)
        env:
          ANTHROPIC_API_KEY: \${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          if [ "\${{ github.event_name }}" = "workflow_dispatch" ]; then
            bash .claude/skills/winning-product-curator/scripts/run_weekly_plan.sh
          else
            bash .claude/skills/winning-product-curator/scripts/run_weekly_plan.sh --slot-guard
          fi
      - name: Upload plan and log
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: change-plan-\${{ github.run_id }}
          path: |
            change-plans/
            logs/
          if-no-files-found: ignore
YAML
  echo "Wrote $WF (UTC hours $UTC_A and $UTC_B, Wed-Fri, slot-guarded to Thursday 04:xx $STORE_TZ)."
  echo "Add the listed repository secrets, then push the workflow."
  exit 0
fi

uname_s="$(uname -s)"
if [[ "$uname_s" == "Darwin" ]]; then
  build_plist() {
    local intervals
    if [[ $EXACT -eq 1 ]]; then
      intervals="<dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>"
    else
      intervals=""
      for wd in 3 4 5; do for h in $(seq 0 23); do
        intervals+="<dict><key>Weekday</key><integer>$wd</integer><key>Hour</key><integer>$h</integer><key>Minute</key><integer>0</integer></dict>"
      done; done
    fi
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$RUNNER</string>$( [[ $EXACT -eq 0 ]] && echo "<string>--slot-guard</string>" )</array>
  <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
  <key>StartCalendarInterval</key>
  <array>$intervals</array>
  <key>StandardOutPath</key><string>$PROJECT_DIR/logs/launchd-weekly.out.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_DIR/logs/launchd-weekly.err.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict>
</plist>
PLIST
  }
  case "$ACTION" in
    print) build_plist; exit 0 ;;
    uninstall)
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
      rm -f "$PLIST"; echo "Removed launchd job $LABEL"; exit 0 ;;
  esac
  mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  build_plist > "$PLIST"
  plutil -lint "$PLIST" >/dev/null
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
  echo "Installed launchd job $LABEL -> $PLIST"
  echo "launchd runs a missed job when the Mac wakes; keep the Mac on (or use pmset) for an on-time 04:00 run."
else
  if ! command -v crontab >/dev/null 2>&1; then
    echo "crontab not found. Install cron (e.g. 'sudo apt-get install cron') or use --github-actions." >&2
    exit 1
  fi
  CURRENT="$(crontab -l 2>/dev/null || true)"
  FILTERED="$(printf '%s\n' "$CURRENT" | grep -vF "$MARKER" || true)"
  case "$ACTION" in
    print) echo "$CRON_LINE"; exit 0 ;;
    uninstall) printf '%s\n' "$FILTERED" | sed '/^$/d' | crontab -; echo "Removed crontab entry."; exit 0 ;;
  esac
  { printf '%s\n' "$FILTERED" | sed '/^$/d'; echo "$CRON_LINE"; } | crontab -
  echo "Installed crontab entry:"
  echo "  $CRON_LINE"
  if grep -qi microsoft /proc/version 2>/dev/null; then
    echo "WSL detected: cron only runs while WSL is up and the cron service is started ('sudo service cron start')."
  fi
fi

if [[ $EXACT -eq 1 ]]; then
  echo "Schedule: Thursday 04:00 $STORE_TZ (exact, machine clock is $SYS_TZ)."
else
  echo "Schedule: hourly Wed-Fri, guarded to fire once at Thursday 04:xx $STORE_TZ (machine clock is $SYS_TZ)."
fi
echo "Dry-run test:  bash $SCRIPT_DIR/install_thursday_4am_cron.sh --test"
echo "Logs:          $PROJECT_DIR/logs/weekly-audit-YYYY-MM-DD.log"
