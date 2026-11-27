#!/usr/bin/env python3
"""Send the "weekly Change Plan is ready" (or failed) notification.

Channels (comma-separated NOTIFY_CHANNELS; default "desktop"):
  slack    SLACK_WEBHOOK_URL (incoming webhook)
  ntfy     NTFY_TOPIC_URL (e.g. https://ntfy.sh/<private-topic>), optional NTFY_TOKEN
  email    SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO
  desktop  macOS notification (osascript) or Linux notify-send, if available
  stdout   print only

Usage: notify.py --title "..." --message "..." [--level ok|error]
A failing channel is logged and does not stop the others. Exit code is 0 if at least one channel succeeded.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import smtplib
import ssl
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import get_logger, http_json, load_env  # noqa: E402

log = get_logger("notify")


def slack(title, message, level):
    url = os.environ["SLACK_WEBHOOK_URL"]
    emoji = ":white_check_mark:" if level == "ok" else ":rotating_light:"
    http_json("POST", url, body={"text": f"{emoji} *{title}*\n{message}"}, max_retries=2)


def ntfy(title, message, level):
    import urllib.request
    req = urllib.request.Request(os.environ["NTFY_TOPIC_URL"], data=message.encode(), method="POST",
                                 headers={"Title": title, "Priority": "default" if level == "ok" else "high",
                                          "Tags": "white_check_mark" if level == "ok" else "rotating_light"})
    if os.environ.get("NTFY_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['NTFY_TOKEN']}")
    urllib.request.urlopen(req, timeout=30).read()


def email(title, message, level):
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = os.environ.get("NOTIFY_EMAIL_FROM") or os.environ["SMTP_USER"]
    msg["To"] = os.environ["NOTIFY_EMAIL_TO"]
    msg.set_content(message)
    port = int(os.environ.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], port, context=ctx, timeout=30) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)


def desktop(title, message, level):
    if platform.system() == "Darwin" and shutil.which("osascript"):
        script = f'display notification {json_str(message[:200])} with title {json_str(title)}'
        subprocess.run(["osascript", "-e", script], check=True, timeout=15)
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", "-u", "normal" if level == "ok" else "critical", title, message[:300]], check=True, timeout=15)
    else:
        raise RuntimeError("no desktop notifier available")


def stdout(title, message, level):
    print(f"[{level.upper()}] {title}\n{message}")


def json_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


CHANNELS = {"slack": slack, "ntfy": ntfy, "email": email, "desktop": desktop, "stdout": stdout}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--title", required=True)
    ap.add_argument("--message", required=True)
    ap.add_argument("--level", choices=["ok", "error"], default="ok")
    args = ap.parse_args()
    load_env()
    names = [c.strip() for c in os.environ.get("NOTIFY_CHANNELS", "desktop").split(",") if c.strip()]
    ok = 0
    for name in names:
        fn = CHANNELS.get(name)
        if not fn:
            log.error("unknown channel %s", name)
            continue
        try:
            fn(args.title, args.message, args.level)
            ok += 1
        except Exception as exc:  # never let one channel break the others
            log.error("channel %s failed: %s", name, exc)
    if not ok:
        stdout(args.title, args.message, args.level)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
