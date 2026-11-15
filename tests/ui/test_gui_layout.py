"""Компоновка главного окна: кнопки и путь доступны на минимальном размере."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("customtkinter")

from video_renamer.gui.app import RenamerGUI
from video_renamer.gui.controller import RenamerController

MIN_SIZE = (1180, 700)
RECOMMENDED_SIZE = (1440, 900)


def _settle(window, rounds: int = 15) -> None:
    """Даёт Tk и CustomTkinter применить геометрию и пересчитать размеры."""
    for _ in range(rounds):
        window.update_idletasks()
        window.update()
        time.sleep(0.01)


def _rect(widget) -> tuple[int, int, int, int]:
    return (
        widget.winfo_rootx(),
        widget.winfo_rooty(),
        widget.winfo_width(),
        widget.winfo_height(),
    )


def _inside(widget, container) -> bool:
    x, y, width, height = _rect(widget)
    cx, cy, cwidth, cheight = _rect(container)
    return x >= cx and y >= cy and x + width <= cx + cwidth and y + height <= cy + cheight


def _mapped_size(widget) -> bool:
    return bool(widget.winfo_ismapped()) and widget.winfo_width() > 1 and widget.winfo_height() > 1


@pytest.fixture
def gui(tmp_path):
    tkinter = pytest.importorskip("tkinter")
    controller = RenamerController(log_path=str(tmp_path / "log.csv"))
    try:
        window = RenamerGUI(controller=controller)
    except (RuntimeError, tkinter.TclError) as exc:
        pytest.skip(f"Tk недоступен: {exc}")
    window.geometry("+3000+3000")
    _settle(window)
    try:
        yield window
    finally:
        window.destroy()


@pytest.mark.parametrize("size", [RECOMMENDED_SIZE, MIN_SIZE])
def test_sidebar_controls_are_visible_and_inside_the_window(gui, size):
    gui.geometry(f"{size[0]}x{size[1]}+3000+3000")
    _settle(gui)

    for name in (
        "folder_entry",
        "browse_button",
        "preview_button",
        "studio_button",
        "undo_button",
    ):
        widget = getattr(gui, name)
        assert _mapped_size(widget), name
        assert _inside(widget, gui), name


def test_footer_does_not_overlap_the_scrollable_body(gui):
    gui.geometry(f"{MIN_SIZE[0]}x{MIN_SIZE[1]}+3000+3000")
    _settle(gui)
    canvas = gui.sidebar_body._parent_canvas

    _, canvas_y, _, canvas_height = _rect(canvas)
    _, footer_y, _, _ = _rect(gui.studio_button.master)

    assert canvas_y + canvas_height <= footer_y


def test_stats_are_reachable_by_scrolling_at_minimum_size(gui):
    gui.geometry(f"{MIN_SIZE[0]}x{MIN_SIZE[1]}+3000+3000")
    _settle(gui)
    canvas = gui.sidebar_body._parent_canvas

    first, last = canvas.yview()
    assert (first, last) != (0.0, 1.0), "контент должен прокручиваться"

    canvas.yview_moveto(1.0)
    _settle(gui)

    stats = gui.stats_card
    canvas_rect = _rect(canvas)
    stats_rect = _rect(stats)
    assert stats_rect[1] >= canvas_rect[1]
    assert stats_rect[1] + stats_rect[3] <= canvas_rect[1] + canvas_rect[3]


def test_recommended_size_shows_everything_without_scrolling(gui):
    gui.geometry(f"{RECOMMENDED_SIZE[0]}x{RECOMMENDED_SIZE[1]}+3000+3000")
    _settle(gui)
    canvas = gui.sidebar_body._parent_canvas

    assert (
        _rect(gui.stats_card)[1] + _rect(gui.stats_card)[3] <= _rect(canvas)[1] + _rect(canvas)[3]
    )
    assert canvas.yview() == (0.0, 1.0) or _inside(gui.stats_card, canvas)


def test_folder_path_is_shown_and_field_is_wide_enough(gui, tmp_path):
    gui.geometry(f"{MIN_SIZE[0]}x{MIN_SIZE[1]}+3000+3000")
    assert gui._apply_folder(str(tmp_path)) is True
    _settle(gui)

    assert gui.folder_entry.get() == str(tmp_path)
    assert gui.folder_entry.winfo_width() >= 180


def test_keyboard_shortcuts_stay_bound(gui):
    for sequence in ("<F5>", "<Control-Return>", "<Control-o>", "<Escape>", "<space>"):
        assert gui.bind_all(sequence), sequence


@pytest.mark.parametrize("scaling", [1.25, 1.5])
def test_controls_stay_visible_with_widget_scaling(tmp_path, scaling):
    import customtkinter as ctk

    tkinter = pytest.importorskip("tkinter")
    ctk.set_widget_scaling(scaling)
    try:
        controller = RenamerController(log_path=str(tmp_path / "log.csv"))
        try:
            window = RenamerGUI(controller=controller)
        except (RuntimeError, tkinter.TclError) as exc:
            pytest.skip(f"Tk недоступен: {exc}")
        try:
            window.geometry(f"{MIN_SIZE[0]}x{MIN_SIZE[1]}+3000+3000")
            _settle(window)
            for name in ("studio_button", "undo_button", "folder_entry", "preview_button"):
                widget = getattr(window, name)
                assert _mapped_size(widget), name
                assert _inside(widget, window), name
        finally:
            window.destroy()
    finally:
        ctk.set_widget_scaling(1.0)
