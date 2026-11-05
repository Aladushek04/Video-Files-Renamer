#!/usr/bin/env python3
"""Красивый TUI для просмотра и применения переименований.

Запуск: `.venv\\Scripts\\python.exe tui.py` или `start_tui.bat`.
Textual нужен только для этого файла; ядро проекта остаётся stdlib.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    ProgressBar,
    Select,
    Static,
)

from video_renamer.core.clean_ph_names import load_studios
from video_renamer.core.env_file import load_env_file
from video_renamer.cli import append_rename_log
from video_renamer.core.stashdb_client import StashDBClient
from video_renamer.core.title_cache import TitleCache
from video_renamer.core.title_service import (
    ApplyReport,
    PreviewReport,
    PreviewRow,
    ProgressEvent,
    TitleService,
)
from video_renamer.tui_model import FILTERS, ReviewModel

SOURCE_LABELS = {
    "fingerprint": "Fingerprint",
    "text": "Текст",
    "heuristic": "Эвристика",
    "unchanged": "Без изменений",
}
SOURCE_STYLES = {
    "fingerprint": "bold #67e8f9",
    "text": "bold #fbbf24",
    "heuristic": "#f87171",
    "unchanged": "dim",
}
PHASE_LABELS = {
    "scan": "Сканирую файлы",
    "hash": "Считаю отпечатки",
    "fingerprint": "Ищу в StashDB",
    "text": "Текстовый поиск",
    "done": "Готово",
}


def _default_folder() -> Path:
    preferred = Path(r"D:\Videos\PH")
    return preferred if preferred.is_dir() else Path.cwd()


def _approved_text(approved: bool) -> Text:
    if approved:
        return Text("✓", style="bold #34d399")
    return Text("·", style="dim")


class DirectoryPicker(ModalScreen[Path | None]):
    """Модальный выбор папки через дерево каталогов."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, start: Path) -> None:
        super().__init__()
        self.start = start

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Static("Выберите папку с видео", classes="modal-title")
            yield Static(
                "Enter — выбрать подсвеченную папку",
                classes="modal-hint",
            )
            yield DirectoryTree(str(self.start), id="picker-tree")
            with Horizontal(classes="modal-buttons"):
                yield Button("Отмена", id="picker-cancel")

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected
    ) -> None:
        self.dismiss(event.path)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "picker-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmApply(ModalScreen[bool]):
    """Подтверждение реального переименования."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

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
                "Отменить это будет нельзя без rename_log.csv.",
                classes="modal-body",
            )
            with Horizontal(classes="modal-buttons"):
                yield Button("Отмена", id="confirm-cancel")
                yield Button(
                    "Переименовать", id="confirm-apply", variant="success"
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-apply")

    def action_cancel(self) -> None:
        self.dismiss(False)


class ApplyResultModal(ModalScreen[None]):
    """Итог применения: сколько переименовано и какие ошибки."""

    BINDINGS = [Binding("escape", "close", "Закрыть")]

    def __init__(self, report: ApplyReport) -> None:
        super().__init__()
        self.report = report

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
                f"Журнал: rename_log.csv",
                classes="modal-body",
            )
            if errors:
                with VerticalScroll(id="error-list"):
                    for result in errors[:20]:
                        yield Static(
                            Text(
                                f"{result.old_rel} → {result.new_rel}\n"
                                f"{result.message}",
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


class RenamerApp(App[None]):
    """Главный экран: папка, preview и таблица ручного ревью."""

    CSS_PATH = "tui.tcss"
    TITLE = "Video Files Renamer"
    SUB_TITLE = "StashDB · FansDB · ThePornDB"
    BINDINGS = [
        Binding("q", "quit", "Выход"),
        Binding("p", "preview", "Preview"),
        Binding("b", "browse", "Папка"),
        Binding("space", "toggle", "Одобрить"),
        Binding("a", "apply", "Применить"),
    ]

    def __init__(
        self,
        folder: str | Path | None = None,
        *,
        preview_factory: Callable[[str, Callable], PreviewReport] | None = None,
        apply_factory: Callable[[list[PreviewRow]], ApplyReport] | None = None,
    ) -> None:
        super().__init__()
        self.folder = Path(folder) if folder else _default_folder()
        self.model = ReviewModel(str(self.folder), [])
        self._preview_factory = preview_factory
        self._apply_factory = apply_factory
        self._busy = False
        self.key = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("VIDEO FILES\nRENAMER", id="brand")
                yield Static("Папка с видео", classes="field-label")
                yield Input(
                    value=str(self.folder),
                    id="folder",
                    placeholder=r"D:\Videos\PH",
                )
                with Horizontal(id="sidebar-actions"):
                    yield Button("Выбрать", id="browse")
                    yield Button("Preview", id="preview", variant="primary")
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
                    yield Button("Одобрить", id="approve", variant="success")
                    yield Button("Снять", id="reject", variant="warning")
                    yield Button("Применить", id="apply", variant="error")
                yield DataTable(
                    id="table", cursor_type="row", zebra_stripes=True
                )
                yield Static("Выберите строку", id="detail")
                yield ProgressBar(
                    total=100, show_eta=False, id="progress"
                )
        yield Footer()

    def on_mount(self) -> None:
        load_env_file()
        self.key = os.environ.get("STASHDB_API_KEY", "").strip()
        self._update_environment()
        table = self.query_one("#table", DataTable)
        table.add_column("", key="approved", width=3)
        table.add_column("Новое имя", key="new", width=58)
        table.add_column("Источник", key="source", width=12)
        table.add_column("Conf", key="confidence", width=7)
        table.add_column("Комментарий", key="note", width=32)
        self.query_one("#progress", ProgressBar).add_class("hidden")
        self._refresh_table()
        self.query_one("#folder", Input).focus()

    def _update_environment(self) -> None:
        key_status = (
            "StashDB: ключ найден"
            if self.key
            else "StashDB: ключа нет — offline"
        )
        self.query_one("#api-status", Static).update(Text(key_status))
        studios = Path("data/studios.json")
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
        self.query_one("#preview", Button).disabled = busy
        self.query_one("#apply", Button).disabled = busy or not self.model.approved_rows()
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
        self.query_one("#apply", Button).disabled = (
            self._busy or not self.model.approved_rows()
        )

    def _update_detail(self, index: int) -> None:
        row = self.model.rows[index]
        detail = Text()
        detail.append("Было:  ", style="bold #94a3b8")
        detail.append(f"{row.old_rel}\n")
        detail.append("Станет: ", style="bold #94a3b8")
        detail.append(f"{row.new_rel}\n", style="#e2e8f0")
        detail.append("Источник: ", style="bold #94a3b8")
        detail.append(
            f"{SOURCE_LABELS.get(row.source, row.source)} · "
            f"confidence {row.confidence:.2f}\n",
            style=SOURCE_STYLES.get(row.source, ""),
        )
        if row.note:
            detail.append("Заметка: ", style="bold #94a3b8")
            detail.append(row.note)
        self.query_one("#detail", Static).update(detail)

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
                Text(
                    row.note or "—", overflow="ellipsis", no_wrap=True
                ),
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
            cell = table.coordinate_to_cell_key(
                (table.cursor_row, 0)
            )
        except Exception:
            return None
        return int(cell.row_key.value)

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

    def action_browse(self) -> None:
        start = Path(self.query_one("#folder", Input).value or self.folder)
        if not start.is_dir():
            start = Path.cwd()
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

        studios_path = Path("data/studios.json")
        studios = load_studios(str(studios_path)) if studios_path.exists() else {}
        cache = TitleCache("cache/title_fixer.sqlite3")
        client = StashDBClient(self.key) if self.key else None
        try:
            service = TitleService(
                folder,
                studios,
                cache,
                client,
                offline=not bool(self.key),
                progress=progress,
            )
            return service.preview()
        finally:
            cache.close()

    @work(thread=True, exclusive=True)
    def _preview_worker(self, folder: str) -> None:
        def progress(event: ProgressEvent) -> None:
            self.call_from_thread(self._update_progress, event)

        try:
            report = self._preview_worker_impl(folder, progress)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.call_from_thread(self._preview_failed, exc)
            return
        self.call_from_thread(self._preview_done, folder, report)

    def _preview_done(self, folder: str, report: PreviewReport) -> None:
        self.model = ReviewModel(folder, list(report.rows))
        self._set_busy(False)
        self._refresh_table()
        self.query_one("#table", DataTable).focus()
        self._set_status(
            f"Готово: {len(report.rows)} строк. "
            "Проверьте таблицу и нажмите «Применить»."
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

    def action_approve(self) -> None:
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
            if self._apply_factory is not None:
                report = self._apply_factory(rows)
            else:
                service = TitleService(self.model.folder, {}, None, None)
                report = service.apply_rows(rows, dry_run=False)
                append_rename_log(
                    "rename_log.csv",
                    rows,
                    report.results,
                    datetime.now(timezone.utc).isoformat(),
                )
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.call_from_thread(self._apply_failed, exc)
            return
        self.call_from_thread(self._apply_done, report)

    def _apply_done(self, report: ApplyReport) -> None:
        self._set_busy(False)
        self.model.set_rows([])
        self._refresh_table()
        self._set_status("Применено")
        self.push_screen(ApplyResultModal(report))

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

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
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
