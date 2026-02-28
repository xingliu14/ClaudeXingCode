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
  POST /tasks/<id>/cancel        - cancel in-progress task -> failed
  POST /tasks/<id>/retry         - requeue failed/done task -> pending
  POST /tasks/<id>/approve-push  - approve push -> dispatcher runs git push
  POST /tasks/<id>/reject-push   - reject push -> done (local commit only)
  GET  /progress                 - view PROGRESS.md
  GET  /log                      - view recent git log
  GET  /status                   - dispatcher status (JSON)
"""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, url_for, jsonify

TASKS_FILE = Path(os.environ.get("TASKS_FILE", "/agent/tasks.json"))
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
PROGRESS_FILE = WORKSPACE / "PROGRESS.md"

app = Flask(__name__)


# ---------------------------------------------------------------------------
# tasks.json helpers
# ---------------------------------------------------------------------------

def load_tasks() -> dict:
    if not TASKS_FILE.exists():
        return {"tasks": []}
    return json.loads(TASKS_FILE.read_text())


def save_tasks(data: dict) -> None:
    TASKS_FILE.write_text(json.dumps(data, indent=2))


def next_id(tasks: list) -> int:
    return max((t["id"] for t in tasks), default=0) + 1


# ---------------------------------------------------------------------------
# Shared CSS
# ---------------------------------------------------------------------------

SHARED_CSS = """
    body { font-family: system-ui, sans-serif; margin: 0; background: #f5f5f5; }
    header { background: #1a1a2e; color: #fff; padding: 1rem 1.5rem; display: flex;
             align-items: center; justify-content: space-between; }
    header h1 { margin: 0; font-size: 1.2rem; }
    header nav { display: flex; gap: 1rem; }
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
"""

# ---------------------------------------------------------------------------
# Header partial (nav bar shown on every page)
# ---------------------------------------------------------------------------

HEADER_HTML = """
<header>
  <h1>Agent Board</h1>
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
  <title>Agent Board</title>
  <style>
    """ + SHARED_CSS + """
    .board { display: flex; gap: 1rem; padding: 1rem; overflow-x: auto; }
    .col { background: #fff; border-radius: 8px; min-width: 200px; flex: 1; padding: 0.75rem; }
    .col h2 { font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em;
               color: #666; margin: 0 0 0.75rem; }
    .col h2 .count { font-weight: normal; color: #aaa; }
    .card { background: #f9f9f9; border: 1px solid #e0e0e0; border-radius: 6px;
            padding: 0.6rem; margin-bottom: 0.5rem; font-size: 0.85rem; }
    .card a { color: #1a1a2e; text-decoration: none; font-weight: 600; }
    .card .meta { color: #888; font-size: 0.75rem; margin-top: 0.3rem; }
    .badge { display: inline-block; padding: 0.15rem 0.4rem; border-radius: 4px;
             font-size: 0.7rem; font-weight: 600; }
    .badge-high { background: #fee2e2; color: #b91c1c; }
    .badge-medium { background: #fef9c3; color: #92400e; }
    .badge-low { background: #dcfce7; color: #166534; }
    form.add { padding: 0 1rem 1rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }
    form.add textarea { flex: 1; min-width: 200px; padding: 0.5rem; border-radius: 6px;
                        border: 1px solid #ccc; font-size: 0.9rem; }
    form.add select, form.add button { padding: 0.5rem; border-radius: 6px; border: 1px solid #ccc; }
    form.add button { background: #1a1a2e; color: #fff; border: none; cursor: pointer; }
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

<div class="board">
  {% for col_status, col_label in columns %}
  <div class="col">
    <h2>{{ col_label }} <span class="count">({{ tasks|selectattr('status','equalto',col_status)|list|length }})</span></h2>
    {% for t in tasks if t.status == col_status %}
    <div class="card">
      <a href="/tasks/{{ t.id }}">#{{ t.id }} {{ t.prompt[:60] }}{% if t.prompt|length > 60 %}...{% endif %}</a>
      <div class="meta">
        <span class="badge badge-{{ t.get('priority','medium') }}">{{ t.get('priority','medium') }}</span>
        {% if t.get('parent') %} &middot; subtask of #{{ t.parent }}{% endif %}
        {% if t.get('created_at') %} &middot; {{ t.created_at[:10] }}{% endif %}
      </div>
    </div>
    {% else %}
    <div style="color:#bbb;font-size:0.8rem">&mdash;</div>
    {% endfor %}
  </div>
  {% endfor %}
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
<p>Status: <strong>{{ task.status }}</strong>
   | Priority: {{ task.get('priority','medium') }}
   {% if task.get('parent') %}| Subtask of <a href="/tasks/{{ task.parent }}">#{{ task.parent }}</a>{% endif %}
   {% if task.get('created_at') %}| Created: <span class="timestamp">{{ task.created_at[:19] }}</span>{% endif %}
   {% if task.get('completed_at') %}| Completed: <span class="timestamp">{{ task.completed_at[:19] }}</span>{% endif %}
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
  {% if task.status in ('in_progress', 'plan_review', 'approved') %}
  <form method="post" action="/tasks/{{ task.id }}/cancel" onsubmit="return confirm('Cancel this task?')">
    <button class="btn btn-cancel btn-sm">Cancel</button>
  </form>
  {% endif %}

  {# Retry: only for failed, rejected, or done #}
  {% if task.status in ('failed', 'rejected', 'done') %}
  <form method="post" action="/tasks/{{ task.id }}/retry">
    <button class="btn btn-retry btn-sm">Retry / Requeue</button>
  </form>
  {% endif %}

  {# Edit: available for pending, failed, rejected #}
  {% if task.status in ('pending', 'failed', 'rejected') %}
  <button class="btn btn-edit btn-sm" onclick="document.getElementById('edit-form').style.display='block'">Edit</button>
  {% endif %}

  {# Delete: available unless in_progress #}
  {% if task.status != 'in_progress' %}
  <form method="post" action="/tasks/{{ task.id }}/delete" onsubmit="return confirm('Delete task #{{ task.id }}? This cannot be undone.')">
    <button class="btn btn-delete btn-sm">Delete</button>
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

BOARD_COLUMNS = [
    ("pending",      "Pending"),
    ("in_progress",  "In Progress"),
    ("plan_review",  "Awaiting Approval"),
    ("push_review",  "Push Review"),
    ("decomposed",   "Decomposed"),
    ("done",         "Done"),
    ("failed",       "Failed"),
]


@app.get("/")
def board():
    data = load_tasks()
    return render_template_string(BOARD_HTML, tasks=data["tasks"], columns=BOARD_COLUMNS)


@app.post("/tasks")
def add_task():
    prompt = request.form.get("prompt", "").strip()
    priority = request.form.get("priority", "medium")
    if not prompt:
        return redirect(url_for("board"))

    data = load_tasks()
    task = {
        "id": next_id(data["tasks"]),
        "status": "pending",
        "prompt": prompt,
        "priority": priority,
        "parent": None,
        "plan": None,
        "created_at": datetime.utcnow().isoformat(),
        "completed_at": None,
        "summary": None,
    }
    data["tasks"].append(task)
    save_tasks(data)
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
    data = load_tasks()
    for t in data["tasks"]:
        if t["id"] == task_id and t["status"] in ("pending", "failed", "rejected"):
            if prompt:
                t["prompt"] = prompt
            t["priority"] = priority
            break
    save_tasks(data)
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/delete")
def delete_task(task_id: int):
    data = load_tasks()
    data["tasks"] = [t for t in data["tasks"] if t["id"] != task_id or t["status"] == "in_progress"]
    save_tasks(data)
    return redirect(url_for("board"))


@app.post("/tasks/<int:task_id>/approve")
def approve_task(task_id: int):
    data = load_tasks()
    for t in data["tasks"]:
        if t["id"] == task_id and t["status"] == "plan_review":
            t["status"] = "approved"
            break
    save_tasks(data)
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/reject")
def reject_task(task_id: int):
    feedback = request.form.get("feedback", "")
    data = load_tasks()
    for t in data["tasks"]:
        if t["id"] == task_id and t["status"] == "plan_review":
            t["status"] = "rejected"
            if feedback:
                t["summary"] = f"Rejected: {feedback}"
            break
    save_tasks(data)
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/cancel")
def cancel_task(task_id: int):
    data = load_tasks()
    for t in data["tasks"]:
        if t["id"] == task_id and t["status"] in ("in_progress", "plan_review", "approved"):
            t["status"] = "failed"
            t["summary"] = (t.get("summary") or "") + "\nCancelled by user via Web UI."
            break
    save_tasks(data)
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/retry")
def retry_task(task_id: int):
    data = load_tasks()
    for t in data["tasks"]:
        if t["id"] == task_id and t["status"] in ("failed", "rejected", "done"):
            t["status"] = "pending"
            t["completed_at"] = None
            t["summary"] = None
            t["plan"] = None
            break
    save_tasks(data)
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/approve-push")
def approve_push(task_id: int):
    data = load_tasks()
    for t in data["tasks"]:
        if t["id"] == task_id and t["status"] == "push_review":
            t["status"] = "pushed"
            break
    save_tasks(data)
    return redirect(url_for("task_detail", task_id=task_id))


@app.post("/tasks/<int:task_id>/reject-push")
def reject_push(task_id: int):
    data = load_tasks()
    for t in data["tasks"]:
        if t["id"] == task_id and t["status"] == "push_review":
            t["status"] = "done"
            t["summary"] = (t.get("summary") or "") + "\nPush skipped by user (local commit only)."
            break
    save_tasks(data)
    return redirect(url_for("task_detail", task_id=task_id))


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
