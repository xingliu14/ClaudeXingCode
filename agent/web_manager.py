"""
Web UI for the agentic coding team.

Routes:
  GET  /                    — task board
  POST /tasks               — add new task
  GET  /tasks/<id>          — task detail (plan, log, subtasks)
  POST /tasks/<id>/approve  — approve plan → dispatcher executes
  POST /tasks/<id>/reject   — reject plan with feedback
"""

import json
import os
from datetime import datetime
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, url_for

TASKS_FILE = Path(os.environ.get("TASKS_FILE", "/agent/tasks.json"))

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
    body { font-family: system-ui, sans-serif; margin: 0; background: #f5f5f5; }
    header { background: #1a1a2e; color: #fff; padding: 1rem 1.5rem; }
    header h1 { margin: 0; font-size: 1.2rem; }
    .board { display: flex; gap: 1rem; padding: 1rem; overflow-x: auto; }
    .col { background: #fff; border-radius: 8px; min-width: 220px; flex: 1; padding: 0.75rem; }
    .col h2 { font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em;
               color: #666; margin: 0 0 0.75rem; }
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
<header><h1>Agent Board</h1></header>

<form class="add" method="post" action="/tasks">
  <textarea name="prompt" rows="2" placeholder="New task… (speak or type)" required></textarea>
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
    <h2>{{ col_label }}</h2>
    {% for t in tasks if t.status == col_status %}
    <div class="card">
      <a href="/tasks/{{ t.id }}">#{{ t.id }} {{ t.prompt[:60] }}{% if t.prompt|length > 60 %}…{% endif %}</a>
      <div class="meta">
        <span class="badge badge-{{ t.get('priority','medium') }}">{{ t.get('priority','medium') }}</span>
        {% if t.get('parent') %} · subtask of #{{ t.parent }}{% endif %}
      </div>
    </div>
    {% else %}
    <div style="color:#bbb;font-size:0.8rem">—</div>
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
    body { font-family: system-ui, sans-serif; margin: 0; background: #f5f5f5; padding: 1rem; }
    h1 { font-size: 1.1rem; color: #1a1a2e; }
    pre { background: #1e1e1e; color: #d4d4d4; padding: 1rem; border-radius: 8px;
          overflow-x: auto; font-size: 0.8rem; white-space: pre-wrap; }
    .actions { display: flex; gap: 0.5rem; margin: 1rem 0; }
    .btn { padding: 0.5rem 1rem; border-radius: 6px; border: none; cursor: pointer; font-size: 0.9rem; }
    .btn-approve { background: #16a34a; color: #fff; }
    .btn-reject  { background: #dc2626; color: #fff; }
    a { color: #1a1a2e; }
    .subtasks { margin-top: 1rem; }
    .subtask-item { background: #fff; border: 1px solid #e0e0e0; border-radius: 6px;
                    padding: 0.5rem; margin-bottom: 0.4rem; font-size: 0.85rem; }
  </style>
</head>
<body>
<p><a href="/">← Board</a></p>
<h1>#{{ task.id }} — {{ task.prompt }}</h1>
<p>Status: <strong>{{ task.status }}</strong>
   | Priority: {{ task.get('priority','medium') }}
   {% if task.get('parent') %}| Subtask of <a href="/tasks/{{ task.parent }}">#{{ task.parent }}</a>{% endif %}
</p>

{% if task.status == 'plan_review' %}
<div class="actions">
  <form method="post" action="/tasks/{{ task.id }}/approve">
    <button class="btn btn-approve">Approve Plan</button>
  </form>
  <form method="post" action="/tasks/{{ task.id }}/reject">
    <input name="feedback" placeholder="Rejection reason (optional)" style="padding:0.4rem;border-radius:6px;border:1px solid #ccc">
    <button class="btn btn-reject">Reject</button>
  </form>
</div>
{% endif %}

{% if task.get('plan') %}
<h2>Plan</h2>
<pre>{{ task.plan }}</pre>
{% endif %}

{% if task.get('summary') %}
<h2>Result Summary</h2>
<pre>{{ task.summary }}</pre>
{% endif %}

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
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def board():
    data = load_tasks()
    columns = [
        ("pending",     "Pending"),
        ("in_progress", "In Progress"),
        ("plan_review", "Awaiting Approval"),
        ("decomposed",  "Decomposed"),
        ("done",        "Done"),
        ("failed",      "Failed"),
    ]
    return render_template_string(BOARD_HTML, tasks=data["tasks"], columns=columns)


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


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
