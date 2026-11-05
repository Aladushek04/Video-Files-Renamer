#!/usr/bin/env python3
"""Сервисы, по которым ищутся фингерпринты (OSHASH, PHASH).

Все три сервиса — stash-box GraphQL с одинаковыми запросами. Порядок в
`FINGERPRINT_SOURCES` задаёт приоритет при выборе между одинаковыми сценами.
Эндпоинты совпадают с теми, что использует скрапер студий
(`video_renamer/scrapers/stashdb.py`).

Зависимостей нет — только стандартная библиотека.
"""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from video_renamer.core.stashdb_client import Scene, StashDBClient
from video_renamer.core.title_matcher import normalize_for_match

__all__ = [
    "FINGERPRINT_SOURCES",
    "FingerprintSource",
    "PRIMARY_SOURCE_NAME",
    "build_fingerprint_clients",
    "merge_scenes",
]

PRIMARY_SOURCE_NAME = "StashDB"


@dataclass(frozen=True)
class FingerprintSource:
    """Сервис фингерпринтов: имя для отображения, эндпоинт и имя переменной ключа."""

    name: str
    endpoint: str
    api_key_env: str


def _dedupe_key(scene: Scene) -> tuple[str, str, str]:
    """Ключ одной и той же сцены: название, дата и студия после нормализации."""
    studio = scene.studio.name if scene.studio is not None else ""
    return (
        normalize_for_match(scene.title or ""),
        scene.release_date or scene.date or "",
        normalize_for_match(studio or ""),
    )


def merge_scenes(per_source: Sequence[Sequence[Scene]]) -> list[Scene]:
    """Объединяет сцены источников; одинаковые сцены остаются из раннего источника.

    Порядок результата: сцены первого источника в исходном порядке, затем новые
    из следующих. Одинаковой считается сцена с тем же ключом `_dedupe_key`.
    Сцены без названия не объединяются: по пустому ключу нельзя доказать, что
    это одна и та же сцена.
    """
    seen: set[tuple[str, str, str]] = set()
    merged: list[Scene] = []
    for scenes in per_source:
        for scene in scenes:
            key = _dedupe_key(scene)
            if key[0] and key in seen:
                continue
            seen.add(key)
            merged.append(scene)
    return merged


def build_fingerprint_clients(
    env: Mapping[str, str], *, offline: bool
) -> dict[str, StashDBClient]:
    """Клиенты вторичных источников; без ключа источник пропускается.

    Клиент StashDB сюда не входит: он создаётся вызывающим кодом. В офлайн-режиме
    сеть не нужна, клиенты не создаются.
    """
    if offline:
        return {}
    clients: dict[str, StashDBClient] = {}
    for source in FINGERPRINT_SOURCES[1:]:
        key = (env.get(source.api_key_env) or "").strip()
        if key:
            clients[source.name] = StashDBClient(key, endpoint=source.endpoint)
    return clients


FINGERPRINT_SOURCES: tuple[FingerprintSource, ...] = (
    FingerprintSource(
        name="StashDB",
        endpoint="https://stashdb.org/graphql",
        api_key_env="STASHDB_API_KEY",
    ),
    FingerprintSource(
        name="ThePornDB",
        endpoint="https://theporndb.net/graphql",
        api_key_env="THE_PORNDB_API_KEY",
    ),
    FingerprintSource(
        name="FansDB",
        endpoint="https://fansdb.cc/graphql",
        api_key_env="FANSDB_API_KEY",
    ),
)
