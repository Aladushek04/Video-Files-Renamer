#!/usr/bin/env python3
"""Журнал переименований `rename_log.csv`: формат, идентификаторы и состояние строк.

Каждая строка — одно переименование (`rename`) или откат (`undo`). Откат не
удаляет строку, а дописывает новую со ссылкой `undoes` на отменённую строку.
Состояние вычисляется при чтении: `active_renames` возвращает строки, которые
ещё не откатены.

Старый формат (`timestamp;old;new;oshash;scene_id;source`) читается как строки
без корня; при первой записи в новом формате старый файл переносится в
`rename_log.legacy*.csv` рядом с ним.

Зависимостей нет — только стандартная библиотека.
"""
import csv
import glob
import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

__all__ = [
    "LogEntry",
    "LogMigrationError",
    "active_renames",
    "append_log",
    "entry_id",
    "format_log_timestamp",
    "read_log",
]

LOG_FIELDS = (
    "timestamp", "action", "undoes", "root", "old", "new",
    "oshash", "size", "scene_id", "source",
)
LEGACY_FIELDS = ("timestamp", "old", "new", "oshash", "scene_id", "source")
ACTION_RENAME = "rename"
ACTION_UNDO = "undo"
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class LogMigrationError(OSError):
    """Старый журнал не удалось перенести; новая запись не выполнена."""


@dataclass(frozen=True)
class LogEntry:
    """Одна строка журнала. `size` — `None`, если размер не записан."""

    timestamp: str
    action: str
    undoes: str
    root: str
    old: str
    new: str
    oshash: str
    size: int | None
    scene_id: str
    source: str
    legacy: bool

    @property
    def id(self) -> str:
        return entry_id(self.timestamp, self.action, self.root, self.old, self.new)


def _guard_formula(value: str) -> str:
    """Экранирует значение, которое Excel принял бы за формулу.

    Если значение уже начинается с апострофа, за которым идёт формульный
    префикс, добавляется ещё один апостроф: так настоящий апостроф в имени
    не путается с защитным.
    """
    if value.startswith(_FORMULA_PREFIXES) or (
        value.startswith("'")
        and value.lstrip("'").startswith(_FORMULA_PREFIXES)
    ):
        return "'" + value
    return value


def _unguard_formula(value: str) -> str:
    """Снимает ровно один защитный апостроф, добавленный `_guard_formula`."""
    if (value.startswith("'")
            and value.lstrip("'").startswith(_FORMULA_PREFIXES)):
        return value[1:]
    return value


def format_log_timestamp(moment: datetime | None = None) -> str:
    """Метка времени журнала: UTC с микросекундами, например `2026-11-06T10:00:00.000005Z`."""
    if moment is None:
        moment = datetime.now(timezone.utc)
    elif moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def entry_id(timestamp: str, action: str, root: str, old: str, new: str) -> str:
    """Идентификатор строки: 10 hex-символов от её полей."""
    raw = f"{timestamp}|{action}|{root}|{old}|{new}".encode()
    return hashlib.sha1(raw).hexdigest()[:10]


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _parse_size(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _read_file(path: str) -> list[LogEntry]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        header = next(reader, None)
        if header is None:
            return []
        header = tuple(header)
        if header == LOG_FIELDS:
            legacy = False
        elif header == LEGACY_FIELDS:
            legacy = True
        else:
            raise ValueError(f"unknown rename log format: {path}")
        entries: list[LogEntry] = []
        for record in reader:
            if not record or not any(cell.strip() for cell in record):
                continue
            data = dict(zip(header, record))
            if legacy:
                entries.append(LogEntry(
                    timestamp=data.get("timestamp", ""),
                    action=ACTION_RENAME,
                    undoes="",
                    root="",
                    old=_unguard_formula(data.get("old", "")),
                    new=_unguard_formula(data.get("new", "")),
                    oshash=data.get("oshash", ""),
                    size=None,
                    scene_id=data.get("scene_id", ""),
                    source=data.get("source", ""),
                    legacy=True,
                ))
            else:
                entries.append(LogEntry(
                    timestamp=data.get("timestamp", ""),
                    action=data.get("action", "") or ACTION_RENAME,
                    undoes=data.get("undoes", ""),
                    root=data.get("root", ""),
                    old=_unguard_formula(data.get("old", "")),
                    new=_unguard_formula(data.get("new", "")),
                    oshash=data.get("oshash", ""),
                    size=_parse_size(data.get("size", "")),
                    scene_id=data.get("scene_id", ""),
                    source=data.get("source", ""),
                    legacy=False,
                ))
    return entries


def _legacy_paths(path: str) -> list[str]:
    """Файлы старого журнала рядом с `path`: `<имя>.legacy*<расширение>`."""
    stem, ext = os.path.splitext(os.path.basename(path))
    folder = os.path.dirname(os.path.abspath(path))
    return sorted(glob.glob(os.path.join(glob.escape(folder), f"{stem}.legacy*{ext}")))


def read_log(path: str) -> list[LogEntry]:
    """Все строки журнала: сам `path` и перенесённые старые файлы рядом с ним."""
    entries: list[LogEntry] = []
    for candidate in _legacy_paths(path) + [path]:
        if os.path.isfile(candidate):
            entries.extend(_read_file(candidate))
    return entries


def active_renames(entries: Sequence[LogEntry]) -> list[LogEntry]:
    """Строки `rename`, которые не откатены записью `undo`."""
    undone = {entry.undoes for entry in entries if entry.action == ACTION_UNDO}
    return [
        entry for entry in entries
        if entry.action == ACTION_RENAME and entry.id not in undone
    ]


def _is_legacy_file(path: str) -> bool:
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    with open(path, encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle, delimiter=";"), None)
    return header is not None and tuple(header) == LEGACY_FIELDS


def _refuse_unknown_header(path: str) -> None:
    """Не дописывать строки в файл с чужим заголовком: он стал бы нечитаемым."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return
    with open(path, encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle, delimiter=";"), None)
    if header is None or tuple(header) != LOG_FIELDS:
        raise LogMigrationError(f"unknown rename log format, not appending: {path}")


def _legacy_target(path: str) -> str:
    """Свободное имя для старого журнала: `.legacy.csv`, затем с датой и номером."""
    stem, ext = os.path.splitext(path)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    candidates = [f"{stem}.legacy{ext}", f"{stem}.legacy-{day}{ext}"]
    candidates += [f"{stem}.legacy-{day}-{number}{ext}" for number in range(2, 1000)]
    return next(candidate for candidate in candidates if not os.path.exists(candidate))


def _migrate_legacy(path: str) -> None:
    if not _is_legacy_file(path):
        return
    target = _legacy_target(path)
    try:
        os.replace(path, target)
    except OSError as exc:
        raise LogMigrationError(
            f"cannot move legacy rename log {path} to {target}: {exc}"
        ) from exc


def append_log(path: str, entries: Sequence[LogEntry]) -> None:
    """Дописывает строки в журнал нового формата.

    Старый файл переносится в `rename_log.legacy*.csv`; если это не удалось,
    запись прерывается с `LogMigrationError` и файл не меняется.
    """
    if not entries:
        return
    _migrate_legacy(path)
    _refuse_unknown_header(path)
    exists = os.path.isfile(path) and os.path.getsize(path) > 0
    _ensure_parent(path)
    mode = "a" if exists else "w"
    encoding = "utf-8" if exists else "utf-8-sig"
    with open(path, mode, encoding=encoding, newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        if not exists:
            writer.writerow(LOG_FIELDS)
        for entry in entries:
            writer.writerow((
                entry.timestamp,
                entry.action,
                entry.undoes,
                entry.root,
                _guard_formula(entry.old),
                _guard_formula(entry.new),
                entry.oshash,
                "" if entry.size is None else str(entry.size),
                entry.scene_id,
                entry.source,
            ))
