import csv
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
    log_write_problem,
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
        t2, "rename", "C:/r", "a.mp4", "b.mp4"
    )


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
    path.write_text(
        LEGACY_HEADER + "\n2026-11-04T12:00:00Z;a.mp4;b.mp4;h;s;fingerprint\n", encoding="utf-8-sig"
    )
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
    assert (
        path.read_text(encoding="utf-8-sig").splitlines()[0].startswith("timestamp;action;undoes")
    )
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


def test_log_write_problem_accepts_usable_path(tmp_path):
    path = tmp_path / "rename_log.csv"

    assert log_write_problem(str(path)) == ""
    assert list(tmp_path.iterdir()) == []


def test_log_write_problem_rejects_unknown_header_without_touching_file(tmp_path):
    path = tmp_path / "rename_log.csv"
    path.write_text("header\n", encoding="utf-8")

    problem = log_write_problem(str(path))

    assert "format" in problem
    assert path.read_text(encoding="utf-8") == "header\n"


def test_log_write_problem_reports_unpreparable_path(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a folder")

    assert log_write_problem(str(blocker / "rename_log.csv")) != ""


DANGEROUS_VALUES = [
    "=2+2",
    "+1",
    "-1",
    "@cmd",
    " =2+2",
    "\t=2+2",
    "\r=2+2",
    "\n@cmd",
    "'=2+2",
    "''=2+2",
    "=SUM(A1)",
]


@pytest.mark.parametrize("value", DANGEROUS_VALUES)
def test_guard_roundtrips_and_leaves_nothing_dangerous(value):
    guarded = rename_log._guard_formula(value)

    assert rename_log._unguard_formula(guarded) == value
    assert rename_log._needs_formula_guard(guarded) is False


@pytest.mark.parametrize("value", ["plain.mp4", " spaced.mp4", "a=b.mp4", "'quote", ""])
def test_guard_leaves_safe_values_untouched(value):
    assert rename_log._guard_formula(value) == value


def test_append_log_guards_every_untrusted_field(tmp_path):
    entry = LogEntry(
        timestamp="2026-11-15T10:00:00.000000Z",
        action="rename",
        undoes="",
        root="=root",
        old="+old.mp4",
        new="@new.mp4",
        oshash="-h",
        size=1,
        scene_id="=3+3",
        source="=2+2",
        legacy=False,
    )
    path = tmp_path / "rename_log.csv"

    append_log(str(path), [entry])

    with open(path, encoding="utf-8-sig", newline="") as handle:
        records = list(csv.reader(handle, delimiter=";"))
    cells = dict(zip(records[0], records[1], strict=True))
    for field in ("root", "old", "new", "oshash", "scene_id", "source"):
        assert cells[field].startswith("'"), field
    read_back = read_log(str(path))
    assert read_back == [entry]
    assert read_back[0].id == entry.id


def test_old_journal_with_raw_formula_values_reads_as_written(tmp_path):
    """Журнал 0.2.0: source/scene_id писались без защиты."""
    path = tmp_path / "rename_log.csv"
    path.write_text(
        "timestamp;action;undoes;root;old;new;oshash;size;scene_id;source\n"
        "2026-11-01T10:00:00.000000Z;rename;;R;=a.mp4;'=b.mp4;h;3;=3+3;=2+2\n",
        encoding="utf-8-sig",
    )

    entry = read_log(str(path))[0]

    assert entry.old == "=a.mp4"
    assert entry.new == "=b.mp4"
    assert entry.scene_id == "=3+3"
    assert entry.source == "=2+2"


def test_legacy_journal_with_formula_values_reads_as_written(tmp_path):
    path = tmp_path / "rename_log.csv"
    path.write_text(
        f"{LEGACY_HEADER}\n2026-10-01T10:00:00.000000Z;'=a.mp4;@b.mp4;h;=3+3;=2+2\n",
        encoding="utf-8-sig",
    )

    entry = read_log(str(path))[0]

    assert entry.legacy is True
    assert entry.old == "=a.mp4"
    assert entry.new == "@b.mp4"
    assert entry.scene_id == "=3+3"
    assert entry.source == "=2+2"
