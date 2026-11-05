#!/usr/bin/env python3
"""Сервисы, по которым ищутся фингерпринты (OSHASH, PHASH).

Все три сервиса — stash-box GraphQL с одинаковыми запросами. Порядок в
`FINGERPRINT_SOURCES` задаёт приоритет при выборе между одинаковыми сценами.
Эндпоинты совпадают с теми, что использует скрапер студий
(`video_renamer/scrapers/stashdb.py`).

Зависимостей нет — только стандартная библиотека.
"""
from dataclasses import dataclass

__all__ = [
    "FINGERPRINT_SOURCES",
    "FingerprintSource",
    "PRIMARY_SOURCE_NAME",
]

PRIMARY_SOURCE_NAME = "StashDB"


@dataclass(frozen=True)
class FingerprintSource:
    """Сервис фингерпринтов: имя для отображения, эндпоинт и имя переменной ключа."""

    name: str
    endpoint: str
    api_key_env: str


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
