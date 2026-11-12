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
from video_renamer.core.fingerprint_sources import build_fingerprint_clients
from video_renamer.core.performer_names import PerformerIndex
from video_renamer.core.rename_log import (
    ACTION_RENAME,
    LogEntry,
    append_log,
    format_log_timestamp,
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
from video_renamer.paths import CACHE_PATH, PERFORMERS_INDEX_PATH, STUDIOS_PATH

__all__ = [
    "append_rename_log",
    "apply_rows",
    "basename_new_rel",
    "build_service",
    "load_performer_index",
    "load_studios_catalog",
    "remove_renamed",
    "run_preview",
    "validate_basename",
]

ProgressCallback = Callable[[ProgressEvent], None]


def load_studios_catalog() -> dict[str, str]:
    """Справочник студий; нет файла или он битый — пустой справочник."""
    if not STUDIOS_PATH.exists():
        return {}
    try:
        return load_studios(str(STUDIOS_PATH))
    except (OSError, ValueError, TypeError):
        return {}


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
    studios: dict[str, str] | None = None,
    performers: PerformerIndex | None = None,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    service_callback: Callable[[TitleService], None] | None = None,
    cache_path: str | Path | None = None,
) -> PreviewReport:
    """Строит preview в текущем потоке; кэш создаётся и закрывается здесь.

    `service_callback` получает готовый сервис — интерфейс запоминает его,
    чтобы потом собирать имена по альтернативным кандидатам.
    """
    key = (api_key or "").strip()
    offline = not key
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
            extra_clients=build_fingerprint_clients(os.environ, offline=offline),
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
) -> tuple[ApplyReport, str]:
    """Переименовывает строки и пишет журнал.

    Возвращает отчёт и текст ошибки журнала. Ошибка журнала не отменяет
    переименование: файлы уже переименованы, пользователь видит предупреждение.
    """
    root = str(root)
    if not root:
        raise ValueError("Не выбрана папка для переименования")
    service = TitleService(root, {}, None, None)
    report = service.apply_rows(list(rows), dry_run=False)
    log_error = ""
    if any(result.status == "renamed" for result in report.results):
        try:
            append_rename_log(
                str(log_path),
                rows,
                report.results,
                format_log_timestamp(),
                root=root,
            )
        except Exception as exc:
            log_error = str(exc) or exc.__class__.__name__
    return report, log_error


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
