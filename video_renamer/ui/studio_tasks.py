"""Запуск скраперов и слияния справочников из TUI.

Операции запускаются отдельными процессами: так их можно отменить из TUI,
не убивая сам интерфейс. Вывод читается построчно и отдаётся в журнал.

Корень проекта берётся из `video_renamer.paths`, а не из расположения этого
файла: `Path(__file__).parent.parent` указывал на сам пакет `video_renamer`,
поэтому дочерние процессы запускались в `video_renamer/` и искали данные в
`video_renamer/data/`. Пути передаются абсолютными, чтобы результат не
зависел от текущего каталога вызывающего процесса.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from video_renamer import paths

__all__ = ["run"]

PROJECT_ROOT = paths.PROJECT_ROOT
USER_ROOT = paths.USER_ROOT
DATA_DIR = paths.DATA_DIR
MERGE_INPUT_NAMES = (
    "studios.json",
    "studios_fansdb.json",
    "studios_theporndb.json",
)
CANCELLED_CODE = 130


def _merge_inputs() -> tuple[Path, ...]:
    """Входные файлы merge как абсолютные пути.

    База — тот же справочник, что читает приложение: обновлённый пользователем
    или поставляемый в пакете. Так первый запуск в установленном wheel не
    теряет поставленный справочник.
    """
    user_catalog = DATA_DIR / MERGE_INPUT_NAMES[0]
    base = user_catalog if user_catalog.is_file() else paths.PACKAGED_STUDIOS_PATH
    return (base, *(DATA_DIR / name for name in MERGE_INPUT_NAMES[1:]))


def _command(kind: str) -> list[str]:
    """Команда процесса для вида операции."""
    python = sys.executable
    if kind in {"stashdb", "fansdb", "theporndb"}:
        return [python, "-m", f"video_renamer.scrapers.{kind}", "--apply"]
    if kind == "merge":
        return [
            python,
            "-m",
            "video_renamer.scrapers.merge_studios",
            *(str(path) for path in _merge_inputs()),
            "--out",
            str(DATA_DIR / "studios.json"),
            "--apply",
        ]
    raise ValueError(f"unknown studio task: {kind}")


def _child_env() -> dict[str, str]:
    """Окружение дочернего процесса: UTF-8 и импорт пакета из этого дерева."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    root = str(PROJECT_ROOT)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root if not existing else root + os.pathsep + existing
    return env


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
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    process = subprocess.Popen(
        command,
        cwd=str(USER_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_child_env(),
    )
    lines: queue.Queue[str | None] = queue.Queue()
    if process.stdout is None:  # stdout=PIPE всегда даёт поток
        _terminate(process)
        raise RuntimeError("studio task started without a stdout pipe")

    def read_output() -> None:
        try:
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
