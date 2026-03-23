"""Tests for doom loop detection (Phase 11) — retry count tracking and auto-stop."""

import importlib
import json
import pytest
from unittest.mock import MagicMock
import dispatcher
import task_store
from helpers import write_tasks


class TestDoomLoop:
    def _exec_mock(self):
        return MagicMock(return_value=MagicMock(
            returncode=0, stdout='{"type":"result","result":"done"}', stderr=""
        ))

    def _make_task(self, retry_count=0):
        return {"id": 1, "status": "in_progress", "prompt": "test",
                "exec_model": "sonnet", "plan": None, "priority": "medium",
                "retry_count": retry_count, "parent": None,
                "dependents": [], "blocked_on": []}

    def test_retry_count_increments_on_each_call(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        task = self._make_task(retry_count=0)
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr(dispatcher, "MAX_RETRIES", 10)
        monkeypatch.setattr("subprocess.run", self._exec_mock())

        dispatcher.execute_task(task)

        t = json.loads(tf.read_text())["tasks"][0]
        assert t["retry_count"] == 1

    def test_retry_count_accumulates_across_calls(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        task = self._make_task(retry_count=2)
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr(dispatcher, "MAX_RETRIES", 10)
        monkeypatch.setattr("subprocess.run", self._exec_mock())

        dispatcher.execute_task(task)

        t = json.loads(tf.read_text())["tasks"][0]
        assert t["retry_count"] == 3

    def test_stops_with_loop_detected_when_over_threshold(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        task = self._make_task(retry_count=3)  # already at MAX_RETRIES; next call exceeds it
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr(dispatcher, "MAX_RETRIES", 3)
        monkeypatch.setattr("subprocess.run", self._exec_mock())

        dispatcher.execute_task(task)

        t = json.loads(tf.read_text())["tasks"][0]
        assert t["status"] == "stopped"
        assert t["stop_reason"] == "loop_detected"

    def test_no_docker_call_when_loop_detected(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        task = self._make_task(retry_count=3)
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr(dispatcher, "MAX_RETRIES", 3)
        mock_run = self._exec_mock()
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.execute_task(task)

        mock_run.assert_not_called()

    def test_runs_normally_at_threshold(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        task = self._make_task(retry_count=2)  # 2 prior runs, MAX_RETRIES=3 → allowed
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr(dispatcher, "MAX_RETRIES", 3)
        monkeypatch.setattr("subprocess.run", self._exec_mock())

        dispatcher.execute_task(task)

        t = json.loads(tf.read_text())["tasks"][0]
        assert t["status"] == "done"
        assert t.get("stop_reason") is None

    def test_default_max_retries_is_3(self):
        assert dispatcher.MAX_RETRIES == 3

    def test_max_retries_env_override(self, monkeypatch):
        monkeypatch.setenv("MAX_RETRIES", "5")
        importlib.reload(dispatcher)
        assert dispatcher.MAX_RETRIES == 5
        monkeypatch.setattr(dispatcher, "MAX_RETRIES", 3)  # restore — reload persists across tests
