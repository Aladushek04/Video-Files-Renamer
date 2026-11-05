"""Тесты запуска скраперов студий из TUI (без Textual и сети)."""
import sys
import threading
import time

import pytest

from video_renamer.ui import studio_tasks


def test_run_captures_child_output(monkeypatch):
    monkeypatch.setattr(
        studio_tasks,
        "_command",
        lambda kind: [
            sys.executable,
            "-c",
            "print('page 1: 10 studios')",
        ],
    )
    lines = []
    code = studio_tasks.run("stashdb", lines.append)
    assert code == 0
    assert lines == ["page 1: 10 studios"]


def test_commands_use_module_entrypoints():
    assert "video_renamer.scrapers.stashdb" in studio_tasks._command("stashdb")
    assert "video_renamer.scrapers.fansdb" in studio_tasks._command("fansdb")
    assert "video_renamer.scrapers.theporndb" in studio_tasks._command(
        "theporndb"
    )
    merge = studio_tasks._command("merge")
    assert all(path in merge for path in studio_tasks.MERGE_INPUTS)
    assert "--apply" in merge


def test_cancel_terminates_child(monkeypatch):
    monkeypatch.setattr(
        studio_tasks,
        "_command",
        lambda kind: [
            sys.executable,
            "-c",
            "import time; print('started', flush=True); time.sleep(30)",
        ],
    )
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    started = time.monotonic()
    code = studio_tasks.run("stashdb", cancel_event=cancel)
    elapsed = time.monotonic() - started
    timer.cancel()
    assert code == studio_tasks.CANCELLED_CODE
    assert elapsed < 10


def test_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown"):
        studio_tasks.run("unknown")
