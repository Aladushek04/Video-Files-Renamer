"""Минимальный stdlib-загрузчик `.env` без внешних зависимостей."""
from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path


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
    """Читает `KEY=VALUE` из `.env` и добавляет значения в окружение.

    По умолчанию уже существующие переменные не перезаписываются. Если путь
    не задан, ищется `.env` в текущей папке. Возвращает только реально
    разобранные пары.
    """
    env = os.environ if environ is None else environ
    target = next((candidate for candidate in _candidate_paths(path)
                   if candidate.is_file()), None)
    if target is None:
        return {}

    loaded: dict[str, str] = {}
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
        if override or key not in env:
            env[key] = value

    return loaded
