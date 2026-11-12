"""Главное окно CustomTkinter: сайдбар, тулбар и таблица ревью."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from video_renamer.core.stashdb_client import Scene
from video_renamer.core.title_service import ApplyReport, PreviewReport, ProgressEvent
from video_renamer.gui.controller import RenamerController
from video_renamer.gui.dialogs import (
    StudioDialog,
    UndoDialog,
    ask_confirm,
    choose_candidate,
    show_apply_result,
    show_notice,
)
from video_renamer.gui.tasks import OperationCancelled, UiDispatcher, run_in_thread
from video_renamer.gui.theme import (
    COLORS,
    PHASE_LABELS,
    font,
    source_label,
)
from video_renamer.gui.virtual_rows import VirtualRowsView
from video_renamer.gui.widgets import configure_row_columns
from video_renamer.ui.tui_model import FILTERS

SOURCE_ORDER = ("StashDB", "FansDB", "ThePornDB")
TEXT_INPUT_CLASSES = frozenset(
    {"Entry", "TEntry", "Text", "TText", "Spinbox", "TSpinbox", "TCombobox", "Listbox"}
)
STAT_ROWS = (
    ("total", "Всего", COLORS["text"]),
    ("approved", "Одобрено", COLORS["green_text"]),
    ("pending", "Ждут ревью", COLORS["orange_text"]),
    ("fingerprint", "Fingerprint", "#67e8f9"),
    ("phash", "PHASH", "#c4b5fd"),
    ("text", "Текст", COLORS["orange_text"]),
    ("heuristic", "Эвристика", COLORS["red_text"]),
)


class RenamerGUI(ctk.CTk):
    """Десктопный аналог Textual-экрана `RenamerApp`."""

    def __init__(
        self,
        folder: str | None = None,
        *,
        controller: RenamerController | None = None,
    ) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        super().__init__()

        self.controller = controller or RenamerController()
        self.dispatcher = UiDispatcher(self)
        self.selected_index: int | None = None
        self._busy = False
        self._progress_mode = "determinate"
        self._cancel_event: threading.Event | None = None
        self._visible_total = 0
        self._filter_by_label = {label: key for key, label in FILTERS}
        self._update_status_text = os.environ.get("VIDEO_RENAMER_UPDATE_STATUS", "").strip()

        self._build_window()
        self._build_sidebar()
        self._build_main()
        self._bind_shortcuts()
        self._refresh_environment()

        if folder:
            self._apply_folder(folder, notify=False)
        elif self.controller.folder is not None:
            self.folder_entry.insert(0, str(self.controller.folder))

        self._refresh_rows()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._initial_focus)

    # --- построение окна ------------------------------------------------

    def _build_window(self) -> None:
        self.title("Video Files Renamer")
        self.geometry("1440x900")
        self.minsize(1180, 700)
        self.configure(fg_color=COLORS["bg"])
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

    def _build_sidebar(self) -> None:
        self.sidebar = ctk.CTkFrame(
            self,
            width=304,
            corner_radius=0,
            fg_color=COLORS["panel"],
        )
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        self.sidebar.pack_propagate(False)

        brand = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand.pack(fill="x", padx=18, pady=(22, 6))
        ctk.CTkFrame(brand, width=5, height=48, corner_radius=3, fg_color=COLORS["accent"]).pack(
            side="left", padx=(0, 12)
        )
        text_block = ctk.CTkFrame(brand, fg_color="transparent")
        text_block.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            text_block,
            text="VIDEO FILES\nRENAMER",
            anchor="w",
            justify="left",
            font=font(20, "bold"),
            text_color=COLORS["accent"],
        ).pack(anchor="w")
        ctk.CTkLabel(
            text_block,
            text="StashDB · FansDB · ThePornDB",
            anchor="w",
            font=font(10),
            text_color=COLORS["muted"],
        ).pack(anchor="w", pady=(2, 0))

        self._section_label("ПАПКА С ВИДЕО").pack(fill="x", padx=18, pady=(18, 6))
        self.folder_entry = ctk.CTkEntry(
            self.sidebar,
            height=36,
            font=font(11),
            fg_color=COLORS["panel_2"],
            border_color=COLORS["border_light"],
            text_color=COLORS["text"],
            placeholder_text="Путь к папке",
        )
        self.folder_entry.pack(fill="x", padx=18)
        self.folder_entry.bind("<Return>", self._commit_folder)
        self.browse_button = ctk.CTkButton(
            self.sidebar,
            text="Выбрать папку",
            height=34,
            font=font(12),
            fg_color=COLORS["panel_2"],
            hover_color=COLORS["border_light"],
            border_width=1,
            border_color=COLORS["border"],
            command=self.action_browse,
        )
        self.browse_button.pack(fill="x", padx=18, pady=(6, 0))

        self.preview_button = ctk.CTkButton(
            self.sidebar,
            text="▶   Preview",
            height=44,
            font=font(14, "bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color="#041018",
            command=self.action_preview,
        )
        self.preview_button.pack(fill="x", padx=18, pady=(10, 4))

        self._section_label("ИСТОЧНИКИ").pack(fill="x", padx=18, pady=(18, 4))
        self._source_state_labels: dict[str, ctk.CTkLabel] = {}
        self._source_dots: dict[str, ctk.CTkLabel] = {}
        for name in SOURCE_ORDER:
            row = ctk.CTkFrame(self.sidebar, fg_color="transparent", height=26)
            row.pack(fill="x", padx=18, pady=1)
            dot = ctk.CTkLabel(
                row,
                text="●",
                width=16,
                font=font(12),
                text_color=COLORS["muted"],
            )
            dot.pack(side="left")
            ctk.CTkLabel(
                row,
                text=name,
                anchor="w",
                font=font(12),
                text_color=COLORS["text"],
            ).pack(side="left", padx=(2, 0))
            state = ctk.CTkLabel(
                row,
                text="",
                anchor="e",
                font=font(10),
                text_color=COLORS["muted"],
            )
            state.pack(side="right")
            self._source_dots[name] = dot
            self._source_state_labels[name] = state

        self.catalog_label = ctk.CTkLabel(
            self.sidebar,
            text="",
            anchor="w",
            font=font(10),
            text_color=COLORS["muted"],
        )
        self.catalog_label.pack(fill="x", padx=18, pady=(4, 0))

        self._section_label("СТАТИСТИКА").pack(fill="x", padx=18, pady=(18, 6))
        stats_card = ctk.CTkFrame(
            self.sidebar,
            fg_color=COLORS["panel_2"],
            corner_radius=10,
        )
        stats_card.pack(fill="x", padx=18)
        self._stat_labels: dict[str, ctk.CTkLabel] = {}
        for key, label, color in STAT_ROWS:
            row = ctk.CTkFrame(stats_card, fg_color="transparent", height=24)
            row.pack(fill="x", padx=12, pady=1)
            ctk.CTkLabel(
                row,
                text=label,
                anchor="w",
                font=font(11),
                text_color=COLORS["muted"],
            ).pack(side="left")
            value = ctk.CTkLabel(
                row,
                text="0",
                anchor="e",
                font=font(12, "bold"),
                text_color=color,
            )
            value.pack(side="right")
            self._stat_labels[key] = value

        footer = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=18, pady=(8, 16))
        self.studio_button = ctk.CTkButton(
            footer,
            text="Студии и справочники",
            height=34,
            font=font(11),
            fg_color=COLORS["panel_2"],
            hover_color=COLORS["border_light"],
            command=self.open_studios,
        )
        self.studio_button.pack(fill="x")
        self.undo_button = ctk.CTkButton(
            footer,
            text="Журнал переименований",
            height=34,
            font=font(11),
            fg_color=COLORS["panel_2"],
            hover_color=COLORS["border_light"],
            command=self.open_undo,
        )
        self.undo_button.pack(fill="x", pady=(6, 0))
        self.update_label = ctk.CTkLabel(
            footer,
            text="",
            anchor="w",
            justify="left",
            wraplength=264,
            font=font(10),
            text_color=COLORS["muted"],
        )
        self.update_label.pack(fill="x", pady=(10, 0))
        if self._update_status_text:
            self.update_label.configure(
                text=f"Git: {self._update_status_text}",
                text_color=self._update_color(self._update_status_text),
            )
        ctk.CTkLabel(
            footer,
            text="Space — одобрить/снять · Ctrl+Enter — применить\n↑/↓ — строка · F5 — Preview · Esc — снять выделение",
            anchor="w",
            justify="left",
            font=font(9),
            text_color=COLORS["muted"],
        ).pack(fill="x", pady=(8, 0))

    def _build_main(self) -> None:
        self.main = ctk.CTkFrame(self, fg_color=COLORS["bg"], corner_radius=0)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(3, weight=1)

        toolbar = ctk.CTkFrame(self.main, fg_color=COLORS["panel"], corner_radius=10)
        toolbar.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        toolbar.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(
            toolbar,
            text="ФИЛЬТР",
            font=font(10, "bold"),
            text_color=COLORS["muted"],
        ).grid(row=0, column=0, padx=(14, 8), pady=10)
        self.filter_menu = ctk.CTkOptionMenu(
            toolbar,
            values=[label for _key, label in FILTERS],
            width=180,
            height=34,
            dropdown_font=font(11),
            font=font(11),
            fg_color=COLORS["panel_2"],
            button_color=COLORS["border_light"],
            button_hover_color=COLORS["border"],
            dropdown_fg_color=COLORS["panel"],
            dropdown_hover_color=COLORS["row_selected"],
            dropdown_text_color=COLORS["text"],
            text_color=COLORS["text"],
            command=self._on_filter_changed,
        )
        self.filter_menu.grid(row=0, column=1, padx=(0, 8), pady=10, sticky="w")
        self.filter_menu.set(dict(FILTERS).get("all", "Все"))

        actions = ctk.CTkFrame(toolbar, fg_color="transparent")
        actions.grid(row=0, column=3, padx=(0, 10), pady=8, sticky="e")
        self.candidates_button = ctk.CTkButton(
            actions,
            text="Кандидаты",
            width=112,
            height=36,
            font=font(12),
            fg_color=COLORS["panel_2"],
            hover_color=COLORS["border_light"],
            border_width=1,
            border_color=COLORS["border"],
            command=self.action_candidates,
        )
        self.candidates_button.pack(side="left", padx=(0, 8))
        self.approve_button = ctk.CTkButton(
            actions,
            text="Одобрить",
            width=112,
            height=36,
            font=font(12, "bold"),
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            command=self.action_approve,
        )
        self.approve_button.pack(side="left", padx=(0, 8))
        self.reject_button = ctk.CTkButton(
            actions,
            text="Снять",
            width=100,
            height=36,
            font=font(12, "bold"),
            fg_color=COLORS["orange"],
            hover_color=COLORS["orange_hover"],
            command=self.action_reject,
        )
        self.reject_button.pack(side="left", padx=(0, 8))
        self.apply_button = ctk.CTkButton(
            actions,
            text="Применить",
            width=124,
            height=36,
            font=font(12, "bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color="#041018",
            state="disabled",
            command=self.action_apply,
        )
        self.apply_button.pack(side="left")

        status_frame = ctk.CTkFrame(self.main, fg_color="transparent")
        status_frame.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 4))
        status_frame.grid_columnconfigure(0, weight=1)
        status_frame.grid_columnconfigure(1, minsize=100)
        self.status_label = ctk.CTkLabel(
            status_frame,
            text="Готов к работе",
            anchor="w",
            font=font(11),
            text_color=COLORS["muted"],
        )
        self.status_label.grid(row=0, column=0, sticky="w")
        self.visible_label = ctk.CTkLabel(
            status_frame,
            text="",
            anchor="e",
            font=font(10),
            text_color=COLORS["muted"],
        )
        self.visible_label.grid(row=0, column=1, sticky="e")
        self.progress = ctk.CTkProgressBar(
            status_frame,
            height=5,
            corner_radius=2,
            fg_color=COLORS["border"],
            progress_color=COLORS["accent"],
        )
        self.progress.set(0)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.progress.grid_remove()
        self.cancel_button = ctk.CTkButton(
            status_frame,
            text="Отмена",
            width=92,
            height=24,
            font=font(10, "bold"),
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            command=self.action_cancel,
        )
        self.cancel_button.grid(row=1, column=1, padx=(8, 0), pady=(4, 0), sticky="e")
        self.cancel_button.grid_remove()

        self.header = ctk.CTkFrame(
            self.main,
            fg_color=COLORS["panel_2"],
            corner_radius=8,
            height=34,
        )
        self.header.grid(row=2, column=0, sticky="ew", padx=(16, 30), pady=(0, 4))
        configure_row_columns(self.header)
        for column, text in enumerate(
            ("", "ОРИГИНАЛЬНОЕ ИМЯ", "КАНДИДАТЫ", "НОВОЕ ИМЯ", "ИСТОЧНИК", "CONF")
        ):
            anchor = "e" if column == 5 else "w"
            ctk.CTkLabel(
                self.header,
                text=text,
                anchor=anchor,
                font=font(10, "bold"),
                text_color=COLORS["muted"],
            ).grid(
                row=0,
                column=column,
                padx=(10 if column == 0 else 4, 10 if column == 5 else 4),
                pady=6,
                sticky="ew",
            )

        self.rows_view = VirtualRowsView(
            self.main,
            on_select=self._on_select,
            on_toggle=self._on_toggle,
            on_candidate=self._on_candidate,
            on_name=self._on_name_commit,
        )
        self.rows_view.grid(row=3, column=0, sticky="nsew", padx=16)

        detail = ctk.CTkFrame(self.main, fg_color=COLORS["panel"], corner_radius=10)
        detail.grid(row=4, column=0, sticky="ew", padx=16, pady=(8, 14))
        ctk.CTkLabel(
            detail,
            text="ВЫБРАННАЯ СТРОКА",
            anchor="w",
            font=font(10, "bold"),
            text_color=COLORS["muted"],
        ).pack(fill="x", padx=12, pady=(8, 0))
        self.detail_box = ctk.CTkTextbox(
            detail,
            height=84,
            font=("Consolas", 11),
            fg_color=COLORS["panel_2"],
            border_width=0,
            text_color=COLORS["text"],
        )
        self.detail_box.pack(fill="x", padx=12, pady=(2, 10))
        self.detail_box.configure(state="disabled")

    def _section_label(self, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            self.sidebar,
            text=text,
            anchor="w",
            font=font(10, "bold"),
            text_color=COLORS["muted"],
        )

    def _bind_shortcuts(self) -> None:
        self.bind_all("<F5>", lambda _event: self.action_preview())
        self.bind_all("<Control-Return>", lambda _event: self.action_apply())
        self.bind_all("<Control-KP_Enter>", lambda _event: self.action_apply())
        self.bind_all("<Control-o>", lambda _event: self.action_browse())
        self.bind_all("<Escape>", lambda _event: self._clear_selection())
        self.bind_all("<space>", self._on_space)
        self.bind_all("<Up>", lambda _event: self._move_selection(-1))
        self.bind_all("<Down>", lambda _event: self._move_selection(1))

    def _initial_focus(self) -> None:
        if not self.controller.model.rows:
            self.folder_entry.focus_set()

    @staticmethod
    def _update_color(text: str) -> str:
        lowered = text.casefold()
        if lowered.startswith(("ошибка", "не удалось", "конфликт")):
            return COLORS["red_text"]
        if lowered.startswith(("обновлено", "уже актуально")):
            return COLORS["green_text"]
        return COLORS["muted"]

    # --- окружение -------------------------------------------------------

    def _refresh_environment(self) -> None:
        self.controller.reload_environment()
        for name in SOURCE_ORDER:
            configured = self.controller.source_configured(name)
            dot = self._source_dots[name]
            state = self._source_state_labels[name]
            dot.configure(text_color=COLORS["green_text"] if configured else COLORS["muted"])
            state.configure(
                text="ключ найден" if configured else "нет ключа",
                text_color=COLORS["green_text"] if configured else COLORS["muted"],
            )
        self.catalog_label.configure(text=self.controller.catalog_text())
        if self._update_status_text:
            self.update_label.configure(
                text=f"Git: {self._update_status_text}",
                text_color=self._update_color(self._update_status_text),
            )

    def _after_studio_update(self) -> None:
        self._refresh_environment()
        self._set_status("Справочники обновлены. Повторите Preview.", "ok")

    # --- папка -----------------------------------------------------------

    def action_browse(self) -> None:
        if self._busy or not self._shortcut_allowed():
            return
        initial = self.folder_entry.get().strip()
        if not initial or not Path(initial).is_dir():
            initial = str(Path.home())
        selected = filedialog.askdirectory(
            parent=self,
            initialdir=initial,
            title="Выберите папку с видео",
            mustexist=True,
        )
        if selected:
            self._apply_folder(selected)

    def _commit_folder(self, _event=None) -> None:
        self._apply_folder(self.folder_entry.get())

    def _apply_folder(self, folder: str, *, notify: bool = True) -> bool:
        try:
            path = self.controller.set_folder(folder)
        except ValueError as exc:
            if notify:
                show_notice(
                    self,
                    "Папка",
                    str(exc),
                    accent=COLORS["red"],
                    height=240,
                )
            else:
                self._set_status(str(exc), "error")
            return False
        self.folder_entry.delete(0, "end")
        self.folder_entry.insert(0, str(path))
        self._set_status(f"Папка: {path}", "info")
        return True

    # --- preview / apply -------------------------------------------------

    def action_preview(self) -> None:
        if self._busy or not self._shortcut_allowed():
            return
        if not self._apply_folder(self.folder_entry.get()):
            return
        self._cancel_event = threading.Event()
        cancel_event = self._cancel_event
        self._set_busy(True, "Сканирую папку...", indeterminate=True, cancellable=True)
        run_in_thread(
            lambda: self.controller.run_preview(self._dispatch_progress, cancel_event=cancel_event),
            dispatcher=self.dispatcher,
            on_success=self._preview_done,
            on_error=self._preview_failed,
            on_cancelled=self._preview_cancelled,
            cancel_event=cancel_event,
            name="preview",
        )

    def action_cancel(self) -> None:
        if not self._busy or self._cancel_event is None:
            return
        if self._cancel_event.is_set():
            return
        self._cancel_event.set()
        self.cancel_button.configure(text="Отмена...", state="disabled")
        self._set_status("Останавливаю preview...", "warn")

    def _dispatch_progress(self, event: ProgressEvent) -> None:
        """Вызывается из воркера: проверяет отмену и ставит обновление в Tk."""
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise OperationCancelled
        self.dispatcher.call_soon(self._on_progress, event)

    def _on_progress(self, event: ProgressEvent) -> None:
        if not self._busy:
            return
        if self._progress_mode == "indeterminate":
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self._progress_mode = "determinate"
        if event.phase == "scan":
            self.progress.set(0.0)
            self._set_status(f"Найдено файлов: {event.total}", "info")
            return
        if event.phase == "done":
            self.progress.set(1.0)
            self._set_status("Сканирование завершено", "ok")
            return
        total = max(event.total, 1)
        self.progress.set(min(max(event.current / total, 0.0), 1.0))
        label = PHASE_LABELS.get(event.phase, event.message or event.phase)
        self._set_status(f"{label}: {event.current}/{event.total}", "info")

    def _preview_done(self, report: PreviewReport) -> None:
        self._cancel_event = None
        self.controller.apply_preview(report)
        self.filter_menu.set(dict(FILTERS).get("all", "Все"))
        self.selected_index = None
        self._set_busy(False)
        self._refresh_rows(reset_scroll=True)
        self._set_status(
            f"Готово: {len(report.rows)} строк. Проверьте таблицу и нажмите «Применить».",
            "ok",
        )
        if report.warnings:
            show_notice(
                self,
                "Предупреждения preview",
                "\n\n".join(report.warnings[:5]),
                accent=COLORS["orange"],
                height=300,
            )

    def _preview_failed(self, exc: Exception) -> None:
        self._cancel_event = None
        self._set_busy(False)
        self._refresh_rows()
        self._set_status("Ошибка preview", "error")
        show_notice(
            self,
            "Ошибка preview",
            str(exc) or exc.__class__.__name__,
            accent=COLORS["red"],
            height=300,
        )

    def _preview_cancelled(self) -> None:
        self._cancel_event = None
        self._set_busy(False)
        self._refresh_rows()
        self._set_status("Preview отменён", "warn")

    def action_apply(self) -> None:
        if self._busy or not self._shortcut_allowed():
            return
        approved = self.controller.model.approved_rows()
        if not approved:
            self._set_status("Нет одобренных строк", "warn")
            return
        counts: dict[str, int] = {}
        for row in approved:
            counts[row.source] = counts.get(row.source, 0) + 1
        summary = ", ".join(
            f"{source_label(source)}: {count}" for source, count in sorted(counts.items())
        )
        message = (
            f"Переименовать {len(approved)} файлов?\n\n"
            f"{summary}\n\n"
            "Проверьте имена: операция изменяет файлы на диске."
        )
        if not ask_confirm(
            self,
            "Применить переименования?",
            message,
            confirm_text="Применить",
            accent=COLORS["green"],
            width=620,
            height=340,
        ):
            return
        rows = list(approved)
        self._cancel_event = None
        self._set_busy(True, "Переименовываю...", indeterminate=True)
        run_in_thread(
            lambda: self.controller.run_apply(rows),
            dispatcher=self.dispatcher,
            on_success=self._apply_done,
            on_error=self._apply_failed,
            name="apply",
        )

    def _apply_done(self, result: tuple[ApplyReport, str]) -> None:
        report, log_error = result
        self.controller.remove_renamed(report)
        self.selected_index = None
        self._set_busy(False)
        self._refresh_rows(reset_scroll=True)
        renamed = sum(1 for item in report.results if item.status == "renamed")
        if log_error:
            self._set_status("Переименовано, но журнал не записан", "warn")
        elif report.had_errors and self.controller.model.rows:
            self._set_status(
                f"Применено: {renamed}; строк с ошибками осталось: "
                f"{len(self.controller.model.rows)}",
                "warn",
            )
        else:
            self._set_status(f"Применено: {renamed}", "ok")
        show_apply_result(
            self,
            report,
            log_path=self.controller.log_path,
            log_error=log_error,
        )

    def _apply_failed(self, exc: Exception) -> None:
        self._set_busy(False)
        self._set_status("Ошибка apply", "error")
        show_notice(
            self,
            "Ошибка переименования",
            str(exc) or exc.__class__.__name__,
            accent=COLORS["red"],
            height=300,
        )

    # --- действия над строками ------------------------------------------

    def _on_select(self, index: int, take_focus: bool = True) -> None:
        old = self.selected_index
        if take_focus:
            self.focus_set()
        if old == index:
            return
        self.selected_index = index
        self.rows_view.set_selected_index(index)
        self._update_detail()
        self._update_action_state()

    def _clear_selection(self) -> None:
        if not self._shortcut_allowed():
            return
        if self.selected_index is None:
            return
        self.selected_index = None
        self.rows_view.set_selected_index(None)
        self._update_detail()
        self._update_action_state()

    def _shortcut_allowed(self) -> bool:
        """Не перехватывать клавиши, когда открыт модальный диалог."""
        focused = self.focus_get()
        if focused is None:
            return True
        try:
            return focused.winfo_toplevel() is self
        except Exception:
            return False

    def _move_selection(self, delta: int) -> str | None:
        """Перемещает выделение по видимому списку и прокручивает холст."""
        if not self._shortcut_allowed():
            return None
        if self._focus_is_editing(self.focus_get()):
            return None
        if self._busy:
            return "break"
        visible = self.controller.model.visible()
        if not visible:
            return "break"
        positions = {index: position for position, (index, _row) in enumerate(visible)}
        if self.selected_index in positions:
            position = positions[self.selected_index] + delta
        else:
            position = 0 if delta > 0 else len(visible) - 1
        position = min(max(position, 0), len(visible) - 1)
        index = visible[position][0]
        self._on_select(index)
        self.rows_view.ensure_visible(position)
        return "break"

    def _on_space(self, _event=None) -> str | None:
        focused = self.focus_get()
        if not self._shortcut_allowed():
            return None
        if self._focus_is_editing(focused):
            return None
        if self._busy or self.selected_index is None:
            return "break"
        index = self.selected_index
        rows = self.controller.model.rows
        if not (0 <= index < len(rows)):
            return "break"
        self._on_toggle(index, not rows[index].approved)
        self.focus_set()
        return "break"

    @staticmethod
    def _focus_is_editing(widget) -> bool:
        """Space должен печататься в Entry/Text и активировать кнопки."""
        current = widget
        while current is not None:
            try:
                if current.winfo_class() in TEXT_INPUT_CLASSES:
                    return True
            except Exception:
                return False
            if isinstance(
                current,
                (ctk.CTkEntry, ctk.CTkTextbox, ctk.CTkButton, ctk.CTkCheckBox, ctk.CTkOptionMenu),
            ):
                return True
            parent = getattr(current, "master", None)
            if parent is None or parent is current:
                break
            current = parent
        return False

    def _on_toggle(self, index: int, approved: bool) -> None:
        if self._busy:
            view = self.rows_view.view_for(index)
            if view is not None:
                view.refresh(self.controller.model.rows[index])
            return
        self._on_select(index)
        updated = self.controller.approve(index) if approved else self.controller.reject(index)
        self._apply_row_update(index, updated)

    def _apply_row_update(self, index: int, updated) -> None:
        if not self.controller.model.matches(updated):
            self._refresh_rows()
            return
        view = self.rows_view.view_for(index)
        if view is not None:
            view.refresh(updated)
        self._update_detail()
        self._update_stats()
        self._update_action_state()

    def _on_name_commit(self, index: int, value: str) -> None:
        if self._busy:
            return
        row = self.controller.model.rows[index]
        current = os.path.basename(row.new_rel)
        if value.strip() == current:
            return
        try:
            updated = self.controller.rename_basename(index, value)
        except ValueError as exc:
            self._set_status(str(exc), "error")
            view = self.rows_view.view_for(index)
            if view is not None:
                view.reset_name()
            return
        self._on_select(index)
        self._apply_row_update(index, updated)
        self._set_status("Имя изменено вручную", "ok")

    def _on_candidate(self, index: int, scene: Scene) -> None:
        if self._busy:
            return
        try:
            updated = self.controller.build_alternative(index, scene)
        except Exception as exc:  # имя собирается из метаданных сцены
            self._set_status(f"Не удалось собрать имя: {exc}", "error")
            return
        self._on_select(index)
        self._apply_row_update(index, updated)
        self._set_status("Кандидат выбран; проверьте имя", "ok")

    def action_candidates(self) -> None:
        if self._busy or self.selected_index is None:
            return
        index = self.selected_index
        row = self.controller.model.rows[index]
        if not row.candidates:
            self._set_status("У этой строки нет альтернативных кандидатов", "warn")
            return
        scene = choose_candidate(self, row)
        if scene is not None:
            self._on_candidate(index, scene)

    def action_approve(self) -> None:
        if self._busy or self.selected_index is None:
            return
        index = self.selected_index
        self._apply_row_update(index, self.controller.approve(index))

    def action_reject(self) -> None:
        if self._busy or self.selected_index is None:
            return
        index = self.selected_index
        self._apply_row_update(index, self.controller.reject(index))

    # --- отрисовка -------------------------------------------------------

    def _refresh_rows(self, *, reset_scroll: bool = False) -> None:
        visible = self.controller.model.visible()
        self._visible_total = len(visible)
        if self.selected_index is not None and not any(
            index == self.selected_index for index, _row in visible
        ):
            self.selected_index = None
        self.rows_view.set_rows(visible, reset_scroll=reset_scroll)
        self.rows_view.set_selected_index(self.selected_index)
        self.visible_label.configure(text=f"{self._visible_total} строк")
        self._update_stats()
        self._update_detail()
        self._update_action_state()

    def _update_stats(self) -> None:
        stats = self.controller.model.stats()
        for key, label in self._stat_labels.items():
            label.configure(text=str(stats.get(key, 0)))

    def _update_detail(self) -> None:
        index = self.selected_index
        rows = self.controller.model.rows
        if index is None or not (0 <= index < len(rows)):
            text = "Выберите строку, чтобы увидеть полные пути и заметку."
        else:
            row = rows[index]
            lines = [
                f"Было:    {row.old_rel}",
                f"Станет:  {row.new_rel}",
                f"Источник: {source_label(row.source)} · confidence {row.confidence:.2f}",
            ]
            if row.note:
                lines.append(f"Заметка: {row.note}")
            if row.candidates:
                lines.append(f"Кандидатов: {len(row.candidates)}")
            text = "\n".join(lines)
        self.detail_box.configure(state="normal")
        self.detail_box.delete("1.0", "end")
        self.detail_box.insert("1.0", text)
        self.detail_box.configure(state="disabled")

    def _update_action_state(self) -> None:
        index = self.selected_index
        rows = self.controller.model.rows
        row = rows[index] if index is not None and 0 <= index < len(rows) else None
        editable = not self._busy and row is not None
        self.approve_button.configure(state="normal" if editable else "disabled")
        self.reject_button.configure(state="normal" if editable else "disabled")
        self.candidates_button.configure(
            state="normal" if editable and bool(row.candidates) else "disabled"
        )
        has_approved = bool(self.controller.model.approved_rows())
        self.apply_button.configure(
            state="normal" if has_approved and not self._busy else "disabled"
        )

    def _set_status(self, text: str, kind: str = "info") -> None:
        colors = {
            "info": COLORS["muted"],
            "ok": COLORS["green_text"],
            "warn": COLORS["orange_text"],
            "error": COLORS["red_text"],
        }
        self.status_label.configure(text=text, text_color=colors.get(kind, COLORS["muted"]))

    def _set_busy(
        self,
        busy: bool,
        text: str = "",
        *,
        indeterminate: bool = False,
        cancellable: bool = False,
    ) -> None:
        self._busy = busy
        for widget in (
            self.preview_button,
            self.browse_button,
            self.studio_button,
            self.undo_button,
            self.folder_entry,
            self.filter_menu,
        ):
            widget.configure(state="disabled" if busy else "normal")
        self.rows_view.set_enabled(not busy)

        if busy:
            if indeterminate:
                self._progress_mode = "indeterminate"
                self.progress.stop()
                self.progress.configure(mode="indeterminate")
                self.progress.start()
            else:
                self._progress_mode = "determinate"
                self.progress.stop()
                self.progress.configure(mode="determinate")
                self.progress.set(0)
            self.progress.grid()
            if text:
                self._set_status(text, "info")
            if cancellable:
                self.cancel_button.configure(text="Отмена", state="normal")
                self.cancel_button.grid()
            else:
                self.cancel_button.grid_remove()
            if not self.controller.model.rows:
                self.rows_view.show_skeleton()
        else:
            self.rows_view.hide_skeleton()
            self.progress.stop()
            self.progress.grid_remove()
            self.cancel_button.grid_remove()
            self._cancel_event = None
            self._progress_mode = "determinate"
        self._update_action_state()

    def _on_filter_changed(self, label: str) -> None:
        key = self._filter_by_label.get(label, "all")
        self.controller.model.filter = key
        self._refresh_rows(reset_scroll=True)

    # --- диалоги ---------------------------------------------------------

    def open_studios(self) -> None:
        if self._busy:
            return
        dialog = StudioDialog(
            self,
            self.dispatcher,
            on_finished=self._after_studio_update,
        )
        self.wait_window(dialog)

    def open_undo(self) -> None:
        if self._busy:
            return
        folder = self.controller.model.folder or (
            str(self.controller.folder) if self.controller.folder else ""
        )
        dialog = UndoDialog(
            self,
            self.dispatcher,
            log_path=self.controller.log_path,
            current_folder=folder,
        )
        self.wait_window(dialog)

    # --- закрытие --------------------------------------------------------

    def _on_close(self) -> None:
        if self._busy:
            show_notice(
                self,
                "Операция выполняется",
                "Дождитесь её завершения, чтобы не прервать переименование.",
                accent=COLORS["orange"],
                height=240,
            )
            return
        if self.controller.model.rows and not ask_confirm(
            self,
            "Закрыть программу?",
            "В таблице остались необработанные предложения. Выйти?",
            confirm_text="Выйти",
            accent=COLORS["orange"],
            width=560,
            height=250,
        ):
            return
        self.dispatcher.stop()
        self.destroy()


def main(folder: str | None = None) -> int:
    app = RenamerGUI(folder=folder)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
