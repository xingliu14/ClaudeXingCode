"""Tests for web_manager.py Flask routes."""

import json
import pytest
import task_store
import web_manager
from helpers import write_tasks


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    """Flask test client with tasks.json and status file isolated in tmp_path.

    Yields (client, tf) where tf is the tmp tasks.json path.
    All routes read/write through task_store.TASKS_FILE, so pointing it at
    tmp_path gives each test a fresh, isolated data store.
    """
    tf = tmp_path / "tasks.json"
    write_tasks(tf, {"tasks": []})
    monkeypatch.setattr(task_store, "TASKS_FILE", tf)
    monkeypatch.setattr(web_manager, "STATUS_FILE", tmp_path / "status.json")
    web_manager.app.config["TESTING"] = True
    web_manager.app.config["SECRET_KEY"] = "test-secret"
    with web_manager.app.test_client() as client:
        with client.session_transaction() as sess:
            sess["account"] = "personal"
        yield client, tf


class TestBoardRoute:
    """GET / — the main dashboard page that lists all tasks."""

    def test_returns_200(self, web_client):
        client, _ = web_client
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"ClaudeXingCode Dashboard" in resp.data

    def test_blocked_task_shows_blocked_badge(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [
            {"id": 1, "status": "pending", "prompt": "Blocked task", "priority": "medium",
             "blocked_on": [2]},
            {"id": 2, "status": "pending", "prompt": "Blocker", "priority": "medium",
             "blocked_on": []},
        ]})
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"blocked 1" in resp.data

    def test_unblocked_task_no_blocked_badge(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [
            {"id": 1, "status": "pending", "prompt": "Free task", "priority": "medium",
             "blocked_on": []},
        ]})
        resp = client.get("/")
        assert b"blocked 1" not in resp.data


class TestAddTaskRoute:
    """POST /tasks — create a new task from the web form."""

    def test_creates_task(self, web_client):
        client, tf = web_client
        resp = client.post("/tasks", data={"title": "Fix the bug", "prompt": "Fix the bug", "priority": "high"})
        assert resp.status_code == 302

        data = json.loads(tf.read_text())
        assert len(data["tasks"]) == 1
        task = data["tasks"][0]
        assert task["id"] == 1
        assert task["prompt"] == "Fix the bug"
        assert task["priority"] == "high"
        assert task["status"] == "pending"
        assert task["plan_model"] == "sonnet"
        assert task["exec_model"] == "sonnet"

    def test_creates_task_full_schema(self, web_client):
        client, tf = web_client
        client.post("/tasks", data={"title": "Schema test", "prompt": "Schema test", "priority": "medium"})

        task = json.loads(tf.read_text())["tasks"][0]
        assert task["depth"] == 0
        assert task["blocked_on"] == []
        assert task["depends_on"] == []
        assert task["dependents"] == []
        assert task["children"] == []
        assert task["unresolved_children"] == 0
        assert task["report"] is None
        assert task["parent"] is None

    def test_creates_task_with_model(self, web_client):
        client, tf = web_client
        resp = client.post("/tasks", data={"title": "Think hard", "prompt": "Think hard", "priority": "high", "plan_model": "opus", "exec_model": "opus"})
        assert resp.status_code == 302

        task = json.loads(tf.read_text())["tasks"][0]
        assert task["plan_model"] == "opus"
        assert task["exec_model"] == "opus"

    def test_invalid_model_defaults_to_sonnet(self, web_client):
        client, tf = web_client
        resp = client.post("/tasks", data={"title": "Test", "prompt": "Test", "priority": "medium", "model": "gpt4"})
        assert resp.status_code == 302

        task = json.loads(tf.read_text())["tasks"][0]
        assert task["plan_model"] == "sonnet"
        assert task["exec_model"] == "sonnet"

    def test_empty_prompt_no_task(self, web_client):
        client, tf = web_client
        resp = client.post("/tasks", data={"prompt": "  ", "priority": "medium"})
        assert resp.status_code == 302
        assert len(json.loads(tf.read_text())["tasks"]) == 0


class TestTaskDetailRoute:
    def test_returns_task(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "pending", "prompt": "Test task",
                                    "priority": "medium", "parent": None, "plan": None,
                                    "summary": None}]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"Test task" in resp.data

    def test_404_not_found(self, web_client):
        client, _ = web_client
        resp = client.get("/tasks/999")
        assert resp.status_code == 404

    def test_subtask_blocked_on_shown_in_detail(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [
            {"id": 1, "status": "decomposed", "prompt": "Parent", "priority": "medium",
             "parent": None, "plan": None, "summary": None,
             "children": [2, 3], "unresolved_children": 2},
            {"id": 2, "status": "pending", "prompt": "First subtask", "priority": "medium",
             "parent": 1, "blocked_on": []},
            {"id": 3, "status": "pending", "prompt": "Second subtask", "priority": "medium",
             "parent": 1, "blocked_on": [2]},
        ]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"blocked(#2)" in resp.data

    def test_subtask_not_blocked_no_indicator(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [
            {"id": 1, "status": "decomposed", "prompt": "Parent", "priority": "medium",
             "parent": None, "plan": None, "summary": None,
             "children": [2], "unresolved_children": 1},
            {"id": 2, "status": "pending", "prompt": "Free subtask", "priority": "medium",
             "parent": 1, "blocked_on": []},
        ]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"blocked(" not in resp.data


class TestApproveRoute:
    """POST /tasks/<id>/approve — two code paths:
    'execute' decision sets status to in_progress,
    'decompose' decision creates subtasks and sets parent to 'decomposed'."""

    def test_approves_plan_review_task(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                                    "priority": "medium"}]})
        resp = client.post("/tasks/1/approve")
        assert resp.status_code == 302
        assert json.loads(tf.read_text())["tasks"][0]["status"] == "executing"

    def test_does_not_approve_non_plan_review(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "pending", "prompt": "x",
                                    "priority": "medium"}]})
        client.post("/tasks/1/approve")
        assert json.loads(tf.read_text())["tasks"][0]["status"] == "pending"

    def test_approve_execute_plan_sets_in_progress(self, web_client):
        client, tf = web_client
        plan = json.dumps({"decision": "execute", "plan": "1. do it"})
        write_tasks(tf, {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                                    "priority": "medium", "plan": plan, "depth": 0,
                                    "children": [], "unresolved_children": 0}], "next_id": 2})
        client.post("/tasks/1/approve")

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["status"] == "executing"
        assert len(result["tasks"]) == 1  # no subtasks created

    def test_approve_decompose_sets_parent_decomposed(self, web_client):
        client, tf = web_client
        plan = json.dumps({"decision": "decompose", "subtasks": [
            {"prompt": "A", "depends_on": []},
            {"prompt": "B", "depends_on": []},
        ]})
        write_tasks(tf, {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                                    "priority": "medium", "plan": plan, "depth": 0,
                                    "plan_model": "sonnet", "exec_model": "sonnet",
                                    "auto_approve": False,
                                    "children": [], "unresolved_children": 0}], "next_id": 2})
        client.post("/tasks/1/approve")

        result = json.loads(tf.read_text())
        parent = result["tasks"][0]
        assert parent["status"] == "decomposed"
        assert parent["unresolved_children"] == 2
        assert len(parent["children"]) == 2

    def test_approve_decompose_creates_subtasks_with_correct_schema(self, web_client):
        """Subtask B depends_on [0] (positional index of A). The approve route
        maps positional indices to real task IDs in depends_on, blocked_on,
        and the reverse index (dependents)."""
        client, tf = web_client
        plan = json.dumps({"decision": "decompose", "subtasks": [
            {"prompt": "do A", "depends_on": []},
            {"prompt": "do B", "depends_on": [0]},
        ]})
        write_tasks(tf, {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                                    "priority": "high", "plan": plan, "depth": 0,
                                    "plan_model": "opus", "exec_model": "sonnet",
                                    "auto_approve": False,
                                    "children": [], "unresolved_children": 0}], "next_id": 2})
        client.post("/tasks/1/approve")

        result = json.loads(tf.read_text())
        assert len(result["tasks"]) == 3

        subtasks = [t for t in result["tasks"] if t.get("parent") == 1]
        a = next(t for t in subtasks if t["prompt"] == "do A")
        b = next(t for t in subtasks if t["prompt"] == "do B")

        assert a["depth"] == 1
        assert a["blocked_on"] == []
        assert a["depends_on"] == []
        assert a["priority"] == "high"
        assert a["plan_model"] == "opus"
        assert b["depends_on"] == [a["id"]]
        assert b["blocked_on"] == [a["id"]]
        assert b["id"] in a["dependents"]
        assert b["dependents"] == []

    def test_approve_decompose_inherits_parent_fields(self, web_client):
        client, tf = web_client
        plan = json.dumps({"decision": "decompose", "subtasks": [
            {"prompt": "sub", "depends_on": []}
        ]})
        write_tasks(tf, {"tasks": [{"id": 5, "status": "plan_review", "prompt": "parent",
                                    "priority": "high", "plan": plan, "depth": 1,
                                    "plan_model": "opus", "exec_model": "haiku",
                                    "auto_approve": True,
                                    "children": [], "unresolved_children": 0}], "next_id": 10})
        client.post("/tasks/5/approve")

        result = json.loads(tf.read_text())
        sub = next(t for t in result["tasks"] if t.get("parent") == 5)
        assert sub["depth"] == 2
        assert sub["priority"] == "high"
        assert sub["plan_model"] == "opus"
        assert sub["exec_model"] == "haiku"
        assert sub["auto_approve"] is True
        assert sub["id"] == 10


class TestRejectRoute:
    """POST /tasks/<id>/reject — resets to pending, clears plan, appends feedback."""

    def test_rejects_sets_pending(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                                    "priority": "medium", "plan": "old plan"}]})
        resp = client.post("/tasks/1/reject", data={"feedback": "Bad plan"})
        assert resp.status_code == 302

        task = json.loads(tf.read_text())["tasks"][0]
        assert task["status"] == "pending"
        assert "stop_reason" not in task

    def test_rejects_clears_plan(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                                    "priority": "medium", "plan": "old plan"}]})
        client.post("/tasks/1/reject", data={"feedback": ""})
        assert json.loads(tf.read_text())["tasks"][0]["plan"] is None

    def test_rejects_appends_rejection_comment(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                                    "priority": "medium", "plan": "p"}]})
        client.post("/tasks/1/reject", data={"feedback": "Please decompose"})

        task = json.loads(tf.read_text())["tasks"][0]
        assert task["rejection_comments"] == [{"round": 1, "comment": "Please decompose"}]

    def test_rejects_increments_round(self, web_client):
        client, tf = web_client
        existing = [{"round": 1, "comment": "first rejection"}]
        write_tasks(tf, {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                                    "priority": "medium", "plan": "p",
                                    "rejection_comments": existing}]})
        client.post("/tasks/1/reject", data={"feedback": "still bad"})

        task = json.loads(tf.read_text())["tasks"][0]
        assert len(task["rejection_comments"]) == 2
        assert task["rejection_comments"][1] == {"round": 2, "comment": "still bad"}

    def test_rejects_without_feedback_appends_empty_comment(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                                    "priority": "medium", "plan": "p"}]})
        client.post("/tasks/1/reject", data={"feedback": ""})

        task = json.loads(tf.read_text())["tasks"][0]
        assert task["rejection_comments"] == [{"round": 1, "comment": ""}]


class TestCancelRoute:
    """Cancel is allowed from both in_progress and plan_review states."""

    def test_cancels_in_progress(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "executing", "prompt": "x",
                                    "priority": "medium"}]})
        resp = client.post("/tasks/1/cancel")
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["status"] == "stopped"
        assert result["tasks"][0]["stop_reason"] == "cancelled"

    def test_cancels_plan_review(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                                    "priority": "medium", "plan": "some plan"}]})
        client.post("/tasks/1/cancel")

        task = json.loads(tf.read_text())["tasks"][0]
        assert task["status"] == "stopped"
        assert task["stop_reason"] == "cancelled"

    def test_ignores_pending(self, web_client):
        """Cancel should not affect tasks that aren't in_progress or plan_review."""
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "pending", "prompt": "x",
                                    "priority": "medium"}]})
        client.post("/tasks/1/cancel")
        assert json.loads(tf.read_text())["tasks"][0]["status"] == "pending"


class TestRetryRoute:
    """Retry is allowed from both stopped and done states — resets all execution state."""

    def test_requeues_stopped_task(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "stopped", "prompt": "x",
                                    "priority": "medium", "stop_reason": "rejected",
                                    "summary": "old", "result": {"summary": "old", "artifacts": []},
                                    "plan": "old plan",
                                    "retry_count": 3}]})
        resp = client.post("/tasks/1/retry")
        assert resp.status_code == 302

        task = json.loads(tf.read_text())["tasks"][0]
        assert task["status"] == "pending"
        assert task["summary"] is None
        assert task["result"] is None
        assert task["plan"] is None
        assert "stop_reason" not in task
        # retry_count must reset to 0, otherwise a doom-looped task that retries
        # would immediately hit the MAX_RETRIES guard again on next execution.
        assert task["retry_count"] == 0

    def test_requeues_done_task(self, web_client):
        """Done tasks can also be retried (e.g., user wants a second attempt)."""
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "done", "prompt": "x",
                                    "priority": "medium", "summary": "completed",
                                    "plan": "old plan", "completed_at": "2026-03-23",
                                    "retry_count": 1}]})
        client.post("/tasks/1/retry")

        task = json.loads(tf.read_text())["tasks"][0]
        assert task["status"] == "pending"
        assert task["completed_at"] is None
        assert task["retry_count"] == 0

    def test_retry_clears_rejection_comments(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "stopped", "prompt": "x",
                                    "priority": "medium", "plan": None,
                                    "rejection_comments": [{"round": 1, "comment": "bad"}]}]})
        client.post("/tasks/1/retry")

        task = json.loads(tf.read_text())["tasks"][0]
        assert task["rejection_comments"] == []

    def test_retry_ignores_pending(self, web_client):
        """Retry should not affect tasks that aren't stopped or done."""
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "pending", "prompt": "x",
                                    "priority": "medium"}]})
        client.post("/tasks/1/retry")
        assert json.loads(tf.read_text())["tasks"][0]["status"] == "pending"

    def test_result_struct_backward_compat_renders_summary(self, web_client):
        """Task detail page renders result.summary via backward-compat template logic.
        Seeds a done task with a typed result dict and verifies the summary text
        appears in the rendered page (covers the 'task.get(result).get(summary)' path)."""
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "done", "prompt": "Some work",
                                    "priority": "medium", "parent": None, "plan": None,
                                    "summary": None,
                                    "result": {"summary": "All done successfully",
                                               "artifacts": []}}]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"All done successfully" in resp.data


class TestDeleteRoute:
    """POST /tasks/<id>/delete — removes a task unless it's in_progress (safety guard)."""

    def test_deletes_non_running_task(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "pending", "prompt": "x"}]})
        resp = client.post("/tasks/1/delete")
        assert resp.status_code == 302
        assert len(json.loads(tf.read_text())["tasks"]) == 0

    def test_does_not_delete_in_progress(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "in_progress", "prompt": "x"}]})
        client.post("/tasks/1/delete")
        assert len(json.loads(tf.read_text())["tasks"]) == 1


class TestSetModelRoute:
    """POST /tasks/<id>/set-model — updates plan_model and/or exec_model.
    Invalid model names are silently ignored (original value preserved)."""

    def test_changes_plan_model(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                                    "priority": "medium", "plan_model": "sonnet",
                                    "exec_model": "sonnet"}]})
        resp = client.post("/tasks/1/set-model", data={"plan_model": "opus", "exec_model": "sonnet"})
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["plan_model"] == "opus"
        assert result["tasks"][0]["exec_model"] == "sonnet"

    def test_changes_exec_model(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                                    "priority": "medium", "plan_model": "sonnet",
                                    "exec_model": "sonnet"}]})
        resp = client.post("/tasks/1/set-model", data={"plan_model": "sonnet", "exec_model": "opus"})
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["plan_model"] == "sonnet"
        assert result["tasks"][0]["exec_model"] == "opus"

    def test_changes_both_models(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "pending", "prompt": "x",
                                    "priority": "medium", "plan_model": "sonnet",
                                    "exec_model": "sonnet"}]})
        resp = client.post("/tasks/1/set-model", data={"plan_model": "haiku", "exec_model": "opus"})
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["plan_model"] == "haiku"
        assert result["tasks"][0]["exec_model"] == "opus"

    def test_ignores_invalid_model(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "pending", "prompt": "x",
                                    "priority": "medium", "plan_model": "sonnet",
                                    "exec_model": "sonnet"}]})
        client.post("/tasks/1/set-model", data={"plan_model": "gpt4", "exec_model": "gpt4"})

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["plan_model"] == "sonnet"
        assert result["tasks"][0]["exec_model"] == "sonnet"

    def test_works_on_any_status(self, web_client):
        client, tf = web_client
        for status in ("pending", "plan_review", "done", "stopped"):
            write_tasks(tf, {"tasks": [{"id": 1, "status": status, "prompt": "x",
                                        "plan_model": "sonnet", "exec_model": "sonnet"}]})
            client.post("/tasks/1/set-model", data={"plan_model": "opus", "exec_model": "haiku"})

            result = json.loads(tf.read_text())
            assert result["tasks"][0]["plan_model"] == "opus"
            assert result["tasks"][0]["exec_model"] == "haiku"


class TestEditTaskModels:
    """POST /tasks/<id>/edit — edit prompt, priority, and models (blocked while in_progress)."""

    def test_edit_updates_both_models(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "pending", "prompt": "x",
                                    "priority": "medium", "plan_model": "sonnet",
                                    "exec_model": "sonnet"}]})
        client.post("/tasks/1/edit", data={
            "prompt": "updated", "priority": "high",
            "plan_model": "opus", "exec_model": "haiku"
        })

        task = json.loads(tf.read_text())["tasks"][0]
        assert task["plan_model"] == "opus"
        assert task["exec_model"] == "haiku"
        assert task["priority"] == "high"

    def test_edit_blocked_for_in_progress(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [{"id": 1, "status": "in_progress", "prompt": "x",
                                    "priority": "medium", "plan_model": "sonnet",
                                    "exec_model": "sonnet"}]})
        client.post("/tasks/1/edit", data={
            "prompt": "updated", "priority": "high",
            "plan_model": "opus", "exec_model": "opus"
        })

        task = json.loads(tf.read_text())["tasks"][0]
        assert task["plan_model"] == "sonnet"  # unchanged
        assert task["exec_model"] == "sonnet"


class TestArtifactRendering:
    """GET /tasks/<id> — artifact section renders each type correctly."""

    def _task(self, artifacts):
        return {
            "id": 1, "status": "done", "prompt": "Artifact task",
            "priority": "medium", "parent": None, "plan": None,
            "summary": None,
            "result": {"summary": None, "artifacts": artifacts},
        }

    def test_no_artifacts_section_when_empty(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [self._task([])]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"<h2>Artifacts</h2>" not in resp.data

    def test_git_commit_artifact_rendered(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [self._task([
            {"type": "git_commit", "ref": "abc1234567890", "message": "fix bug in parser"}
        ])]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"abc12345" in resp.data       # first 8 chars of ref
        assert b"fix bug in parser" in resp.data

    def test_text_artifact_rendered(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [self._task([
            {"type": "text", "content": "Hello artifact world"}
        ])]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"Hello artifact world" in resp.data

    def test_document_artifact_uses_details_tag(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [self._task([
            {"type": "document", "content": "Long document content here"}
        ])]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"<details" in resp.data
        assert b"Long document content here" in resp.data

    def test_code_diff_uses_pre_tag(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [self._task([
            {"type": "code_diff", "content": "--- a/foo\n+++ b/foo\n@@ -1 +1 @@\n-old\n+new"}
        ])]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"artifact-code-diff" in resp.data

    def test_url_list_renders_anchor_tags(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [self._task([
            {"type": "url_list", "items": [
                {"url": "https://example.com", "title": "Example", "note": ""},
                {"url": "https://docs.python.org", "title": "Python Docs", "note": ""},
            ]}
        ])]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b'<a href=' in resp.data
        assert b"https://example.com" in resp.data

    def test_url_list_has_noopener_noreferrer(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [self._task([
            {"type": "url_list", "items": [
                {"url": "https://example.com", "title": "Example", "note": ""},
            ]}
        ])]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b'rel="noopener noreferrer"' in resp.data

    def test_url_list_blocks_javascript_scheme(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [self._task([
            {"type": "url_list", "items": [
                {"url": "javascript:alert(1)", "title": "", "note": ""},
            ]}
        ])]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b'href="javascript:' not in resp.data
        assert b"javascript:alert(1)" in resp.data

    def test_document_artifact_shows_path_when_file_based(self, web_client):
        client, tf = web_client
        write_tasks(tf, {"tasks": [self._task([
            {"type": "document", "path": "agent_log/tasks/task_1/document_1.md", "title": "My Doc"}
        ])]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"<details" in resp.data
        assert b"agent_log/tasks/task_1/document_1.md" in resp.data


class TestStatusRoute:
    def test_returns_idle_when_no_file(self, web_client):
        client, _ = web_client
        resp = client.get("/status")
        assert resp.status_code == 200
        assert resp.get_json()["state"] == "idle"


class TestReportDisplay:
    """GET /tasks/<id> — Report section visibility based on task.report field."""

    def test_report_section_shown_when_report_set(self, web_client):
        """Decomposed task with a report shows the consolidated report section."""
        client, tf = web_client
        write_tasks(tf, {"tasks": [{
            "id": 1, "status": "decomposed", "prompt": "Big task",
            "priority": "medium", "parent": None, "plan": None,
            "summary": None, "report": "All subtasks completed.",
        }]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"Consolidated report from all subtasks" in resp.data
        assert b"All subtasks completed." in resp.data

    def test_report_section_hidden_when_no_report(self, web_client):
        """Task with no report field should not render the report section."""
        client, tf = web_client
        write_tasks(tf, {"tasks": [{
            "id": 1, "status": "decomposed", "prompt": "Big task",
            "priority": "medium", "parent": None, "plan": None,
            "summary": None,
        }]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"Consolidated report" not in resp.data

    def test_report_shows_on_non_decomposed_task_too(self, web_client):
        """The report guard is status-agnostic: any task with report set shows it."""
        client, tf = web_client
        write_tasks(tf, {"tasks": [{
            "id": 1, "status": "done", "prompt": "Some task",
            "priority": "medium", "parent": None, "plan": None,
            "summary": None, "report": "Finished with flying colours.",
        }]})
        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"Consolidated report from all subtasks" in resp.data
        assert b"Finished with flying colours." in resp.data
