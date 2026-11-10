#!/usr/bin/env python3
"""Локальный справочник имён исполнителей StashDB.

Индекс — плоский словарь «нормализованное имя или алиас → каноническое имя».
В индекс попадают только многотокенные ключи, которые указывают ровно на
одного исполнителя: `Veronica Leal`, но не `Veronica`, `Zoe` или `Glory`.
Односложные имена и неоднозначные алиасы в имени файла неотличимы от обычных
слов, поэтому для разбора имён они не используются.

`PerformerIndex.strip_names()` вырезает найденные имена из строки целиком по
границам токенов и возвращает канонические написания. Это позволяет
переименователю отделять имена до того, как `extract_studio` примет первое
слово имени за студию (например, `VERONICA` — реальная студия StashDB).

Зависимостей нет — только стандартная библиотека.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from video_renamer.core.text_norm import normalize_for_match

__all__ = [
    "MAX_KEY_TOKENS",
    "MIN_KEY_LENGTH",
    "NameMatch",
    "PerformerIndex",
    "build_index",
]

MAX_KEY_TOKENS = 5
MIN_KEY_LENGTH = 3


@dataclass(frozen=True)
class NameMatch:
    """Найденное имя: каноническое написание и границы в исходных токенах."""

    name: str
    key: str
    start: int
    end: int


def _usable_key(key: str) -> bool:
    """Ключ пригоден для поиска в имени файла."""
    tokens = key.split()
    return (
        len(key) >= MIN_KEY_LENGTH
        and 2 <= len(tokens) <= MAX_KEY_TOKENS
        and not key.replace(" ", "").isdigit()
    )


def build_index(performers: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """Строит индекс из записей StashDB; конфликтующие ключи выбрасывает.

    Ключ, который после нормализации указывает на разные канонические имена,
    удаляется целиком: угадывать исполнителя по такому алиасу нельзя.
    """
    index: dict[str, str] = {}
    conflicts: set[str] = set()

    for performer in performers:
        if not isinstance(performer, Mapping):
            continue
        if performer.get("deleted") or performer.get("merged_into_id"):
            continue
        name = str(performer.get("name") or "").strip()
        if not name:
            continue
        raw_aliases = performer.get("aliases") or []
        if not isinstance(raw_aliases, (list, tuple)):
            raw_aliases = []
        for raw in (name, *raw_aliases):
            key = normalize_for_match(str(raw))
            if not _usable_key(key):
                continue
            known = index.get(key)
            if known is not None and known != name:
                conflicts.add(key)
            else:
                index.setdefault(key, name)

    for key in conflicts:
        index.pop(key, None)
    return index


class PerformerIndex:
    """Индекс имён с поиском самого длинного совпадения по токенам."""

    def __init__(self, index: Mapping[str, str]) -> None:
        self._index = {
            key: name
            for key, name in index.items()
            if isinstance(key, str) and isinstance(name, str) and _usable_key(key)
        }
        self._max_tokens = max((len(key.split()) for key in self._index), default=0)

    def __len__(self) -> int:
        return len(self._index)

    @classmethod
    def from_index(cls, index: Mapping[str, str]) -> "PerformerIndex":
        """Индекс из готового словаря (например, в тестах)."""
        return cls(index)

    @classmethod
    def load(cls, path: str) -> "PerformerIndex":
        """Читает JSON-индекс; битый файл — `ValueError`."""
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise TypeError("performer index must be a JSON object")
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("performer index must map strings to strings")
        return cls(payload)

    @classmethod
    def load_if_exists(cls, path: str) -> "PerformerIndex | None":
        """Индекс из файла или `None`, если файла нет."""
        try:
            return cls.load(path)
        except FileNotFoundError:
            return None

    def find_names(self, text: str) -> tuple[NameMatch, ...]:
        """Все известные имена в строке, самое длинное совпадение первым.

        Совпадения не пересекаются. Имя ищется по границам исходных токенов,
        поэтому апострофы и дефисы внутри токена (`O'Neil`) не ломают
        сопоставление: части токена склеиваются в один нормализованный ключ.
        """
        if not text or not self._index:
            return ()

        tokens = text.split()
        norm_parts = [normalize_for_match(token).split() for token in tokens]
        flat: list[str] = []
        owner: list[int] = []
        first: list[bool] = []
        for token_index, parts in enumerate(norm_parts):
            for part_index, part in enumerate(parts):
                flat.append(part)
                owner.append(token_index)
                first.append(part_index == 0)

        matches: list[NameMatch] = []
        position = 0
        total = len(flat)
        while position < total:
            found: tuple[int, str, str, int, int] | None = None
            if first[position]:
                upper = min(self._max_tokens, total - position)
                for size in range(upper, 0, -1):
                    stop = position + size
                    if stop < total and owner[stop] == owner[stop - 1]:
                        continue
                    key = " ".join(flat[position:stop])
                    name = self._index.get(key)
                    if name is not None:
                        found = (size, key, name, owner[position], owner[stop - 1])
                        break
            if found is None:
                position += 1
                continue
            size, key, name, start, end = found
            matches.append(NameMatch(name=name, key=key, start=start, end=end + 1))
            position += size
        return tuple(matches)

    def strip_names(self, text: str) -> tuple[str, tuple[str, ...]]:
        """Убирает известные имена из строки и возвращает их канонические имена."""
        matches = self.find_names(text)
        if not matches:
            return text, ()
        tokens = text.split()
        dropped: set[int] = set()
        for match in matches:
            dropped.update(range(match.start, match.end))
        rest = " ".join(token for index, token in enumerate(tokens) if index not in dropped)
        names = tuple(dict.fromkeys(match.name for match in matches))
        return rest, names
