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
  POST /tasks/<id>/approve-push  - approve push -> dispatcher pushes -> done (with pushed_at)
  POST /tasks/<id>/reject-push   - reject push -> done (local commit only)
  GET  /progress                 - view PROGRESS.md
  GET  /log                      - view recent git log
  GET  /status                   - dispatcher status (JSON)
"""

import json
import os
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
    body { font-family: system-ui, sans-serif; margin: 0; background: #f5f5f5; }
    header { background: #1a1a2e; color: #fff; padding: 1rem 1.5rem; display: flex;
             align-items: center; justify-content: space-between; }
    header h1 { margin: 0; font-size: 1.2rem; }
    header nav { display: flex; gap: 1rem; align-items: center; }
    header nav a { color: #ccc; text-decoration: none; font-size: 0.85rem; }
    header nav a:hover { color: #fff; }
    .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
                  margin-right: 0.3rem; vertical-align: middle; }
    #review-badge { display: inline-block; background: #f59e0b; color: #fff;
                    font-size: 0.75rem; font-weight: 700; padding: 0.15rem 0.5rem;
                    border-radius: 4px; }
    .status-running { background: #22c55e; }
    .status-sleeping { background: #f59e0b; }
    .status-idle { background: #94a3b8; }
    a { color: #1a1a2e; }
    .btn { padding: 0.5rem 1rem; border-radius: 6px; border: none; cursor: pointer;
           font-size: 0.85rem; text-decoration: none; display: inline-block; }
    .btn-approve { background: #16a34a; color: #fff; }
    .btn-reject  { background: #dc2626; color: #fff; }
    .btn-edit    { background: #2563eb; color: #fff; }
    .btn-delete  { background: #7f1d1d; color: #fff; }
    .btn-cancel  { background: #d97706; color: #fff; }
    .btn-retry   { background: #0891b2; color: #fff; }
    .btn-sm      { padding: 0.3rem 0.6rem; font-size: 0.75rem; }
    .state-badge { display: inline-block; padding: 0.2rem 0.5rem; border-radius: 4px;
                   font-size: 0.75rem; font-weight: 600; }
    .state-pending      { background: #e0e7ff; color: #3730a3; }
    .state-in_progress  { background: #dbeafe; color: #1d4ed8; }
    .state-plan_review  { background: #fef3c7; color: #92400e; }
    .state-push_review  { background: #fef3c7; color: #92400e; }
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
    <span id="review-badge"></span>
    <span id="dispatcher-status"></span>
    <span style="color:#888;font-size:0.8rem">{{ username }}</span>
    <a href="/logout" style="font-size:0.8rem">Sign out</a>
  </nav>
</header>
<script>
fetch('/status').then(r=>r.json()).then(d=>{
  const el=document.getElementById('dispatcher-status');
  const dot=d.state||'idle';
  const label=d.label||dot;
  el.innerHTML='<span class="status-dot status-'+dot+'"></span><span style="color:#ccc;font-size:0.8rem">'+label+'</span>';
}).catch(()=>{});
</script>
"""

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

BOARD_HTML = """
{% macro priority_select(t) %}<form method="post" action="/tasks/{{ t.id }}/set-priority" style="margin:0;display:inline"><select name="priority" onchange="this.form.submit()" class="prio-select prio-sel-{{ t.get('priority','medium') }}" style="padding:0.1rem 0.2rem;border-radius:4px;border:1px solid #ccc;font-size:0.65rem;font-weight:600;cursor:pointer"><option value="high" {% if t.get('priority')=='high' %}selected{% endif %}>high</option><option value="medium" {% if t.get('priority','medium')=='medium' %}selected{% endif %}>medium</option><option value="low" {% if t.get('priority')=='low' %}selected{% endif %}>low</option></select></form>{% endmacro %}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⭐</text></svg>">
  <title>ClaudeXingCode Dashboard</title>
  <style>
    """ + SHARED_CSS + """
    /* --- Pipeline section --- */
    .section-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em;
                     color: #999; padding: 0.75rem 1rem 0; font-weight: 600; }
    .pipeline { display: flex; gap: 0.75rem; padding: 0 1rem 0.5rem; overflow-x: auto; align-items: stretch; }
    .col { background: #fff; border-radius: 8px; min-width: 180px; flex: 1;
           padding: 0.6rem; border-top: 3px solid #e5e7eb; }
    .col-pending     { border-top-color: #818cf8; }
    .col-in_progress { border-top-color: #3b82f6; }
    .col-plan_review { border-top-color: #f59e0b; }
    .col-push_review { border-top-color: #f59e0b; }
    .col-done        { border-top-color: #22c55e; }
    .col-stopped     { border-top-color: #ef4444; }
    .col-decomposed  { border-top-color: #a855f7; }
    .col h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em;
               color: #666; margin: 0 0 0.6rem; display: flex; align-items: center; gap: 0.4rem; }
    .col h2 .count { font-weight: normal; color: #aaa; }
    .col h2 .gate-icon { font-size: 0.7rem; }
    .card { background: #f9f9f9; border: 1px solid #e0e0e0; border-radius: 6px;
            padding: 0.5rem; margin-bottom: 0.4rem; font-size: 0.82rem; }
    .card a { color: #1a1a2e; text-decoration: none; font-weight: 600; }
    .card .meta { color: #888; font-size: 0.72rem; margin-top: 0.25rem;
                  display: flex; flex-wrap: wrap; gap: 0.3rem; align-items: center; }
    .badge { display: inline-block; padding: 0.1rem 0.35rem; border-radius: 4px;
             font-size: 0.65rem; font-weight: 600; }
    .badge-high { background: #fee2e2; color: #b91c1c; }
    .badge-medium { background: #fef9c3; color: #92400e; }
    .badge-low { background: #dcfce7; color: #166534; }
    .badge-model { background: #e0e7ff; color: #3730a3; }
    .badge-auto { background: #d1fae5; color: #065f46; }
    .badge-blocked { background: #fde68a; color: #92400e; }
    .reason-tag { display: inline-block; padding: 0.1rem 0.35rem; border-radius: 4px;
                  font-size: 0.65rem; font-weight: 600; background: #fecaca; color: #991b1b; }
    .pushed-tag { display: inline-block; padding: 0.1rem 0.35rem; border-radius: 4px;
                  font-size: 0.65rem; font-weight: 600; background: #d1fae5; color: #065f46; }
    /* --- Off-ramp row --- */
    .offramp { display: flex; gap: 1rem; padding: 0 1rem 1rem; }
    .offramp .col { flex: none; min-width: 250px; max-width: 350px; }
    /* --- Add form --- */
    form.add { padding: 0 1rem 0.75rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }
    form.add textarea { flex: 1; min-width: 200px; padding: 0.5rem; border-radius: 6px;
                        border: 1px solid #ccc; font-size: 0.9rem; resize: none; overflow: hidden; }
    form.add select, form.add button { padding: 0.5rem; border-radius: 6px; border: 1px solid #ccc; }
    form.add button { background: #1a1a2e; color: #fff; border: none; cursor: pointer; }
    .toolbar { padding: 0 1rem 0.5rem; display: flex; gap: 0.5rem; align-items: center; }
    .btn-hide { background: none; border: 1px solid #ccc; border-radius: 4px; padding: 0.15rem 0.4rem;
                font-size: 0.65rem; color: #888; cursor: pointer; }
    .btn-hide:hover { background: #f0f0f0; }
    .card.hidden-card { opacity: 0.5; }
    .col-done h2 { cursor: pointer; user-select: none; }
    .col-done h2::after { content: ' ▾'; font-size: 1.1rem; color: #aaa; }
    .col-done.col-collapsed h2::after { content: ' ▸'; }
    .col-done.col-collapsed > :not(h2) { display: none; }
    /* Human-attention: review columns */
    .col-plan_review, .col-push_review { background: #fffbeb; }
    .col-plan_review h2, .col-push_review h2 { color: #92400e; }
    .card-review { border-color: #f59e0b !important; background: #fffbeb !important; }
    .btn-review { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px;
                  background: #f59e0b; color: #fff; font-size: 0.7rem; font-weight: 700;
                  text-decoration: none; border: none; cursor: pointer; }
    /* Action Required banner */
    #review-banner { display: none; margin: 0 1rem 0.75rem; padding: 0.6rem 1rem;
                     background: #fef3c7; border: 1px solid #f59e0b; border-radius: 6px;
                     font-size: 0.85rem; color: #92400e; font-weight: 600; }
    #review-banner a { color: #92400e; }
    .prio-sel-high { background: #fee2e2 !important; color: #b91c1c !important; }
    .prio-sel-medium { background: #fef9c3 !important; color: #92400e !important; }
    .prio-sel-low { background: #dcfce7 !important; color: #166534 !important; }
  </style>
</head>
<body>
""" + HEADER_HTML + """

<form class="add" method="post" action="/tasks">
  <input type="text" name="title" placeholder="Title..." required style="flex-basis:100%;padding:0.5rem;border-radius:6px;border:1px solid #ccc;font-size:0.9rem">
  <textarea name="prompt" rows="1" placeholder="Description (optional detail for the agent)..."
    oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"></textarea>
  <label style="display:flex;align-items:center;gap:0.3rem;font-size:0.85rem">
    Priority:
    <select name="priority">
      <option value="medium">Medium</option>
      <option value="high">High</option>
      <option value="low">Low</option>
    </select>
  </label>
  <label style="display:flex;align-items:center;gap:0.3rem;font-size:0.85rem" title="Applies to both plan and execution phases. Change them independently on the task detail page.">
    Model (plan &amp; exec):
    <select name="model">
      <option value="sonnet">Sonnet</option>
      <option value="opus">Opus</option>
      <option value="haiku">Haiku</option>
    </select>
  </label>
  <label style="display:flex;align-items:center;gap:0.3rem;font-size:0.85rem;cursor:pointer">
    <input type="checkbox" name="auto_approve" value="1"> Auto-approve
  </label>
  <button type="submit">Add</button>
</form>

<div class="toolbar">
  {% if show_hidden %}
  <a href="/" class="btn btn-sm" style="background:#e5e7eb;color:#333;font-size:0.75rem">Hide Hidden Tasks</a>
  {% else %}
  <a href="/?show_hidden=1" class="btn btn-sm" style="background:#e5e7eb;color:#333;font-size:0.75rem">Show Hidden Tasks</a>
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
      <span class="count">({{ tasks|selectattr('status','equalto',col_status)|list|length }})</span>
    </h2>
    {% for t in tasks if t.status == col_status %}
    <div class="card{% if t.get('hidden') %} hidden-card{% endif %}">
      <a href="/tasks/{{ t.id }}">#{{ t.id }} {{ (t.get('title') or t.prompt)[:60] }}{% if (t.get('title') or t.prompt)|length > 60 %}...{% endif %}</a>
      <div class="meta">
        {{ priority_select(t) }}
        <span class="badge badge-model">P:{{ t.get('plan_model', t.get('model','sonnet')) }}</span>
        <span class="badge badge-model">E:{{ t.get('exec_model', t.get('model','sonnet')) }}</span>
        {% if t.get('auto_approve') %}<span class="badge badge-auto">auto</span>{% endif %}
        {% if t.get('parent') %}<span>&middot; #{{ t.parent }}</span>{% endif %}
        {% if t.get('blocked_on') %}<span class="badge badge-blocked">blocked {{ t.blocked_on|length }}</span>{% endif %}
        {% if t.get('pushed_at') %}<span class="pushed-tag">pushed</span>{% endif %}
        {% if t.get('hidden') %}
        <form method="post" action="/tasks/{{ t.id }}/unhide" style="margin:0"><button class="btn-hide">unhide</button></form>
        {% else %}
        <form method="post" action="/tasks/{{ t.id }}/hide" style="margin:0"><button class="btn-hide">hide</button></form>
        {% endif %}
      </div>
    </div>
    {% else %}
    <div style="color:#ccc;font-size:0.75rem">&mdash;</div>
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
    <h2>Stopped <span class="count">({{ stopped_tasks|length }})</span></h2>
    {% for t in stopped_tasks %}
    <div class="card{% if t.get('hidden') %} hidden-card{% endif %}">
      <a href="/tasks/{{ t.id }}">#{{ t.id }} {{ (t.get('title') or t.prompt)[:60] }}{% if (t.get('title') or t.prompt)|length > 60 %}...{% endif %}</a>
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
    <div style="color:#ccc;font-size:0.75rem">&mdash;</div>
    {% endfor %}
  </div>
  <div class="col col-decomposed">
    <h2>Decomposed <span class="count">({{ decomposed_tasks|length }})</span></h2>
    {% for t in decomposed_tasks %}
    <div class="card{% if t.get('hidden') %} hidden-card{% endif %}">
      <a href="/tasks/{{ t.id }}">#{{ t.id }} {{ (t.get('title') or t.prompt)[:60] }}{% if (t.get('title') or t.prompt)|length > 60 %}...{% endif %}</a>
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
    <div style="color:#ccc;font-size:0.75rem">&mdash;</div>
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
    const display = t.title || t.prompt;
    const label = display.length > 60 ? esc(display.slice(0,60)) + '...' : esc(display);
    const prio = t.priority || 'medium';
    const planModel = t.plan_model || t.model || 'sonnet';
    const execModel = t.exec_model || t.model || 'sonnet';
    let meta = '<form method="post" action="/tasks/' + t.id + '/set-priority" style="margin:0;display:inline">'
      + '<select name="priority" onchange="this.form.submit()" class="prio-select prio-sel-' + prio + '" style="padding:0.1rem 0.2rem;border-radius:4px;border:1px solid #ccc;font-size:0.65rem;font-weight:600;cursor:pointer">'
      + '<option value="high"' + (prio==='high'?' selected':'') + '>high</option>'
      + '<option value="medium"' + (prio==='medium'?' selected':'') + '>medium</option>'
      + '<option value="low"' + (prio==='low'?' selected':'') + '>low</option>'
      + '</select></form>';
    meta += '<span class="badge badge-model">P:' + planModel + '</span>';
    meta += '<span class="badge badge-model">E:' + execModel + '</span>';
    if (t.auto_approve) meta += '<span class="badge badge-auto">auto</span>';
    if (t.parent) meta += '<span>&middot; #' + t.parent + '</span>';
    if (t.blocked_on && t.blocked_on.length) meta += '<span class="badge badge-blocked">blocked ' + t.blocked_on.length + '</span>';
    if (t.pushed_at) meta += '<span class="pushed-tag">pushed</span>';
    if (t.stop_reason) meta += '<span class="reason-tag">' + esc(t.stop_reason) + '</span>';
    if (t.status === 'in_progress' && t.started_at) {
      const elapsed = Math.round((Date.now() - Date.parse(t.started_at)) / 1000);
      const m = Math.floor(elapsed / 60);
      const s = elapsed % 60;
      meta += '<span class="badge" style="background:#dbeafe;color:#1d4ed8">\u29d7 ' + (m > 0 ? m + 'm ' + s + 's' : s + 's') + '</span>';
    }
    const hideAction = t.hidden
      ? '<form method="post" action="/tasks/' + t.id + '/unhide" style="margin:0"><button class="btn-hide">unhide</button></form>'
      : '<form method="post" action="/tasks/' + t.id + '/hide" style="margin:0"><button class="btn-hide">hide</button></form>';
    meta += hideAction;
    const isReview = t.status === 'plan_review' || t.status === 'push_review';
    if (isReview) meta += '<a href="/tasks/' + t.id + '" class="btn-review">Review \u2192</a>';
    const cardCls = 'card' + cls + (isReview ? ' card-review' : '');
    return '<div class="' + cardCls + '"><a href="/tasks/' + t.id + '">#' + t.id + ' ' + label + '</a><div class="meta">' + meta + '</div></div>';
  }

  function renderCol(tasks, status) {
    const items = tasks.filter(t => t.status === status);
    if (items.length === 0) return '<div style="color:#ccc;font-size:0.75rem">&mdash;</div>';
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
            if (countSpan) countSpan.textContent = '(' + count + ')';
          }
          // Replace cards
          const existingCards = cols[i].querySelectorAll('.card, div[style]');
          existingCards.forEach(el => { if (!el.matches('h2')) el.remove(); });
          // Remove all children except h2
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
            if (countSpan) countSpan.textContent = '(' + items.length + ')';
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
      el.innerHTML = '<span class="status-dot status-' + dot + '"></span><span style="color:#ccc;font-size:0.8rem">' + esc(label) + '</span>';
    }

    // Review badge: ⚑ N when plan_review or push_review tasks exist
    const reviewEl = document.getElementById('review-badge');
    const reviewTasks = data.tasks.filter(t => t.status === 'plan_review' || t.status === 'push_review');
    if (reviewEl) {
      const n = reviewTasks.length;
      reviewEl.textContent = n > 0 ? '\u2691 ' + n : '';
      reviewEl.style.display = n > 0 ? 'inline-block' : 'none';
    }

    // Action Required banner
    const banner = document.getElementById('review-banner');
    if (banner) {
      if (reviewTasks.length > 0) {
        const first = reviewTasks[0];
        const label = first.status === 'push_review' ? 'push' : 'plan';
        banner.innerHTML = '\u2691 Action Required \u2014 ' + reviewTasks.length + ' task' + (reviewTasks.length > 1 ? 's' : '') +
          ' need' + (reviewTasks.length === 1 ? 's' : '') + ' your review &mdash; ' +
          '<a href="/tasks/' + first.id + '">Review #' + first.id + ' (' + label + ') &rarr;</a>';
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

  // Done column: collapsed by default; click h2 to toggle; state persisted in localStorage.
  // The col-collapsed class is on the container div, so AJAX card replacements don't affect it.
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
    .content { padding: 1rem; max-width: 1100px; }
    pre { background: #1e1e1e; color: #d4d4d4; padding: 1rem; border-radius: 8px;
          overflow-x: auto; font-size: 0.8rem; white-space: pre-wrap; }
    .actions { display: flex; gap: 0.5rem; margin: 1rem 0; flex-wrap: wrap; align-items: center; }
    /* Two-column layout */
    .detail-cols { display: flex; gap: 1.5rem; align-items: flex-start; margin-top: 1rem; }
    .detail-left  { flex: 3; min-width: 0; }
    .detail-right { flex: 2; min-width: 0; }
    .meta-card { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; }
    .meta-card h3 { margin: 0 0 0.5rem; font-size: 0.85rem; text-transform: uppercase;
                    letter-spacing: 0.05em; color: #666; }
    .meta-row { font-size: 0.85rem; margin-bottom: 0.35rem; }
    .meta-row label { color: #888; font-size: 0.75rem; display: block; margin-bottom: 0.1rem; }
    .subtasks { margin-top: 0; }
    .subtask-item { background: #fff; border: 1px solid #e0e0e0; border-radius: 6px;
                    padding: 0.5rem; margin-bottom: 0.4rem; font-size: 0.85rem; }
    .status-icon { font-style: normal; margin-right: 0.3rem; }
    .status-icon.done       { color: #16a34a; }
    .status-icon.running    { color: #2563eb; }
    .status-icon.review     { color: #d97706; }
    .status-icon.pending    { color: #6b7280; }
    .status-icon.blocked    { color: #7c3aed; }
    .status-icon.stopped    { color: #dc2626; }
    table.sessions { border-collapse: collapse; width: 100%; font-size: 0.8rem; margin-top: 0.5rem; }
    table.sessions th, table.sessions td { border: 1px solid #e0e0e0; padding: 0.4rem 0.6rem; text-align: left; }
    table.sessions th { background: #f3f4f6; font-weight: 600; }
    table.sessions tr:nth-child(even) { background: #fafafa; }
    .edit-form { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
                 padding: 1rem; margin: 1rem 0; display: none; }
    .edit-form textarea { width: 100%; padding: 0.5rem; border: 1px solid #ccc;
                          border-radius: 6px; font-size: 0.9rem; resize: none; overflow: hidden; box-sizing: border-box; }
    .edit-form select { padding: 0.4rem; border-radius: 6px; border: 1px solid #ccc; }
    .timestamp { color: #888; font-size: 0.75rem; }
    .rate-limit-banner { background: #fef3c7; border: 1px solid #f59e0b; border-radius: 6px;
                         padding: 0.5rem 0.75rem; font-size: 0.85rem; margin: 0.5rem 0; }
    /* Plan rendering */
    .plan-card { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
                 padding: 1rem; margin-bottom: 1rem; }
    .plan-card h3 { margin: 0 0 0.5rem; font-size: 0.85rem; text-transform: uppercase;
                    letter-spacing: 0.05em; color: #666; }
    .plan-decision-badge { display: inline-block; padding: 0.2rem 0.6rem; border-radius: 4px;
                           font-size: 0.8rem; font-weight: 700; margin-bottom: 0.75rem; }
    .plan-decision-execute  { background: #dbeafe; color: #1d4ed8; }
    .plan-decision-decompose { background: #f3e8ff; color: #6b21a8; }
    .plan-reasoning { font-size: 0.85rem; color: #555; font-style: italic;
                      margin-bottom: 0.75rem; padding: 0.5rem; background: #f9f9f9;
                      border-left: 3px solid #d1d5db; border-radius: 0 4px 4px 0; }
    .plan-steps { font-size: 0.85rem; white-space: pre-wrap; margin: 0; }
    .subtask-tree { list-style: none; padding: 0; margin: 0; }
    .subtask-tree li { padding: 0.5rem 0.6rem; margin-bottom: 0.35rem; background: #f5f3ff;
                       border: 1px solid #ddd6fe; border-radius: 6px; font-size: 0.85rem; }
    .subtask-tree li .stnum { font-weight: 700; color: #6b21a8; margin-right: 0.4rem; }
    .subtask-tree li .stdep { font-size: 0.75rem; color: #888; margin-top: 0.2rem; }
    /* Artifact rendering */
    .artifact { margin: 0.5rem 0; padding: 0.5rem; border-radius: 4px; }
    .artifact-git-commit { font-family: monospace; background: #f0f9ff; border: 1px solid #bae6fd; }
    .artifact-ref { font-weight: bold; color: #0284c7; margin-right: 0.5rem; }
    .artifact-text { background: #f9fafb; border: 1px solid #e5e7eb; white-space: pre-wrap; }
    .artifact-document { background: #f9fafb; border: 1px solid #e5e7eb; }
    .artifact-document pre { margin: 0.5rem 0 0; white-space: pre-wrap; }
    .artifact-doc-path { padding: 0.4rem 0.5rem; font-size: 0.82rem; color: #555; }
    .artifact-code-diff { background: #1e1e1e; color: #d4d4d4; padding: 0.75rem; border-radius: 4px; font-size: 0.8rem; overflow-x: auto; }
    .artifact-url-list { margin: 0; padding-left: 1.2rem; }
    .artifact-url-list a { color: #2563eb; }
    /* Report rendering (decomposed parent tasks) */
    .report-card { margin: 1rem 0; border: 1px solid #d1fae5; border-radius: 6px; background: #f0fdf4; }
    .report-card summary { padding: 0.6rem 0.8rem; cursor: pointer; font-weight: 600; color: #065f46; }
    .report-card summary:hover { background: #dcfce7; border-radius: 6px; }
    .report-content { margin: 0; padding: 0.8rem; font-size: 0.85rem; border-top: 1px solid #d1fae5; line-height: 1.6; }
    .report-content h1,.report-content h2,.report-content h3 { margin: 0.8rem 0 0.3rem; }
    .report-content table { border-collapse: collapse; width: 100%; margin: 0.5rem 0; }
    .report-content th, .report-content td { border: 1px solid #d1d5db; padding: 0.3rem 0.5rem; }
    .report-content th { background: #f3f4f6; }
    .report-content code { background: #f3f4f6; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.8rem; }
    .report-content pre { background: #f3f4f6; padding: 0.5rem; border-radius: 4px; overflow-x: auto; }
    .report-content ul, .report-content ol { padding-left: 1.5rem; }
    .report-content a { color: #2563eb; }
  </style>
</head>
<body>
""" + HEADER_HTML + """
<div class="content">
<p><a href="/">&larr; Board</a></p>
<h1>#{{ task.id }} &mdash; {{ task.get('title') or task.prompt }}</h1>
{% if task.get('title') and task.get('title') != task.prompt %}
<p style="color:#555;margin:-0.5rem 0 1rem;font-size:0.9rem;white-space:pre-wrap">{{ task.prompt }}</p>
{% endif %}

{% if task.get('rate_limited_at') %}
<div class="rate-limit-banner">
  Rate limited at {{ task.rate_limited_at[:19] }} &mdash; dispatcher will retry automatically.
</div>
{% endif %}

{# ---- Two-column layout ---- #}
<div class="detail-cols">

  {# ======= LEFT: plan + approve/reject ======= #}
  <div class="detail-left">

    {# ---- Edit form (hidden by default, shown by "Edit Task" button) ---- #}
    <div class="edit-form" id="edit-form">
      <form method="post" action="/tasks/{{ task.id }}/edit">
        <p><strong>Edit Task</strong></p>
        <input type="text" name="title" value="{{ task.get('title') or '' }}" placeholder="Title..."
          style="width:100%;padding:0.4rem;border-radius:6px;border:1px solid #ccc;font-size:0.9rem;box-sizing:border-box;margin-bottom:0.4rem">
        <textarea name="prompt" rows="3"
          oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'">{{ task.prompt }}</textarea>
        <div style="margin-top:0.5rem;display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap">
          <label>Priority:</label>
          <select name="priority">
            <option value="high" {% if task.get('priority')=='high' %}selected{% endif %}>High</option>
            <option value="medium" {% if task.get('priority','medium')=='medium' %}selected{% endif %}>Medium</option>
            <option value="low" {% if task.get('priority')=='low' %}selected{% endif %}>Low</option>
          </select>
          <label>Plan:</label>
          <select name="plan_model">
            {% set pm = task.get('plan_model', task.get('model','sonnet')) %}
            <option value="sonnet" {% if pm=='sonnet' %}selected{% endif %}>Sonnet</option>
            <option value="opus" {% if pm=='opus' %}selected{% endif %}>Opus</option>
            <option value="haiku" {% if pm=='haiku' %}selected{% endif %}>Haiku</option>
          </select>
          <label>Exec:</label>
          <select name="exec_model">
            {% set em = task.get('exec_model', task.get('model','sonnet')) %}
            <option value="sonnet" {% if em=='sonnet' %}selected{% endif %}>Sonnet</option>
            <option value="opus" {% if em=='opus' %}selected{% endif %}>Opus</option>
            <option value="haiku" {% if em=='haiku' %}selected{% endif %}>Haiku</option>
          </select>
          <button class="btn btn-edit" type="submit">Save</button>
          <button class="btn" type="button" onclick="document.getElementById('edit-form').style.display='none'" style="background:#e5e7eb;color:#333">Cancel</button>
        </div>
      </form>
    </div>

    {# ---- Action buttons based on current status ---- #}
    <div class="actions">

      {# Plan review: approve / reject #}
      {% if task.status == 'plan_review' %}
      <form method="post" action="/tasks/{{ task.id }}/approve">
        <button class="btn btn-approve">Approve Plan</button>
      </form>
      <div id="reject-section-{{ task.id }}">
        <button class="btn btn-reject" onclick="document.getElementById('reject-expand-{{ task.id }}').style.display='block';this.style.display='none'">Reject</button>
        <div id="reject-expand-{{ task.id }}" style="display:none;margin-top:0.5rem">
          <form method="post" action="/tasks/{{ task.id }}/reject">
            <textarea name="feedback" placeholder="Rejection reason (optional)" rows="3"
              style="width:100%;padding:0.4rem;border-radius:6px;border:1px solid #ccc;resize:vertical;box-sizing:border-box"></textarea>
            <div style="display:flex;gap:0.5rem;margin-top:0.3rem">
              <button class="btn btn-reject">Submit Rejection</button>
              <button type="button" class="btn btn-sm" style="background:#6b7280;color:#fff"
                onclick="document.getElementById('reject-expand-{{ task.id }}').style.display='none';document.querySelector('#reject-section-{{ task.id }} > .btn-reject').style.display=''">Cancel</button>
            </div>
          </form>
        </div>
      </div>
      {% endif %}

      {# Push review: approve push / reject push #}
      {% if task.status == 'push_review' %}
      <form method="post" action="/tasks/{{ task.id }}/approve-push">
        <button class="btn btn-approve">Approve Push</button>
      </form>
      <form method="post" action="/tasks/{{ task.id }}/reject-push">
        <button class="btn btn-reject">Skip Push (keep local)</button>
      </form>
      {% endif %}

      {# Cancel: only for in_progress or plan_review #}
      {% if task.status in ('in_progress', 'plan_review') %}
      <form method="post" action="/tasks/{{ task.id }}/cancel" onsubmit="return confirm('Cancel this task?')">
        <button class="btn btn-cancel btn-sm">Cancel</button>
      </form>
      {% endif %}

      {# Retry: only for stopped or done #}
      {% if task.status in ('stopped', 'done') %}
      <form method="post" action="/tasks/{{ task.id }}/retry">
        <button class="btn btn-retry btn-sm">Retry / Requeue</button>
      </form>
      {% endif %}

    </div>{# end .actions #}

    {# ---- Plan ---- #}
    {% if task.get('plan') %}
    <h2>Plan</h2>
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

    {# ---- Report (decomposed parent tasks) ---- #}
    {% if task.get('report') %}
    <h2>Report</h2>
    <details class="report-card" open>
      <summary>Consolidated report from all subtasks</summary>
      <div class="report-content">{{ task.report | md }}</div>
    </details>
    {% endif %}

    {# ---- Result Summary ---- #}
    {# Only show if report is absent or different — avoids duplicate when report == summary (leaf tasks) #}
    {% set result_summary = (task.get('result') or {}).get('summary') or task.get('summary') %}
    {% if result_summary and result_summary != task.get('report') %}
    <h2>Result Summary</h2>
    <div class="report-content" style="border:none;padding:0">{{ result_summary | md }}</div>
    {% endif %}

    {# ---- Artifacts ---- #}
    {% set artifacts = (task.get('result') or {}).get('artifacts') or [] %}
    {% if artifacts %}
    <h2>Artifacts</h2>
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
      <div class="artifact artifact-unknown">{{ a | tojson }}</div>
      {% endif %}
    {% endfor %}
    {% endif %}

  </div>{# end .detail-left #}

  {# ======= RIGHT: metadata + subtasks + sessions ======= #}
  <div class="detail-right">

    {# ---- Metadata card ---- #}
    <div class="meta-card">
      <h3>Details</h3>
      <div class="meta-row">
        <span class="state-badge state-{{ task.status }}">{{ task.status }}</span>
        {% if task.get('stop_reason') %}<span class="state-badge state-stopped" style="margin-left:0.3rem">{{ task.stop_reason }}</span>{% endif %}
        {% if task.get('pushed_at') %}<span class="state-badge state-done" style="margin-left:0.3rem">pushed</span>{% endif %}
      </div>
      <div class="meta-row">
        <label>Priority</label>
        {{ task.get('priority','medium') }}
      </div>
      {% set pm = task.get('plan_model', task.get('model','sonnet')) %}
      {% set em = task.get('exec_model', task.get('model','sonnet')) %}
      <div class="meta-row">
        <label>Models</label>
        <form method="post" action="/tasks/{{ task.id }}/set-model" style="display:inline">
          Plan: <select name="plan_model" onchange="this.form.submit()" style="padding:0.2rem;border-radius:4px;border:1px solid #ccc;font-size:0.82rem">
            <option value="sonnet" {% if pm=='sonnet' %}selected{% endif %}>Sonnet</option>
            <option value="opus" {% if pm=='opus' %}selected{% endif %}>Opus</option>
            <option value="haiku" {% if pm=='haiku' %}selected{% endif %}>Haiku</option>
          </select>
          Exec: <select name="exec_model" onchange="this.form.submit()" style="padding:0.2rem;border-radius:4px;border:1px solid #ccc;font-size:0.82rem">
            <option value="sonnet" {% if em=='sonnet' %}selected{% endif %}>Sonnet</option>
            <option value="opus" {% if em=='opus' %}selected{% endif %}>Opus</option>
            <option value="haiku" {% if em=='haiku' %}selected{% endif %}>Haiku</option>
          </select>
        </form>
      </div>
      <div class="meta-row">
        <label>Auto-approve</label>
        <form method="post" action="/tasks/{{ task.id }}/set-auto-approve" style="display:inline">
          <input type="hidden" name="auto_approve" value="0">
          <input type="checkbox" name="auto_approve" value="1" {% if task.get('auto_approve') %}checked{% endif %} onchange="this.form.submit()">
        </form>
      </div>
      {% if task.get('parent') %}
      <div class="meta-row"><label>Parent</label><a href="/tasks/{{ task.parent }}">#{{ task.parent }}</a></div>
      {% endif %}
      {% if task.get('created_at') %}
      <div class="meta-row"><label>Created</label><span class="timestamp">{{ task.created_at[:19] }}</span></div>
      {% endif %}
      {% if task.get('completed_at') %}
      <div class="meta-row"><label>Completed</label><span class="timestamp">{{ task.completed_at[:19] }}</span></div>
      {% endif %}
      {% if task.get('pushed_at') %}
      <div class="meta-row"><label>Pushed</label><span class="timestamp">{{ task.pushed_at[:19] }}</span></div>
      {% endif %}
      <div class="meta-row" style="margin-top:0.75rem;display:flex;gap:0.4rem;flex-wrap:wrap">
        {# Edit: available for any status except in_progress #}
        {% if task.status != 'in_progress' %}
        <button class="btn btn-edit btn-sm" onclick="var f=document.getElementById('edit-form');f.style.display='block';var ta=f.querySelector('textarea');ta.style.height='auto';ta.style.height=ta.scrollHeight+'px';f.scrollIntoView({behavior:'smooth',block:'start'})">Edit Task</button>
        {% endif %}
        {# Delete: available unless in_progress #}
        {% if task.status != 'in_progress' %}
        <form method="post" action="/tasks/{{ task.id }}/delete" onsubmit="return confirm('Delete task #{{ task.id }}? This cannot be undone.')">
          <button class="btn btn-delete btn-sm">Delete</button>
        </form>
        {% endif %}
        {# Hide / Unhide #}
        {% if task.get('hidden') %}
        <form method="post" action="/tasks/{{ task.id }}/unhide">
          <button class="btn btn-sm" style="background:#6b7280;color:#fff">Unhide</button>
        </form>
        {% else %}
        <form method="post" action="/tasks/{{ task.id }}/hide">
          <button class="btn btn-sm" style="background:#6b7280;color:#fff">Hide</button>
        </form>
        {% endif %}
      </div>
    </div>{# end .meta-card #}

    {# ---- Subtasks card ---- #}
    {% if subtasks %}
    <div class="meta-card">
      <h3>Subtasks ({{ subtasks|length }})</h3>
      <div class="subtasks">
        {% for s in subtasks %}
        {% set _blocked = s.get('blocked_on') and s.blocked_on|length > 0 %}
        {% if _blocked %}
          {% set _icon = '⊟' %}{% set _cls = 'blocked' %}
        {% elif s.status == 'done' or s.status == 'push_review' %}
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
          {% if _blocked %}blocked(#{{ s.blocked_on|join(', #') }}){% endif %}
          {{ s.prompt[:80] }}
        </div>
        {% endfor %}
      </div>
    </div>{# end subtasks card #}
    {% endif %}

    {# ---- Sessions card ---- #}
    {% if task.get('sessions') %}
    <div class="meta-card">
      <h3>Run History</h3>
      <table class="sessions">
        <thead>
          <tr><th>Run</th><th>Started</th><th>Duration</th><th>Outcome</th></tr>
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
              {% if dur >= 3600 %}{{ (dur // 3600) }}h {{ ((dur % 3600) // 60) }}m
              {% elif dur >= 60 %}{{ (dur // 60) }}m {{ (dur % 60) }}s
              {% else %}{{ dur }}s{% endif %}
            </td>
            <td>
              {% if rl %}
                ⚠ Rate limited — will retry
              {% elif rc == 0 %}
                ✓ Completed
              {% else %}
                ✗ Failed (exit {{ rc }})
              {% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>{# end sessions card #}
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
        const n = data.tasks.filter(t => t.status === 'plan_review' || t.status === 'push_review').length;
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
    .content { padding: 1rem; max-width: 900px; }
    pre { background: #1e1e1e; color: #d4d4d4; padding: 1rem; border-radius: 8px;
          overflow-x: auto; font-size: 0.8rem; white-space: pre-wrap; }
    .empty { color: #888; font-style: italic; }
  </style>
</head>
<body>
""" + HEADER_HTML + """
<div class="content">
<h1>Agent Task Log</h1>
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
    .content { padding: 1rem; max-width: 900px; }
    pre { background: #1e1e1e; color: #d4d4d4; padding: 1rem; border-radius: 8px;
          overflow-x: auto; font-size: 0.8rem; white-space: pre-wrap; }
    .empty { color: #888; font-style: italic; }
  </style>
</head>
<body>
""" + HEADER_HTML + """
<div class="content">
<h1>Recent Git Log</h1>
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
    ("pending",      "Pending",    ""),
    ("in_progress",  "Running",    ""),
    ("plan_review",  "Review Plan", "\u270b"),
    ("push_review",  "Review Push", "\u270b"),
    ("done",         "Done",       ""),
]


LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Sign in — Ralph Loop</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; background: #f5f5f5;
           display: flex; align-items: center; justify-content: center;
           height: 100vh; margin: 0; }
    .box { background: #fff; padding: 2rem; border-radius: 8px;
           box-shadow: 0 2px 8px rgba(0,0,0,0.12); width: 320px; }
    h1 { margin: 0 0 1.5rem; font-size: 1.3rem; color: #1a1a2e; }
    input { width: 100%; padding: 0.6rem 0.75rem; margin-bottom: 1rem;
            border: 1px solid #ddd; border-radius: 4px; font-size: 0.95rem; }
    button { width: 100%; padding: 0.7rem; background: #1a1a2e; color: #fff;
             border: none; border-radius: 4px; font-size: 1rem; cursor: pointer; }
    button:hover { background: #2d2d4e; }
    .error { color: #dc2626; font-size: 0.85rem; margin-bottom: 1rem; }
  </style>
</head>
<body>
  <div class="box">
    <h1>Ralph Loop</h1>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
    <form method="post">
      <input type="text" name="username" placeholder="Username" autofocus>
      <input type="password" name="password" placeholder="Password">
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
    model = request.form.get("model", "sonnet")
    auto_approve = request.form.get("auto_approve") == "1"
    if priority not in ("high", "medium", "low"):
        priority = "medium"
    if model not in ("sonnet", "opus", "haiku"):
        model = "sonnet"
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
            "plan_model": model,
            "exec_model": model,
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
    return redirect(url_for("board"))


@app.post("/tasks/<int:task_id>/approve")
def approve_task(task_id: int):
    """Approve a plan that's in review. Two outcomes:
    - 'execute' decision: set task back to in_progress (dispatcher picks it up for Docker execution)
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
            task["status"] = "in_progress"

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
    """Stop an active task. Only applies to in_progress or plan_review — the
    dispatcher may be mid-execution when this fires, but the status change
    prevents it from being picked up again on the next iteration."""
    _, err = _check_owner(task_id)
    if err:
        return err
    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id and t["status"] in ("in_progress", "plan_review"):
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


@app.post("/tasks/<int:task_id>/approve-push")
def approve_push(task_id: int):
    _, err = _check_owner(task_id)
    if err:
        return err
    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id and t["status"] == "push_review":
                t["push_approved"] = True
                break

    locked_update(mutate)
    log_progress(task_id, "push approved")
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/reject-push")
def reject_push(task_id: int):
    _, err = _check_owner(task_id)
    if err:
        return err
    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id and t["status"] == "push_review":
                t["status"] = "done"
                t["summary"] = (t.get("summary") or "") + "\nPush skipped by user (local commit only)."
                break

    locked_update(mutate)
    log_progress(task_id, "push skipped", "local commit only")
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
