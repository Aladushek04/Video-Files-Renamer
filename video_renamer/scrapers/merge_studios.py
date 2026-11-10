#!/usr/bin/env python3
"""Слияние нескольких JSON-справочников студий в один.

Первый встреченный вариант имени побеждает; альтернативные написания
попадают в отчёт коллизий. Без `--apply` — dry-run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from video_renamer.scrapers.stashdb import (
    _atomic_write_json,
    _backup_studios,
    _collisions_path,
    merge_studios,
)

__all__ = ["main", "merge_studio_dicts", "merge_studio_files"]


def _read_dict(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(f"{path}: expected string keys and values")
    return payload


def merge_studio_dicts(
    sources: Sequence[dict[str, str]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Сливает словари по порядку, сохраняя первое значение для ключа."""
    if not sources:
        return {}, {}
    merged = dict(sources[0])
    collisions: dict[str, list[str]] = {}
    for source in sources[1:]:
        merged, batch = merge_studios(merged, list(source.values()))
        for key, names in batch.items():
            bucket = collisions.setdefault(key, [])
            for name in names:
                if name not in bucket:
                    bucket.append(name)
    return merged, collisions


def merge_studio_files(
    inputs: Sequence[str],
    out: str,
    *,
    apply: bool = False,
) -> dict[str, int]:
    """Читает inputs (+ существующий out), сливает и при `apply` пишет файлы."""
    paths: list[str] = []
    if os.path.exists(out):
        paths.append(os.path.abspath(out))
    for path in inputs:
        absolute = os.path.abspath(path)
        if absolute not in paths:
            paths.append(absolute)

    sources: list[dict[str, str]] = []
    for path in paths:
        try:
            sources.append(_read_dict(path))
        except FileNotFoundError as exc:
            raise ValueError(f"input file not found: {path}") from exc

    merged, collisions = merge_studio_dicts(sources)
    if apply:
        if os.path.exists(out):
            _backup_studios(out)
        _atomic_write_json(out, merged)
        _atomic_write_json(_collisions_path(out), collisions)
    return {
        "merged_keys": len(merged),
        "collisions": sum(len(names) for names in collisions.values()),
        "written": int(apply),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m video_renamer.scrapers.merge_studios",
        description="Merge studio JSON dictionaries, first value wins.",
    )
    parser.add_argument("inputs", nargs="+", help="studio JSON files to merge")
    parser.add_argument("--out", default="data/studios.json",
                        help="output JSON (default: data/studios.json)")
    parser.add_argument("--apply", action="store_true",
                        help="write output and collision report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = merge_studio_files(args.inputs, args.out, apply=args.apply)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    action = "written" if args.apply else "dry-run"
    print(
        f"{action}: {report['merged_keys']} key(s), "
        f"{report['collisions']} collision(s)"
    )
    if not args.apply:
        print(f"add --apply to write {args.out} and "
              f"{_collisions_path(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
