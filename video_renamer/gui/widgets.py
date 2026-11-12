"""Виджеты таблицы ревью: строка, бейджи и сетка колонок."""

from __future__ import annotations

import os
import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from video_renamer.core.stashdb_client import Scene
from video_renamer.core.title_service import PreviewRow
from video_renamer.gui.theme import (
    COLORS,
    clip,
    font,
    mono,
    source_label,
    source_palette,
)

_COLUMNS: tuple[tuple[int, int], ...] = (
    (40, 0),
    (250, 0),
    (190, 0),
    (150, 1),
    (104, 0),
    (54, 0),
)


def configure_row_columns(frame: ctk.CTkFrame) -> None:
    """Одинаковая сетка для заголовка и строк, чтобы колонки совпадали."""
    for column, (minsize, weight) in enumerate(_COLUMNS):
        frame.grid_columnconfigure(column, minsize=minsize, weight=weight)


def _candidate_label(scene: Scene, number: int) -> str:
    title = scene.title or "(без названия)"
    studio = scene.studio.name if scene.studio else "—"
    return f"{number}. {clip(title, 31)} · {clip(studio, 14)}"


class RowView(ctk.CTkFrame):
    """Одна строка таблицы: approve, старое имя, кандидаты, новое имя."""

    def __init__(
        self,
        master,
        *,
        index: int = -1,
        row: PreviewRow | None = None,
        selected: bool = False,
        striped: bool = False,
        height: int | None = None,
        on_select: Callable[[int, bool], None],
        on_toggle: Callable[[int, bool], None],
        on_candidate: Callable[[int, Scene], None],
        on_name: Callable[[int, str], None],
    ) -> None:
        base = COLORS["row_alt"] if striped else COLORS["row"]
        options = {
            "fg_color": base,
            "corner_radius": 8,
            "border_width": 1,
            "border_color": COLORS["border"],
        }
        if height is not None:
            options["height"] = height
        super().__init__(master, **options)
        if height is not None:
            self.grid_propagate(False)
        self.index = index
        self._base_color = base
        self._on_select = on_select
        self._on_toggle = on_toggle
        self._on_candidate = on_candidate
        self._on_name = on_name
        self._updating = False
        self._suppress_focusout = False
        self._current_basename = ""
        self._candidate_map: dict[str, Scene] = {}
        self._candidate_values: list[str] = []

        configure_row_columns(self)

        self.approved_var = tk.BooleanVar(value=row.approved if row is not None else False)
        self.checkbox = ctk.CTkCheckBox(
            self,
            text="",
            variable=self.approved_var,
            onvalue=True,
            offvalue=False,
            width=24,
            height=24,
            checkbox_width=18,
            checkbox_height=18,
            corner_radius=5,
            border_width=2,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            border_color=COLORS["border_light"],
            command=self._toggle,
        )
        self.checkbox.grid(row=0, column=0, padx=(10, 2), pady=8, sticky="w")

        self.old_label = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            justify="left",
            font=font(12),
            text_color=COLORS["text"],
        )
        self.old_label.grid(row=0, column=1, padx=4, pady=8, sticky="ew")

        self.candidate_menu = ctk.CTkOptionMenu(
            self,
            values=["—"],
            width=180,
            height=28,
            dynamic_resizing=False,
            font=font(11),
            fg_color=COLORS["panel_2"],
            button_color=COLORS["border_light"],
            button_hover_color=COLORS["border"],
            dropdown_fg_color=COLORS["panel"],
            dropdown_hover_color=COLORS["row_selected"],
            dropdown_text_color=COLORS["text"],
            text_color=COLORS["text"],
            command=self._candidate_chosen,
        )
        self.candidate_menu.grid(row=0, column=2, padx=4, pady=8, sticky="ew")

        self.name_entry = ctk.CTkEntry(
            self,
            height=30,
            font=font(12),
            fg_color=COLORS["bg"],
            border_color=COLORS["border_light"],
            text_color=COLORS["text"],
        )
        self.name_entry.grid(row=0, column=3, padx=4, pady=8, sticky="ew")
        self.name_entry.bind("<Return>", self._commit_name)
        self.name_entry.bind("<FocusOut>", self._commit_on_focus_out)
        self.name_entry.bind("<FocusIn>", self._on_entry_focus)

        self.source_badge = ctk.CTkLabel(
            self,
            text="",
            width=96,
            height=26,
            corner_radius=8,
            font=font(10, "bold"),
        )
        self.source_badge.grid(row=0, column=4, padx=4, pady=8, sticky="ew")

        self.confidence_label = ctk.CTkLabel(
            self,
            text="",
            width=40,
            font=mono(11),
            text_color=COLORS["muted"],
        )
        self.confidence_label.grid(row=0, column=5, padx=(4, 10), pady=8, sticky="e")

        self.old_label.bind("<Button-1>", lambda _event: self._select())
        self.bind("<Button-1>", lambda _event: self._select())
        self.set_selected(selected)
        if row is not None:
            self.refresh(row)

    def bind_row(
        self,
        index: int,
        row: PreviewRow,
        *,
        selected: bool,
        striped: bool,
    ) -> None:
        """Переиспользует виджет для другой строки при виртуальном скролле."""
        self.index = index
        self._suppress_focusout = False
        self._base_color = COLORS["row_alt"] if striped else COLORS["row"]
        self.set_selected(selected)
        self.refresh(row)

    def suspend_edit(self) -> None:
        """Не коммитить FocusOut, когда пул забирает строку под другую запись."""
        self._updating = True
        self._suppress_focusout = True

    def _on_entry_focus(self, _event=None) -> None:
        self._suppress_focusout = False
        self._on_select(self.index, False)

    def _select(self) -> None:
        self._on_select(self.index, True)

    def _toggle(self) -> None:
        if self._updating:
            return
        self._select()
        self._on_toggle(self.index, bool(self.approved_var.get()))

    def _commit_name(self, _event=None) -> None:
        """Enter или явный вызов: отмена пула не должна его глушить."""
        if self._updating:
            return
        value = self.name_entry.get()
        if value != self._current_basename:
            self._on_name(self.index, value)

    def _commit_on_focus_out(self, _event=None) -> None:
        """FocusOut от перепривязки пула — не коммит: строка уже другая."""
        if self._suppress_focusout:
            self._suppress_focusout = False
            return
        self._commit_name()

    def _candidate_chosen(self, label: str) -> None:
        if self._updating:
            return
        scene = self._candidate_map.get(label)
        if scene is None:
            return
        self._select()
        self._on_candidate(self.index, scene)

    def refresh(self, row: PreviewRow) -> None:
        """Синхронизирует виджет с моделью без вызова пользовательских команд."""
        self._updating = True
        try:
            self.approved_var.set(row.approved)
            self.old_label.configure(text=clip(row.old_rel, 62))
            self._current_basename = os.path.basename(row.new_rel)
            self.name_entry.delete(0, "end")
            self.name_entry.insert(0, self._current_basename)

            background, foreground = source_palette(row.source)
            self.source_badge.configure(
                text=source_label(row.source),
                fg_color=background,
                text_color=foreground,
            )
            self.confidence_label.configure(text=f"{row.confidence:.2f}")

            self._candidate_map = {}
            self._candidate_values = []
            for number, scene in enumerate(self._unique_candidates(row), start=1):
                label = _candidate_label(scene, number)
                self._candidate_map[label] = scene
                self._candidate_values.append(label)
            if self._candidate_values:
                self.candidate_menu.configure(
                    values=self._candidate_values,
                    state="normal",
                )
                self.candidate_menu.set(f"Кандидаты · {len(self._candidate_values)}")
            else:
                self.candidate_menu.configure(values=["—"], state="disabled")
                self.candidate_menu.set("—")
        finally:
            self._updating = False

    @staticmethod
    def _unique_candidates(row: PreviewRow) -> list[Scene]:
        seen: set[str] = set()
        result: list[Scene] = []
        for scene in row.candidates:
            if scene.id in seen:
                continue
            seen.add(scene.id)
            result.append(scene)
        return result

    def reset_name(self) -> None:
        self._updating = True
        try:
            self.name_entry.delete(0, "end")
            self.name_entry.insert(0, self._current_basename)
        finally:
            self._updating = False

    def set_selected(self, selected: bool) -> None:
        self.configure(
            fg_color=COLORS["row_selected"] if selected else self._base_color,
            border_color=COLORS["accent"] if selected else COLORS["border"],
        )

    def set_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.checkbox.configure(state=state)
        self.name_entry.configure(state=state)
        if self._candidate_values:
            self.candidate_menu.configure(state=state)
        else:
            self.candidate_menu.configure(state="disabled")
