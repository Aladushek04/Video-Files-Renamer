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

Текстовые кандидаты оцениваются по сходству названия, исполнителям, студии и
дате; `resolve_match` выбирает сцену, собирает stem и решает, можно ли
одобрить переименование без человека.

Зависимостей нет — только стандартная библиотека.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from video_renamer.core.clean_ph_names import (
    build_new_name,
    clean_stem,
    prepare_stem,
    prepare_stem_with_names,
    split_actors_title,
    split_name,
)
from video_renamer.core.stashdb_client import Scene
from video_renamer.core.text_norm import normalize_for_match

__all__ = [
    "LocalNameInfo",
    "MatchResult",
    "ScoredScene",
    "build_heuristic_stem",
    "build_scene_stem",
    "format_scene_date",
    "is_valid_final_stem",
    "normalize_for_match",
    "parse_local_name",
    "pick_text_match",
    "resolve_match",
    "sanitize_windows_stem",
    "score_scene",
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

# Веса сигналов скоринга; недоступные сигналы выбрасываются с перенормировкой.
_WEIGHT_TITLE = 0.50
_WEIGHT_PERFORMER = 0.20
_WEIGHT_STUDIO = 0.10
_WEIGHT_DATE = 0.10
_WEIGHT_CODE = 0.10

_TEXT_THRESHOLD = 0.60
_TITLE_GUARD = 0.80
_PERFORMER_GUARD = 0.50
_AMBIGUITY_GAP = 0.05
_TOP_CANDIDATES = 3


@dataclass(frozen=True)
class LocalNameInfo:
    """Разобранное локальное имя.

    `stem` — имя без расширения, `date` — дата в `ДД.ММ.ГГГГ`, `studio` —
    распознанная студия, `rest` — очищенный остаток без даты, студии и
    известных имён исполнителей. `known_actors` — канонические имена,
    найденные в справочнике.
    """

    stem: str
    date: str | None
    studio: str | None
    rest: str
    known_actors: tuple[str, ...] = ()

    @property
    def search_text(self) -> str:
        """Текст для поиска: имена из справочника и остаток без них."""
        return " ".join((*self.known_actors, self.rest)).strip()


def parse_local_name(filename: str, studios: dict[str, str], performers=None) -> LocalNameInfo:
    """Разбирает имя файла: stem без расширения, дата, студия и остаток.

    Если передан справочник имён, известные имена вырезаются до определения
    студии и попадают в `known_actors`.
    """
    stem, _ = split_name(filename)
    if performers is None:
        rest, date, studio = prepare_stem(stem, studios)
        return LocalNameInfo(stem=stem, date=date, studio=studio, rest=rest)
    rest, date, studio, known_actors = prepare_stem_with_names(stem, studios, performers)
    return LocalNameInfo(
        stem=stem,
        date=date,
        studio=studio,
        rest=rest,
        known_actors=tuple(known_actors),
    )


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


def _scene_blocks(scene: Scene, local: LocalNameInfo) -> tuple[str, str, tuple[str, ...], str]:
    """Блоки итогового имени: студия, дата, исполнители, название."""
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

    actors = tuple(p.display_name for p in scene.performers if p.display_name.strip())
    title = scene.title if (scene.title or "").strip() else local.rest
    return studio, date, actors, title


def build_scene_stem(scene: Scene, local: LocalNameInfo, studios: dict[str, str]) -> str:
    """Итоговый stem из официальных полей сцены с локальными fallback-ами."""
    studio, date, actors, title = _scene_blocks(scene, local)
    return sanitize_windows_stem(build_new_name(studio, date, actors, title))


def build_heuristic_stem(local: LocalNameInfo, studios: dict[str, str]) -> str:
    """Эвристический stem из локального имени (старый конвейер)."""
    if local.known_actors:
        actors, title = split_actors_title(local.rest, local.known_actors)
        return sanitize_windows_stem(build_new_name(local.studio, local.date, actors, title))
    return sanitize_windows_stem(clean_stem(local.stem, studios))


def _jaccard(left: str, right: str) -> float:
    """Jaccard по токенам уже нормализованных строк."""
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _remove_phrase(text: str, phrase: str) -> str:
    """Убирает целую фразу из уже нормализованной строки."""
    if not phrase:
        return text
    return _WS.sub(" ", f" {text} ".replace(f" {phrase} ", " ")).strip()


def _title_similarity(scene: Scene, local: LocalNameInfo) -> float:
    """Сходство названий без имён исполнителей и названий студий.

    В локальном имени и официальном названии часто повторяются одни и те же
    актрисы; если их не убрать, правильная сцена получает заниженный score.
    """
    left = normalize_for_match(local.rest)
    right = normalize_for_match(scene.title or "")

    phrases: set[str] = set()
    for performer in scene.performers:
        for name in (performer.display_name, performer.name):
            normalized = normalize_for_match(name)
            if normalized:
                phrases.add(normalized)
    if scene.studio is not None:
        normalized = normalize_for_match(scene.studio.name)
        if normalized:
            phrases.add(normalized)
    if local.studio:
        normalized = normalize_for_match(local.studio)
        if normalized:
            phrases.add(normalized)

    for phrase in phrases:
        left = _remove_phrase(left, phrase)
        right = _remove_phrase(right, phrase)

    if not left or not right:
        return 0.0
    ratio = SequenceMatcher(None, left, right).ratio()
    return max(ratio, _jaccard(left, right))


def _performer_signal(scene: Scene, stem_norm: str) -> tuple[float, bool]:
    """Доля исполнителей сцены, чьё имя или алиас есть в stem."""
    haystack = f" {stem_norm} "
    found = 0
    total = 0
    for performer in scene.performers:
        names = {
            normalize_for_match(performer.display_name),
            normalize_for_match(performer.name),
        }
        names.discard("")
        if not names:
            continue
        total += 1
        if any(f" {name} " in haystack for name in names):
            found += 1
    if total == 0:
        return 0.0, False
    return found / total, True


def _studio_signal(scene: Scene, local: LocalNameInfo, stem_norm: str) -> tuple[float, bool]:
    """1/0: студия сцены совпала с распознанной или встречается в stem."""
    name = ""
    if scene.studio is not None:
        name = normalize_for_match(scene.studio.name)
    if not name:
        return 0.0, False

    compact_name = name.replace(" ", "")
    local_studio = normalize_for_match(local.studio or "")
    if local_studio:
        compact_local = local_studio.replace(" ", "")
        if compact_local == compact_name:
            return 1.0, True
        if compact_local in compact_name or compact_name in compact_local:
            return 0.5, True
    compact_stem = stem_norm.replace(" ", "")
    return (1.0 if compact_name in compact_stem else 0.0), True


def _code_signal(scene: Scene, stem_norm: str) -> tuple[float, bool]:
    """Бонус за code сцены, если он реально встречается в имени файла."""
    code = normalize_for_match(scene.code or "")
    if not code:
        return 0.0, False
    compact_code = code.replace(" ", "")
    if compact_code and compact_code in stem_norm.replace(" ", ""):
        return 1.0, True
    return 0.0, False


def _scene_date(scene: Scene) -> str | None:
    """Дата сцены в `ДД.ММ.ГГГГ` из актуального или устаревшего поля."""
    return format_scene_date(scene.release_date) or format_scene_date(scene.date)


def _date_signal(scene: Scene, local: LocalNameInfo) -> tuple[float, bool]:
    """1 при полной дате, 0.5 при совпадении года, иначе 0."""
    scene_date = _scene_date(scene)
    local_date = local.date
    if not scene_date or not local_date:
        return 0.0, False
    if scene_date == local_date:
        return 1.0, True
    scene_year = scene_date.rsplit(".", 1)[-1]
    local_year = local_date.rsplit(".", 1)[-1]
    if scene_year == local_year:
        return 0.5, True
    return 0.0, True


@dataclass(frozen=True)
class ScoredScene:
    """Сцена с оценкой сходства и её сигналами."""

    scene: Scene
    score: float
    title_sim: float
    performer_hit: float
    studio_hit: float
    date_hit: float
    code_hit: float = 0.0


def score_scene(scene: Scene, local: LocalNameInfo, studios: dict[str, str]) -> ScoredScene:
    """Считает сходство сцены с локальным именем.

    `studios` принимается для единообразия с `resolve_match`; скоринг
    использует уже разобранный `local.studio`. Недоступные сигналы (нет
    названия, исполнителей, студии, даты) выбрасываются, веса оставшихся
    перенормируются.
    """
    stem_norm = normalize_for_match(local.stem)
    title_sim = _title_similarity(scene, local)
    performer_hit, performer_ok = _performer_signal(scene, stem_norm)
    studio_hit, studio_ok = _studio_signal(scene, local, stem_norm)
    date_hit, date_ok = _date_signal(scene, local)
    code_hit, code_ok = _code_signal(scene, stem_norm)

    signals: list[tuple[float, float]] = []
    if normalize_for_match(local.rest) and normalize_for_match(scene.title or ""):
        signals.append((_WEIGHT_TITLE, title_sim))
    if performer_ok:
        signals.append((_WEIGHT_PERFORMER, performer_hit))
    if studio_ok:
        signals.append((_WEIGHT_STUDIO, studio_hit))
    if date_ok:
        signals.append((_WEIGHT_DATE, date_hit))
    if code_ok:
        signals.append((_WEIGHT_CODE, code_hit))

    total_weight = sum(weight for weight, _ in signals)
    score = sum(weight * value for weight, value in signals) / total_weight if total_weight else 0.0
    return ScoredScene(
        scene=scene,
        score=score,
        title_sim=title_sim,
        performer_hit=performer_hit,
        studio_hit=studio_hit,
        date_hit=date_hit,
        code_hit=code_hit,
    )


def pick_text_match(
    scored: Sequence[ScoredScene],
) -> tuple[ScoredScene | None, bool]:
    """Лучший текстовый кандидат и признак неоднозначности.

    Кандидат проходит, если `score >= 0.60` и сработал guard:
    `performer_hit >= 0.5` или `title_sim >= 0.80`. Неоднозначность — разрыв
    с лучшим из оставшихся прошедших кандидатов меньше 0.05.
    """
    eligible = [
        item
        for item in scored
        if item.score >= _TEXT_THRESHOLD
        and (item.performer_hit >= _PERFORMER_GUARD or item.title_sim >= _TITLE_GUARD)
    ]
    if not eligible:
        return None, False
    ranked = sorted(eligible, key=lambda item: (-item.score, item.scene.id))
    best = ranked[0]
    ambiguous = len(ranked) > 1 and best.score - ranked[1].score < _AMBIGUITY_GAP
    return best, ambiguous


@dataclass(frozen=True)
class MatchResult:
    """Результат сопоставления локального имени со сценами StashDB."""

    source: str
    confidence: float
    scene: Scene | None
    candidates: tuple[Scene, ...] = ()
    new_stem: str = ""
    note: str = ""
    approved: bool = False


def _heuristic_stem(local: LocalNameInfo, studios: dict[str, str]) -> str:
    """Эвристический stem, а при пустом результате — исходный stem."""
    return build_heuristic_stem(local, studios) or local.stem


def _has_title(scene: Scene) -> bool:
    return bool((scene.title or "").strip())


def _other_block_count(scene: Scene) -> int:
    """Сколько блоков, кроме названия, даёт сама сцена.

    Локальные fallback-и не считаются: пустая сцена должна уходить в
    эвристику, а не одобряться за счёт данных из имени файла.
    """
    studio = bool(scene.studio and (scene.studio.name or "").strip())
    date = _scene_date(scene) is not None
    actors = any((performer.display_name or "").strip() for performer in scene.performers)
    return sum((studio, date, actors))


def _signals_note(scored: ScoredScene) -> str:
    return (
        f"score={scored.score:.3f}, title_sim={scored.title_sim:.3f}, "
        f"performer_hit={scored.performer_hit:.2f}, "
        f"studio_hit={scored.studio_hit:.1f}, date_hit={scored.date_hit:.1f}, "
        f"code_hit={scored.code_hit:.0f}"
    )


def resolve_match(
    local: LocalNameInfo,
    studios: dict[str, str],
    fingerprint_scenes: Sequence[Scene],
    text_scenes: Sequence[Scene],
    phash_scenes: Sequence[Scene] = (),
) -> MatchResult:
    """Выбирает сцену, собирает итоговый stem и решает, можно ли одобрить.

    Уникальный fingerprint одобряется автоматически только при валидном
    итоговом stem и содержательном имени (название или два других блока).
    PHASH-совпадение всегда требует ревью. Текстовое совпадение используется
    при `score >= 0.60` и пройденном guard, но никогда не одобряется
    автоматически.
    """
    if len(fingerprint_scenes) == 1:
        scene = fingerprint_scenes[0]
        stem = build_scene_stem(scene, local, studios)
        if is_valid_final_stem(stem) and (_has_title(scene) or _other_block_count(scene) >= 2):
            return MatchResult(
                source="fingerprint",
                confidence=1.0,
                scene=scene,
                candidates=(scene,),
                new_stem=stem,
                note="",
                approved=True,
            )
        return MatchResult(
            source="fingerprint",
            confidence=1.0,
            scene=scene,
            candidates=(scene,),
            new_stem=_heuristic_stem(local, studios),
            note="имя сцены неполное, heuristic fallback",
            approved=False,
        )

    if len(fingerprint_scenes) >= 2:
        scored = sorted(
            (score_scene(scene, local, studios) for scene in fingerprint_scenes),
            key=lambda item: (-item.score, item.scene.id),
        )
        best = scored[0]
        stem = build_scene_stem(best.scene, local, studios)
        note = "совпадения fingerprint: " + ", ".join(
            f"{item.scene.id}={item.score:.2f}" for item in scored
        )
        if not is_valid_final_stem(stem):
            stem = _heuristic_stem(local, studios)
            note += "; имя сцены неполное, heuristic fallback"
        return MatchResult(
            source="fingerprint",
            confidence=0.99,
            scene=best.scene,
            candidates=tuple(item.scene for item in scored),
            new_stem=stem,
            note=note,
            approved=False,
        )

    if phash_scenes:
        ordered = list(phash_scenes)
        best = ordered[0]
        stem = build_scene_stem(best, local, studios)
        if len(ordered) == 1:
            note = f"phash: {best.source} {best.id}; требует ревью"
        else:
            note = (
                "phash совпадения: "
                + ", ".join(f"{scene.source} {scene.id}" for scene in ordered)
                + "; требует ревью"
            )
        if not is_valid_final_stem(stem):
            stem = _heuristic_stem(local, studios)
            note += "; имя сцены неполное, heuristic fallback"
        return MatchResult(
            source="phash",
            confidence=0.9,
            scene=best,
            candidates=tuple(ordered),
            new_stem=stem,
            note=note,
            approved=False,
        )

    scored_text = sorted(
        (score_scene(scene, local, studios) for scene in text_scenes),
        key=lambda item: (-item.score, item.scene.id),
    )
    best, ambiguous = pick_text_match(scored_text)
    if best is not None:
        stem = build_scene_stem(best.scene, local, studios)
        note = "ambiguous" if ambiguous else "text: " + _signals_note(best)
        if not is_valid_final_stem(stem):
            stem = _heuristic_stem(local, studios)
            if not ambiguous:
                note += "; имя сцены неполное, heuristic fallback"
        return MatchResult(
            source="text",
            confidence=best.score,
            scene=best.scene,
            candidates=tuple(item.scene for item in scored_text[:_TOP_CANDIDATES]),
            new_stem=stem,
            note=note,
            approved=False,
        )

    top = scored_text[:_TOP_CANDIDATES]
    if not scored_text:
        reason = "нет текстовых кандидатов"
    elif scored_text[0].score < _TEXT_THRESHOLD:
        reason = f"лучший score {scored_text[0].score:.2f} < {_TEXT_THRESHOLD:.2f}"
    else:
        reason = "текстовый guard не пройден"
    if top:
        reason += "; top: " + ", ".join(f"{item.scene.id}={item.score:.2f}" for item in top)
    return MatchResult(
        source="heuristic",
        confidence=0.0,
        scene=None,
        candidates=tuple(item.scene for item in top),
        new_stem=_heuristic_stem(local, studios),
        note=reason,
        approved=False,
    )
