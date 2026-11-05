"""Тесты запуска скраперов студий из TUI (без Textual и сети)."""
from video_renamer import studio_tasks


def test_run_stashdb_captures_output(monkeypatch):
    def fake_main(args):
        print("page 1: 10 studios")
        return 0

    monkeypatch.setattr(studio_tasks.stashdb, "main", fake_main)
    lines = []
    code = studio_tasks.run("stashdb", lines.append)
    assert code == 0
    assert lines == ["page 1: 10 studios"]


def test_run_fansdb_passes_its_source(monkeypatch):
    seen = {}

    def fake_main(args, *, source):
        seen["args"] = args
        seen["source"] = source
        print("fansdb done")
        return 0

    monkeypatch.setattr(studio_tasks.fansdb, "main", fake_main)
    code = studio_tasks.run("fansdb")
    assert code == 0
    assert seen["args"] == ["--apply"]
    assert seen["source"] is studio_tasks.fansdb.FANSDB_SOURCE


def test_run_merge_uses_data_files(monkeypatch):
    seen = {}

    def fake_main(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(studio_tasks.merge_studios, "main", fake_main)
    assert studio_tasks.run("merge") == 0
    assert seen["args"][:3] == [
        "data/studios.json",
        "data/studios_fansdb.json",
        "data/studios_theporndb.json",
    ]
    assert "--apply" in seen["args"]


def test_unknown_kind_raises():
    try:
        studio_tasks.run("unknown")
    except ValueError as exc:
        assert "unknown" in str(exc)
    else:
        raise AssertionError("ValueError expected")
