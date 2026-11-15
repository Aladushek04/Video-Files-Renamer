"""Свежесть локального PHASH-кэша (A15).

Пара (OSHASH, размер) не доказывает идентичность содержимого: правка в
середине файла при том же размере оставляет OSHASH прежним. Поэтому сервис
принимает кэш, только если совпадает версия файла (mtime и file id), а иначе
пересчитывает PHASH.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from video_renamer.core.title_cache import TitleCache
from video_renamer.core.title_service import TitleService


class EmptyClient:
    """Клиент без совпадений: заставляет считать PHASH локально."""

    def find_scenes_by_hashes(self, hashes, algorithm="OSHASH"):
        return [[] for _ in hashes]

    def search_scenes(self, term, limit=10):
        return []


def preview(root: Path, cache: TitleCache, hasher) -> None:
    TitleService(
        str(root),
        {},
        cache,
        EmptyClient(),
        oshash_func=lambda path: "h",
        phash=True,
        phash_func=hasher,
    ).preview()


def write(path: Path, data: bytes = b"x" * 100) -> Path:
    path.write_bytes(data)
    return path


def bump_mtime(path: Path, seconds: float = 10.0) -> None:
    moment = path.stat().st_mtime + seconds
    os.utime(path, (moment, moment))


def counting_hasher(value: str = "p1"):
    calls: list[str] = []

    def hasher(path):
        calls.append(path)
        return value

    return calls, hasher


def test_unchanged_file_is_read_from_cache(tmp_path):
    write(tmp_path / "a.mp4")
    calls, hasher = counting_hasher()
    cache = TitleCache(tmp_path / "c.sqlite3")

    preview(tmp_path, cache, hasher)
    preview(tmp_path, cache, hasher)

    assert len(calls) == 1
    cache.close()


def test_changed_middle_recomputes_with_same_size_and_oshash(tmp_path):
    target = write(tmp_path / "a.mp4", b"a" * 50 + b"b" * 50)
    calls, hasher = counting_hasher()
    cache = TitleCache(tmp_path / "c.sqlite3")

    preview(tmp_path, cache, hasher)
    assert len(calls) == 1

    # Размер тот же, заглушка OSHASH та же — меняется только середина.
    target.write_bytes(b"a" * 50 + b"c" * 50)
    bump_mtime(target)

    preview(tmp_path, cache, hasher)
    assert len(calls) == 2

    # Пересчитанное значение сохранено для новой версии файла.
    preview(tmp_path, cache, hasher)
    assert len(calls) == 2
    cache.close()


def test_touched_file_recomputes(tmp_path):
    target = write(tmp_path / "a.mp4")
    calls, hasher = counting_hasher()
    cache = TitleCache(tmp_path / "c.sqlite3")

    preview(tmp_path, cache, hasher)
    bump_mtime(target)
    preview(tmp_path, cache, hasher)

    assert len(calls) == 2
    cache.close()


def test_renamed_file_keeps_cache(tmp_path):
    target = write(tmp_path / "a.mp4")
    calls, hasher = counting_hasher()
    cache = TitleCache(tmp_path / "c.sqlite3")

    preview(tmp_path, cache, hasher)
    target.rename(tmp_path / "b.mp4")
    preview(tmp_path, cache, hasher)

    assert len(calls) == 1
    cache.close()


def test_replaced_file_with_same_size_and_mtime_recomputes(tmp_path):
    target = write(tmp_path / "a.mp4", b"x" * 100)
    replacement = write(tmp_path / "packed.tmp", b"y" * 100)
    stamp = target.stat()
    if not stamp.st_ino or not replacement.stat().st_ino:
        pytest.skip("ФС не сообщает file id: сверка по mtime остаётся единственной")
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    calls, hasher = counting_hasher()
    cache = TitleCache(tmp_path / "c.sqlite3")
    preview(tmp_path, cache, hasher)

    os.replace(replacement, target)
    preview(tmp_path, cache, hasher)

    assert len(calls) == 2
    cache.close()


def test_file_changed_during_hashing_is_not_cached(tmp_path):
    write(tmp_path / "a.mp4", b"a" * 100)
    db = tmp_path / "c.sqlite3"

    def hasher(path):
        Path(path).write_bytes(b"b" * 100)
        return "p1"

    cache = TitleCache(db)
    preview(tmp_path, cache, hasher)

    with sqlite3.connect(db) as conn:
        stored = conn.execute("SELECT COUNT(*) FROM local_phash").fetchone()[0]
    assert stored == 0
    cache.close()
