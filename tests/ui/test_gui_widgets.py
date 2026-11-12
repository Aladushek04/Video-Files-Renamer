"""Тесты строки таблицы и общей сетки колонок."""

from __future__ import annotations

import pytest

pytest.importorskip("customtkinter")

from video_renamer.core.stashdb_client import Scene, Studio
from video_renamer.core.title_service import PreviewRow
from video_renamer.gui.theme import COLORS
from video_renamer.gui.widgets import RowView, configure_row_columns


def make_row(**overrides) -> PreviewRow:
    values = {
        "approved": False,
        "old_rel": "old.mp4",
        "new_rel": "Studio - 01.01.2026 - Title.mp4",
        "source": "fingerprint",
        "confidence": 1.0,
        "scene_id": "s1",
        "note": "",
        "oshash": "h1",
        "size": 10,
        "candidates": (),
    }
    values.update(overrides)
    return PreviewRow(**values)


def make_scene(scene_id: str, title: str = "Title") -> Scene:
    return Scene(scene_id, title, "2026-01-01", None, None, 600, Studio("st1", "Studio"))


def make_view(root, row: PreviewRow | None = None, *, index: int = 3, callbacks=None) -> RowView:
    calls = callbacks if callbacks is not None else make_callbacks()
    return RowView(
        root,
        index=index,
        row=row,
        selected=False,
        striped=False,
        on_select=lambda index, focus: calls["select"].append((index, focus)),
        on_toggle=lambda index, approved: calls["toggle"].append((index, approved)),
        on_candidate=lambda index, scene: calls["candidate"].append((index, scene)),
        on_name=lambda index, value: calls["name"].append((index, value)),
    )


def make_callbacks() -> dict[str, list]:
    return {"select": [], "toggle": [], "candidate": [], "name": []}


def test_refresh_renders_row_fields(ctk_root):
    view = make_view(
        ctk_root,
        make_row(approved=True, source="text", confidence=0.75),
    )

    assert view.approved_var.get() is True
    assert view.old_label.cget("text") == "old.mp4"
    assert view.name_entry.get() == "Studio - 01.01.2026 - Title.mp4"
    assert view.source_badge.cget("text") == "Текст"
    assert view.confidence_label.cget("text") == "0.75"


def test_refresh_does_not_fire_user_callbacks(ctk_root):
    calls = make_callbacks()
    view = make_view(ctk_root, make_row(), callbacks=calls)

    view.refresh(make_row(approved=True, old_rel="other.mp4", new_rel="Other.mp4"))

    assert calls == {"select": [], "toggle": [], "candidate": [], "name": []}


def test_bind_row_updates_index_and_content(ctk_root):
    view = make_view(ctk_root, make_row(), index=0)
    striped = COLORS["row_alt"]

    view.bind_row(
        7,
        make_row(approved=True, old_rel="seven.mp4", new_rel="Seven.mp4", source="heuristic"),
        selected=False,
        striped=True,
    )

    assert view.index == 7
    assert view.old_label.cget("text") == "seven.mp4"
    assert view.name_entry.get() == "Seven.mp4"
    assert view.approved_var.get() is True
    assert view.source_badge.cget("text") == "Эвристика"
    assert view.cget("fg_color") == striped


def test_toggle_reports_index_and_new_state(ctk_root):
    calls = make_callbacks()
    view = make_view(ctk_root, make_row(approved=True), index=4, callbacks=calls)

    view.approved_var.set(False)
    view._toggle()

    assert calls["select"] == [(4, True)]
    assert calls["toggle"] == [(4, False)]


def test_commit_name_reports_changed_basename(ctk_root):
    calls = make_callbacks()
    view = make_view(ctk_root, make_row(new_rel="Old.mp4"), index=2, callbacks=calls)

    view.name_entry.delete(0, "end")
    view.name_entry.insert(0, "Changed.mp4")
    view._commit_name()

    assert calls["name"] == [(2, "Changed.mp4")]


def test_commit_name_is_ignored_while_pool_suspends_edit(ctk_root):
    calls = make_callbacks()
    view = make_view(ctk_root, make_row(), callbacks=calls)

    view.suspend_edit()
    view.name_entry.delete(0, "end")
    view.name_entry.insert(0, "Changed.mp4")
    view._commit_name()

    assert calls["name"] == []


def test_stale_focusout_does_not_swallow_next_commit(ctk_root):
    calls = make_callbacks()
    view = make_view(ctk_root, make_row(new_rel="Old.mp4"), callbacks=calls)

    view.suspend_edit()
    view._updating = False  # пул уже отпустил виджет, FocusOut ещё в очереди
    view.name_entry.delete(0, "end")
    view.name_entry.insert(0, "Changed.mp4")

    view._commit_on_focus_out()
    assert calls["name"] == []

    view._commit_name()
    assert calls["name"] == [(3, "Changed.mp4")]


def test_commit_after_rebinding_is_not_swallowed(ctk_root):
    calls = make_callbacks()
    view = make_view(ctk_root, make_row(new_rel="Old.mp4"), callbacks=calls)

    view.suspend_edit()
    view.bind_row(
        9,
        make_row(old_rel="nine.mp4", new_rel="Nine.mp4"),
        selected=False,
        striped=True,
    )
    view.name_entry.delete(0, "end")
    view.name_entry.insert(0, "Renamed.mp4")

    view._commit_name()

    assert calls["name"] == [(9, "Renamed.mp4")]


def test_set_enabled_disables_controls(ctk_root):
    view = make_view(ctk_root, make_row(candidates=(make_scene("s1"),)))

    view.set_enabled(False)

    assert view.checkbox.cget("state") == "disabled"
    assert view.name_entry.cget("state") == "disabled"
    assert view.candidate_menu.cget("state") == "disabled"


def test_candidate_choice_reports_mapped_scene(ctk_root):
    calls = make_callbacks()
    scene = make_scene("s9", "Picked")
    view = make_view(ctk_root, make_row(candidates=(make_scene("s1"), scene)), callbacks=calls)

    labels = view.candidate_menu.cget("values")
    assert len(labels) == 2
    view._candidate_chosen(labels[1])

    assert calls["candidate"] == [(3, scene)]
    assert calls["select"] == [(3, True)]


def test_unique_candidates_drops_duplicates():
    first = make_scene("s1", "One")
    second = make_scene("s2", "Two")

    unique = RowView._unique_candidates(make_row(candidates=(first, first, second)))

    assert [scene.id for scene in unique] == ["s1", "s2"]


def test_configure_row_columns_matches_header_grid(ctk_root):
    import customtkinter as ctk

    frame = ctk.CTkFrame(ctk_root)

    configure_row_columns(frame)

    assert frame.grid_columnconfigure(0)["minsize"] == 40
    assert frame.grid_columnconfigure(3)["minsize"] == 150
    assert frame.grid_columnconfigure(3)["weight"] == 1
    assert frame.grid_columnconfigure(5)["weight"] == 0
