"""Минимальный stdlib-загрузчик `.env` без внешних зависимостей."""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

__all__ = ["load_env_file", "reset_tracking"]

# Значения, которые поставил сам загрузчик: по ним видно, можно ли обновить
# переменную при повторном чтении файла и нужно ли убрать её после удаления.
# Ключ — сам mapping по идентичности, чтобы не путать `os.environ` и словари
# из тестов.
_APPLIED: list[tuple[MutableMapping[str, str], dict[str, str]]] = []


def _applied_for(env: MutableMapping[str, str]) -> dict[str, str]:
    for mapping, applied in _APPLIED:
        if mapping is env:
            return applied
    applied: dict[str, str] = {}
    _APPLIED.append((env, applied))
    return applied


def reset_tracking(environ: MutableMapping[str, str] | None = None) -> None:
    """Забывает, какие значения поставило приложение.

    Нужно тестам: окружение восстанавливается между ними, а запись о
    происхождении переменных иначе осталась бы от предыдущего сценария.
    """
    global _APPLIED
    if environ is None:
        _APPLIED = []
        return
    _APPLIED = [(mapping, applied) for mapping, applied in _APPLIED if mapping is not environ]


def _candidate_paths(path: str | os.PathLike[str] | None) -> list[Path]:
    if path is not None:
        return [Path(path)]
    return [Path.cwd() / ".env"]


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        return value[1:-1]
    return value


def _strip_inline_comment(value: str) -> str:
    for marker in (" #", "\t#"):
        index = value.find(marker)
        if index != -1:
            return value[:index].rstrip()
    return value


def load_env_file(
    path: str | os.PathLike[str] | None = None,
    *,
    override: bool = False,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Читает `KEY=VALUE` из `.env` и применяет значения к окружению.

    Приоритет: внешняя переменная важнее файла. Если переменную поставило само
    приложение при прошлом чтении, повторное чтение обновляет её из файла, а
    удалённая строка (или удалённый файл) убирает значение — устаревшего
    состояния не остаётся. Внешние значения не трогаются, даже если файл
    изменился; `override=True` перезаписывает всё явно.

    Если путь не задан, ищется `.env` в текущей папке. Возвращает только
    реально разобранные пары.
    """
    env = os.environ if environ is None else environ
    applied = _applied_for(env)
    target = next((candidate for candidate in _candidate_paths(path) if candidate.is_file()), None)

    loaded: dict[str, str] = {}
    if target is not None:
        for raw in target.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key[0].isdigit() or not key.replace("_", "").isalnum():
                continue

            value = value.strip()
            if value.startswith(("'", '"')):
                value = _unquote(value)
            else:
                value = _strip_inline_comment(value)

            loaded[key] = value

    for key, value in loaded.items():
        current = env.get(key)
        if override or key not in env or applied.get(key) == current:
            env[key] = value
            applied[key] = value

    # Строку убрали из файла или файл исчез: снимаем только свои значения,
    # подменённые пользователем переменные остаются.
    for key in list(applied):
        if key in loaded:
            continue
        if env.get(key) == applied[key]:
            del env[key]
        del applied[key]

    return loaded
