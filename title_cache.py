#!/usr/bin/env python3
"""Локальный SQLite-кэш ответов StashDB.

Кэш хранит полный JSON сцены (а не только идентификатор), поэтому он
самодостаточен: с его помощью можно работать офлайн, без сети и без
отдельной таблицы сцен.

Две таблицы:

    fingerprints(hash TEXT PRIMARY KEY, scenes TEXT NOT NULL, fetched_at TEXT NOT NULL)
    text_queries(query TEXT PRIMARY KEY, scenes TEXT NOT NULL, fetched_at TEXT NOT NULL)

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

from stashdb_client import Scene

__all__ = ["CACHE_TTL_DAYS", "TitleCache", "utcnow"]

CACHE_TTL_DAYS = 30
_TTL = timedelta(days=CACHE_TTL_DAYS)

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS fingerprints ("
    "hash TEXT PRIMARY KEY, scenes TEXT NOT NULL, fetched_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS text_queries ("
    "query TEXT PRIMARY KEY, scenes TEXT NOT NULL, fetched_at TEXT NOT NULL)",
)


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
        """Сцены по OSHASH; `None`, если записи нет или она истекла."""
        row = self._connection().execute(
            "SELECT scenes, fetched_at FROM fingerprints WHERE hash = ?",
            (oshash,),
        ).fetchone()
        return self._scenes_from_row(row)

    def put_fingerprint(self, oshash: str, scenes: Sequence[Scene]) -> None:
        """Сохраняет сцены по OSHASH (пустой список — отрицательный кэш)."""
        self._put("fingerprints", "hash", oshash, scenes)

    def get_text_query(self, term: str) -> list[Scene] | None:
        """Сцены по текстовому запросу; `None`, если записи нет или истекла."""
        row = self._connection().execute(
            "SELECT scenes, fetched_at FROM text_queries WHERE query = ?",
            (term,),
        ).fetchone()
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

    def _put(self, table: str, key_column: str, key: str,
             scenes: Sequence[Scene]) -> None:
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
