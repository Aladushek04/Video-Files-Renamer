"""Тесты GUI-контроллера без импорта Tk/customtkinter."""

from __future__ import annotations

import os

import pytest

from video_renamer.core.title_service import (
    ApplyReport,
    ApplyResult,
    PreviewReport,
    PreviewRow,
    ProgressEvent,
)
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

    report, log_error = controller.run_apply([row("a.mp4", "A.mp4", approved=True)])

    assert log_error == ""
    assert report.results[0].status == "renamed"
