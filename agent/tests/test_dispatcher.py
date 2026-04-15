"""Tests for dispatcher.py core helpers — task picking, CC runners, git, status."""

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock
import dispatcher
import task_store
from dispatcher import (
    pick_next_task, pick_approved_task, pick_actionable_task,
    is_token_limit_error, parse_stream_json,
    task_artifact_folder, write_result_md,
    parse_result_artifacts, auto_detect_artifacts,
    _materialize_document_artifacts,
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
            {"id": 2, "status": "executing", "priority": "high"},
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
    """pick_approved_task returns executing tasks (approved, awaiting Docker execution).
    Same priority/id sorting as pick_next_task."""

    def test_returns_none_when_empty(self):
        assert pick_approved_task([]) is None

    def test_returns_none_when_no_executing(self):
        tasks = [
            {"id": 1, "status": "planning", "priority": "high"},
            {"id": 2, "status": "pending", "priority": "high", "plan": "p"},
        ]
        assert pick_approved_task(tasks) is None

    def test_picks_executing(self):
        tasks = [
            {"id": 1, "status": "executing", "priority": "medium", "plan": "the plan"},
            {"id": 2, "status": "pending", "priority": "high"},
        ]
        assert pick_approved_task(tasks)["id"] == 1

    def test_picks_highest_priority(self):
        tasks = [
            {"id": 1, "status": "executing", "priority": "low", "plan": "p"},
            {"id": 2, "status": "executing", "priority": "high", "plan": "p"},
        ]
        assert pick_approved_task(tasks)["id"] == 2

    def test_breaks_ties_by_id(self):
        tasks = [
            {"id": 5, "status": "executing", "priority": "high", "plan": "p"},
            {"id": 3, "status": "executing", "priority": "high", "plan": "p"},
        ]
        assert pick_approved_task(tasks)["id"] == 3

    def test_skips_non_executing(self):
        """Only 'executing' status is picked — planning tasks are not yet approved."""
        tasks = [
            {"id": 1, "status": "planning", "priority": "high", "plan": None},
            {"id": 2, "status": "pending", "priority": "low"},
        ]
        assert pick_approved_task(tasks) is None


class TestPickActionableTask:
    """pick_actionable_task gives approved tasks priority over pending ones —
    ensuring work the user already reviewed doesn't get stuck behind new tasks."""

    def test_prefers_approved_over_pending(self):
        tasks = [
            {"id": 1, "status": "pending", "priority": "high"},
            {"id": 2, "status": "executing", "priority": "low", "plan": "approved plan"},
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

    def test_execute_runs_in_docker(self, monkeypatch, tmp_path):
        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout="exec output", stderr="warn"
        ))
        monkeypatch.setattr("subprocess.run", mock_run)
        monkeypatch.setattr(dispatcher, "task_artifact_folder", lambda tid: tmp_path / f"task_{tid}")

        code, output = dispatcher.run_cc_docker("do something", task_id=1)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "docker"
        assert "run" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--permission-mode" not in cmd
        assert "--model" in cmd
        assert code == 0

    def test_execute_uses_specified_model(self, monkeypatch, tmp_path):
        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout="exec output", stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)
        monkeypatch.setattr(dispatcher, "task_artifact_folder", lambda tid: tmp_path / f"task_{tid}")

        dispatcher.run_cc_docker("do something", task_id=1, model="haiku")

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
        task = {"id": 1, "status": "executing", "prompt": "test",
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
        task = {"id": 1, "status": "executing", "prompt": "test",
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


class TestTaskArtifactFolder:
    """task_artifact_folder builds the artifact path by walking the parent chain.

    Root task N       → <WORKSPACE>/agent_log/tasks/task_N/
    Subtask N (parent P) → <WORKSPACE>/agent_log/tasks/task_P/task_N/
    Nested (grandparent G → parent P → child N)
                      → <WORKSPACE>/agent_log/tasks/task_G/task_P/task_N/
    """

    def _setup(self, tmp_path, monkeypatch, tasks):
        """Write tasks.json and redirect both WORKSPACE and TASKS_FILE to tmp_path."""
        tf = tmp_path / "tasks.json"
        write_tasks(tf, {"tasks": tasks})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "WORKSPACE", str(tmp_path))

    def test_root_task_path(self, tmp_path, monkeypatch):
        """A task with no parent maps to agent_log/tasks/task_N/."""
        self._setup(tmp_path, monkeypatch, [
            {"id": 5, "status": "pending", "prompt": "root task"},
        ])
        expected = tmp_path / "agent_log" / "tasks" / "task_5"
        assert task_artifact_folder(5) == expected

    def test_one_level_subtask_path(self, tmp_path, monkeypatch):
        """A subtask with parent P maps to agent_log/tasks/task_P/task_N/."""
        self._setup(tmp_path, monkeypatch, [
            {"id": 10, "status": "pending", "prompt": "parent"},
            {"id": 20, "status": "pending", "prompt": "child", "parent": 10},
        ])
        expected = tmp_path / "agent_log" / "tasks" / "task_10" / "task_20"
        assert task_artifact_folder(20) == expected

    def test_two_level_nested_path(self, tmp_path, monkeypatch):
        """A grandchild (G→P→N) maps to agent_log/tasks/task_G/task_P/task_N/."""
        self._setup(tmp_path, monkeypatch, [
            {"id": 1, "status": "pending", "prompt": "grandparent"},
            {"id": 2, "status": "pending", "prompt": "parent", "parent": 1},
            {"id": 3, "status": "pending", "prompt": "child", "parent": 2},
        ])
        expected = tmp_path / "agent_log" / "tasks" / "task_1" / "task_2" / "task_3"
        assert task_artifact_folder(3) == expected


class TestWriteResultMd:
    """write_result_md creates the artifact folder and writes result.md."""

    def _setup(self, tmp_path, monkeypatch, tasks):
        tf = tmp_path / "tasks.json"
        write_tasks(tf, {"tasks": tasks})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "WORKSPACE", str(tmp_path))

    def test_creates_folder_and_writes_result_md(self, tmp_path, monkeypatch):
        """For a root task, creates the folder and writes result.md with the summary."""
        self._setup(tmp_path, monkeypatch, [
            {"id": 7, "status": "done", "prompt": "do something"},
        ])
        write_result_md(7, "All done!")

        result_path = tmp_path / "agent_log" / "tasks" / "task_7" / "result.md"
        assert result_path.exists(), "result.md should be created"
        content = result_path.read_text()
        assert "# Task #7 Result" in content
        assert "All done!" in content

    def test_write_result_md_nonfatal_on_broken_parent_chain(self, tmp_path, monkeypatch):
        """Orphaned task (parent ID references a missing task) must not raise.

        task_artifact_folder will include the orphan's own ID segment in the path
        because the chain terminates when current_id is looked up but not found
        (task_map.get returns None, so current_id becomes None immediately).
        write_result_md catches all exceptions so the caller never sees a failure."""
        self._setup(tmp_path, monkeypatch, [
            # parent=999 does not exist in the task list
            {"id": 42, "status": "pending", "prompt": "orphan", "parent": 999},
        ])
        # Must not raise even though parent 999 is missing
        write_result_md(42, "orphan summary")

        # The folder path for task 42 with missing parent 999:
        # chain starts at 42, walks to parent=999 which is not in task_map,
        # so current_id becomes None immediately for the next iteration.
        # chain = [42, 999] reversed = [999, 42] — but 999 is not in task_map,
        # so the loop exits after appending 42 then trying 999 (None lookup).
        # Actually: chain = [42], then tries t=task_map.get(42) which has parent=999,
        # so appends 999; then t=task_map.get(999)=None so current_id=None.
        # chain = [42, 999] reversed = [999, 42].
        # result_path = tasks_root / task_999 / task_42 / result.md
        result_path = tmp_path / "agent_log" / "tasks" / "task_999" / "task_42" / "result.md"
        assert result_path.exists(), "result.md should still be written despite broken chain"
        assert "orphan summary" in result_path.read_text()


class TestParseResultArtifacts:
    """parse_result_artifacts parses CC's execution output into a structured result.
    Falls back to raw-text treatment when JSON parsing fails or shape is wrong."""

    def test_plain_text_returns_summary_and_empty_artifacts(self):
        """Raw text input produces summary equal to that text and an empty artifacts list."""
        result = parse_result_artifacts("Task completed successfully.")
        assert result == {"summary": "Task completed successfully.", "artifacts": []}

    def test_valid_json_with_artifacts_parsed(self):
        """Valid JSON with summary and known artifact type is parsed as-is."""
        payload = json.dumps({
            "summary": "did X",
            "artifacts": [{"type": "git_commit", "hash": "abc123"}],
        })
        result = parse_result_artifacts(payload)
        assert result["summary"] == "did X"
        assert result["artifacts"] == [{"type": "git_commit", "hash": "abc123"}]

    def test_json_with_all_valid_artifact_types(self):
        """All five valid artifact types are preserved when present together."""
        artifacts = [
            {"type": "git_commit", "hash": "aaa"},
            {"type": "document", "title": "report"},
            {"type": "text", "content": "some text"},
            {"type": "code_diff", "diff": "--- a\n+++ b"},
            {"type": "url_list", "urls": ["https://example.com"]},
        ]
        payload = json.dumps({"summary": "all types", "artifacts": artifacts})
        result = parse_result_artifacts(payload)
        assert len(result["artifacts"]) == 5
        types = {a["type"] for a in result["artifacts"]}
        assert types == {"git_commit", "document", "text", "code_diff", "url_list"}

    def test_json_with_unknown_artifact_type_filtered_out(self):
        """An artifact with an unknown type is removed; valid ones are kept."""
        payload = json.dumps({
            "summary": "mixed",
            "artifacts": [
                {"type": "git_commit", "hash": "abc"},
                {"type": "blob", "data": "ignored"},
                {"type": "text", "content": "kept"},
            ],
        })
        result = parse_result_artifacts(payload)
        assert len(result["artifacts"]) == 2
        types = {a["type"] for a in result["artifacts"]}
        assert types == {"git_commit", "text"}

    def test_markdown_fenced_json_parsed(self):
        """JSON wrapped in ```json...``` fences is unwrapped and parsed correctly."""
        payload = '```json\n{"summary": "fenced", "artifacts": []}\n```'
        result = parse_result_artifacts(payload)
        assert result == {"summary": "fenced", "artifacts": []}

    def test_long_text_fallback_truncated_to_2000(self):
        """Raw text longer than 2000 chars falls back with summary = last 2000 chars."""
        long_text = "x" * 1500 + "y" * 1000  # 2500 chars total
        result = parse_result_artifacts(long_text)
        assert result["artifacts"] == []
        assert len(result["summary"]) == 2000
        assert result["summary"] == long_text[-2000:]

    def test_json_missing_summary_key_falls_back(self):
        """JSON dict without a 'summary' key triggers the raw-text fallback."""
        payload = json.dumps({"artifacts": [{"type": "git_commit", "hash": "abc"}]})
        result = parse_result_artifacts(payload)
        assert result["artifacts"] == []
        assert result["summary"] == payload

    def test_json_artifacts_null_treated_as_empty(self):
        """artifacts: null in JSON should not crash — treated as an empty list."""
        payload = json.dumps({"summary": "done", "artifacts": None})
        result = parse_result_artifacts(payload)
        assert result["summary"] == "done"
        assert result["artifacts"] == []


class TestAutoDetectArtifacts:
    """auto_detect_artifacts fills in artifacts when CC didn't output structured JSON.

    Only runs when result["artifacts"] is empty. Detects git commits made since
    session_start, then falls back to text/document classification by summary length.
    """

    _SESSION_START = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def _empty_result(self, summary="short summary"):
        return {"summary": summary, "artifacts": []}

    def test_skips_when_artifacts_already_present(self, monkeypatch):
        """If result already has artifacts, auto_detect must not modify them."""
        existing = [{"type": "git_commit", "ref": "abc", "message": "existing"}]
        result = {"summary": "done", "artifacts": list(existing)}

        called = []
        monkeypatch.setattr(dispatcher.subprocess, "run",
                            lambda *a, **kw: called.append(1) or MagicMock(returncode=0, stdout=""))

        auto_detect_artifacts(result, self._SESSION_START, "/fake/workspace")

        assert called == [], "subprocess.run should not be called when artifacts already present"
        assert result["artifacts"] == existing

    def test_adds_git_commit_artifact_from_log(self, monkeypatch):
        """Monkeypatched git log returning one commit produces one git_commit artifact."""
        result = self._empty_result()

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0,
            stdout="deadbeef|Add feature X\n",
        ))
        monkeypatch.setattr(dispatcher.subprocess, "run", mock_run)

        auto_detect_artifacts(result, self._SESSION_START, "/fake/workspace")

        assert len(result["artifacts"]) == 1
        artifact = result["artifacts"][0]
        assert artifact["type"] == "git_commit"
        assert artifact["ref"] == "deadbeef"
        assert artifact["message"] == "Add feature X"

    def test_adds_multiple_git_commits(self, monkeypatch):
        """Git log with two commits produces two git_commit artifacts in order."""
        result = self._empty_result()

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0,
            stdout="aaa111|First commit\nbbb222|Second commit\n",
        ))
        monkeypatch.setattr(dispatcher.subprocess, "run", mock_run)

        auto_detect_artifacts(result, self._SESSION_START, "/fake/workspace")

        assert len(result["artifacts"]) == 2
        assert result["artifacts"][0] == {"type": "git_commit", "ref": "aaa111", "message": "First commit"}
        assert result["artifacts"][1] == {"type": "git_commit", "ref": "bbb222", "message": "Second commit"}

    def test_git_error_is_silent(self, monkeypatch):
        """subprocess.run returning returncode=1 must not crash; git artifacts skipped."""
        result = self._empty_result("short")

        mock_run = MagicMock(return_value=MagicMock(
            returncode=1,
            stdout="",
            stderr="not a git repository",
        ))
        monkeypatch.setattr(dispatcher.subprocess, "run", mock_run)

        auto_detect_artifacts(result, self._SESSION_START, "/fake/workspace")

        # No git_commit artifacts — only the text fallback from the short summary
        git_artifacts = [a for a in result["artifacts"] if a["type"] == "git_commit"]
        assert git_artifacts == []

    def test_adds_text_artifact_for_short_summary(self, monkeypatch):
        """No git commits and summary < 500 chars → text artifact with summary as content."""
        short_summary = "Task done."
        result = self._empty_result(short_summary)

        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout=""))
        monkeypatch.setattr(dispatcher.subprocess, "run", mock_run)

        auto_detect_artifacts(result, self._SESSION_START, "/fake/workspace")

        assert len(result["artifacts"]) == 1
        assert result["artifacts"][0] == {"type": "text", "content": short_summary}

    def test_adds_document_artifact_for_long_summary(self, monkeypatch):
        """No git commits and summary >= 500 chars → document artifact with summary as content."""
        long_summary = "A" * 500
        result = self._empty_result(long_summary)

        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout=""))
        monkeypatch.setattr(dispatcher.subprocess, "run", mock_run)

        auto_detect_artifacts(result, self._SESSION_START, "/fake/workspace")

        assert len(result["artifacts"]) == 1
        assert result["artifacts"][0] == {"type": "document", "content": long_summary}

    def test_no_text_artifact_when_git_commits_found(self, monkeypatch):
        """When git commits are detected, no text/document artifact is added."""
        result = self._empty_result("short summary")

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0,
            stdout="cafebabe|Fix bug\n",
        ))
        monkeypatch.setattr(dispatcher.subprocess, "run", mock_run)

        auto_detect_artifacts(result, self._SESSION_START, "/fake/workspace")

        types = [a["type"] for a in result["artifacts"]]
        assert "text" not in types
        assert "document" not in types
        assert types == ["git_commit"]


class TestExecuteTaskRollupIntegration:
    """Integration test: execute_task triggers rollup when last child completes.

    Verifies the full wiring inside execute_task: when a child task completes
    and is the last unresolved child, on_task_complete returns the parent_id and
    generate_parent_report fires end-to-end, writing report.md and updating
    parent.report in tasks.json.
    """

    def test_execute_task_triggers_rollup_when_last_child_completes(
        self, tmp_path, monkeypatch
    ):
        # --- Setup tasks.json with parent (#1) and child (#2) ---
        tf = tmp_path / "tasks.json"
        parent_task = {
            "id": 1,
            "status": "decomposed",
            "prompt": "parent task",
            "children": [2],
            "unresolved_children": 1,
            "parent": None,
            "priority": "medium",
            "plan_model": "sonnet",
            "exec_model": "sonnet",
            "auto_approve": False,
            "retry_count": 0,
            "plan": None,
            "report": None,
            "result": None,
        }
        child_task = {
            "id": 2,
            "status": "executing",
            "prompt": "child task",
            "plan": '{"decision":"execute","plan":"do it"}',
            "parent": 1,
            "children": [],
            "unresolved_children": 0,
            "retry_count": 0,
            "auto_approve": False,
            "priority": "medium",
            "plan_model": "sonnet",
            "exec_model": "sonnet",
        }
        write_tasks(tf, {"tasks": [parent_task, child_task]})

        # --- Patch TASKS_FILE, WORKSPACE, STATUS_FILE ---
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "WORKSPACE", str(tmp_path))
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")

        # --- Mock subprocess.run to handle docker, git, and claude calls ---
        def mock_subprocess_run(args, **kwargs):
            m = MagicMock()
            m.returncode = 0
            cmd = args[0] if args else ""
            if cmd == "docker":
                # run_cc_docker returns stream-json with a structured result
                m.stdout = (
                    '{"type":"result","result":'
                    '"{\\"summary\\":\\"child done\\",\\"artifacts\\":[]}"}'
                )
                m.stderr = ""
            elif cmd == "claude":
                # run_cc_local for generate_parent_report rollup
                m.stdout = '{"type":"result","result":"Rollup report"}'
                m.stderr = ""
            else:
                m.stdout = ""
                m.stderr = ""
            return m

        monkeypatch.setattr(dispatcher.subprocess, "run", mock_subprocess_run)

        # --- Execute the child task ---
        dispatcher.execute_task(child_task)

        # --- Assertions ---
        data = json.loads(tf.read_text())
        task_map = {t["id"]: t for t in data["tasks"]}

        # 1. Child task #2 completed after execution
        assert task_map[2]["status"] == "done", (
            f"Expected child #2 status 'done', got '{task_map[2]['status']}'"
        )

        # 2. Parent task #1 has report set (rollup fired)
        assert task_map[1]["report"] is not None, (
            "Parent #1 report should be set after rollup, but is None"
        )

        # 3. report.md was written to the parent's artifact folder
        report_path = tmp_path / "agent_log" / "tasks" / "task_1" / "report.md"
        assert report_path.exists(), (
            f"report.md not found at expected path: {report_path}"
        )


class TestMaterializeDocumentArtifacts:
    """_materialize_document_artifacts writes inline document content to files."""

    def _setup(self, tmp_path, monkeypatch, tasks):
        tf = tmp_path / "tasks.json"
        write_tasks(tf, {"tasks": tasks})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "WORKSPACE", str(tmp_path))

    def test_materialize_document_artifacts_writes_file_and_sets_path(
        self, tmp_path, monkeypatch
    ):
        """Inline document artifact: content written to file, path set, content removed."""
        self._setup(tmp_path, monkeypatch, [
            {"id": 1, "status": "executing", "prompt": "doc task", "parent": None},
        ])
        result = {"summary": "done", "artifacts": [
            {"type": "document", "content": "long content here"}
        ]}
        _materialize_document_artifacts(result, task_id=1)

        artifact = result["artifacts"][0]
        assert "content" not in artifact, "content should be removed after materialization"
        assert "path" in artifact, "path should be set"
        assert artifact["path"].endswith("document_1.md")
        assert artifact.get("title") == "Document"

        # File must exist and contain the original content
        file_path = tmp_path / artifact["path"]
        assert file_path.exists(), f"Document file not found: {file_path}"
        assert file_path.read_text() == "long content here"

    def test_materialize_skips_already_path_based(self, tmp_path, monkeypatch):
        """Artifact that already has 'path' set must not be modified."""
        self._setup(tmp_path, monkeypatch, [
            {"id": 1, "status": "executing", "prompt": "doc task", "parent": None},
        ])
        original = {"type": "document", "path": "some/existing/path.md", "title": "Existing"}
        result = {"summary": "done", "artifacts": [dict(original)]}
        _materialize_document_artifacts(result, task_id=1)
        assert result["artifacts"][0] == original

    def test_materialize_skips_non_document_types(self, tmp_path, monkeypatch):
        """text and git_commit artifacts must not be touched."""
        self._setup(tmp_path, monkeypatch, [
            {"id": 1, "status": "executing", "prompt": "task", "parent": None},
        ])
        artifacts = [
            {"type": "text", "content": "short text"},
            {"type": "git_commit", "ref": "abc123", "message": "fix"},
        ]
        result = {"summary": "done", "artifacts": [dict(a) for a in artifacts]}
        _materialize_document_artifacts(result, task_id=1)
        assert result["artifacts"][0] == artifacts[0]
        assert result["artifacts"][1] == artifacts[1]
