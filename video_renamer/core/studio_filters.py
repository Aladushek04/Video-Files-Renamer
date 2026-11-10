"""Фильтр мусорных имён студий из больших stash-box источников.

FansDB и ThePornDB содержат тысячи пользовательских записей вида
`00000000jhvfl (dirtyFans)`, `+AddFriends (network)` и просто чисел. Такие
имена не помогают распознавать студию в имени файла и только засоряют
справочник. Фильтр консервативный: он режет явные ID/заглушки, но не трогает
короткие латинские названия вроде `4based` или `XArt`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from video_renamer.core.clean_ph_names import normalize_studio_key

_PLACEHOLDER_NAMES = frozenset(
    {"unknown", "n/a", "na", "none", "null", "test", "untitled", "no name"}
)
_LONG_DIGIT_RUN = re.compile(r"\d{6,}")
_GENERATED_ID_PREFIX = re.compile(r"^\d{4,}[A-Za-z]")
_LEADING_JUNK = ("+", "-", "_")


def is_useful_studio_name(name: str) -> bool:
    """False для пустых, числовых, слишком коротких и явно сгенерированных имён."""
    text = (name or "").strip()
    if not text:
        return False
    if text.casefold() in _PLACEHOLDER_NAMES:
        return False
    if text.startswith(_LEADING_JUNK):
        return False

    key = normalize_studio_key(text)
    if len(key) < 4:
        return False

    letters = [char for char in text if char.isalpha()]
    if not letters:
        return False
    if len({char.lower() for char in letters}) < 2:
        return False
    if _LONG_DIGIT_RUN.search(text):
        return False
    return not _GENERATED_ID_PREFIX.match(text)


def filter_studio_names(names: Sequence[str]) -> tuple[list[str], int]:
    """Возвращает (оставленные имена, сколько отброшено)."""
    kept = [name for name in names if is_useful_studio_name(name)]
    return kept, len(names) - len(kept)
