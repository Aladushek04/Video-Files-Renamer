"""Модальные окна GUI: подтверждение, результат, кандидаты, студии, undo."""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from video_renamer.core.rename_log import active_renames, read_log
from video_renamer.core.stashdb_client import Scene
from video_renamer.core.title_service import ApplyReport, PreviewRow
from video_renamer.core.undo import run_undo, select_for_undo
from video_renamer.gui.tasks import UiDispatcher, run_in_thread
from video_renamer.gui.theme import COLORS, clip, font, mono
from video_renamer.ui.studio_tasks import run as run_studio_task


def _hover_for(accent: str) -> str:
    return {
        COLORS["green"]: COLORS["green_hover"],
        COLORS["orange"]: COLORS["orange_hover"],
        COLORS["red"]: COLORS["red_hover"],
    }.get(accent, COLORS["accent_hover"])


def _center(window: ctk.CTkToplevel, master, width: int, height: int) -> None:
    master.update_idletasks()
    x = max(0, master.winfo_rootx() + (master.winfo_width() - width) // 2)
    y = max(0, master.winfo_rooty() + (master.winfo_height() - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


def _scene_text(scene: Scene) -> str:
    title = scene.title or "(без названия)"
    studio = scene.studio.name if scene.studio else "—"
    date = scene.release_date or scene.date or "—"
    performers = ", ".join(performer.display_name for performer in scene.performers) or "—"
    return f"{title}\n{studio} · {date} · {performers}"


def _set_textbox(textbox: ctk.CTkTextbox, text: str) -> None:
    textbox.configure(state="normal")
    textbox.delete("1.0", "end")
    textbox.insert("1.0", text)
    textbox.configure(state="disabled")


class ConfirmDialog(ctk.CTkToplevel):
    """Модальное подтверждение действия; результат доступен после закрытия."""

    def __init__(
        self,
        master,
        *,
        title: str,
        message: str,
        confirm_text: str = "Применить",
        cancel_text: str | None = "Отмена",
        accent: str = COLORS["green"],
        width: int = 560,
        height: int = 260,
    ) -> None:
        super().__init__(master)
        self.result = False
        self.title(title)
        self.resizable(False, False)
        self.configure(fg_color=COLORS["bg"])
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel())
        self.bind("<Return>", lambda _event: self._confirm())

        header = ctk.CTkLabel(
            self,
            text=title,
            anchor="w",
            font=font(18, "bold"),
            text_color=COLORS["accent"],
        )
        header.pack(fill="x", padx=24, pady=(22, 8))

        body = ctk.CTkLabel(
            self,
            text=message,
            anchor="w",
            justify="left",
            wraplength=width - 56,
            font=font(12),
            text_color=COLORS["text"],
        )
        body.pack(fill="both", expand=True, padx=24, pady=(0, 12))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=24, pady=(0, 20))
        ctk.CTkButton(
            buttons,
            text=confirm_text,
            width=150,
            height=36,
            font=font(12, "bold"),
            fg_color=accent,
            hover_color=_hover_for(accent),
            command=self._confirm,
        ).pack(side="right")
        if cancel_text is not None:
            ctk.CTkButton(
                buttons,
                text=cancel_text,
                width=120,
                height=36,
                font=font(12),
                fg_color=COLORS["panel_2"],
                hover_color=COLORS["border_light"],
                command=self._cancel,
            ).pack(side="right", padx=(0, 10))

        _center(self, master, width, height)
        self.update_idletasks()
        self.grab_set()
        self.lift()
        self.focus_force()

    def _confirm(self) -> None:
        self.result = True
        self.destroy()

    def _cancel(self) -> None:
        self.result = False
        self.destroy()


def ask_confirm(
    master,
    title: str,
    message: str,
    *,
    confirm_text: str = "Применить",
    cancel_text: str | None = "Отмена",
    accent: str = COLORS["green"],
    width: int = 560,
    height: int = 260,
) -> bool:
    dialog = ConfirmDialog(
        master,
        title=title,
        message=message,
        confirm_text=confirm_text,
        cancel_text=cancel_text,
        accent=accent,
        width=width,
        height=height,
    )
    master.wait_window(dialog)
    return dialog.result


def show_notice(
    master,
    title: str,
    message: str,
    *,
    accent: str = COLORS["accent"],
    width: int = 620,
    height: int = 300,
) -> None:
    ask_confirm(
        master,
        title,
        message,
        confirm_text="Закрыть",
        cancel_text=None,
        accent=accent,
        width=width,
        height=height,
    )


class ApplyResultDialog(ctk.CTkToplevel):
    """Итог apply со списком переименованных и ошибочных строк."""

    def __init__(
        self,
        master,
        report: ApplyReport,
        *,
        log_path: str,
        log_error: str = "",
    ) -> None:
        super().__init__(master)
        self.title("Результат переименования")
        self.configure(fg_color=COLORS["bg"])
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._log_path = log_path
        self._log_error = log_error

        renamed = sum(1 for result in report.results if result.status == "renamed")
        errors = sum(1 for result in report.results if result.status == "error")
        skipped = sum(1 for result in report.results if result.status == "skipped")

        ctk.CTkLabel(
            self,
            text="Готово" if not errors else "Готово, но есть ошибки",
            anchor="w",
            font=font(20, "bold"),
            text_color=COLORS["green_text"] if not errors else COLORS["orange_text"],
        ).grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 4))

        summary = f"Переименовано: {renamed}   ·   Ошибок: {errors}   ·   Пропущено: {skipped}"
        ctk.CTkLabel(
            self,
            text=summary,
            anchor="w",
            font=font(12),
            text_color=COLORS["muted"],
        ).grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 10))

        self.textbox = ctk.CTkTextbox(
            self,
            font=mono(11),
            fg_color=COLORS["panel"],
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["text"],
        )
        self.textbox.grid(row=2, column=0, sticky="nsew", padx=24, pady=(0, 12))
        _set_textbox(self.textbox, self._lines(report))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", padx=24, pady=(0, 20))
        ctk.CTkButton(
            buttons,
            text="Закрыть",
            width=120,
            height=36,
            font=font(12),
            fg_color=COLORS["panel_2"],
            hover_color=COLORS["border_light"],
            command=self.destroy,
        ).pack(side="right")
        self._open_button = ctk.CTkButton(
            buttons,
            text="Открыть журнал",
            width=160,
            height=36,
            font=font(12, "bold"),
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            command=self._open_log,
        )
        self._open_button.pack(side="right", padx=(0, 10))
        if log_error or not os.path.isfile(log_path):
            self._open_button.configure(state="disabled")

        if log_error:
            ctk.CTkLabel(
                self,
                text=f"Журнал не записан: {log_error}",
                anchor="w",
                wraplength=720,
                font=font(11),
                text_color=COLORS["red_text"],
            ).grid(row=4, column=0, sticky="ew", padx=24, pady=(0, 16))

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        _center(self, master, 820, 560)
        self.update_idletasks()
        self.grab_set()
        self.lift()
        self.focus_force()

    @staticmethod
    def _lines(report: ApplyReport) -> str:
        lines: list[str] = []
        for result in report.results:
            if result.status == "renamed":
                lines.append(f"✓ {result.old_rel}  →  {result.new_rel}")
            elif result.status == "error":
                lines.append(f"✗ {result.old_rel}: {result.message}")
            elif result.status == "skipped":
                lines.append(f"· {result.old_rel}: пропущено")
        return "\n".join(lines) or "Нет изменений."

    def _open_log(self) -> None:
        if not os.path.isfile(self._log_path):
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(self._log_path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", self._log_path])
        except OSError as exc:
            self._open_button.configure(text=f"Не открылось: {clip(str(exc), 24)}")


def show_apply_result(
    master,
    report: ApplyReport,
    *,
    log_path: str,
    log_error: str = "",
) -> None:
    dialog = ApplyResultDialog(master, report, log_path=log_path, log_error=log_error)
    master.wait_window(dialog)


class CandidateDialog(ctk.CTkToplevel):
    """Подробный выбор альтернативной сцены."""

    def __init__(self, master, row: PreviewRow) -> None:
        super().__init__(master)
        self.result: Scene | None = None
        self.title("Альтернативные кандидаты")
        self.configure(fg_color=COLORS["bg"])
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())

        ctk.CTkLabel(
            self,
            text="Альтернативные кандидаты",
            anchor="w",
            font=font(18, "bold"),
            text_color=COLORS["accent"],
        ).pack(fill="x", padx=24, pady=(20, 4))
        ctk.CTkLabel(
            self,
            text="Выберите сцену — имя и дата будут взяты из её метаданных.",
            anchor="w",
            font=font(11),
            text_color=COLORS["muted"],
        ).pack(fill="x", padx=24, pady=(0, 12))

        list_frame = ctk.CTkScrollableFrame(
            self,
            fg_color=COLORS["panel"],
            corner_radius=10,
            scrollbar_button_color=COLORS["border_light"],
        )
        list_frame.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        list_frame.grid_columnconfigure(0, weight=1)

        seen: set[str] = set()
        position = 0
        for scene in row.candidates:
            if scene.id in seen:
                continue
            seen.add(scene.id)
            ctk.CTkButton(
                list_frame,
                text=_scene_text(scene),
                anchor="w",
                height=58,
                font=font(12),
                fg_color=COLORS["panel_2"],
                hover_color=COLORS["row_selected"],
                command=lambda item=scene: self._pick(item),
            ).grid(row=position, column=0, sticky="ew", padx=4, pady=3)
            position += 1
        if position == 0:
            ctk.CTkLabel(
                list_frame,
                text="Кандидатов нет.",
                font=font(12),
                text_color=COLORS["muted"],
            ).grid(row=0, column=0, pady=40)

        ctk.CTkButton(
            self,
            text="Отмена",
            width=120,
            height=36,
            font=font(12),
            fg_color=COLORS["panel_2"],
            hover_color=COLORS["border_light"],
            command=self.destroy,
        ).pack(anchor="e", padx=24, pady=(0, 20))

        _center(self, master, 760, 560)
        self.update_idletasks()
        self.grab_set()
        self.lift()
        self.focus_force()

    def _pick(self, scene: Scene) -> None:
        self.result = scene
        self.destroy()


def choose_candidate(master, row: PreviewRow) -> Scene | None:
    dialog = CandidateDialog(master, row)
    master.wait_window(dialog)
    return dialog.result


class StudioDialog(ctk.CTkToplevel):
    """Запуск скраперов студий с потоковым логом."""

    _TASKS = (
        ("stashdb", "StashDB"),
        ("fansdb", "FansDB"),
        ("theporndb", "ThePornDB"),
        ("merge", "Объединить"),
    )

    def __init__(
        self,
        master,
        dispatcher: UiDispatcher,
        *,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.dispatcher = dispatcher
        self.on_finished = on_finished
        self._running = False
        self._close_when_done = False
        self._cancel = threading.Event()
        self._task_buttons: list[ctk.CTkButton] = []

        self.title("Студии и справочники")
        self.configure(fg_color=COLORS["bg"])
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda _event: self._on_close())

        ctk.CTkLabel(
            self,
            text="Студии и справочники",
            anchor="w",
            font=font(18, "bold"),
            text_color=COLORS["accent"],
        ).pack(fill="x", padx=24, pady=(20, 4))
        ctk.CTkLabel(
            self,
            text="Скраперы обновляют data/studios.json. Операцию можно остановить.",
            anchor="w",
            font=font(11),
            text_color=COLORS["muted"],
        ).pack(fill="x", padx=24, pady=(0, 10))

        tasks = ctk.CTkFrame(self, fg_color="transparent")
        tasks.pack(fill="x", padx=24)
        for kind, label in self._TASKS:
            button = ctk.CTkButton(
                tasks,
                text=label,
                height=36,
                font=font(12),
                fg_color=COLORS["panel_2"],
                hover_color=COLORS["row_selected"],
                command=lambda value=kind: self._start(value),
            )
            button.pack(side="left", expand=True, fill="x", padx=(0, 6))
            self._task_buttons.append(button)

        self.log = ctk.CTkTextbox(
            self,
            font=mono(11),
            fg_color=COLORS["panel"],
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["text"],
        )
        self.log.pack(fill="both", expand=True, padx=24, pady=(12, 8))
        self.log.configure(state="disabled")

        self.status_label = ctk.CTkLabel(
            self,
            text="Готово к запуску.",
            anchor="w",
            font=font(11),
            text_color=COLORS["muted"],
        )
        self.status_label.pack(fill="x", padx=24)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=24, pady=(8, 20))
        self.stop_button = ctk.CTkButton(
            buttons,
            text="Остановить",
            width=140,
            height=36,
            font=font(12),
            fg_color=COLORS["orange"],
            hover_color=COLORS["orange_hover"],
            state="disabled",
            command=self._stop,
        )
        self.stop_button.pack(side="left")
        ctk.CTkButton(
            buttons,
            text="Закрыть",
            width=120,
            height=36,
            font=font(12),
            fg_color=COLORS["panel_2"],
            hover_color=COLORS["border_light"],
            command=self._on_close,
        ).pack(side="right")

        _center(self, master, 760, 620)
        self.update_idletasks()
        self.grab_set()
        self.lift()
        self.focus_force()

    def _append_line(self, line: str) -> None:
        if not self.winfo_exists():
            return
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text: str, color: str = COLORS["muted"]) -> None:
        self.status_label.configure(text=text, text_color=color)

    def _set_running(self, running: bool) -> None:
        self._running = running
        state = "disabled" if running else "normal"
        for button in self._task_buttons:
            button.configure(state=state)
        self.stop_button.configure(state="normal" if running else "disabled")

    def _start(self, kind: str) -> None:
        if self._running:
            return
        self._close_when_done = False
        self._cancel = threading.Event()
        self._set_running(True)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._append_line(f"Запуск: {kind}")
        self._set_status("Выполняется...", COLORS["accent"])
        run_in_thread(
            lambda: self._run_task(kind),
            dispatcher=self.dispatcher,
            on_success=self._done,
            on_error=self._failed,
            name=f"studio-{kind}",
        )

    def _run_task(self, kind: str) -> int:
        return run_studio_task(
            kind,
            on_line=lambda line: self.dispatcher.call_soon(self._append_line, line),
            cancel_event=self._cancel,
        )

    def _done(self, code: int) -> None:
        self._set_running(False)
        if code == 0:
            message, color = "Готово.", COLORS["green_text"]
        elif code == 130:
            message, color = "Остановлено пользователем.", COLORS["orange_text"]
        else:
            message, color = f"Завершено с кодом {code}.", COLORS["red_text"]
        self._append_line(message)
        self._set_status(message, color)
        if self.on_finished is not None:
            self.on_finished()
        if self._close_when_done:
            self.destroy()

    def _failed(self, exc: Exception) -> None:
        self._set_running(False)
        message = f"Ошибка: {exc}"
        self._append_line(message)
        self._set_status(message, COLORS["red_text"])
        if self.on_finished is not None:
            self.on_finished()
        if self._close_when_done:
            self.destroy()

    def _stop(self) -> None:
        if self._running:
            self._cancel.set()
            self._set_status("Останавливаю...", COLORS["orange_text"])

    def _on_close(self) -> None:
        if self._running:
            self._close_when_done = True
            self._stop()
            return
        self.destroy()


class UndoDialog(ctk.CTkToplevel):
    """Активные строки журнала и откат выбранных переименований."""

    _MAX_ROWS = 300

    def __init__(
        self,
        master,
        dispatcher: UiDispatcher,
        *,
        log_path: str,
        current_folder: str,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.dispatcher = dispatcher
        self.log_path = log_path
        self.current_folder = current_folder
        self.on_finished = on_finished
        self._entries: list = []
        self._items: list[tuple[object, tk.BooleanVar]] = []
        self._busy = False
        self._pending_status: tuple[str, str] | None = None

        self.title("Журнал переименований")
        self.configure(fg_color=COLORS["bg"])
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda _event: self._on_close())

        ctk.CTkLabel(
            self,
            text="Журнал переименований",
            anchor="w",
            font=font(18, "bold"),
            text_color=COLORS["accent"],
        ).pack(fill="x", padx=24, pady=(20, 4))
        self.path_label = ctk.CTkLabel(
            self,
            text=clip(log_path, 95),
            anchor="w",
            font=mono(10),
            text_color=COLORS["muted"],
        )
        self.path_label.pack(fill="x", padx=24, pady=(0, 8))

        self.status_label = ctk.CTkLabel(
            self,
            text="Читаю журнал...",
            anchor="w",
            font=font(11),
            text_color=COLORS["muted"],
        )
        self.status_label.pack(fill="x", padx=24, pady=(0, 6))

        self.rows_frame = ctk.CTkScrollableFrame(
            self,
            fg_color=COLORS["panel"],
            corner_radius=10,
            scrollbar_button_color=COLORS["border_light"],
        )
        self.rows_frame.pack(fill="both", expand=True, padx=24, pady=(0, 8))
        self.rows_frame.grid_columnconfigure(0, weight=1)

        self.legacy_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self,
            text="Разрешить строки старого формата (корень = текущая папка)",
            variable=self.legacy_var,
            font=font(11),
            text_color=COLORS["muted"],
            fg_color=COLORS["orange"],
            hover_color=COLORS["orange_hover"],
        ).pack(fill="x", padx=24, pady=(0, 8))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=24, pady=(0, 20))
        self.undo_button = ctk.CTkButton(
            buttons,
            text="Откатить выбранные",
            width=190,
            height=36,
            font=font(12, "bold"),
            fg_color=COLORS["orange"],
            hover_color=COLORS["orange_hover"],
            state="disabled",
            command=self._start_undo,
        )
        self.undo_button.pack(side="left")
        ctk.CTkButton(
            buttons,
            text="Обновить",
            width=120,
            height=36,
            font=font(12),
            fg_color=COLORS["panel_2"],
            hover_color=COLORS["border_light"],
            command=self._reload,
        ).pack(side="left", padx=(10, 0))
        ctk.CTkButton(
            buttons,
            text="Закрыть",
            width=120,
            height=36,
            font=font(12),
            fg_color=COLORS["panel_2"],
            hover_color=COLORS["border_light"],
            command=self._on_close,
        ).pack(side="right")

        _center(self, master, 940, 660)
        self.update_idletasks()
        self.grab_set()
        self.lift()
        self.focus_force()
        self._reload()

    def _set_status(self, text: str, color: str = COLORS["muted"]) -> None:
        self.status_label.configure(text=text, text_color=color)

    def _clear_rows(self) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()
        self._items = []

    def _reload(self, done_status: tuple[str, str] | None = None) -> None:
        """Перечитать журнал. done_status показывается после загрузки списка."""
        if self._busy:
            return
        self._pending_status = done_status
        self._clear_rows()
        self._set_status("Читаю журнал...")
        self.undo_button.configure(state="disabled")
        run_in_thread(
            lambda: read_log(self.log_path),
            dispatcher=self.dispatcher,
            on_success=self._loaded,
            on_error=self._load_failed,
            name="undo-read-log",
        )

    def _loaded(self, entries: list) -> None:
        if not self.winfo_exists():
            return
        self._entries = entries
        active = list(reversed(active_renames(entries)))
        shown = active[: self._MAX_ROWS]
        for position, entry in enumerate(shown):
            variable = tk.BooleanVar(value=False)
            self._items.append((entry, variable))
            caption = f"{entry.timestamp[:19]}   {entry.old}  →  {entry.new}"
            ctk.CTkCheckBox(
                self.rows_frame,
                text=clip(caption, 118),
                variable=variable,
                font=font(11),
                text_color=COLORS["text"],
                fg_color=COLORS["green"],
                hover_color=COLORS["green_hover"],
                command=self._sync_button,
            ).grid(row=position, column=0, sticky="w", padx=6, pady=2)
        suffix = f" Показаны последние {self._MAX_ROWS}." if len(active) > len(shown) else ""
        summary = f"Активных строк: {len(active)}.{suffix}"
        pending = self._pending_status
        self._pending_status = None
        if pending is not None:
            self._set_status(f"{pending[0]} · {summary}", pending[1])
        else:
            self._set_status(summary)
        self._sync_button()

    def _load_failed(self, exc: Exception) -> None:
        if not self.winfo_exists():
            return
        pending = self._pending_status
        self._pending_status = None
        if pending is not None:
            self._set_status(
                f"{pending[0]} Список не обновлён: {exc}",
                COLORS["red_text"],
            )
        else:
            self._set_status(f"Не удалось прочитать журнал: {exc}", COLORS["red_text"])

    def _sync_button(self) -> None:
        selected = any(variable.get() for _entry, variable in self._items)
        if self._busy:
            self.undo_button.configure(state="disabled")
        else:
            self.undo_button.configure(state="normal" if selected else "disabled")

    def _selected(self) -> list[str]:
        return [entry.id for entry, variable in self._items if variable.get()]

    def _start_undo(self) -> None:
        if self._busy:
            return
        ids = self._selected()
        if not ids:
            self._set_status("Ничего не выбрано.", COLORS["orange_text"])
            return
        has_legacy = any(entry.legacy for entry, variable in self._items if variable.get())
        if has_legacy and not self.current_folder:
            self._set_status(
                "Для старых строк нужна открытая папка или ручной --root.",
                COLORS["red_text"],
            )
            return
        root = self.current_folder if has_legacy else None
        selection = select_for_undo(
            self._entries,
            ids,
            root_override=root,
            confirm_legacy=has_legacy,
        )
        if not selection.targets:
            details = "; ".join(selection.errors[:3]) or "нет активных строк"
            self._set_status(f"Нет строк для отката: {details}", COLORS["red_text"])
            return
        self._busy = True
        self._sync_button()
        self._set_status("Проверяю и откатываю...", COLORS["accent"])
        run_in_thread(
            lambda: run_undo(selection, log_path=self.log_path, dry_run=True),
            dispatcher=self.dispatcher,
            on_success=lambda preview: self._preview_ready(ids, has_legacy, preview, selection),
            on_error=self._undo_failed,
            name="undo-preview",
        )

    def _preview_ready(self, ids: list[str], has_legacy: bool, preview, selection) -> None:
        self._busy = False
        self._sync_button()
        lines = [
            f"{result.old_rel} → {result.new_rel}: {result.status} {result.message}".rstrip()
            for result in preview.results
        ]
        lines += [f"пропуск: {error}" for error in selection.errors]
        body = "Откат выбранных строк (предпросмотр):\n" + "\n".join(lines)
        if has_legacy:
            body += f"\nКорень для строк старого формата: {self.current_folder}"
        confirmed = ask_confirm(
            self,
            "Откатить переименования?",
            body,
            confirm_text="Откатить",
            accent=COLORS["orange"],
            width=760,
            height=420,
        )
        self.grab_set()
        self.lift()
        if not confirmed:
            self._set_status("Откат отменён.")
            return
        self._busy = True
        self._sync_button()
        self._set_status("Откатываю...", COLORS["accent"])
        run_in_thread(
            lambda: self._run_undo(ids, has_legacy),
            dispatcher=self.dispatcher,
            on_success=self._undo_done,
            on_error=self._undo_failed,
            name="undo-run",
        )

    def _run_undo(self, ids: list[str], has_legacy: bool):
        entries = read_log(self.log_path)
        selection = select_for_undo(
            entries,
            ids,
            root_override=self.current_folder if has_legacy else None,
            confirm_legacy=has_legacy,
        )
        report = run_undo(selection, log_path=self.log_path, dry_run=False)
        return report, selection

    def _undo_done(self, result) -> None:
        report, selection = result
        self._busy = False
        renamed = sum(1 for item in report.results if item.status == "renamed")
        failures = [
            f"{item.id}: {item.message or item.status}"
            for item in report.results
            if item.status not in ("renamed", "dry-run")
        ]
        failures += [f"пропуск: {error}" for error in selection.errors]
        message = f"Откачено: {renamed}, ошибок: {len(failures)}"
        if failures:
            message += "; " + "; ".join(failures[:3])
            color = COLORS["red_text"]
        else:
            color = COLORS["green_text"]
        if self.on_finished is not None:
            self.on_finished()
        self._reload((message, color))

    def _undo_failed(self, exc: Exception) -> None:
        self._busy = False
        self._sync_button()
        self._set_status(f"Откат не выполнен: {exc}", COLORS["red_text"])

    def _on_close(self) -> None:
        if self._busy:
            self._set_status("Дождитесь завершения отката.", COLORS["orange_text"])
            return
        self.destroy()


__all__ = [
    "ApplyResultDialog",
    "CandidateDialog",
    "ConfirmDialog",
    "StudioDialog",
    "UndoDialog",
    "ask_confirm",
    "choose_candidate",
    "show_apply_result",
    "show_notice",
]
