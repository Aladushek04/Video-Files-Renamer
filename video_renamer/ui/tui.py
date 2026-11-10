#!/usr/bin/env python3
"""Красивый TUI для просмотра и применения переименований.

Запуск: `.venv\\Scripts\\python.exe tui.py` или `start_tui.bat`.
Textual нужен только для этого файла; ядро проекта остаётся stdlib.
"""

from __future__ import annotations

import csv
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    OptionList,
    ProgressBar,
    RichLog,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from video_renamer.cli import append_rename_log
from video_renamer.core.clean_ph_names import load_studios
from video_renamer.core.fingerprint_sources import build_fingerprint_clients
from video_renamer.core.rename_log import (
    active_renames,
    format_log_timestamp,
    read_log,
)
from video_renamer.core.stashdb_client import Scene, StashDBClient
from video_renamer.core.title_cache import TitleCache
from video_renamer.core.title_matcher import is_valid_final_stem
from video_renamer.core.title_service import (
    ApplyReport,
    PreviewReport,
    PreviewRow,
    ProgressEvent,
    TitleService,
)
from video_renamer.core.undo import run_undo, select_for_undo
from video_renamer.paths import (
    CACHE_PATH,
    LOG_PATH,
    STUDIOS_PATH,
    load_project_env,
)
from video_renamer.ui.studio_tasks import run as run_studio_task
from video_renamer.ui.tui_model import FILTERS, ReviewModel

SOURCE_LABELS = {
    "fingerprint": "Fingerprint",
    "phash": "PHASH",
    "text": "Текст",
    "heuristic": "Эвристика",
    "unchanged": "Без изменений",
}
SOURCE_STYLES = {
    "fingerprint": "bold #67e8f9",
    "phash": "bold #c4b5fd",
    "text": "bold #fbbf24",
    "heuristic": "#f87171",
    "unchanged": "dim",
}
PHASE_LABELS = {
    "scan": "Сканирую файлы",
    "hash": "Считаю отпечатки",
    "fingerprint": "Ищу в StashDB, ThePornDB и FansDB",
    "phash": "Считаю PHASH",
    "text": "Текстовый поиск",
    "done": "Готово",
}


def _reveal_folder() -> Path:
    """Стартовая папка для окна выбора, если путь ещё не задан."""
    for candidate in (Path.home(), Path.cwd()):
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return Path.cwd()


def _windows_drives() -> list[Path]:
    """Список доступных дисков для навигации с корня."""
    if os.name != "nt":
        return []
    try:
        return [Path(drive) for drive in os.listdrives()]
    except AttributeError:
        drives: list[Path] = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            path = Path(f"{letter}:\\")
            try:
                if path.is_dir():
                    drives.append(path)
            except OSError:
                continue
        return drives


def _approved_text(approved: bool) -> Text:
    if approved:
        return Text("✓", style="bold #34d399")
    return Text("·", style="dim")


class DirectoryPicker(ModalScreen[Path | None]):
    """Модальный выбор папки с навигацией по каталогам и дискам."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Отмена"),
        Binding("backspace", "up", "Вверх"),
    ]

    def __init__(self, start: Path | None = None) -> None:
        super().__init__()
        self.path = self._start_dir(start)

    @staticmethod
    def _start_dir(start: Path | None) -> Path:
        if start is not None:
            try:
                path = Path(start).expanduser()
                if path.is_dir():
                    return path
            except OSError:
                pass
        return _reveal_folder()

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Static("Выберите папку с видео", classes="modal-title")
            yield Static(
                "Enter — открыть папку, Backspace — на уровень выше. "
                "«Выбрать» — взять текущую папку.",
                classes="modal-hint",
            )
            yield Input(value=str(self.path), id="picker-path")
            yield OptionList(id="picker-list")
            with Horizontal(classes="modal-buttons"):
                yield Button("Отмена", id="picker-cancel")
                yield Button("Выбрать", id="picker-choose", variant="primary")

    def on_mount(self) -> None:
        self._reload()
        self.query_one("#picker-list", OptionList).focus()

    def _reload(self) -> None:
        self.query_one("#picker-path", Input).value = str(self.path)
        parent = self.path.parent
        options: list[Option] = []
        if parent != self.path:
            options.append(Option(Text("..  (на уровень выше)"), id=".."))
        for drive in _windows_drives():
            if drive != self.path:
                options.append(Option(Text(str(drive)), id=str(drive)))
        folders: list[str] = []
        try:
            with os.scandir(self.path) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir():
                            folders.append(entry.path)
                    except OSError:
                        continue
        except OSError as exc:
            self.notify(
                f"Не удалось прочитать папку: {exc}",
                severity="error",
                timeout=8,
            )
        folders.sort(key=lambda item: Path(item).name.casefold())
        for folder in folders:
            options.append(Option(Text(Path(folder).name), id=folder))
        if not options:
            options.append(Option(Text("(подпапок нет)"), id=None, disabled=True))
        option_list = self.query_one("#picker-list", OptionList)
        option_list.clear_options()
        option_list.add_options(options)
        if options:
            option_list.highlighted = 0
        option_list.scroll_to_highlight()

    def _navigate(self, path: Path) -> None:
        try:
            path = path.expanduser()
            if not path.is_dir():
                self.notify("Папка не найдена", severity="error")
                return
        except OSError:
            self.notify("Папка не найдена", severity="error")
            return
        self.path = path
        self._reload()
        self.query_one("#picker-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        option_id = event.option_id
        if not option_id:
            return
        if option_id == "..":
            self._navigate(self.path.parent)
        else:
            self._navigate(Path(option_id))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "picker-path":
            return
        raw = event.value.strip().strip('"')
        if raw:
            self._navigate(Path(raw))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "picker-cancel":
            self.dismiss(None)
        elif event.button.id == "picker-choose":
            self.dismiss(self.path)

    def action_up(self) -> None:
        self._navigate(self.path.parent)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmApply(ModalScreen[bool]):
    """Подтверждение реального переименования."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, folder: str, count: int, sources: dict[str, int]) -> None:
        super().__init__()
        self.folder = folder
        self.count = count
        self.sources = sources

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static("Применить переименования?", classes="modal-title")
            yield Static(
                f"Папка:\n{self.folder}\n\n"
                f"Будет переименовано: {self.count}\n"
                f"Fingerprint: {self.sources.get('fingerprint', 0)} · "
                f"Текст: {self.sources.get('text', 0)} · "
                f"Эвристика: {self.sources.get('heuristic', 0)}\n\n"
                "Переименование выполняется сразу, отменить его нельзя. "
                f"Журнал операций: {LOG_PATH}.",
                classes="modal-body",
                id="confirm-body",
            )
            with Horizontal(classes="modal-buttons"):
                yield Button("Отмена", id="confirm-cancel")
                yield Button("Переименовать", id="confirm-apply", variant="success")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-apply")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfirmQuit(ModalScreen[bool]):
    """Подтверждение выхода, если в таблице есть несохранённые строки."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, total: int, approved: int) -> None:
        super().__init__()
        self.total = total
        self.approved = approved

    def compose(self) -> ComposeResult:
        with Vertical(id="quit-dialog"):
            yield Static("Выйти из программы?", classes="modal-title")
            yield Static(
                f"В таблице строк: {self.total}, одобрено: {self.approved}.\n\n"
                "Если выйти сейчас, результаты preview и несохранённые "
                "одобрения будут потеряны. Переименования уже применённых "
                "файлов не отменяются.",
                classes="modal-body",
            )
            with Horizontal(classes="modal-buttons"):
                yield Button("Остаться", id="quit-cancel")
                yield Button("Выйти", id="quit-confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "quit-confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ApplyResultModal(ModalScreen[None]):
    """Итог применения: сколько переименовано и какие ошибки."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "close", "Закрыть")]

    def __init__(
        self,
        report: ApplyReport,
        *,
        log_path: str = "rename_log.csv",
        log_error: str = "",
    ) -> None:
        super().__init__()
        self.report = report
        self.log_path = log_path
        self.log_error = log_error

    def compose(self) -> ComposeResult:
        replaced = sum(1 for r in self.report.results if r.status == "renamed")
        skipped = sum(1 for r in self.report.results if r.status == "skipped")
        errors = [r for r in self.report.results if r.status == "error"]
        with Vertical(id="result-dialog"):
            yield Static("Готово", classes="modal-title")
            yield Static(
                f"Переименовано: {replaced}\n"
                f"Пропущено: {skipped}\n"
                f"Ошибок: {len(errors)}\n\n"
                f"Журнал: {self.log_path}",
                classes="modal-body",
            )
            if self.log_error:
                yield Static(
                    Text(
                        f"Журнал не записан: {self.log_error}",
                        style="#fca5a5",
                    ),
                    id="log-error",
                )
            if errors:
                with VerticalScroll(id="error-list"):
                    for result in errors[:20]:
                        yield Static(
                            Text(
                                f"{result.old_rel} → {result.new_rel}\n{result.message}",
                                style="#fca5a5",
                            )
                        )
            with Horizontal(classes="modal-buttons"):
                yield Button("Закрыть", id="result-close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "result-close":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class CandidatePicker(ModalScreen[Scene | None]):
    """Выбор альтернативной сцены из `PreviewRow.candidates`."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, row: PreviewRow) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        options: list[Option] = []
        seen: set[str] = set()
        for scene in self.row.candidates:
            if scene.id in seen:
                continue
            seen.add(scene.id)
            title = scene.title or "(без названия)"
            studio = scene.studio.name if scene.studio else "—"
            date = scene.release_date or scene.date or "—"
            performers = ", ".join(performer.display_name for performer in scene.performers) or "—"
            label = Text(
                f"{title} — {studio} · {date} · {performers}",
                overflow="ellipsis",
                no_wrap=True,
            )
            options.append(Option(label, id=scene.id))

        with Vertical(id="candidate-dialog"):
            yield Static("Альтернативные кандидаты", classes="modal-title")
            yield Static(
                "Выберите сцену — имя и дата будут взяты из её метаданных.",
                classes="modal-hint",
            )
            if options:
                yield OptionList(*options, id="candidate-list")
            else:
                yield Static("Кандидатов нет.", classes="modal-body")
            with Horizontal(classes="modal-buttons"):
                yield Button("Отмена", id="candidate-cancel")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        scene = next(
            (item for item in self.row.candidates if item.id == event.option_id),
            None,
        )
        self.dismiss(scene)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "candidate-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NameEditor(ModalScreen[str | None]):
    """Ручное редактирование имени файла."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, row: PreviewRow) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        with Vertical(id="name-dialog"):
            yield Static("Новое имя файла", classes="modal-title")
            yield Static(
                "Можно менять только имя, папка и расширение сохраняются.",
                classes="modal-hint",
            )
            yield Input(
                value=os.path.basename(self.row.new_rel),
                id="new-name",
            )
            yield Static("", id="name-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Отмена", id="name-cancel")
                yield Button("Сохранить", id="name-save", variant="primary")

    def _save(self) -> None:
        value = self.query_one("#new-name", Input).value.strip()
        error = self.query_one("#name-error", Static)
        suffix = os.path.splitext(self.row.old_rel)[1]
        if not value:
            error.update(Text("Имя не может быть пустым.", style="#f87171"))
            return
        if "/" in value or "\\" in value:
            error.update(Text("Подпапки запрещены.", style="#f87171"))
            return
        value_suffix = os.path.splitext(value)[1]
        if value_suffix.casefold() != suffix.casefold():
            error.update(
                Text(
                    f"Расширение должно совпадать с исходным: {suffix} (регистр не важен).",
                    style="#f87171",
                )
            )
            return
        if len(value) > 255:
            error.update(Text("Имя длиннее 255 символов.", style="#f87171"))
            return
        if not is_valid_final_stem(os.path.splitext(value)[0]):
            error.update(Text("Недопустимое имя для Windows.", style="#f87171"))
            return
        self.dismiss(value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "new-name":
            self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "name-save":
            self._save()
        elif event.button.id == "name-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class StudioDialog(ModalScreen[None]):
    """Запуск скраперов студий и слияния справочников."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "close", "Закрыть")]

    TASKS: ClassVar[dict[str, str]] = {
        "studio-stashdb": "stashdb",
        "studio-fansdb": "fansdb",
        "studio-theporndb": "theporndb",
        "studio-merge": "merge",
    }

    def __init__(self) -> None:
        super().__init__()
        self._studio_running = False
        self._cancel_event = threading.Event()

    def compose(self) -> ComposeResult:
        with Vertical(id="studio-dialog"):
            yield Static("Справочник студий", classes="modal-title")
            yield Static(
                "StashDB — около 5 минут. FansDB — около часа. "
                "ThePornDB может занять несколько часов. "
                "Прогресс сохраняется; «Отменить» останавливает процесс.",
                classes="modal-hint",
            )
            with Horizontal(classes="studio-buttons"):
                yield Button("StashDB", id="studio-stashdb")
                yield Button("FansDB", id="studio-fansdb")
                yield Button("ThePornDB", id="studio-theporndb")
                yield Button("Слить всё", id="studio-merge", variant="primary")
            yield RichLog(id="studio-log", wrap=True)
            with Horizontal(classes="modal-buttons"):
                yield Button("Отменить", id="studio-cancel", variant="error", disabled=True)
                yield Button("Закрыть", id="studio-close")

    def _set_running(self, running: bool) -> None:
        self._studio_running = running
        for button in self.query(".studio-buttons Button"):
            button.disabled = running
        self.query_one("#studio-close", Button).disabled = running
        self.query_one("#studio-cancel", Button).disabled = not running

    def _start(self, kind: str) -> None:
        if self._studio_running:
            return
        self._cancel_event.clear()
        self._set_running(True)
        log = self.query_one("#studio-log", RichLog)
        log.clear()
        log.write(Text(f"Запуск: {kind}...", style="bold #38bdf8"))
        self._run_worker(kind)

    @work(thread=True, exclusive=True)
    def _run_worker(self, kind: str) -> None:
        try:
            code = run_studio_task(
                kind,
                lambda line: self.app.call_from_thread(self._append_log, line),
                cancel_event=self._cancel_event,
            )
        except Exception as exc:
            self.app.call_from_thread(self._finished, kind, 2, str(exc))
            return
        self.app.call_from_thread(self._finished, kind, code, "")

    def _append_log(self, line: str) -> None:
        try:
            self.query_one("#studio-log", RichLog).write(Text(line))
        except NoMatches:
            pass

    def _finished(self, kind: str, code: int, error: str) -> None:
        self._set_running(False)
        log = self.query_one("#studio-log", RichLog)
        app = self.app
        if code == 130:
            log.write(Text("Отменено", style="#fbbf24"))
            app.notify("Скрапер отменён", severity="warning", timeout=6)
        elif error:
            log.write(Text(f"Ошибка: {error}", style="#f87171"))
            app.notify(f"Студии: ошибка ({code})", severity="error", timeout=6)
        else:
            style = "#34d399" if code == 0 else "#f87171"
            log.write(Text(f"Готово: код {code}", style=style))
            app.notify(
                f"Студии: {kind} завершён" if code == 0 else f"Студии: ошибка ({code})",
                severity="information" if code == 0 else "error",
                timeout=6,
            )
        app.invalidate_services()
        app._update_environment()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "studio-close":
            self.action_close()
            return
        if button_id == "studio-cancel":
            self._cancel_event.set()
            self.query_one("#studio-cancel", Button).disabled = True
            self._append_log("Отмена...")
            return
        kind = self.TASKS.get(button_id or "")
        if kind:
            self._start(kind)

    def on_unmount(self) -> None:
        if self._studio_running:
            self._cancel_event.set()

    def action_close(self) -> None:
        if self._studio_running:
            self.notify("Сначала нажмите «Отменить»", severity="warning")
            return
        self.dismiss(None)


class ConfirmUndo(ModalScreen[bool]):
    """Подтверждение отката: предпросмотр и отметка для строк старого формата."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, body: str, has_legacy: bool) -> None:
        super().__init__()
        self.body_text = body
        self._has_legacy = has_legacy

    def compose(self) -> ComposeResult:
        with Vertical(id="undo-dialog"):
            yield Static(self.body_text, id="undo-body")
            if self._has_legacy:
                yield Checkbox(
                    "подтверждаю откат строк старого формата",
                    id="undo-legacy-ack",
                )
            with Horizontal(id="undo-buttons"):
                yield Button("Отмена", id="undo-cancel")
                yield Button(
                    "Откатить",
                    id="undo-confirm",
                    variant="success",
                    disabled=self._has_legacy,
                )

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "undo-legacy-ack":
            self.query_one("#undo-confirm", Button).disabled = not event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "undo-confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class UndoLogScreen(ModalScreen[None]):
    """Журнал: активные строки, отметки пробелом и откат выбранных."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Закрыть"),
        Binding("space", "toggle", "Отметить"),
    ]

    def __init__(self, log_path: str, folder: str) -> None:
        super().__init__()
        self._log_path = log_path
        self._folder = folder
        self._selected: set[str] = set()
        self._all_entries: list = []
        self._active: dict[str, object] = {}
        self.status_message = ""
        self.failures: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="undo-screen"):
            yield Static(f"Журнал: {self._log_path}", id="undo-title")
            yield DataTable(id="undo-table", cursor_type="row")
            yield Static("", id="undo-status")
            with Horizontal(id="undo-actions"):
                yield Button("Откатить выбранные", id="undo-run", variant="warning")
                yield Button("Закрыть", id="undo-close")

    def on_mount(self) -> None:
        table = self.query_one("#undo-table", DataTable)
        table.add_column("", key="mark")
        table.add_column("id", key="id")
        table.add_column("старое → новое", key="names")
        table.add_column("корень", key="root")
        table.add_column("время", key="time")
        self._reload()

    def _set_status(self, message: str) -> None:
        self.status_message = message
        self.query_one("#undo-status", Static).update(message)

    def _reload(self) -> None:
        table = self.query_one("#undo-table", DataTable)
        table.clear()
        self._active = {}
        try:
            self._all_entries = read_log(self._log_path)
        except (OSError, ValueError, csv.Error) as exc:
            self._set_status(f"Не удалось прочитать журнал: {exc}")
            return
        for entry in active_renames(self._all_entries):
            self._active[entry.id] = entry
            table.add_row(
                self._mark(entry.id),
                entry.id,
                f"{entry.old} → {entry.new}",
                entry.root or "(старый формат)",
                entry.timestamp,
                key=entry.id,
            )
        self._set_status(f"Активных строк: {len(self._active)}")

    def _mark(self, identifier: str) -> str:
        return "[x]" if identifier in self._selected else "[ ]"

    def action_toggle(self) -> None:
        table = self.query_one("#undo-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        identifier = str(row_key.value)
        if identifier in self._selected:
            self._selected.discard(identifier)
        else:
            self._selected.add(identifier)
        table.update_cell(row_key, "mark", self._mark(identifier))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "undo-run":
            self._start_undo()
        elif event.button.id == "undo-close":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)

    def _start_undo(self) -> None:
        if not self._selected:
            self.app.notify("Ничего не выбрано", severity="warning")
            return
        ids = sorted(self._selected)
        has_legacy = any(self._active[i].legacy for i in ids if i in self._active)
        selection = select_for_undo(
            self._all_entries,
            ids,
            root_override=self._folder if has_legacy else None,
            confirm_legacy=True,
        )
        preview = run_undo(selection, log_path=self._log_path, dry_run=True)
        lines = [
            f"{r.old_rel} → {r.new_rel}: {r.status} {r.message}".rstrip() for r in preview.results
        ]
        lines += [f"пропуск: {error}" for error in selection.errors]
        body = "Откат выбранных строк (предпросмотр):\n" + "\n".join(lines)
        if has_legacy:
            body += f"\nКорень для строк старого формата: {self._folder}"
        self.app.push_screen(
            ConfirmUndo(body, has_legacy),
            lambda confirmed: self._undo_confirmed(confirmed, ids, has_legacy),
        )

    def _undo_confirmed(self, confirmed: bool | None, ids: list[str], has_legacy: bool) -> None:
        if confirmed:
            self._run_undo(ids, has_legacy)

    @work(thread=True, exclusive=True)
    def _run_undo(self, ids: list[str], has_legacy: bool) -> None:
        try:
            entries = read_log(self._log_path)
            selection = select_for_undo(
                entries,
                ids,
                root_override=self._folder if has_legacy else None,
                confirm_legacy=has_legacy,
            )
            report = run_undo(selection, log_path=self._log_path, dry_run=False)
        except Exception as exc:
            self.app.call_from_thread(self._undo_failed, str(exc) or exc.__class__.__name__)
            return
        renamed = sum(1 for r in report.results if r.status == "renamed")
        failures = [
            f"{r.id}: {r.message or r.status}"
            for r in report.results
            if r.status not in ("renamed", "dry-run")
        ]
        failures += [f"пропуск: {error}" for error in selection.errors]
        self.app.call_from_thread(self._undo_done, renamed, failures)

    def _undo_done(self, renamed: int, failures: list[str]) -> None:
        if not self.is_attached:
            return
        self.failures = failures
        self._selected.clear()
        self._reload()
        message = f"Откачено: {renamed}, ошибок: {len(failures)}"
        if failures:
            message += "; " + "; ".join(failures[:3])
            self.app.notify(failures[0], severity="error", timeout=10)
        self._set_status(message)

    def _undo_failed(self, message: str) -> None:
        if not self.is_attached:
            return
        self._set_status(f"Откат не выполнен: {message}")
        self.app.notify(message, severity="error", timeout=10)


class RenamerApp(App[None]):
    """Главный экран: папка, preview и таблица ручного ревью."""

    CSS_PATH = "tui.tcss"
    TITLE = "Video Files Renamer"
    SUB_TITLE = "StashDB · FansDB · ThePornDB"
    MIN_WIDTH = 140
    MIN_HEIGHT = 40
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Выход"),
        Binding("p", "preview", "Preview"),
        Binding("b", "browse", "Папка"),
        Binding("space", "toggle", "Одобрить"),
        Binding("a", "apply", "Применить"),
        Binding("c", "candidates", "Кандидаты"),
        Binding("e", "edit", "Имя"),
        Binding("s", "studios", "Студии"),
        Binding("u", "undo_log", "Журнал"),
    ]

    def __init__(
        self,
        folder: str | Path | None = None,
        *,
        preview_factory: Callable[[str, Callable], PreviewReport] | None = None,
        apply_factory: Callable[[list[PreviewRow]], ApplyReport] | None = None,
        undo_log_path: str | None = None,
    ) -> None:
        super().__init__()
        self.folder: Path | None = Path(folder) if folder else None
        self.model = ReviewModel(str(self.folder) if self.folder else "", [])
        self._preview_factory = preview_factory
        self._apply_factory = apply_factory
        self.undo_log_path = undo_log_path or str(LOG_PATH)
        self._busy = False
        self.key = ""
        self._service: TitleService | None = None
        self._size_warned = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("VIDEO FILES\nRENAMER", id="brand")
                yield Static("Папка с видео", classes="field-label")
                yield Input(
                    value=str(self.folder) if self.folder else "",
                    id="folder",
                    placeholder="Выберите папку",
                )
                with Horizontal(id="sidebar-actions"):
                    yield Button("Выбрать", id="browse")
                    yield Button("Preview", id="preview", variant="primary")
                yield Button("Студии", id="studios", classes="wide")
                yield Static("", id="api-status", classes="status")
                yield Static("", id="db-status", classes="status")
                yield Static("", id="stats", classes="stats")
                yield Static("", id="status", classes="status")
            with Vertical(id="main"):
                with Horizontal(id="toolbar"):
                    yield Select(
                        [(label, key) for key, label in FILTERS],
                        value="all",
                        allow_blank=False,
                        id="filter",
                    )
                    yield Button("Кандидаты", id="candidates")
                    yield Button("Имя", id="edit")
                    yield Button("Одобрить", id="approve", variant="success")
                    yield Button("Снять", id="reject", variant="warning")
                    yield Button("Применить", id="apply", variant="error")
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                yield Static("Выберите строку", id="detail")
                yield ProgressBar(total=100, show_eta=False, id="progress")
        yield Footer()

    def on_resize(self, event) -> None:
        too_small = event.size.width < self.MIN_WIDTH or event.size.height < self.MIN_HEIGHT
        if too_small and not self._size_warned:
            self.notify(
                f"Увеличьте окно минимум до {self.MIN_WIDTH}×{self.MIN_HEIGHT} символов",
                severity="warning",
                timeout=8,
            )
            self._size_warned = True
        elif not too_small:
            self._size_warned = False

    def on_mount(self) -> None:
        load_project_env()
        self.key = os.environ.get("STASHDB_API_KEY", "").strip()
        self._update_environment()
        table = self.query_one("#table", DataTable)
        table.add_column("", key="approved", width=3)
        table.add_column("Новое имя", key="new", width=58)
        table.add_column("Источник", key="source", width=12)
        table.add_column("Conf", key="confidence", width=7)
        self.query_one("#progress", ProgressBar).add_class("hidden")
        self._refresh_table()
        self.query_one("#folder", Input).focus()

    def _update_environment(self) -> None:
        key_status = "StashDB: ключ найден" if self.key else "StashDB: ключа нет — offline"
        self.query_one("#api-status", Static).update(Text(key_status))
        studios = STUDIOS_PATH
        if studios.exists():
            size = studios.stat().st_size / (1024 * 1024)
            db_status = f"Справочник: {size:.1f} MB"
        else:
            db_status = "Справочник: studios.json не найден"
        self.query_one("#db-status", Static).update(Text(db_status))

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(Text(text))

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = busy
        for selector in (
            "#preview",
            "#studios",
            "#browse",
            "#approve",
            "#reject",
            "#filter",
        ):
            self.query_one(selector).disabled = busy
        self.query_one("#folder", Input).disabled = busy
        self._update_action_state()
        progress = self.query_one("#progress", ProgressBar)
        if busy:
            progress.remove_class("hidden")
            progress.update(total=100, progress=0)
        else:
            progress.add_class("hidden")
        if text:
            self._set_status(text)

    def _update_progress(self, event: ProgressEvent) -> None:
        total = max(event.total, 1)
        progress = self.query_one("#progress", ProgressBar)
        progress.update(total=total, progress=min(event.current, total))
        label = event.message or PHASE_LABELS.get(event.phase, event.phase)
        self._set_status(f"{label}: {event.current}/{event.total}")

    def _update_stats(self) -> None:
        stats = self.model.stats()
        text = Text()
        text.append(f"Всего: {stats['total']}\n", style="bold")
        text.append(f"Одобрено: {stats['approved']}\n", style="#34d399")
        text.append(f"Ждут ревью: {stats['pending']}\n", style="#fbbf24")
        text.append(
            f"Fingerprint {stats['fingerprint']} · "
            f"Текст {stats['text']} · "
            f"Эвристика {stats['heuristic']}",
            style="dim",
        )
        self.query_one("#stats", Static).update(text)
        self._update_action_state()

    def _update_action_state(self) -> None:
        index = self._selected_index()
        row = self.model.rows[index] if index is not None else None
        self.query_one("#edit", Button).disabled = self._busy or row is None
        self.query_one("#candidates", Button).disabled = (
            self._busy or row is None or not bool(row.candidates)
        )
        self.query_one("#apply", Button).disabled = self._busy or not self.model.approved_rows()

    def _update_detail(self, index: int) -> None:
        row = self.model.rows[index]
        detail = Text()
        detail.append("Было:  ", style="bold #94a3b8")
        detail.append(f"{row.old_rel}\n")
        detail.append("Станет: ", style="bold #94a3b8")
        detail.append(f"{row.new_rel}\n", style="#e2e8f0")
        detail.append("Источник: ", style="bold #94a3b8")
        detail.append(
            f"{SOURCE_LABELS.get(row.source, row.source)} · confidence {row.confidence:.2f}\n",
            style=SOURCE_STYLES.get(row.source, ""),
        )
        if row.note:
            detail.append("Заметка: ", style="bold #94a3b8")
            detail.append(row.note)
        if row.candidates:
            detail.append(
                f"\nКандидатов: {len(row.candidates)} — клавиша c",
                style="dim #67e8f9",
            )
        self.query_one("#detail", Static).update(detail)
        self._update_action_state()

    def _refresh_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        visible = self.model.visible()
        for index, row in visible:
            table.add_row(
                _approved_text(row.approved),
                Text(row.new_rel, overflow="ellipsis", no_wrap=True),
                Text(
                    SOURCE_LABELS.get(row.source, row.source),
                    style=SOURCE_STYLES.get(row.source, ""),
                ),
                f"{row.confidence:.2f}",
                key=str(index),
            )
        if visible:
            table.move_cursor(row=0)
            self._update_detail(visible[0][0])
        else:
            self.query_one("#detail", Static).update(
                Text(
                    "Нет строк. Нажмите Preview, чтобы собрать предложения.",
                    style="dim",
                )
            )
        self._update_stats()

    def _selected_index(self) -> int | None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            return None
        try:
            cell = table.coordinate_to_cell_key((table.cursor_row, 0))
        except Exception:
            return None
        return int(cell.row_key.value)

    def _get_service(self) -> TitleService:
        if self._service is not None:
            return self._service
        studios_path = STUDIOS_PATH
        studios = load_studios(str(studios_path)) if studios_path.exists() else {}
        self._service = TitleService(self.model.folder, studios, None, None)
        return self._service

    def invalidate_services(self) -> None:
        """Сбрасывает справочник после обновления студий."""
        self._service = None

    def _select_index(self, target: int) -> None:
        table = self.query_one("#table", DataTable)
        for position, (index, _row) in enumerate(self.model.visible()):
            if index == target:
                table.move_cursor(row=position)
                self._update_detail(target)
                return

    def _set_folder(self, folder: str) -> None:
        folder = folder.strip().strip('"')
        if not folder:
            return
        path = Path(folder)
        if not path.is_dir():
            self.notify("Папка не найдена", severity="error")
            return
        self.folder = path
        self.model.folder = str(path)
        self.query_one("#folder", Input).value = str(path)
        self._set_status(f"Папка: {path}")

    # --- actions ---------------------------------------------------------

    def action_quit(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if self._busy:
            self.notify("Дождитесь завершения операции", severity="warning")
            return
        if not self.model.rows:
            self.exit()
            return
        self.push_screen(
            ConfirmQuit(len(self.model.rows), len(self.model.approved_rows())),
            self._quit_confirmed,
        )

    def _quit_confirmed(self, confirmed: bool | None) -> None:
        if confirmed:
            self.exit()

    def action_browse(self) -> None:
        if self._busy:
            return
        typed = self.query_one("#folder", Input).value.strip()
        start: Path | None = None
        if typed:
            candidate = Path(typed).expanduser()
            if candidate.is_dir():
                start = candidate
        if start is None and self.folder is not None and self.folder.is_dir():
            start = self.folder
        self.push_screen(DirectoryPicker(start), self._folder_picked)

    def _folder_picked(self, path: Path | None) -> None:
        if path is not None:
            self._set_folder(str(path))

    def action_preview(self) -> None:
        if self._busy:
            return
        folder = self.query_one("#folder", Input).value.strip()
        if not folder or not Path(folder).is_dir():
            self.notify("Выберите существующую папку", severity="error")
            return
        self._set_folder(folder)
        self._set_busy(True, "Загружаю справочник...")
        self._preview_worker(str(self.folder))

    def _preview_worker_impl(
        self, folder: str, progress: Callable[[ProgressEvent], None]
    ) -> PreviewReport:
        if self._preview_factory is not None:
            return self._preview_factory(folder, progress)

        studios_path = STUDIOS_PATH
        studios = load_studios(str(studios_path)) if studios_path.exists() else {}
        cache = TitleCache(str(CACHE_PATH))
        client = StashDBClient(self.key) if self.key else None
        try:
            service = TitleService(
                folder,
                studios,
                cache,
                client,
                offline=not bool(self.key),
                progress=progress,
                extra_clients=build_fingerprint_clients(os.environ, offline=not bool(self.key)),
            )
            self._service = service
            return service.preview()
        finally:
            cache.close()

    @work(thread=True, exclusive=True)
    def _preview_worker(self, folder: str) -> None:
        def progress(event: ProgressEvent) -> None:
            self.call_from_thread(self._update_progress, event)

        try:
            report = self._preview_worker_impl(folder, progress)
        except Exception as exc:
            self.call_from_thread(self._preview_failed, exc)
            return
        self.call_from_thread(self._preview_done, folder, report)

    def _preview_done(self, folder: str, report: PreviewReport) -> None:
        self.model = ReviewModel(folder, list(report.rows))
        self._set_busy(False)
        self._refresh_table()
        self.query_one("#table", DataTable).focus()
        self._set_status(
            f"Готово: {len(report.rows)} строк. Проверьте таблицу и нажмите «Применить»."
        )
        if report.warnings:
            self.notify(
                "Предупреждения: " + " ".join(report.warnings[:2]),
                severity="warning",
                timeout=8,
            )

    def _preview_failed(self, exc: Exception) -> None:
        self._set_busy(False)
        self._set_status("Ошибка preview")
        self.notify(str(exc), severity="error", timeout=10)

    def action_toggle(self) -> None:
        if self._busy:
            return
        index = self._selected_index()
        if index is None:
            return
        updated = self.model.toggle(index)
        table = self.query_one("#table", DataTable)
        if self.model.filter == "pending" and updated.approved:
            self._refresh_table()
        else:
            table.update_cell(str(index), "approved", _approved_text(updated.approved))
            self._update_detail(index)
            self._update_stats()

    def action_undo_log(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.push_screen(UndoLogScreen(self.undo_log_path, str(self.model.folder)))

    def action_studios(self) -> None:
        if self._busy:
            return
        self.push_screen(StudioDialog())

    def action_candidates(self) -> None:
        if self._busy:
            return
        index = self._selected_index()
        if index is None:
            return
        row = self.model.rows[index]
        if not row.candidates:
            self.notify("У этой строки нет альтернативных кандидатов", severity="warning")
            return
        self.push_screen(
            CandidatePicker(row),
            lambda scene: self._candidate_picked(index, scene),
        )

    def _candidate_picked(self, index: int, scene: Scene | None) -> None:
        if scene is None:
            return
        try:
            updated = self._get_service().build_alternative(
                self.model.rows[index], scene, self.model.rows
            )
        except Exception as exc:
            self.notify(f"Не удалось собрать имя: {exc}", severity="error")
            return
        self.model.set_row(index, updated)
        self._refresh_table()
        self._select_index(index)
        self.notify("Кандидат выбран; проверьте имя", timeout=4)

    def action_edit(self) -> None:
        if self._busy:
            return
        index = self._selected_index()
        if index is None:
            return
        self.push_screen(
            NameEditor(self.model.rows[index]),
            lambda value: self._name_edited(index, value),
        )

    def _name_edited(self, index: int, value: str | None) -> None:
        if not value:
            return
        row = self.model.rows[index]
        parent = os.path.dirname(row.old_rel)
        new_rel = os.path.join(parent, value) if parent else value
        self.model.set_new_rel(index, new_rel, approved=True)
        self._refresh_table()
        self._select_index(index)
        self.notify("Имя изменено вручную", timeout=4)

    def action_approve(self) -> None:
        if self._busy:
            return
        index = self._selected_index()
        if index is None:
            return
        self.model.set_approved(index, True)
        self.query_one("#table", DataTable).update_cell(
            str(index), "approved", _approved_text(True)
        )
        self._update_detail(index)
        self._update_stats()

    def action_reject(self) -> None:
        if self._busy:
            return
        index = self._selected_index()
        if index is None:
            return
        self.model.set_approved(index, False)
        self.query_one("#table", DataTable).update_cell(
            str(index), "approved", _approved_text(False)
        )
        self._update_detail(index)
        self._update_stats()

    def action_apply(self) -> None:
        if self._busy:
            return
        approved = self.model.approved_rows()
        if not approved:
            self.notify("Нет одобренных строк", severity="warning")
            return
        sources: dict[str, int] = {}
        for row in approved:
            sources[row.source] = sources.get(row.source, 0) + 1
        self.push_screen(
            ConfirmApply(self.model.folder, len(approved), sources),
            self._apply_confirmed,
        )

    def _apply_confirmed(self, confirmed: bool) -> None:
        if not confirmed:
            return
        self._set_busy(True, "Переименовываю...")
        self._apply_worker(self.model.approved_rows())

    @work(thread=True, exclusive=True)
    def _apply_worker(self, rows: list[PreviewRow]) -> None:
        try:
            report, log_error = self._apply_worker_impl(rows)
        except Exception as exc:
            self.call_from_thread(self._apply_failed, exc)
            return
        self.call_from_thread(self._apply_done, report, log_error)

    def _apply_worker_impl(self, rows: list[PreviewRow]) -> tuple[ApplyReport, str]:
        """Переименовывает строки; сбой журнала не отменяет результат apply."""
        if self._apply_factory is not None:
            return self._apply_factory(rows), ""
        service = TitleService(self.model.folder, {}, None, None)
        report = service.apply_rows(rows, dry_run=False)
        log_error = ""
        if any(result.status == "renamed" for result in report.results):
            try:
                append_rename_log(
                    str(LOG_PATH),
                    rows,
                    report.results,
                    format_log_timestamp(),
                    root=str(self.model.folder),
                )
            except Exception as exc:
                log_error = str(exc) or exc.__class__.__name__
        return report, log_error

    def _apply_done(self, report: ApplyReport, log_error: str = "") -> None:
        self._set_busy(False)
        renamed = {result.old_rel for result in report.results if result.status == "renamed"}
        kept = [row for row in self.model.rows if not row.approved or row.old_rel not in renamed]
        self.model.set_rows(kept)
        self._refresh_table()
        if log_error:
            self._set_status("Переименовано, но журнал не записан")
            self.notify(
                f"Журнал не записан: {log_error}",
                severity="warning",
                timeout=10,
            )
        elif report.had_errors and kept:
            self._set_status(f"Применено: {len(renamed)}; строк с ошибками осталось: {len(kept)}")
        else:
            self._set_status(f"Применено: {len(renamed)}")
        self.push_screen(ApplyResultModal(report, log_path=str(LOG_PATH), log_error=log_error))

    def _apply_failed(self, exc: Exception) -> None:
        self._set_busy(False)
        self._set_status("Ошибка apply")
        self.notify(str(exc), severity="error", timeout=10)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "folder":
            self._set_folder(event.value)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "filter":
            self.model.filter = str(event.value)
            self._refresh_table()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        try:
            self._update_detail(int(event.row_key.value))
        except (TypeError, ValueError):
            pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_toggle()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "browse":
            self.action_browse()
        elif button_id == "preview":
            self.action_preview()
        elif button_id == "studios":
            self.action_studios()
        elif button_id == "candidates":
            self.action_candidates()
        elif button_id == "edit":
            self.action_edit()
        elif button_id == "approve":
            self.action_approve()
        elif button_id == "reject":
            self.action_reject()
        elif button_id == "apply":
            self.action_apply()


def main() -> None:
    folder = sys.argv[1] if len(sys.argv) > 1 else None
    RenamerApp(folder).run()


if __name__ == "__main__":
    main()
