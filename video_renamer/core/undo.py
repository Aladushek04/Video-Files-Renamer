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

from video_renamer.core.file_lock import RenamerLock
from video_renamer.core.rename_intent import (
    KIND_UNDO,
    Intent,
    PlannedStep,
    raise_if_blocked,
    record_phase,
    unresolved_steps,
    write_intent,
)
from video_renamer.core.rename_log import (
    ACTION_UNDO,
    LogEntry,
    active_renames,
    append_missing_log,
    format_log_timestamp,
    log_write_problem,
)
from video_renamer.core.title_service import PreviewRow, TitleService, new_temp_path

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


def _undo_row(target: UndoTarget) -> PreviewRow:
    """Строка apply для возврата файла на прежнее имя."""
    entry = target.entry
    return PreviewRow(
        True,
        entry.new,
        entry.old,
        entry.source,
        0.0,
        entry.scene_id,
        "undo",
        entry.oshash,
        entry.size,
        (),
    )


def _path_key(path: str) -> str:
    """Ключ пути для виртуальной модели dry-run."""
    return os.path.normcase(os.path.abspath(path))


def _all_sources_present(root: str, items: Sequence[tuple[int, UndoTarget]]) -> bool:
    """Существуют ли все текущие имена, которые нужно вернуть назад.

    Если да — строки можно откатывать одной партией: так работает обмен
    `A ↔ B` и цепочка `a → b, b → c`, применённая одним apply. Если хотя бы
    одного имени нет — это последовательные операции одного файла, и откат
    идёт пошагово от новой к старой. Проверка всегда идёт по настоящему диску:
    имена, которые вернут предыдущие шаги, партией не откатываются.
    """
    return all(os.path.lexists(os.path.join(root, target.entry.new)) for _, target in items)


def _batch_apply(
    root: str,
    items: Sequence[tuple[int, UndoTarget]],
    dry_run: bool,
    temp_paths: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Откат партией: одновременное переименование через временные имена."""
    rows = [_undo_row(target) for _, target in items]
    report = TitleService(root, {}, None).apply_rows(rows, dry_run=dry_run, temp_paths=temp_paths)
    return [(result.status, result.message) for result in report.results]


def _remember_batch(
    virtual: dict[str, bool], root: str, items: Sequence[tuple[int, UndoTarget]]
) -> None:
    """Учитывает в dry-run, какие имена партия вернёт и какие заберёт."""
    restored = {_path_key(os.path.join(root, target.entry.old)) for _, target in items}
    sources = {_path_key(os.path.join(root, target.entry.new)) for _, target in items}
    for key in sources - restored:
        virtual[key] = False
    for key in restored:
        virtual[key] = True


def _sequential_apply(
    root: str,
    items: Sequence[tuple[int, UndoTarget]],
    temp_paths: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Пошаговый откат от новой записи к старой: каждый шаг видит диск после прошлого."""
    service = TitleService(root, {}, None)
    outcomes: list[tuple[str, str]] = []
    for _, target in items:
        row = _undo_row(target)
        report = service.apply_rows([row], dry_run=False, temp_paths=temp_paths)
        result = report.results[0]
        outcomes.append((result.status, result.message))
    return outcomes


def _sequential_dry_run(
    root: str, items: Sequence[tuple[int, UndoTarget]], virtual: dict[str, bool]
) -> list[tuple[str, str]]:
    """Предсказывает пошаговый откат, не меняя диск.

    Пока путь реальный, шаг проверяется настоящим apply в режиме dry-run
    (размер, OSHASH, правила имени, занятость цели). Для пути, который появится
    только после предыдущего шага, проверка невозможна: он помечается
    `dry-run` с явной оговоркой, а не выдаётся за проверенный. `virtual`
    живёт всё время отката, потому что шаги последовательных операций
    распределены по разным группам журнала.
    """
    service = TitleService(root, {}, None)
    outcomes: list[tuple[str, str]] = []
    for _, target in items:
        row = _undo_row(target)
        old_path = os.path.join(root, row.old_rel)
        new_path = os.path.join(root, row.new_rel)
        old_key = _path_key(old_path)
        new_key = _path_key(new_path)
        old_virtual = old_key in virtual
        old_exists = virtual.get(old_key, os.path.lexists(old_path))
        new_exists = virtual.get(new_key, os.path.lexists(new_path))
        if not old_exists:
            outcomes.append(("error", "cannot stat old path: файл отсутствует"))
            continue
        if new_exists and new_key != old_key:
            outcomes.append(("error", "target is occupied"))
            continue
        if old_virtual:
            outcomes.append(("dry-run", "после отката предыдущей операции"))
            virtual[old_key] = False
            virtual[new_key] = True
            continue
        result = service.apply_rows([row], dry_run=True).results[0]
        if result.status == "renamed":
            outcomes.append(("dry-run", result.message))
            virtual[old_key] = False
            virtual[new_key] = True
        else:
            outcomes.append((result.status, result.message))
    return outcomes


def _undo_log_row(target: UndoTarget, timestamp: str) -> dict:
    """Строка журнала отката в том же виде, что хранится в карте намерения."""
    entry = target.entry
    return {
        "timestamp": timestamp,
        "action": ACTION_UNDO,
        "undoes": entry.id,
        "root": target.root,
        "old": entry.new,
        "new": entry.old,
        "oshash": entry.oshash,
        "size": entry.size,
        "scene_id": entry.scene_id,
        "source": entry.source,
    }


def _undo_steps(targets: Sequence[UndoTarget]) -> list[PlannedStep]:
    """Все шаги отката с точными путями и временными именами."""
    steps: list[PlannedStep] = []
    for target in targets:
        old_path = os.path.abspath(os.path.join(target.root, target.entry.new))
        new_path = os.path.abspath(os.path.join(target.root, target.entry.old))
        steps.append(
            PlannedStep(
                old_path=old_path,
                temp_path=new_temp_path(old_path),
                new_path=new_path,
                old_rel=target.entry.new,
                new_rel=target.entry.old,
                root=target.root,
                oshash=target.entry.oshash,
                size=target.entry.size,
                scene_id=target.entry.scene_id,
                source=target.entry.source,
            )
        )
    return steps


def _blocked_report(selection: UndoSelection, message: str) -> UndoReport:
    """Ни один файл не тронут: каждая выбранная строка получает ошибку."""
    results = tuple(
        UndoResult(target.entry.id, target.entry.new, target.entry.old, "error", message)
        for target in selection.targets
    )
    return UndoReport(results, bool(results))


def run_undo(
    selection: UndoSelection,
    *,
    log_path: str,
    dry_run: bool,
    now: datetime | None = None,
) -> UndoReport:
    """Откатывает выбранные строки; при `dry_run` ничего не меняет и не пишет журнал.

    Записи одного apply (одинаковый timestamp) откатываются одной партией.
    Последовательные операции над одним файлом (`a → b`, затем `b → c`) идут
    пошагово от новой к старой: имени `b` ещё нет, его вернёт предыдущий шаг.

    Без `dry_run` откат идёт под общим с журналом замком: сначала проверяются
    журнал и карта намерения, потом пишется карта всей партии, затем файлы.
    """
    if dry_run:
        return _run_undo_impl(selection, log_path=log_path, dry_run=True, now=now)
    problem = log_write_problem(log_path)
    if problem:
        return _blocked_report(selection, f"journal is not writable: {problem}")
    with RenamerLock(log_path):
        raise_if_blocked(log_path)
        problem = log_write_problem(log_path)
        if problem:
            return _blocked_report(selection, f"journal is not writable: {problem}")
        return _run_undo_impl(selection, log_path=log_path, dry_run=False, now=now)


def _run_undo_impl(
    selection: UndoSelection,
    *,
    log_path: str,
    dry_run: bool,
    now: datetime | None,
) -> UndoReport:
    timestamp = format_log_timestamp(now)
    groups: dict[tuple[str, str], list[tuple[int, UndoTarget]]] = {}
    for index, target in enumerate(selection.targets):
        groups.setdefault((target.root, target.entry.timestamp), []).append((index, target))

    intent: Intent | None = None
    temp_paths: dict[str, str] | None = None
    if not dry_run and selection.targets:
        try:
            intent = write_intent(
                log_path,
                kind=KIND_UNDO,
                steps=_undo_steps(selection.targets),
                log_rows=[_undo_log_row(target, timestamp) for target in selection.targets],
            )
        except OSError as exc:
            return _blocked_report(selection, f"cannot record rename intent: {exc}")
        temp_paths = {_path_key(step.old_path): step.temp_path for step in intent.steps}

    results: dict[int, UndoResult] = {}
    renamed_indexes: list[int] = []
    virtual: dict[str, bool] = {}
    journal_error = ""
    for (root, _stamp), items in groups.items():
        if _all_sources_present(root, items):
            outcomes = _batch_apply(root, items, dry_run, temp_paths)
            if dry_run:
                _remember_batch(virtual, root, items)
        elif dry_run:
            outcomes = _sequential_dry_run(root, items, virtual)
        else:
            outcomes = _sequential_apply(root, items, temp_paths)
        for (index, target), (status, message) in zip(items, outcomes):
            if status == "renamed" and dry_run:
                status = "dry-run"
            results[index] = UndoResult(
                target.entry.id,
                target.entry.new,
                target.entry.old,
                status,
                message,
            )
            if status == "renamed":
                renamed_indexes.append(index)

    if renamed_indexes and intent is not None:
        renamed_ids = {selection.targets[index].entry.id for index in renamed_indexes}
        rows = [
            LogEntry(legacy=False, **row)
            for row in intent.log_rows
            if row.get("undoes") in renamed_ids
        ]
        try:
            append_missing_log(log_path, rows)
        except (OSError, ValueError) as exc:
            journal_error = str(exc) or exc.__class__.__name__
            for index in renamed_indexes:
                previous = results[index]
                results[index] = UndoResult(
                    previous.id,
                    previous.old_rel,
                    previous.new_rel,
                    "journal-missing",
                    f"откат выполнен, журнал не записан: {exc}",
                )

    if intent is not None:
        problems = unresolved_steps(intent)
        if journal_error:
            problems.append(f"журнал не записан: {journal_error}")
        try:
            record_phase(
                log_path,
                intent.op_id,
                "needs_repair" if problems else "done",
                problems,
            )
        except OSError:
            # Партия остаётся открытой; repair восстановит фазу по диску.
            pass

    ordered = tuple(results[index] for index in sorted(results))
    had_errors = any(result.status in {"error", "journal-missing"} for result in ordered)
    return UndoReport(ordered, had_errors)
