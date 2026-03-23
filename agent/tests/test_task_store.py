"""Tests for agent/core/task_store.py — the shared data layer."""

import json
import pytest
import task_store
from helpers import write_tasks


class TestTaskStore:
    def test_load_tasks_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(task_store, "TASKS_FILE", tmp_path / "tasks.json")
        assert task_store.load_tasks() == {"tasks": []}

    def test_load_tasks_valid_json(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        data = {"tasks": [{"id": 1, "status": "pending", "prompt": "hello"}]}
        tf.write_text(json.dumps(data))
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        assert task_store.load_tasks() == data

    def test_save_tasks_roundtrip(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        data = {"tasks": [{"id": 1, "status": "done"}]}
        task_store.save_tasks(data)
        assert json.loads(tf.read_text()) == data

    def test_locked_update(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        write_tasks(tf, {"tasks": [{"id": 1, "status": "pending"}]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        def mutate(data):
            data["tasks"][0]["status"] = "done"

        result = task_store.locked_update(mutate)
        assert result["tasks"][0]["status"] == "done"
        assert json.loads(tf.read_text())["tasks"][0]["status"] == "done"

    def test_next_id_empty(self):
        data = {"tasks": []}
        assert task_store.next_id(data) == 1
        assert data["next_id"] == 2

    def test_next_id_existing(self):
        data = {"tasks": [{"id": 3}, {"id": 7}, {"id": 5}]}
        assert task_store.next_id(data) == 8
        assert data["next_id"] == 9

    def test_next_id_monotonic_after_delete(self):
        data = {"tasks": [{"id": 1}], "next_id": 2}
        assert task_store.next_id(data) == 2
        # Simulate deleting all tasks
        data["tasks"] = []
        assert task_store.next_id(data) == 3  # never reuses ID 1 or 2
