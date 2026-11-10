#!/usr/bin/env python3
"""Модели данных StashDB, GraphQL-клиент и разбор ответов.

Модуль содержит доменные модели (`Studio`, `Performer`, `Scene`), функции,
превращающие узел ответа StashDB в эти модели, и HTTP/GraphQL-клиент
`StashDBClient` с ретраями и разбором ошибок.

Схема узла сцены (см. запросы `findScenesBySceneFingerprints` и `searchScene`):

    {
      "id": "s1",
      "title": "Poking Glory",
      "release_date": "2026-10-03",
      "date": "2026-10-03",
      "code": "GP1",
      "duration": 1200,
      "studio": {"id": "st1", "name": "BrazzersExxtra",
                 "parent": {"id": "st0", "name": "Brazzers"}},
      "performers": [{"as": "Ryan Reid",
                      "performer": {"id": "p1", "name": "Ryan Reid"}}]
    }

Зависимостей нет — только стандартная библиотека.
"""
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass

STASHDB_ENDPOINT = "https://stashdb.org/graphql"
FINGERPRINT_BATCH_SIZE = 40
RETRY_AFTER_CAP = 300
BACKOFF_SECONDS = (1, 3, 9)

__all__ = [
    "FINGERPRINT_BATCH_SIZE",
    "SCENES_BY_FINGERPRINTS_QUERY",
    "SEARCH_SCENES_QUERY",
    "STASHDB_ENDPOINT",
    "STUDIOS_QUERY",
    "Performer",
    "Scene",
    "StashDBAuthError",
    "StashDBClient",
    "StashDBError",
    "StashDBQueryError",
    "StashDBRateLimitError",
    "StashDBResponseError",
    "StashDBTransientError",
    "Studio",
    "scene_from_node",
    "studio_from_node",
]

SCENES_BY_FINGERPRINTS_QUERY = """
query FindScenesByFingerprints($fingerprints: [[FingerprintQueryInput!]!]!) {
  findScenesBySceneFingerprints(fingerprints: $fingerprints) {
    id
    title
    release_date
    code
    duration
    studio { id name parent { id name } }
    performers { as performer { id name } }
  }
}
""".strip()

SEARCH_SCENES_QUERY = """
query SearchScenes($term: String!, $limit: Int!) {
  searchScene(term: $term, limit: $limit) {
    id
    title
    release_date
    code
    studio { id name parent { id name } }
    performers { as performer { id name } }
  }
}
""".strip()

STUDIOS_QUERY = """
query Studios($page: Int!, $perPage: Int!) {
  queryStudios(input: {page: $page, per_page: $perPage, sort: NAME, direction: ASC}) {
    count
    studios { id name }
  }
}
""".strip()


class StashDBError(RuntimeError):
    """Базовая ошибка работы со StashDB."""


class StashDBAuthError(StashDBError):
    """Неверный или отсутствующий API-ключ (401/403, `not authorized`)."""


class StashDBQueryError(StashDBError):
    """GraphQL вернул ошибку запроса (не связанную с авторизацией)."""


class StashDBTransientError(StashDBError):
    """Временная ошибка: сеть, таймаут или 5xx после всех попыток."""


class StashDBRateLimitError(StashDBTransientError):
    """429 после всех попыток повтора (учитывая `Retry-After`)."""


class StashDBResponseError(StashDBError):
    """Ответ не является ожидаемым JSON/объектом GraphQL."""


@dataclass(frozen=True)
class Studio:
    """Студия сцены; `parent` — родительская студия из StashDB."""

    id: str
    name: str
    parent_id: str | None = None
    parent_name: str | None = None

    def to_dict(self) -> dict:
        """Плоское представление для JSON-кэша (SQLite)."""
        return {
            "id": self.id,
            "name": self.name,
            "parent_id": self.parent_id,
            "parent_name": self.parent_name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Studio":
        """Восстанавливает студию из словаря `to_dict`."""
        return cls(
            id=data.get("id") or "",
            name=data.get("name") or "",
            parent_id=data.get("parent_id"),
            parent_name=data.get("parent_name"),
        )


@dataclass(frozen=True)
class Performer:
    """Исполнитель сцены; `alias` — отображаемое имя из поля `as`."""

    id: str
    name: str
    alias: str | None = None

    @property
    def display_name(self) -> str:
        """Имя для показа: псевдоним `as`, иначе настоящее имя."""
        return self.alias or self.name

    def to_dict(self) -> dict:
        """Плоское представление для JSON-кэша (SQLite)."""
        return {
            "id": self.id,
            "name": self.name,
            "alias": self.alias,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Performer":
        """Восстанавливает исполнителя из словаря `to_dict`."""
        return cls(
            id=data.get("id") or "",
            name=data.get("name") or "",
            alias=data.get("alias"),
        )


@dataclass(frozen=True)
class Scene:
    """Сцена StashDB.

    `release_date` — актуальная дата выхода, `date` — устаревшее поле.
    `studio` может быть `None`, `performers` — кортеж (пустой тоже допустим).
    """

    id: str
    title: str | None
    release_date: str | None
    date: str | None
    code: str | None
    duration: int | None
    studio: Studio | None = None
    performers: tuple[Performer, ...] = ()
    source: str = "StashDB"

    def to_dict(self) -> dict:
        """Плоское представление для JSON-кэша (SQLite)."""
        return {
            "id": self.id,
            "title": self.title,
            "release_date": self.release_date,
            "date": self.date,
            "code": self.code,
            "duration": self.duration,
            "studio": self.studio.to_dict() if self.studio is not None else None,
            "performers": [p.to_dict() for p in self.performers],
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Scene":
        """Восстанавливает сцену из словаря `to_dict`."""
        studio_data = data.get("studio")
        performers = data.get("performers") or []
        return cls(
            id=data.get("id") or "",
            title=data.get("title"),
            release_date=data.get("release_date"),
            date=data.get("date"),
            code=data.get("code"),
            duration=data.get("duration"),
            studio=Studio.from_dict(studio_data) if studio_data else None,
            performers=tuple(Performer.from_dict(p) for p in performers),
            source=data.get("source") or "StashDB",
        )


def studio_from_node(node: dict) -> Studio:
    """Студия из узла GraphQL `{id, name, parent {id, name}}`.

    Битый узел (не словарь) и битый `parent` не считаются ошибкой:
    отсутствующие поля остаются пустыми, как и у обычного узла без них.
    """
    if not isinstance(node, dict):
        node = {}
    parent = node.get("parent")
    if not isinstance(parent, dict):
        parent = None
    return Studio(
        id=node.get("id") or "",
        name=node.get("name") or "",
        parent_id=(parent.get("id") or None) if parent else None,
        parent_name=(parent.get("name") or None) if parent else None,
    )


def _performer_from_entry(entry: object) -> Performer | None:
    """Исполнитель из элемента списка `{as, performer {id, name}}`.

    Битый элемент (не словарь или не-словарь в `performer`) пропускается:
    один плохой узел не должен ломать разбор всей сцены.
    """
    if not isinstance(entry, dict):
        return None
    performer = entry.get("performer")
    if performer is None:
        performer = {}
    elif not isinstance(performer, dict):
        return None
    return Performer(
        id=performer.get("id") or "",
        name=performer.get("name") or "",
        alias=entry.get("as") or None,
    )


def scene_from_node(node: dict) -> Scene:
    """Сцена из узла GraphQL.

    Отсутствующие поля остаются `None`, `studio` может быть `None`,
    пустой `performers` даёт пустой кортеж. Некорректные вложенные узлы
    (`studio` не словарь, битые элементы `performers`) пропускаются.
    """
    studio_node = node.get("studio")
    studio = (
        studio_from_node(studio_node)
        if isinstance(studio_node, dict) and studio_node
        else None
    )
    performers = node.get("performers")
    if not isinstance(performers, list):
        performers = []
    return Scene(
        id=node.get("id") or "",
        title=node.get("title"),
        release_date=node.get("release_date"),
        date=node.get("date"),
        code=node.get("code"),
        duration=node.get("duration"),
        studio=studio,
        performers=tuple(
            performer
            for entry in performers
            if (performer := _performer_from_entry(entry)) is not None
        ),
    )


def _scenes_from_nodes(nodes: object) -> list[Scene]:
    """Список сцен из списка узлов GraphQL; не-списки дают пустой список."""
    if not isinstance(nodes, list):
        return []
    return [scene_from_node(node) for node in nodes if isinstance(node, dict)]


def _studios_from_nodes(nodes: object) -> list[Studio]:
    """Список студий из списка узлов GraphQL; не-списки дают пустой список."""
    if not isinstance(nodes, list):
        return []
    return [studio_from_node(node) for node in nodes if isinstance(node, dict)]


class StashDBClient:
    """Минимальный GraphQL-клиент StashDB.

    Сетевой слой можно подменить через `transport` — функция
    `(urllib.request.Request, timeout) -> bytes`. Это используется в тестах,
    чтобы не ходить в сеть. Ключ отправляется заголовком `ApiKey` (не Bearer).

    Повторы: 429 — по `Retry-After` (целые секунды, не больше 300); 5xx,
    `URLError` и таймауты — с паузой 1/3/9 секунд. 401/403 и GraphQL
    `not authorized` фатальны и не повторяются.
    """

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = STASHDB_ENDPOINT,
        delay: float = 1.0,
        timeout: float = 30.0,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        transport: Callable[[urllib.request.Request, float], bytes] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._api_key = api_key
        self._endpoint = endpoint
        self._delay = delay
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._transport = transport

    def find_scenes_by_hashes(
        self, hashes: Sequence[str], algorithm: str = "OSHASH"
    ) -> list[list[Scene]]:
        """Сцены для каждого фингерпринта указанного алгоритма, в порядке входных хэшей.

        Дубликаты запрашиваются один раз; результат выровнен по исходной
        последовательности (включая повторные хэши). Хэши отправляются
        батчами не более 40 штук.
        """
        unique_hashes = list(dict.fromkeys(hashes))
        if not unique_hashes:
            return []

        scenes_by_hash: dict[str, list[Scene]] = {}
        for start in range(0, len(unique_hashes), FINGERPRINT_BATCH_SIZE):
            batch = unique_hashes[start:start + FINGERPRINT_BATCH_SIZE]
            data = self._post(
                SCENES_BY_FINGERPRINTS_QUERY,
                {
                    "fingerprints": [
                        [{"hash": h, "algorithm": algorithm}] for h in batch
                    ]
                },
            )
            groups = data.get("findScenesBySceneFingerprints")
            if not isinstance(groups, list):
                groups = []
            for index, h in enumerate(batch):
                nodes = groups[index] if index < len(groups) else []
                scenes_by_hash[h] = _scenes_from_nodes(nodes)

        return [list(scenes_by_hash[h]) for h in hashes]

    def search_scenes(self, term: str, limit: int = 10) -> list[Scene]:
        """Текстовый поиск сцен через `searchScene`."""
        data = self._post(SEARCH_SCENES_QUERY, {"term": term, "limit": limit})
        return _scenes_from_nodes(data.get("searchScene"))

    def execute(self, query: str, variables: dict | None = None) -> dict:
        """Выполняет произвольный GraphQL-запрос и возвращает поле `data`."""
        return self._post(query, variables or {})

    def query_studios(self, page: int, per_page: int = 100) -> tuple[list[Studio], int]:
        """Одна страница студий и общее количество (`count`)."""
        data = self._post(STUDIOS_QUERY, {"page": page, "perPage": per_page})
        payload = data.get("queryStudios")
        if not isinstance(payload, dict):
            raise StashDBResponseError("StashDB response has no `queryStudios`")
        count = payload.get("count")
        if not isinstance(count, int) or isinstance(count, bool):
            raise StashDBResponseError("StashDB response has no studio count")
        return _studios_from_nodes(payload.get("studios")), count

    def _post(self, query: str, variables: dict) -> dict:
        """Выполняет GraphQL-запрос с ретраями и возвращает поле `data`."""
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        last_error: StashDBError | None = None

        for attempt in range(self._max_attempts):
            if self._delay > 0:
                self._sleep(self._delay)
            request = urllib.request.Request(
                self._endpoint,
                data=body,
                headers={
                    "ApiKey": self._api_key,
                    "Content-Type": "application/json",
                    "User-Agent": "filename-cleaner/1.0",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                raw = self._send(request)
            except urllib.error.HTTPError as exc:
                status = exc.code
                if status in (401, 403):
                    raise StashDBAuthError(
                        f"StashDB rejected the API key (HTTP {status})"
                    ) from exc
                if status == 429:
                    last_error = StashDBRateLimitError(
                        "StashDB rate limit reached (HTTP 429)"
                    )
                    if attempt + 1 >= self._max_attempts:
                        raise last_error from exc
                    retry_after = self._retry_after_seconds(exc)
                    if retry_after is None:
                        retry_after = self._backoff_seconds(attempt)
                    self._sleep(retry_after)
                    continue
                if 500 <= status < 600:
                    last_error = StashDBTransientError(
                        f"StashDB server error (HTTP {status})"
                    )
                    if attempt + 1 >= self._max_attempts:
                        raise last_error from exc
                    self._sleep(self._backoff_seconds(attempt))
                    continue
                raise StashDBResponseError(
                    f"StashDB returned unexpected HTTP {status}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = StashDBTransientError(
                    f"StashDB request failed: {exc}"
                )
                if attempt + 1 >= self._max_attempts:
                    raise last_error from exc
                self._sleep(self._backoff_seconds(attempt))
                continue

            return self._parse_response(raw)

        # Цикл всегда возвращает результат или возбуждает исключение.
        if last_error is not None:
            raise last_error
        raise StashDBTransientError("StashDB request failed")

    def _send(self, request: urllib.request.Request) -> bytes:
        """Один сетевой вызов — через `transport`, если он задан."""
        if self._transport is not None:
            return self._transport(request, self._timeout)
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return response.read()

    def _parse_response(self, raw: bytes | str) -> dict:
        """Разбирает JSON-ответ и превращает GraphQL `errors` в исключения."""
        if isinstance(raw, (bytes, bytearray)):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise StashDBResponseError(
                    "StashDB returned invalid UTF-8"
                ) from exc
        elif isinstance(raw, str):
            text = raw
        else:
            raise StashDBResponseError("StashDB returned a non-text response")

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StashDBResponseError("StashDB returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise StashDBResponseError("StashDB returned a non-object response")

        errors = payload.get("errors")
        if errors:
            message = self._error_message(errors)
            if "not authorized" in message.lower():
                raise StashDBAuthError(
                    f"StashDB rejected the API key: {message}"
                )
            raise StashDBQueryError(message)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise StashDBResponseError("StashDB response has no `data` object")
        return data

    @staticmethod
    def _error_message(errors: object) -> str:
        """Склеивает сообщения GraphQL-ошибок в одну строку."""
        if isinstance(errors, list):
            messages = []
            for error in errors:
                if isinstance(error, dict):
                    messages.append(str(error.get("message") or ""))
                else:
                    messages.append(str(error))
        else:
            messages = [str(errors)]
        message = "; ".join(m for m in messages if m)
        return message or "unknown GraphQL error"

    @staticmethod
    def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
        """`Retry-After` в секундах, ограниченный 300; None, если его нет."""
        headers = exc.headers or {}
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw is None:
            return None
        try:
            seconds = int(str(raw).strip())
        except ValueError:
            return None
        return float(min(max(seconds, 0), RETRY_AFTER_CAP))

    @staticmethod
    def _backoff_seconds(attempt: int) -> int:
        """Пауза 1/3/9 секунд по номеру неудачной попытки (с нуля)."""
        return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
