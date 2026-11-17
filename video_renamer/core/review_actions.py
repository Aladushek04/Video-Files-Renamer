"""Общие операции экрана ревью: справочники, сервис, preview и apply.

TUI (`ui/tui.py`) и GUI (`gui/controller.py`) отличаются только виджетами:
сервис строится, preview запускается и строки применяются одинаково. Журнал
пишется отдельным шагом, поэтому его сбой не отменяет уже выполненное
переименование.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from video_renamer.core.clean_ph_names import load_studios
from video_renamer.core.file_lock import RenamerLock
from video_renamer.core.fingerprint_sources import build_fingerprint_clients
from video_renamer.core.performer_names import PerformerIndex
from video_renamer.core.rename_intent import (
    KIND_RENAME,
    Intent,
    PlannedStep,
    raise_if_blocked,
    record_phase,
    unresolved_steps,
    write_intent,
)
from video_renamer.core.rename_log import (
    ACTION_RENAME,
    LogEntry,
    append_log,
    append_missing_log,
    format_log_timestamp,
    log_write_problem,
)
from video_renamer.core.stashdb_client import StashDBClient
from video_renamer.core.title_cache import TitleCache
from video_renamer.core.title_matcher import is_valid_final_stem
from video_renamer.core.title_service import (
    ApplyReport,
    ApplyResult,
    PreviewReport,
    PreviewRow,
    ProgressEvent,
    TitleService,
)
from video_renamer.paths import (
    CACHE_PATH,
    PERFORMERS_INDEX_PATH,
    resolve_studios_path,
)

__all__ = [
    "append_rename_log",
    "apply_rows",
    "basename_new_rel",
    "build_service",
    "load_performer_index",
    "load_studios_catalog",
    "remove_renamed",
    "run_preview",
    "studios_catalog",
    "validate_basename",
]

ProgressCallback = Callable[[ProgressEvent], None]

# Кеш разобранного справочника: повторные запросы состояния не должны
# перечитывать ~19 МБ JSON на каждое обновление индикаторов.
_catalog_cache: tuple[tuple[str, int, int], tuple[dict[str, str], str]] | None = None


def studios_catalog() -> tuple[dict[str, str], str]:
    """Справочник студий и причина, по которой он пуст.

    Второй элемент: `""` — файл прочитан (пустой словарь допустим),
    `"missing"` — файла нет, `"unreadable: …"` — файл есть, но не читается
    или повреждён. Ответ кешируется по пути, размеру и mtime: GUI и TUI
    спрашивают состояние при обновлении индикаторов, а разбор большого
    справочника занимает заметное время. Возвращённый словарь только для
    чтения — его же получают и вызовы `build_service`.
    """
    global _catalog_cache
    path = resolve_studios_path()
    try:
        info = path.stat()
    except OSError:
        _catalog_cache = None
        return {}, "missing"
    key = (os.fspath(path), info.st_mtime_ns, info.st_size)
    if _catalog_cache is not None and _catalog_cache[0] == key:
        return _catalog_cache[1]
    try:
        studios = load_studios(os.fspath(path))
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        result: tuple[dict[str, str], str] = ({}, f"unreadable: {exc}")
    else:
        result = (studios, "")
    _catalog_cache = (key, result)
    return result


def load_studios_catalog() -> dict[str, str]:
    """Справочник студий; нет файла или он битый — пустой справочник.

    Читается тот же файл, что найдёт сама программа: обновлённый пользователем
    справочник, а если его нет — поставляемый в пакете.
    """
    return studios_catalog()[0]


def load_performer_index() -> PerformerIndex | None:
    """Индекс имён, если он собран; иначе разбор без справочника."""
    if not PERFORMERS_INDEX_PATH.exists():
        return None
    try:
        return PerformerIndex.load(str(PERFORMERS_INDEX_PATH))
    except (OSError, ValueError, TypeError):
        return None


def build_service(
    folder: str | Path,
    *,
    studios: dict[str, str] | None = None,
    performers: PerformerIndex | None = None,
) -> TitleService:
    """Сервис без сети и кэша: ручные правки строк и сборка альтернатив."""
    return TitleService(
        str(folder),
        load_studios_catalog() if studios is None else studios,
        None,
        None,
        performers=load_performer_index() if performers is None else performers,
    )


def run_preview(
    folder: str | Path,
    *,
    api_key: str = "",
    offline: bool = False,
    studios: dict[str, str] | None = None,
    performers: PerformerIndex | None = None,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    service_callback: Callable[[TitleService], None] | None = None,
    cache_path: str | Path | None = None,
) -> PreviewReport:
    """Строит preview в текущем потоке; кэш создаётся и закрывается здесь.

    `offline` — явный запрет сети; он не включается автоматически из-за
    отсутствия ключа StashDB, иначе присутствующие ключи вторичных источников
    молча переставали бы работать. `service_callback` получает готовый сервис —
    интерфейс запоминает его, чтобы потом собирать имена по альтернативным
    кандидатам.
    """
    key = (api_key or "").strip()
    cache = TitleCache(str(CACHE_PATH if cache_path is None else cache_path))
    client = StashDBClient(key) if key else None
    try:
        service = TitleService(
            str(folder),
            load_studios_catalog() if studios is None else studios,
            cache,
            client,
            offline=offline,
            progress=progress,
            extra_clients=build_fingerprint_clients(os.environ),
            performers=load_performer_index() if performers is None else performers,
            cancel_event=cancel_event,
        )
        if service_callback is not None:
            service_callback(service)
        return service.preview()
    finally:
        cache.close()


def append_rename_log(
    path: str,
    rows: Sequence[PreviewRow],
    results: Sequence[ApplyResult],
    timestamp: str,
    *,
    root: str,
) -> None:
    """Дописывает в журнал только фактически переименованные строки.

    `new` — фактическое имя из `ApplyResult` (с суффиксом коллизии, если он был
    добавлен), `oshash`/`size`/`scene_id`/`source` — из `PreviewRow`, найденной
    по `old_rel`. `root` — корневая папка, относительно которой заданы пути.
    Запись выполняет `rename_log.append_log` (формат, миграция старого файла).
    """
    renamed = [result for result in results if result.status == "renamed"]
    if not renamed:
        return

    by_old = {row.old_rel: row for row in rows}
    abs_root = os.path.abspath(root)
    entries = []
    for result in renamed:
        row = by_old.get(result.old_rel)
        entries.append(
            LogEntry(
                timestamp=timestamp,
                action=ACTION_RENAME,
                undoes="",
                root=abs_root,
                old=result.old_rel,
                new=result.new_rel,
                oshash=row.oshash if row is not None else "",
                size=row.size if row is not None else None,
                scene_id=row.scene_id if row is not None else "",
                source=row.source if row is not None else "",
                legacy=False,
            )
        )
    append_log(path, entries)


def apply_rows(
    root: str | Path,
    rows: Sequence[PreviewRow],
    *,
    log_path: str | Path,
    force: bool = False,
    suffix_collisions: bool = False,
) -> tuple[ApplyReport, str]:
    """Переименовывает строки и пишет журнал.

    Возвращает отчёт и текст ошибки журнала. Работа идёт под общим замком: он
    покрывает проверку намерения, все rename и запись журнала. Если журнал или
    карта намерения недоступны, ни один файл не перемещается. Карта пишется до
    первого rename и хранит всю партию, чтобы `repair` мог разобрать сбой.
    """
    root = str(root)
    if not root:
        raise ValueError("Не выбрана папка для переименования")
    rows = list(rows)
    approved = [row for row in rows if row.approved]
    if not approved:
        return (
            ApplyReport(
                results=tuple(
                    ApplyResult(row.old_rel, row.new_rel, "skipped", "not approved") for row in rows
                ),
                had_errors=False,
            ),
            "",
        )

    # Быстрая проверка без замка: если папка журнала недоступна, замок тоже
    # создать нельзя, а пользователю нужен понятный отчёт, а не трассировка.
    journal_problem = log_write_problem(log_path)
    if journal_problem:
        return _journal_error_report(rows, journal_problem), ""

    with RenamerLock(log_path):
        raise_if_blocked(log_path)
        journal_problem = log_write_problem(log_path)
        if journal_problem:
            return _journal_error_report(rows, journal_problem), ""

        timestamp = format_log_timestamp()
        intent_box: dict[str, Intent] = {}

        def plan_hook(steps: tuple[PlannedStep, ...]) -> None:
            log_rows = _rename_log_rows(steps, timestamp)
            intent_box["intent"] = write_intent(
                log_path,
                kind=KIND_RENAME,
                steps=list(steps),
                log_rows=log_rows,
            )

        service = TitleService(root, {}, None)
        report = service.apply_rows(
            rows,
            dry_run=False,
            force=force,
            suffix_collisions=suffix_collisions,
            plan_hook=plan_hook,
        )

        intent = intent_box.get("intent")
        if intent is None:
            return report, ""

        final = {
            result.old_rel: result.new_rel
            for result in report.results
            if result.status == "renamed"
        }
        rows_to_log = [row for row in intent.log_rows if final.get(row["old"]) == row["new"]]
        log_error = ""
        problems: list[str] = []
        try:
            append_missing_log(
                str(log_path),
                [LogEntry(legacy=False, **row) for row in rows_to_log],
            )
        except (OSError, ValueError) as exc:
            log_error = str(exc) or exc.__class__.__name__
            problems.append(f"журнал не записан: {log_error}")

        problems.extend(unresolved_steps(intent))
        try:
            record_phase(
                log_path,
                intent.op_id,
                "needs_repair" if problems else "done",
                problems,
            )
        except OSError as exc:
            # Операция остаётся открытой: repair допишет фазу по диску.
            log_error = log_error or f"не удалось записать состояние: {exc}"
        return report, log_error


def _rename_log_rows(steps: Sequence[PlannedStep], timestamp: str) -> list[dict]:
    """Строки журнала для партии; id вычисляется из тех же полей при чтении."""
    return [
        {
            "timestamp": timestamp,
            "action": ACTION_RENAME,
            "undoes": "",
            "root": step.root,
            "old": step.old_rel,
            "new": step.new_rel,
            "oshash": step.oshash,
            "size": step.size,
            "scene_id": step.scene_id,
            "source": step.source,
        }
        for step in steps
    ]


def _journal_error_report(rows: Sequence[PreviewRow], problem: str) -> ApplyReport:
    """Все одобренные строки получают ошибку, неодобренные — `skipped`."""
    results = tuple(
        ApplyResult(row.old_rel, row.new_rel, "error", f"journal is not writable: {problem}")
        if row.approved
        else ApplyResult(row.old_rel, row.new_rel, "skipped", "not approved")
        for row in rows
    )
    return ApplyReport(results=results, had_errors=any(row.approved for row in rows))


def remove_renamed(rows: Iterable[PreviewRow], report: ApplyReport) -> list[PreviewRow]:
    """Оставляет строки для повторной попытки: неодобренные и неудавшиеся."""
    renamed = {result.old_rel for result in report.results if result.status == "renamed"}
    return [row for row in rows if not row.approved or row.old_rel not in renamed]


def validate_basename(row: PreviewRow, value: str) -> str:
    """Правила ручного имени в TUI и GUI; пустая строка — значение верное."""
    value = value.strip()
    suffix = os.path.splitext(row.old_rel)[1]
    if not value:
        return "Имя не может быть пустым."
    if "/" in value or "\\" in value:
        return "Подпапки запрещены."
    value_suffix = os.path.splitext(value)[1]
    if value_suffix.casefold() != suffix.casefold():
        return f"Расширение должно совпадать с исходным: {suffix} (регистр не важен)."
    if len(value) > 255:
        return "Имя длиннее 255 символов."
    if not is_valid_final_stem(os.path.splitext(value)[0]):
        return "Недопустимое имя для Windows."
    return ""


def basename_new_rel(row: PreviewRow, value: str) -> str:
    """`new_rel` из нового имени файла с сохранением папки."""
    parent = os.path.dirname(row.old_rel)
    value = value.strip()
    return os.path.join(parent, value) if parent else value
