"""Tests for dependency graph enforcement — on_task_complete and end-to-end wiring."""

import json
import pytest
import dispatcher
import task_store
import web_manager
from helpers import write_tasks


class TestOnTaskComplete:
    """on_task_complete propagates a task's completion through the dependency graph:
    1. Removes the task from blocked_on of its listed dependents (reverse index).
    2. Decrements unresolved_children on the parent task.
    Both mutations happen in a single locked_update for atomicity."""

    def test_clears_blocked_on_for_dependents(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        data = {"tasks": [
            {"id": 1, "status": "done", "parent": None, "dependents": [2, 3]},
            {"id": 2, "status": "pending", "parent": None, "blocked_on": [1]},
            {"id": 3, "status": "pending", "parent": None, "blocked_on": [1, 4]},
        ]}
        write_tasks(tf, data)
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        dispatcher.on_task_complete(1)

        tasks = {t["id"]: t for t in json.loads(tf.read_text())["tasks"]}
        assert tasks[2]["blocked_on"] == []       # fully unblocked
        assert tasks[3]["blocked_on"] == [4]      # only id=1 removed

    def test_decrements_unresolved_children_on_parent(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        data = {"tasks": [
            {"id": 1, "status": "decomposed", "unresolved_children": 2},
            {"id": 2, "status": "done", "parent": 1, "blocked_on": [], "dependents": []},
        ]}
        write_tasks(tf, data)
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        dispatcher.on_task_complete(2)

        tasks = {t["id"]: t for t in json.loads(tf.read_text())["tasks"]}
        assert tasks[1]["unresolved_children"] == 1

    def test_unresolved_children_does_not_go_below_zero(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        data = {"tasks": [
            {"id": 1, "status": "decomposed", "unresolved_children": 0},
            {"id": 2, "status": "done", "parent": 1, "blocked_on": [], "dependents": []},
        ]}
        write_tasks(tf, data)
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        dispatcher.on_task_complete(2)

        tasks = {t["id"]: t for t in json.loads(tf.read_text())["tasks"]}
        assert tasks[1]["unresolved_children"] == 0

    def test_no_parent_does_not_crash(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        data = {"tasks": [{"id": 5, "status": "done", "parent": None,
                           "blocked_on": [], "dependents": []}]}
        write_tasks(tf, data)
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        dispatcher.on_task_complete(5)  # should not raise

    def test_only_touches_listed_dependents_not_all_tasks(self, tmp_path, monkeypatch):
        """Verify the reverse index is used — unrelated tasks' blocked_on is untouched."""
        tf = tmp_path / "tasks.json"
        data = {"tasks": [
            {"id": 1, "status": "done", "parent": None, "dependents": [2]},
            {"id": 2, "status": "pending", "parent": None, "blocked_on": [1]},
            # id=3 is NOT in id=1's dependents — its blocked_on must be unchanged
            {"id": 3, "status": "pending", "parent": None, "blocked_on": [1]},
        ]}
        write_tasks(tf, data)
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        dispatcher.on_task_complete(1)

        tasks = {t["id"]: t for t in json.loads(tf.read_text())["tasks"]}
        assert tasks[2]["blocked_on"] == []   # in dependents — unblocked
        assert tasks[3]["blocked_on"] == [1]  # not in dependents — untouched

    def test_nonexistent_task_is_noop(self, tmp_path, monkeypatch):
        """Completing a task_id that doesn't exist in tasks.json must be a silent no-op."""
        tf = tmp_path / "tasks.json"
        write_tasks(tf, {"tasks": [
            {"id": 1, "status": "pending", "parent": None, "blocked_on": [999]},
        ]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        dispatcher.on_task_complete(999)  # task 999 doesn't exist

        tasks = json.loads(tf.read_text())["tasks"]
        assert tasks[0]["blocked_on"] == [999]  # nothing changed

    def test_unblocks_dependents_and_decrements_parent(self, tmp_path, monkeypatch):
        """The most common real case: a subtask that has both dependents (siblings
        waiting on it) and a parent (whose unresolved_children counter needs decrementing).
        Both must happen in the same locked_update call."""
        tf = tmp_path / "tasks.json"
        data = {"tasks": [
            {"id": 1, "status": "decomposed", "unresolved_children": 2,
             "children": [2, 3]},
            {"id": 2, "status": "done", "parent": 1,
             "dependents": [3], "blocked_on": []},
            {"id": 3, "status": "pending", "parent": 1,
             "dependents": [], "blocked_on": [2]},
        ]}
        write_tasks(tf, data)
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        dispatcher.on_task_complete(2)

        tasks = {t["id"]: t for t in json.loads(tf.read_text())["tasks"]}
        assert tasks[3]["blocked_on"] == []          # sibling unblocked
        assert tasks[1]["unresolved_children"] == 1  # parent decremented

    def test_backward_compat_missing_dependents_field(self, tmp_path, monkeypatch):
        """Tasks without `dependents` (old schema) must not crash on_task_complete."""
        tf = tmp_path / "tasks.json"
        write_tasks(tf, {"tasks": [
            {"id": 1, "status": "done", "parent": None},
            {"id": 2, "status": "pending", "blocked_on": [1]},
        ]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        dispatcher.on_task_complete(1)  # must not raise

        tasks = {t["id"]: t for t in json.loads(tf.read_text())["tasks"]}
        assert tasks[2]["blocked_on"] == [1]  # expected: old tasks aren't auto-unblocked


class TestDependencyGraphIntegration:
    """
    End-to-end: approve_task (web route) wires the graph, then dispatcher
    functions drive the completion cycle. Both share the same tasks.json.
    """

    def test_full_dependency_cycle(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(web_manager, "STATUS_FILE", tmp_path / "status.json")

        # Parent task at plan_review with a decompose plan: A then B (B depends on A)
        plan = json.dumps({"decision": "decompose", "subtasks": [
            {"prompt": "do A", "depends_on": []},
            {"prompt": "do B", "depends_on": [0]},
        ]})
        write_tasks(tf, {
            "tasks": [{"id": 1, "status": "plan_review", "prompt": "parent",
                       "priority": "medium", "plan": plan, "depth": 0,
                       "plan_model": "sonnet", "exec_model": "sonnet",
                       "auto_approve": False, "children": [], "unresolved_children": 0}],
            "next_id": 2,
        })

        # Step 1: approve via web route
        web_manager.app.config["TESTING"] = True
        with web_manager.app.test_client() as client:
            assert client.post("/tasks/1/approve").status_code == 302

        tasks = {t["id"]: t for t in json.loads(tf.read_text())["tasks"]}
        parent = tasks[1]
        a = next(t for t in tasks.values() if t.get("prompt") == "do A")
        b = next(t for t in tasks.values() if t.get("prompt") == "do B")

        assert parent["status"] == "decomposed"
        assert parent["unresolved_children"] == 2
        assert set(parent["children"]) == {a["id"], b["id"]}
        assert a["blocked_on"] == [] and a["dependents"] == [b["id"]]
        assert b["blocked_on"] == [a["id"]] and b["dependents"] == []

        # Step 2: pick_next_task respects blocked_on — only A is pickable
        all_tasks = list(tasks.values())
        picked = dispatcher.pick_next_task(all_tasks)
        assert picked["id"] == a["id"]
        # B alone is not pickable (blocked_on is non-empty)
        assert dispatcher.pick_next_task(
            [t for t in all_tasks if t["id"] == b["id"]]
        ) is None

        # Step 3: complete A → B unblocked, parent decremented
        dispatcher.on_task_complete(a["id"])

        tasks = {t["id"]: t for t in json.loads(tf.read_text())["tasks"]}
        assert tasks[b["id"]]["blocked_on"] == []
        assert tasks[1]["unresolved_children"] == 1

        # Step 4: pick_next_task now picks B.
        # Mark A as done in the in-memory dict (not the file) since pick_next_task
        # takes a list argument — it doesn't read from disk. The file still has
        # A's original status; only blocked_on was updated by on_task_complete.
        tasks[a["id"]]["status"] = "done"
        picked2 = dispatcher.pick_next_task(list(tasks.values()))
        assert picked2["id"] == b["id"]

        # Step 5: complete B → parent fully resolved
        dispatcher.on_task_complete(b["id"])

        tasks = {t["id"]: t for t in json.loads(tf.read_text())["tasks"]}
        assert tasks[1]["unresolved_children"] == 0
