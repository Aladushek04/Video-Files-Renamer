"""Карта намерения, фазы и общий замок (A02/A03)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from video_renamer.core.file_lock import LockBusy, RenamerLock
from video_renamer.core.rename_intent import (
    PendingOperationError,
    PlannedStep,
    intent_path,
    open_states,
    raise_if_blocked,
    read_intent_states,
    record_phase,
    write_intent,
)

ROOT = Path(__file__).resolve().parents[2]


def step(tmp_path: Path, old: str = "a.mp4", new: str = "b.mp4") -> PlannedStep:
    return PlannedStep(
        old_path=str(tmp_path / old),
        temp_path=str(tmp_path / "__ren_tmp_test"),
        new_path=str(tmp_path / new),
        old_rel=old,
        new_rel=new,
        root=str(tmp_path),
        oshash="0123456789abcdef",
        size=100,
        scene_id="s1",
        source="fingerprint",
    )


def log_row(tmp_path: Path, old: str = "a.mp4", new: str = "b.mp4") -> dict:
    return {
        "timestamp": "2026-01-01T00:00:00.000001Z",
        "action": "rename",
        "undoes": "",
        "root": str(tmp_path),
        "old": old,
        "new": new,
        "oshash": "0123456789abcdef",
        "size": 100,
        "scene_id": "s1",
        "source": "fingerprint",
    }


def test_intent_roundtrip_and_terminal_phase(tmp_path):
    log = str(tmp_path / "rename_log.csv")
    intent = write_intent(log, kind="rename", steps=[step(tmp_path)], log_rows=[log_row(tmp_path)])

    states, problems = read_intent_states(log)
    assert problems == []
    assert [state.intent.op_id for state in states] == [intent.op_id]
    assert states[0].phase == "planned"
    assert open_states(states) == states

    record_phase(log, intent.op_id, "done", ["ok"])
    states, problems = read_intent_states(log)
    assert problems == []
    assert states[0].phase == "done"
    assert states[0].messages == ("ok",)
    assert open_states(states) == []


def test_torn_last_line_after_power_loss_is_tolerated(tmp_path):
    log = str(tmp_path / "rename_log.csv")
    write_intent(log, kind="rename", steps=[step(tmp_path)], log_rows=[log_row(tmp_path)])
    with open(intent_path(log), "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"event": "phase", "op_id"')  # оборвано без \n

    states, problems = read_intent_states(log)
    assert problems == []
    assert len(states) == 1
    assert states[0].is_open


def test_corrupt_middle_line_blocks_new_operations(tmp_path):
    log = str(tmp_path / "rename_log.csv")
    write_intent(log, kind="rename", steps=[step(tmp_path)], log_rows=[log_row(tmp_path)])
    with open(intent_path(log), "a", encoding="utf-8", newline="\n") as handle:
        handle.write("not json\n")
    record_phase(log, "unknown", "done")

    _states, problems = read_intent_states(log)
    assert problems
    with pytest.raises(PendingOperationError, match="поврежд"):
        raise_if_blocked(log)


def test_open_operation_blocks_new_work(tmp_path):
    log = str(tmp_path / "rename_log.csv")
    intent = write_intent(log, kind="rename", steps=[step(tmp_path)], log_rows=[log_row(tmp_path)])

    with pytest.raises(PendingOperationError, match=intent.op_id):
        raise_if_blocked(log)

    record_phase(log, intent.op_id, "aborted")
    raise_if_blocked(log)  # терминальная фаза больше не блокирует


def test_lock_is_exclusive_and_released(tmp_path):
    log = str(tmp_path / "rename_log.csv")
    first = RenamerLock(log)
    first.acquire()
    try:
        with pytest.raises(LockBusy):
            RenamerLock(log).acquire()
    finally:
        first.release()
    again = RenamerLock(log)
    again.acquire()
    again.release()


def test_lock_is_shared_between_processes(tmp_path):
    log = str(tmp_path / "rename_log.csv")
    script = "\n".join(
        (
            "import sys, time",
            "from video_renamer.core.file_lock import RenamerLock",
            f"lock = RenamerLock({log!r})",
            "lock.acquire()",
            "print('locked', flush=True)",
            "time.sleep(30)",
        )
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(LockBusy):
            RenamerLock(log).acquire()
    finally:
        child.kill()
        child.wait(timeout=10)
        if child.stdout is not None:
            child.stdout.close()

    # Замок снят вместе с процессом: новый захват проходит.
    lock = RenamerLock(log)
    lock.acquire()
    lock.release()


def test_lock_released_when_process_is_killed(tmp_path):
    """Аварийное завершение не оставляет вечный замок (проверка через kill)."""
    log = str(tmp_path / "rename_log.csv")
    script = "\n".join(
        (
            "import time",
            "from video_renamer.core.file_lock import RenamerLock",
            f"lock = RenamerLock({log!r})",
            "lock.acquire()",
            "print('ready', flush=True)",
            "time.sleep(30)",
        )
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
    finally:
        child.kill()
        child.wait(timeout=10)
        if child.stdout is not None:
            child.stdout.close()

    lock = RenamerLock(log)
    lock.acquire()
    lock.release()
