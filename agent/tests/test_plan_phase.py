"""Tests for the plan phase — decision parsing, prompt building, plan injection, max depth."""

import importlib
import json
import pytest
from unittest.mock import MagicMock
import dispatcher
import task_store
from dispatcher import parse_plan_decision, build_plan_prompt, build_task_prompt
from helpers import write_tasks


class TestParsePlanDecision:
    def test_valid_execute_decision(self):
raw = '{"decision": "execute", "reasoning": "simple", "plan": "1. do it"}'
        result = parse_plan_decision(raw)
        assert result["decision"] == "execute"
        assert result["plan"] == "1. do it"

    def test_valid_decompose_decision(self):
raw = '{"decision": "decompose", "reasoning": "big", "subtasks": [{"prompt": "step 1", "depends_on": []}]}'
        result = parse_plan_decision(raw)
        assert result["decision"] == "decompose"
        assert len(result["subtasks"]) == 1

    def test_strips_json_fence(self):
raw = '```json\n{"decision": "execute", "plan": "do it"}\n```'
        result = parse_plan_decision(raw)
        assert result["decision"] == "execute"

    def test_strips_plain_fence(self):
raw = '```\n{"decision": "execute", "plan": "do it"}\n```'
        result = parse_plan_decision(raw)
        assert result["decision"] == "execute"

    def test_falls_back_on_invalid_json(self):
raw = "I think we should do X then Y"
        result = parse_plan_decision(raw)
        assert result["decision"] == "execute"
        assert result["plan"] == raw

    def test_falls_back_on_unknown_decision(self):
raw = '{"decision": "unknown", "plan": "do it"}'
        result = parse_plan_decision(raw)
        assert result["decision"] == "execute"
        assert result["plan"] == raw


class TestBuildPlanPrompt:
    def test_includes_task_prompt(self):
result = build_plan_prompt("Write a story")
        assert "Write a story" in result

    def test_includes_decision_criteria(self):
result = build_plan_prompt("Do X")
        assert "decompose" in result
        assert "execute" in result

    def test_includes_json_spec(self):
result = build_plan_prompt("Do X")
        assert '"decision"' in result
        assert "JSON" in result

    def test_includes_all_decompose_criteria(self):
result = build_plan_prompt("Do X")
        assert "independent concern" in result
        assert "~3 files" in result
        assert "step A" in result
        assert "max depth" in result

    def test_includes_execute_criteria(self):
result = build_plan_prompt("Do X")
        assert "one focused session" in result

    def test_includes_rejection_comments(self):
comments = [{"round": 1, "comment": "Please break this into smaller parts"}]
        result = build_plan_prompt("Do X", rejection_comments=comments)
        assert "Please break this into smaller parts" in result
        assert "PRIOR FEEDBACK" in result

    def test_no_rejection_comments_by_default(self):
result = build_plan_prompt("Do X")
        assert "PRIOR FEEDBACK" not in result

    def test_skips_empty_comments(self):
comments = [{"round": 1, "comment": ""}]
        result = build_plan_prompt("Do X", rejection_comments=comments)
        assert "PRIOR FEEDBACK" not in result


class TestBuildTaskPrompt:
    def test_without_plan(self):
result = build_task_prompt("Do the thing")
        assert "Do the thing" in result
        assert "APPROVED PLAN" not in result

    def test_with_plan_injects_section(self):
result = build_task_prompt("Do the thing", plan_text="1. step one\n2. step two")
        assert "APPROVED PLAN:" in result
        assert "1. step one" in result
        assert result.index("APPROVED PLAN") < result.index("TASK:")

    def test_none_plan_omits_section(self):
result = build_task_prompt("Do X", plan_text=None)
        assert "APPROVED PLAN" not in result


class TestExecuteTaskPlanInjection:
    def test_injects_execute_plan_into_prompt(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        plan = json.dumps({"decision": "execute", "reasoning": "small", "plan": "1. do it\n2. done"})
        task = {"id": 1, "status": "in_progress", "prompt": "Fix the bug",
                "plan_model": "sonnet", "exec_model": "sonnet",
                "plan": plan, "priority": "medium"}
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout='{"type":"result","result":"done"}', stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.execute_task(task)

        docker_call = mock_run.call_args_list[0][0][0]
        p_idx = docker_call.index("-p")
        prompt_sent = docker_call[p_idx + 1]
        assert "APPROVED PLAN:" in prompt_sent
        assert "1. do it" in prompt_sent

    def test_skips_plan_for_decompose_decision(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        plan = json.dumps({"decision": "decompose", "subtasks": []})
        task = {"id": 1, "status": "in_progress", "prompt": "Fix the bug",
                "plan_model": "sonnet", "exec_model": "sonnet",
                "plan": plan, "priority": "medium"}
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout='{"type":"result","result":"done"}', stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.execute_task(task)

        docker_call = mock_run.call_args_list[0][0][0]
        p_idx = docker_call.index("-p")
        prompt_sent = docker_call[p_idx + 1]
        assert "APPROVED PLAN" not in prompt_sent

    def test_skips_plan_for_legacy_plain_string(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        task = {"id": 1, "status": "in_progress", "prompt": "Fix the bug",
                "plan_model": "sonnet", "exec_model": "sonnet",
                "plan": "plain text plan (not JSON)", "priority": "medium"}
        write_tasks(tf, {"tasks": [task]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")

        mock_run = MagicMock(return_value=MagicMock(
            returncode=0, stdout='{"type":"result","result":"done"}', stderr=""
        ))
        monkeypatch.setattr("subprocess.run", mock_run)

        dispatcher.execute_task(task)  # should not raise

        docker_call = mock_run.call_args_list[0][0][0]
        p_idx = docker_call.index("-p")
        prompt_sent = docker_call[p_idx + 1]
        assert "APPROVED PLAN" not in prompt_sent


class TestMaxDepth:
    def _decompose_mock(self):
        decompose_json = json.dumps({"decision": "decompose", "subtasks": [
            {"prompt": "s", "depends_on": []}
        ]})
        return MagicMock(return_value=MagicMock(
            returncode=0,
            stdout=json.dumps({"type": "result", "result": decompose_json}),
            stderr="",
        ))

    def test_stops_decompose_at_max_depth(self, tmp_path, monkeypatch):
        import dispatcher
        import task_store

        monkeypatch.setattr(dispatcher, "MAX_SUB_TASK_DEPTH", 3)
        tf = tmp_path / "tasks.json"
        task = {"id": 1, "status": "pending", "prompt": "test",
                "plan_model": "sonnet", "exec_model": "sonnet",
                "priority": "medium", "depth": 3}
        write_tasks(tf, {"tasks": [task], "next_id": 2})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("subprocess.run", self._decompose_mock())

        dispatcher.plan_task(task)

        t = json.loads(tf.read_text())["tasks"][0]
        assert t["status"] == "stopped"
        assert t["stop_reason"] == "max_depth_reached"

    def test_executes_normally_below_max_depth(self, tmp_path, monkeypatch):
        import dispatcher
        import task_store

        monkeypatch.setattr(dispatcher, "MAX_SUB_TASK_DEPTH", 3)
        tf = tmp_path / "tasks.json"
        task = {"id": 1, "status": "pending", "prompt": "test",
                "plan_model": "sonnet", "exec_model": "sonnet",
                "priority": "medium", "depth": 2}
        write_tasks(tf, {"tasks": [task], "next_id": 2})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("subprocess.run", self._decompose_mock())

        dispatcher.plan_task(task)

        t = json.loads(tf.read_text())["tasks"][0]
        assert t["status"] == "plan_review"
        assert t.get("stop_reason") is None

    def test_default_max_depth_is_9(self):
        assert dispatcher.MAX_SUB_TASK_DEPTH == 9

    def test_max_depth_env_override(self, monkeypatch):
        monkeypatch.setenv("MAX_SUB_TASK_DEPTH", "5")
        importlib.reload(dispatcher)
        assert dispatcher.MAX_SUB_TASK_DEPTH == 5
        monkeypatch.setattr(dispatcher, "MAX_SUB_TASK_DEPTH", 9)  # restore — reload persists across tests


class TestAutoApproveDecompose:
    """Bug fix: auto_approve=True with a decompose decision must create subtasks."""

    def _decompose_mock(self):
        decompose_json = json.dumps({"decision": "decompose", "subtasks": [
            {"prompt": "do A", "depends_on": []},
            {"prompt": "do B", "depends_on": [0]},
        ]})
        return MagicMock(return_value=MagicMock(
            returncode=0,
            stdout=json.dumps({"type": "result", "result": decompose_json}),
            stderr="",
        ))

    def _execute_mock(self):
        execute_json = json.dumps({"decision": "execute", "plan": "1. do it"})
        return MagicMock(return_value=MagicMock(
            returncode=0,
            stdout=json.dumps({"type": "result", "result": execute_json}),
            stderr="",
        ))

    def test_auto_approve_execute_sets_in_progress(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        task = {"id": 1, "status": "pending", "prompt": "test",
                "plan_model": "sonnet", "exec_model": "sonnet",
                "priority": "medium", "depth": 0, "auto_approve": True}
        write_tasks(tf, {"tasks": [task], "next_id": 2})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("subprocess.run", self._execute_mock())

        dispatcher.plan_task(task)

        t = json.loads(tf.read_text())["tasks"][0]
        assert t["status"] == "in_progress"

    def test_auto_approve_decompose_sets_decomposed(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        task = {"id": 1, "status": "pending", "prompt": "test",
                "plan_model": "sonnet", "exec_model": "sonnet",
                "priority": "medium", "depth": 0, "auto_approve": True}
        write_tasks(tf, {"tasks": [task], "next_id": 2})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("subprocess.run", self._decompose_mock())

        dispatcher.plan_task(task)

        tasks = {t["id"]: t for t in json.loads(tf.read_text())["tasks"]}
        parent = tasks[1]
        assert parent["status"] == "decomposed"
        assert parent["unresolved_children"] == 2
        assert len(parent["children"]) == 2

    def test_auto_approve_decompose_creates_subtasks(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        task = {"id": 1, "status": "pending", "prompt": "test",
                "plan_model": "sonnet", "exec_model": "opus",
                "priority": "high", "depth": 0, "auto_approve": True}
        write_tasks(tf, {"tasks": [task], "next_id": 2})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("subprocess.run", self._decompose_mock())

        dispatcher.plan_task(task)

        all_tasks = json.loads(tf.read_text())["tasks"]
        subtasks = [t for t in all_tasks if t.get("parent") == 1]
        assert len(subtasks) == 2
        prompts = {t["prompt"] for t in subtasks}
        assert prompts == {"do A", "do B"}
        # Subtasks inherit priority and models
        for s in subtasks:
            assert s["priority"] == "high"
            assert s["plan_model"] == "sonnet"
            assert s["exec_model"] == "opus"
            assert s["auto_approve"] is True

    def test_auto_approve_decompose_wires_dependency(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        task = {"id": 1, "status": "pending", "prompt": "test",
                "plan_model": "sonnet", "exec_model": "sonnet",
                "priority": "medium", "depth": 0, "auto_approve": True}
        write_tasks(tf, {"tasks": [task], "next_id": 2})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("subprocess.run", self._decompose_mock())

        dispatcher.plan_task(task)

        all_tasks = {t["id"]: t for t in json.loads(tf.read_text())["tasks"]}
        subtasks = [t for t in all_tasks.values() if t.get("parent") == 1]
        a = next(t for t in subtasks if t["prompt"] == "do A")
        b = next(t for t in subtasks if t["prompt"] == "do B")
        assert a["blocked_on"] == []
        assert b["blocked_on"] == [a["id"]]
        assert a["id"] in b["depends_on"]

    def test_no_auto_approve_decompose_goes_to_plan_review(self, tmp_path, monkeypatch):
        """Without auto_approve, a decompose decision still waits in plan_review."""
        tf = tmp_path / "tasks.json"
        task = {"id": 1, "status": "pending", "prompt": "test",
                "plan_model": "sonnet", "exec_model": "sonnet",
                "priority": "medium", "depth": 0, "auto_approve": False}
        write_tasks(tf, {"tasks": [task], "next_id": 2})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        monkeypatch.setattr(dispatcher, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr("subprocess.run", self._decompose_mock())

        dispatcher.plan_task(task)

        all_tasks = json.loads(tf.read_text())["tasks"]
        assert all_tasks[0]["status"] == "plan_review"
        # No subtasks created yet
        assert len(all_tasks) == 1
