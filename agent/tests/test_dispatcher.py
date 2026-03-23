"""Tests for dispatcher.py core helpers — task picking, CC runners, git, status."""

import json
import pytest
from unittest.mock import MagicMock
import dispatcher
import task_store
from dispatcher import (
    pick_next_task, pick_approved_task, pick_actionable_task,
    is_token_limit_error, parse_stream_json,
)
from helpers import write_tasks


class TestUpdateTask:
    """update_task applies kwargs to the matching task in tasks.json via locked_update."""

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

    def test_nonexistent_task_is_noop(self, tmp_path, monkeypatch):
        """update_task with an ID that doesn't exist should be a safe no-op —
        the file is unchanged and no exception is raised."""
        tf = tmp_path / "tasks.json"
        original = {"tasks": [{"id": 1, "status": "pending", "prompt": "a"}]}
        write_tasks(tf, original)
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        dispatcher.update_task(999, status="done")

        result = json.loads(tf.read_text())
        assert result["tasks"][0]["status"] == "pending"


class TestPickNextTask:
    """pick_next_task selects the highest-priority pending task that isn't blocked.
    Ties are broken by lowest id (oldest first). Tasks with non-empty blocked_on are skipped."""

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


class TestPickApprovedTask:
    """pick_approved_task returns in_progress tasks with a plan (approved, awaiting execution).
    Same priority/id sorting as pick_next_task."""

    def test_returns_none_when_empty(self):
        assert pick_approved_task([]) is None

    def test_returns_none_when_no_in_progress_with_plan(self):
        tasks = [
            {"id": 1, "status": "in_progress", "priority": "high"},  # no plan
            {"id": 2, "status": "pending", "priority": "high", "plan": "p"},
        ]
        assert pick_approved_task(tasks) is None

    def test_picks_in_progress_with_plan(self):
        tasks = [
            {"id": 1, "status": "in_progress", "priority": "medium", "plan": "the plan"},
            {"id": 2, "status": "pending", "priority": "high"},
        ]
        assert pick_approved_task(tasks)["id"] == 1

    def test_picks_highest_priority(self):
        tasks = [
            {"id": 1, "status": "in_progress", "priority": "low", "plan": "p"},
            {"id": 2, "status": "in_progress", "priority": "high", "plan": "p"},
        ]
        assert pick_approved_task(tasks)["id"] == 2

    def test_breaks_ties_by_id(self):
        tasks = [
            {"id": 5, "status": "in_progress", "priority": "high", "plan": "p"},
            {"id": 3, "status": "in_progress", "priority": "high", "plan": "p"},
        ]
        assert pick_approved_task(tasks)["id"] == 3

    def test_skips_null_plan(self):
        """A task with plan=None (explicitly set, not missing key) should be skipped.
        t.get("plan") returns None which is falsy — same behavior as missing key."""
        tasks = [
            {"id": 1, "status": "in_progress", "priority": "high", "plan": None},
            {"id": 2, "status": "pending", "priority": "low"},
        ]
        assert pick_approved_task(tasks) is None


class TestPickActionableTask:
    """pick_actionable_task gives approved tasks priority over pending ones —
    ensuring work the user already reviewed doesn't get stuck behind new tasks."""

    def test_prefers_approved_over_pending(self):
        tasks = [
            {"id": 1, "status": "pending", "priority": "high"},
            {"id": 2, "status": "in_progress", "priority": "low", "plan": "approved plan"},
        ]
        assert pick_actionable_task(tasks)["id"] == 2

    def test_falls_back_to_pending_when_no_approved(self):
        tasks = [{"id": 1, "status": "pending", "priority": "medium"}]
        assert pick_actionable_task(tasks)["id"] == 1

    def test_returns_none_when_nothing_actionable(self):
        tasks = [{"id": 1, "status": "done", "priority": "high"}]
        assert pick_actionable_task(tasks) is None


class TestIsTokenLimitError:
    """Matches against TOKEN_LIMIT_PATTERNS — case-insensitive substring search.
    All 8 patterns: token limit, rate_limit, rate limit, too many tokens,
    context window, max_tokens, exceeded your current quota, overloaded."""

    def test_detects_rate_limit(self):
        assert is_token_limit_error("Error: rate_limit_error — too many requests") is True

    def test_detects_token_limit(self):
        assert is_token_limit_error("Stopped: token limit reached") is True

    def test_detects_context_window(self):
        assert is_token_limit_error("context window exceeded") is True

    def test_detects_max_tokens(self):
        assert is_token_limit_error("Error: max_tokens parameter exceeded") is True

    def test_detects_quota_exceeded(self):
        assert is_token_limit_error("You have exceeded your current quota") is True

    def test_detects_overloaded(self):
        assert is_token_limit_error("The API is overloaded, please retry") is True

    def test_case_insensitive(self):
        assert is_token_limit_error("RATE_LIMIT error") is True

    def test_ignores_normal_output(self):
        assert is_token_limit_error("Task completed successfully") is False


class TestParseStreamJson:
    """parse_stream_json extracts text from CC's stream-json output.
    Priority: result object > concatenated assistant text > raw string."""

    def test_extracts_result(self):
        raw = '{"type":"result","result":"the answer"}\n'
        assert parse_stream_json(raw) == "the answer"

    def test_extracts_assistant_text(self):
        raw = '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'
        assert parse_stream_json(raw) == "hello"

    def test_result_beats_assistant_text(self):
        """When both assistant text and a result object exist, result wins."""
        raw = (
            '{"type":"assistant","message":{"content":[{"type":"text","text":"thinking..."}]}}\n'
            '{"type":"result","result":"final answer"}\n'
        )
        assert parse_stream_json(raw) == "final answer"

    def test_concatenates_multiple_assistant_blocks(self):
        """Multiple assistant messages are joined when there's no result."""
        raw = (
            '{"type":"assistant","message":{"content":[{"type":"text","text":"part 1"}]}}\n'
            '{"type":"assistant","message":{"content":[{"type":"text","text":"part 2"}]}}\n'
        )
        assert parse_stream_json(raw) == "part 1\n\npart 2"

    def test_falls_back_to_raw(self):
        assert parse_stream_json("not json at all") == "not json at all"

    def test_skips_blank_and_malformed_lines(self):
        raw = '\n\n{"type":"result","result":"ok"}\n{bad json\n\n'
        assert parse_stream_json(raw) == "ok"


class TestRunCC:
    """run_cc_local uses --permission-mode plan (read-only, no Docker).
    run_cc_docker uses --dangerously-skip-permissions inside a Docker container.
    Both map short model names to full model IDs via MODEL_MAP."""

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
    """git_commit runs `git add -A && git commit` inside Docker for consistent file
    ownership. Falls back to local git if Docker fails (e.g., image not built)."""

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
        """When Docker commit fails, dispatcher falls back to local git add + commit.
        The mock returns returncode=1 for ALL calls (including local git), which is
        fine — the local fallback is fire-and-forget (no return code check in source).
        We're testing that the fallback path IS taken, not that git succeeds."""
        mock_run = MagicMock(return_value=MagicMock(returncode=1, stderr="docker not found"))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.git_commit("test commit message")

        # 1 docker attempt + 2 local git calls (add -A, commit -m)
        assert mock_run.call_count == 3
        assert mock_run.call_args_list[1][0][0] == ["git", "add", "-A"]
        assert mock_run.call_args_list[2][0][0] == ["git", "commit", "-m", "test commit message"]


class TestPlanTaskModel:
    """plan_task passes task['plan_model'] to run_cc_local.
    Legacy tasks with only a 'model' field fall back gracefully."""

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
    """execute_task passes task['exec_model'] to run_cc_docker.
    Legacy tasks with only a 'model' field fall back gracefully."""

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
    """write_status writes a small JSON file that the web UI polls for dispatcher state."""

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

    def test_includes_task_id_zero(self, tmp_path, monkeypatch):
        """task_id=0 is a valid int, not None — it must appear in the output.
        Guards against `if task_id:` (wrong) vs `if task_id is not None:` (correct)."""
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        dispatcher.write_status("running", "Executing #0", task_id=0)

        status = json.loads((tmp_path / "status.json").read_text())
        assert status["task_id"] == 0
