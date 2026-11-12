"""Виртуализированная таблица: пул RowView переиспользуется при скролле."""

from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from video_renamer.core.stashdb_client import Scene
from video_renamer.core.title_service import PreviewRow
from video_renamer.gui.theme import COLORS, font, mono
from video_renamer.gui.widgets import RowView, configure_row_columns

ROW_HEIGHT = 56
BUFFER_ROWS = 4
WHEEL_ROWS = 3


class VirtualRowsView(ctk.CTkFrame):
    """Canvas со строками фиксированной высоты и небольшим пулом виджетов."""

    def __init__(
        self,
        master,
        *,
        on_select: Callable[[int, bool], None],
        on_toggle: Callable[[int, bool], None],
        on_candidate: Callable[[int, Scene], None],
        on_name: Callable[[int, str], None],
    ) -> None:
        super().__init__(master, fg_color="transparent", corner_radius=0)
        self._on_select = on_select
        self._on_toggle = on_toggle
        self._on_candidate = on_candidate
        self._on_name = on_name
        try:
            scale = ctk.ScalingTracker.get_widget_scaling(self)
        except Exception:
            scale = 1.0
        self._row_height = max(46, round(ROW_HEIGHT * scale))

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            self,
            bg=COLORS["bg"],
            highlightthickness=0,
            bd=0,
            takefocus=0,
            yscrollincrement=self._row_height,
        )
        self.scrollbar = ctk.CTkScrollbar(
            self,
            command=self.canvas.yview,
            width=14,
            fg_color=COLORS["panel"],
            button_color=COLORS["border_light"],
            button_hover_color=COLORS["border"],
        )
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")

        self.inner = tk.Frame(self.canvas, bg=COLORS["bg"], highlightthickness=0, bd=0)
        self._window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self._rows: list[tuple[int, PreviewRow]] = []
        self._selected_index: int | None = None
        self._enabled = True
        self._pool: list[RowView] = []
        self._free: list[RowView] = []
        self._bound_by_pos: dict[int, RowView] = {}
        self._bound_by_index: dict[int, RowView] = {}
        self._canvas_width = 1
        self._pool_job: str | None = None

        self._empty_frame = self._build_empty_frame()
        self._skeleton_frame: ctk.CTkFrame | None = None
        self._skeleton_labels: list[ctk.CTkLabel] = []
        self._skeleton_job: str | None = None
        self._skeleton_phase = 0

        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.inner)

    # --- данные ----------------------------------------------------------

    def set_rows(
        self,
        rows: list[tuple[int, PreviewRow]],
        *,
        reset_scroll: bool = False,
    ) -> None:
        self.hide_skeleton()
        self._cancel_pool_job()
        self._rows = list(rows)
        self._release_all()
        self._update_scrollregion()
        if reset_scroll:
            self.canvas.yview_moveto(0)
        if self._rows:
            self._hide_empty()
        else:
            self._show_empty()
        self._refresh_pool()

    def set_selected_index(self, index: int | None) -> None:
        self._selected_index = index
        for position, view in self._bound_by_pos.items():
            view.set_selected(self._rows[position][0] == index)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        for view in self._bound_by_pos.values():
            view.set_enabled(enabled)

    def view_for(self, index: int) -> RowView | None:
        return self._bound_by_index.get(index)

    def ensure_visible(self, position: int) -> None:
        """Прокручивает холст так, чтобы строка position попала в окно."""
        if not (0 <= position < len(self._rows)):
            return
        viewport = self.canvas.winfo_height()
        if viewport <= 1:
            return
        total_height = max(1, len(self._rows) * self._row_height)
        top = self.canvas.canvasy(0)
        row_top = position * self._row_height
        row_bottom = row_top + self._row_height
        if row_top < top:
            target = row_top
        elif row_bottom > top + viewport:
            target = row_bottom - viewport
        else:
            return
        target = min(max(target, 0), max(0, total_height - viewport))
        self.canvas.yview_moveto(target / total_height)
        self._cancel_pool_job()
        self._refresh_pool()

    def _focused_row(self) -> RowView | None:
        widget = self.focus_get()
        while widget is not None:
            if isinstance(widget, RowView):
                return widget
            widget = getattr(widget, "master", None)
        return None

    def views(self) -> list[RowView]:
        return list(self._bound_by_pos.values())

    # --- пул строк -------------------------------------------------------

    def _acquire_row(self) -> RowView:
        if self._free:
            return self._free.pop()
        view = RowView(
            self.inner,
            index=-1,
            row=None,
            selected=False,
            striped=False,
            height=ROW_HEIGHT,  # CTk scales height to match _row_height
            on_select=self._on_select,
            on_toggle=self._on_toggle,
            on_candidate=self._on_candidate,
            on_name=self._on_name,
        )
        self._pool.append(view)
        self._bind_wheel_tree(view)
        return view

    def _release_all(self) -> None:
        focused_view = self._focused_row()
        for view in self._bound_by_pos.values():
            if view is focused_view:
                view.suspend_edit()
                self.focus_set()
            view.place_forget()
            self._free.append(view)
        self._bound_by_pos.clear()
        self._bound_by_index.clear()

    def _refresh_pool(self) -> None:
        if not self._rows:
            return
        top = max(0, int(self.canvas.canvasy(0) // self._row_height))
        viewport = self.canvas.winfo_height()
        if viewport <= 1:
            viewport = 640
        visible_rows = math.ceil(viewport / self._row_height)
        first = max(0, top - BUFFER_ROWS)
        last = min(len(self._rows), top + visible_rows + BUFFER_ROWS + 1)
        if first >= last:
            first = max(0, len(self._rows) - 1)
            last = len(self._rows)
        needed = set(range(first, last))
        focused_view = self._focused_row()

        for position in list(self._bound_by_pos):
            if position in needed:
                continue
            view = self._bound_by_pos[position]
            if view is focused_view:
                view.suspend_edit()
                self.focus_set()
            self._bound_by_pos.pop(position)
            index = self._rows[position][0]
            self._bound_by_index.pop(index, None)
            view.place_forget()
            self._free.append(view)

        for position in range(first, last):
            if position in self._bound_by_pos:
                continue
            index, row = self._rows[position]
            view = self._acquire_row()
            view.bind_row(
                index,
                row,
                selected=index == self._selected_index,
                striped=position % 2 == 1,
            )
            view.set_enabled(self._enabled)
            view.place(x=0, y=position * self._row_height, relwidth=1)
            self._bound_by_pos[position] = view
            self._bound_by_index[index] = view

    def _update_scrollregion(self) -> None:
        height = max(1, len(self._rows) * self._row_height)
        self.inner.configure(width=max(self._canvas_width, 1), height=height)
        self.canvas.configure(scrollregion=(0, 0, max(self._canvas_width, 1), height))

    # --- события ---------------------------------------------------------

    def _on_canvas_configure(self, event) -> None:
        self._canvas_width = max(event.width, 1)
        self.canvas.itemconfigure(self._window_id, width=self._canvas_width)
        self._cancel_pool_job()
        self._update_scrollregion()
        self._refresh_pool()

    def _on_yscroll(self, first: str, last: str) -> None:
        self.scrollbar.set(first, last)
        self._schedule_pool_refresh()

    def _schedule_pool_refresh(self) -> None:
        """Склеивает серию событий скролла в один пересчёт пула."""
        if self._pool_job is None:
            self._pool_job = self.after(16, self._run_pool_refresh)

    def _run_pool_refresh(self) -> None:
        self._pool_job = None
        self._refresh_pool()

    def _cancel_pool_job(self) -> None:
        if self._pool_job is not None:
            try:
                self.after_cancel(self._pool_job)
            except Exception:
                pass
            self._pool_job = None

    def _on_mousewheel(self, event) -> str:
        focused = self.focus_get()
        if isinstance(focused, (tk.Entry, tk.Text)):
            return "break"
        if getattr(event, "num", None) == 4:
            direction = -1
        elif getattr(event, "num", None) == 5:
            direction = 1
        else:
            direction = -1 if getattr(event, "delta", 0) > 0 else 1
        self.canvas.yview_scroll(direction * WHEEL_ROWS, "units")
        return "break"

    def _bind_wheel(self, widget) -> None:
        widget.bind("<MouseWheel>", self._on_mousewheel, add="+")
        widget.bind("<Button-4>", self._on_mousewheel, add="+")
        widget.bind("<Button-5>", self._on_mousewheel, add="+")

    def _bind_wheel_tree(self, widget) -> None:
        self._bind_wheel(widget)
        for child in widget.winfo_children():
            self._bind_wheel_tree(child)

    # --- пустое состояние ------------------------------------------------

    def _build_empty_frame(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self, fg_color="transparent")
        ctk.CTkLabel(
            frame,
            text="Нет строк",
            font=font(18, "bold"),
            text_color=COLORS["muted"],
        ).pack()
        ctk.CTkLabel(
            frame,
            text="Нажмите Preview, чтобы собрать предложения.",
            font=font(12),
            text_color=COLORS["muted"],
        ).pack(pady=(6, 0))
        return frame

    def _show_empty(self) -> None:
        self._empty_frame.place(relx=0.5, rely=0.5, anchor="center")

    def _hide_empty(self) -> None:
        self._empty_frame.place_forget()

    # --- skeleton --------------------------------------------------------

    def show_skeleton(self) -> None:
        self.hide_skeleton()
        self._hide_empty()
        frame = ctk.CTkFrame(self, fg_color=COLORS["bg"], corner_radius=0)
        frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frame,
            text="Сканирую папку и подбираю совпадения…",
            anchor="w",
            font=font(12, "bold"),
            text_color=COLORS["accent"],
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(18, 8))

        self._skeleton_labels = []
        for position in range(6):
            card = ctk.CTkFrame(
                frame,
                fg_color=COLORS["row_alt"] if position % 2 else COLORS["row"],
                corner_radius=8,
                height=46,
            )
            card.grid(row=position + 1, column=0, sticky="ew", padx=4, pady=2)
            configure_row_columns(card)
            bars = (
                (0, "□", (10, 2)),
                (1, "▰▰▰▰▰▰▰▰▰▰▰▰", (4, 4)),
                (2, "▰▰▰▰▰▰▰▰▰", (4, 4)),
                (3, "▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰", (4, 4)),
                (4, "▰▰▰▰▰▰", (4, 4)),
                (5, "▰▰", (4, 10)),
            )
            for column, text, padding in bars:
                label = ctk.CTkLabel(
                    card,
                    text=text,
                    anchor="w" if column < 5 else "e",
                    font=mono(12),
                    text_color=COLORS["border_light"],
                )
                label.grid(row=0, column=column, padx=padding, pady=8, sticky="ew")
                self._skeleton_labels.append(label)
        self._skeleton_frame = frame
        self._skeleton_phase = 0
        self._pulse_skeleton()

    def hide_skeleton(self) -> None:
        self._stop_skeleton()
        if self._skeleton_frame is not None:
            self._skeleton_frame.destroy()
            self._skeleton_frame = None
        if not self._rows:
            self._show_empty()

    def _pulse_skeleton(self) -> None:
        if self._skeleton_frame is None:
            return
        color = COLORS["border_light"] if self._skeleton_phase else COLORS["border"]
        for label in self._skeleton_labels:
            try:
                if label.winfo_exists():
                    label.configure(text_color=color)
            except Exception:
                pass
        self._skeleton_phase = 1 - self._skeleton_phase
        self._skeleton_job = self.after(450, self._pulse_skeleton)

    def _stop_skeleton(self) -> None:
        if self._skeleton_job is not None:
            try:
                self.after_cancel(self._skeleton_job)
            except Exception:
                pass
            self._skeleton_job = None
        self._skeleton_labels.clear()

    def destroy(self) -> None:
        self._cancel_pool_job()
        self._stop_skeleton()
        super().destroy()
