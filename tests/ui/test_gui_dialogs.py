"""Тесты окна журнала и отката (`UndoDialog`) без реального окна.

Потоки заменены синхронным раннером, а захват фокуса и всплытие окна
отключены: в тестах важен порядок вызовов и итоговый статус, а не Tk-цикл.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("customtkinter")

from video_renamer.core.rename_log import ACTION_RENAME, LogEntry
from video_renamer.gui import dialogs
from video_renamer.gui.theme import COLORS


def make_entry(
    old: str,
    new: str,
    *,
    root: str = "C:/videos",
    legacy: bool = False,
) -> LogEntry:
    return LogEntry(
        timestamp="2026-11-11T10:00:00.000000Z",
        action=ACTION_RENAME,
        undoes="",
        root=root,
        old=old,
        new=new,
        oshash="h1",
        size=10,
        scene_id="s1",
        source="fingerprint",
        legacy=legacy,
    )


def make_result(old: str, new: str, status: str = "renamed", message: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        id=f"{old}->{new}",
        old_rel=old,
        new_rel=new,
        status=status,
        message=message,
    )


@pytest.fixture
def sync_runner(monkeypatch):
    """`run_in_thread` выполняется сразу в вызывающем потоке."""

    def run(
        work,
        *,
        dispatcher,
        on_success=None,
        on_error=None,
        on_cancelled=None,
        cancel_event=None,
        name="",
    ):
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - как в реальном раннере
            if on_error is not None:
                on_error(exc)
            return
        if on_success is not None:
            on_success(result)

    monkeypatch.setattr(dialogs, "run_in_thread", run)


@pytest.fixture
def headless_dialog(monkeypatch):
    """Диалог не должен захватывать фокус и всплывать поверх рабочего стола."""
    for method in ("grab_set", "lift", "focus_force"):
        monkeypatch.setattr(dialogs.UndoDialog, method, lambda self, *args, **kwargs: None)


def make_dialog(ctk_root, tmp_path, *, on_finished=None) -> dialogs.UndoDialog:
    return dialogs.UndoDialog(
        ctk_root,
        dispatcher=object(),
        log_path=str(tmp_path / "rename_log.csv"),
        current_folder=str(tmp_path),
        on_finished=on_finished,
    )


def test_lists_active_rows_and_keeps_undo_disabled(
    ctk_root, tmp_path, monkeypatch, sync_runner, headless_dialog
):
    entries = [make_entry("a.mp4", "A.mp4"), make_entry("b.mp4", "B.mp4")]
    monkeypatch.setattr(dialogs, "read_log", lambda path: list(entries))

    dialog = make_dialog(ctk_root, tmp_path)
    try:
        assert len(dialog._items) == 2
        assert dialog.status_label.cget("text") == "Активных строк: 2."
        assert dialog.undo_button.cget("state") == "disabled"
    finally:
        dialog.destroy()


def test_selection_enables_and_disables_undo_button(
    ctk_root, tmp_path, monkeypatch, sync_runner, headless_dialog
):
    monkeypatch.setattr(dialogs, "read_log", lambda path: [make_entry("a.mp4", "A.mp4")])
    dialog = make_dialog(ctk_root, tmp_path)
    try:
        dialog._items[0][1].set(True)
        dialog._sync_button()
        assert dialog.undo_button.cget("state") == "normal"

        dialog._items[0][1].set(False)
        dialog._sync_button()
        assert dialog.undo_button.cget("state") == "disabled"
    finally:
        dialog.destroy()


def test_undo_result_stays_visible_after_rows_reload(
    ctk_root, tmp_path, monkeypatch, sync_runner, headless_dialog
):
    holder = [make_entry("a.mp4", "A.mp4")]
    monkeypatch.setattr(dialogs, "read_log", lambda path: list(holder))
    monkeypatch.setattr(dialogs, "ask_confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        dialogs,
        "select_for_undo",
        lambda entries, ids, **kwargs: SimpleNamespace(targets=list(ids), errors=[]),
    )

    preview = SimpleNamespace(results=[make_result("a.mp4", "A.mp4")])
    report = SimpleNamespace(results=[make_result("a.mp4", "A.mp4")])

    def fake_run_undo(selection, *, log_path, dry_run):
        if not dry_run:
            holder.clear()
        return preview if dry_run else report

    monkeypatch.setattr(dialogs, "run_undo", fake_run_undo)
    finished: list[int] = []
    dialog = make_dialog(ctk_root, tmp_path, on_finished=lambda: finished.append(1))
    try:
        dialog._items[0][1].set(True)
        dialog._start_undo()

        assert finished == [1]
        assert dialog._items == []
        text = dialog.status_label.cget("text")
        assert text.startswith("Откачено: 1, ошибок: 0")
        assert "Активных строк: 0" in text
        assert dialog.status_label.cget("text_color") == COLORS["green_text"]
        assert dialog.undo_button.cget("state") == "disabled"
    finally:
        dialog.destroy()


def test_undo_errors_are_reported_in_red(
    ctk_root, tmp_path, monkeypatch, sync_runner, headless_dialog
):
    holder = [make_entry("a.mp4", "A.mp4")]
    monkeypatch.setattr(dialogs, "read_log", lambda path: list(holder))
    monkeypatch.setattr(dialogs, "ask_confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        dialogs,
        "select_for_undo",
        lambda entries, ids, **kwargs: SimpleNamespace(targets=list(ids), errors=[]),
    )
    preview = SimpleNamespace(results=[make_result("a.mp4", "A.mp4")])
    failed = SimpleNamespace(results=[make_result("a.mp4", "A.mp4", "error", "target is occupied")])
    monkeypatch.setattr(
        dialogs,
        "run_undo",
        lambda selection, *, log_path, dry_run: preview if dry_run else failed,
    )

    dialog = make_dialog(ctk_root, tmp_path)
    try:
        dialog._items[0][1].set(True)
        dialog._start_undo()

        text = dialog.status_label.cget("text")
        assert text.startswith("Откачено: 0, ошибок: 1")
        assert "target is occupied" in text
        assert dialog.status_label.cget("text_color") == COLORS["red_text"]
    finally:
        dialog.destroy()


def test_declined_confirmation_keeps_rows(
    ctk_root, tmp_path, monkeypatch, sync_runner, headless_dialog
):
    entries = [make_entry("a.mp4", "A.mp4")]
    monkeypatch.setattr(dialogs, "read_log", lambda path: list(entries))
    monkeypatch.setattr(dialogs, "ask_confirm", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        dialogs,
        "select_for_undo",
        lambda entries, ids, **kwargs: SimpleNamespace(targets=list(ids), errors=[]),
    )
    monkeypatch.setattr(
        dialogs,
        "run_undo",
        lambda selection, *, log_path, dry_run: SimpleNamespace(results=[]),
    )

    dialog = make_dialog(ctk_root, tmp_path)
    try:
        dialog._items[0][1].set(True)
        dialog._start_undo()

        assert len(dialog._items) == 1
        assert dialog.status_label.cget("text") == "Откат отменён."
    finally:
        dialog.destroy()


def test_nothing_selected_reports_status(
    ctk_root, tmp_path, monkeypatch, sync_runner, headless_dialog
):
    monkeypatch.setattr(dialogs, "read_log", lambda path: [make_entry("a.mp4", "A.mp4")])
    dialog = make_dialog(ctk_root, tmp_path)
    try:
        dialog._start_undo()
        assert dialog.status_label.cget("text") == "Ничего не выбрано."
    finally:
        dialog.destroy()


def test_load_failure_is_shown_in_red(
    ctk_root, tmp_path, monkeypatch, sync_runner, headless_dialog
):
    def broken_read(path):
        raise OSError("disk unreadable")

    monkeypatch.setattr(dialogs, "read_log", broken_read)

    dialog = make_dialog(ctk_root, tmp_path)
    try:
        text = dialog.status_label.cget("text")
        assert text.startswith("Не удалось прочитать журнал")
        assert "disk unreadable" in text
        assert dialog.status_label.cget("text_color") == COLORS["red_text"]
    finally:
        dialog.destroy()


def test_close_is_refused_while_busy(ctk_root, tmp_path, monkeypatch, sync_runner, headless_dialog):
    monkeypatch.setattr(dialogs, "read_log", lambda path: [make_entry("a.mp4", "A.mp4")])
    dialog = make_dialog(ctk_root, tmp_path)

    dialog._busy = True
    dialog._on_close()
    assert dialog.winfo_exists()
    assert "Дождитесь" in dialog.status_label.cget("text")

    dialog._busy = False
    dialog._on_close()
    assert not dialog.winfo_exists()
