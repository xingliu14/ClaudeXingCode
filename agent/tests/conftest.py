"""
Pytest configuration for the agent test suite.

Fixtures and helpers here are available to all test files automatically.
"""

from pathlib import Path
import sys

# Wire all agent subpackages onto sys.path so test files can import them
# with plain `import dispatcher` / `import task_store` etc.
_AGENT = Path(__file__).resolve().parent.parent
_TESTS = Path(__file__).resolve().parent
for _p in (_AGENT / "core", _AGENT / "dispatcher", _AGENT / "web", _AGENT, _TESTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest


# ---------------------------------------------------------------------------
# Autouse fixtures (apply to every test automatically)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_progress_logger(tmp_path, monkeypatch):
    """Redirect progress_logger file I/O to tmp_path for every test.

    Prevents tests from writing to the real agent_log/ directory.
    Without this, any test that exercises update_task(), plan_task(),
    execute_task(), or web routes will append entries to the live
    agent_log/entries.jsonl and rebuild agent_log/agent_log.md.
    """
    import progress_logger

    log_dir = tmp_path / "agent_log"
    log_dir.mkdir()
    monkeypatch.setattr(progress_logger, "ENTRIES_FILE", log_dir / "entries.jsonl")
    monkeypatch.setattr(progress_logger, "PROGRESS_FILE", log_dir / "agent_log.md")
    monkeypatch.setattr(progress_logger, "DETAILS_DIR", log_dir)
