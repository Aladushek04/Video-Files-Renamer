"""Тесты общих операций ревью, которыми пользуются TUI и GUI."""

from __future__ import annotations

import os

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

    monkeypatch.setattr(review_actions, "append_missing_log", broken_log)

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
    monkeypatch.setattr(review_actions, "resolve_studios_path", lambda: missing)
    monkeypatch.setattr(review_actions, "PERFORMERS_INDEX_PATH", missing)

    assert load_studios_catalog() == {}
    assert load_performer_index() is None


def test_loaders_tolerate_broken_files(tmp_path, monkeypatch):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(review_actions, "resolve_studios_path", lambda: broken)
    monkeypatch.setattr(review_actions, "PERFORMERS_INDEX_PATH", broken)

    assert load_studios_catalog() == {}
    assert load_performer_index() is None


def test_studios_catalog_distinguishes_states(tmp_path, monkeypatch):
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(review_actions, "resolve_studios_path", lambda: missing)
    assert review_actions.studios_catalog() == ({}, "missing")

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(review_actions, "resolve_studios_path", lambda: broken)
    studios, problem = review_actions.studios_catalog()
    assert studios == {}
    assert problem.startswith("unreadable: ")

    # JSON-массив с тем же расширением — тоже повреждённый справочник.
    array = tmp_path / "array.json"
    array.write_text("[1, 2]", encoding="utf-8")
    monkeypatch.setattr(review_actions, "resolve_studios_path", lambda: array)
    assert review_actions.studios_catalog()[1].startswith("unreadable: ")

    good = tmp_path / "studios.json"
    good.write_text('{"A Studio": "A"}', encoding="utf-8")
    monkeypatch.setattr(review_actions, "resolve_studios_path", lambda: good)
    studios, problem = review_actions.studios_catalog()
    assert problem == ""
    assert list(studios) == ["astudio"]


def test_studios_catalog_cache_follows_the_file(tmp_path, monkeypatch):
    path = tmp_path / "studios.json"
    path.write_text('{"A Studio": "A"}', encoding="utf-8")
    monkeypatch.setattr(review_actions, "resolve_studios_path", lambda: path)
    assert len(review_actions.studios_catalog()[0]) == 1

    path.write_text('{"A Studio": "A", "B Studio": "B"}', encoding="utf-8")
    # Сдвигаем mtime, чтобы кеш не зависел от гранулярности ФС.
    os.utime(path, ns=(1_000_000_000, 2_000_000_000))

    assert len(review_actions.studios_catalog()[0]) == 2


def test_apply_rows_moves_nothing_when_journal_is_unavailable(tmp_path):
    preview_row = make_video(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a folder")

    report, log_error = apply_rows(
        str(tmp_path),
        [preview_row],
        log_path=str(blocker / "rename_log.csv"),
    )

    assert log_error == ""
    assert report.had_errors is True
    assert report.results[0].status == "error"
    assert "journal is not writable" in report.results[0].message
    assert (tmp_path / "old.mp4").exists()
    assert not (tmp_path / "new.mp4").exists()


def test_journal_failure_keeps_unapproved_rows_skipped(tmp_path):
    approved = make_video(tmp_path)
    pending = row("pending.mp4", "Pending.mp4", approved=False)
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a folder")

    report, log_error = apply_rows(
        str(tmp_path),
        [approved, pending],
        log_path=str(blocker / "rename_log.csv"),
    )

    assert log_error == ""
    assert [result.status for result in report.results] == ["error", "skipped"]
    assert (tmp_path / "old.mp4").exists()
