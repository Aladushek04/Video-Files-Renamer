#!/usr/bin/env python3
"""Откат выбранных записанных переименований.

`select_for_undo` выбирает активные строки журнала по идентификаторам и
проверяет, что для них известен корень. `run_undo` откатывает их через
`TitleService.apply_rows` с теми же проверками, что у `apply` (путь внутри
корня, обычный файл, OSHASH и размер, если записан, занятость цели,
двухфазное переименование). Успешные откаты дописываются в журнал строками
`undo`, которые ссылаются на отменённые строки.

Строки старого формата (без корня) откатываются только с явным корнем и
`confirm_legacy=True`.

Зависимостей нет — только стандартная библиотека.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from video_renamer.core.rename_log import (
    ACTION_UNDO,
    LogEntry,
    active_renames,
    append_log,
    format_log_timestamp,
)
from video_renamer.core.title_service import PreviewRow, TitleService

__all__ = [
    "UndoReport",
    "UndoResult",
    "UndoSelection",
    "UndoTarget",
    "run_undo",
    "select_for_undo",
]


@dataclass(frozen=True)
class UndoTarget:
    """Строка журнала, которую нужно откатить, и корень, относительно которого она."""

    entry: LogEntry
    root: str


@dataclass(frozen=True)
class UndoSelection:
    """Выбранные для отката строки и ошибки по остальным идентификаторам."""

    targets: tuple[UndoTarget, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class UndoResult:
    """Итог отката одной строки: `renamed`, `dry-run`, `error` или `journal-missing`."""

    id: str
    old_rel: str
    new_rel: str
    status: str
    message: str


@dataclass(frozen=True)
class UndoReport:
    results: tuple[UndoResult, ...]
    had_errors: bool


def select_for_undo(
    entries: Sequence[LogEntry],
    ids: Sequence[str],
    *,
    root_override: str | None = None,
    confirm_legacy: bool = False,
) -> UndoSelection:
    """Выбирает активные строки по идентификаторам; порядок — от новой к старой."""
    position = {entry.id: index for index, entry in enumerate(entries)}
    by_id = {entry.id: entry for entry in entries}
    active = {entry.id for entry in active_renames(entries)}
    targets: list[UndoTarget] = []
    errors: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        identifier = raw.strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        entry = by_id.get(identifier)
        if entry is None:
            errors.append(f"{identifier}: строка не найдена")
            continue
        if identifier not in active:
            errors.append(f"{identifier}: строка уже откатена или не является переименованием")
            continue
        if entry.legacy:
            if not root_override:
                errors.append(f"{identifier}: строка старого формата: нужен --root")
                continue
            if not confirm_legacy:
                errors.append(f"{identifier}: строка старого формата: нужен --confirm-legacy")
                continue
            root = root_override
        else:
            root = entry.root
        targets.append(UndoTarget(entry, os.path.abspath(root)))
    targets.sort(key=lambda target: position[target.entry.id], reverse=True)
    return UndoSelection(tuple(targets), tuple(errors))


def _undo_entry(target: UndoTarget, timestamp: str) -> LogEntry:
    entry = target.entry
    return LogEntry(
        timestamp=timestamp,
        action=ACTION_UNDO,
        undoes=entry.id,
        root=target.root,
        old=entry.new,
        new=entry.old,
        oshash=entry.oshash,
        size=entry.size,
        scene_id=entry.scene_id,
        source=entry.source,
        legacy=False,
    )


def run_undo(
    selection: UndoSelection,
    *,
    log_path: str,
    dry_run: bool,
    now: datetime | None = None,
) -> UndoReport:
    """Откатывает выбранные строки; при `dry_run` ничего не меняет и не пишет журнал."""
    timestamp = format_log_timestamp(now)
    groups: dict[str, list[tuple[int, UndoTarget]]] = {}
    for index, target in enumerate(selection.targets):
        groups.setdefault(target.root, []).append((index, target))

    results: dict[int, UndoResult] = {}
    undo_entries: list[LogEntry] = []
    renamed_indexes: list[int] = []
    for root, items in groups.items():
        rows = [
            PreviewRow(
                True,
                target.entry.new,
                target.entry.old,
                target.entry.source,
                0.0,
                target.entry.scene_id,
                "undo",
                target.entry.oshash,
                target.entry.size,
                (),
            )
            for _, target in items
        ]
        report = TitleService(root, {}, None).apply_rows(rows, dry_run=dry_run)
        for (index, target), outcome in zip(items, report.results):
            status = outcome.status
            if status == "renamed" and dry_run:
                status = "dry-run"
            results[index] = UndoResult(
                target.entry.id,
                target.entry.new,
                target.entry.old,
                status,
                outcome.message,
            )
            if status == "renamed":
                undo_entries.append(_undo_entry(target, timestamp))
                renamed_indexes.append(index)

    had_errors = any(result.status == "error" for result in results.values())
    if undo_entries and not dry_run:
        try:
            append_log(log_path, undo_entries)
        except OSError as exc:
            for index in renamed_indexes:
                previous = results[index]
                results[index] = UndoResult(
                    previous.id,
                    previous.old_rel,
                    previous.new_rel,
                    "journal-missing",
                    f"откат выполнен, журнал не записан: {exc}",
                )
            had_errors = True

    ordered = tuple(results[index] for index in sorted(results))
    return UndoReport(
        ordered, had_errors or any(result.status == "journal-missing" for result in ordered)
    )
