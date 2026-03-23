"""Tests for agent/core/task_store.py — the shared data layer.

Covers: load_tasks, save_tasks, locked_update, next_id.
Concurrency/locking is hard to unit-test; these tests verify correctness
and safety guarantees (atomic writes, exception safety, monotonic IDs).
"""

import json
import pytest
import task_store
from helpers import write_tasks


class TestLoadTasks:
    """load_tasks reads tasks.json, returning a default structure if missing."""

    def test_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(task_store, "TASKS_FILE", tmp_path / "tasks.json")
        assert task_store.load_tasks() == {"tasks": []}

    def test_valid_json(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        data = {"tasks": [{"id": 1, "status": "pending", "prompt": "hello"}]}
        tf.write_text(json.dumps(data))
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        assert task_store.load_tasks() == data

    def test_corrupt_json_raises(self, tmp_path, monkeypatch):
        """Corrupt tasks.json raises JSONDecodeError — fail loud rather than
        silently losing data. The dispatcher and web manager will crash,
        which is the correct behavior for data corruption."""
        tf = tmp_path / "tasks.json"
        tf.write_text("{invalid json!!!")
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        with pytest.raises(json.JSONDecodeError):
            task_store.load_tasks()


class TestSaveTasks:
    """save_tasks writes atomically via tmp.replace (POSIX rename)."""

    def test_roundtrip(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        data = {"tasks": [{"id": 1, "status": "done"}]}
        task_store.save_tasks(data)
        assert json.loads(tf.read_text()) == data

    def test_overwrites_existing(self, tmp_path, monkeypatch):
        """Verify save replaces the old file content entirely, not appends."""
        tf = tmp_path / "tasks.json"
        tf.write_text(json.dumps({"tasks": [{"id": 1}]}))
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        task_store.save_tasks({"tasks": []})
        assert json.loads(tf.read_text()) == {"tasks": []}


class TestLockedUpdate:
    """locked_update holds an exclusive flock during read→mutate→write.
    Key guarantee: if mutate_fn raises, the file is unchanged."""

    def test_applies_mutation(self, tmp_path, monkeypatch):
        tf = tmp_path / "tasks.json"
        write_tasks(tf, {"tasks": [{"id": 1, "status": "pending"}]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        def mutate(data):
            data["tasks"][0]["status"] = "done"

        result = task_store.locked_update(mutate)
        assert result["tasks"][0]["status"] == "done"
        assert json.loads(tf.read_text())["tasks"][0]["status"] == "done"

    def test_returns_mutated_data(self, tmp_path, monkeypatch):
        """The return value is the post-mutation data dict — callers can use it
        without a second read."""
        tf = tmp_path / "tasks.json"
        write_tasks(tf, {"tasks": [{"id": 1, "status": "pending"}]})
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        result = task_store.locked_update(lambda d: d["tasks"].append({"id": 2, "status": "new"}))
        assert len(result["tasks"]) == 2

    def test_exception_safety(self, tmp_path, monkeypatch):
        """If mutate_fn raises, the file must be unchanged — the exception
        propagates but the lock is released and no partial write occurs."""
        tf = tmp_path / "tasks.json"
        original = {"tasks": [{"id": 1, "status": "pending"}]}
        write_tasks(tf, original)
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        def bad_mutate(data):
            data["tasks"][0]["status"] = "corrupted"
            raise ValueError("something broke")

        with pytest.raises(ValueError, match="something broke"):
            task_store.locked_update(bad_mutate)

        # File must be unchanged — save_tasks was never called
        assert json.loads(tf.read_text())["tasks"][0]["status"] == "pending"


class TestNextId:
    """next_id returns a monotonically increasing ID. Uses data['next_id'] counter
    so deleted tasks never get their IDs reused. Falls back to max(existing IDs)+1
    for backward compatibility with pre-counter task files."""

    def test_empty_tasks(self):
        data = {"tasks": []}
        assert task_store.next_id(data) == 1
        assert data["next_id"] == 2

    def test_derives_from_existing_ids(self):
        """Without next_id counter, derives from max existing ID."""
        data = {"tasks": [{"id": 3}, {"id": 7}, {"id": 5}]}
        assert task_store.next_id(data) == 8
        assert data["next_id"] == 9

    def test_monotonic_after_delete(self):
        """IDs are never reused even after all tasks are deleted."""
        data = {"tasks": [{"id": 1}], "next_id": 2}
        assert task_store.next_id(data) == 2
        # Simulate deleting all tasks
        data["tasks"] = []
        assert task_store.next_id(data) == 3  # never reuses ID 1 or 2

    def test_consecutive_calls_increment(self):
        """Multiple calls in sequence produce unique, ascending IDs."""
        data = {"tasks": [], "next_id": 1}
        ids = [task_store.next_id(data) for _ in range(5)]
        assert ids == [1, 2, 3, 4, 5]
        assert data["next_id"] == 6


class TestAtomicWrite:
    """Verify atomic write guarantees: no .tmp residue, file creation from scratch."""

    def test_save_tasks_no_tmp_residue(self, tmp_path, monkeypatch):
        """After save_tasks, the .tmp file must not exist — Path.replace is a
        rename, not copy+delete, so the source disappears atomically."""
        tf = tmp_path / "tasks.json"
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)
        task_store.save_tasks({"tasks": [{"id": 1}]})
        assert tf.exists()
        assert not tf.with_suffix(".tmp").exists()

    def test_locked_update_creates_file_from_scratch(self, tmp_path, monkeypatch):
        """locked_update on a missing tasks.json should create it — load_tasks
        returns the empty structure, mutate_fn populates it, save_tasks writes it."""
        tf = tmp_path / "tasks.json"
        assert not tf.exists()
        monkeypatch.setattr(task_store, "TASKS_FILE", tf)

        def add_task(data):
            data["tasks"].append({"id": 1, "status": "pending", "prompt": "hello"})

        result = task_store.locked_update(add_task)
        assert tf.exists()
        assert len(result["tasks"]) == 1
        assert json.loads(tf.read_text())["tasks"][0]["id"] == 1
