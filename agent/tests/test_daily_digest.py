"""Tests for agent/daily_digest.py — email digest generation and delivery."""

import json
import os
import pytest
from unittest.mock import MagicMock, patch
import daily_digest
from daily_digest import load_env_file, build_body


class TestLoadEnvFile:
    def test_loads_vars(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\nBAZ=qux\n")
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAZ", raising=False)

        load_env_file(str(env_file))

        assert os.environ["FOO"] == "bar"
        assert os.environ["BAZ"] == "qux"

    def test_skips_comments_and_blanks(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\n\nKEY=val\n")
        monkeypatch.delenv("KEY", raising=False)

        load_env_file(str(env_file))

        assert os.environ["KEY"] == "val"

    def test_handles_missing_file(self):
        load_env_file("/nonexistent/path/.env")  # should not raise


class TestBuildBody:
    def test_formats_sections(self):
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
        body = build_body([], "2026-02-27")
        assert "(none)" in body
        assert "Completed (0):" in body
        assert "Pending (0):" in body
        assert "Failed (0):" in body


class TestSendDigest:
    def test_skips_without_credentials(self, tmp_path, monkeypatch):
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
        tf = tmp_path / "tasks.json"
        tf.write_text(json.dumps({"tasks": []}))
        monkeypatch.setattr(daily_digest, "TASKS_FILE", tf)
        monkeypatch.setattr(daily_digest, "load_env_file", lambda *a: None)
        monkeypatch.setenv("SMTP_USER", "test@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "secret")
        monkeypatch.setenv("SMTP_HOST", "smtp.test.com")
        monkeypatch.setenv("SMTP_PORT", "587")

        mock_server = MagicMock()
        mock_server.__enter__.return_value = mock_server
        with patch("smtplib.SMTP", return_value=mock_server) as mock_smtp:
            daily_digest.send_digest()

            mock_smtp.assert_called_once_with("smtp.test.com", 587)
            mock_server.starttls.assert_called_once()
            mock_server.login.assert_called_once_with("test@example.com", "secret")
            mock_server.send_message.assert_called_once()
