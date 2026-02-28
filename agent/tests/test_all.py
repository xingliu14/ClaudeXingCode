"""
Tests for dispatcher.py, web_manager.py, and daily_digest.py.

All file I/O uses tmp_path fixtures; all external calls are mocked.
"""

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Ensure the agent package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ============================================================================
# dispatcher.py tests
# ============================================================================


class TestLoadSaveTasks:
    def test_load_tasks_missing_file(self, tmp_path, monkeypatch):
        import dispatcher

        monkeypatch.setattr(dispatcher, "TASKS_FILE", tmp_path / "tasks.json")
        assert dispatcher.load_tasks() == {"tasks": []}

    def test_load_tasks_valid_json(self, tmp_path, monkeypatch):
        import dispatcher

        tf = tmp_path / "tasks.json"
        data = {"tasks": [{"id": 1, "status": "pending", "prompt": "hello"}]}
        tf.write_text(json.dumps(data))
        monkeypatch.setattr(dispatcher, "TASKS_FILE", tf)
        assert dispatcher.load_tasks() == data

    def test_save_tasks_roundtrip(self, tmp_path, monkeypatch):
        import dispatcher

        tf = tmp_path / "tasks.json"
        monkeypatch.setattr(dispatcher, "TASKS_FILE", tf)
        data = {"tasks": [{"id": 1, "status": "done"}]}
        dispatcher.save_tasks(data)
        assert json.loads(tf.read_text()) == data


class TestUpdateTask:
    def test_updates_matching_task(self, tmp_path, monkeypatch):
        import dispatcher

        tf = tmp_path / "tasks.json"
        data = {"tasks": [
            {"id": 1, "status": "pending", "prompt": "a"},
            {"id": 2, "status": "pending", "prompt": "b"},
        ]}
        tf.write_text(json.dumps(data))
        monkeypatch.setattr(dispatcher, "TASKS_FILE", tf)

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


class TestTasksCompletedToday:
    def test_counts_today(self):
        from dispatcher import tasks_completed_today

        today = date.today().isoformat()
        tasks = [
            {"id": 1, "status": "done", "completed_at": f"{today}T10:00:00"},
            {"id": 2, "status": "done", "completed_at": f"{today}T14:00:00"},
            {"id": 3, "status": "pending"},
        ]
        assert tasks_completed_today(tasks) == 2

    def test_ignores_other_dates(self):
        from dispatcher import tasks_completed_today

        tasks = [
            {"id": 1, "status": "done", "completed_at": "2020-01-01T10:00:00"},
        ]
        assert tasks_completed_today(tasks) == 0

    def test_ignores_non_done(self):
        from dispatcher import tasks_completed_today

        today = date.today().isoformat()
        tasks = [
            {"id": 1, "status": "failed", "completed_at": f"{today}T10:00:00"},
        ]
        assert tasks_completed_today(tasks) == 0


class TestRunCC:
    def test_plan_mode_command(self, monkeypatch):
        import dispatcher

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout="plan output", stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        code, output = dispatcher.run_cc("do something", "plan")

        cmd = mock_run.call_args[0][0]
        assert "--plan" in cmd
        assert "--dangerously-skip-permissions" not in cmd
        assert "-p" in cmd
        assert "do something" in cmd
        assert code == 0
        assert output == "plan output"

    def test_execute_mode_command(self, monkeypatch):
        import dispatcher

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout="exec output", stderr="warn"
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        code, output = dispatcher.run_cc("do something", "execute")

        cmd = mock_run.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd
        assert "--plan" not in cmd
        assert code == 0
        assert output == "exec outputwarn"


class TestWaitForApproval:
    def test_returns_true_on_approved(self, tmp_path, monkeypatch):
        import dispatcher

        tf = tmp_path / "tasks.json"
        # Start with plan_review, then switch to approved on second load
        states = [
            {"tasks": [{"id": 1, "status": "plan_review"}]},
            {"tasks": [{"id": 1, "status": "approved"}]},
        ]
        call_count = {"n": 0}

        def fake_load():
            data = states[min(call_count["n"], len(states) - 1)]
            call_count["n"] += 1
            return data

        monkeypatch.setattr(dispatcher, "load_tasks", fake_load)
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(dispatcher, "PLAN_TIMEOUT_HOURS", 1)

        assert dispatcher.wait_for_approval({"id": 1}) is True

    def test_returns_false_on_rejected(self, tmp_path, monkeypatch):
        import dispatcher

        def fake_load():
            return {"tasks": [{"id": 1, "status": "rejected"}]}

        monkeypatch.setattr(dispatcher, "load_tasks", fake_load)
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(dispatcher, "PLAN_TIMEOUT_HOURS", 1)

        assert dispatcher.wait_for_approval({"id": 1}) is False

    def test_returns_false_on_missing_task(self, tmp_path, monkeypatch):
        import dispatcher

        def fake_load():
            return {"tasks": []}

        monkeypatch.setattr(dispatcher, "load_tasks", fake_load)
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(dispatcher, "PLAN_TIMEOUT_HOURS", 1)

        assert dispatcher.wait_for_approval({"id": 99}) is False


class TestGitCommit:
    def test_calls_git_add_and_commit(self, monkeypatch):
        import dispatcher

        mock_run = MagicMock()
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.git_commit("test commit message")

        assert mock_run.call_count == 2
        add_call = mock_run.call_args_list[0]
        assert add_call[0][0] == ["git", "add", "-A"]
        commit_call = mock_run.call_args_list[1]
        assert commit_call[0][0] == ["git", "commit", "-m", "test commit message"]


class TestAppendProgress:
    def test_appends_entry(self, tmp_path, monkeypatch):
        import dispatcher

        pf = tmp_path / "PROGRESS.md"
        pf.write_text("# Progress\n")
        monkeypatch.setattr(dispatcher, "PROGRESS_FILE", pf)

        dispatcher.append_progress(42, "Did the thing")

        content = pf.read_text()
        assert "## Task #42" in content
        assert "Did the thing" in content
        assert content.startswith("# Progress\n")


class TestDetectDecomposition:
    def test_true_when_subtasks_exist(self, tmp_path, monkeypatch):
        import dispatcher

        tf = tmp_path / "tasks.json"
        data = {"tasks": [
            {"id": 1, "status": "decomposed"},
            {"id": 2, "status": "pending", "parent": 1},
            {"id": 3, "status": "pending", "parent": 1},
        ]}
        tf.write_text(json.dumps(data))
        monkeypatch.setattr(dispatcher, "TASKS_FILE", tf)

        assert dispatcher.detect_decomposition(1) is True

    def test_false_when_no_subtasks(self, tmp_path, monkeypatch):
        import dispatcher

        tf = tmp_path / "tasks.json"
        data = {"tasks": [{"id": 1, "status": "pending"}]}
        tf.write_text(json.dumps(data))
        monkeypatch.setattr(dispatcher, "TASKS_FILE", tf)

        assert dispatcher.detect_decomposition(1) is False

    def test_false_when_subtasks_not_pending(self, tmp_path, monkeypatch):
        import dispatcher

        tf = tmp_path / "tasks.json"
        data = {"tasks": [
            {"id": 1, "status": "decomposed"},
            {"id": 2, "status": "done", "parent": 1},
        ]}
        tf.write_text(json.dumps(data))
        monkeypatch.setattr(dispatcher, "TASKS_FILE", tf)

        assert dispatcher.detect_decomposition(1) is False


# ============================================================================
# web_manager.py tests
# ============================================================================


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    """Flask test client with tasks.json in tmp_path."""
    import web_manager

    tf = tmp_path / "tasks.json"
    tf.write_text(json.dumps({"tasks": []}))
    monkeypatch.setattr(web_manager, "TASKS_FILE", tf)
    web_manager.app.config["TESTING"] = True
    with web_manager.app.test_client() as client:
        yield client, tf


class TestNextId:
    def test_empty_list(self):
        from web_manager import next_id

        assert next_id([]) == 1

    def test_existing_tasks(self):
        from web_manager import next_id

        tasks = [{"id": 3}, {"id": 7}, {"id": 5}]
        assert next_id(tasks) == 8


class TestBoardRoute:
    def test_returns_200(self, web_client):
        client, _ = web_client
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Agent Board" in resp.data


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
        tf.write_text(json.dumps(data))

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
        tf.write_text(json.dumps(data))

        resp = client.post("/tasks/1/approve")
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["status"] == "approved"

    def test_does_not_approve_non_plan_review(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "pending", "prompt": "x",
                           "priority": "medium"}]}
        tf.write_text(json.dumps(data))

        client.post("/tasks/1/approve")

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["status"] == "pending"


class TestRejectRoute:
    def test_rejects_with_feedback(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                           "priority": "medium"}]}
        tf.write_text(json.dumps(data))

        resp = client.post("/tasks/1/reject", data={"feedback": "Bad plan"})
        assert resp.status_code == 302

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["status"] == "rejected"
        assert result["tasks"][0]["summary"] == "Rejected: Bad plan"

    def test_rejects_without_feedback(self, web_client):
        client, tf = web_client
        data = {"tasks": [{"id": 1, "status": "plan_review", "prompt": "x",
                           "priority": "medium"}]}
        tf.write_text(json.dumps(data))

        client.post("/tasks/1/reject", data={"feedback": ""})

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["status"] == "rejected"
        assert "summary" not in result["tasks"][0] or result["tasks"][0].get("summary") is None or result["tasks"][0].get("summary") == ""


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
            {"id": 3, "status": "failed", "prompt": "Task C", "created_at": f"{today}T08:00:00"},
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
