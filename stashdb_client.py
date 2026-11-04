#!/usr/bin/env python3
"""Модели данных StashDB и разбор ответов GraphQL.

Модуль содержит доменные модели (`Studio`, `Performer`, `Scene`) и функции,
превращающие узел ответа StashDB в эти модели. Сетевой GraphQL-клиент
добавляется в этот же файл отдельным заданием.

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
from dataclasses import dataclass

__all__ = [
    "Performer",
    "Scene",
    "Studio",
    "scene_from_node",
    "studio_from_node",
]


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
        )


def studio_from_node(node: dict) -> Studio:
    """Студия из узла GraphQL `{id, name, parent {id, name}}`."""
    parent = node.get("parent") or None
    return Studio(
        id=node.get("id") or "",
        name=node.get("name") or "",
        parent_id=(parent.get("id") or None) if parent else None,
        parent_name=(parent.get("name") or None) if parent else None,
    )


def _performer_from_entry(entry: dict) -> Performer:
    """Исполнитель из элемента списка `{as, performer {id, name}}`."""
    performer = entry.get("performer") or {}
    return Performer(
        id=performer.get("id") or "",
        name=performer.get("name") or "",
        alias=entry.get("as") or None,
    )


def scene_from_node(node: dict) -> Scene:
    """Сцена из узла GraphQL.

    Отсутствующие поля остаются `None`, `studio` может быть `None`,
    пустой `performers` даёт пустой кортеж.
    """
    studio_node = node.get("studio") or None
    performers = node.get("performers") or []
    return Scene(
        id=node.get("id") or "",
        title=node.get("title"),
        release_date=node.get("release_date"),
        date=node.get("date"),
        code=node.get("code"),
        duration=node.get("duration"),
        studio=studio_from_node(studio_node) if studio_node else None,
        performers=tuple(_performer_from_entry(e) for e in performers),
    )
