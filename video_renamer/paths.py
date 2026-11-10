"""Пути данных проекта, не зависящие от текущей папки запуска.

Пакет может запускаться из любого каталога (`video-renamer-tui`, ярлык,
другой диск). Справочник студий, кэш и журнал TUI должны при этом оставаться
рядом с проектом, а не появляться в случайной папке.
"""

from __future__ import annotations

from pathlib import Path

from video_renamer.core.env_file import load_env_file

__all__ = [
    "CACHE_PATH",
    "LOG_PATH",
    "PROJECT_ROOT",
    "STUDIOS_PATH",
    "load_project_env",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STUDIOS_PATH = PROJECT_ROOT / "data" / "studios.json"
CACHE_PATH = PROJECT_ROOT / "cache" / "title_fixer.sqlite3"
LOG_PATH = PROJECT_ROOT / "rename_log.csv"


def load_project_env() -> dict[str, str]:
    """Читает `.env` из текущей папки, иначе из корня проекта."""
    candidates = (Path.cwd() / ".env", PROJECT_ROOT / ".env")
    for candidate in dict.fromkeys(candidates):
        if candidate.is_file():
            return load_env_file(candidate)
    return {}
