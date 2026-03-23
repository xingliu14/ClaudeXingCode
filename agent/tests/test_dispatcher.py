"""Tests for dispatcher.py core helpers — task picking, CC runners, git, status."""

import json
import pytest
from unittest.mock import MagicMock
import dispatcher
import task_store
from dispatcher import pick_next_task, is_token_limit_error, parse_stream_json
from helpers import write_tasks


class TestUpdateTask:
    def test_updates_matching_task(self, tmp_path, monkeypatch):
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
        task1 = next(t for t in result["tasks"] if t["id"] == 1)
        assert task1["status"] == "pending"


class TestPickNextTask:
    def test_returns_none_on_empty(self):
        assert pick_next_task([]) is None

    def test_returns_none_when_no_pending(self):
        tasks = [{"id": 1, "status": "done", "priority": "high"}]
        assert pick_next_task(tasks) is None

    def test_picks_highest_priority(self):
        tasks = [
            {"id": 1, "status": "pending", "priority": "low"},
            {"id": 2, "status": "pending", "priority": "high"},
            {"id": 3, "status": "pending", "priority": "medium"},
        ]
        assert pick_next_task(tasks)["id"] == 2

    def test_breaks_ties_by_id(self):
        tasks = [
            {"id": 5, "status": "pending", "priority": "high"},
            {"id": 3, "status": "pending", "priority": "high"},
        ]
        assert pick_next_task(tasks)["id"] == 3

    def test_skips_non_pending(self):
        tasks = [
            {"id": 1, "status": "done", "priority": "high"},
            {"id": 2, "status": "in_progress", "priority": "high"},
            {"id": 3, "status": "pending", "priority": "low"},
        ]
        assert pick_next_task(tasks)["id"] == 3

    def test_default_priority_medium(self):
        tasks = [
            {"id": 1, "status": "pending"},  # no priority key
            {"id": 2, "status": "pending", "priority": "low"},
        ]
        assert pick_next_task(tasks)["id"] == 1

    def test_skips_blocked_task(self):
        tasks = [
            {"id": 1, "status": "pending", "priority": "high", "blocked_on": [2]},
            {"id": 2, "status": "pending", "priority": "low", "blocked_on": []},
        ]
        assert pick_next_task(tasks)["id"] == 2

    def test_skips_blocked_returns_none_when_all_blocked(self):
        tasks = [
            {"id": 1, "status": "pending", "priority": "high", "blocked_on": [2]},
            {"id": 2, "status": "pending", "priority": "high", "blocked_on": [1]},
        ]
        assert pick_next_task(tasks) is None

    def test_unblocked_task_no_blocked_on_key(self):
        tasks = [{"id": 1, "status": "pending", "priority": "medium"}]
        assert pick_next_task(tasks)["id"] == 1


class TestIsTokenLimitError:
    def test_detects_rate_limit(self):
        assert is_token_limit_error("Error: rate_limit_error — too many requests") is True

    def test_detects_token_limit(self):
        assert is_token_limit_error("Stopped: token limit reached") is True

    def test_detects_context_window(self):
        assert is_token_limit_error("context window exceeded") is True

    def test_ignores_normal_output(self):
        assert is_token_limit_error("Task completed successfully") is False


class TestParseStreamJson:
    def test_extracts_result(self):
        raw = '{"type":"result","result":"the answer"}\n'
        assert parse_stream_json(raw) == "the answer"

    def test_extracts_assistant_text(self):
        raw = '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'
        assert parse_stream_json(raw) == "hello"

    def test_falls_back_to_raw(self):
        assert parse_stream_json("not json at all") == "not json at all"


class TestRunCC:
    def test_plan_runs_locally(self, monkeypatch):
        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout="plan output", stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        code, output = dispatcher.run_cc_local("do something")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "--permission-mode" in cmd
        assert "plan" in cmd
        assert "--dangerously-skip-permissions" not in cmd
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-sonnet-4-6"
        assert code == 0

    def test_plan_uses_specified_model(self, monkeypatch):
        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout="plan output", stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.run_cc_local("do something", model="opus")

        cmd = mock_run.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-opus-4-6"

    def test_execute_runs_in_docker(self, monkeypatch):
        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout="exec output", stderr="warn"
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        code, output = dispatcher.run_cc_docker("do something")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "docker"
        assert "run" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--permission-mode" not in cmd
        assert "--model" in cmd
        assert code == 0

    def test_execute_uses_specified_model(self, monkeypatch):
        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout="exec output", stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.run_cc_docker("do something", model="haiku")

        cmd = mock_run.call_args[0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-haiku-4-5-20251001"


class TestGitCommit:
    def test_runs_in_docker(self, monkeypatch):
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.git_commit("test commit message")

        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "docker"
        assert "run" in cmd
        assert "bash" in cmd

    def test_falls_back_to_local_on_docker_failure(self, monkeypatch):
        mock_run = MagicMock(return_value=MagicMock(returncode=1, stderr="docker not found"))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.git_commit("test commit message")

        # 1 docker attempt + 2 local git calls
        assert mock_run.call_count == 3
        assert mock_run.call_args_list[1][0][0] == ["git", "add", "-A"]
        assert mock_run.call_args_list[2][0][0] == ["git", "commit", "-m", "test commit message"]



class TestPlanTaskModel:
    def test_uses_plan_model(self, tmp_path, monkeypatch):
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

        cmd = mock_run.call_args_list[0][0][0]
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "claude-opus-4-6"

    def test_falls_back_to_legacy_model(self, tmp_path, monkeypatch):
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


class TestWriteStatus:
    def test_writes_status_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        dispatcher.write_status("running", "Executing #1", task_id=1)

        status = json.loads((tmp_path / "status.json").read_text())
        assert status["state"] == "running"
        assert status["label"] == "Executing #1"
        assert status["task_id"] == 1

    def test_writes_without_task_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        dispatcher.write_status("idle", "Idle")

        status = json.loads((tmp_path / "status.json").read_text())
        assert status["state"] == "idle"
        assert "task_id" not in status
