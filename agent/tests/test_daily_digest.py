"""Tests for agent/daily_digest.py — email digest generation and delivery."""

import json
import os
import pytest
from unittest.mock import MagicMock, patch
import daily_digest
from daily_digest import load_env_file, build_body


class TestLoadEnvFile:
    """load_env_file parses .env files into os.environ using setdefault.
    Key behaviors: strips quotes, skips comments/blanks, never overrides existing vars."""

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

    def test_strips_double_quotes(self, tmp_path, monkeypatch):
        """SMTP_PASSWORD="secret" should yield 'secret', not '"secret"'."""
        env_file = tmp_path / ".env"
        env_file.write_text('SMTP_PASSWORD="my secret"\nSMTP_HOST=\'quoted\'\n')
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        monkeypatch.delenv("SMTP_HOST", raising=False)

        load_env_file(str(env_file))

        assert os.environ["SMTP_PASSWORD"] == "my secret"
        assert os.environ["SMTP_HOST"] == "quoted"

    def test_setdefault_does_not_override(self, tmp_path, monkeypatch):
        """Existing env vars must win over .env file values."""
        env_file = tmp_path / ".env"
        env_file.write_text("EXISTING_VAR=from_file\n")
        monkeypatch.setenv("EXISTING_VAR", "from_env")

        load_env_file(str(env_file))

        assert os.environ["EXISTING_VAR"] == "from_env"

    def test_handles_missing_file(self):
        load_env_file("/nonexistent/path/.env")  # should not raise


class TestBuildBody:
    """build_body produces three sections: Completed (today only), Pending (all),
    Failed (all stopped). Stopped tasks show stop_reason when available."""

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

    def test_excludes_done_tasks_from_other_days(self):
        """Only tasks completed today appear in the Completed section."""
        today = "2026-03-23"
        tasks = [
            {"id": 1, "status": "done", "prompt": "Today", "completed_at": f"{today}T10:00:00"},
            {"id": 2, "status": "done", "prompt": "Yesterday", "completed_at": "2026-03-22T23:59:00"},
        ]
        body = build_body(tasks, today)

        assert "#1" in body
        assert "#2" not in body
        assert "Completed (1):" in body

    def test_shows_stop_reason_in_failed_section(self):
        """Failed tasks display their stop_reason in parentheses."""
        today = "2026-03-23"
        tasks = [
            {"id": 1, "status": "stopped", "prompt": "Broken task",
             "stop_reason": "loop_detected", "created_at": f"{today}T08:00:00"},
        ]
        body = build_body(tasks, today)

        assert "loop_detected" in body
        assert "#1" in body

    def test_empty_lists(self):
        body = build_body([], "2026-02-27")
        assert "(none)" in body
        assert "Completed (0):" in body
        assert "Pending (0):" in body
        assert "Failed (0):" in body

    def test_session_stats_appear(self):
        """Stats line shows today's session count, rate-limit hits, and avg duration."""
        today = "2026-03-29"
        tasks = [
            {"id": 1, "status": "done", "prompt": "Task A", "completed_at": f"{today}T10:00:00",
             "sessions": [
                 {"started_at": f"{today}T09:00:00", "duration_s": 120, "exit_code": 0, "rate_limited": False},
                 {"started_at": f"{today}T09:30:00", "duration_s": 60,  "exit_code": 0, "rate_limited": True},
             ]},
        ]
        body = build_body(tasks, today)

        assert "Sessions today: 2" in body
        assert "Rate limits: 1" in body
        assert "Avg duration: 1m 30s" in body

    def test_session_stats_excludes_other_days(self):
        """Sessions from other days are not counted in today's stats."""
        today = "2026-03-29"
        tasks = [
            {"id": 1, "status": "done", "prompt": "Task A", "completed_at": f"{today}T10:00:00",
             "sessions": [
                 {"started_at": "2026-03-28T22:00:00", "duration_s": 300, "exit_code": 0, "rate_limited": True},
             ]},
        ]
        body = build_body(tasks, today)

        assert "Sessions today: 0" in body
        assert "Rate limits: 0" in body
        assert "Avg duration: n/a" in body

    def test_session_stats_no_sessions(self):
        """Tasks with no sessions show zero stats without error."""
        today = "2026-03-29"
        tasks = [{"id": 1, "status": "pending", "prompt": "Task A"}]
        body = build_body(tasks, today)

        assert "Sessions today: 0" in body
        assert "Rate limits: 0" in body
        assert "Avg duration: n/a" in body


class TestSendDigest:
    """send_digest loads credentials, builds the email body, and delivers via SMTP.
    Silently skips if SMTP_USER or SMTP_PASSWORD are missing."""

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
