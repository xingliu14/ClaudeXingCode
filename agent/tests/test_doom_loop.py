"""Tests for doom loop detection (Phase 11) — retry count tracking and auto-stop."""

import importlib
import json
import pytest
from unittest.mock import MagicMock
import dispatcher
import task_store
from helpers import write_tasks


class TestDoomLoop:
    """Doom loop guard: execute_task increments retry_count BEFORE running CC.
    If retry_count > MAX_RETRIES, the task is stopped without calling Docker.
    First real execution has retry_count=1, so MAX_RETRIES=3 allows 3 attempts
    (retry_count 1, 2, 3 pass; retry_count 4 triggers stop)."""

    def _exec_mock(self):
        """Mock subprocess.run that returns a successful CC result."""
        return MagicMock(return_value=MagicMock(
            returncode=0, stdout='{"type":"result","result":"done"}', stderr=""
        ))

    def _make_task(self, retry_count=0):
        """Build a minimal in_progress task dict with the given retry_count."""
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
        """retry_count=3 + increment → 4 > MAX_RETRIES(3) → stopped."""
        tf = tmp_path / "tasks.json"
        task = self._make_task(retry_count=3)
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
        """retry_count=2 + increment → 3, which is NOT > MAX_RETRIES(3) → allowed."""
        tf = tmp_path / "tasks.json"
        task = self._make_task(retry_count=2)
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr(dispatcher, "MAX_RETRIES", 3)
        monkeypatch.setattr("subprocess.run", self._exec_mock())

        dispatcher.execute_task(task)

        t = json.loads(tf.read_text())["tasks"][0]
        assert t["status"] == "done"
        assert t.get("stop_reason") is None

    def test_missing_retry_count_defaults_to_zero(self, tmp_path, monkeypatch):
        """Tasks created before the doom-loop feature lack retry_count.
        The dispatcher uses t.get('retry_count', 0), so the first execution
        should set it to 1 and proceed normally."""
        tf = tmp_path / "tasks.json"
        task = {"id": 1, "status": "in_progress", "prompt": "legacy task",
                "exec_model": "sonnet", "plan": None, "priority": "medium",
                "parent": None, "dependents": [], "blocked_on": []}
        # Intentionally no "retry_count" key
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("subprocess.run", self._exec_mock())

        dispatcher.execute_task(task)

        t = json.loads(tf.read_text())["tasks"][0]
        assert t["retry_count"] == 1
        assert t["status"] == "done"

    def test_default_max_retries_is_3(self):
        assert dispatcher.MAX_RETRIES == 3

    def test_max_retries_env_override(self, monkeypatch):
        """Verify MAX_RETRIES can be overridden via env var.
        importlib.reload mutates the module in-place — monkeypatch.setattr
        can't properly undo that (it would restore to 5, not 3). The try/finally
        ensures we always re-reload with the env var removed, even if the assert
        fails — without this, a failing assert would leave the module poisoned
        at MAX_RETRIES=5 for all subsequent tests."""
        monkeypatch.setenv("MAX_RETRIES", "5")
        importlib.reload(dispatcher)
        try:
            assert dispatcher.MAX_RETRIES == 5
        finally:
            monkeypatch.delenv("MAX_RETRIES", raising=False)
            importlib.reload(dispatcher)
