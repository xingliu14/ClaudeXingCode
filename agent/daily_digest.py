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

_AGENT_DIR = Path(__file__).resolve().parent
TASKS_FILE = Path(os.environ.get("TASKS_FILE", str(_AGENT_DIR.parent / "tasks.json")))


def load_env_file(path: str = "") -> None:
    """Load key=value pairs from .env into os.environ.

    Uses setdefault so existing env vars are never overridden — the real
    environment always wins over the file. Handles standard .env quoting:
    both SMTP_PASSWORD="secret" and SMTP_PASSWORD=secret are accepted.
    Splits on the first '=' only, so values containing '=' are preserved.
    """
    if not path:
        path = str(_AGENT_DIR / ".env")
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                val = val.strip()
                # Strip surrounding quotes (single or double) from values
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                os.environ.setdefault(key.strip(), val)
    except FileNotFoundError:
        pass


def build_body(tasks: list, today: str) -> str:
    """Build the plain-text email body with three sections plus a session stats footer.

    Sections:
      - Completed: tasks done today (filtered by completed_at date)
      - Pending: all currently-pending tasks (always shown for awareness)
      - Failed: all currently-stopped tasks. Not date-filtered because stopped
        tasks lack a stopped_at timestamp — filtering by created_at would silently
        miss tasks that were created on one day and stopped on another.
      - Stats: session count, rate-limit hits, avg duration — filtered to today's
        sessions by started_at date prefix.
    """
    done    = [t for t in tasks if t["status"] == "done"    and (t.get("completed_at") or "").startswith(today)]
    pending = [t for t in tasks if t["status"] == "pending"]
    failed  = [t for t in tasks if t["status"] == "stopped"]

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
        reason = (t.get("stop_reason") or t.get("summary") or "")[:80]
        lines.append(f"  #{t['id']} — {t['prompt'][:60]}{(' (' + reason + ')') if reason else ''}")
    if not failed:
        lines.append("  (none)")

    # Session stats: only today's sessions (filtered by started_at date prefix)
    today_sessions = [
        s for t in tasks for s in (t.get("sessions") or [])
        if (s.get("started_at") or "").startswith(today)
    ]
    rate_limit_count = sum(1 for s in today_sessions if s.get("rate_limited"))
    durations = [s["duration_s"] for s in today_sessions if "duration_s" in s]
    if durations:
        avg_s = round(sum(durations) / len(durations))
        avg_str = f"{avg_s // 60}m {avg_s % 60}s" if avg_s >= 60 else f"{avg_s}s"
    else:
        avg_str = "n/a"

    lines.append("")
    lines.append(f"📊 Sessions today: {len(today_sessions)}  |  Rate limits: {rate_limit_count}  |  Avg duration: {avg_str}")

    return "\n".join(lines)


def send_digest() -> None:
    """Build and send the daily digest email via SMTP.

    Flow: load .env credentials → read tasks.json → build body → send via STARTTLS.
    Silently exits if SMTP_USER or SMTP_PASSWORD are not set (allows running
    the cron job on machines without email configured).
    Reads tasks.json directly (not via task_store) so the digest can run
    standalone without the agent's sys.path setup.
    """
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

    body = build_body(tasks, today)
    done_count    = sum(1 for t in tasks if t["status"] == "done"    and (t.get("completed_at") or "").startswith(today))
    pending_count = sum(1 for t in tasks if t["status"] == "pending")
    subject = f"Agent Daily Report — {done_count} done, {pending_count} pending [{today}]"

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
