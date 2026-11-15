"""Пути поставляемых ресурсов и пользовательских данных.

Неизменяемый справочник студий поставляется внутри пакета
(`video_renamer/data/studios.json`) и попадает в wheel. Всё, что программа
меняет — журнал, кэш, скачанные справочники, — лежит в пользовательском
каталоге:

- в checkout (и при editable-установке) это корень проекта, как было раньше:
  существующие `rename_log.csv`, `cache/` и `data/studios.json` не мигрируют;
- в установленном пакете — `%LOCALAPPDATA%\\VideoFilesRenamer` (Windows) или
  `$XDG_DATA_HOME/video-files-renamer`. Каталог установки может быть только
  для чтения, поэтому туда не пишет никто;
- переопределение — переменная `VIDEO_RENAMER_DATA_DIR`.

Чтение справочника: сначала пользовательский `data/studios.json` (его обновляют
скраперы), затем поставляемый ресурс.
"""

from __future__ import annotations

import os
from pathlib import Path

from video_renamer.core.env_file import load_env_file

__all__ = [
    "CACHE_DIR",
    "CACHE_PATH",
    "DATA_DIR",
    "IN_CHECKOUT",
    "LOG_PATH",
    "PACKAGED_STUDIOS_PATH",
    "PACKAGE_DIR",
    "PERFORMERS_INDEX_PATH",
    "PERFORMERS_PATH",
    "PROJECT_ROOT",
    "STUDIOS_PATH",
    "USER_ROOT",
    "load_project_env",
    "resolve_studios_path",
    "same_folder",
    "user_studios_path",
]

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
IN_CHECKOUT = (PROJECT_ROOT / "pyproject.toml").is_file() and (
    PROJECT_ROOT / "video_renamer" / "__init__.py"
).is_file()
PACKAGED_STUDIOS_PATH = PACKAGE_DIR / "data" / "studios.json"


def _default_user_root() -> Path:
    """Каталог изменяемых данных: checkout, переопределение или профиль."""
    override = os.environ.get("VIDEO_RENAMER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if IN_CHECKOUT:
        return PROJECT_ROOT
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "VideoFilesRenamer"
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(base) / "video-files-renamer"
    return Path.home() / ".video-files-renamer"


USER_ROOT = _default_user_root()
DATA_DIR = USER_ROOT / "data"
CACHE_DIR = USER_ROOT / "cache"
CACHE_PATH = CACHE_DIR / "title_fixer.sqlite3"
LOG_PATH = USER_ROOT / "rename_log.csv"
PERFORMERS_PATH = DATA_DIR / "performers.json"
PERFORMERS_INDEX_PATH = DATA_DIR / "performers_index.json"


def user_studios_path() -> Path:
    """Справочник, который обновляют скраперы (может отсутствовать)."""
    return DATA_DIR / "studios.json"


def resolve_studios_path() -> Path:
    """Что читать: обновлённый пользовательский справочник или поставленный."""
    user = user_studios_path()
    if user.is_file():
        return user
    if PACKAGED_STUDIOS_PATH.is_file():
        return PACKAGED_STUDIOS_PATH
    return user


STUDIOS_PATH = resolve_studios_path()


def same_folder(first: str | Path, second: str | Path) -> bool:
    """Указывают ли пути на одну и ту же папку.

    Сравнение нормализует `.`/`..`, `~` и регистр Windows, но не обращается к
    диску: используется для решения, можно ли сохранить ревью при смене папки.
    """
    first_text = os.fspath(first).strip() if first is not None else ""
    second_text = os.fspath(second).strip() if second is not None else ""
    if not first_text or not second_text:
        return False
    return _folder_key(first_text) == _folder_key(second_text)


def _folder_key(path: str) -> str:
    expanded = os.path.expanduser(path)
    return os.path.normcase(os.path.abspath(os.path.normpath(expanded)))


def load_project_env() -> dict[str, str]:
    """Читает `.env` из текущей папки, иначе из корня проекта или профиля.

    Если файла нет, ранее загруженные приложением значения снимаются: удаление
    `.env` не оставляет устаревших ключей в окружении.
    """
    candidates = (Path.cwd() / ".env", PROJECT_ROOT / ".env", USER_ROOT / ".env")
    for candidate in dict.fromkeys(candidates):
        if candidate.is_file():
            return load_env_file(candidate)
    return load_env_file(Path.cwd() / ".env")
