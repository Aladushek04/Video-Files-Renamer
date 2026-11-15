"""Тесты главного окна: ревью не перепривязывается к другой папке."""

from __future__ import annotations

import pytest

pytest.importorskip("customtkinter")

from video_renamer.core.title_service import (
    ApplyReport,
    ApplyResult,
    PreviewReport,
    PreviewRow,
)
from video_renamer.gui import app as gui_app
from video_renamer.gui.app import RenamerGUI
from video_renamer.gui.controller import RenamerController


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
