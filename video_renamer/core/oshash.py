#!/usr/bin/env python3
"""OSHASH — отпечаток файла по алгоритму OpenSubtitles.org.

Реализация повторяет Go-пакет stashapp/stash pkg/hash/oshash, поэтому хеш
совпадает с тем, что считает Stash для своих видеофайлов:

    размер файла + сумма 64-битных little-endian блоков первых и последних
    64 КиБ файла (для файлов меньше 64 КиБ берётся кратное 8 число байт).

Зависимостей нет — только стандартная библиотека.
"""

import os

__all__ = ["OshashError", "oshash_from_path"]

CHUNK_SIZE = 64 * 1024
_UINT64_MOD = 2**64
_MIN_SIZE = 8


class OshashError(ValueError):
    """Файл слишком мал, чтобы посчитать OSHASH."""


def _sum_bytes(buf: bytes) -> int:
    """Сумма 64-битных little-endian блоков; хвост короче 8 байт игнорируется."""
    return sum(int.from_bytes(buf[i : i + 8], "little") for i in range(0, len(buf) - 7, 8))


def _read_exact(f, count: int) -> bytes:
    """Читает ровно count байт; при нехватке — сколько есть."""
    parts = []
    remaining = count
    while remaining > 0:
        block = f.read(remaining)
        if not block:
            break
        parts.append(block)
        remaining -= len(block)
    return b"".join(parts)


def oshash_from_path(path: str | os.PathLike[str]) -> str:
    """Считает OSHASH файла. Возвращает 16 hex-символов в нижнем регистре.

    Бросает OshashError, если размер файла не больше 8 байт.
    """
    with open(os.fspath(path), "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size <= _MIN_SIZE:
            raise OshashError(
                f"cannot calculate oshash for file of size {size} bytes (need > {_MIN_SIZE})"
            )

        chunk = CHUNK_SIZE
        if size < chunk:
            # Размер должен быть кратен 8, иначе блоки не соберутся.
            chunk = (size // 8) * 8

        f.seek(0)
        head = _read_exact(f, chunk)
        f.seek(-chunk, os.SEEK_END)
        tail = _read_exact(f, chunk)

    if len(head) < chunk or len(tail) < chunk:
        raise OshashError("file changed size while reading")

    total = size + _sum_bytes(head) + _sum_bytes(tail)
    return f"{total % _UINT64_MOD:016x}"
