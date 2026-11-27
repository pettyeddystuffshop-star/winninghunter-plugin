#!/usr/bin/env python3
"""Claude Code PreToolUse guard for winning-product-curator (defense in depth).

Registered in .claude/settings.json for every tool call in this project. Exit 2
blocks the call and shows the reason to Claude; exit 0 allows it (normal
permission rules still apply afterwards).

Blocks:
  * Shopify MCP write tools (graphql_mutation, create/update collection, bulk status,
    inventory, discounts, theme writes ...) - store writes only go through
    apply_approved_plan.py, which enforces the hash + approval gates.
  * DSers / AutoDS MCP write tools, unless a Phase B apply session is active
    (.wpc/apply-session.json written by `apply_approved_plan.py execute` after all
    gates passed, bound to the plan hash, valid for 4 hours) - used only for the
    supplier-linking work orders.
  * Any Bash call to apply_approved_plan.py review/execute/mark-linked/finalize without a
    full 64-hex --confirm-hash, and every such call while WPC_MODE=plan / WPC_HEADLESS=1
    (the Thursday scheduled run).
  * Bash commands that send GraphQL mutations or REST writes to *.myshopify.com, or that
    reference SHOPIFY_ADMIN_WRITE_TOKEN outside the apply script.
  * Write/Edit/NotebookEdit on change-plans/** (plans are immutable; the owner edits the
    .md checklist in their own editor and decisions are synced by `review`).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[4])
SESSION = PROJECT / ".wpc" / "apply-session.json"
APPLY_SCRIPT = "apply_approved_plan.py"

SHOPIFY_WRITE_NAMES = re.compile(
    r"^(graphql_mutation|create[-_].*|update[-_].*|delete[-_].*|add[-_]to[-_].*|bulk[-_].*|set[-_].*|publish[-_].*|"
    r"import[-_].*|upload[-_].*|theme_(commit|publish|bash)|create_store)$", re.IGNORECASE)
SUPPLIER_WRITE_NAMES = re.compile(
    r"^(apply_.*|batch_update_.*|cancel_.*|create_.*|delete_.*|import_.*|link_.*|place_.*|publish_.*|request_.*|"
    r"send_.*|set_.*|start_.*|sync_.*|update_.*|upload_.*)$", re.IGNORECASE)


def mcp_parts(tool: str) -> tuple[str, str] | None:
    """mcp__<server>__<tool> -> (server lower-case, tool). Server names may contain single underscores."""
    if not tool.startswith("mcp__"):
        return None
    rest = tool[len("mcp__"):]
    server, sep, name = rest.rpartition("__")
    return (server.lower(), name) if sep else None


def block(msg: str) -> None:
    print(f"[winning-product-curator guard] BLOCKED: {msg}", file=sys.stderr)
    sys.exit(2)


def apply_session_active() -> bool:
    try:
        data = json.loads(SESSION.read_text(encoding="utf-8"))
        expires = dt.datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        return expires > dt.datetime.now(dt.timezone.utc) and bool(re.fullmatch(r"[0-9a-f]{64}", data.get("plan_sha256", "")))
    except (OSError, ValueError, KeyError):
        return False


def headless_plan_mode() -> bool:
    return os.environ.get("WPC_MODE") == "plan" or os.environ.get("WPC_HEADLESS") == "1"


def check_bash(cmd: str) -> None:
    if APPLY_SCRIPT in cmd:
        m = re.search(rf"{APPLY_SCRIPT}\s+(\S+)", cmd)
        sub = m.group(1) if m else ""
        if sub in ("review", "execute", "finalize", "mark-linked"):
            if headless_plan_mode():
                block("Phase B commands are disabled in scheduled/headless plan mode.")
            if not re.search(r"--confirm-hash[ =][0-9a-fA-F]{64}\b", cmd):
                block("apply_approved_plan.py requires --confirm-hash <full SHA-256> supplied by the store owner.")
        return
    low = cmd.lower()
    if "shopify_admin_write_token" in low:
        block("SHOPIFY_ADMIN_WRITE_TOKEN may only be used by apply_approved_plan.py.")
    if "myshopify.com" in low or "/admin/api/" in low:
        if re.search(r"\bmutation\b", low):
            block("GraphQL mutations to Shopify are only allowed through the approval-gated apply script.")
        if re.search(r"(-x|--request)\s*['\"]?(put|delete|patch)\b", low):
            block("REST writes to Shopify are not allowed.")
        if re.search(r"(-x|--request)\s*['\"]?post\b", low) and "graphql.json" not in low:
            block("REST writes to Shopify are not allowed.")


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = event.get("tool_name") or ""
    tin = event.get("tool_input") or {}

    parts = mcp_parts(tool)
    server, name = parts if parts else ("", "")
    if "shopify" in server and SHOPIFY_WRITE_NAMES.match(name):
        block(f"{tool} writes to the Shopify store. All store changes go through an approved Change Plan "
              "(/winning-product-curator --apply <plan> --confirm-hash <sha256>).")
    if ("dsers" in server or "autods" in server or "cjdropshipping" in server) and SUPPLIER_WRITE_NAMES.match(name):
        if headless_plan_mode():
            block(f"{tool} is a supplier-side write; disabled in scheduled plan mode.")
        if not apply_session_active():
            block(f"{tool} is a supplier-side write. It is only allowed while a verified Phase B apply session is active "
                  "(run apply_approved_plan.py execute first) and only for the approved supplier work orders.")
    if tool == "Bash":
        check_bash(str(tin.get("command") or ""))
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = str(tin.get("file_path") or tin.get("notebook_path") or "")
        try:
            rel = Path(path).resolve().relative_to(PROJECT.resolve())
        except ValueError:
            rel = Path(path)
        if rel.parts and rel.parts[0] == "change-plans":
            block("Change Plans are immutable. The owner ticks the .md checklist in their own editor; "
                  "decisions are recorded with `apply_approved_plan.py review`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
