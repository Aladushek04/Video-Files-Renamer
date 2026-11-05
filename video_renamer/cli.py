#!/usr/bin/env python3
"""CLI для предпросмотра и применения переименований видео.

Тонкий адаптер над `title_service.TitleService`: CLI отвечает за argparse,
CSV-ревью, печать и коды возврата, а вся логика сопоставления и переименования
живёт в сервисе.

Коды возврата:

- `0` — успех;
- `1` — были ошибки отдельных файлов или временные сбои StashDB;
- `2` — фатальная ошибка (неверный ключ, ошибка запроса, плохой CSV/каталог).

`preview` никогда не переименовывает файлы. `apply` по умолчанию dry-run и
пишет `rename_log.csv` только для фактически переименованных строк при
явном `--apply`.
"""
import argparse
import csv
import os
import sqlite3
import sys
from collections.abc import Sequence

from video_renamer.core.clean_ph_names import load_studios
from video_renamer.core.rename_log import (
    ACTION_RENAME,
    LogEntry,
    _guard_formula,
    _unguard_formula,
    active_renames,
    append_log,
    format_log_timestamp,
    read_log,
)
from video_renamer.core.undo import run_undo, select_for_undo
from video_renamer.core.stashdb_client import (
    StashDBAuthError,
    StashDBClient,
    StashDBQueryError,
    StashDBResponseError,
    StashDBTransientError,
)
from video_renamer.core.title_cache import TitleCache
from video_renamer.core.title_service import ApplyResult, PreviewRow, TitleService
from video_renamer.paths import CACHE_PATH, LOG_PATH, STUDIOS_PATH, load_project_env

__all__ = [
    "append_rename_log",
    "main",
    "read_preview_csv",
    "write_preview_csv",
]

CSV_FIELDS = (
    "approved", "old", "new", "source", "confidence",
    "scene_id", "note", "oshash", "size",
)
_APPROVED_VALUES = frozenset({"y", "yes", "1", "true"})

DEFAULT_CSV = "preview.csv"
DEFAULT_LOG = str(LOG_PATH)
DEFAULT_STUDIOS = str(STUDIOS_PATH)
DEFAULT_CACHE = str(CACHE_PATH)


def _parse_approved(value: object) -> bool:
    """`approved` из CSV: пустое значение и мусор означают `n`."""
    if value is None:
        return False
    return str(value).strip().lower() in _APPROVED_VALUES


def _parse_float(value: object) -> float:
    """Число из CSV; пустое или битое значение — 0.0."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _parse_int(value: object) -> int:
    """Целое из CSV; пустое или битое значение — 0."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _ensure_parent(path: str) -> None:
    """Создаёт родительскую папку файла, если её нет."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_preview_csv(path: str, rows: Sequence[PreviewRow]) -> None:
    """Пишет CSV-ревью в `utf-8-sig` с разделителем `;`."""
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(CSV_FIELDS)
        for row in rows:
            writer.writerow((
                "y" if row.approved else "n",
                _guard_formula(row.old_rel),
                _guard_formula(row.new_rel),
                row.source,
                str(row.confidence),
                row.scene_id,
                _guard_formula(row.note),
                row.oshash,
                "" if row.size is None else str(row.size),
            ))


def read_preview_csv(path: str) -> list[PreviewRow]:
    """Читает CSV-ревью и снимает формула-guard с `old`/`new`."""
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        fields = reader.fieldnames or []
        missing = [name for name in CSV_FIELDS if name not in fields]
        if missing:
            raise ValueError(
                "CSV is missing columns: " + ", ".join(missing)
            )

        rows: list[PreviewRow] = []
        for record in reader:
            if not any(
                (record.get(name) or "").strip() for name in CSV_FIELDS
            ):
                continue
            rows.append(PreviewRow(
                approved=_parse_approved(record.get("approved")),
                old_rel=_unguard_formula(record.get("old") or ""),
                new_rel=_unguard_formula(record.get("new") or ""),
                source=record.get("source") or "",
                confidence=_parse_float(record.get("confidence")),
                scene_id=record.get("scene_id") or "",
                note=_unguard_formula(record.get("note") or ""),
                oshash=record.get("oshash") or "",
                size=_parse_int(record.get("size")),
            ))
    return rows


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
        entries.append(LogEntry(
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
        ))
    append_log(path, entries)


def _timestamp() -> str:
    """Метка времени журнала: одна функция для CLI и TUI."""
    return format_log_timestamp()


def _build_parser() -> argparse.ArgumentParser:
    """Аргументы `preview` и `apply`."""
    parser = argparse.ArgumentParser(
        prog="python -m video_renamer.cli",
        description="Prefetch scene titles and rename videos after review.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview = subparsers.add_parser(
        "preview", help="scan a folder and write preview.csv"
    )
    preview.add_argument("directory", help="folder with video files")
    preview.add_argument("--csv", default=DEFAULT_CSV,
                         help="preview CSV path (default: preview.csv)")
    preview.add_argument("--recursive", action="store_true",
                         help="scan subfolders")
    preview.add_argument("--limit", type=int, default=None,
                         help="process only the first N files")
    preview.add_argument("--no-text", action="store_true",
                         help="disable StashDB text search")
    preview.add_argument("--offline", action="store_true",
                         help="use only the local cache, no network")
    preview.add_argument("--refresh", action="store_true",
                         help="ignore cached results")
    preview.add_argument("--studios", default=DEFAULT_STUDIOS,
                         help="studios JSON (default: studios.json)")
    preview.add_argument("--cache", default=DEFAULT_CACHE,
                         help="SQLite cache path")
    preview.set_defaults(handler=_run_preview)

    apply = subparsers.add_parser(
        "apply", help="apply approved rows from a reviewed CSV"
    )
    apply.add_argument("csv", help="reviewed preview CSV")
    apply.add_argument("--root", required=True,
                       help="folder the relative paths are based on")
    apply.add_argument("--apply", action="store_true",
                       help="actually rename files (default: dry-run)")
    apply.add_argument("--log", default=DEFAULT_LOG,
                       help="rename log path (default: rename_log.csv)")
    apply.add_argument("--force", action="store_true",
                       help="skip stale size/OSHASH checks")
    apply.add_argument("--suffix-collisions", action="store_true",
                       help="add (2), (3) instead of failing on collisions")
    apply.set_defaults(handler=_run_apply)

    undo = subparsers.add_parser(
        "undo", help="list or undo applied renames from the journal"
    )
    undo.add_argument("--list", action="store_true",
                      help="print active journal rows with their ids")
    undo.add_argument("--ids", default=None,
                      help="comma-separated ids to undo (see --list)")
    undo.add_argument("--log", default=DEFAULT_LOG,
                      help="rename log path (default: rename_log.csv)")
    undo.add_argument("--root", default=None,
                      help="folder for legacy rows without a stored root")
    undo.add_argument("--apply", action="store_true",
                      help="actually undo (default: dry-run)")
    undo.add_argument("--confirm-legacy", action="store_true",
                      help="allow undoing legacy rows (requires --root)")
    undo.set_defaults(handler=_run_undo)

    return parser


def _load_studios(path: str) -> dict[str, str] | None:
    """Читает studios.json; отсутствие файла — предупреждение и `{}`."""
    if not os.path.exists(path):
        print(
            f"warning: studios file not found: {path}; "
            f"studio matching is disabled",
            file=sys.stderr,
        )
        return {}
    try:
        return load_studios(path)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read studios file {path}: {exc}",
              file=sys.stderr)
        return None


def _run_preview(args: argparse.Namespace,
                 parser: argparse.ArgumentParser) -> int:
    """Выполняет preview и пишет CSV; файлы не изменяются."""
    if args.refresh and args.offline:
        parser.error("--refresh cannot be used together with --offline")

    if not os.path.isdir(args.directory):
        print(f"error: not a directory: {args.directory}", file=sys.stderr)
        return 2

    load_project_env()
    api_key = os.environ.get("STASHDB_API_KEY", "").strip()
    offline = bool(args.offline)
    if not api_key and not offline:
        print(
            "warning: STASHDB_API_KEY is not set; "
            "running offline (cache + heuristics)",
            file=sys.stderr,
        )
        offline = True

    studios = _load_studios(args.studios)
    if studios is None:
        return 2

    try:
        cache = TitleCache(args.cache)
    except (OSError, sqlite3.Error) as exc:
        print(f"error: cannot open cache {args.cache}: {exc}",
              file=sys.stderr)
        return 2

    client = StashDBClient(api_key) if api_key and not offline else None
    try:
        report = TitleService(
            args.directory,
            studios,
            cache,
            client,
            recursive=args.recursive,
            text_search=not args.no_text,
            offline=offline,
            refresh=args.refresh,
            limit=args.limit,
        ).preview()
    except StashDBAuthError as exc:
        print(
            f"error: StashDB rejected the request: {exc}; "
            f"проверь STASHDB_API_KEY",
            file=sys.stderr,
        )
        return 2
    except (StashDBQueryError, StashDBResponseError) as exc:
        print(f"error: StashDB query failed: {exc}", file=sys.stderr)
        return 2
    except StashDBTransientError as exc:
        print(f"error: StashDB request failed: {exc}", file=sys.stderr)
        return 1
    finally:
        cache.close()

    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    try:
        write_preview_csv(args.csv, report.rows)
    except OSError as exc:
        print(f"error: cannot write {args.csv}: {exc}", file=sys.stderr)
        return 2

    print(f"{len(report.rows)} row(s) written to {args.csv}")
    return 1 if report.had_transient_errors else 0


def _run_apply(args: argparse.Namespace,
               _parser: argparse.ArgumentParser) -> int:
    """Применяет одобренные строки; по умолчанию — dry-run."""
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2

    try:
        rows = read_preview_csv(args.csv)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"error: cannot read {args.csv}: {exc}", file=sys.stderr)
        return 2

    report = TitleService(root, {}, None).apply_rows(
        rows,
        dry_run=not args.apply,
        force=args.force,
        suffix_collisions=args.suffix_collisions,
    )

    renamed = 0
    skipped = 0
    errors = 0
    for result in report.results:
        if result.status == "renamed":
            renamed += 1
            verb = "renamed" if args.apply else "would rename"
            print(f"{verb}: {result.old_rel} -> {result.new_rel}")
        elif result.status == "skipped":
            skipped += 1
        else:
            errors += 1
            print(
                f"error: {result.old_rel}: {result.message}",
                file=sys.stderr,
            )

    if args.apply and renamed:
        try:
            append_rename_log(args.log, rows, report.results, _timestamp(), root=root)
        except OSError as exc:
            print(
                f"error: cannot write rename log {args.log}: {exc}",
                file=sys.stderr,
            )
            errors += 1

    mode = "applied" if args.apply else "dry-run"
    print(
        f"{mode}: {renamed} renamed, {skipped} skipped, {errors} error(s)"
    )
    return 1 if (report.had_errors or errors) else 0


def _run_undo(args: argparse.Namespace,
              parser: argparse.ArgumentParser) -> int:
    """Список активных строк журнала или откат выбранных; по умолчанию dry-run."""
    if not args.list and not args.ids:
        print("error: undo needs --list or --ids", file=sys.stderr)
        return 2

    try:
        entries = read_log(args.log)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"error: cannot read rename log {args.log}: {exc}", file=sys.stderr)
        return 2

    if args.list:
        for entry in active_renames(entries):
            origin = entry.root or "(старый формат)"
            print(f"{entry.id}\t{entry.old} -> {entry.new}\t{origin}\t{entry.timestamp}")
        return 0

    ids = [part for part in args.ids.split(",") if part.strip()]
    root_override = os.path.abspath(args.root) if args.root else None
    selection = select_for_undo(
        entries, ids,
        root_override=root_override,
        confirm_legacy=args.confirm_legacy,
    )
    for error in selection.errors:
        print(f"error: {error}", file=sys.stderr)

    report = run_undo(selection, log_path=args.log, dry_run=not args.apply)
    undone = 0
    failed = 0
    for result in report.results:
        if result.status == "renamed":
            undone += 1
            print(f"undone: {result.old_rel} -> {result.new_rel}")
        elif result.status == "dry-run":
            undone += 1
            print(f"would undo: {result.old_rel} -> {result.new_rel}")
        else:
            failed += 1
            print(f"error: {result.id}: {result.message}", file=sys.stderr)

    mode = "undo" if args.apply else "undo (dry-run)"
    print(f"{mode}: {undone} undone, {failed + len(selection.errors)} error(s)")
    return 1 if (selection.errors or report.had_errors) else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа CLI; возвращает код вместо `sys.exit`.

    `argparse` по-прежнему завершает процесс `SystemExit(2)` на ошибке
    аргументов (включая несовместимые `--refresh` и `--offline`).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args, parser)


if __name__ == "__main__":
    sys.exit(main())
