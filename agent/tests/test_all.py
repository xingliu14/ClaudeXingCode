"""
Tests for dispatcher.py, web_manager.py, daily_digest.py, and task_store.py.

All file I/O uses tmp_path fixtures; all external calls are mocked.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Wire all subpackages so imports work without install
_AGENT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_AGENT / "core"))        # task_store, progress_logger
sys.path.insert(0, str(_AGENT / "dispatcher"))  # dispatcher
sys.path.insert(0, str(_AGENT / "web"))         # web_manager
sys.path.insert(0, str(_AGENT))                 # daily_digest


# ============================================================================
# Helpers
# ============================================================================


def write_tasks(tf: Path, data: dict) -> None:
    """Write a tasks dict to a file and ensure the lock file exists."""
    tf.write_text(json.dumps(data))
    tf.with_suffix(".lock").touch(exist_ok=True)


# ============================================================================
# task_store.py tests
# ============================================================================


class TestTaskStore:
    def test_load_tasks_missing_file(self, tmp_path, monkeypatch):
        import task_store

        monkeypatch.setattr(task_store, "TASKS_FILE", tmp_path / "tasks.json")
        assert task_store.load_tasks() == {"tasks": []}

    def test_load_tasks_valid_json(self, tmp_path, monkeypatch):
        import task_store

        tf = tmp_path / "tasks.json"
        data = {"tasks": [{"id": 1, "status": "pending", "prompt": "hello"}]}
        tf.write_text(json.dumps(data))
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        assert task_store.load_tasks() == data

    def test_save_tasks_roundtrip(self, tmp_path, monkeypatch):
        import task_store

        tf = tmp_path / "tasks.json"
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        data = {"tasks": [{"id": 1, "status": "done"}]}
        task_store.save_tasks(data)
        assert json.loads(tf.read_text()) == data

    def test_locked_update(self, tmp_path, monkeypatch):
        import task_store

        tf = tmp_path / "tasks.json"
        write_tasks(tf, {"tasks": [{"id": 1, "status": "pending"}]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        def mutate(data):
            data["tasks"][0]["status"] = "done"

        result = task_store.locked_update(mutate)
        assert result["tasks"][0]["status"] == "done"
        assert json.loads(tf.read_text())["tasks"][0]["status"] == "done"

    def test_next_id_empty(self):
        from task_store import next_id

        data = {"tasks": []}
        assert next_id(data) == 1
        assert data["next_id"] == 2

    def test_next_id_existing(self):
        from task_store import next_id

        data = {"tasks": [{"id": 3}, {"id": 7}, {"id": 5}]}
        assert next_id(data) == 8
        assert data["next_id"] == 9

    def test_next_id_monotonic_after_delete(self):
        from task_store import next_id

        data = {"tasks": [{"id": 1}], "next_id": 2}
        assert next_id(data) == 2
        # Simulate deleting all tasks
        data["tasks"] = []
        assert next_id(data) == 3  # never reuses ID 1 or 2


# ============================================================================
# dispatcher.py tests
# ============================================================================


class TestUpdateTask:
    def test_updates_matching_task(self, tmp_path, monkeypatch):
        import dispatcher
        import task_store

        tf = tmp_path / "tasks.json"
        data = {"tasks": [
            {"id": 1, "status": "pending", "prompt": "a"},
            {"id": 2, "status": "pending", "prompt": "b"},
        ]}
        write_tasks(tf, data)
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        dispatcher.update_task(2, status="done", summary="finished")

        result = json.loads(tf.read_text())
        task2 = next(t for t in result["tasks"] if t["id"] == 2)
        assert task2["status"] == "done"
        assert task2["summary"] == "finished"
        # Task 1 unchanged
        task1 = next(t for t in result["tasks"] if t["id"] == 1)
        assert task1["status"] == "pending"


class TestPickNextTask:
    def test_returns_none_on_empty(self):
        from dispatcher import pick_next_task

        assert pick_next_task([]) is None

    def test_returns_none_when_no_pending(self):
        from dispatcher import pick_next_task

        tasks = [{"id": 1, "status": "done", "priority": "high"}]
        assert pick_next_task(tasks) is None

    def test_picks_highest_priority(self):
        from dispatcher import pick_next_task

        tasks = [
            {"id": 1, "status": "pending", "priority": "low"},
            {"id": 2, "status": "pending", "priority": "high"},
            {"id": 3, "status": "pending", "priority": "medium"},
        ]
        assert pick_next_task(tasks)["id"] == 2

    def test_breaks_ties_by_id(self):
        from dispatcher import pick_next_task

        tasks = [
            {"id": 5, "status": "pending", "priority": "high"},
            {"id": 3, "status": "pending", "priority": "high"},
        ]
        assert pick_next_task(tasks)["id"] == 3

    def test_skips_non_pending(self):
        from dispatcher import pick_next_task

        tasks = [
            {"id": 1, "status": "done", "priority": "high"},
            {"id": 2, "status": "in_progress", "priority": "high"},
            {"id": 3, "status": "pending", "priority": "low"},
        ]
        assert pick_next_task(tasks)["id"] == 3

    def test_default_priority_medium(self):
        from dispatcher import pick_next_task

        tasks = [
            {"id": 1, "status": "pending"},  # no priority key
            {"id": 2, "status": "pending", "priority": "low"},
        ]
        # default medium (1) < low (2), so id=1 picked
        assert pick_next_task(tasks)["id"] == 1


class TestIsTokenLimitError:
    def test_detects_rate_limit(self):
        from dispatcher import is_token_limit_error

        assert is_token_limit_error("Error: rate_limit_error — too many requests") is True

    def test_detects_token_limit(self):
        from dispatcher import is_token_limit_error

        assert is_token_limit_error("Stopped: token limit reached") is True

    def test_detects_context_window(self):
        from dispatcher import is_token_limit_error

        assert is_token_limit_error("context window exceeded") is True

    def test_ignores_normal_output(self):
        from dispatcher import is_token_limit_error

        assert is_token_limit_error("Task completed successfully") is False


class TestParseStreamJson:
    def test_extracts_result(self):
        from dispatcher import parse_stream_json

        raw = '{"type":"result","result":"the answer"}\n'
        assert parse_stream_json(raw) == "the answer"

    def test_extracts_assistant_text(self):
        from dispatcher import parse_stream_json

        raw = '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'
        assert parse_stream_json(raw) == "hello"

    def test_falls_back_to_raw(self):
        from dispatcher import parse_stream_json

        assert parse_stream_json("not json at all") == "not json at all"


class TestRunCC:
    def test_plan_runs_locally(self, monkeypatch):
        import dispatcher

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout="plan output", stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        code, output = dispatcher.run_cc_local("do something")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"  # runs locally, not via docker
        assert "--permission-mode" in cmd
        assert "plan" in cmd
        assert "--dangerously-skip-permissions" not in cmd
        assert "--model" in cmd
        # Default model is sonnet
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-sonnet-4-6"
        assert code == 0

    def test_plan_uses_specified_model(self, monkeypatch):
        import dispatcher

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout="plan output", stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.run_cc_local("do something", model="opus")

        cmd = mock_run.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-opus-4-6"

    def test_execute_runs_in_docker(self, monkeypatch):
        import dispatcher

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout="exec output", stderr="warn"
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        code, output = dispatcher.run_cc_docker("do something")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "docker"  # runs in container
        assert "run" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--permission-mode" not in cmd
        assert "--model" in cmd
        assert code == 0

    def test_execute_uses_specified_model(self, monkeypatch):
        import dispatcher

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout="exec output", stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.run_cc_docker("do something", model="haiku")

        cmd = mock_run.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-haiku-4-5-20251001"



class TestPlanTaskModel:
    def test_uses_plan_model(self, tmp_path, monkeypatch):
        import dispatcher
        import task_store

        tf = tmp_path / "tasks.json"
        task = {"id": 1, "status": "pending", "prompt": "test",
                "plan_model": "opus", "exec_model": "haiku", "priority": "medium"}
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout='{"type":"result","result":"plan"}', stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.plan_task(task)

        cmd = mock_run.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-opus-4-6"

    def test_falls_back_to_legacy_model(self, tmp_path, monkeypatch):
        import dispatcher
        import task_store

        tf = tmp_path / "tasks.json"
        task = {"id": 1, "status": "pending", "prompt": "test",
                "model": "haiku", "priority": "medium"}
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout='{"type":"result","result":"plan"}', stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.plan_task(task)

        cmd = mock_run.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-haiku-4-5-20251001"


class TestExecuteTaskModel:
    def test_uses_exec_model(self, tmp_path, monkeypatch):
        import dispatcher
        import task_store

        tf = tmp_path / "tasks.json"
        task = {"id": 1, "status": "in_progress", "prompt": "test",
                "plan_model": "sonnet", "exec_model": "opus",
                "plan": "the plan", "priority": "medium"}
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout='{"type":"result","result":"done"}', stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.execute_task(task)

        # First call is docker run (execution), subsequent calls are git commit
        cmd = mock_run.call_args_list[0][0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-opus-4-6"

    def test_falls_back_to_legacy_model(self, tmp_path, monkeypatch):
        import dispatcher
        import task_store

        tf = tmp_path / "tasks.json"
        task = {"id": 1, "status": "in_progress", "prompt": "test",
                "model": "opus", "plan": "the plan", "priority": "medium"}
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout='{"type":"result","result":"done"}', stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.execute_task(task)

        cmd = mock_run.call_args_list[0][0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-opus-4-6"


class TestGitCommit:
    def test_runs_in_docker(self, monkeypatch):
        import dispatcher

        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.git_commit("test commit message")

        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "docker"
        assert "run" in cmd
        assert "bash" in cmd

    def test_falls_back_to_local_on_docker_failure(self, monkeypatch):
        import dispatcher

        mock_run = MagicMock(return_value=MagicMock(returncode=1, stderr="docker not found"))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.git_commit("test commit message")

        # 1 docker attempt + 2 local git calls
        assert mock_run.call_count == 3
        assert mock_run.call_args_list[1][0][0] == ["git", "add", "-A"]
        assert mock_run.call_args_list[2][0][0] == ["git", "commit", "-m", "test commit message"]


class TestDetectDecomposition:
    def test_true_when_subtasks_exist(self, tmp_path, monkeypatch):
        import dispatcher
        import task_store

        tf = tmp_path / "tasks.json"
        data = {"tasks": [
            {"id": 1, "status": "decomposed"},
            {"id": 2, "status": "pending", "parent": 1},
            {"id": 3, "status": "pending", "parent": 1},
        ]}
        write_tasks(tf, data)
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        assert dispatcher.detect_decomposition(1) is True

    def test_false_when_no_subtasks(self, tmp_path, monkeypatch):
        import dispatcher
        import task_store

        tf = tmp_path / "tasks.json"
        data = {"tasks": [{"id": 1, "status": "pending"}]}
        write_tasks(tf, data)
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        assert dispatcher.detect_decomposition(1) is False

    def test_false_when_subtasks_not_pending(self, tmp_path, monkeypatch):
        import dispatcher
        import task_store

        tf = tmp_path / "tasks.json"
        data = {"tasks": [
            {"id": 1, "status": "decomposed"},
            {"id": 2, "status": "done", "parent": 1},
        ]}
        write_tasks(tf, data)
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        assert dispatcher.detect_decomposition(1) is False


class TestWriteStatus:
    def test_writes_status_file(self, tmp_path, monkeypatch):
        import dispatcher

        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        dispatcher.write_status("running", "Executing #1", task_id=1)

        status = json.loads((tmp_path / "status.json").read_text())
        assert status["state"] == "running"
        assert status["label"] == "Executing #1"
        assert status["task_id"] == 1

    def test_writes_without_task_id(self, tmp_path, monkeypatch):
        import dispatcher

        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        dispatcher.write_status("idle", "Idle")

        status = json.loads((tmp_path / "status.json").read_text())
        assert status["state"] == "idle"
        assert "task_id" not in status


# ============================================================================
# web_manager.py tests
# ============================================================================


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    """Flask test client with tasks.json in tmp_path."""
    import progress_logger
    import task_store
    import web_manager

    tf = tmp_path / "tasks.json"
    write_tasks(tf, {"tasks": []})
    monkeypatch.setattr(task_store, "TASKS_FILE", tf)
    # Redirect progress logger to tmp_path so it doesn't try /workspace
    monkeypatch.setattr(progress_logger, "WORKSPACE", tmp_path)
    monkeypatch.setattr(progress_logger, "PROGRESS_FILE", tmp_path / "agent_log" / "agent_log.md")
    monkeypatch.setattr(progress_logger, "ENTRIES_FILE", tmp_path / "agent_log" / "entries.jsonl")
    monkeypatch.setattr(progress_logger, "DETAILS_DIR", tmp_path / "agent_log")
    web_manager.app.config["TESTING"] = True
    with web_manager.app.test_client() as client:
        yield client, tf


class TestBoardRoute:
    def test_returns_200(self, web_client):
        client, _ = web_client
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"ClaudeXingCode Dashboard" in resp.data


class TestAddTaskRoute:
    def test_creates_task(self, web_client):
        client, tf = web_client
        resp = client.post("/tasks", data={"prompt": "Fix the bug", "priority": "high"})
        assert resp.status_code == 302  # redirect

        data = json.loads(tf.read_text())
        assert len(data["tasks"]) == 1
        task = data["tasks"][0]
        assert task["id"] == 1
        assert task["prompt"] == "Fix the bug"
        assert task["priority"] == "high"
        assert task["status"] == "pending"
        assert task["plan_model"] == "sonnet"  # default model
        assert task["exec_model"] == "sonnet"

    def test_creates_task_with_model(self, web_client):
        client, tf = web_client
        resp = client.post("/tasks", data={"prompt": "Think hard", "priority": "high", "model": "opus"})
        assert resp.status_code == 302

        data = json.loads(tf.read_text())
        task = data["tasks"][0]
        assert task["plan_model"] == "opus"
        assert task["exec_model"] == "opus"

    def test_invalid_model_defaults_to_sonnet(self, web_client):
        client, tf = web_client
        resp = client.post("/tasks", data={"prompt": "Test", "priority": "medium", "model": "gpt4"})
        assert resp.status_code == 302

        data = json.loads(tf.read_text())
        task = data["tasks"][0]
        assert task["plan_model"] == "sonnet"
        assert task["exec_model"] == "sonnet"

    def test_empty_prompt_no_task(self, web_client):
        client, tf = web_client
        resp = client.post("/tasks", data={"prompt": "  ", "priority": "medium"})
        assert resp.status_code == 302
        data = json.loads(tf.read_text())
        assert len(data["tasks"]) == 0


class TestTaskDetailRoute:
    def test_returns_task(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "pending", "prompt": "Test task",
                           "priority": "medium", "parent": None, "plan": None,
                           "summary": None}]}
        write_tasks(tf, data)

        resp = client.get("/tasks/1")
        assert resp.status_code == 200
        assert b"Test task" in resp.data

    def test_404_not_found(self, web_client):
        client, _ = web_client
        resp = client.get("/tasks/999")
        assert resp.status_code == 404


class TestApproveRoute:
    def test_approves_plan_review_task(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                           "priority": "medium"}]}
        write_tasks(tf, data)

        resp = client.post("/tasks/1/approve")
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["status"] == "in_progress"

    def test_does_not_approve_non_plan_review(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "pending", "prompt": "x",
                           "priority": "medium"}]}
        write_tasks(tf, data)

        client.post("/tasks/1/approve")

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["status"] == "pending"


class TestRejectRoute:
    def test_rejects_with_feedback(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                           "priority": "medium"}]}
        write_tasks(tf, data)

        resp = client.post("/tasks/1/reject", data={"feedback": "Bad plan"})
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["status"] == "stopped"
        assert result["tasks"][0]["stop_reason"] == "rejected"
        assert result["tasks"][0]["summary"] == "Rejected: Bad plan"

    def test_rejects_without_feedback(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                           "priority": "medium"}]}
        write_tasks(tf, data)

        client.post("/tasks/1/reject", data={"feedback": ""})

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["status"] == "stopped"
        assert result["tasks"][0]["stop_reason"] == "rejected"


class TestCancelRoute:
    def test_cancels_in_progress(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "in_progress", "prompt": "x",
                           "priority": "medium"}]}
        write_tasks(tf, data)

        resp = client.post("/tasks/1/cancel")
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["status"] == "stopped"
        assert result["tasks"][0]["stop_reason"] == "cancelled"


class TestRetryRoute:
    def test_requeues_stopped_task(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "stopped", "prompt": "x",
                           "priority": "medium", "stop_reason": "rejected",
                           "summary": "old", "plan": "old plan"}]}
        write_tasks(tf, data)

        resp = client.post("/tasks/1/retry")
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        task = result["tasks"][0]
        assert task["status"] == "pending"
        assert task["summary"] is None
        assert task["plan"] is None
        assert "stop_reason" not in task


class TestDeleteRoute:
    def test_deletes_non_running_task(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "pending", "prompt": "x"}]}
        write_tasks(tf, data)

        resp = client.post("/tasks/1/delete")
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        assert len(result["tasks"]) == 0

    def test_does_not_delete_in_progress(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "in_progress", "prompt": "x"}]}
        write_tasks(tf, data)

        client.post("/tasks/1/delete")

        result = json.loads(tf.read_text())
        assert len(result["tasks"]) == 1


class TestSetModelRoute:
    def test_changes_plan_model(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                           "priority": "medium", "plan_model": "sonnet",
                           "exec_model": "sonnet"}]}
        write_tasks(tf, data)

        resp = client.post("/tasks/1/set-model", data={
            "plan_model": "opus", "exec_model": "sonnet"
        })
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["plan_model"] == "opus"
        assert result["tasks"][0]["exec_model"] == "sonnet"

    def test_changes_exec_model(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                           "priority": "medium", "plan_model": "sonnet",
                           "exec_model": "sonnet"}]}
        write_tasks(tf, data)

        resp = client.post("/tasks/1/set-model", data={
            "plan_model": "sonnet", "exec_model": "opus"
        })
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["plan_model"] == "sonnet"
        assert result["tasks"][0]["exec_model"] == "opus"

    def test_changes_both_models(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "pending", "prompt": "x",
                           "priority": "medium", "plan_model": "sonnet",
                           "exec_model": "sonnet"}]}
        write_tasks(tf, data)

        resp = client.post("/tasks/1/set-model", data={
            "plan_model": "haiku", "exec_model": "opus"
        })
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["plan_model"] == "haiku"
        assert result["tasks"][0]["exec_model"] == "opus"

    def test_ignores_invalid_model(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "pending", "prompt": "x",
                           "priority": "medium", "plan_model": "sonnet",
                           "exec_model": "sonnet"}]}
        write_tasks(tf, data)

        client.post("/tasks/1/set-model", data={
            "plan_model": "gpt4", "exec_model": "gpt4"
        })

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["plan_model"] == "sonnet"
        assert result["tasks"][0]["exec_model"] == "sonnet"

    def test_works_on_any_status(self, web_client):
        """Model can be changed even on done/stopped tasks."""
        client, tf = web_client
        for status in ("pending", "plan_review", "done", "stopped"):
            data = {"tasks": [{"id": 1, "status": status, "prompt": "x",
                               "plan_model": "sonnet", "exec_model": "sonnet"}]}
            write_tasks(tf, data)

            client.post("/tasks/1/set-model", data={
                "plan_model": "opus", "exec_model": "haiku"
            })

            result = json.loads(tf.read_text())
            assert result["tasks"][0]["plan_model"] == "opus"
            assert result["tasks"][0]["exec_model"] == "haiku"


class TestEditTaskModels:
    def test_edit_updates_both_models(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "pending", "prompt": "x",
                           "priority": "medium", "plan_model": "sonnet",
                           "exec_model": "sonnet"}]}
        write_tasks(tf, data)

        client.post("/tasks/1/edit", data={
            "prompt": "updated", "priority": "high",
            "plan_model": "opus", "exec_model": "haiku"
        })

        result = json.loads(tf.read_text())
        task = result["tasks"][0]
        assert task["plan_model"] == "opus"
        assert task["exec_model"] == "haiku"
        assert task["priority"] == "high"

    def test_edit_blocked_for_in_progress(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "in_progress", "prompt": "x",
                           "priority": "medium", "plan_model": "sonnet",
                           "exec_model": "sonnet"}]}
        write_tasks(tf, data)

        client.post("/tasks/1/edit", data={
            "prompt": "updated", "priority": "high",
            "plan_model": "opus", "exec_model": "opus"
        })

        result = json.loads(tf.read_text())
        task = result["tasks"][0]
        assert task["plan_model"] == "sonnet"  # unchanged
        assert task["exec_model"] == "sonnet"


class TestStatusRoute:
    def test_returns_idle_when_no_file(self, web_client):
        client, _ = web_client
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["state"] == "idle"


# ============================================================================
# daily_digest.py tests
# ============================================================================


class TestLoadEnvFile:
    def test_loads_vars(self, tmp_path, monkeypatch):
        from daily_digest import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\nBAZ=qux\n")
        # Clear any existing values
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAZ", raising=False)

        load_env_file(str(env_file))

        assert os.environ["FOO"] == "bar"
        assert os.environ["BAZ"] == "qux"

    def test_skips_comments_and_blanks(self, tmp_path, monkeypatch):
        from daily_digest import load_env_file

        env_file = tmp_path / ".env"
        env_file.write_text("# comment\n\nKEY=val\n")
        monkeypatch.delenv("KEY", raising=False)

        load_env_file(str(env_file))

        assert os.environ["KEY"] == "val"

    def test_handles_missing_file(self):
        from daily_digest import load_env_file

        # Should not raise
        load_env_file("/nonexistent/path/.env")


class TestBuildBody:
    def test_formats_sections(self):
        from daily_digest import build_body

        today = "2026-02-27"
        tasks = [
            {"id": 1, "status": "done", "prompt": "Task A", "completed_at": f"{today}T10:00:00"},
            {"id": 2, "status": "pending", "prompt": "Task B"},
            {"id": 3, "status": "stopped", "prompt": "Task C", "created_at": f"{today}T08:00:00"},
        ]
        body = build_body(tasks, today)

        assert "Completed (1):" in body
        assert "#1" in body
        assert "Pending (1):" in body
        assert "#2" in body
        assert "Failed (1):" in body
        assert "#3" in body

    def test_empty_lists(self):
        from daily_digest import build_body

        body = build_body([], "2026-02-27")
        assert "(none)" in body
        assert "Completed (0):" in body
        assert "Pending (0):" in body
        assert "Failed (0):" in body


class TestSendDigest:
    def test_skips_without_credentials(self, tmp_path, monkeypatch):
        import daily_digest

        tf = tmp_path / "tasks.json"
        tf.write_text(json.dumps({"tasks": []}))
        monkeypatch.setattr(daily_digest, "TASKS_FILE", tf)
        monkeypatch.delenv("SMTP_USER", raising=False)
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        monkeypatch.setattr(daily_digest, "load_env_file", lambda *a: None)

        with patch("smtplib.SMTP") as mock_smtp:
            daily_digest.send_digest()
            mock_smtp.assert_not_called()

    def test_sends_email_with_credentials(self, tmp_path, monkeypatch):
        import daily_digest

        tf = tmp_path / "tasks.json"
        tf.write_text(json.dumps({"tasks": []}))
        monkeypatch.setattr(daily_digest, "TASKS_FILE", tf)
        monkeypatch.setattr(daily_digest, "load_env_file", lambda *a: None)
        monkeypatch.setenv("SMTP_USER", "test@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "secret")
        monkeypatch.setenv("SMTP_HOST", "smtp.test.com")
        monkeypatch.setenv("SMTP_PORT", "587")

        mock_server = MagicMock()
        with patch("smtplib.SMTP", return_value=mock_server) as mock_smtp:
            mock_server.__enter__ = MagicMock(return_value=mock_server)
            mock_server.__exit__ = MagicMock(return_value=False)

            daily_digest.send_digest()

            mock_smtp.assert_called_once_with("smtp.test.com", 587)
            mock_server.starttls.assert_called_once()
            mock_server.login.assert_called_once_with("test@example.com", "secret")
            mock_server.send_message.assert_called_once()
