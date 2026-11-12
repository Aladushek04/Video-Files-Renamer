"""Тесты общих операций ревью, которыми пользуются TUI и GUI."""

from __future__ import annotations

from video_renamer.core import review_actions
from video_renamer.core.oshash import oshash_from_path
from video_renamer.core.review_actions import (
    apply_rows,
    load_performer_index,
    load_studios_catalog,
    remove_renamed,
)
from video_renamer.core.title_service import ApplyReport, ApplyResult, PreviewRow


def row(old_rel: str, new_rel: str, *, approved: bool = True) -> PreviewRow:
    return PreviewRow(approved, old_rel, new_rel, "fingerprint", 1.0, "s1", "", "h1", 10, ())


def make_video(tmp_path) -> PreviewRow:
    video = tmp_path / "old.mp4"
    video.write_bytes(b"0123456789")
    return PreviewRow(
        True,
        "old.mp4",
        "new.mp4",
        "fingerprint",
        1.0,
        "s1",
        "",
        oshash_from_path(str(video)),
        video.stat().st_size,
        (),
    )


def test_apply_rows_renames_files_and_writes_log(tmp_path):
    preview_row = make_video(tmp_path)
    log = tmp_path / "rename_log.csv"

    report, log_error = apply_rows(str(tmp_path), [preview_row], log_path=str(log))

    assert log_error == ""
    assert [result.status for result in report.results] == ["renamed"]
    assert (tmp_path / "new.mp4").exists()
    assert "old.mp4" in log.read_text(encoding="utf-8-sig")


def test_apply_rows_survives_log_failure(tmp_path, monkeypatch):
    preview_row = make_video(tmp_path)

    def broken_log(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(review_actions, "append_rename_log", broken_log)

    report, log_error = apply_rows(
        str(tmp_path),
        [preview_row],
        log_path=str(tmp_path / "rename_log.csv"),
    )

    assert report.results[0].status == "renamed"
    assert (tmp_path / "new.mp4").exists()
    assert log_error == "disk full"


def test_remove_renamed_keeps_pending_and_failed_rows():
    rows = [
        row("a.mp4", "A.mp4"),
        row("b.mp4", "B.mp4"),
        row("c.mp4", "C.mp4", approved=False),
    ]
    report = ApplyReport(
        (
            ApplyResult("a.mp4", "A.mp4", "renamed", ""),
            ApplyResult("b.mp4", "B.mp4", "error", "target is occupied"),
        ),
        had_errors=True,
    )

    kept = remove_renamed(rows, report)

    assert [item.old_rel for item in kept] == ["b.mp4", "c.mp4"]


def test_loaders_tolerate_missing_files(tmp_path, monkeypatch):
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(review_actions, "STUDIOS_PATH", missing)
    monkeypatch.setattr(review_actions, "PERFORMERS_INDEX_PATH", missing)

    assert load_studios_catalog() == {}
    assert load_performer_index() is None


def test_loaders_tolerate_broken_files(tmp_path, monkeypatch):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(review_actions, "STUDIOS_PATH", broken)
    monkeypatch.setattr(review_actions, "PERFORMERS_INDEX_PATH", broken)

    assert load_studios_catalog() == {}
    assert load_performer_index() is None
