"""Shared test utilities."""

import json
from pathlib import Path


def write_tasks(tf: Path, data: dict) -> None:
    """Write a tasks dict to a tmp file and ensure the lock file exists."""
    tf.write_text(json.dumps(data))
    tf.with_suffix(".lock").touch(exist_ok=True)
