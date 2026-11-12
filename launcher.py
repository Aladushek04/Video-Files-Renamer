#!/usr/bin/env python3
"""Запуск GUI с безопасной проверкой обновлений в Git.

Порядок:
1. `git fetch` текущей ветки (короткий таймаут, ошибки не фатальны).
2. Если ветка отстала и рабочее дерево чистое — `git pull --ff-only`.
3. Импорт GUI и запуск. Любая проблема с Git только пишется в консоль/диалог
   и никогда не мешает запуску приложения.

Запуск: `.venv\\Scripts\\python.exe launcher.py [папка] [--no-update]`
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GIT_TIMEOUT = 20
UPDATE_ENV = "VIDEO_RENAMER_UPDATE_STATUS"


@dataclass(frozen=True)
class UpdateResult:
    status: str
    message: str


def _git(
    root: Path,
    args: list[str],
    *,
    timeout: int = GIT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=flags,
        check=False,
    )


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def check_for_updates(
    root: Path | None = None,
    *,
    timeout: int = GIT_TIMEOUT,
    enabled: bool = True,
) -> UpdateResult:
    """Проверяет и, если безопасно, применяет fast-forward обновление."""
    root = (root or ROOT).resolve()
    if not enabled:
        return UpdateResult("skipped", "автообновление отключено")

    if shutil.which("git") is None:
        return UpdateResult("skipped", "git не найден — запуск без обновления")

    try:
        inside = _git(root, ["rev-parse", "--is-inside-work-tree"], timeout=5)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return UpdateResult("skipped", "папка не является git-репозиторием")
        branch_result = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
        branch = branch_result.stdout.strip()
        if branch_result.returncode != 0 or not branch or branch == "HEAD":
            return UpdateResult("skipped", "не удалось определить текущую ветку")
        upstream_result = _git(
            root,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            timeout=5,
        )
        upstream = upstream_result.stdout.strip()
        if upstream_result.returncode != 0 or "/" not in upstream:
            return UpdateResult("skipped", f"у ветки {branch} нет upstream")
        remote = upstream.split("/", 1)[0]

        fetch = _git(root, ["fetch", "--quiet", "--prune", remote], timeout=timeout)
        if fetch.returncode != 0:
            return UpdateResult(
                "error",
                f"не удалось проверить обновления: {_last_line(fetch.stderr) or 'git fetch failed'}",
            )

        behind = _git(root, ["rev-list", "--count", f"HEAD..{upstream}"], timeout=5)
        if behind.returncode != 0:
            return UpdateResult("error", "не удалось сравнить локальную и удалённую ветки")
        try:
            commits = int(behind.stdout.strip() or "0")
        except ValueError:
            commits = 0
        if commits <= 0:
            head = _git(root, ["rev-parse", "--short", "HEAD"], timeout=5)
            return UpdateResult(
                "up-to-date",
                f"уже актуально ({branch} @ {head.stdout.strip() or 'unknown'})",
            )

        dirty = _git(root, ["status", "--porcelain", "--untracked-files=no"], timeout=5)
        if dirty.returncode != 0:
            return UpdateResult("error", "не удалось проверить локальные изменения")
        if dirty.stdout.strip():
            return UpdateResult(
                "skipped",
                f"есть локальные изменения — пропущено {commits} коммитов",
            )

        pull = _git(
            root,
            ["pull", "--ff-only", "--quiet", remote, branch],
            timeout=timeout,
        )
        if pull.returncode != 0:
            return UpdateResult(
                "error",
                f"git pull не выполнен: {_last_line(pull.stderr) or 'non-fast-forward'}",
            )
        head = _git(root, ["rev-parse", "--short", "HEAD"], timeout=5)
        return UpdateResult(
            "updated",
            f"обновлено: +{commits} коммитов ({branch} @ {head.stdout.strip() or 'unknown'})",
        )
    except subprocess.TimeoutExpired:
        return UpdateResult("error", "git не ответил вовремя — запуск без обновления")
    except OSError as exc:
        return UpdateResult("error", f"git недоступен: {exc}")


def _configure_console() -> None:
    """UTF-8 в консоли Windows, иначе русские сообщения падают в cp1251/cp1252."""
    stream = sys.stdout
    if stream is not None and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _report(result: UpdateResult, *, dialog: bool) -> None:
    """Пишет статус в консоль и передаёт его GUI; при pythonw — в диалог."""
    os.environ[UPDATE_ENV] = result.message
    if sys.stdout is not None:
        try:
            print(f"[update] {result.message}", flush=True)
        except (OSError, UnicodeError):
            pass
    if dialog or sys.stdout is None:
        _show_dialog(result)


def _show_dialog(result: UpdateResult) -> None:
    icon = 0x10 if result.status == "error" else 0x40
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0,
            result.message,
            "Video Files Renamer · обновление",
            icon,
        )
    except Exception:
        pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launcher.py",
        description="Проверить обновления Git и запустить GUI Video Files Renamer.",
    )
    parser.add_argument("folder", nargs="?", default=None, help="папка с видео")
    parser.add_argument(
        "--no-update",
        action="store_true",
        help="не проверять обновления репозитория",
    )
    parser.add_argument(
        "--update-dialog",
        action="store_true",
        help="показать статус обновления в диалоговом окне",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    args = _build_parser().parse_args(argv)
    skip = args.no_update or os.environ.get("VIDEO_RENAMER_SKIP_UPDATE", "").strip() in {
        "1",
        "true",
        "yes",
    }
    result = check_for_updates(enabled=not skip)
    _report(result, dialog=args.update_dialog)

    sys.path.insert(0, str(ROOT))
    try:
        from video_renamer.gui.app import main as gui_main
    except ImportError as exc:
        message = (
            "Не удалось загрузить GUI. Установите зависимости:\n"
            "  .venv\\Scripts\\python.exe -m pip install -r requirements\\gui.txt\n\n"
            f"{exc}"
        )
        if sys.stdout is not None:
            try:
                print(message, flush=True)
            except (OSError, UnicodeError):
                pass
        _show_dialog(UpdateResult("error", message))
        return 1
    return gui_main(args.folder)


if __name__ == "__main__":
    raise SystemExit(main())
