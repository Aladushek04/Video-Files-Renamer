"""Запуск скраперов и слияния справочников из TUI.

Операции запускаются отдельными процессами: так их можно отменить из TUI,
не убивая сам интерфейс. Вывод читается построчно и отдаётся в журнал.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

__all__ = ["run"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MERGE_INPUTS = (
    "data/studios.json",
    "data/studios_fansdb.json",
    "data/studios_theporndb.json",
)
CANCELLED_CODE = 130


def _command(kind: str) -> list[str]:
    """Команда процесса для вида операции."""
    python = sys.executable
    if kind == "stashdb":
        return [python, "-m", "video_renamer.scrapers.stashdb", "--apply"]
    if kind == "fansdb":
        return [python, "-m", "video_renamer.scrapers.fansdb", "--apply"]
    if kind == "theporndb":
        return [python, "-m", "video_renamer.scrapers.theporndb", "--apply"]
    if kind == "merge":
        return [
            python,
            "-m",
            "video_renamer.scrapers.merge_studios",
            *MERGE_INPUTS,
            "--out",
            "data/studios.json",
            "--apply",
        ]
    raise ValueError(f"unknown studio task: {kind}")


def _terminate(process: subprocess.Popen[str]) -> int:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    return CANCELLED_CODE


def run(
    kind: str,
    on_line: Callable[[str], None] | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> int:
    """Выполняет операцию и возвращает код; 130 — отменено пользователем."""
    if cancel_event is not None and cancel_event.is_set():
        return CANCELLED_CODE

    command = _command(kind)
    line_callback = on_line or (lambda _line: None)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line.rstrip("\n"))
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()

    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return _terminate(process)
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None and lines.empty():
                    break
                continue
            if line is None:
                break
            if line.strip():
                line_callback(line)
        return process.wait()
    finally:
        if process.poll() is None:
            _terminate(process)
