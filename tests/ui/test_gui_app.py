"""Тесты главного окна: ревью не перепривязывается к другой папке."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("customtkinter")

from video_renamer.core.rename_log import ACTION_RENAME, LogEntry, append_log
from video_renamer.core.title_service import (
    ApplyReport,
    ApplyResult,
    PreviewReport,
    PreviewRow,
)
from video_renamer.gui import app as gui_app
from video_renamer.gui.app import RenamerGUI
from video_renamer.gui.controller import RenamerController
from video_renamer.gui.dialogs import ApplyResultDialog, UndoDialog


def row(old_rel: str = "original.mp4", new_rel: str = "changed.mp4") -> PreviewRow:
    return PreviewRow(True, old_rel, new_rel, "text", 0.9, "s1", "", "h1", 10, ())


@pytest.fixture
def gui(tmp_path):
    tkinter = pytest.importorskip("tkinter")
    controller = RenamerController(log_path=str(tmp_path / "log.csv"))
    try:
        window = RenamerGUI(controller=controller)
    except (RuntimeError, tkinter.TclError) as exc:  # нет дисплея или сломанный Tk
        pytest.skip(f"Tk недоступен: {exc}")
    window.withdraw()
    try:
        yield window
    finally:
        window.destroy()


def _folders(tmp_path):
    folder_a = tmp_path / "A"
    folder_b = tmp_path / "B"
    folder_a.mkdir()
    folder_b.mkdir()
    return folder_a, folder_b


def _prepare_review(gui, folder_a: str) -> None:
    gui.controller.set_folder(folder_a)
    gui.controller.apply_preview(PreviewReport((row(),), (), False))
    gui._refresh_rows(reset_scroll=True)


def test_switching_folder_asks_and_discards_old_rows(gui, tmp_path, monkeypatch):
    folder_a, folder_b = _folders(tmp_path)
    _prepare_review(gui, str(folder_a))
    asked: list[tuple] = []
    monkeypatch.setattr(gui_app, "ask_confirm", lambda *args, **kwargs: asked.append(args) or True)

    assert gui._apply_folder(str(folder_b)) is True

    assert [args[1] for args in asked] == ["Сменить папку?"]
    assert gui.controller.model.rows == []
    assert gui.controller.review_root == ""
    assert gui.controller.folder == folder_b
    assert gui.folder_entry.get() == str(folder_b)
    assert gui.apply_button.cget("state") == "disabled"


def test_cancelled_folder_switch_keeps_review(gui, tmp_path, monkeypatch):
    folder_a, folder_b = _folders(tmp_path)
    _prepare_review(gui, str(folder_a))
    monkeypatch.setattr(gui_app, "ask_confirm", lambda *args, **kwargs: False)

    assert gui._apply_folder(str(folder_b)) is False

    assert gui.controller.folder == folder_a
    assert gui.controller.review_root == str(folder_a)
    assert [item.old_rel for item in gui.controller.model.approved_rows()] == ["original.mp4"]
    assert gui.folder_entry.get() == str(folder_a)
    assert gui.apply_button.cget("state") == "normal"


def test_same_folder_does_not_ask_and_keeps_rows(gui, tmp_path, monkeypatch):
    folder_a, _folder_b = _folders(tmp_path)
    _prepare_review(gui, str(folder_a))
    monkeypatch.setattr(
        gui_app, "ask_confirm", lambda *args, **kwargs: pytest.fail("лишний диалог")
    )

    assert gui._apply_folder(str(folder_a)) is True

    assert gui.controller.review_root == str(folder_a)
    assert len(gui.controller.model.rows) == 1


def test_missing_folder_keeps_review_and_restores_entry(gui, tmp_path, monkeypatch):
    folder_a, _folder_b = _folders(tmp_path)
    _prepare_review(gui, str(folder_a))
    shown: list[str] = []
    monkeypatch.setattr(
        gui_app, "show_notice", lambda parent, title, message, **kwargs: shown.append(message)
    )

    assert gui._apply_folder(str(tmp_path / "missing")) is False

    assert gui.controller.review_root == str(folder_a)
    assert len(gui.controller.model.rows) == 1
    assert shown and "не найдена" in shown[0]


def test_apply_confirmation_shows_review_root_and_count(gui, tmp_path, monkeypatch):
    folder = tmp_path / ("Очень длинная папка — 日本語 — " + "x" * 40)
    folder.mkdir()
    (folder / "original.mp4").write_bytes(b"o" * 100)
    gui.controller.set_folder(str(folder))
    gui.controller.apply_preview(PreviewReport((row(),), (), False))
    gui._refresh_rows(reset_scroll=True)
    calls: list[tuple] = []
    monkeypatch.setattr(gui_app, "ask_confirm", lambda *args, **kwargs: calls.append(args) or False)

    gui.action_apply()

    assert calls, "подтверждение не запрошено"
    message = calls[0][2]
    # Тот же неизменяемый корень, что передаётся в run_apply, а не поле ввода.
    assert gui.controller.review_root == str(folder)
    assert str(folder) in message
    assert "1 из 1" in message
    assert "original.mp4 → changed.mp4" in message
    # Отказ в диалоге не трогает файлы.
    assert (folder / "original.mp4").exists()
    assert not (folder / "changed.mp4").exists()


def test_apply_status_counts_only_failed_rows(gui, tmp_path, monkeypatch):
    folder = tmp_path / "A"
    folder.mkdir()
    gui.controller.set_folder(str(folder))
    gui.controller.apply_preview(
        PreviewReport(
            (
                row("bad.mp4", "bad-new.mp4"),
                PreviewRow(
                    False,
                    "pending.mp4",
                    "pending-new.mp4",
                    "heuristic",
                    0.0,
                    "",
                    "",
                    "h3",
                    10,
                    (),
                ),
            ),
            (),
            False,
        )
    )
    gui._refresh_rows(reset_scroll=True)
    monkeypatch.setattr(gui_app, "show_apply_result", lambda *args, **kwargs: None)
    report = ApplyReport(
        results=(ApplyResult("bad.mp4", "bad-new.mp4", "error", "target is occupied"),),
        had_errors=True,
    )

    gui._apply_done((report, ""))

    # Неодобренная строка осталась в модели, но ошибкой не считается.
    assert len(gui.controller.model.rows) == 2
    assert gui.status_label.cget("text") == "Применено: 0; строк с ошибками: 1"


def pending_row(old_rel: str = "original.mp4") -> PreviewRow:
    return PreviewRow(False, old_rel, "changed.mp4", "text", 0.9, "s1", "", "h1", 10, ())


def test_preview_without_approvals_asks_to_mark_rows(gui, tmp_path):
    folder = tmp_path / "A"
    folder.mkdir()
    gui.controller.set_folder(str(folder))

    gui._preview_done(PreviewReport((pending_row(),), (), False))

    assert gui.status_label.cget("text") == "Отметьте строки для переименования"


def test_apply_without_approvals_asks_to_mark_rows(gui):
    gui.action_apply()

    assert gui.status_label.cget("text") == "Отметьте строки для переименования"


def test_filter_without_matches_shows_message_and_resets(gui, tmp_path):
    folder = tmp_path / "A"
    folder.mkdir()
    gui.controller.set_folder(str(folder))
    gui.controller.apply_preview(PreviewReport((pending_row(),), (), False))
    gui._refresh_rows(reset_scroll=True)

    gui._on_filter_changed("Эвристика")

    assert gui.controller.model.filter == "heuristic"
    assert gui.rows_view.empty_title.cget("text") == "Нет строк по этому фильтру"
    assert gui.rows_view.empty_button.cget("text") == "Сбросить фильтр"
    assert gui.rows_view.empty_button.winfo_manager() == "pack"
    assert gui.rows_view._empty_frame.winfo_manager() == "place"

    gui.rows_view.empty_button.invoke()

    assert gui.controller.model.filter == "all"
    assert gui.filter_menu.get() == "Все"
    assert gui.rows_view.views(), "строки должны вернуться после сброса фильтра"
    assert gui.rows_view._empty_frame.winfo_manager() == ""


def test_apply_result_can_open_builtin_journal(gui, tmp_path, monkeypatch):
    folder = tmp_path / "A"
    folder.mkdir()
    gui.controller.set_folder(str(folder))
    monkeypatch.setattr(gui_app, "show_apply_result", lambda *args, **kwargs: True)
    opened: list[int] = []
    monkeypatch.setattr(gui, "open_undo", lambda: opened.append(1))

    gui._apply_done((ApplyReport(results=(), had_errors=False), ""))
    # Журнал открывается отложенно, вне текущего прохода диспетчера.
    gui.update_idletasks()

    assert opened == [1]


def test_journal_from_apply_result_loads_rows_with_real_dispatcher(gui, tmp_path):
    """Журнал, открытый из итога apply, дожидается доставки строк диспетчером."""
    append_log(
        str(tmp_path / "log.csv"),
        [
            LogEntry(
                timestamp="2026-11-18T10:00:00.000000Z",
                action=ACTION_RENAME,
                undoes="",
                root=str(tmp_path),
                old="old.mp4",
                new="New.mp4",
                oshash="h1",
                size=10,
                scene_id="s1",
                source="fingerprint",
                legacy=False,
            )
        ],
    )
    report = ApplyReport(
        results=(ApplyResult("old.mp4", "New.mp4", "renamed", ""),),
        had_errors=False,
    )

    gui.geometry("1180x700+3000+3000")
    gui.deiconify()
    gui.update()

    state: dict[str, object] = {"done": False, "deadline": time.monotonic() + 8.0}

    def find(cls):
        for child in gui.winfo_children():
            if isinstance(child, cls):
                return child
        return None

    def close_dialogs() -> None:
        for cls in (UndoDialog, ApplyResultDialog):
            dialog = find(cls)
            if dialog is not None:
                dialog.destroy()

    def poll() -> None:
        if state["done"]:
            return
        result_dialog = find(ApplyResultDialog)
        journal_button = getattr(result_dialog, "_journal_button", None)
        if journal_button is not None and not state.get("journal_requested"):
            state["journal_requested"] = True
            journal_button.invoke()
        journal = find(UndoDialog)
        items = getattr(journal, "_items", None)
        if items:
            items[0][1].set(True)
            journal._sync_button()
            state["rows_loaded"] = len(items)
            state["undo_state"] = journal.undo_button.cget("state")
            state["done"] = True
            journal.destroy()
            return
        if time.monotonic() > state["deadline"]:
            state["done"] = True
            state["timed_out"] = True
            close_dialogs()
            return
        gui.after(20, poll)

    gui.dispatcher.call_soon(gui._apply_done, (report, ""))
    gui.after(20, poll)

    while not state["done"] and time.monotonic() < state["deadline"] + 2:
        gui.update()
        time.sleep(0.01)

    assert not state.get("timed_out"), "журнал так и не загрузил строки"
    assert state.get("journal_requested"), "кнопка журнала не была нажата"
    assert state.get("rows_loaded") == 1
    assert state.get("undo_state") == "normal"
