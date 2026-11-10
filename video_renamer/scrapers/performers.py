#!/usr/bin/env python3
"""Скрейп справочника имён исполнителей StashDB.

Выгружает `queryPerformers` постранично и пишет два файла:

- `<out>` — полная база записей для TUI и обновлений;
- `<index>` — компактный индекс «нормализованное имя или алиас →
  каноническое имя» для `core.performer_names`.

Без `--apply` команда работает как dry-run и ничего не пишет. Прогресс
сохраняется в raw JSONL (одна страница — одна строка) и checkpoint; после
обрыва выгрузка продолжается с последней целой страницы. Полный прогон
занимает 2,5–3,5 минуты для ~115 тысяч записей.

Сортировка только `NAME`: `CREATED_AT` даёт неустойчивые страницы и теряет
записи на границах. После выгрузки число уникальных id сверяется с `count`.

Коды возврата: `0` — успех, `1` — временный сбой сети/сервера, `2` —
фатальная ошибка (ключ, запрос, файлы, неполная выгрузка).
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from video_renamer.core.env_file import load_env_file
from video_renamer.core.performer_names import build_index
from video_renamer.core.stashdb_client import (
    StashDBAuthError,
    StashDBClient,
    StashDBError,
    StashDBResponseError,
    StashDBTransientError,
)

__all__ = [
    "DEFAULT_CHECKPOINT",
    "DEFAULT_DELAY",
    "DEFAULT_INDEX",
    "DEFAULT_OUT",
    "DEFAULT_PER_PAGE",
    "DEFAULT_RAW",
    "PERFORMERS_QUERY",
    "main",
]

SOURCE = "stashdb"
DEFAULT_OUT = "data/performers.json"
DEFAULT_INDEX = "data/performers_index.json"
DEFAULT_RAW = "cache/performers_raw.jsonl"
DEFAULT_CHECKPOINT = "cache/performers_checkpoint.json"
DEFAULT_PER_PAGE = 1000
DEFAULT_DELAY = 0.5

PERFORMERS_QUERY = """
query Performers($input: PerformerQueryInput!) {
  queryPerformers(input: $input) {
    count
    performers {
      id
      name
      aliases
      gender
      disambiguation
      scene_count
      deleted
      merged_into_id
      updated
    }
  }
}
""".strip()


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def _atomic_write_json(path: str, payload: Any, *, compact: bool = False) -> None:
    """Пишет JSON атомарно: временный файл рядом, затем `os.replace`."""
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + os.path.basename(target) + ".",
        suffix=".tmp",
        dir=parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            if compact:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            else:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def _read_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _backup(path: str) -> None:
    """Копирует существующий файл в `<path>.bak`."""
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")


def _remove_if_exists(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _append_raw_page(path: str, page: int, count: int, performers: list[dict]) -> None:
    """Дописывает одну страницу в raw JSONL."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        json.dump(
            {"page": page, "count": count, "performers": performers},
            handle,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_raw(path: str) -> tuple[dict[str, dict], dict[int, int]]:
    """Читает raw JSONL: записи по id и номера страниц с count."""
    records: dict[str, dict] = {}
    pages: dict[int, int] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(
                        f"warning: ignoring broken raw line {line_number}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                if not isinstance(payload, dict):
                    continue
                page = payload.get("page")
                count = payload.get("count")
                rows = payload.get("performers")
                if not isinstance(page, int) or not isinstance(count, int):
                    continue
                if not isinstance(rows, list):
                    continue
                pages[page] = count
                for row in rows:
                    if isinstance(row, dict) and isinstance(row.get("id"), str):
                        records.setdefault(row["id"], row)
    except FileNotFoundError:
        return records, pages
    return records, pages


def _contiguous_last_page(pages: Mapping[int, int]) -> int:
    """Последняя страница без пропусков, начиная с 1."""
    page = 0
    while page + 1 in pages:
        page += 1
    return page


def _load_checkpoint(path: str) -> dict | None:
    try:
        payload = _read_json(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read checkpoint {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"checkpoint {path} must be a JSON object")
    return payload


def _save_checkpoint(path: str, page: int, per_page: int, count: int, complete: bool) -> None:
    _atomic_write_json(
        path,
        {
            "source": SOURCE,
            "page": page,
            "per_page": per_page,
            "count": count,
            "complete": complete,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


def _normalize_record(row: Mapping[str, Any]) -> dict:
    """Оставляет известные поля и приводит типы к предсказуемым."""
    aliases = row.get("aliases") or []
    if not isinstance(aliases, list):
        aliases = []
    scene_count = row.get("scene_count")
    return {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or "").strip(),
        "aliases": [str(alias) for alias in aliases if str(alias).strip()],
        "gender": row.get("gender"),
        "disambiguation": row.get("disambiguation"),
        "scene_count": scene_count if isinstance(scene_count, int) else 0,
        "updated": row.get("updated"),
    }


def _build_payload(records: Iterable[Mapping[str, Any]], count: int) -> dict:
    performers = [
        _normalize_record(row)
        for row in records
        if not row.get("deleted") and not row.get("merged_into_id") and row.get("name")
    ]
    performers.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    return {
        "source": SOURCE,
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": count,
        "performers": performers,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scrape-performers",
        description="Scrape performer names from StashDB into a JSON database and index.",
    )
    parser.add_argument(
        "--out", default=DEFAULT_OUT, help=f"database JSON (default: {DEFAULT_OUT})"
    )
    parser.add_argument(
        "--index", default=DEFAULT_INDEX, help=f"matching index JSON (default: {DEFAULT_INDEX})"
    )
    parser.add_argument(
        "--raw", default=DEFAULT_RAW, help=f"raw pages JSONL (default: {DEFAULT_RAW})"
    )
    parser.add_argument(
        "--checkpoint",
        default=DEFAULT_CHECKPOINT,
        help=f"checkpoint JSON (default: {DEFAULT_CHECKPOINT})",
    )
    parser.add_argument(
        "--per-page",
        type=_positive_int,
        default=DEFAULT_PER_PAGE,
        help=f"performers per page (default: {DEFAULT_PER_PAGE})",
    )
    parser.add_argument(
        "--delay",
        type=_non_negative_float,
        default=DEFAULT_DELAY,
        help=f"seconds before each request (default: {DEFAULT_DELAY})",
    )
    parser.add_argument(
        "--max-pages",
        type=_positive_int,
        default=None,
        help="stop after N pages (keeps checkpoint, writes no database)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="write raw, checkpoint, database and index"
    )
    parser.add_argument("--reset", action="store_true", help="start over, ignoring checkpoint/raw")
    parser.add_argument(
        "--allow-gaps",
        action="store_true",
        help="accept unique id count different from the server count",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа CLI; возвращает код вместо `sys.exit`."""
    args = _build_parser().parse_args(argv)

    load_env_file()
    api_key = os.environ.get("STASHDB_API_KEY", "").strip()
    if not api_key:
        print("error: STASHDB_API_KEY is not set (checked environment and .env)", file=sys.stderr)
        return 2

    if args.reset and args.apply:
        try:
            _remove_if_exists(args.raw)
            _remove_if_exists(args.checkpoint)
        except OSError as exc:
            print(f"error: cannot reset raw/checkpoint files: {exc}", file=sys.stderr)
            return 2

    if not args.reset:
        try:
            checkpoint = _load_checkpoint(args.checkpoint)
        except (ValueError, TypeError) as exc:
            print(f"error: {exc}; use --reset to start over", file=sys.stderr)
            return 2
        if checkpoint is not None and checkpoint.get("per_page") != args.per_page:
            print(
                f"error: checkpoint uses --per-page {checkpoint.get('per_page')}, "
                f"but --per-page {args.per_page} was requested; pass --reset",
                file=sys.stderr,
            )
            return 2
        try:
            records, pages = _load_raw(args.raw)
        except OSError as exc:
            print(
                f"error: cannot read raw {args.raw}: {exc}; use --reset",
                file=sys.stderr,
            )
            return 2
        last_page = _contiguous_last_page(pages)
        checkpoint_page = int(checkpoint.get("page") or 0) if checkpoint else 0
        if checkpoint is not None and checkpoint.get("complete"):
            # Полная база уже собрана: обычный запуск — это обновление, а не
            # продолжение. Иначе скрейп навсегда остался бы на последней странице.
            print("previous scrape is complete; starting a fresh refresh")
            records, pages = {}, {}
            last_page = checkpoint_page = 0
            if args.apply:
                _remove_if_exists(args.raw)
                _remove_if_exists(args.checkpoint)
        if checkpoint is not None and last_page < checkpoint_page:
            print(
                f"error: checkpoint expects page {checkpoint.get('page')}, "
                f"but raw {args.raw} ends at page {last_page}; pass --reset",
                file=sys.stderr,
            )
            return 2
        start_page = max(last_page, checkpoint_page) + 1
    else:
        start_page = 1
        records, pages = {}, {}

    if args.max_pages is not None and start_page > args.max_pages:
        print(f"nothing to do: next page {start_page} > --max-pages {args.max_pages}")
        return 0

    client = StashDBClient(api_key, delay=args.delay)
    count = 0
    complete = False
    page = start_page
    fetched = 0

    try:
        while True:
            if args.max_pages is not None and fetched >= args.max_pages:
                print(f"stopped after {fetched} page(s); checkpoint keeps the progress")
                break
            data = client.execute(
                PERFORMERS_QUERY,
                {
                    "input": {
                        "page": page,
                        "per_page": args.per_page,
                        "sort": "NAME",
                        "direction": "ASC",
                    }
                },
            )
            payload = data.get("queryPerformers")
            if not isinstance(payload, dict):
                raise StashDBResponseError("StashDB response has no `queryPerformers`")
            page_count = payload.get("count")
            rows = payload.get("performers")
            if not isinstance(page_count, int) or isinstance(page_count, bool):
                raise StashDBResponseError("StashDB response has no performer count")
            if not isinstance(rows, list):
                raise StashDBResponseError("StashDB response has no performer list")
            count = page_count
            if not rows:
                if len(records) < count and not args.allow_gaps:
                    print(
                        f"error: page {page} is empty, but only {len(records)}/{count} "
                        f"performer(s) collected; pass --allow-gaps to accept",
                        file=sys.stderr,
                    )
                    return 2
                complete = True
                break
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    records.setdefault(row["id"], row)
            if args.apply:
                _append_raw_page(
                    args.raw, page, count, [row for row in rows if isinstance(row, dict)]
                )
                _save_checkpoint(
                    args.checkpoint,
                    page,
                    args.per_page,
                    count,
                    complete=len(records) >= count,
                )
            print(f"page {page}: {len(rows)} performer(s), {len(records)}/{count} collected")
            fetched += 1
            if len(records) >= count:
                complete = True
                break
            page += 1
    except StashDBAuthError as exc:
        print(
            f"error: StashDB rejected the request: {exc}; проверь STASHDB_API_KEY", file=sys.stderr
        )
        return 2
    except StashDBTransientError as exc:
        print(f"error: StashDB request failed: {exc}", file=sys.stderr)
        return 1
    except StashDBError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot save scrape progress: {exc}", file=sys.stderr)
        return 2

    if not complete:
        print(
            f"dry-run: incomplete scrape, {len(records)}/{count} performer(s), "
            f"next page {page}; database not written"
        )
        return 0

    if not args.allow_gaps and len(records) != count:
        print(
            f"error: collected {len(records)} unique performer(s), server reports {count}; "
            f"pass --allow-gaps to accept or --reset and retry",
            file=sys.stderr,
        )
        return 2

    payload = _build_payload(records.values(), count)
    index = build_index(payload["performers"])

    if not args.apply:
        print(
            f"dry-run: {len(payload['performers'])} performer(s), "
            f"{len(index)} index key(s), {fetched} page(s) fetched"
        )
        return 0

    try:
        _backup(args.out)
        _backup(args.index)
        _atomic_write_json(args.out, payload, compact=True)
        _atomic_write_json(args.index, index, compact=True)
    except OSError as exc:
        print(f"error: cannot write {args.out}: {exc}", file=sys.stderr)
        return 2

    print(
        f"applied: {len(payload['performers'])} performer(s), "
        f"{len(index)} index key(s); wrote {args.out} and {args.index}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
