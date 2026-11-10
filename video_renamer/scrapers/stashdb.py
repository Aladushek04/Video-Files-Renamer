#!/usr/bin/env python3
"""Скрапер студий stash-box (StashDB, FansDB, ThePornDB) с возобновлением.

CLI выгружает все студии постранично, сохраняет сырой прогресс и checkpoint
после каждой страницы (только с `--apply`), а в конце добавляет новые имена в
выходной JSON, не перезаписывая ручные значения. Коллизии (одинаковый
нормализованный ключ, разные написания) пишутся в
`<out>_collisions.json`.

Без `--apply` команда работает как dry-run и не пишет ни одного файла.

Коды возврата:

- `0` — успех (включая намеренную остановку по `--max-pages`);
- `1` — временный сбой источника/сети (прогресс предыдущих страниц сохранён);
- `2` — фатальная ошибка: нет ключа, ошибка авторизации/запроса, разрыв
  пагинации, несовпадение `per_page` с checkpoint или ошибка файлов.
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from video_renamer.core.clean_ph_names import normalize_studio_key
from video_renamer.core.env_file import load_env_file
from video_renamer.core.stashdb_client import (
    StashDBAuthError,
    StashDBClient,
    StashDBError,
    StashDBTransientError,
    Studio,
)
from video_renamer.core.studio_filters import filter_studio_names

__all__ = [
    "DEFAULT_CHECKPOINT",
    "DEFAULT_DELAY",
    "DEFAULT_OUT",
    "DEFAULT_PER_PAGE",
    "DEFAULT_RAW",
    "FANSDB_SOURCE",
    "STASHDB_SOURCE",
    "THEPORNDB_SOURCE",
    "Checkpoint",
    "ScrapeReport",
    "ScraperGapError",
    "StudioSource",
    "fetch_studios_page",
    "load_checkpoint",
    "load_raw",
    "main",
    "merge_studios",
    "normalize_key",
    "run_scrape",
    "save_checkpoint",
    "save_raw",
]

DEFAULT_OUT = "data/studios.json"
DEFAULT_CHECKPOINT = "data/studios_checkpoint.json"
DEFAULT_RAW = "data/studios_raw.json"
DEFAULT_PER_PAGE = 100
DEFAULT_DELAY = 1.0


@dataclass(frozen=True)
class StudioSource:
    """Описание stash-box источника студий."""

    name: str
    endpoint: str
    api_key_env: str
    script: str
    default_out: str
    default_checkpoint: str
    default_raw: str


STASHDB_SOURCE = StudioSource(
    name="StashDB",
    endpoint="https://stashdb.org/graphql",
    api_key_env="STASHDB_API_KEY",
    script="python -m video_renamer.scrapers.stashdb",
    default_out=DEFAULT_OUT,
    default_checkpoint=DEFAULT_CHECKPOINT,
    default_raw=DEFAULT_RAW,
)

FANSDB_SOURCE = StudioSource(
    name="FansDB",
    endpoint="https://fansdb.cc/graphql",
    api_key_env="FANSDB_API_KEY",
    script="python -m video_renamer.scrapers.fansdb",
    default_out="data/studios_fansdb.json",
    default_checkpoint="data/studios_fansdb_checkpoint.json",
    default_raw="data/studios_fansdb_raw.json",
)

THEPORNDB_SOURCE = StudioSource(
    name="ThePornDB",
    endpoint="https://theporndb.net/graphql",
    api_key_env="THE_PORNDB_API_KEY",
    script="python -m video_renamer.scrapers.theporndb",
    default_out="data/studios_theporndb.json",
    default_checkpoint="data/studios_theporndb_checkpoint.json",
    default_raw="data/studios_theporndb_raw.json",
)

# Ключ для группировки коллизий: дополнительно к `normalize_key` убираем
# пунктуацию. Тест плана считает `Brazzers!` коллизией с `Brazzers`
# (`collisions["brazzers"]`), хотя записываемые ключи остаются ровно такими,
# как у `clean_ph_names.normalize_studio_key`.
_COLLISION_JUNK = re.compile(r"[\W_]+")


class ScraperGapError(RuntimeError):
    """StashDB вернул пустую страницу, хотя `count` ещё не достигнут."""


def normalize_key(name: str) -> str:
    """Ключ студии — тот же, что у `clean_ph_names`."""
    return normalize_studio_key(name)


def _collision_key(name: str) -> str:
    """Ключ сравнения для коллизий: `normalize_key` без пунктуации."""
    return _COLLISION_JUNK.sub("", normalize_key(name or ""))


def merge_studios(
    existing: dict[str, str],
    names: Sequence[str],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Добавляет новые имена, сохраняя существующие значения.

    Существующие ключи и значения не перезаписываются. Для каждого имени
    ключ нормализуется; если ключ уже занят другим написанием, имя попадает
    в отчёт коллизий (`collisions[ключ]`), а не в результат. Пустые имена
    пропускаются. Возвращает `(merged, collisions)`.

    Коллизии группируются по `_collision_key` (без пунктуации), поэтому
    `Brazzers!` попадает в коллизию с `Brazzers`; новые ключи при этом
    пишутся через `normalize_key`, чтобы совпадать с `clean_ph_names`.
    """
    merged = dict(existing)
    collision_to_stored: dict[str, str] = {}
    for stored_key in merged:
        collision_to_stored.setdefault(_collision_key(stored_key), stored_key)

    collisions: dict[str, list[str]] = {}
    for name in names:
        collision_key = _collision_key(name)
        if not collision_key:
            continue
        stored_key = collision_to_stored.get(collision_key)
        if stored_key is not None:
            if merged[stored_key] == name:
                continue
            bucket = collisions.setdefault(collision_key, [])
            if name not in bucket:
                bucket.append(name)
            continue
        key = normalize_key(name)
        if not key:
            continue
        merged[key] = name
        collision_to_stored[collision_key] = key

    return merged, collisions


def _atomic_write_json(path: str, payload: Any) -> None:
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
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json(path: str, what: str) -> Any:
    """Читает JSON; битый файл превращает в `ValueError` с контекстом."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{what} is not valid JSON: {exc}") from exc


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Checkpoint:
    """Последняя успешно сохранённая страница скрапера."""

    page: int
    per_page: int
    count: int
    complete: bool


def load_checkpoint(path: str) -> Checkpoint | None:
    """Читает checkpoint; отсутствие файла даёт `None`.

    Битый или неполный checkpoint — `ValueError`: продолжать с догадкой
    опаснее, чем попросить `--reset`.
    """
    try:
        payload = _read_json(path, "checkpoint")
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict):
        raise ValueError("checkpoint must be a JSON object")

    page = payload.get("page")
    per_page = payload.get("per_page")
    count = payload.get("count")
    complete = payload.get("complete", False)
    if not _is_int(page) or not _is_int(per_page) or not _is_int(count):
        raise ValueError("checkpoint must contain integer page/per_page/count")
    if not isinstance(complete, bool):
        raise ValueError("checkpoint `complete` must be a boolean")
    if page < 1 or per_page < 1 or count < 0:
        raise ValueError("checkpoint has out-of-range values")
    return Checkpoint(page, per_page, count, complete)


def save_checkpoint(path: str, checkpoint: Checkpoint) -> None:
    """Сохраняет checkpoint атомарно."""
    _atomic_write_json(path, asdict(checkpoint))


def load_raw(path: str) -> list[Studio]:
    """Читает сырой список студий; отсутствие файла даёт пустой список."""
    try:
        payload = _read_json(path, "raw studios")
    except FileNotFoundError:
        return []
    if not isinstance(payload, list):
        raise ValueError("raw studios must be a JSON array")
    studios: list[Studio] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("raw studios must contain JSON objects")
        studios.append(Studio.from_dict(item))
    return studios


def save_raw(path: str, studios: Sequence[Studio]) -> None:
    """Сохраняет сырой список студий атомарно."""
    _atomic_write_json(path, [studio.to_dict() for studio in studios])


def _unique_studios(studios: Sequence[Studio]) -> list[Studio]:
    """Убирает повторы по `Studio.id`, сохраняя порядок первого появления."""
    seen: set[str] = set()
    unique: list[Studio] = []
    for studio in studios:
        if studio.id in seen:
            continue
        seen.add(studio.id)
        unique.append(studio)
    return unique


@dataclass(frozen=True)
class ScrapeReport:
    """Итог `run_scrape`: собранные студии и точка продолжения."""

    studios: tuple[Studio, ...]
    next_page: int
    complete: bool
    pages_fetched: int


def fetch_studios_page(
    client: StashDBClient,
    page: int,
    per_page: int,
) -> tuple[list[Studio], int]:
    """Одна страница студий через клиент StashDB."""
    return client.query_studios(page, per_page)


def run_scrape(
    client: StashDBClient,
    *,
    start_page: int,
    per_page: int,
    max_pages: int | None,
    initial: Sequence[Studio] = (),
    fetch: Callable[[StashDBClient, int, int], tuple[list[Studio], int]] = fetch_studios_page,
    on_page: Callable[[int, list[Studio], int], None] | None = None,
    allow_empty_tail: bool = False,
) -> ScrapeReport:
    """Листает студии с `start_page`, дедуплицируя по `Studio.id`.

    `count` берётся из каждого ответа. Пустая страница при
    `len(studios) < count` — `ScraperGapError`, кроме последней страницы при
    `allow_empty_tail=True`: источник может сообщить count на несколько
    записей больше, чем реально отдаёт. `max_pages` ограничивает число
    сетевых страниц и оставляет `complete=False`. `on_page(page,
    page_studios, count)` вызывается после каждой успешной непустой проверки
    страницы (включая терминальную пустую страницу при `count == 0`).
    """
    if start_page < 1:
        raise ValueError("start_page must be at least 1")
    if per_page < 1:
        raise ValueError("per_page must be at least 1")
    if max_pages is not None and max_pages < 0:
        raise ValueError("max_pages must not be negative")

    studios = _unique_studios(initial)
    seen = {studio.id for studio in studios}
    page = start_page
    pages_fetched = 0
    complete = False

    while max_pages is None or pages_fetched < max_pages:
        page_studios, count = fetch(client, page, per_page)
        page_studios = list(page_studios)
        pages_fetched += 1

        if not page_studios and len(studios) < count:
            if allow_empty_tail and studios and page * per_page >= count:
                count = len(studios)
                if on_page is not None:
                    on_page(page, [], count)
                complete = True
                break
            raise ScraperGapError(
                f"page {page} is empty, but only {len(studios)} of {count} studios were collected"
            )

        for studio in page_studios:
            if studio.id not in seen:
                seen.add(studio.id)
                studios.append(studio)

        if on_page is not None:
            on_page(page, page_studios, count)

        if len(studios) >= count:
            complete = True
            break
        page += 1

    return ScrapeReport(
        studios=tuple(studios),
        next_page=start_page + pages_fetched,
        complete=complete,
        pages_fetched=pages_fetched,
    )


def _positive_int(value: str) -> int:
    """Тип argparse: целое >= 1."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_float(value: str) -> float:
    """Тип argparse: число >= 0."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def _build_parser(
    source: StudioSource = STASHDB_SOURCE,
) -> argparse.ArgumentParser:
    """Аргументы скрапера для выбранного источника."""
    parser = argparse.ArgumentParser(
        prog=source.script,
        description=(f"Scrape {source.name} studios into {source.default_out} with resume."),
    )
    parser.add_argument(
        "--out",
        default=source.default_out,
        help=f"studios JSON to merge into (default: {source.default_out})",
    )
    parser.add_argument(
        "--checkpoint",
        default=source.default_checkpoint,
        help=f"checkpoint JSON (default: {source.default_checkpoint})",
    )
    parser.add_argument(
        "--raw",
        default=source.default_raw,
        help=f"raw scraped studios (default: {source.default_raw})",
    )
    parser.add_argument(
        "--per-page",
        type=_positive_int,
        default=DEFAULT_PER_PAGE,
        help="studios per page (default: 100)",
    )
    parser.add_argument(
        "--delay",
        type=_non_negative_float,
        default=DEFAULT_DELAY,
        help="seconds before each request (default: 1.0)",
    )
    parser.add_argument(
        "--max-pages",
        type=_positive_int,
        default=None,
        help="stop after N pages (incomplete checkpoint)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="write progress, studios.json and collision report"
    )
    parser.add_argument(
        "--reset", action="store_true", help="ignore and (with --apply) delete checkpoint/raw"
    )
    parser.add_argument(
        "--no-filter", action="store_true", help="keep generated/placeholder studio names too"
    )
    parser.add_argument(
        "--allow-empty-tail",
        action="store_true",
        help="accept an empty last page when the reported count is a few records too high",
    )
    return parser


def _remove_if_exists(path: str) -> None:
    """Удаляет файл, если он есть; отсутствие не ошибка."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _load_existing_studios(path: str) -> dict[str, str]:
    """Читает существующий studios.json, не меняя ключи и значения."""
    try:
        payload = _read_json(path, "studios")
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("studios JSON must be an object")
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("studios JSON must map strings to strings")
    return payload


def _collisions_path(out: str) -> str:
    """`studios.json` → `studios_collisions.json` рядом с выходным файлом."""
    stem, extension = os.path.splitext(out)
    return f"{stem}_collisions{extension or '.json'}"


def _backup_studios(path: str) -> None:
    """Копирует текущий `studios.json` в `<path>.bak` перед записью."""
    shutil.copy2(path, path + ".bak")


def main(
    argv: Sequence[str] | None = None,
    *,
    source: StudioSource = STASHDB_SOURCE,
) -> int:
    """Точка входа CLI; возвращает код вместо `sys.exit`."""
    parser = _build_parser(source)
    args = parser.parse_args(argv)

    load_env_file()
    api_key = os.environ.get(source.api_key_env, "").strip()
    if not api_key:
        print(
            f"error: {source.api_key_env} is not set (checked environment and .env)",
            file=sys.stderr,
        )
        return 2

    try:
        checkpoint = None if args.reset else load_checkpoint(args.checkpoint)
    except (OSError, ValueError) as exc:
        print(
            f"error: cannot read checkpoint {args.checkpoint}: {exc}; use --reset to start over",
            file=sys.stderr,
        )
        return 2

    if checkpoint is not None and checkpoint.per_page != args.per_page:
        print(
            f"error: checkpoint {args.checkpoint} uses --per-page "
            f"{checkpoint.per_page}, but --per-page {args.per_page} was "
            f"requested; pass --reset to start over with a new page size",
            file=sys.stderr,
        )
        return 2

    if args.reset and args.apply:
        try:
            _remove_if_exists(args.checkpoint)
            _remove_if_exists(args.raw)
        except OSError as exc:
            print(
                f"error: cannot reset checkpoint/raw files: {exc}",
                file=sys.stderr,
            )
            return 2

    initial: list[Studio] = []
    if not args.reset:
        try:
            initial = load_raw(args.raw)
        except (OSError, ValueError) as exc:
            print(
                f"error: cannot read raw studios {args.raw}: {exc}; use --reset to start over",
                file=sys.stderr,
            )
            return 2

    if (
        checkpoint is not None
        and not args.reset
        and not initial
        and (checkpoint.count > 0 or (not checkpoint.complete and checkpoint.page >= 1))
    ):
        print(
            f"error: checkpoint {args.checkpoint} expects "
            f"{checkpoint.count} studio(s), but raw studios {args.raw} "
            f"is missing or empty; resume would lose earlier pages; "
            f"pass --reset to start from page 1",
            file=sys.stderr,
        )
        return 2

    if checkpoint is not None and checkpoint.complete and not args.reset:
        studios = tuple(_unique_studios(initial))
        report = ScrapeReport(studios, checkpoint.page + 1, True, 0)
        print(f"checkpoint is complete; using {len(studios)} saved studio(s)")
    else:
        start_page = 1 if (args.reset or checkpoint is None) else checkpoint.page + 1
        accumulated = _unique_studios(initial)
        seen = {studio.id for studio in accumulated}

        def on_page(
            page: int,
            page_studios: list[Studio],
            count: int,
        ) -> None:
            for studio in page_studios:
                if studio.id not in seen:
                    seen.add(studio.id)
                    accumulated.append(studio)
            if args.apply:
                save_raw(args.raw, accumulated)
                save_checkpoint(
                    args.checkpoint,
                    Checkpoint(
                        page=page,
                        per_page=args.per_page,
                        count=count,
                        complete=len(accumulated) >= count,
                    ),
                )
            print(
                f"page {page}: {len(page_studios)} studio(s), {len(accumulated)}/{count} collected"
            )

        client = StashDBClient(api_key, endpoint=source.endpoint, delay=args.delay)
        try:
            report = run_scrape(
                client,
                start_page=start_page,
                per_page=args.per_page,
                max_pages=args.max_pages,
                initial=initial,
                on_page=on_page,
                allow_empty_tail=args.allow_empty_tail,
            )
        except StashDBAuthError as exc:
            print(
                f"error: {source.name} rejected the request: {exc}; проверь {source.api_key_env}",
                file=sys.stderr,
            )
            return 2
        except StashDBTransientError as exc:
            print(
                f"error: {source.name} request failed: {exc}",
                file=sys.stderr,
            )
            return 1
        except (StashDBError, ScraperGapError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"error: cannot save scrape progress: {exc}", file=sys.stderr)
            return 2

    all_names = [studio.name for studio in report.studios]
    if args.no_filter:
        names_to_merge = all_names
        filtered_count = 0
    else:
        names_to_merge, filtered_count = filter_studio_names(all_names)

    if args.apply:
        try:
            existing = _load_existing_studios(args.out)
        except (OSError, ValueError) as exc:
            print(
                f"error: cannot read studios file {args.out}: {exc}",
                file=sys.stderr,
            )
            return 2

        merged, collisions = merge_studios(existing, names_to_merge)
        try:
            if os.path.exists(args.out):
                _backup_studios(args.out)
            _atomic_write_json(args.out, merged)
            _atomic_write_json(_collisions_path(args.out), collisions)
        except OSError as exc:
            print(f"error: cannot write {args.out}: {exc}", file=sys.stderr)
            return 2

        print(
            f"applied: {len(report.studios)} scraped, "
            f"{len(names_to_merge)} kept, {filtered_count} filtered, "
            f"{len(merged) - len(existing)} new key(s), "
            f"{sum(len(v) for v in collisions.values())} collision(s)"
        )
    else:
        print(
            f"dry-run: {len(report.studios)} studio(s), "
            f"{len(names_to_merge)} kept, {filtered_count} filtered, "
            f"{report.pages_fetched} page(s), "
            f"next page {report.next_page}, complete={report.complete}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
