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
  POST /tasks/<id>/approve-push  - approve push -> done (with pushed_at)
  POST /tasks/<id>/reject-push   - reject push -> done (local commit only)
  GET  /progress                 - view PROGRESS.md
  GET  /log                      - view recent git log
  GET  /status                   - dispatcher status (JSON)
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, url_for, jsonify
from progress_logger import log_progress
from task_store import load_tasks, save_tasks, locked_update, next_id, TASKS_FILE

WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
PROGRESS_FILE = WORKSPACE / "PROGRESS.md"

app = Flask(__name__)


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
    <span id="dispatcher-status"></span>
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
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ClaudeXingCode Dashboard</title>
  <style>
    """ + SHARED_CSS + """
    /* --- Pipeline section --- */
    .section-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em;
                     color: #999; padding: 0.75rem 1rem 0; font-weight: 600; }
    .pipeline { display: flex; gap: 0; padding: 0 1rem 0.5rem; overflow-x: auto; align-items: stretch; }
    .pipe-arrow { display: flex; align-items: center; color: #cbd5e1; font-size: 1.2rem;
                  padding: 0 0.15rem; user-select: none; margin-top: 1.8rem; }
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
                        border: 1px solid #ccc; font-size: 0.9rem; }
    form.add select, form.add button { padding: 0.5rem; border-radius: 6px; border: 1px solid #ccc; }
    form.add button { background: #1a1a2e; color: #fff; border: none; cursor: pointer; }
    .toolbar { padding: 0 1rem 0.5rem; display: flex; gap: 0.5rem; align-items: center; }
    .btn-hide { background: none; border: 1px solid #ccc; border-radius: 4px; padding: 0.15rem 0.4rem;
                font-size: 0.65rem; color: #888; cursor: pointer; }
    .btn-hide:hover { background: #f0f0f0; }
    .card.hidden-card { opacity: 0.5; }
  </style>
</head>
<body>
""" + HEADER_HTML + """

<form class="add" method="post" action="/tasks">
  <textarea name="prompt" rows="2" placeholder="New task... (speak or type)" required></textarea>
  <select name="priority">
    <option value="medium">Medium</option>
    <option value="high">High</option>
    <option value="low">Low</option>
  </select>
  <button type="submit">Add</button>
</form>

<div class="toolbar">
  {% if show_hidden %}
  <a href="/" class="btn btn-sm" style="background:#e5e7eb;color:#333;font-size:0.75rem">Hide Hidden Tasks</a>
  {% else %}
  <a href="/?show_hidden=1" class="btn btn-sm" style="background:#e5e7eb;color:#333;font-size:0.75rem">Show Hidden Tasks</a>
  {% endif %}
</div>

{# --- Main pipeline: happy path left-to-right --- #}
<div class="section-label">Pipeline</div>
<div class="pipeline">
  {% for col_status, col_label, col_icon in pipeline_cols %}
  {% if not loop.first %}<div class="pipe-arrow">&rarr;</div>{% endif %}
  <div class="col col-{{ col_status }}">
    <h2>
      {% if col_icon %}<span class="gate-icon">{{ col_icon }}</span>{% endif %}
      {{ col_label }}
      <span class="count">({{ tasks|selectattr('status','equalto',col_status)|list|length }})</span>
    </h2>
    {% for t in tasks if t.status == col_status %}
    <div class="card{% if t.get('hidden') %} hidden-card{% endif %}">
      <a href="/tasks/{{ t.id }}">#{{ t.id }} {{ t.prompt[:50] }}{% if t.prompt|length > 50 %}...{% endif %}</a>
      <div class="meta">
        <span class="badge badge-{{ t.get('priority','medium') }}">{{ t.get('priority','medium') }}</span>
        {% if t.get('parent') %}<span>&middot; #{{ t.parent }}</span>{% endif %}
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
      <a href="/tasks/{{ t.id }}">#{{ t.id }} {{ t.prompt[:50] }}{% if t.prompt|length > 50 %}...{% endif %}</a>
      <div class="meta">
        <span class="badge badge-{{ t.get('priority','medium') }}">{{ t.get('priority','medium') }}</span>
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
      <a href="/tasks/{{ t.id }}">#{{ t.id }} {{ t.prompt[:50] }}{% if t.prompt|length > 50 %}...{% endif %}</a>
      <div class="meta">
        <span class="badge badge-{{ t.get('priority','medium') }}">{{ t.get('priority','medium') }}</span>
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

</body>
</html>
"""

DETAIL_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Task #{{ task.id }}</title>
  <style>
    """ + SHARED_CSS + """
    .content { padding: 1rem; max-width: 900px; }
    pre { background: #1e1e1e; color: #d4d4d4; padding: 1rem; border-radius: 8px;
          overflow-x: auto; font-size: 0.8rem; white-space: pre-wrap; }
    .actions { display: flex; gap: 0.5rem; margin: 1rem 0; flex-wrap: wrap; align-items: center; }
    .subtasks { margin-top: 1rem; }
    .subtask-item { background: #fff; border: 1px solid #e0e0e0; border-radius: 6px;
                    padding: 0.5rem; margin-bottom: 0.4rem; font-size: 0.85rem; }
    table.sessions { border-collapse: collapse; width: 100%; font-size: 0.8rem; margin-top: 0.5rem; }
    table.sessions th, table.sessions td { border: 1px solid #e0e0e0; padding: 0.4rem 0.6rem; text-align: left; }
    table.sessions th { background: #f3f4f6; font-weight: 600; }
    table.sessions tr:nth-child(even) { background: #fafafa; }
    .edit-form { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
                 padding: 1rem; margin: 1rem 0; display: none; }
    .edit-form textarea { width: 100%; padding: 0.5rem; border: 1px solid #ccc;
                          border-radius: 6px; font-size: 0.9rem; }
    .edit-form select { padding: 0.4rem; border-radius: 6px; border: 1px solid #ccc; }
    .timestamp { color: #888; font-size: 0.75rem; }
    .rate-limit-banner { background: #fef3c7; border: 1px solid #f59e0b; border-radius: 6px;
                         padding: 0.5rem 0.75rem; font-size: 0.85rem; margin: 0.5rem 0; }
  </style>
</head>
<body>
""" + HEADER_HTML + """
<div class="content">
<p><a href="/">&larr; Board</a></p>
<h1>#{{ task.id }} &mdash; {{ task.prompt }}</h1>
<p>
  <span class="state-badge state-{{ task.status }}">{{ task.status }}</span>
  {% if task.get('stop_reason') %}<span class="state-badge state-stopped" style="margin-left:0.3rem">{{ task.stop_reason }}</span>{% endif %}
  {% if task.get('pushed_at') %}<span class="state-badge state-done" style="margin-left:0.3rem">pushed</span>{% endif %}
   | Priority: {{ task.get('priority','medium') }}
   {% if task.get('parent') %}| Subtask of <a href="/tasks/{{ task.parent }}">#{{ task.parent }}</a>{% endif %}
   {% if task.get('created_at') %}| Created: <span class="timestamp">{{ task.created_at[:19] }}</span>{% endif %}
   {% if task.get('completed_at') %}| Completed: <span class="timestamp">{{ task.completed_at[:19] }}</span>{% endif %}
   {% if task.get('pushed_at') %}| Pushed: <span class="timestamp">{{ task.pushed_at[:19] }}</span>{% endif %}
</p>

{% if task.get('rate_limited_at') %}
<div class="rate-limit-banner">
  Rate limited at {{ task.rate_limited_at[:19] }} &mdash; dispatcher will retry automatically.
</div>
{% endif %}

{# ---- Action buttons based on current status ---- #}
<div class="actions">

  {# Plan review: approve / reject #}
  {% if task.status == 'plan_review' %}
  <form method="post" action="/tasks/{{ task.id }}/approve">
    <button class="btn btn-approve">Approve Plan</button>
  </form>
  <form method="post" action="/tasks/{{ task.id }}/reject" style="display:flex;gap:0.3rem">
    <input name="feedback" placeholder="Rejection reason (optional)" style="padding:0.4rem;border-radius:6px;border:1px solid #ccc">
    <button class="btn btn-reject">Reject</button>
  </form>
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

  {# Edit: available for pending or stopped #}
  {% if task.status in ('pending', 'stopped') %}
  <button class="btn btn-edit btn-sm" onclick="document.getElementById('edit-form').style.display='block'">Edit</button>
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

{# ---- Edit form (hidden by default) ---- #}
<div class="edit-form" id="edit-form">
  <form method="post" action="/tasks/{{ task.id }}/edit">
    <p><strong>Edit Task</strong></p>
    <textarea name="prompt" rows="3">{{ task.prompt }}</textarea>
    <div style="margin-top:0.5rem;display:flex;gap:0.5rem;align-items:center">
      <label>Priority:</label>
      <select name="priority">
        <option value="high" {% if task.get('priority')=='high' %}selected{% endif %}>High</option>
        <option value="medium" {% if task.get('priority','medium')=='medium' %}selected{% endif %}>Medium</option>
        <option value="low" {% if task.get('priority')=='low' %}selected{% endif %}>Low</option>
      </select>
      <button class="btn btn-edit" type="submit">Save</button>
      <button class="btn" type="button" onclick="document.getElementById('edit-form').style.display='none'" style="background:#e5e7eb;color:#333">Cancel</button>
    </div>
  </form>
</div>

{# ---- Plan ---- #}
{% if task.get('plan') %}
<h2>Plan</h2>
<pre>{{ task.plan }}</pre>
{% endif %}

{# ---- Result Summary ---- #}
{% if task.get('summary') %}
<h2>Result Summary</h2>
<pre>{{ task.summary }}</pre>
{% endif %}

{# ---- Sessions table (Phase 8) ---- #}
{% if task.get('sessions') %}
<h2>Sessions</h2>
<table class="sessions">
  <thead>
    <tr><th>#</th><th>Started</th><th>Duration</th><th>Exit Code</th><th>Rate Limited?</th></tr>
  </thead>
  <tbody>
    {% for s in task.sessions %}
    <tr>
      <td>{{ loop.index }}</td>
      <td>{{ s.get('started_at', '?')[:19] }}</td>
      <td>
        {% set dur = s.get('duration_s', 0) %}
        {% if dur >= 3600 %}{{ (dur // 3600) }}h {{ ((dur % 3600) // 60) }}m
        {% elif dur >= 60 %}{{ (dur // 60) }}m {{ (dur % 60) }}s
        {% else %}{{ dur }}s{% endif %}
      </td>
      <td>{{ s.get('exit_code', '?') }}</td>
      <td>{% if s.get('rate_limited') %}Yes &#9888;{% else %}No{% endif %}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% endif %}

{# ---- Subtasks ---- #}
{% if subtasks %}
<div class="subtasks">
  <h2>Subtasks</h2>
  {% for s in subtasks %}
  <div class="subtask-item">
    <a href="/tasks/{{ s.id }}">#{{ s.id }}</a> [{{ s.status }}] {{ s.prompt[:80] }}
  </div>
  {% endfor %}
</div>
{% endif %}

</div>
</body>
</html>
"""

PROGRESS_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
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
<h1>PROGRESS.md</h1>
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


@app.get("/")
def board():
    data = load_tasks()
    show_hidden = request.args.get("show_hidden", "0") == "1"
    tasks = data["tasks"] if show_hidden else [t for t in data["tasks"] if not t.get("hidden")]
    return render_template_string(
        BOARD_HTML, tasks=tasks, pipeline_cols=PIPELINE_COLS, show_hidden=show_hidden,
    )


@app.post("/tasks")
def add_task():
    prompt = request.form.get("prompt", "").strip()
    priority = request.form.get("priority", "medium")
    if not prompt:
        return redirect(url_for("board"))

    new_task = {}

    def mutate(data):
        task = {
            "id": next_id(data),
            "status": "pending",
            "prompt": prompt,
            "priority": priority,
            "parent": None,
            "plan": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "summary": None,
        }
        data["tasks"].append(task)
        new_task.update(task)

    locked_update(mutate)
    log_progress(new_task["id"], "created", f"priority={priority}, prompt={prompt[:80]}")
    return redirect(url_for("board"))


@app.get("/tasks/<int:task_id>")
def task_detail(task_id: int):
    data = load_tasks()
    task = next((t for t in data["tasks"] if t["id"] == task_id), None)
    if task is None:
        return "Task not found", 404
    subtasks = [t for t in data["tasks"] if t.get("parent") == task_id]
    return render_template_string(DETAIL_HTML, task=task, subtasks=subtasks)


@app.post("/tasks/<int:task_id>/edit")
def edit_task(task_id: int):
    prompt = request.form.get("prompt", "").strip()
    priority = request.form.get("priority", "medium")

    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id and t["status"] in ("pending", "stopped"):
                if prompt:
                    t["prompt"] = prompt
                t["priority"] = priority
                break

    locked_update(mutate)
    log_progress(task_id, "edited", f"priority={priority}")
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/delete")
def delete_task(task_id: int):
    def mutate(data):
        data["tasks"] = [t for t in data["tasks"] if t["id"] != task_id or t["status"] == "in_progress"]

    locked_update(mutate)
    log_progress(task_id, "deleted")
    return redirect(url_for("board"))


@app.post("/tasks/<int:task_id>/approve")
def approve_task(task_id: int):
    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id and t["status"] == "plan_review":
                t["status"] = "in_progress"
                break

    locked_update(mutate)
    log_progress(task_id, "plan approved")
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/reject")
def reject_task(task_id: int):
    feedback = request.form.get("feedback", "")

    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id and t["status"] == "plan_review":
                t["status"] = "stopped"
                t["stop_reason"] = "rejected"
                if feedback:
                    t["summary"] = f"Rejected: {feedback}"
                break

    locked_update(mutate)
    log_progress(task_id, "plan rejected", feedback or "")
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/cancel")
def cancel_task(task_id: int):
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
    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id and t["status"] in ("stopped", "done"):
                t["status"] = "pending"
                t["completed_at"] = None
                t["summary"] = None
                t["plan"] = None
                t.pop("stop_reason", None)
                t.pop("pushed_at", None)
                break

    locked_update(mutate)
    log_progress(task_id, "requeued (retry)")
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/approve-push")
def approve_push(task_id: int):
    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id and t["status"] == "push_review":
                t["status"] = "done"
                t["pushed_at"] = datetime.now(timezone.utc).isoformat()
                break

    locked_update(mutate)
    log_progress(task_id, "push approved")
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/reject-push")
def reject_push(task_id: int):
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
    def mutate(data):
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["hidden"] = True
                break

    locked_update(mutate)
    return redirect(url_for("board"))


@app.post("/tasks/<int:task_id>/unhide")
def unhide_task(task_id: int):
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


@app.get("/status")
def dispatcher_status():
    """Return dispatcher status as JSON.

    The dispatcher writes its state to a small JSON file so the Web UI can
    display it.  If the file doesn't exist we report "idle".
    """
    status_file = TASKS_FILE.parent / "dispatcher_status.json"
    if status_file.exists():
        try:
            return jsonify(json.loads(status_file.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return jsonify({"state": "idle", "label": "Idle"})


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
