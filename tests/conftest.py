"""Shared fixtures.

The modules under test hold their paths in module-level constants resolved at
import time, which is fine for an app that owns its own folder and inconvenient
for tests. Rather than restructure them, the fixtures here point those constants
at a tmp_path for the duration of a test and put them back afterwards — so a
test run never touches the real journal, trash store or memory file.
"""

from __future__ import annotations

import pytest

from jarvis.config import config


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect the journal and trash store into a temporary directory."""
    from jarvis.safety import journal

    trash = tmp_path / "trash"
    trash.mkdir()
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.jsonl")
    monkeypatch.setattr(journal, "TRASH_DIR", trash)
    return tmp_path


@pytest.fixture
def level(monkeypatch):
    """Set config.safety.level for one test, e.g. ``level("strict")``."""

    def _set(value: str) -> None:
        monkeypatch.setattr(config.safety, "level", value)

    return _set


@pytest.fixture(autouse=True)
def _no_study_leak():
    """Study mode is a process-global; make sure a failing test cannot strand it."""
    from jarvis.safety import gate

    yield
    gate.exit_study()
    gate.set_approver(None)
