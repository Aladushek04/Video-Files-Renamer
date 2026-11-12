"""Тесты виртуализированной таблицы: пул строк переиспользуется при скролле."""

from __future__ import annotations

import pytest

pytest.importorskip("customtkinter")

from video_renamer.core.title_service import PreviewRow
from video_renamer.gui.theme import COLORS
from video_renamer.gui.virtual_rows import VirtualRowsView


def make_row(index: int) -> PreviewRow:
    return PreviewRow(
        False,
        f"old{index}.mp4",
        f"New{index}.mp4",
        "fingerprint",
        1.0,
        f"s{index}",
        "",
        "h",
        10,
        (),
    )


def make_rows(count: int) -> list[tuple[int, PreviewRow]]:
    return [(index, make_row(index)) for index in range(count)]


def make_view(root) -> VirtualRowsView:
    return VirtualRowsView(
        root,
        on_select=lambda index, focus: None,
        on_toggle=lambda index, approved: None,
        on_candidate=lambda index, scene: None,
        on_name=lambda index, value: None,
    )


def scroll_to(view: VirtualRowsView, top_row: int) -> None:
    """Имитирует прокрутку: без окна геометрия холста равна 1 px."""
    view.canvas.canvasy = lambda _y, top=top_row * view._row_height: top
    view.canvas.winfo_height = lambda: view._row_height * 20
    view._refresh_pool()


def test_pool_is_reused_instead_of_creating_new_rows(ctk_root):
    view = make_view(ctk_root)
    scroll_to(view, 0)
    view.set_rows(make_rows(200))

    scroll_to(view, 50)
    pool = list(view._pool)
    bound_before = view.view_for(70)
    assert bound_before is not None

    scroll_to(view, 100)

    assert view.view_for(70) is None
    assert view.view_for(100) is not None
    assert view.view_for(100).index == 100
    assert set(view._pool) == set(pool)
    assert bound_before in view._pool


def test_only_window_rows_are_bound(ctk_root):
    view = make_view(ctk_root)
    scroll_to(view, 0)
    view.set_rows(make_rows(200))

    scroll_to(view, 100)

    assert view.view_for(95) is None
    assert view.view_for(96) is not None
    assert view.view_for(124) is not None
    assert view.view_for(125) is None
    assert len(view.views()) == len(view._pool)


def test_selection_survives_rebinding(ctk_root):
    view = make_view(ctk_root)
    scroll_to(view, 0)
    view.set_rows(make_rows(200))
    view.set_selected_index(100)

    scroll_to(view, 100)
    assert view.view_for(100).cget("fg_color") == COLORS["row_selected"]

    scroll_to(view, 0)
    assert view.view_for(0) is not None
    assert view.view_for(0).cget("fg_color") != COLORS["row_selected"]
    assert view.view_for(100) is None


def test_disabled_state_applies_to_rows_bound_later(ctk_root):
    view = make_view(ctk_root)
    scroll_to(view, 0)
    view.set_rows(make_rows(200))
    view.set_enabled(False)

    scroll_to(view, 100)

    assert view.view_for(100).checkbox.cget("state") == "disabled"


def test_set_rows_with_reset_scroll_returns_to_top(ctk_root):
    view = make_view(ctk_root)
    real_canvasy = view.canvas.canvasy
    scroll_to(view, 0)
    view.set_rows(make_rows(200))
    scroll_to(view, 100)
    assert view.view_for(100) is not None

    view.canvas.canvasy = real_canvasy
    view.set_rows(make_rows(50), reset_scroll=True)

    assert view.view_for(0) is not None
    assert view.view_for(100) is None


def test_empty_rows_show_placeholder(ctk_root):
    view = make_view(ctk_root)

    view.set_rows(make_rows(3))
    assert view._empty_frame.winfo_manager() == ""

    view.set_rows([])
    assert view.views() == []
    assert view._empty_frame.winfo_manager() == "place"
