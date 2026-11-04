#!/usr/bin/env python3
"""Имена сцен StashDB: разбор локального имени и сборка итогового stem.

Локальное имя разбирается тем же конвейером, что и `clean_ph_names.clean_stem`
(дата, студия, техмусор), но остаток сохраняется без Title Case: по нему
считается сходство. Итоговый stem собирается из официальных полей StashDB
как есть — регистр `title`, `studio.name` и `performers[].display_name`
не переделывается.

Windows-санитизация заменяет запрещённые символы пробелом, срезает хвостовые
точки/пробелы и добавляет `_` к зарезервированным именам (`CON`, `PRN`,
`AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`). Unicode сохраняется.

Зависимостей нет — только стандартная библиотека.
"""
import re
from dataclasses import dataclass

from clean_ph_names import build_new_name, clean_stem, prepare_stem, split_name
from stashdb_client import Scene

__all__ = [
    "LocalNameInfo",
    "build_heuristic_stem",
    "build_scene_stem",
    "format_scene_date",
    "is_valid_final_stem",
    "parse_local_name",
    "sanitize_windows_stem",
]

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WS = re.compile(r"\s+")
_DATE_FULL = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DATE_MONTH = re.compile(r"(\d{4})-(\d{2})")
_DATE_YEAR = re.compile(r"(\d{4})")
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_MAX_STEM_LENGTH = 255


@dataclass(frozen=True)
class LocalNameInfo:
    """Разобранное локальное имя.

    `stem` — имя без расширения, `date` — дата в `ДД.ММ.ГГГГ`, `studio` —
    распознанная студия, `rest` — очищенный остаток без даты и студии.
    """

    stem: str
    date: str | None
    studio: str | None
    rest: str


def parse_local_name(filename: str, studios: dict[str, str]) -> LocalNameInfo:
    """Разбирает имя файла: stem без расширения, дата, студия и остаток."""
    stem, _ = split_name(filename)
    rest, date, studio = prepare_stem(stem, studios)
    return LocalNameInfo(stem=stem, date=date, studio=studio, rest=rest)


def format_scene_date(value: str | None) -> str | None:
    """Дата StashDB (`YYYY-MM-DD`, `YYYY-MM`, `YYYY`) в `ДД.ММ.ГГГГ`."""
    if not isinstance(value, str):
        return None
    match = _DATE_FULL.fullmatch(value)
    if match:
        year, month, day = match.groups()
        return f"{day}.{month}.{year}"
    match = _DATE_MONTH.fullmatch(value)
    if match:
        year, month = match.groups()
        return f"{month}.{year}"
    match = _DATE_YEAR.fullmatch(value)
    if match:
        return match.group(1)
    return None


def _is_reserved(stem: str) -> bool:
    """Зарезервированное ли имя (проверка до первой точки, без регистра)."""
    return stem.split(".", 1)[0].strip().upper() in _RESERVED_NAMES


def sanitize_windows_stem(name: str) -> str:
    """Запрещённые символы -> пробел, схлопывание, срез ` .`, reserved -> `_`."""
    s = _ILLEGAL.sub(" ", name)
    s = _WS.sub(" ", s).strip(" .")
    if _is_reserved(s):
        s = "_" + s
    return s


def is_valid_final_stem(stem: str) -> bool:
    """Годится ли stem как имя файла Windows (без расширения)."""
    if not stem or len(stem) > _MAX_STEM_LENGTH:
        return False
    if stem.strip(" .") != stem:
        return False
    if _ILLEGAL.search(stem):
        return False
    return not _is_reserved(stem)


def build_scene_stem(
    scene: Scene, local: LocalNameInfo, studios: dict[str, str]
) -> str:
    """Итоговый stem из официальных полей сцены с локальными fallback-ами."""
    studio = ""
    if scene.studio is not None and (scene.studio.name or "").strip():
        studio = scene.studio.name
    elif local.studio:
        studio = local.studio

    date = format_scene_date(scene.release_date)
    if date is None:
        date = format_scene_date(scene.date)
    if date is None:
        date = local.date

    actors = [p.display_name for p in scene.performers if p.display_name.strip()]
    title = scene.title if (scene.title or "").strip() else local.rest

    return sanitize_windows_stem(build_new_name(studio, date, actors, title))


def build_heuristic_stem(local: LocalNameInfo, studios: dict[str, str]) -> str:
    """Эвристический stem из локального имени (старый конвейер)."""
    return sanitize_windows_stem(clean_stem(local.stem, studios))
