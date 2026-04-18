"""
Web UI for the agentic coding team.

Routes:
  GET  /                         - task board (Kanban)
  POST /tasks                    - add new task
  GET  /tasks/<id>               - task detail (plan, log, sessions, subtasks)
  POST /tasks/<id>/edit          - edit task prompt / priority
  POST /tasks/<id>/delete        - delete task
  POST /tasks/<id>/approve       - approve plan -> dispatcher executes
  POST /tasks/<id>/reject        - reject plan with feedback
  POST /tasks/<id>/cancel        - cancel in-progress task -> stopped
  POST /tasks/<id>/retry         - requeue stopped/done task -> pending
  GET  /progress                 - view PROGRESS.md
  GET  /log                      - view recent git log
  GET  /status                   - dispatcher status (JSON)
"""

import json
import os
import shutil
import sys
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_AGENT_DIR / "core"))

import hashlib
from functools import wraps
from flask import Flask, redirect, render_template_string, request, url_for, jsonify, session
from markupsafe import Markup
import markdown as _markdown
from werkzeug.security import check_password_hash
from progress_logger import log_progress
from task_store import load_tasks, save_tasks, locked_update, next_id, TASKS_FILE, STATUS_FILE, DEFAULT_ACCOUNT

_ACCOUNTS_FILE = _AGENT_DIR / "accounts.json"

def _load_accounts() -> dict:
    """Load username → {password_hash, account} from accounts.json."""
    if not _ACCOUNTS_FILE.exists():
        return {}
    try:
        return json.loads(_ACCOUNTS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

_DEFAULT_WORKSPACE = str(Path(__file__).resolve().parent.parent.parent)
WORKSPACE = Path(os.environ.get("WORKSPACE", _DEFAULT_WORKSPACE))
PROGRESS_FILE = WORKSPACE / "agent_log" / "agent_log.md"

app = Flask(__name__)

# Derive a stable secret key from the OAuth token so sessions survive restarts.
# Override with FLASK_SECRET_KEY env var if needed.
app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    hashlib.sha256(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "ralph-loop").encode()).digest(),
)

WEB_PORT = int(os.environ.get("WEB_PORT", 5001))


@app.context_processor
def _inject_user():
    return {"username": session.get("username", "")}


@app.before_request
def _require_auth():
    """Redirect unauthenticated requests to /login. API routes get JSON 401."""
    if request.endpoint in ("login", "logout", "static"):
        return None
    if "account" not in session:
        if request.path.startswith("/api/") or request.path == "/status":
            return jsonify({"error": "not authenticated"}), 401
        return redirect(url_for("login"))


def _check_owner(task_id: int):
    """Return (task, error) — error is a (message, code) tuple or None."""
    acc = session.get("account", DEFAULT_ACCOUNT)
    data = load_tasks()
    task = next((t for t in data["tasks"] if t["id"] == task_id), None)
    if task is None:
        return None, ("Task not found", 404)
    if task.get("account", DEFAULT_ACCOUNT) != acc:
        return None, ("Forbidden", 403)
    return task, None


_PT = ZoneInfo("America/Los_Angeles")

@app.template_filter("md")
def render_md(text):
    """Convert markdown text to safe HTML for rendering in templates."""
    if not text:
        return ""
    return Markup(_markdown.markdown(str(text), extensions=["tables", "fenced_code"]))

@app.template_filter("pt")
def to_pt(ts):
    """Convert an ISO timestamp string to Pacific time, formatted as YYYY-MM-DD HH:MM:SS PT."""
    if not ts:
        return "?"
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_PT).strftime("%Y-%m-%d %H:%M:%S PT")
    except Exception:
        return str(ts)[:19]


# ---------------------------------------------------------------------------
# Shared CSS
# ---------------------------------------------------------------------------

SHARED_CSS = """
    :root {
      --bg: #f1f5f9;
      --surface: #fff;
      --surface-2: #f8fafc;
      --border: #e2e8f0;
      --border-hover: #cbd5e1;
      --text: #0f172a;
      --text-muted: #64748b;
      --text-subtle: #94a3b8;
      --radius-sm: 5px;
      --radius: 8px;
      --shadow-sm: 0 1px 2px rgba(15,23,42,.06);
      --shadow: 0 1px 3px rgba(15,23,42,.08), 0 2px 8px rgba(15,23,42,.04);
    }
    *, *::before, *::after { box-sizing: border-box; }
    body { font-family: system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
           margin: 0; background: var(--bg); color: var(--text); line-height: 1.5;
           -webkit-font-smoothing: antialiased; }
    a { color: #4f46e5; text-decoration: none; }
    a:hover { opacity: .85; }
    header { background: #1a1a2e; color: #fff; padding: 0.65rem 1.25rem; display: flex;
             align-items: center; justify-content: space-between; flex-wrap: wrap;
             gap: 0.5rem; position: sticky; top: 0; z-index: 100;
             box-shadow: 0 1px 0 rgba(255,255,255,.06); }
    header h1 { margin: 0; font-size: 0.98rem; font-weight: 700; color: #f1f5f9;
                letter-spacing: -.01em; }
    header nav { display: flex; gap: 0.7rem; align-items: center; flex-wrap: wrap; }
    header nav a { color: #94a3b8; text-decoration: none; font-size: 0.8rem;
                   font-weight: 500; transition: color .15s; }
    header nav a:hover { color: #e2e8f0; }
    .status-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
                  margin-right: 0.25rem; vertical-align: middle; }
    .status-running  { background: #22c55e; box-shadow: 0 0 0 2px rgba(34,197,94,.25); }
    .status-sleeping { background: #f59e0b; box-shadow: 0 0 0 2px rgba(245,158,11,.25); }
    .status-idle { background: #64748b; }
    #review-badge { display: inline-flex; align-items: center; background: #f59e0b;
                    color: #fff; font-size: 0.7rem; font-weight: 700;
                    padding: 2px 8px; border-radius: 20px; }
    .btn { display: inline-flex; align-items: center; justify-content: center;
           height: 34px; padding: 0 14px; border-radius: var(--radius-sm); border: none;
           cursor: pointer; font-size: 0.85rem; font-weight: 500; text-decoration: none;
           white-space: nowrap; transition: filter .12s; font-family: inherit; }
    .btn:hover { filter: brightness(.88); }
    .btn:active { filter: brightness(.78); }
    .btn-sm { height: 28px; padding: 0 10px; font-size: 0.78rem; }
    .btn-approve   { background: #059669; color: #fff; }
    .btn-reject    { background: #dc2626; color: #fff; }
    .btn-edit      { background: #4f46e5; color: #fff; }
    .btn-delete    { background: #991b1b; color: #fff; }
    .btn-cancel    { background: #d97706; color: #fff; }
    .btn-retry     { background: #0284c7; color: #fff; }
    .btn-secondary { background: #e2e8f0; color: #374151; }
    .state-badge { display: inline-flex; align-items: center; padding: 2px 9px;
                   border-radius: 20px; font-size: 0.7rem; font-weight: 600;
                   letter-spacing: .03em; }
    .state-pending      { background: #e0e7ff; color: #3730a3; }
    .state-planning     { background: #dbeafe; color: #1d4ed8; }
    .state-executing    { background: #e0f2fe; color: #0369a1; }
    .state-plan_review  { background: #fef3c7; color: #92400e; }
    .state-done         { background: #dcfce7; color: #166534; }
    .state-stopped      { background: #fee2e2; color: #991b1b; }
    .state-decomposed   { background: #f3e8ff; color: #6b21a8; }
"""

# ---------------------------------------------------------------------------
# Header partial (nav bar shown on every page)
# ---------------------------------------------------------------------------

HEADER_HTML = """
<header>
  <h1>ClaudeXingCode Dashboard</h1>
  <nav>
    <a href="/">Board</a>
    <a href="/progress">Progress</a>
    <a href="/log">Git Log</a>
    <span id="review-badge" style="display:none"></span>
    <span id="dispatcher-status"></span>
    <span style="color:#475569;font-size:0.78rem">{{ username }}</span>
    <a href="/logout" style="color:#64748b;font-size:0.78rem">Sign out</a>
  </nav>
</header>
<script>
fetch('/status').then(r=>r.json()).then(d=>{
  const el=document.getElementById('dispatcher-status');
  const dot=d.state||'idle';
  const label=d.label||dot;
  el.innerHTML='<span class="status-dot status-'+dot+'"></span><span style="color:#94a3b8;font-size:0.78rem">'+label+'</span>';
}).catch(()=>{});
</script>
"""

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

BOARD_HTML = """
{% macro priority_select(t) %}<form method="post" action="/tasks/{{ t.id }}/set-priority" style="margin:0;display:inline"><select name="priority" onchange="this.form.submit()" class="prio-select prio-sel-{{ t.get('priority','medium') }}"><option value="high" {% if t.get('priority')=='high' %}selected{% endif %}>high</option><option value="medium" {% if t.get('priority','medium')=='medium' %}selected{% endif %}>medium</option><option value="low" {% if t.get('priority')=='low' %}selected{% endif %}>low</option></select></form>{% endmacro %}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⭐</text></svg>">
  <title>ClaudeXingCode Dashboard</title>
  <style>
    """ + SHARED_CSS + """
    /* --- Board layout --- */
    .add-card { background: var(--surface); border-bottom: 1px solid var(--border);
                padding: 0.85rem 1rem; }
    .add-title { font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
                 letter-spacing: .06em; color: var(--text-subtle); margin-bottom: 0.6rem; }
    form.add { display: flex; flex-direction: column; gap: 0.45rem; }
    form.add input[type="text"] { width: 100%; padding: 0.5rem 0.7rem;
      border: 1px solid var(--border); border-radius: var(--radius-sm);
      font-size: 0.875rem; font-family: inherit; color: var(--text);
      background: var(--surface-2); transition: border-color .15s, box-shadow .15s; }
    form.add textarea { width: 100%; padding: 0.5rem 0.7rem;
      border: 1px solid var(--border); border-radius: var(--radius-sm);
      font-size: 0.875rem; font-family: inherit; color: var(--text);
      background: var(--surface-2); resize: none; overflow: hidden; min-height: 36px;
      transition: border-color .15s, box-shadow .15s; }
    form.add input:focus, form.add textarea:focus { outline: none;
      border-color: #a5b4fc; box-shadow: 0 0 0 3px rgba(79,70,229,.1); }
    .add-controls { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
    .add-label { display: flex; align-items: center; gap: 0.3rem; font-size: 0.8rem;
                 color: var(--text-muted); white-space: nowrap; }
    .add-label select { padding: 0.3rem 0.5rem; border: 1px solid var(--border);
      border-radius: var(--radius-sm); font-size: 0.8rem; background: var(--surface);
      color: var(--text); cursor: pointer; }
    .add-label select:focus { outline: none; border-color: #a5b4fc; }
    .add-label input[type="checkbox"] { width: 14px; height: 14px; cursor: pointer;
                                        accent-color: #4f46e5; }
    .add-controls .btn { margin-left: auto; }
    .toolbar { padding: 0.5rem 1rem; }
    .section-label { font-size: 0.67rem; text-transform: uppercase; letter-spacing: .08em;
                     color: var(--text-subtle); padding: 0.6rem 1rem 0.3rem; font-weight: 700; }
    .pipeline { display: flex; gap: 10px; padding: 0 1rem 1rem; overflow-x: auto;
                align-items: flex-start; -webkit-overflow-scrolling: touch;
                scroll-snap-type: x proximity; }
    .pipeline::-webkit-scrollbar { height: 4px; }
    .pipeline::-webkit-scrollbar-track { background: transparent; }
    .pipeline::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
    .col { background: var(--surface-2); border: 1px solid var(--border);
           border-radius: var(--radius); min-width: 210px; flex: 1 1 210px;
           max-width: 300px; padding: 10px; border-top: 3px solid var(--border);
           scroll-snap-align: start; }
    .col-pending     { border-top-color: #818cf8; }
    .col-planning    { border-top-color: #60a5fa; }
    .col-executing   { border-top-color: #38bdf8; }
    .col-plan_review { border-top-color: #fbbf24; }
    .col-done        { border-top-color: #4ade80; }
    .col-stopped     { border-top-color: #f87171; }
    .col-decomposed  { border-top-color: #c084fc; }
    .col h2 { font-size: 0.72rem; text-transform: uppercase; letter-spacing: .06em;
              color: var(--text-muted); margin: 0 0 8px;
              display: flex; align-items: center; gap: 5px; }
    .col h2 .count { background: var(--border); color: var(--text-subtle);
                     font-size: 0.65rem; font-weight: 700; padding: 1px 6px;
                     border-radius: 20px; margin-left: auto; font-weight: 700; }
    .col h2 .gate-icon { font-size: 0.75rem; }
    .card { background: var(--surface); border: 1px solid var(--border);
            border-radius: var(--radius-sm); padding: 9px 10px; margin-bottom: 6px;
            transition: box-shadow .12s, border-color .12s; }
    .card:hover { border-color: var(--border-hover); box-shadow: var(--shadow); }
    .card-link { color: var(--text); text-decoration: none; font-weight: 600;
                 display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
                 overflow: hidden; line-height: 1.4; font-size: 0.82rem; }
    .card-link:hover { color: #4f46e5; }
    .card .meta { color: var(--text-muted); font-size: 0.7rem; margin-top: 6px;
                  display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
    .badge { display: inline-flex; align-items: center; padding: 1px 6px;
             border-radius: 20px; font-size: 0.67rem; font-weight: 600; }
    .badge-high   { background: #fee2e2; color: #b91c1c; }
    .badge-medium { background: #fef9c3; color: #92400e; }
    .badge-low    { background: #dcfce7; color: #166534; }
    .badge-model  { background: #e0e7ff; color: #4338ca; }
    .badge-auto   { background: #d1fae5; color: #065f46; }
    .badge-blocked { background: #fde68a; color: #92400e; }
    .badge-elapsed { background: #dbeafe; color: #1e40af; }
    .reason-tag { display: inline-flex; align-items: center; padding: 1px 6px;
                  border-radius: 20px; font-size: 0.67rem; font-weight: 600;
                  background: #fee2e2; color: #991b1b; }
    .pushed-tag { display: inline-flex; align-items: center; padding: 1px 6px;
                  border-radius: 20px; font-size: 0.67rem; font-weight: 600;
                  background: #d1fae5; color: #065f46; }
    .btn-hide { background: none; border: 1px solid var(--border); border-radius: 20px;
                padding: 1px 7px; font-size: 0.63rem; color: var(--text-subtle);
                cursor: pointer; white-space: nowrap; transition: background .12s;
                font-family: inherit; }
    .btn-hide:hover { background: var(--border); color: var(--text-muted); }
    .card.hidden-card { opacity: 0.4; }
    .col-done h2 { cursor: pointer; user-select: none; }
    .col-done h2::after { content: ' ▾'; color: var(--text-subtle); margin-left: 2px; }
    .col-done.col-collapsed h2::after { content: ' ▸'; }
    .col-done.col-collapsed > :not(h2) { display: none; }
    .col-plan_review { background: #fffbeb; }
    .col-plan_review h2 { color: #92400e; }
    .card-review { border-color: #fbbf24 !important; background: #fffef5 !important; }
    .btn-review { display: inline-flex; align-items: center; padding: 2px 8px;
                  border-radius: 20px; background: #f59e0b; color: #fff !important;
                  font-size: 0.67rem; font-weight: 700; white-space: nowrap; }
    .btn-review:hover { filter: brightness(.9); opacity: 1; }
    #review-banner { display: none; margin: 0 1rem 0.6rem; padding: 0.55rem 1rem;
                     background: #fffbeb; border: 1px solid #fbbf24;
                     border-radius: var(--radius); font-size: 0.85rem;
                     color: #92400e; font-weight: 500; }
    #review-banner a { color: #b45309; font-weight: 700; }
    .prio-select { border-radius: 20px !important; border: 1px solid var(--border) !important;
                   font-size: 0.65rem !important; font-weight: 600 !important;
                   cursor: pointer; padding: 1px 5px !important;
                   font-family: inherit; }
    .prio-sel-high   { background: #fee2e2 !important; color: #b91c1c !important;
                       border-color: #fca5a5 !important; }
    .prio-sel-medium { background: #fef9c3 !important; color: #92400e !important;
                       border-color: #fde68a !important; }
    .prio-sel-low    { background: #dcfce7 !important; color: #166534 !important;
                       border-color: #bbf7d0 !important; }
    .offramp { display: flex; gap: 10px; padding: 0 1rem 1rem; flex-wrap: wrap; }
    .offramp .col { flex: 1 1 250px; max-width: 420px; }
    @media (max-width: 640px) {
      .add-card { padding: 0.7rem 0.75rem; }
      .add-controls { gap: 0.4rem; }
      .add-label span { display: none; }
      .add-controls .btn { margin-left: 0; }
      .toolbar, .section-label { padding-left: 0.75rem; padding-right: 0.75rem; }
      .pipeline { flex-direction: column; overflow-x: visible;
                  padding: 0 0.75rem 0.5rem; gap: 8px; }
      .col { min-width: 0; flex: 0 0 auto; max-width: 100%; width: 100%; }
      .offramp { flex-direction: column; padding: 0 0.75rem 1rem; }
      .offramp .col { max-width: 100%; }
    }
  </style>
</head>
<body>
""" + HEADER_HTML + """

<div class="add-card">
  <div class="add-title">New Task</div>
  <form class="add" method="post" action="/tasks">
    <input type="text" name="title" placeholder="Task title..." required>
    <textarea name="prompt" rows="1" placeholder="Description (optional detail for the agent)..."
      oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"></textarea>
    <div class="add-controls">
      <label class="add-label"><span>Priority</span>
        <select name="priority">
          <option value="medium">Medium</option>
          <option value="high">High</option>
          <option value="low">Low</option>
        </select>
      </label>
      <label class="add-label"><span>Plan</span>
        <select name="plan_model">
          <option value="sonnet">Sonnet</option>
          <option value="opus">Opus</option>
          <option value="haiku">Haiku</option>
        </select>
      </label>
      <label class="add-label"><span>Exec</span>
        <select name="exec_model">
          <option value="sonnet">Sonnet</option>
          <option value="opus">Opus</option>
          <option value="haiku">Haiku</option>
        </select>
      </label>
      <label class="add-label" style="cursor:pointer">
        <input type="checkbox" name="auto_approve" value="1"> Auto
      </label>
      <button type="submit" class="btn btn-edit btn-sm">Add Task</button>
    </div>
  </form>
</div>

<div class="toolbar">
  {% if show_hidden %}
  <a href="/" class="btn btn-sm btn-secondary">Hide Hidden</a>
  {% else %}
  <a href="/?show_hidden=1" class="btn btn-sm btn-secondary">Show Hidden</a>
  {% endif %}
</div>

<div id="review-banner"></div>

{# --- Main pipeline: happy path left-to-right --- #}
<div class="section-label">Pipeline</div>
<div class="pipeline">
  {% for col_status, col_label, col_icon in pipeline_cols %}
  <div class="col col-{{ col_status }}">
    <h2>
      {% if col_icon %}<span class="gate-icon">{{ col_icon }}</span>{% endif %}
      {{ col_label }}
      <span class="count">{{ tasks|selectattr('status','equalto',col_status)|list|length }}</span>
    </h2>
    {% for t in tasks if t.status == col_status %}
    <div class="card{% if t.get('hidden') %} hidden-card{% endif %}{% if t.status == 'plan_review' %} card-review{% endif %}">
      <a class="card-link" href="/tasks/{{ t.id }}">#{{ t.id }} {{ t.get('title') or t.prompt }}</a>
      <div class="meta">
        {{ priority_select(t) }}
        <span class="badge badge-model">P:{{ t.get('plan_model', t.get('model','sonnet')) }}</span>
        <span class="badge badge-model">E:{{ t.get('exec_model', t.get('model','sonnet')) }}</span>
        {% if t.get('auto_approve') %}<span class="badge badge-auto">auto</span>{% endif %}
        {% if t.get('parent') %}<span style="color:var(--text-subtle);font-size:0.67rem">&uarr;#{{ t.parent }}</span>{% endif %}
        {% if t.get('blocked_on') %}<span class="badge badge-blocked">blocked {{ t.blocked_on|length }}</span>{% endif %}
        {% if t.get('pushed_at') %}<span class="pushed-tag">pushed</span>{% endif %}
        {% if t.status == 'plan_review' %}<a href="/tasks/{{ t.id }}" class="btn-review">Review &rarr;</a>{% endif %}
        {% if t.get('hidden') %}
        <form method="post" action="/tasks/{{ t.id }}/unhide" style="margin:0"><button class="btn-hide">unhide</button></form>
        {% else %}
        <form method="post" action="/tasks/{{ t.id }}/hide" style="margin:0"><button class="btn-hide">hide</button></form>
        {% endif %}
      </div>
    </div>
    {% else %}
    <div style="color:var(--text-subtle);font-size:0.75rem;padding:0.2rem 0">&mdash;</div>
    {% endfor %}
  </div>
  {% endfor %}
</div>

{# --- Off-ramp: stopped & decomposed (always visible) --- #}
{% set stopped_tasks = tasks|selectattr('status','equalto','stopped')|list %}
{% set decomposed_tasks = tasks|selectattr('status','equalto','decomposed')|list %}
<div class="section-label">Off-ramp</div>
<div class="offramp">
  <div class="col col-stopped">
    <h2>Stopped <span class="count">{{ stopped_tasks|length }}</span></h2>
    {% for t in stopped_tasks %}
    <div class="card{% if t.get('hidden') %} hidden-card{% endif %}">
      <a class="card-link" href="/tasks/{{ t.id }}">#{{ t.id }} {{ t.get('title') or t.prompt }}</a>
      <div class="meta">
        {{ priority_select(t) }}
        {% if t.get('stop_reason') %}<span class="reason-tag">{{ t.stop_reason }}</span>{% endif %}
        {% if t.get('hidden') %}
        <form method="post" action="/tasks/{{ t.id }}/unhide" style="margin:0"><button class="btn-hide">unhide</button></form>
        {% else %}
        <form method="post" action="/tasks/{{ t.id }}/hide" style="margin:0"><button class="btn-hide">hide</button></form>
        {% endif %}
      </div>
    </div>
    {% else %}
    <div style="color:var(--text-subtle);font-size:0.75rem;padding:0.2rem 0">&mdash;</div>
    {% endfor %}
  </div>
  <div class="col col-decomposed">
    <h2>Decomposed <span class="count">{{ decomposed_tasks|length }}</span></h2>
    {% for t in decomposed_tasks %}
    <div class="card{% if t.get('hidden') %} hidden-card{% endif %}">
      <a class="card-link" href="/tasks/{{ t.id }}">#{{ t.id }} {{ t.get('title') or t.prompt }}</a>
      <div class="meta">
        {{ priority_select(t) }}
        {% if t.get('hidden') %}
        <form method="post" action="/tasks/{{ t.id }}/unhide" style="margin:0"><button class="btn-hide">unhide</button></form>
        {% else %}
        <form method="post" action="/tasks/{{ t.id }}/hide" style="margin:0"><button class="btn-hide">hide</button></form>
        {% endif %}
      </div>
    </div>
    {% else %}
    <div style="color:var(--text-subtle);font-size:0.75rem;padding:0.2rem 0">&mdash;</div>
    {% endfor %}
  </div>
</div>

<script>
(function() {
  const POLL_MS = 3000;
  const PIPELINE_COLS = {{ pipeline_cols_json|safe }};
  const showHidden = {{ 'true' if show_hidden else 'false' }};

  function esc(s) {
    const d = document.createElement('div'); d.textContent = s; return d.innerHTML;
  }

  function renderCard(t) {
    const cls = t.hidden ? ' hidden-card' : '';
    const isReview = t.status === 'plan_review';
    const display = t.title || t.prompt;
    const prio = t.priority || 'medium';
    const planModel = t.plan_model || t.model || 'sonnet';
    const execModel = t.exec_model || t.model || 'sonnet';
    let meta = '<form method="post" action="/tasks/' + t.id + '/set-priority" style="margin:0;display:inline">'
      + '<select name="priority" onchange="this.form.submit()" class="prio-select prio-sel-' + prio + '">'
      + '<option value="high"' + (prio==='high'?' selected':'') + '>high</option>'
      + '<option value="medium"' + (prio==='medium'?' selected':'') + '>medium</option>'
      + '<option value="low"' + (prio==='low'?' selected':'') + '>low</option>'
      + '</select></form>';
    meta += '<span class="badge badge-model">P:' + planModel + '</span>';
    meta += '<span class="badge badge-model">E:' + execModel + '</span>';
    if (t.auto_approve) meta += '<span class="badge badge-auto">auto</span>';
    if (t.parent) meta += '<span style="color:var(--text-subtle);font-size:0.67rem">\u2191#' + t.parent + '</span>';
    if (t.blocked_on && t.blocked_on.length) meta += '<span class="badge badge-blocked">blocked ' + t.blocked_on.length + '</span>';
    if (t.pushed_at) meta += '<span class="pushed-tag">pushed</span>';
    if (t.stop_reason) meta += '<span class="reason-tag">' + esc(t.stop_reason) + '</span>';
    if ((t.status === 'planning' || t.status === 'executing') && t.started_at) {
      const elapsed = Math.round((Date.now() - Date.parse(t.started_at)) / 1000);
      const m = Math.floor(elapsed / 60);
      const s = elapsed % 60;
      meta += '<span class="badge badge-elapsed">\u29d7 ' + (m > 0 ? m + 'm ' + s + 's' : s + 's') + '</span>';
    }
    if (isReview) meta += '<a href="/tasks/' + t.id + '" class="btn-review">Review \u2192</a>';
    const hideAction = t.hidden
      ? '<form method="post" action="/tasks/' + t.id + '/unhide" style="margin:0"><button class="btn-hide">unhide</button></form>'
      : '<form method="post" action="/tasks/' + t.id + '/hide" style="margin:0"><button class="btn-hide">hide</button></form>';
    meta += hideAction;
    const cardCls = 'card' + cls + (isReview ? ' card-review' : '');
    return '<div class="' + cardCls + '"><a class="card-link" href="/tasks/' + t.id + '">#' + t.id + ' ' + esc(display) + '</a><div class="meta">' + meta + '</div></div>';
  }

  function renderCol(tasks, status) {
    const items = tasks.filter(t => t.status === status);
    if (items.length === 0) return '<div style="color:var(--text-subtle);font-size:0.75rem;padding:0.2rem 0">&mdash;</div>';
    return items.map(renderCard).join('');
  }

  function updateBoard(data) {
    const tasks = showHidden ? data.tasks : data.tasks.filter(t => !t.hidden);

    // Update pipeline columns
    const pipeline = document.querySelector('.pipeline');
    if (pipeline) {
      const cols = pipeline.querySelectorAll('.col');
      PIPELINE_COLS.forEach(function(colDef, i) {
        const status = colDef[0];
        const count = tasks.filter(t => t.status === status).length;
        if (cols[i]) {
          const h2 = cols[i].querySelector('h2');
          if (h2) {
            const countSpan = h2.querySelector('.count');
            if (countSpan) countSpan.textContent = count;
          }
          while (cols[i].children.length > 1) cols[i].children[1].remove();
          cols[i].insertAdjacentHTML('beforeend', renderCol(tasks, status));
        }
      });
    }

    // Update off-ramp columns
    const offramp = document.querySelector('.offramp');
    if (offramp) {
      const offCols = offramp.querySelectorAll('.col');
      ['stopped', 'decomposed'].forEach(function(status, i) {
        const items = tasks.filter(t => t.status === status);
        if (offCols[i]) {
          const h2 = offCols[i].querySelector('h2');
          if (h2) {
            const countSpan = h2.querySelector('.count');
            if (countSpan) countSpan.textContent = items.length;
          }
          while (offCols[i].children.length > 1) offCols[i].children[1].remove();
          offCols[i].insertAdjacentHTML('beforeend', renderCol(tasks, status));
        }
      });
    }

    // Update dispatcher status
    const d = data.dispatcher;
    const el = document.getElementById('dispatcher-status');
    if (el && d) {
      const dot = d.state || 'idle';
      const label = d.label || dot;
      el.innerHTML = '<span class="status-dot status-' + dot + '"></span><span style="color:#94a3b8;font-size:0.78rem">' + esc(label) + '</span>';
    }

    // Review badge
    const reviewEl = document.getElementById('review-badge');
    const reviewTasks = data.tasks.filter(t => t.status === 'plan_review');
    if (reviewEl) {
      const n = reviewTasks.length;
      reviewEl.textContent = n > 0 ? '\u2691 ' + n : '';
      reviewEl.style.display = n > 0 ? 'inline-flex' : 'none';
    }

    // Action Required banner
    const banner = document.getElementById('review-banner');
    if (banner) {
      if (reviewTasks.length > 0) {
        const first = reviewTasks[0];
        banner.innerHTML = '\u2691 Action Required \u2014 ' + reviewTasks.length + ' task' + (reviewTasks.length > 1 ? 's' : '') +
          ' need' + (reviewTasks.length === 1 ? 's' : '') + ' your review \u2014 ' +
          '<a href="/tasks/' + first.id + '">Review #' + first.id + ' &rarr;</a>';
        banner.style.display = 'block';
      } else {
        banner.style.display = 'none';
      }
    }
  }

  function poll() {
    fetch('/api/tasks').then(r => r.json()).then(updateBoard).catch(() => {});
    setTimeout(poll, POLL_MS);
  }
  setTimeout(poll, POLL_MS);

  // Done column collapsed by default; persisted in localStorage
  (function() {
    const doneCol = document.querySelector('.col-done');
    if (!doneCol) return;
    const stored = localStorage.getItem('done-col-collapsed');
    if (stored !== 'false') doneCol.classList.add('col-collapsed');
    doneCol.querySelector('h2').addEventListener('click', function() {
      doneCol.classList.toggle('col-collapsed');
      localStorage.setItem('done-col-collapsed', doneCol.classList.contains('col-collapsed'));
    });
  })();
})();
</script>
</body>
</html>
"""

DETAIL_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⭐</text></svg>">
  <title>Task #{{ task.id }}</title>
  <style>
    """ + SHARED_CSS + """
    .content { padding: 1rem; max-width: 1200px; margin: 0 auto; }
    .back-link { font-size: 0.82rem; color: var(--text-muted); }
    .back-link:hover { color: var(--text); }
    .task-title { font-size: 1.25rem; font-weight: 700; margin: 0.5rem 0 0.25rem;
                  color: var(--text); line-height: 1.3; }
    .task-prompt { color: var(--text-muted); margin: 0 0 0.75rem; font-size: 0.88rem;
                   white-space: pre-wrap; line-height: 1.55; }
    pre { background: #1e2433; color: #e2e8f0; padding: 1rem; border-radius: var(--radius);
          overflow-x: auto; font-size: 0.78rem; white-space: pre-wrap; line-height: 1.6; }
    .actions { display: flex; gap: 0.5rem; margin: 0.75rem 0; flex-wrap: wrap; align-items: center; }
    .detail-cols { display: flex; gap: 1.25rem; align-items: flex-start; margin-top: 0.75rem; }
    .detail-left  { flex: 3; min-width: 0; }
    .detail-right { flex: 0 0 280px; min-width: 0; }
    .meta-card { background: var(--surface); border: 1px solid var(--border);
                 border-radius: var(--radius); padding: 0.9rem 1rem; margin-bottom: 0.75rem;
                 box-shadow: var(--shadow-sm); }
    .meta-card h3 { margin: 0 0 0.6rem; font-size: 0.68rem; text-transform: uppercase;
                    letter-spacing: .06em; color: var(--text-subtle); font-weight: 700; }
    .meta-grid { display: grid; grid-template-columns: auto 1fr; gap: 0.35rem 0.85rem;
                 font-size: 0.83rem; align-items: baseline; }
    .meta-key { color: var(--text-muted); font-size: 0.75rem; white-space: nowrap; }
    .meta-val { color: var(--text); }
    .meta-actions { display: flex; gap: 0.4rem; flex-wrap: wrap; margin-top: 0.6rem;
                    padding-top: 0.6rem; border-top: 1px solid var(--border); }
    .subtask-item { background: var(--surface-2); border: 1px solid var(--border);
                    border-radius: var(--radius-sm); padding: 6px 9px; margin-bottom: 5px;
                    font-size: 0.82rem; line-height: 1.4; }
    .status-icon { font-style: normal; margin-right: 0.3rem; }
    .status-icon.done    { color: #059669; }
    .status-icon.running { color: #2563eb; }
    .status-icon.review  { color: #d97706; }
    .status-icon.pending { color: #94a3b8; }
    .status-icon.blocked { color: #7c3aed; }
    .status-icon.stopped { color: #dc2626; }
    table.sessions { border-collapse: collapse; width: 100%; font-size: 0.78rem; }
    table.sessions th, table.sessions td { border: 1px solid var(--border);
                                           padding: 0.35rem 0.55rem; text-align: left; }
    table.sessions th { background: var(--surface-2); font-weight: 600;
                        color: var(--text-muted); font-size: 0.72rem;
                        text-transform: uppercase; letter-spacing: .04em; }
    table.sessions tr:nth-child(even) { background: var(--surface-2); }
    .edit-form { background: var(--surface); border: 1px solid var(--border);
                 border-radius: var(--radius); padding: 1rem; margin: 0.75rem 0;
                 display: none; box-shadow: var(--shadow-sm); }
    .edit-form input[type="text"], .edit-form textarea {
      width: 100%; padding: 0.45rem 0.65rem; border: 1px solid var(--border);
      border-radius: var(--radius-sm); font-size: 0.875rem; font-family: inherit;
      color: var(--text); margin-bottom: 0.4rem; background: var(--surface-2); }
    .edit-form input:focus, .edit-form textarea:focus { outline: none;
      border-color: #a5b4fc; box-shadow: 0 0 0 3px rgba(79,70,229,.1); }
    .edit-form textarea { resize: none; overflow: hidden; }
    .edit-form select { padding: 0.35rem 0.5rem; border-radius: var(--radius-sm);
                        border: 1px solid var(--border); font-size: 0.82rem;
                        background: var(--surface); }
    .edit-row { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;
                margin-top: 0.4rem; }
    .inline-label { font-size: 0.8rem; color: var(--text-muted); }
    .timestamp { color: var(--text-muted); font-size: 0.78rem; font-family: monospace; }
    .rate-limit-banner { background: #fef3c7; border: 1px solid #fbbf24;
                         border-radius: var(--radius); padding: 0.5rem 0.75rem;
                         font-size: 0.85rem; margin: 0.5rem 0; color: #92400e; }
    .plan-card { background: var(--surface); border: 1px solid var(--border);
                 border-radius: var(--radius); padding: 1rem; margin-bottom: 0.75rem;
                 box-shadow: var(--shadow-sm); }
    .plan-card h3 { margin: 0 0 0.5rem; font-size: 0.68rem; text-transform: uppercase;
                    letter-spacing: .06em; color: var(--text-subtle); font-weight: 700; }
    .plan-decision-badge { display: inline-flex; align-items: center; padding: 3px 11px;
                           border-radius: 20px; font-size: 0.78rem; font-weight: 700;
                           margin-bottom: 0.75rem; letter-spacing: .04em; }
    .plan-decision-execute  { background: #dbeafe; color: #1d4ed8; }
    .plan-decision-decompose { background: #f3e8ff; color: #6b21a8; }
    .plan-reasoning { font-size: 0.85rem; color: var(--text-muted); font-style: italic;
                      margin-bottom: 0.75rem; padding: 0.6rem 0.75rem;
                      background: var(--surface-2); border-left: 3px solid #a5b4fc;
                      border-radius: 0 var(--radius-sm) var(--radius-sm) 0; line-height: 1.6; }
    .plan-steps { font-size: 0.82rem; white-space: pre-wrap; margin: 0; font-family: inherit; }
    .subtask-tree { list-style: none; padding: 0; margin: 0; }
    .subtask-tree li { padding: 8px 10px; margin-bottom: 5px; background: #faf5ff;
                       border: 1px solid #e9d5ff; border-radius: var(--radius-sm);
                       font-size: 0.82rem; line-height: 1.4; }
    .subtask-tree li .stnum { font-weight: 700; color: #7c3aed; margin-right: 0.35rem; }
    .subtask-tree li .stdep { font-size: 0.72rem; color: var(--text-muted); margin-top: 0.2rem; }
    .artifact { margin: 0.5rem 0; padding: 0.6rem 0.75rem; border-radius: var(--radius-sm);
                font-size: 0.82rem; }
    .artifact-git-commit { font-family: monospace; background: #f0f9ff;
                           border: 1px solid #bae6fd; }
    .artifact-ref { font-weight: 700; color: #0284c7; margin-right: 0.4rem; }
    .artifact-text { background: var(--surface-2); border: 1px solid var(--border);
                     white-space: pre-wrap; }
    .artifact-document { background: var(--surface-2); border: 1px solid var(--border); }
    .artifact-document pre { margin: 0.4rem 0 0; background: transparent; color: var(--text);
                              padding: 0.4rem; }
    .artifact-doc-path { padding: 0.4rem; font-size: 0.82rem; color: var(--text-muted); }
    .artifact-code-diff { background: #1e2433; color: #e2e8f0; padding: 0.75rem;
                          border-radius: var(--radius-sm); font-size: 0.78rem;
                          overflow-x: auto; font-family: monospace; }
    .artifact-url-list { margin: 0.3rem 0 0; padding-left: 1.2rem; }
    .report-card { margin: 0.75rem 0; border: 1px solid #d1fae5;
                   border-radius: var(--radius); background: #f0fdf4; }
    .report-card summary { padding: 0.6rem 0.8rem; cursor: pointer; font-weight: 600;
                           color: #065f46; font-size: 0.88rem; }
    .report-card summary:hover { background: #dcfce7; border-radius: var(--radius); }
    .report-content { margin: 0; padding: 0.75rem; font-size: 0.85rem;
                      border-top: 1px solid #d1fae5; line-height: 1.65; }
    .report-content h1,.report-content h2,.report-content h3 { margin: 0.8rem 0 0.3rem;
                                                                 font-size: 1rem; }
    .report-content table { border-collapse: collapse; width: 100%; margin: 0.5rem 0; }
    .report-content th,.report-content td { border: 1px solid var(--border);
                                            padding: 0.3rem 0.5rem; }
    .report-content th { background: var(--surface-2); font-weight: 600; }
    .report-content code { background: var(--surface-2); padding: 0.1rem 0.3rem;
                           border-radius: 3px; font-size: 0.8rem; }
    .report-content pre { background: #1e2433; color: #e2e8f0; padding: 0.5rem;
                          border-radius: var(--radius-sm); overflow-x: auto; }
    .report-content ul,.report-content ol { padding-left: 1.5rem; }
    .inline-model-form { display: inline-flex; align-items: center; gap: 0.35rem; }
    .inline-model-form select { padding: 0.25rem 0.4rem; border-radius: var(--radius-sm);
                                 border: 1px solid var(--border); font-size: 0.78rem;
                                 background: var(--surface); }
    @media (max-width: 768px) {
      .content { padding: 0.75rem; }
      .detail-cols { flex-direction: column; }
      .detail-right { flex: 0 0 auto; width: 100%; }
    }
  </style>
</head>
<body>
""" + HEADER_HTML + """
<div class="content">
<a href="/" class="back-link">&larr; Board</a>
<h1 class="task-title">#{{ task.id }} &mdash; {{ task.get('title') or task.prompt }}</h1>
{% if task.get('title') and task.get('title') != task.prompt %}
<p class="task-prompt">{{ task.prompt }}</p>
{% endif %}

{% if task.get('rate_limited_at') %}
<div class="rate-limit-banner">
  Rate limited at {{ task.rate_limited_at[:19] }} &mdash; dispatcher will retry automatically.
</div>
{% endif %}

<div class="detail-cols">

  <div class="detail-left">

    <div class="edit-form" id="edit-form">
      <form method="post" action="/tasks/{{ task.id }}/edit">
        <p style="margin:0 0 0.6rem;font-weight:600;font-size:0.88rem">Edit Task</p>
        <input type="text" name="title" value="{{ task.get('title') or '' }}" placeholder="Title...">
        <textarea name="prompt" rows="3"
          oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'">{{ task.prompt }}</textarea>
        <div class="edit-row">
          <span class="inline-label">Priority</span>
          <select name="priority">
            <option value="high" {% if task.get('priority')=='high' %}selected{% endif %}>High</option>
            <option value="medium" {% if task.get('priority','medium')=='medium' %}selected{% endif %}>Medium</option>
            <option value="low" {% if task.get('priority')=='low' %}selected{% endif %}>Low</option>
          </select>
          <span class="inline-label">Plan</span>
          <select name="plan_model">
            {% set pm = task.get('plan_model', task.get('model','sonnet')) %}
            <option value="sonnet" {% if pm=='sonnet' %}selected{% endif %}>Sonnet</option>
            <option value="opus" {% if pm=='opus' %}selected{% endif %}>Opus</option>
            <option value="haiku" {% if pm=='haiku' %}selected{% endif %}>Haiku</option>
          </select>
          <span class="inline-label">Exec</span>
          <select name="exec_model">
            {% set em = task.get('exec_model', task.get('model','sonnet')) %}
            <option value="sonnet" {% if em=='sonnet' %}selected{% endif %}>Sonnet</option>
            <option value="opus" {% if em=='opus' %}selected{% endif %}>Opus</option>
            <option value="haiku" {% if em=='haiku' %}selected{% endif %}>Haiku</option>
          </select>
          <button class="btn btn-edit btn-sm" type="submit">Save</button>
          <button class="btn btn-sm btn-secondary" type="button"
            onclick="document.getElementById('edit-form').style.display='none'">Cancel</button>
        </div>
      </form>
    </div>

    <div class="actions">
      {% if task.status == 'plan_review' %}
      <form method="post" action="/tasks/{{ task.id }}/approve">
        <button class="btn btn-approve">Approve Plan</button>
      </form>
      <div id="reject-section-{{ task.id }}">
        <button class="btn btn-reject"
          onclick="document.getElementById('reject-expand-{{ task.id }}').style.display='block';this.style.display='none'">Reject</button>
        <div id="reject-expand-{{ task.id }}" style="display:none;margin-top:0.5rem">
          <form method="post" action="/tasks/{{ task.id }}/reject">
            <textarea name="feedback" placeholder="Rejection reason (optional)" rows="3"
              style="width:100%;padding:0.45rem 0.65rem;border:1px solid var(--border);border-radius:var(--radius-sm);resize:vertical;font-family:inherit;font-size:0.875rem;background:var(--surface-2);"></textarea>
            <div style="display:flex;gap:0.5rem;margin-top:0.4rem">
              <button class="btn btn-reject btn-sm">Submit Rejection</button>
              <button type="button" class="btn btn-sm btn-secondary"
                onclick="document.getElementById('reject-expand-{{ task.id }}').style.display='none';document.querySelector('#reject-section-{{ task.id }} > .btn-reject').style.display=''">Cancel</button>
            </div>
          </form>
        </div>
      </div>
      {% endif %}
      {% if task.status in ('planning', 'executing', 'plan_review') %}
      <form method="post" action="/tasks/{{ task.id }}/cancel" onsubmit="return confirm('Cancel this task?')">
        <button class="btn btn-cancel btn-sm">Cancel</button>
      </form>
      {% endif %}
      {% if task.status in ('stopped', 'done') %}
      <form method="post" action="/tasks/{{ task.id }}/retry">
        <button class="btn btn-retry btn-sm">Retry / Requeue</button>
      </form>
      {% endif %}
    </div>

    {% if task.get('plan') %}
    <h2 style="font-size:0.95rem;margin:0.75rem 0 0.4rem">Plan</h2>
    {% if plan_parsed %}
      <div class="plan-card">
        <span class="plan-decision-badge plan-decision-{{ plan_parsed.decision }}">
          {{ plan_parsed.decision | upper }}
        </span>
        {% if plan_parsed.get('reasoning') %}
        <div class="plan-reasoning">{{ plan_parsed.reasoning }}</div>
        {% endif %}
        {% if plan_parsed.decision == 'decompose' %}
          <h3>Subtask Tree ({{ plan_parsed.get('subtasks', [])|length }} tasks)</h3>
          <ul class="subtask-tree">
            {% for st in plan_parsed.get('subtasks', []) %}
            <li>
              <span class="stnum">#{{ loop.index }}</span>{{ st.prompt }}
              {% if st.get('depends_on') %}
              <div class="stdep">depends on: {% for d in st.depends_on %}#{{ d + 1 }}{% if not loop.last %}, {% endif %}{% endfor %}</div>
              {% endif %}
            </li>
            {% endfor %}
          </ul>
        {% else %}
          {% if plan_parsed.get('plan') %}
          <pre class="plan-steps">{{ plan_parsed.plan }}</pre>
          {% endif %}
        {% endif %}
      </div>
    {% else %}
    <pre>{{ task.plan }}</pre>
    {% endif %}
    {% endif %}

    {% if task.get('report') %}
    <h2 style="font-size:0.95rem;margin:0.75rem 0 0.4rem">Report</h2>
    <details class="report-card" open>
      <summary>Consolidated report from all subtasks</summary>
      <div class="report-content">{{ task.report | md }}</div>
    </details>
    {% endif %}

    {% set result_summary = (task.get('result') or {}).get('summary') or task.get('summary') %}
    {% if result_summary and result_summary != task.get('report') %}
    <h2 style="font-size:0.95rem;margin:0.75rem 0 0.4rem">Result Summary</h2>
    <div class="report-content" style="border:none;padding:0">{{ result_summary | md }}</div>
    {% endif %}

    {% set artifacts = (task.get('result') or {}).get('artifacts') or [] %}
    {% if artifacts %}
    <h2 style="font-size:0.95rem;margin:0.75rem 0 0.4rem">Artifacts</h2>
    {% for a in artifacts %}
      {% if a.get('type') == 'git_commit' %}
      <div class="artifact artifact-git-commit"><span class="artifact-ref">{{ a.ref[:8] }}</span> {{ a.get('message','') }}</div>
      {% elif a.get('type') == 'text' %}
      <div class="artifact artifact-text">{{ a.content }}</div>
      {% elif a.get('type') == 'document' %}
      <details class="artifact artifact-document"><summary>{{ a.get('title','Document') }}</summary>{% if a.get('content') %}<pre>{{ a.content }}</pre>{% elif a.get('path') %}<div class="artifact-doc-path">📄 <code>{{ a.path }}</code></div>{% endif %}</details>
      {% elif a.get('type') == 'code_diff' %}
      <pre class="artifact artifact-code-diff">{{ a.content }}</pre>
      {% elif a.get('type') == 'url_list' %}
      <ul class="artifact artifact-url-list">{% for item in a.get('items',[]) %}{% set url = item.get('url','') if item is mapping else item %}{% set title = item.get('title', url) if item is mapping else url %}<li>{% if url.startswith('http://') or url.startswith('https://') %}<a href="{{ url }}" target="_blank" rel="noopener noreferrer">{{ title or url }}</a>{% if item is mapping and item.get('note') %} — {{ item.note }}{% endif %}{% else %}{{ url }}{% endif %}</li>{% endfor %}</ul>
      {% else %}
      <div class="artifact" style="background:var(--surface-2);border:1px solid var(--border)">{{ a | tojson }}</div>
      {% endif %}
    {% endfor %}
    {% endif %}

  </div>{# end .detail-left #}

  <div class="detail-right">

    <div class="meta-card">
      <h3>Details</h3>
      <div style="margin-bottom:0.6rem">
        <span class="state-badge state-{{ task.status }}">{{ task.status }}</span>
        {% if task.get('stop_reason') %}<span class="state-badge state-stopped" style="margin-left:0.3rem">{{ task.stop_reason }}</span>{% endif %}
        {% if task.get('pushed_at') %}<span class="state-badge state-done" style="margin-left:0.3rem">pushed</span>{% endif %}
      </div>
      <div class="meta-grid">
        <span class="meta-key">Priority</span>
        <span class="meta-val">{{ task.get('priority','medium') }}</span>
        {% set pm = task.get('plan_model', task.get('model','sonnet')) %}
        {% set em = task.get('exec_model', task.get('model','sonnet')) %}
        <span class="meta-key">Models</span>
        <span class="meta-val">
          <form method="post" action="/tasks/{{ task.id }}/set-model" class="inline-model-form">
            <span style="font-size:0.75rem;color:var(--text-muted)">P</span>
            <select name="plan_model" onchange="this.form.submit()">
              <option value="sonnet" {% if pm=='sonnet' %}selected{% endif %}>Sonnet</option>
              <option value="opus" {% if pm=='opus' %}selected{% endif %}>Opus</option>
              <option value="haiku" {% if pm=='haiku' %}selected{% endif %}>Haiku</option>
            </select>
            <span style="font-size:0.75rem;color:var(--text-muted)">E</span>
            <select name="exec_model" onchange="this.form.submit()">
              <option value="sonnet" {% if em=='sonnet' %}selected{% endif %}>Sonnet</option>
              <option value="opus" {% if em=='opus' %}selected{% endif %}>Opus</option>
              <option value="haiku" {% if em=='haiku' %}selected{% endif %}>Haiku</option>
            </select>
          </form>
        </span>
        <span class="meta-key">Auto-approve</span>
        <span class="meta-val">
          <form method="post" action="/tasks/{{ task.id }}/set-auto-approve" style="display:inline">
            <input type="hidden" name="auto_approve" value="0">
            <input type="checkbox" name="auto_approve" value="1"
              {% if task.get('auto_approve') %}checked{% endif %}
              onchange="this.form.submit()" style="accent-color:#4f46e5;cursor:pointer">
          </form>
        </span>
        {% if task.get('parent') %}
        <span class="meta-key">Parent</span>
        <span class="meta-val"><a href="/tasks/{{ task.parent }}">#{{ task.parent }}</a></span>
        {% endif %}
        {% if task.get('created_at') %}
        <span class="meta-key">Created</span>
        <span class="timestamp">{{ task.created_at[:19] }}</span>
        {% endif %}
        {% if task.get('completed_at') %}
        <span class="meta-key">Completed</span>
        <span class="timestamp">{{ task.completed_at[:19] }}</span>
        {% endif %}
        {% if task.get('pushed_at') %}
        <span class="meta-key">Pushed</span>
        <span class="timestamp">{{ task.pushed_at[:19] }}</span>
        {% endif %}
      </div>
      <div class="meta-actions">
        {% if task.status != 'in_progress' %}
        <button class="btn btn-edit btn-sm"
          onclick="var f=document.getElementById('edit-form');f.style.display='block';var ta=f.querySelector('textarea');ta.style.height='auto';ta.style.height=ta.scrollHeight+'px';f.scrollIntoView({behavior:'smooth',block:'start'})">Edit</button>
        {% endif %}
        {% if task.status != 'in_progress' %}
        <form method="post" action="/tasks/{{ task.id }}/delete"
          onsubmit="return confirm('Delete task #{{ task.id }}? This cannot be undone.')">
          <button class="btn btn-delete btn-sm">Delete</button>
        </form>
        {% endif %}
        {% if task.get('hidden') %}
        <form method="post" action="/tasks/{{ task.id }}/unhide">
          <button class="btn btn-sm btn-secondary">Unhide</button>
        </form>
        {% else %}
        <form method="post" action="/tasks/{{ task.id }}/hide">
          <button class="btn btn-sm btn-secondary">Hide</button>
        </form>
        {% endif %}
      </div>
    </div>

    {% if subtasks %}
    <div class="meta-card">
      <h3>Subtasks ({{ subtasks|length }})</h3>
      {% for s in subtasks %}
      {% set _blocked = s.get('blocked_on') and s.blocked_on|length > 0 %}
      {% if _blocked %}
        {% set _icon = '⊟' %}{% set _cls = 'blocked' %}
      {% elif s.status == 'done' %}
        {% set _icon = '✓' %}{% set _cls = 'done' %}
      {% elif s.status == 'in_progress' %}
        {% set _icon = '⟳' %}{% set _cls = 'running' %}
      {% elif s.status == 'plan_review' %}
        {% set _icon = '●' %}{% set _cls = 'review' %}
      {% elif s.status == 'stopped' %}
        {% set _icon = '⊘' %}{% set _cls = 'stopped' %}
      {% else %}
        {% set _icon = '○' %}{% set _cls = 'pending' %}
      {% endif %}
      <div class="subtask-item">
        <i class="status-icon {{ _cls }}">{{ _icon }}</i>
        <a href="/tasks/{{ s.id }}">#{{ s.id }}</a>
        {% if _blocked %}<span style="color:var(--text-muted);font-size:0.75rem"> blocked(#{{ s.blocked_on|join(', #') }})</span>{% endif %}
        <span style="color:var(--text-muted)"> {{ s.prompt[:80] }}</span>
      </div>
      {% endfor %}
    </div>
    {% endif %}

    {% if task.get('sessions') %}
    <div class="meta-card">
      <h3>Run History</h3>
      <table class="sessions">
        <thead>
          <tr><th>#</th><th>Started</th><th>Dur</th><th>Result</th></tr>
        </thead>
        <tbody>
          {% for s in task.sessions %}
          {% set rc = s.get('exit_code', '?') %}
          {% set rl = s.get('rate_limited') %}
          <tr>
            <td>{{ loop.index }}</td>
            <td>{{ s.get('started_at') | pt }}</td>
            <td>
              {% set dur = s.get('duration_s', 0) %}
              {% if dur >= 3600 %}{{ (dur // 3600) }}h{{ ((dur % 3600) // 60) }}m
              {% elif dur >= 60 %}{{ (dur // 60) }}m{{ (dur % 60) }}s
              {% else %}{{ dur }}s{% endif %}
            </td>
            <td>
              {% if rl %}⚠ rate limited
              {% elif rc == 0 %}✓ ok
              {% else %}✗ exit {{ rc }}{% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% endif %}

  </div>{# end .detail-right #}

</div>{# end .detail-cols #}

</div>
<script>
(function() {
  const POLL_MS = 3000;
  const TASK_ID = {{ task.id }};
  let lastStatus = '{{ task.status }}';

  function esc(s) {
    const d = document.createElement('div'); d.textContent = s; return d.innerHTML;
  }

  function poll() {
    fetch('/api/tasks').then(r => r.json()).then(function(data) {
      const task = data.tasks.find(t => t.id === TASK_ID);
      if (task && task.status !== lastStatus) {
        // Status changed — reload the page to get fresh server-rendered content
        location.reload();
        return;
      }
      // Update dispatcher status in header
      const d = data.dispatcher;
      const el = document.getElementById('dispatcher-status');
      if (el && d) {
        const dot = d.state || 'idle';
        const label = d.label || dot;
        el.innerHTML = '<span class="status-dot status-' + dot + '"></span><span style="color:#ccc;font-size:0.8rem">' + esc(label) + '</span>';
      }
      // Review badge
      const reviewEl = document.getElementById('review-badge');
      if (reviewEl) {
        const n = data.tasks.filter(t => t.status === 'plan_review').length;
        reviewEl.textContent = n > 0 ? '\u2691 ' + n : '';
        reviewEl.style.display = n > 0 ? 'inline-block' : 'none';
      }
    }).catch(() => {});
    setTimeout(poll, POLL_MS);
  }
  setTimeout(poll, POLL_MS);
})();
</script>
</body>
</html>
"""

PROGRESS_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⭐</text></svg>">
  <title>Progress Log</title>
  <style>
    """ + SHARED_CSS + """
    .content { padding: 1rem; max-width: 960px; margin: 0 auto; }
    pre { background: #1e2433; color: #e2e8f0; padding: 1rem; border-radius: var(--radius);
          overflow-x: auto; font-size: 0.78rem; white-space: pre-wrap; line-height: 1.6; }
    .empty { color: var(--text-subtle); font-style: italic; }
  </style>
</head>
<body>
""" + HEADER_HTML + """
<div class="content">
<h1 style="font-size:1.1rem;margin:0.75rem 0">Agent Task Log</h1>
{% if content %}
<pre>{{ content }}</pre>
{% else %}
<p class="empty">No progress entries yet.</p>
{% endif %}
</div>
</body>
</html>
"""

LOG_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⭐</text></svg>">
  <title>Git Log</title>
  <style>
    """ + SHARED_CSS + """
    .content { padding: 1rem; max-width: 960px; margin: 0 auto; }
    pre { background: #1e2433; color: #e2e8f0; padding: 1rem; border-radius: var(--radius);
          overflow-x: auto; font-size: 0.78rem; white-space: pre-wrap; line-height: 1.6; }
    .empty { color: var(--text-subtle); font-style: italic; }
  </style>
</head>
<body>
""" + HEADER_HTML + """
<div class="content">
<h1 style="font-size:1.1rem;margin:0.75rem 0">Recent Git Log</h1>
{% if log_output %}
<pre>{{ log_output }}</pre>
{% else %}
<p class="empty">No git history available (not a git repo or no commits).</p>
{% endif %}
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_dispatcher_status() -> dict:
    """Read dispatcher status from file, falling back to idle."""
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"state": "idle", "label": "Idle"}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# Pipeline columns: (status, label, icon)
# Icons mark human-gated states that need your action
PIPELINE_COLS = [
    ("pending",      "Pending",     ""),
    ("planning",     "Planning",    ""),
    ("plan_review",  "Review Plan", "\u270b"),
    ("executing",    "Executing",   ""),
    ("done",         "Done",        ""),
]


LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Sign in — Xingent</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body { font-family: system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
           background: #f1f5f9; display: flex; align-items: center;
           justify-content: center; min-height: 100vh; margin: 0;
           -webkit-font-smoothing: antialiased; }
    .box { background: #fff; padding: 2rem 2.25rem; border-radius: 12px;
           box-shadow: 0 4px 24px rgba(15,23,42,.1), 0 1px 4px rgba(15,23,42,.06);
           width: 340px; max-width: calc(100vw - 2rem); }
    .logo { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1.5rem; }
    .logo-icon { width: 32px; height: 32px; background: #1a1a2e; border-radius: 8px;
                 display: flex; align-items: center; justify-content: center;
                 font-size: 1rem; }
    .logo h1 { margin: 0; font-size: 1.15rem; font-weight: 700; color: #0f172a; }
    .field { margin-bottom: 0.85rem; }
    .field label { display: block; font-size: 0.8rem; font-weight: 600;
                   color: #64748b; margin-bottom: 0.3rem; }
    .field input { width: 100%; padding: 0.55rem 0.75rem;
                   border: 1px solid #e2e8f0; border-radius: 6px;
                   font-size: 0.9rem; font-family: inherit; color: #0f172a;
                   background: #f8fafc; transition: border-color .15s, box-shadow .15s; }
    .field input:focus { outline: none; border-color: #a5b4fc;
                         box-shadow: 0 0 0 3px rgba(79,70,229,.1);
                         background: #fff; }
    button[type="submit"] { width: 100%; padding: 0.65rem; background: #4f46e5;
                            color: #fff; border: none; border-radius: 6px;
                            font-size: 0.9rem; font-weight: 600; cursor: pointer;
                            font-family: inherit; margin-top: 0.25rem;
                            transition: filter .15s; }
    button[type="submit"]:hover { filter: brightness(.88); }
    .error { color: #dc2626; font-size: 0.82rem; margin-bottom: 0.75rem;
             padding: 0.45rem 0.65rem; background: #fee2e2; border-radius: 5px; }
  </style>
</head>
<body>
  <div class="box">
    <div class="logo">
      <div class="logo-icon">⭐</div>
      <h1>Xingent</h1>
    </div>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <form method="post">
      <div class="field">
        <label for="username">Username</label>
        <input type="text" id="username" name="username" placeholder="your username" autofocus>
      </div>
      <div class="field">
        <label for="password">Password</label>
        <input type="password" id="password" name="password" placeholder="••••••••">
      </div>
      <button type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>"""


@app.get("/login")
@app.post("/login")
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        accounts = _load_accounts()
        user = accounts.get(username)
        if user and check_password_hash(user["password_hash"], password):
            session["username"] = username
            session["account"] = user["account"]
            return redirect(url_for("board"))
        error = "Invalid username or password."
        if not accounts:
            error = "No accounts configured. Run: python3 add_account.py USERNAME ACCOUNT"
    return render_template_string(LOGIN_HTML, error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def board():
    data = load_tasks()
    show_hidden = request.args.get("show_hidden", "0") == "1"
    acc = session["account"]
    tasks = [t for t in data["tasks"] if t.get("account", DEFAULT_ACCOUNT) == acc]
    if not show_hidden:
        tasks = [t for t in tasks if not t.get("hidden")]
    return render_template_string(
        BOARD_HTML, tasks=tasks, pipeline_cols=PIPELINE_COLS, show_hidden=show_hidden,
        pipeline_cols_json=json.dumps(PIPELINE_COLS),
    )


@app.post("/tasks")
def add_task():
    """Create a new task with a full schema — all fields initialized upfront.
    This ensures the dispatcher, dependency graph, and web UI never encounter
    missing keys. Uses locked_update + next_id for atomic ID allocation."""
    title = request.form.get("title", "").strip()
    prompt = request.form.get("prompt", "").strip()
    priority = request.form.get("priority", "medium")
    plan_model = request.form.get("plan_model", "sonnet")
    exec_model = request.form.get("exec_model", "sonnet")
    auto_approve = request.form.get("auto_approve") == "1"
    if priority not in ("high", "medium", "low"):
        priority = "medium"
    if plan_model not in ("sonnet", "opus", "haiku"):
        plan_model = "sonnet"
    if exec_model not in ("sonnet", "opus", "haiku"):
        exec_model = "sonnet"
    if not title:
        return redirect(url_for("board"))
    if not prompt:
        prompt = title  # agent uses title as description if no detail provided

    acc = session["account"]
    new_task = {}

    def mutate(data):
        task = {
            "id": next_id(data),
            "status": "pending",
            "title": title,
            "prompt": prompt,
            "priority": priority,
            "plan_model": plan_model,
            "exec_model": exec_model,
            "auto_approve": auto_approve,
            "account": acc,
            "parent": None,
            "depth": 0,
            "blocked_on": [],
            "depends_on": [],
            "dependents": [],
            "children": [],
            "unresolved_children": 0,
            "plan": None,
            "report": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "summary": None,
            "rejection_comments": [],
        }
        data["tasks"].append(task)
        new_task.update(task)

    locked_update(mutate)
    log_progress(new_task["id"], "created", f"priority={priority}, prompt={prompt[:80]}")
    return redirect(url_for("board"))


@app.get("/tasks/<int:task_id>")
def task_detail(task_id: int):
    task, err = _check_owner(task_id)
    if err:
        return err
    data = load_tasks()
    subtasks = [t for t in data["tasks"] if t.get("parent") == task_id]
    plan_parsed = None
    if task.get("plan"):
        try:
            plan_parsed = json.loads(task["plan"])
            if not isinstance(plan_parsed, dict) or plan_parsed.get("decision") not in ("execute", "decompose"):
                plan_parsed = None
        except (json.JSONDecodeError, TypeError):
            plan_parsed = None
    return render_template_string(DETAIL_HTML, task=task, subtasks=subtasks, plan_parsed=plan_parsed)


@app.post("/tasks/<int:task_id>/edit")
def edit_task(task_id: int):
    """Edit a task's prompt, priority, and models. Blocked for in_progress tasks
    to avoid mutating a task the dispatcher is actively running."""
    _, err = _check_owner(task_id)
    if err:
        return err
    title = request.form.get("title", "").strip()
    prompt = request.form.get("prompt", "").strip()
    priority = request.form.get("priority", "medium")
    plan_model = request.form.get("plan_model", "sonnet")
    exec_model = request.form.get("exec_model", "sonnet")
    if plan_model not in ("sonnet", "opus", "haiku"):
        plan_model = "sonnet"
    if exec_model not in ("sonnet", "opus", "haiku"):
        exec_model = "sonnet"

    changed = False

    def mutate(data):
        nonlocal changed
        for t in data["tasks"]:
            if t["id"] == task_id and t["status"] != "in_progress":
                if title:
                    t["title"] = title
                if prompt:
                    t["prompt"] = prompt
                t["priority"] = priority
                t["plan_model"] = plan_model
                t["exec_model"] = exec_model
                changed = True
                break

    locked_update(mutate)
    if changed:
        log_progress(task_id, "edited", f"priority={priority}, plan={plan_model}, exec={exec_model}")
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/set-priority")
def set_priority(task_id: int):
    _, err = _check_owner(task_id)
    if err:
        return err
    priority = request.form.get("priority", "medium")
    if priority not in ("high", "medium", "low"):
        priority = "medium"

    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["priority"] = priority
                break

    locked_update(mutate)
    log_progress(task_id, "priority changed", priority)
    return redirect(request.referrer or url_for("board"))


@app.post("/tasks/<int:task_id>/set-auto-approve")
def set_auto_approve(task_id: int):
    _, err = _check_owner(task_id)
    if err:
        return err
    auto_approve = request.form.get("auto_approve") == "1"

    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["auto_approve"] = auto_approve
                break

    locked_update(mutate)
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/set-model")
def set_model(task_id: int):
    _, err = _check_owner(task_id)
    if err:
        return err
    plan_model = request.form.get("plan_model", "")
    exec_model = request.form.get("exec_model", "")
    valid = ("sonnet", "opus", "haiku")

    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id:
                if plan_model in valid:
                    t["plan_model"] = plan_model
                if exec_model in valid:
                    t["exec_model"] = exec_model
                break

    locked_update(mutate)
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/delete")
def delete_task(task_id: int):
    """Delete a task unless it's currently in_progress (safety guard).
    Uses a closure flag to only log if the task was actually removed — this
    pattern prevents phantom log entries from stale form resubmissions."""
    _, err = _check_owner(task_id)
    if err:
        return err
    deleted = False

    def mutate(data):
        nonlocal deleted
        before = len(data["tasks"])
        data["tasks"] = [t for t in data["tasks"] if t["id"] != task_id or t["status"] == "in_progress"]
        deleted = len(data["tasks"]) < before

    locked_update(mutate)
    if deleted:
        log_progress(task_id, "deleted")
        artifact_dir = WORKSPACE / "agent_log" / "tasks" / f"task_{task_id}"
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
    return redirect(url_for("board"))


@app.post("/tasks/<int:task_id>/approve")
def approve_task(task_id: int):
    """Approve a plan that's in review. Two outcomes:
    - 'execute' decision: set task to 'executing' (dispatcher picks it up for Docker execution)
    - 'decompose' decision: create subtasks, set parent to 'decomposed'
    NOTE: The decompose logic here mirrors dispatcher._approve_decompose — keep in sync.
    """
    _, err = _check_owner(task_id)
    if err:
        return err

    def mutate(data):
        task = next((t for t in data["tasks"] if t["id"] == task_id), None)
        if task is None or task["status"] != "plan_review":
            return

        decision = {}
        try:
            if task.get("plan"):
                decision = json.loads(task["plan"])
        except (json.JSONDecodeError, TypeError):
            pass

        if decision.get("decision") == "decompose":
            subtask_defs = decision.get("subtasks") or []
            n = len(subtask_defs)
            abs_ids = [next_id(data) for _ in range(n)]
            now = datetime.now(timezone.utc).isoformat()

            # First pass: create all subtask records
            for i, s in enumerate(subtask_defs):
                abs_depends = [abs_ids[j] for j in s.get("depends_on", []) if 0 <= j < n]
                data["tasks"].append({
                    "id": abs_ids[i],
                    "status": "pending",
                    "title": s.get("title") or s["prompt"][:60],
                    "prompt": s["prompt"],
                    "priority": task.get("priority", "medium"),
                    "plan_model": task.get("plan_model", "sonnet"),
                    "exec_model": task.get("exec_model", "sonnet"),
                    "auto_approve": task.get("auto_approve", False),
                    "account": task.get("account", DEFAULT_ACCOUNT),
                    "parent": task_id,
                    "depth": (task.get("depth") or 0) + 1,
                    "depends_on": abs_depends,
                    "blocked_on": list(abs_depends),
                    "dependents": [],
                    "children": [],
                    "unresolved_children": 0,
                    "plan": None,
                    "report": None,
                    "created_at": now,
                    "completed_at": None,
                    "summary": None,
                    "rejection_comments": [],
                })

            # Second pass: wire reverse index — for each dependency, record who depends on it
            task_map = {t["id"]: t for t in data["tasks"]}
            for i, s in enumerate(subtask_defs):
                for j in s.get("depends_on", []):
                    if 0 <= j < n:
                        dep_task = task_map.get(abs_ids[j])
                        if dep_task is not None:
                            dep_task["dependents"].append(abs_ids[i])

            task["status"] = "decomposed"
            task["children"] = abs_ids
            task["unresolved_children"] = n
        else:
            task["status"] = "executing"

    locked_update(mutate)
    log_progress(task_id, "plan approved")
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/reject")
def reject_task(task_id: int):
    """Reject a plan and send it back for re-planning.
    Clears the plan, appends the rejection feedback to the task's history,
    and resets status to 'pending'. The dispatcher will re-plan with the
    accumulated rejection comments as additional context for the model."""
    _, err = _check_owner(task_id)
    if err:
        return err
    feedback = request.form.get("feedback", "").strip()

    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id and t["status"] == "plan_review":
                comments = t.get("rejection_comments") or []
                comments.append({
                    "round": len(comments) + 1,
                    "comment": feedback,
                })
                t["rejection_comments"] = comments
                t["status"] = "pending"
                t["plan"] = None
                break

    locked_update(mutate)
    log_progress(task_id, "plan rejected", feedback or "")
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/cancel")
def cancel_task(task_id: int):
    """Stop an active task. Only applies to planning, executing, or plan_review — the
    dispatcher may be mid-execution when this fires, but the status change
    prevents it from being picked up again on the next iteration."""
    _, err = _check_owner(task_id)
    if err:
        return err
    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id and t["status"] in ("planning", "executing", "plan_review"):
                t["status"] = "stopped"
                t["stop_reason"] = "cancelled"
                t["summary"] = (t.get("summary") or "") + "\nCancelled by user via Web UI."
                break

    locked_update(mutate)
    log_progress(task_id, "cancelled by user")
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/retry")
def retry_task(task_id: int):
    """Requeue a stopped or done task back to pending for a fresh attempt.
    Clears all execution state so the task starts clean: plan, summary,
    rejection history, stop reason, retry count, and push timestamp.
    Critical: retry_count must be reset to 0 or the doom loop guard in
    execute_task will incorrectly count prior attempts against the new run."""
    _, err = _check_owner(task_id)
    if err:
        return err
    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id and t["status"] in ("stopped", "done"):
                t["status"] = "pending"
                t["completed_at"] = None
                t["summary"] = None
                t["result"] = None
                t["report"] = None
                t["plan"] = None
                t["rejection_comments"] = []
                t["retry_count"] = 0
                t.pop("stop_reason", None)
                t.pop("pushed_at", None)
                break

    locked_update(mutate)
    log_progress(task_id, "requeued (retry)")
    return redirect(url_for("task_detail", task_id=task_id))



@app.post("/tasks/<int:task_id>/hide")
def hide_task(task_id: int):
    _, err = _check_owner(task_id)
    if err:
        return err
    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["hidden"] = True
                break

    locked_update(mutate)
    return redirect(url_for("board"))


@app.post("/tasks/<int:task_id>/unhide")
def unhide_task(task_id: int):
    _, err = _check_owner(task_id)
    if err:
        return err
    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id:
                t.pop("hidden", None)
                break

    locked_update(mutate)
    return redirect(url_for("board", show_hidden="1"))


@app.get("/progress")
def progress():
    content = ""
    if PROGRESS_FILE.exists():
        content = PROGRESS_FILE.read_text()
    return render_template_string(PROGRESS_HTML, content=content)


@app.get("/log")
def git_log():
    log_output = ""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--graph", "--decorate", "-30"],
            cwd=str(WORKSPACE),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            log_output = result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return render_template_string(LOG_HTML, log_output=log_output)


@app.get("/api/tasks")
def api_tasks():
    """Return tasks for the current account + dispatcher status as JSON for live AJAX polling."""
    data = load_tasks()
    acc = session["account"]
    tasks = [t for t in data["tasks"] if t.get("account", DEFAULT_ACCOUNT) == acc]
    return jsonify({"tasks": tasks, "dispatcher": _read_dispatcher_status()})


@app.get("/status")
def dispatcher_status():
    """Return dispatcher status as JSON.

    The dispatcher writes its state to a small JSON file so the Web UI can
    display it.  If the file doesn't exist we report "idle".
    """
    return jsonify(_read_dispatcher_status())


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    use_reloader = os.environ.get("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False, use_reloader=use_reloader)
