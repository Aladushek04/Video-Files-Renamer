from datetime import datetime, timezone

import pytest

from video_renamer.core import rename_log
from video_renamer.core.rename_log import (
    LogEntry,
    LogMigrationError,
    active_renames,
    append_log,
    entry_id,
    format_log_timestamp,
    read_log,
)

LEGACY_HEADER = "timestamp;old;new;oshash;scene_id;source"


def rename_entry(root="R", old="a.mp4", new="b.mp4", timestamp=None):
    return LogEntry(
        timestamp=timestamp or format_log_timestamp(),
        action="rename",
        undoes="",
        root=root,
        old=old,
        new=new,
        oshash="h",
        size=10,
        scene_id="s1",
        source="fingerprint",
        legacy=False,
    )


def undo_entry_for(rename, timestamp):
    return LogEntry(
        timestamp=timestamp,
        action="undo",
        undoes=rename.id,
        root=rename.root,
        old=rename.new,
        new=rename.old,
        oshash=rename.oshash,
        size=rename.size,
        scene_id=rename.scene_id,
        source=rename.source,
        legacy=False,
    )


def boom_oserror(src, dst):
    raise OSError("file is locked")


def test_ids_differ_for_same_paths_in_same_second():
    t1 = "2026-11-06T10:00:00.000001Z"
    t2 = "2026-11-06T10:00:00.000002Z"
    assert entry_id(t1, "rename", "C:/r", "a.mp4", "b.mp4") != entry_id(
        t2, "rename", "C:/r", "a.mp4", "b.mp4")


def test_format_log_timestamp_has_microseconds_and_z():
    value = format_log_timestamp(datetime(2026, 11, 6, 10, 0, 0, 5, tzinfo=timezone.utc))
    assert value == "2026-11-06T10:00:00.000005Z"


def test_entry_id_is_10_lowercase_hex():
    value = entry_id("t", "rename", "r", "a", "b")
    assert len(value) == 10 and value == value.lower()


def test_read_log_missing_file_is_empty(tmp_path):
    assert read_log(str(tmp_path / "none.csv")) == []


def test_read_log_parses_new_format(tmp_path):
    path = tmp_path / "rename_log.csv"
    append_log(str(path), [rename_entry(root=str(tmp_path), old="a.mp4", new="b.mp4")])
    entries = read_log(str(path))
    assert [(e.old, e.new, e.legacy) for e in entries] == [("a.mp4", "b.mp4", False)]


def test_read_log_parses_legacy_rows_with_empty_root(tmp_path):
    path = tmp_path / "rename_log.csv"
    path.write_text(LEGACY_HEADER + "\n2026-11-04T12:00:00Z;a.mp4;b.mp4;h;s;fingerprint\n",
                    encoding="utf-8-sig")
    entries = read_log(str(path))
    assert entries[0].legacy is True
    assert entries[0].root == ""
    assert entries[0].size is None


def test_active_renames_hides_undone_rows():
    rename = rename_entry(old="a.mp4", new="b.mp4")
    undo = undo_entry_for(rename, timestamp="2026-11-06T11:00:00.000000Z")
    assert active_renames([rename, undo]) == []


def test_undo_rejects_already_undone_row():
    rename = rename_entry(old="a.mp4", new="b.mp4")
    undo = undo_entry_for(rename, timestamp="2026-11-06T11:00:00.000000Z")
    assert rename.id not in {e.id for e in active_renames([rename, undo])}


def test_append_migrates_legacy_file_once(tmp_path):
    path = tmp_path / "rename_log.csv"
    path.write_text(LEGACY_HEADER + "\nT;a;b;h;s;f\n", encoding="utf-8-sig")
    append_log(str(path), [rename_entry(root="R", old="c.mp4", new="d.mp4")])
    assert (tmp_path / "rename_log.legacy.csv").exists()
    assert path.read_text(encoding="utf-8-sig").splitlines()[0].startswith(
        "timestamp;action;undoes")
    assert len(read_log(str(path))) == 2


def test_append_aborts_when_legacy_rename_fails(tmp_path, monkeypatch):
    path = tmp_path / "rename_log.csv"
    path.write_text(LEGACY_HEADER + "\nT;a;b;h;s;f\n", encoding="utf-8-sig")
    monkeypatch.setattr(rename_log.os, "replace", boom_oserror)
    with pytest.raises(LogMigrationError):
        append_log(str(path), [rename_entry(root="R", old="c.mp4", new="d.mp4")])
    assert path.read_text(encoding="utf-8-sig").splitlines()[0] == LEGACY_HEADER


def test_append_refuses_unknown_header_and_keeps_file(tmp_path):
    path = tmp_path / "rename_log.csv"
    path.write_text("foo;bar\n1;2\n", encoding="utf-8-sig")
    with pytest.raises(LogMigrationError):
        append_log(str(path), [rename_entry(root="R", old="a.mp4", new="b.mp4")])
    assert path.read_text(encoding="utf-8-sig") == "foo;bar\n1;2\n"


def test_append_writes_bom_only_to_new_file(tmp_path):
    path = tmp_path / "rename_log.csv"
    append_log(str(path), [rename_entry(root="R", old="a.mp4", new="b.mp4")])
    append_log(str(path), [rename_entry(root="R", old="c.mp4", new="d.mp4")])
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\xef\xbb\xbf") == 1
