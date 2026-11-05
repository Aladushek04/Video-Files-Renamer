"""Запуск скраперов и слияния справочников из TUI.

Скраперы остаются обычными CLI-функциями; здесь мы лишь перехватываем их
вывод и отдаём его построчно в TUI. Сеть, ключи и файлы — как в CLI.
"""
from __future__ import annotations

import contextlib
from collections.abc import Callable

from video_renamer import merge_studios
from video_renamer.scrapers import fansdb, stashdb, theporndb

__all__ = ["run"]

MERGE_INPUTS = (
    "data/studios.json",
    "data/studios_fansdb.json",
    "data/studios_theporndb.json",
)


class _LineWriter:
    """Поток, который режет вывод на строки и отдаёт их callback-у."""

    def __init__(self, on_line: Callable[[str], None]) -> None:
        self._on_line = on_line
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._on_line(line)
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self._on_line(self._buffer)
        self._buffer = ""


def run(kind: str, on_line: Callable[[str], None] | None = None) -> int:
    """Выполняет `stashdb` / `fansdb` / `theporndb` / `merge` и возвращает код."""
    line_callback = on_line or (lambda _line: None)
    writer = _LineWriter(line_callback)
    with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
        if kind == "stashdb":
            code = stashdb.main(["--apply"])
        elif kind == "fansdb":
            code = fansdb.main(
                ["--apply"], source=fansdb.FANSDB_SOURCE
            )
        elif kind == "theporndb":
            code = theporndb.main(["--apply"])
        elif kind == "merge":
            code = merge_studios.main([
                *MERGE_INPUTS,
                "--out", "data/studios.json",
                "--apply",
            ])
        else:
            raise ValueError(f"unknown studio task: {kind}")
    writer.flush()
    return code
