#!/usr/bin/env python3
"""Локальный SQLite-кэш ответов StashDB.

Кэш хранит полный JSON сцены (а не только идентификатор), поэтому он
самодостаточен: с его помощью можно работать офлайн, без сети и без
отдельной таблицы сцен.

Таблицы:

    fingerprints(hash TEXT PRIMARY KEY, scenes TEXT NOT NULL, fetched_at TEXT NOT NULL)
        — устаревшая: при каждом открытии переносится в fingerprint_cache
          через INSERT OR IGNORE (существующие строки не перезаписываются)
    fingerprint_cache(source, algorithm, hash) — сцены источника по фингерпринту
    text_queries(query TEXT PRIMARY KEY, scenes TEXT NOT NULL, fetched_at TEXT NOT NULL)
    local_phash(oshash, size) — PHASH файла по содержимому, без TTL;
        запись действительна только для той же версии файла (mtime_ns/file_id)

Смысл возвращаемого значения:

    None — записи нет или она истекла (TTL 30 дней);
    []   — отрицательный результат, сохранённый намеренно.

Время хранится как ISO-дата в UTC; для проверки TTL используется
подставляемый `clock` (по умолчанию — системное время UTC).

Зависимостей нет — только стандартная библиотека.
"""

import json
import os
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from video_renamer.core.stashdb_client import Scene

__all__ = ["CACHE_TTL_DAYS", "TitleCache", "utcnow"]

CACHE_TTL_DAYS = 30
_TTL = timedelta(days=CACHE_TTL_DAYS)

_SCHEMA = (
    (
        "CREATE TABLE IF NOT EXISTS fingerprints ("
        "hash TEXT PRIMARY KEY, scenes TEXT NOT NULL, fetched_at TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS text_queries ("
        "query TEXT PRIMARY KEY, scenes TEXT NOT NULL, fetched_at TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS fingerprint_cache ("
        "source TEXT NOT NULL, algorithm TEXT NOT NULL, hash TEXT NOT NULL, "
        "scenes TEXT NOT NULL, fetched_at TEXT NOT NULL, "
        "PRIMARY KEY (source, algorithm, hash))"
    ),
    (
        "CREATE TABLE IF NOT EXISTS local_phash ("
        "oshash TEXT NOT NULL, size INTEGER NOT NULL, phash TEXT NOT NULL, "
        "computed_at TEXT NOT NULL, mtime_ns INTEGER, file_id INTEGER, "
        "PRIMARY KEY (oshash, size))"
    ),
)
# Столбцы свежести появились позже; в старых базах их добавляет миграция.
_ADD_LOCAL_PHASH_COLUMNS = {
    "mtime_ns": "ALTER TABLE local_phash ADD COLUMN mtime_ns INTEGER",
    "file_id": "ALTER TABLE local_phash ADD COLUMN file_id INTEGER",
}
_LEGACY_MIGRATION = (
    "INSERT OR IGNORE INTO fingerprint_cache "
    "(source, algorithm, hash, scenes, fetched_at) "
    "SELECT 'StashDB', 'OSHASH', hash, scenes, fetched_at FROM fingerprints"
)


def _migrate_local_phash(conn: sqlite3.Connection) -> None:
    """Добавляет столбцы свежести в таблицу, созданную до их появления."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(local_phash)")}
    for column, statement in _ADD_LOCAL_PHASH_COLUMNS.items():
        if column not in existing:
            conn.execute(statement)


def utcnow() -> datetime:
    """Текущее время в UTC (с таймзоной)."""
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    """Дата из строки ISO; `None`, если значение разобрать нельзя."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class TitleCache:
    """Кэш ответов StashDB по OSHASH и по текстовому запросу.

    `clock` — функция без аргументов, возвращающая текущее время
    (в тестах подставляется фиксированное время, чтобы проверить TTL).
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._path = Path(path)
        self._clock = clock
        self._conn = self._connect(self._path)

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        """Открывает базу (создавая родительскую папку) и создаёт таблицы."""
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        try:
            for statement in _SCHEMA:
                conn.execute(statement)
            _migrate_local_phash(conn)
            conn.execute(_LEGACY_MIGRATION)
            conn.commit()
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    @property
    def path(self) -> Path:
        """Путь к файлу базы."""
        return self._path

    def get_fingerprint(self, oshash: str) -> list[Scene] | None:
        """Сцены по OSHASH из StashDB; `None`, если записи нет или она истекла."""
        return self.get_source_fingerprint("StashDB", "OSHASH", oshash)

    def put_fingerprint(self, oshash: str, scenes: Sequence[Scene]) -> None:
        """Сохраняет сцены по OSHASH из StashDB (пустой список — отрицательный кэш)."""
        self.put_source_fingerprint("StashDB", "OSHASH", oshash, scenes)

    def get_source_fingerprint(
        self, source: str, algorithm: str, digest: str
    ) -> list[Scene] | None:
        """Сцены источника по фингерпринту; `None`, если записи нет или она истекла."""
        row = (
            self._connection()
            .execute(
                "SELECT scenes, fetched_at FROM fingerprint_cache "
                "WHERE source = ? AND algorithm = ? AND hash = ?",
                (source, algorithm, digest),
            )
            .fetchone()
        )
        return self._scenes_from_row(row)

    def put_source_fingerprint(
        self, source: str, algorithm: str, digest: str, scenes: Sequence[Scene]
    ) -> None:
        """Сохраняет сцены источника по фингерпринту (пустой список — отрицательный кэш)."""
        payload = json.dumps([scene.to_dict() for scene in scenes])
        conn = self._connection()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO fingerprint_cache "
                "(source, algorithm, hash, scenes, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (source, algorithm, digest, payload, self._now().isoformat()),
            )

    def get_local_phash(
        self, oshash: str, size: int, mtime_ns: int, file_id: int = 0
    ) -> str | None:
        """PHASH файла, если запись сделана для той же версии файла.

        Пара (OSHASH, размер) не доказывает идентичность содержимого: правка в
        середине файла при том же размере оставляет OSHASH прежним. Поэтому
        запись принимается только при совпадении `mtime_ns`, а если ОС сообщает
        `file_id` и он сохранён — ещё и его. Строки без `mtime_ns` (старая
        схема) считаются неоднозначными и требуют пересчёта.
        """
        row = (
            self._connection()
            .execute(
                "SELECT phash, mtime_ns, file_id FROM local_phash WHERE oshash = ? AND size = ?",
                (oshash, size),
            )
            .fetchone()
        )
        if row is None:
            return None
        phash, stored_mtime, stored_id = row
        if stored_mtime is None or stored_mtime != mtime_ns:
            return None
        if file_id and stored_id and stored_id != file_id:
            return None
        return phash

    def put_local_phash(
        self, oshash: str, size: int, phash: str, mtime_ns: int, file_id: int = 0
    ) -> None:
        """Сохраняет PHASH файла вместе с версией файла."""
        conn = self._connection()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO local_phash "
                "(oshash, size, phash, computed_at, mtime_ns, file_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    oshash,
                    size,
                    phash,
                    self._now().isoformat(),
                    mtime_ns,
                    file_id or None,
                ),
            )

    def get_text_query(self, term: str) -> list[Scene] | None:
        """Сцены по текстовому запросу; `None`, если записи нет или истекла."""
        row = (
            self._connection()
            .execute(
                "SELECT scenes, fetched_at FROM text_queries WHERE query = ?",
                (term,),
            )
            .fetchone()
        )
        return self._scenes_from_row(row)

    def put_text_query(self, term: str, scenes: Sequence[Scene]) -> None:
        """Сохраняет сцены по текстовому запросу."""
        self._put("text_queries", "query", term, scenes)

    def close(self) -> None:
        """Закрывает соединение; повторный вызов безопасен."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connection(self) -> sqlite3.Connection:
        """Соединение; после `close` — понятная ошибка вместо `None`."""
        if self._conn is None:
            raise sqlite3.ProgrammingError("title cache is closed")
        return self._conn

    def _put(self, table: str, key_column: str, key: str, scenes: Sequence[Scene]) -> None:
        """Записывает список сцен (JSON) с текущим временем."""
        payload = json.dumps([scene.to_dict() for scene in scenes])
        conn = self._connection()
        with conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {table} "
                f"({key_column}, scenes, fetched_at) VALUES (?, ?, ?)",
                (key, payload, self._now().isoformat()),
            )

    def _scenes_from_row(self, row: object) -> list[Scene] | None:
        """Разбирает строку таблицы в список сцен; `None` — битая/истёкшая."""
        if row is None:
            return None
        payload, fetched_at = row
        fetched = _parse_timestamp(fetched_at)
        if fetched is None:
            return None
        if self._now() - fetched > _TTL:
            return None
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, list):
            return None
        try:
            return [Scene.from_dict(item) for item in data]
        except (AttributeError, TypeError, ValueError):
            return None

    def _now(self) -> datetime:
        """Текущее время из `clock`, всегда в UTC."""
        moment = self._clock()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
