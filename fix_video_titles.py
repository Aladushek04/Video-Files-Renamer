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
from datetime import datetime, timezone

from clean_ph_names import load_studios
from stashdb_client import (
    StashDBAuthError,
    StashDBClient,
    StashDBQueryError,
    StashDBResponseError,
    StashDBTransientError,
)
from title_cache import TitleCache
from title_service import ApplyResult, PreviewRow, TitleService

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
LOG_FIELDS = ("timestamp", "old", "new", "oshash", "scene_id", "source")
_APPROVED_VALUES = frozenset({"y", "yes", "1", "true"})
_FORMULA_PREFIXES = ("=", "+", "-", "@")

DEFAULT_CSV = "preview.csv"
DEFAULT_LOG = "rename_log.csv"
DEFAULT_STUDIOS = "studios.json"
DEFAULT_CACHE = os.path.join("cache", "title_fixer.sqlite3")


def _guard_formula(value: str) -> str:
    """Экранирует значение, которое Excel принял бы за формулу."""
    if value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _unguard_formula(value: str) -> str:
    """Снимает апостроф, добавленный `_guard_formula`."""
    if (len(value) >= 2 and value[0] == "'"
            and value[1:].startswith(_FORMULA_PREFIXES)):
        return value[1:]
    return value


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
                row.note,
                row.oshash,
                str(row.size),
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
                note=record.get("note") or "",
                oshash=record.get("oshash") or "",
                size=_parse_int(record.get("size")),
            ))
    return rows


def append_rename_log(
    path: str,
    rows: Sequence[PreviewRow],
    results: Sequence[ApplyResult],
    timestamp: str,
) -> None:
    """Дописывает в лог только фактически переименованные строки.

    Строка лога: `timestamp;old;new;oshash;scene_id;source`, где `new` —
    фактическое имя из `ApplyResult` (с суффиксом коллизии, если он был
    добавлен), а `oshash`/`scene_id`/`source` берутся из `PreviewRow`,
    найденной по `old_rel`. Заголовок пишется один раз; BOM добавляется
    только в новый файл, чтобы при дописывании он не попадал в середину.
    """
    renamed = [result for result in results if result.status == "renamed"]
    if not renamed:
        return

    by_old = {row.old_rel: row for row in rows}
    _ensure_parent(path)
    exists = os.path.isfile(path) and os.path.getsize(path) > 0
    mode = "a" if exists else "w"
    encoding = "utf-8" if exists else "utf-8-sig"
    with open(path, mode, encoding=encoding, newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        if not exists:
            writer.writerow(LOG_FIELDS)
        for result in renamed:
            row = by_old.get(result.old_rel)
            writer.writerow((
                timestamp,
                _guard_formula(result.old_rel),
                _guard_formula(result.new_rel),
                row.oshash if row is not None else "",
                row.scene_id if row is not None else "",
                row.source if row is not None else "",
            ))


def _timestamp() -> str:
    """Метка времени UTC в ISO 8601 для `rename_log.csv`."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_parser() -> argparse.ArgumentParser:
    """Аргументы `preview` и `apply`."""
    parser = argparse.ArgumentParser(
        prog="fix_video_titles.py",
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
            append_rename_log(args.log, rows, report.results, _timestamp())
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
