#!/usr/bin/env python3
"""ThePornDB: обогащение справочника студий через `findStudio` по именам.

ThePornDB не отдаёт `queryStudios`, поэтому перечислить все студии одним
списком нельзя. Зато у него есть `findStudio(name:)`. Скрипт берёт имена из
seed-файлов (по умолчанию `studios.json`), спрашивает ThePornDB пакетами и
добавляет найденные канонические имена, родителей и дочерние студии.

Без `--apply` — dry-run: сеть читается, файлы не пишутся.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from video_renamer.core.env_file import load_env_file
from video_renamer.core.stashdb_client import (
    StashDBAuthError,
    StashDBClient,
    StashDBError,
    StashDBTransientError,
    Studio,
)
from video_renamer.core.studio_filters import filter_studio_names
from video_renamer.scrapers.stashdb import (
    THEPORNDB_SOURCE,
    _atomic_write_json,
    _backup_studios,
    _collisions_path,
    _load_existing_studios,
    _remove_if_exists,
    load_raw,
    merge_studios,
    save_raw,
)

__all__ = [
    "LookupCheckpoint",
    "LookupReport",
    "build_lookup_query",
    "load_lookup_checkpoint",
    "load_seed_names",
    "main",
    "parse_lookup_response",
    "run_lookup",
    "save_lookup_checkpoint",
]

DEFAULT_BATCH_SIZE = 50
DEFAULT_DELAY = 1.0


@dataclass(frozen=True)
class LookupCheckpoint:
    batch: int
    batch_size: int
    total: int
    complete: bool


@dataclass(frozen=True)
class LookupReport:
    studios: tuple[Studio, ...]
    next_batch: int
    complete: bool
    batches_fetched: int


def load_seed_names(paths: Sequence[str]) -> list[str]:
    """Читает имена из JSON-словарей/списков, сохраняя порядок и уникальность."""
    names: list[str] = []
    seen: set[str] = set()
    for path in paths:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        if isinstance(payload, dict):
            values: Any = payload.values()
        elif isinstance(payload, list):
            values = payload
        else:
            raise ValueError(f"{path}: expected a JSON object or array")
        for value in values:
            if isinstance(value, str):
                name = value
            elif isinstance(value, dict) and isinstance(value.get("name"), str):
                name = value["name"]
            else:
                continue
            name = name.strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def build_lookup_query(names: Sequence[str]) -> tuple[str, dict[str, str]]:
    """Строит один GraphQL-запрос с алиасами для пакета имён."""
    variables: dict[str, str] = {}
    fields: list[str] = []
    for index, name in enumerate(names):
        variable = f"n{index}"
        variables[variable] = name
        fields.append(
            f" s{index}: findStudio(name: ${variable}) {{ "
            "id name parent { id name } child_studios { id name } }"
        )
    definitions = ", ".join(f"${name}: String!" for name in variables)
    return f"query({definitions}) {{" + "".join(fields) + " }", variables


def parse_lookup_response(data: Any, count: int) -> list[Studio]:
    """Разбирает ответ пакета, добавляя родителя и дочерние студии."""
    if not isinstance(data, dict):
        raise ValueError("ThePornDB response is not a JSON object")
    studios: list[Studio] = []
    seen: set[str] = set()

    def add(node: Any) -> None:
        if not isinstance(node, dict):
            return
        name = str(node.get("name") or "").strip()
        if not name:
            return
        studio_id = str(node.get("id") or name)
        key = studio_id or name
        if key in seen:
            return
        seen.add(key)
        studios.append(Studio(studio_id, name))

    for index in range(count):
        node = data.get(f"s{index}")
        if not isinstance(node, dict):
            continue
        add(node)
        add(node.get("parent"))
        children = node.get("child_studios")
        if isinstance(children, list):
            for child in children:
                add(child)
    return studios


def run_lookup(
    client: Any,
    names: Sequence[str],
    *,
    start_batch: int = 0,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int | None = None,
    initial: Sequence[Studio] = (),
    on_batch: Callable[[int, list[Studio], list[Studio], bool], None] | None = None,
) -> LookupReport:
    """Ищет имена пакетами; возвращает накопленные студии и прогресс."""
    unique_names = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    total = len(unique_names)
    accumulated = list(initial)
    seen = {studio.id or studio.name for studio in accumulated}
    batch = start_batch
    fetched = 0

    while batch * batch_size < total:
        if max_batches is not None and fetched >= max_batches:
            break
        chunk = unique_names[batch * batch_size:(batch + 1) * batch_size]
        if not chunk:
            break
        query, variables = build_lookup_query(chunk)
        page = parse_lookup_response(client.execute(query, variables), len(chunk))
        for studio in page:
            key = studio.id or studio.name
            if key not in seen:
                seen.add(key)
                accumulated.append(studio)
        fetched += 1
        batch += 1
        complete = batch * batch_size >= total
        if on_batch is not None:
            on_batch(batch, page, accumulated, complete)

    return LookupReport(
        studios=tuple(accumulated),
        next_batch=batch,
        complete=batch * batch_size >= total,
        batches_fetched=fetched,
    )


def _read_checkpoint(path: str) -> LookupCheckpoint | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict):
        raise ValueError("checkpoint must be a JSON object")
    batch = payload.get("batch")
    batch_size = payload.get("batch_size")
    total = payload.get("total")
    complete = payload.get("complete")
    if (
        not isinstance(batch, int)
        or isinstance(batch, bool)
        or not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or not isinstance(total, int)
        or isinstance(total, bool)
        or not isinstance(complete, bool)
    ):
        raise ValueError("checkpoint has invalid fields")
    if batch < 0 or batch_size < 1 or total < 0:
        raise ValueError("checkpoint has out-of-range values")
    return LookupCheckpoint(batch, batch_size, total, complete)


def load_lookup_checkpoint(path: str) -> LookupCheckpoint | None:
    """Публичная обёртка чтения checkpoint."""
    return _read_checkpoint(path)


def save_lookup_checkpoint(path: str, checkpoint: LookupCheckpoint) -> None:
    """Атомарно сохраняет checkpoint."""
    _atomic_write_json(path, asdict(checkpoint))


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m video_renamer.scrapers.theporndb",
        description=(
            "Enrich known studio names via ThePornDB findStudio "
            "(ThePornDB has no queryStudios)."
        ),
    )
    parser.add_argument(
        "--seed", action="append", default=None,
        help="JSON file with studio names (dict values or list); repeatable "
             "(default: studios.json)",
    )
    parser.add_argument(
        "--out", default=THEPORNDB_SOURCE.default_out,
        help=f"studios JSON to merge into "
             f"(default: {THEPORNDB_SOURCE.default_out})",
    )
    parser.add_argument(
        "--checkpoint", default=THEPORNDB_SOURCE.default_checkpoint,
        help=f"checkpoint JSON "
             f"(default: {THEPORNDB_SOURCE.default_checkpoint})",
    )
    parser.add_argument(
        "--raw", default=THEPORNDB_SOURCE.default_raw,
        help=f"raw found studios "
             f"(default: {THEPORNDB_SOURCE.default_raw})",
    )
    parser.add_argument("--batch-size", type=_positive_int,
                        default=DEFAULT_BATCH_SIZE,
                        help=f"names per request (default: {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--delay", type=_non_negative_float,
                        default=DEFAULT_DELAY,
                        help=f"seconds before each request (default: {DEFAULT_DELAY})")
    parser.add_argument("--max-batches", type=_positive_int, default=None,
                        help="stop after N batches (incomplete checkpoint)")
    parser.add_argument("--apply", action="store_true",
                        help="write progress, studios JSON and collision report")
    parser.add_argument("--reset", action="store_true",
                        help="ignore and (with --apply) delete checkpoint/raw")
    parser.add_argument("--no-filter", action="store_true",
                        help="keep generated/placeholder studio names too")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    load_env_file()
    api_key = os.environ.get(THEPORNDB_SOURCE.api_key_env, "").strip()
    if not api_key:
        print(
            f"error: {THEPORNDB_SOURCE.api_key_env} is not set "
            "(checked environment and .env)",
            file=sys.stderr,
        )
        return 2

    seed_paths = args.seed or ["studios.json"]
    try:
        names = load_seed_names(seed_paths)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read seed names: {exc}", file=sys.stderr)
        return 2
    if not names:
        print("error: seed files contain no studio names", file=sys.stderr)
        return 2
    total = len(names)

    try:
        checkpoint = None if args.reset else load_lookup_checkpoint(args.checkpoint)
    except (OSError, ValueError) as exc:
        print(
            f"error: cannot read checkpoint {args.checkpoint}: {exc}; "
            f"use --reset to start over",
            file=sys.stderr,
        )
        return 2

    if checkpoint is not None and (
        checkpoint.batch_size != args.batch_size or checkpoint.total != total
    ):
        print(
            f"error: checkpoint {args.checkpoint} was created with "
            f"batch_size={checkpoint.batch_size}, total={checkpoint.total}, "
            f"but current run has batch_size={args.batch_size}, total={total}; "
            f"pass --reset to start over",
            file=sys.stderr,
        )
        return 2

    if args.reset and args.apply:
        try:
            _remove_if_exists(args.checkpoint)
            _remove_if_exists(args.raw)
        except OSError as exc:
            print(f"error: cannot reset checkpoint/raw: {exc}", file=sys.stderr)
            return 2

    initial: list[Studio] = []
    if not args.reset:
        try:
            initial = load_raw(args.raw)
        except (OSError, ValueError) as exc:
            print(
                f"error: cannot read raw studios {args.raw}: {exc}; "
                f"use --reset to start over",
                file=sys.stderr,
            )
            return 2

    if checkpoint is not None and checkpoint.complete and not args.reset:
        if not initial:
            print(
                f"error: checkpoint is complete, but raw studios {args.raw} "
                f"is missing or empty; pass --reset",
                file=sys.stderr,
            )
            return 2
        report = LookupReport(tuple(initial), checkpoint.batch, True, 0)
        print(f"checkpoint is complete; using {len(initial)} saved studio(s)")
    else:
        start_batch = 0 if (args.reset or checkpoint is None) else checkpoint.batch
        if checkpoint is not None and not initial and start_batch > 0:
            print(
                f"error: checkpoint expects earlier batches, but raw studios "
                f"{args.raw} is missing or empty; pass --reset",
                file=sys.stderr,
            )
            return 2

        def on_batch(
            batch: int,
            page: list[Studio],
            accumulated: list[Studio],
            complete: bool,
        ) -> None:
            if args.apply:
                save_raw(args.raw, accumulated)
                save_lookup_checkpoint(
                    args.checkpoint,
                    LookupCheckpoint(batch, args.batch_size, total, complete),
                )
            total_batches = math.ceil(total / args.batch_size)
            print(
                f"batch {batch}/{total_batches}: {len(page)} studio(s), "
                f"{len(accumulated)} collected"
            )

        client = StashDBClient(
            api_key, endpoint=THEPORNDB_SOURCE.endpoint, delay=args.delay
        )
        try:
            report = run_lookup(
                client,
                names,
                start_batch=start_batch,
                batch_size=args.batch_size,
                max_batches=args.max_batches,
                initial=initial,
                on_batch=on_batch,
            )
        except StashDBAuthError as exc:
            print(
                f"error: {THEPORNDB_SOURCE.name} rejected the request: {exc}; "
                f"проверь {THEPORNDB_SOURCE.api_key_env}",
                file=sys.stderr,
            )
            return 2
        except StashDBTransientError as exc:
            print(
                f"error: {THEPORNDB_SOURCE.name} request failed: {exc}",
                file=sys.stderr,
            )
            return 1
        except (StashDBError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
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
            f"applied: {len(report.studios)} found, "
            f"{len(names_to_merge)} kept, {filtered_count} filtered, "
            f"{len(merged) - len(existing)} new key(s), "
            f"{sum(len(v) for v in collisions.values())} collision(s)"
        )
    else:
        print(
            f"dry-run: {total} seed(s), {len(report.studios)} found, "
            f"{len(names_to_merge)} kept, {filtered_count} filtered, "
            f"{report.batches_fetched} batch(es), "
            f"complete={report.complete}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
