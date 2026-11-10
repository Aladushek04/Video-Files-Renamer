from datetime import datetime, timezone

from video_renamer.cli import append_rename_log
from video_renamer.core import undo as undo_module
from video_renamer.core.oshash import oshash_from_path
from video_renamer.core.rename_log import (
    LogEntry,
    LogMigrationError,
    active_renames,
    format_log_timestamp,
    read_log,
)
from video_renamer.core.title_service import PreviewRow, TitleService
from video_renamer.core.undo import run_undo, select_for_undo


def write(path, data: bytes):
    path.write_bytes(data)


def apply_pairs(tmp_path, log, pairs, moment=None):
    """Реальный apply пар (old, new) и запись журнала; возвращает строки PreviewRow."""
    rows = [
        PreviewRow(
            True,
            old,
            new,
            "fingerprint",
            1.0,
            f"s-{old}",
            "",
            oshash_from_path(str(tmp_path / old)),
            (tmp_path / old).stat().st_size,
        )
        for old, new in pairs
    ]
    report = TitleService(str(tmp_path), {}, None).apply_rows(rows, dry_run=False)
    append_rename_log(
        str(log), rows, report.results, format_log_timestamp(moment), root=str(tmp_path)
    )
    return rows


def entries_of(log):
    return read_log(str(log))


def test_select_rejects_unknown_and_inactive_ids(tmp_path):
    log = tmp_path / "rename_log.csv"
    write(tmp_path / "a.mp4", b"a" * 100)
    apply_pairs(tmp_path, log, [("a.mp4", "b.mp4")])
    entry = active_renames(entries_of(log))[0]

    unknown = select_for_undo(entries_of(log), ["nope"])
    assert unknown.targets == () and len(unknown.errors) == 1

    run_undo(select_for_undo(entries_of(log), [entry.id]), log_path=str(log), dry_run=False)
    inactive = select_for_undo(entries_of(log), [entry.id])
    assert inactive.targets == () and len(inactive.errors) == 1


def test_select_legacy_requires_root_and_confirm(tmp_path):
    legacy = LogEntry(
        "2026-11-04T12:00:00Z",
        "rename",
        "",
        "",
        "a.mp4",
        "b.mp4",
        "h",
        None,
        "s",
        "fingerprint",
        True,
    )
    assert select_for_undo([legacy], [legacy.id]).errors
    assert select_for_undo([legacy], [legacy.id], root_override=str(tmp_path)).errors
    ok = select_for_undo([legacy], [legacy.id], root_override=str(tmp_path), confirm_legacy=True)
    assert ok.errors == ()
    assert ok.targets[0].root == str(tmp_path)


def test_select_orders_newest_first(tmp_path):
    log = tmp_path / "rename_log.csv"
    write(tmp_path / "a.mp4", b"a" * 100)
    apply_pairs(
        tmp_path,
        log,
        [("a.mp4", "b.mp4")],
        moment=datetime(2026, 11, 6, 10, 0, 0, tzinfo=timezone.utc),
    )
    write(tmp_path / "c.mp4", b"c" * 100)
    apply_pairs(
        tmp_path,
        log,
        [("c.mp4", "d.mp4")],
        moment=datetime(2026, 11, 6, 11, 0, 0, tzinfo=timezone.utc),
    )
    ids = [e.id for e in active_renames(entries_of(log))]
    selection = select_for_undo(entries_of(log), ids)
    assert [t.entry.new for t in selection.targets] == ["d.mp4", "b.mp4"]


def test_select_deduplicates_ids(tmp_path):
    log = tmp_path / "rename_log.csv"
    write(tmp_path / "a.mp4", b"a" * 100)
    apply_pairs(tmp_path, log, [("a.mp4", "b.mp4")])
    entry = active_renames(entries_of(log))[0]
    assert len(select_for_undo(entries_of(log), [entry.id, entry.id]).targets) == 1


def test_undo_restores_file_and_appends_undo_row(tmp_path):
    log = tmp_path / "rename_log.csv"
    write(tmp_path / "a.mp4", b"a" * 100)
    apply_pairs(tmp_path, log, [("a.mp4", "b.mp4")])
    entry = active_renames(entries_of(log))[0]

    report = run_undo(
        select_for_undo(entries_of(log), [entry.id]), log_path=str(log), dry_run=False
    )

    assert report.had_errors is False
    assert (tmp_path / "a.mp4").exists()
    assert not (tmp_path / "b.mp4").exists()
    undo_rows = [e for e in entries_of(log) if e.action == "undo"]
    assert [e.undoes for e in undo_rows] == [entry.id]
    assert active_renames(entries_of(log)) == []


def test_undo_refuses_when_oshash_changed(tmp_path):
    log = tmp_path / "rename_log.csv"
    write(tmp_path / "a.mp4", b"a" * 100)
    apply_pairs(tmp_path, log, [("a.mp4", "b.mp4")])
    entry = active_renames(entries_of(log))[0]
    write(tmp_path / "b.mp4", b"someone else's bytes")

    report = run_undo(
        select_for_undo(entries_of(log), [entry.id]), log_path=str(log), dry_run=False
    )

    assert report.had_errors is True
    assert report.results[0].status == "error"
    assert (tmp_path / "b.mp4").read_bytes() == b"someone else's bytes"
    assert not (tmp_path / "a.mp4").exists()
    assert active_renames(entries_of(log)) != []


def test_undo_chain_restores_both(tmp_path):
    log = tmp_path / "rename_log.csv"
    write(tmp_path / "a.mp4", b"a" * 100)
    write(tmp_path / "b.mp4", b"b" * 100)
    apply_pairs(tmp_path, log, [("a.mp4", "b.mp4"), ("b.mp4", "c.mp4")])
    ids = [e.id for e in active_renames(entries_of(log))]

    report = run_undo(select_for_undo(entries_of(log), ids), log_path=str(log), dry_run=False)

    assert report.had_errors is False
    assert (tmp_path / "a.mp4").read_bytes() == b"a" * 100
    assert (tmp_path / "b.mp4").read_bytes() == b"b" * 100
    assert not (tmp_path / "c.mp4").exists()


def test_undo_rejects_already_undone_row(tmp_path):
    log = tmp_path / "rename_log.csv"
    write(tmp_path / "a.mp4", b"a" * 100)
    apply_pairs(tmp_path, log, [("a.mp4", "b.mp4")])
    entry = active_renames(entries_of(log))[0]
    run_undo(select_for_undo(entries_of(log), [entry.id]), log_path=str(log), dry_run=False)

    again = select_for_undo(entries_of(log), [entry.id])
    assert again.targets == () and again.errors
    assert len([e for e in entries_of(log) if e.action == "undo"]) == 1


def test_undo_dry_run_changes_nothing(tmp_path):
    log = tmp_path / "rename_log.csv"
    write(tmp_path / "a.mp4", b"a" * 100)
    apply_pairs(tmp_path, log, [("a.mp4", "b.mp4")])
    entry = active_renames(entries_of(log))[0]
    before = log.read_bytes()

    report = run_undo(select_for_undo(entries_of(log), [entry.id]), log_path=str(log), dry_run=True)

    assert report.results[0].status == "dry-run"
    assert (tmp_path / "b.mp4").exists()
    assert log.read_bytes() == before


def test_undo_reports_unwritten_journal(tmp_path, monkeypatch):
    log = tmp_path / "rename_log.csv"
    write(tmp_path / "a.mp4", b"a" * 100)
    apply_pairs(tmp_path, log, [("a.mp4", "b.mp4")])
    entry = active_renames(entries_of(log))[0]

    def fail(path, entries):
        raise LogMigrationError("journal is locked")

    monkeypatch.setattr(undo_module, "append_log", fail)
    report = run_undo(
        select_for_undo(entries_of(log), [entry.id]), log_path=str(log), dry_run=False
    )

    assert report.had_errors is True
    assert report.results[0].status == "journal-missing"
    assert (tmp_path / "a.mp4").exists()
    assert active_renames(entries_of(log)) != []
