"""
Daily email digest — run via cron at 9 PM.

Crontab entry (example):
  0 21 * * * /usr/bin/python3 /agent/daily_digest.py >> /agent/digest.log 2>&1

SMTP credentials come from agent/.env (never mounted into Docker, never in CC context):
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=you@gmail.com
  SMTP_PASSWORD=your-app-password
  DIGEST_TO=you@gmail.com
"""

import json
import os
import smtplib
from datetime import date
from email.message import EmailMessage
from pathlib import Path

TASKS_FILE = Path(os.environ.get("TASKS_FILE", "/agent/tasks.json"))


def load_env_file(path: str = "/agent/.env") -> None:
    """Load key=value pairs from .env into os.environ."""
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass


def build_body(tasks: list, today: str) -> str:
    done    = [t for t in tasks if t["status"] == "done"    and (t.get("completed_at") or "").startswith(today)]
    pending = [t for t in tasks if t["status"] == "pending"]
    failed  = [t for t in tasks if t["status"] == "failed"  and (t.get("completed_at") or (t.get("created_at") or "")).startswith(today)]

    lines = []

    lines.append(f"✓ Completed ({len(done)}):")
    for t in done:
        lines.append(f"  #{t['id']} — {t['prompt'][:70]}")
    if not done:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"⏳ Pending ({len(pending)}):")
    for t in pending:
        lines.append(f"  #{t['id']} — {t['prompt'][:70]}")
    if not pending:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"✗ Failed ({len(failed)}):")
    for t in failed:
        reason = t.get("summary", "")
        lines.append(f"  #{t['id']} — {t['prompt'][:60]}{(' (' + reason + ')') if reason else ''}")
    if not failed:
        lines.append("  (none)")

    return "\n".join(lines)


def send_digest() -> None:
    load_env_file()

    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    digest_to = os.environ.get("DIGEST_TO", smtp_user)

    if not smtp_user or not smtp_pass:
        print("[digest] SMTP credentials not set — skipping email.")
        return

    today = date.today().isoformat()
    data = json.loads(TASKS_FILE.read_text()) if TASKS_FILE.exists() else {"tasks": []}
    tasks = data["tasks"]

    done_count    = sum(1 for t in tasks if t["status"] == "done"    and (t.get("completed_at") or "").startswith(today))
    pending_count = sum(1 for t in tasks if t["status"] == "pending")

    subject = f"Agent Daily Report — {done_count} done, {pending_count} pending [{today}]"
    body = build_body(tasks, today)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = digest_to
    msg.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)

    print(f"[digest] Sent: {subject}")


if __name__ == "__main__":
    send_digest()
