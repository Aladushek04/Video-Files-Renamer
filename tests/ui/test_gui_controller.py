"""Тесты GUI-контроллера без импорта Tk/customtkinter."""

from __future__ import annotations

import os

import pytest

from video_renamer.core import review_actions
from video_renamer.core.oshash import oshash_from_path
from video_renamer.core.title_service import (
    ApplyReport,
    ApplyResult,
    PreviewReport,
    PreviewRow,
    ProgressEvent,
)
from video_renamer.gui import controller as controller_module
from video_renamer.gui.controller import RenamerController


def row(
    old_rel: str,
    new_rel: str,
    *,
    approved: bool = False,
    source: str = "text",
) -> PreviewRow:
    return PreviewRow(approved, old_rel, new_rel, source, 0.8, "s1", "", "h1", 100, ())


def test_preview_factory_publishes_report_in_model(tmp_path):
    def preview(folder: str, progress):
        assert folder == str(tmp_path)
        progress(ProgressEvent("scan", 1, 1, "1 file"))
        return PreviewReport((row("a.mp4", "Studio - 01.01.2026 - Title.mp4"),), (), False)

    controller = RenamerController(
        preview_factory=preview,
        log_path=str(tmp_path / "log.csv"),
    )
    controller.set_folder(str(tmp_path))
    events: list[ProgressEvent] = []

    report = controller.run_preview(events.append)
    controller.apply_preview(report)

    assert [event.phase for event in events] == ["scan"]
    assert controller.model.stats()["total"] == 1
    assert controller.model.rows[0].new_rel == "Studio - 01.01.2026 - Title.mp4"


def test_rename_basename_keeps_parent_and_approves(tmp_path):
    controller = RenamerController(log_path=str(tmp_path / "log.csv"))
    controller.set_folder(str(tmp_path))
    controller.apply_preview(
        PreviewReport(
            (row(os.path.join("sub", "a.mp4"), os.path.join("sub", "Old.mp4")),), (), False
        )
    )

    updated = controller.rename_basename(0, "Custom.mp4")

    assert updated.new_rel == os.path.join("sub", "Custom.mp4")
    assert updated.approved is True
    assert updated.source == "text"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "пустым"),
        ("sub/Custom.mp4", "Подпапки"),
        ("Custom.avi", "Расширение"),
        ("CON.mp4", "Недопустимое"),
    ],
)
def test_rename_basename_rejects_invalid_values(tmp_path, value, message):
    controller = RenamerController(log_path=str(tmp_path / "log.csv"))
    controller.set_folder(str(tmp_path))
    controller.apply_preview(PreviewReport((row("a.mp4", "Old.mp4"),), (), False))

    with pytest.raises(ValueError, match=message):
        controller.rename_basename(0, value)


def test_remove_renamed_keeps_unapproved_and_failed_rows(tmp_path):
    controller = RenamerController(log_path=str(tmp_path / "log.csv"))
    controller.set_folder(str(tmp_path))
    controller.apply_preview(
        PreviewReport(
            (
                row("a.mp4", "A.mp4", approved=True),
                row("b.mp4", "B.mp4", approved=True),
                row("c.mp4", "C.mp4", approved=False),
            ),
            (),
            False,
        )
    )
    report = ApplyReport(
        (
            ApplyResult("a.mp4", "A.mp4", "renamed", ""),
            ApplyResult("b.mp4", "B.mp4", "error", "target is occupied"),
        ),
        had_errors=True,
    )

    controller.remove_renamed(report)

    assert [item.old_rel for item in controller.model.rows] == ["b.mp4", "c.mp4"]


def test_apply_factory_returns_report_without_log(tmp_path):
    controller = RenamerController(
        apply_factory=lambda rows: ApplyReport(
            (ApplyResult(rows[0].old_rel, rows[0].new_rel, "renamed", ""),),
            had_errors=False,
        ),
        log_path=str(tmp_path / "log.csv"),
    )
    controller.set_folder(str(tmp_path))
    controller.apply_preview(PreviewReport((row("a.mp4", "A.mp4", approved=True),), (), False))

    report, log_error = controller.run_apply([row("a.mp4", "A.mp4", approved=True)])

    assert log_error == ""
    assert report.results[0].status == "renamed"


def test_folder_switch_does_not_rebind_existing_review(tmp_path):
    folder_a = tmp_path / "A"
    folder_b = tmp_path / "B"
    folder_a.mkdir()
    folder_b.mkdir()
    controller = RenamerController(log_path=str(tmp_path / "log.csv"))
    controller.set_folder(str(folder_a))
    controller.apply_preview(
        PreviewReport((row("original.mp4", "changed.mp4", approved=True),), (), False)
    )

    controller.set_folder(str(folder_b))

    assert controller.review_root == str(folder_a)
    assert controller.review_stale is True
    assert [item.old_rel for item in controller.model.approved_rows()] == ["original.mp4"]
    assert controller.folder == folder_b


def test_switching_to_the_same_folder_keeps_review(tmp_path):
    folder = tmp_path / "A"
    folder.mkdir()
    controller = RenamerController(log_path=str(tmp_path / "log.csv"))
    controller.set_folder(str(folder))
    controller.apply_preview(PreviewReport((row("a.mp4", "A.mp4", approved=True),), (), False))

    controller.set_folder(os.path.join(str(folder), ".", "..", folder.name))

    assert controller.review_stale is False
    assert len(controller.model.rows) == 1
    assert controller.folder_change_needs_confirmation(folder) is False


def test_discard_review_clears_rows_and_root(tmp_path):
    folder = tmp_path / "A"
    folder.mkdir()
    controller = RenamerController(log_path=str(tmp_path / "log.csv"))
    controller.set_folder(str(folder))
    controller.apply_preview(PreviewReport((row("a.mp4", "A.mp4", approved=True),), (), False))
    assert controller.folder_change_needs_confirmation(tmp_path) is True

    controller.discard_review()

    assert controller.model.rows == []
    assert controller.review_root == ""
    assert controller.review_stale is False


def test_stale_apply_is_refused_and_touches_no_files(tmp_path):
    folder_a = tmp_path / "A"
    folder_b = tmp_path / "B"
    folder_a.mkdir()
    folder_b.mkdir()
    content = b"0123456789"
    video_a = folder_a / "original.mp4"
    video_b = folder_b / "original.mp4"
    video_a.write_bytes(content)
    video_b.write_bytes(content)
    digest = oshash_from_path(str(video_a))
    approved = PreviewRow(
        True, "original.mp4", "changed.mp4", "text", 0.9, "s1", "", digest, len(content)
    )
    controller = RenamerController(
        preview_factory=lambda folder, progress: PreviewReport((approved,), (), False),
        log_path=str(tmp_path / "log.csv"),
    )
    controller.set_folder(str(folder_a))
    controller.apply_preview(controller.run_preview(lambda event: None))
    controller.set_folder(str(folder_b))

    with pytest.raises(ValueError, match="Папка изменилась"):
        controller.run_apply(controller.model.approved_rows())

    assert (folder_a / "original.mp4").read_bytes() == content
    assert (folder_b / "original.mp4").read_bytes() == content
    assert not (folder_a / "changed.mp4").exists()
    assert not (folder_b / "changed.mp4").exists()


def test_apply_uses_review_root_after_switching_back(tmp_path):
    folder_a = tmp_path / "A"
    folder_b = tmp_path / "B"
    folder_a.mkdir()
    folder_b.mkdir()
    content = b"0123456789"
    video_a = folder_a / "original.mp4"
    video_b = folder_b / "original.mp4"
    video_a.write_bytes(content)
    video_b.write_bytes(content)
    digest = oshash_from_path(str(video_a))
    approved = PreviewRow(
        True, "original.mp4", "changed.mp4", "text", 0.9, "s1", "", digest, len(content)
    )
    controller = RenamerController(
        preview_factory=lambda folder, progress: PreviewReport((approved,), (), False),
        log_path=str(tmp_path / "log.csv"),
    )
    controller.set_folder(str(folder_a))
    controller.apply_preview(controller.run_preview(lambda event: None))
    controller.set_folder(str(folder_b))
    controller.set_folder(str(folder_a))

    report, log_error = controller.run_apply(controller.model.approved_rows())

    assert log_error == ""
    assert [item.status for item in report.results] == ["renamed"]
    assert (folder_a / "changed.mp4").read_bytes() == content
    assert (folder_b / "original.mp4").read_bytes() == content
    assert not (folder_b / "changed.mp4").exists()


def test_apply_requires_a_preview_root(tmp_path):
    controller = RenamerController(log_path=str(tmp_path / "log.csv"))
    controller.set_folder(str(tmp_path))

    with pytest.raises(ValueError, match="Preview"):
        controller.run_apply([row("a.mp4", "A.mp4", approved=True)])


def test_reload_environment_picks_up_changed_dotenv(tmp_path, monkeypatch):
    from video_renamer.core.env_file import load_env_file, reset_tracking
    from video_renamer.gui import controller as gui_controller

    reset_tracking()
    env_file = tmp_path / ".env"
    env_file.write_text("STASHDB_API_KEY=first-test-key\n", encoding="utf-8")
    monkeypatch.delenv("STASHDB_API_KEY", raising=False)
    monkeypatch.setattr(gui_controller, "load_project_env", lambda: load_env_file(env_file))
    monkeypatch.setattr(gui_controller, "studios_catalog", lambda: ({}, ""))
    monkeypatch.setattr(gui_controller, "load_performer_index", lambda: None)

    controller = RenamerController(log_path=str(tmp_path / "log.csv"))
    assert controller.stashdb_key == "first-test-key"
    assert controller.source_configured("StashDB") is True

    env_file.write_text("STASHDB_API_KEY=second-test-key\n", encoding="utf-8")
    controller.reload_environment()

    assert controller.stashdb_key == "second-test-key"
    assert controller.keys["StashDB"] is True

    env_file.unlink()
    controller.reload_environment()

    assert controller.stashdb_key == ""
    assert controller.source_configured("StashDB") is False
    reset_tracking()


def test_secondary_key_is_reported_without_stashdb(tmp_path, monkeypatch):
    from video_renamer.gui import controller as gui_controller

    monkeypatch.setattr(gui_controller, "load_project_env", dict)
    monkeypatch.setattr(gui_controller, "studios_catalog", lambda: ({}, ""))
    monkeypatch.setattr(gui_controller, "load_performer_index", lambda: None)
    monkeypatch.delenv("STASHDB_API_KEY", raising=False)
    monkeypatch.setenv("FANSDB_API_KEY", "fans-test-key")
    monkeypatch.setenv("THE_PORNDB_API_KEY", "")

    controller = RenamerController(log_path=str(tmp_path / "log.csv"))

    assert controller.source_configured("FansDB") is True
    assert controller.source_configured("ThePornDB") is False
    assert controller.source_configured("StashDB") is False
    assert controller.offline is True


def test_catalog_text_distinguishes_missing_broken_and_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(controller_module, "load_project_env", dict)
    monkeypatch.setattr(controller_module, "load_performer_index", lambda: None)
    monkeypatch.delenv("STASHDB_API_KEY", raising=False)

    missing = tmp_path / "missing.json"
    monkeypatch.setattr(review_actions, "resolve_studios_path", lambda: missing)
    monkeypatch.setattr(controller_module, "resolve_studios_path", lambda: missing)
    controller = RenamerController(log_path=str(tmp_path / "log.csv"))
    assert "не найден" in controller.catalog_text()

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(review_actions, "resolve_studios_path", lambda: broken)
    monkeypatch.setattr(controller_module, "resolve_studios_path", lambda: broken)
    controller.reload_environment()
    text = controller.catalog_text()
    assert "повреждён" in text
    assert "не найден" not in text

    good = tmp_path / "studios.json"
    good.write_text('{"A Studio": "A"}', encoding="utf-8")
    monkeypatch.setattr(review_actions, "resolve_studios_path", lambda: good)
    monkeypatch.setattr(controller_module, "resolve_studios_path", lambda: good)
    controller.reload_environment()
    assert controller.catalog_text().startswith("Справочник: ")
    assert controller.catalog_text().endswith("MB")
