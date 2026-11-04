"""Тесты CLI `fix_video_titles.py`: CSV-ревью и режимы preview/apply."""
import csv

import pytest

import fix_video_titles
from fix_video_titles import (
    append_rename_log,
    main,
    read_preview_csv,
    write_preview_csv,
)
from oshash import oshash_from_path
from stashdb_client import StashDBAuthError, StashDBQueryError
from title_service import ApplyResult, PreviewReport, PreviewRow


def _row(tmp_path, approved=True, old="old.mp4", new="new.mp4", create=True):
    """Строка preview с реальными size/oshash для файла в tmp_path."""
    path = tmp_path / old
    if create and not path.exists():
        path.write_bytes(b"abcdefghij")
    if not create:
        return PreviewRow(
            approved, old, new, "fingerprint", 1.0, "scene-1", "", "h", 10
        )
    return PreviewRow(
        approved,
        old,
        new,
        "fingerprint",
        1.0,
        "scene-1",
        "",
        oshash_from_path(str(path)),
        path.stat().st_size,
    )


def test_csv_roundtrip_and_formula_guard(tmp_path):
    p = tmp_path / "preview.csv"
    row = PreviewRow(True, "old.mp4", "=New.mp4", "fingerprint", 1.0,
                     "s1", "note", "h", 3)
    write_preview_csv(str(p), [row])
    raw = p.read_text(encoding="utf-8-sig")
    assert "'=New.mp4" in raw
    assert read_preview_csv(str(p))[0].new_rel == "=New.mp4"


def test_csv_formula_guard_covers_old_and_roundtrips_all_fields(tmp_path):
    p = tmp_path / "preview.csv"
    row = PreviewRow(False, "-old.mp4", "@new.mp4", "text", 0.5,
                     "s2", "a;b", "abc", 42)
    write_preview_csv(str(p), [row])
    raw = p.read_text(encoding="utf-8-sig")
    assert "'-old.mp4" in raw
    assert "'@new.mp4" in raw
    assert read_preview_csv(str(p)) == [row]


def test_csv_read_accepts_approved_variants(tmp_path):
    p = tmp_path / "preview.csv"
    p.write_text(
        "approved;old;new;source;confidence;scene_id;note;oshash;size\n"
        "YES;a.mp4;b.mp4;text;0.7;;;h;1\n"
        "n;c.mp4;d.mp4;text;0.7;;;h;1\n"
        "1;e.mp4;f.mp4;text;0.7;;;h;1\n"
        ";g.mp4;h.mp4;text;0.7;;;h;1\n",
        encoding="utf-8-sig",
    )
    rows = read_preview_csv(str(p))
    assert [row.approved for row in rows] == [True, False, True, False]


def test_csv_read_rejects_missing_columns(tmp_path):
    p = tmp_path / "preview.csv"
    p.write_text("approved;old\nn;a.mp4\n", encoding="utf-8-sig")
    with pytest.raises(ValueError):
        read_preview_csv(str(p))


def test_preview_cli_writes_csv_and_does_not_rename(tmp_path, monkeypatch):
    (tmp_path / "old.mp4").write_bytes(b"abcdefghij")
    monkeypatch.setenv("STASHDB_API_KEY", "")
    code = main(["preview", str(tmp_path), "--offline",
                 "--csv", str(tmp_path / "p.csv"),
                 "--cache", str(tmp_path / "c.sqlite3")])
    assert code == 0
    assert (tmp_path / "old.mp4").exists()
    assert (tmp_path / "p.csv").exists()


def test_preview_missing_key_warns_and_goes_offline(tmp_path, monkeypatch, capsys):
    captured = {}

    class FakeService:
        def __init__(self, root, studios, cache, client=None, **kwargs):
            captured["offline"] = kwargs["offline"]
            captured["client"] = client

        def preview(self):
            return PreviewReport(rows=(), warnings=(), had_transient_errors=False)

    monkeypatch.delenv("STASHDB_API_KEY", raising=False)
    monkeypatch.setattr(fix_video_titles, "TitleService", FakeService)
    code = main(["preview", str(tmp_path),
                 "--csv", str(tmp_path / "p.csv"),
                 "--cache", str(tmp_path / "c.sqlite3")])
    assert code == 0
    assert captured["offline"] is True
    assert captured["client"] is None
    assert "STASHDB_API_KEY" in capsys.readouterr().err


def test_preview_auth_error_exits_2_without_csv(tmp_path, monkeypatch, capsys):
    class FakeService:
        def __init__(self, *args, **kwargs):
            pass

        def preview(self):
            raise StashDBAuthError("StashDB rejected the API key")

    monkeypatch.setenv("STASHDB_API_KEY", "bad")
    monkeypatch.setattr(fix_video_titles, "TitleService", FakeService)
    code = main(["preview", str(tmp_path),
                 "--csv", str(tmp_path / "p.csv"),
                 "--cache", str(tmp_path / "c.sqlite3")])
    assert code == 2
    assert not (tmp_path / "p.csv").exists()
    assert "STASHDB_API_KEY" in capsys.readouterr().err


def test_preview_query_error_exits_2_without_csv(tmp_path, monkeypatch, capsys):
    class FakeService:
        def __init__(self, *args, **kwargs):
            pass

        def preview(self):
            raise StashDBQueryError("bad query")

    monkeypatch.setenv("STASHDB_API_KEY", "key")
    monkeypatch.setattr(fix_video_titles, "TitleService", FakeService)
    code = main(["preview", str(tmp_path),
                 "--csv", str(tmp_path / "p.csv"),
                 "--cache", str(tmp_path / "c.sqlite3")])
    assert code == 2
    assert not (tmp_path / "p.csv").exists()
    assert "bad query" in capsys.readouterr().err


def test_preview_transient_errors_write_csv_and_exit_1(tmp_path, monkeypatch):
    class FakeService:
        def __init__(self, *args, **kwargs):
            pass

        def preview(self):
            return PreviewReport(
                rows=(), warnings=("StashDB text search failed",),
                had_transient_errors=True,
            )

    monkeypatch.setenv("STASHDB_API_KEY", "key")
    monkeypatch.setattr(fix_video_titles, "TitleService", FakeService)
    code = main(["preview", str(tmp_path),
                 "--csv", str(tmp_path / "p.csv"),
                 "--cache", str(tmp_path / "c.sqlite3")])
    assert code == 1
    assert (tmp_path / "p.csv").exists()


def test_refresh_with_offline_is_parser_error(tmp_path, monkeypatch):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    with pytest.raises(SystemExit) as exc:
        main(["preview", str(tmp_path), "--offline", "--refresh"])
    assert exc.value.code == 2


def test_apply_dry_run_does_not_rename_or_log(tmp_path):
    row = _row(tmp_path)
    csv_path = tmp_path / "preview.csv"
    write_preview_csv(str(csv_path), [row])

    code = main(["apply", str(csv_path), "--root", str(tmp_path),
                 "--log", str(tmp_path / "rename_log.csv")])

    assert code == 0
    assert (tmp_path / "old.mp4").exists()
    assert not (tmp_path / "new.mp4").exists()
    assert not (tmp_path / "rename_log.csv").exists()


def test_apply_log_write_error_exits_1(tmp_path, monkeypatch, capsys):
    row = _row(tmp_path)
    csv_path = tmp_path / "preview.csv"
    write_preview_csv(str(csv_path), [row])

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(fix_video_titles, "append_rename_log", boom)
    code = main(["apply", str(csv_path), "--root", str(tmp_path),
                 "--log", str(tmp_path / "rename_log.csv"), "--apply"])

    assert code == 1
    assert "disk full" in capsys.readouterr().err


def test_apply_renames_only_approved_and_logs_renames(tmp_path, capsys):
    approved = _row(tmp_path, approved=True, old="old.mp4", new="new.mp4")
    skipped = _row(tmp_path, approved=False, old="keep.mp4", new="gone.mp4")
    csv_path = tmp_path / "preview.csv"
    write_preview_csv(str(csv_path), [approved, skipped])

    code = main(["apply", str(csv_path), "--root", str(tmp_path),
                 "--log", str(tmp_path / "rename_log.csv"), "--apply"])

    assert code == 0
    assert (tmp_path / "new.mp4").exists()
    assert not (tmp_path / "old.mp4").exists()
    assert (tmp_path / "keep.mp4").exists()
    log = (tmp_path / "rename_log.csv").read_text(encoding="utf-8-sig")
    assert log.splitlines()[0] == "timestamp;old;new;oshash;scene_id;source"
    assert "new.mp4" in log
    assert "keep.mp4" not in log
    assert len(log.strip().splitlines()) == 2
    assert approved.oshash in log
    assert "scene-1" in log
    assert "fingerprint" in log
    assert "new.mp4" in capsys.readouterr().out


def test_apply_suffix_collisions_logs_actual_target(tmp_path, capsys):
    row = _row(tmp_path, new="new.mp4")
    (tmp_path / "new.mp4").write_bytes(b"occupied!!")
    csv_path = tmp_path / "preview.csv"
    write_preview_csv(str(csv_path), [row])

    code = main(["apply", str(csv_path), "--root", str(tmp_path),
                 "--log", str(tmp_path / "rename_log.csv"), "--apply",
                 "--suffix-collisions"])

    assert code == 0
    assert (tmp_path / "new (2).mp4").exists()
    assert "new (2).mp4" in capsys.readouterr().out
    log = (tmp_path / "rename_log.csv").read_text(encoding="utf-8-sig")
    fields = log.strip().splitlines()[1].split(";")
    assert fields[1:] == [
        "old.mp4", "new (2).mp4", row.oshash, "scene-1", "fingerprint",
    ]



def test_apply_row_error_exits_1(tmp_path, capsys):
    row = _row(tmp_path, approved=True, old="missing.mp4", new="new.mp4",
               create=False)
    csv_path = tmp_path / "preview.csv"
    write_preview_csv(str(csv_path), [row])

    code = main(["apply", str(csv_path), "--root", str(tmp_path),
                 "--log", str(tmp_path / "rename_log.csv"), "--apply"])

    assert code == 1
    assert not (tmp_path / "rename_log.csv").exists()
    assert "missing.mp4" in capsys.readouterr().err


def test_apply_missing_csv_exits_2(tmp_path, capsys):
    code = main(["apply", str(tmp_path / "nope.csv"),
                 "--root", str(tmp_path), "--apply"])
    assert code == 2
    assert capsys.readouterr().err


def test_apply_csv_parser_error_exits_2_without_traceback(tmp_path, capsys):
    # Поле длиннее csv.field_size_limit() реально заставляет DictReader
    # возбудить csv.Error, а не ValueError/OSError.
    huge = "x" * (csv.field_size_limit() + 1)
    csv_path = tmp_path / "broken.csv"
    csv_path.write_text(
        "approved;old;new;source;confidence;scene_id;note;oshash;size\n"
        f"y;old.mp4;new.mp4;text;1.0;;{huge};;\n",
        encoding="utf-8-sig",
    )

    code = main(["apply", str(csv_path), "--root", str(tmp_path),
                 "--log", str(tmp_path / "rename_log.csv")])

    assert code == 2
    err = capsys.readouterr().err
    assert "cannot read" in err
    assert "Traceback" not in err
    assert not (tmp_path / "new.mp4").exists()
    assert not (tmp_path / "rename_log.csv").exists()


def test_append_rename_log_only_renamed_and_header_once(tmp_path):
    log = tmp_path / "rename_log.csv"
    rows = [
        PreviewRow(True, "a.mp4", "b.mp4", "fingerprint", 1.0,
                   "scene-a", "", "hash-a", 10),
        PreviewRow(False, "c.mp4", "d.mp4", "text", 0.5,
                   "scene-c", "", "hash-c", 20),
        PreviewRow(True, "e.mp4", "f.mp4", "heuristic", 0.0,
                   "", "", "hash-e", 30),
    ]
    results = [
        ApplyResult("a.mp4", "b.mp4", "renamed", ""),
        ApplyResult("c.mp4", "d.mp4", "skipped", "not approved"),
        ApplyResult("e.mp4", "f.mp4", "error", "target is occupied"),
    ]
    append_rename_log(str(log), rows, results, "2026-11-04T12:00:00Z")
    append_rename_log(
        str(log),
        [PreviewRow(True, "g.mp4", "h.mp4", "text", 0.9,
                    "scene-g", "", "hash-g", 40)],
        [ApplyResult("g.mp4", "h.mp4", "renamed", "")],
        "2026-11-04T12:01:00Z",
    )

    text = log.read_text(encoding="utf-8-sig")
    lines = text.strip().splitlines()
    assert lines[0] == "timestamp;old;new;oshash;scene_id;source"
    assert len(lines) == 3
    assert lines[1] == (
        "2026-11-04T12:00:00Z;a.mp4;b.mp4;hash-a;scene-a;fingerprint"
    )
    assert lines[2] == (
        "2026-11-04T12:01:00Z;g.mp4;h.mp4;hash-g;scene-g;text"
    )
    assert "c.mp4" not in text
    assert "e.mp4" not in text


def test_append_rename_log_writes_all_six_fields(tmp_path):
    log = tmp_path / "rename_log.csv"
    rows = [PreviewRow(True, "old.mp4", "new.mp4", "fingerprint", 1.0,
                       "scene-42", "note", "deadbeef", 123)]
    results = [ApplyResult("old.mp4", "new.mp4", "renamed", "")]

    append_rename_log(str(log), rows, results, "2026-11-04T12:00:00Z")

    fields = log.read_text(encoding="utf-8-sig").splitlines()[1].split(";")
    assert fields == [
        "2026-11-04T12:00:00Z", "old.mp4", "new.mp4",
        "deadbeef", "scene-42", "fingerprint",
    ]

