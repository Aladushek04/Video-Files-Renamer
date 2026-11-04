import os
from pathlib import Path

import pytest

from stashdb_client import (
    Performer,
    Scene,
    StashDBAuthError,
    StashDBQueryError,
    StashDBTransientError,
    Studio,
)
from title_matcher import normalize_for_match, parse_local_name
from title_service import ProgressEvent, TitleService, iter_video_files


def scene(title="Poking Glory", studio="BrazzersExxtra", date="2026-10-03",
          performers=("Ryan Reid",), scene_id="s1"):
    return Scene(
        scene_id,
        title,
        date,
        None,
        None,
        1200,
        Studio("st", studio) if studio else None,
        tuple(Performer(f"p{index}", name)
              for index, name in enumerate(performers)),
    )


SCENE = scene()
TEXT_SCENE = scene(title="Ryan Reid Poking Glory", studio=None, date=None)


class FakeCache:
    def __init__(self, fingerprints=None, text=None):
        self.fingerprints = dict(fingerprints or {})
        self.text = dict(text or {})
        self.fingerprint_gets = []
        self.fingerprint_puts = []
        self.text_gets = []
        self.text_puts = []

    def get_fingerprint(self, oshash):
        self.fingerprint_gets.append(oshash)
        return self.fingerprints.get(oshash)

    def put_fingerprint(self, oshash, scenes):
        stored = list(scenes)
        self.fingerprint_puts.append((oshash, stored))
        self.fingerprints[oshash] = stored

    def get_text_query(self, term):
        self.text_gets.append(term)
        return self.text.get(term)

    def put_text_query(self, term, scenes):
        stored = list(scenes)
        self.text_puts.append((term, stored))
        self.text[term] = stored


class FakeClient:
    def __init__(self, by_hash=None, text=None, hash_error=None,
                 text_error=None):
        self.by_hash = by_hash
        self.text = list(text or [])
        self.hash_error = hash_error
        self.text_error = text_error
        self.hash_calls = []
        self.text_calls = []

    def find_scenes_by_hashes(self, hashes):
        self.hash_calls.append(list(hashes))
        if self.hash_error is not None:
            raise self.hash_error
        if self.by_hash is None:
            return [[SCENE] for _ in hashes]
        return [list(self.by_hash.get(oshash, [])) for oshash in hashes]

    def search_scenes(self, term, limit=10):
        self.text_calls.append((term, limit))
        if self.text_error is not None:
            raise self.text_error
        return list(self.text)


def write(path: Path, size: int = 100) -> None:
    path.write_bytes(b"x" * size)


def by_name(mapping):
    return lambda path: mapping[Path(path).name]


# --- test file iteration ----------------------------------------------------


def test_iter_video_files_filters_extensions_and_temp_names(tmp_path):
    write(tmp_path / "a.mp4")
    write(tmp_path / "B.MKV")
    write(tmp_path / "notes.txt")
    write(tmp_path / "__ren_tmp_5.mp4")
    (tmp_path / "folder.mp4").mkdir()

    names = {Path(path).name
             for path in iter_video_files(str(tmp_path), False)}
    assert names == {"a.mp4", "B.MKV"}


def test_iter_video_files_recurses_and_skips_symlinks(tmp_path):
    write(tmp_path / "a.mp4")
    sub = tmp_path / "sub"
    sub.mkdir()
    write(sub / "c.webm")
    write(sub / "d.txt")

    real = tmp_path / "real.mov"
    write(real)
    link = tmp_path / "link.mov"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pass

    found = {
        Path(path).relative_to(tmp_path).as_posix()
        for path in iter_video_files(str(tmp_path), True)
    }
    assert "sub/c.webm" in found
    assert "sub/d.txt" not in found
    if link.exists():
        assert "link.mov" not in found


def test_iter_video_files_missing_root_is_empty(tmp_path):
    assert list(iter_video_files(str(tmp_path / "missing"), True)) == []


def test_iter_video_files_skips_symlinked_files(tmp_path):
    real = tmp_path / "real.mp4"
    write(real)
    link = tmp_path / "alias.mp4"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")

    names = {Path(path).name
             for path in iter_video_files(str(tmp_path), False)}
    assert names == {"real.mp4"}


# --- preview: fingerprints --------------------------------------------------


def test_preview_marks_unique_fingerprint_approved(tmp_path):
    name = "brazzersexxtra 26.10.03 ryan reid poking glory.mp4"
    write(tmp_path / name)
    service = TitleService(
        str(tmp_path), {}, FakeCache(), FakeClient(),
        oshash_func=lambda path: "abc123",
    )

    report = service.preview()

    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.source == "fingerprint"
    assert row.approved is True
    assert row.new_rel.endswith("Poking Glory.mp4")
    assert row.old_rel == name
    assert row.scene_id == "s1"
    assert row.confidence == 1.0
    assert row.oshash == "abc123"
    assert row.size == 100
    assert report.warnings == ()
    assert report.had_transient_errors is False


def test_fingerprints_are_batched_once_and_negative_results_cached(tmp_path):
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        write(tmp_path / name)
    cache = FakeCache({"h1": [SCENE]})
    client = FakeClient(by_hash={"h2": [SCENE], "h3": []})
    hashes = {"a.mp4": "h1", "b.mp4": "h2", "c.mp4": "h3"}

    report = TitleService(
        str(tmp_path), {}, cache, client, oshash_func=by_name(hashes),
    ).preview()

    assert len(report.rows) == 3
    assert client.hash_calls == [["h2", "h3"]]
    assert cache.fingerprint_puts == [("h2", [SCENE]), ("h3", [])]
    assert cache.fingerprint_gets == ["h1", "h2", "h3"]


def test_second_preview_reuses_cached_negative_results(tmp_path):
    write(tmp_path / "a.mp4")
    cache = FakeCache()
    first_client = FakeClient(by_hash={"h": []})
    service = TitleService(
        str(tmp_path), {}, cache, first_client, oshash_func=lambda path: "h",
    )
    service.preview()
    assert first_client.hash_calls == [["h"]]

    second_client = FakeClient(by_hash={"h": [SCENE]})
    report = TitleService(
        str(tmp_path), {}, cache, second_client, oshash_func=lambda path: "h",
    ).preview()

    assert second_client.hash_calls == []
    assert report.rows[0].source == "heuristic"


def test_refresh_ignores_cached_fingerprints(tmp_path):
    write(tmp_path / "a.mp4")
    cache = FakeCache({"h": []})
    client = FakeClient(by_hash={"h": [SCENE]})

    report = TitleService(
        str(tmp_path), {}, cache, client, refresh=True,
        oshash_func=lambda path: "h",
    ).preview()

    assert cache.fingerprint_gets == []
    assert client.hash_calls == [["h"]]
    assert report.rows[0].source == "fingerprint"


def test_fingerprint_transient_error_is_a_warning_and_not_cached(tmp_path):
    write(tmp_path / "a.mp4")
    cache = FakeCache()
    client = FakeClient(hash_error=StashDBTransientError("network down"))

    report = TitleService(
        str(tmp_path), {}, cache, client, oshash_func=lambda path: "h",
    ).preview()

    assert report.had_transient_errors is True
    assert len(report.warnings) == 1
    assert "network down" in report.warnings[0]
    assert cache.fingerprint_puts == []
    assert report.rows[0].source == "heuristic"
    assert "stashdb_error" in report.rows[0].note


@pytest.mark.parametrize("error", [
    StashDBAuthError("bad key"),
    StashDBQueryError("bad query"),
])
def test_fatal_stashdb_errors_propagate(tmp_path, error):
    write(tmp_path / "a.mp4")
    service = TitleService(
        str(tmp_path), {}, FakeCache(), FakeClient(hash_error=error),
        oshash_func=lambda path: "h",
    )

    with pytest.raises(type(error)):
        service.preview()


# --- preview: text search ---------------------------------------------------


def test_text_search_is_used_when_fingerprint_is_empty(tmp_path):
    name = "ryan reid poking glory.mp4"
    write(tmp_path / name)
    cache = FakeCache()
    client = FakeClient(by_hash={"h": []}, text=[TEXT_SCENE])

    report = TitleService(
        str(tmp_path), {}, cache, client, oshash_func=lambda path: "h",
    ).preview()

    assert client.hash_calls == [["h"]]
    assert client.text_calls == [("ryan reid poking glory", 10)]
    assert cache.text_puts == [("ryan reid poking glory", [TEXT_SCENE])]

    row = report.rows[0]
    assert row.source == "text"
    assert row.approved is False
    assert row.scene_id == "s1"


def test_text_search_reads_cache_offline_without_network(tmp_path):
    name = "ryan reid poking glory.mp4"
    write(tmp_path / name)
    term = normalize_for_match(parse_local_name(name, {}).rest)
    cache = FakeCache({"h": []}, {term: [TEXT_SCENE]})
    client = FakeClient(by_hash={"h": [SCENE]}, text=[SCENE])

    report = TitleService(
        str(tmp_path), {}, cache, client, offline=True,
        oshash_func=lambda path: "h",
    ).preview()

    assert client.hash_calls == []
    assert client.text_calls == []
    assert report.rows[0].source == "text"


def test_offline_without_cache_uses_heuristic_only(tmp_path):
    write(tmp_path / "a 26.10.03 ryan reid poking glory.mp4")
    client = FakeClient(by_hash={"h": [SCENE]}, text=[SCENE])

    report = TitleService(
        str(tmp_path), {}, FakeCache(), client, offline=True,
        oshash_func=lambda path: "h",
    ).preview()

    assert client.hash_calls == []
    assert client.text_calls == []
    assert report.rows[0].source == "heuristic"


def test_no_text_search_skips_network_and_cache(tmp_path):
    write(tmp_path / "a.mp4")
    cache = FakeCache({"h": []})
    client = FakeClient(text=[SCENE])

    report = TitleService(
        str(tmp_path), {}, cache, client, text_search=False,
        oshash_func=lambda path: "h",
    ).preview()

    assert client.text_calls == []
    assert cache.text_gets == []
    assert report.rows[0].source == "heuristic"


def test_text_transient_error_marks_row_and_is_not_cached(tmp_path):
    write(tmp_path / "ryan reid poking glory.mp4")
    cache = FakeCache({"h": []})
    client = FakeClient(text_error=StashDBTransientError("timeout"))

    report = TitleService(
        str(tmp_path), {}, cache, client, oshash_func=lambda path: "h",
    ).preview()

    assert report.had_transient_errors is True
    assert any("timeout" in warning for warning in report.warnings)
    assert cache.text_puts == []
    assert "stashdb_error" in report.rows[0].note


# --- preview: naming and collisions ----------------------------------------


def test_already_correct_name_is_marked_unchanged(tmp_path):
    name = ("BrazzersExxtra - 03.10.2026 - Ryan Reid - Poking Glory.mp4")
    write(tmp_path / name)
    studios = {"brazzersexxtra": "BrazzersExxtra"}

    report = TitleService(
        str(tmp_path), studios, FakeCache({"h": [SCENE]}),
        oshash_func=lambda path: "h",
    ).preview()

    row = report.rows[0]
    assert row.old_rel == name
    assert row.new_rel == name
    assert row.source == "unchanged"
    assert row.approved is False


def test_collision_inside_batch_gets_numeric_suffix(tmp_path):
    write(tmp_path / "a 26.10.03 ryan reid poking glory.mp4")
    write(tmp_path / "b 26.10.03 ryan reid poking glory.mp4")

    report = TitleService(
        str(tmp_path), {}, FakeCache({"h": [SCENE]}), FakeClient(),
        oshash_func=lambda path: "h",
    ).preview()

    names = [Path(row.new_rel).name for row in report.rows]
    assert names[0] == \
        "BrazzersExxtra - 03.10.2026 - Ryan Reid - Poking Glory.mp4"
    assert names[1] == \
        "BrazzersExxtra - 03.10.2026 - Ryan Reid - Poking Glory (2).mp4"
    assert "collision" in report.rows[1].note


def test_existing_target_outside_batch_is_occupied(tmp_path):
    target = "BrazzersExxtra - 03.10.2026 - Ryan Reid - Poking Glory.mp4"
    write(tmp_path / target)
    source = "0 26.10.03 ryan reid poking glory.mp4"
    write(tmp_path / source)

    report = TitleService(
        str(tmp_path), {}, FakeCache({"h": [SCENE]}), FakeClient(),
        limit=1, oshash_func=lambda path: "h",
    ).preview()

    assert len(report.rows) == 1
    assert Path(report.rows[0].new_rel).name == \
        "BrazzersExxtra - 03.10.2026 - Ryan Reid - Poking Glory (2).mp4"


def test_target_that_is_itself_renamed_is_not_occupied(tmp_path):
    first = "0 26.10.03 ryan reid poking glory.mp4"
    second = "BrazzersExxtra - 03.10.2026 - Ryan Reid - Poking Glory.mp4"
    write(tmp_path / first)
    write(tmp_path / second)
    hashes = {first: "h1", second: "h2"}
    cache = FakeCache({"h1": [SCENE],
                       "h2": [scene(title="Second Title")]})

    report = TitleService(
        str(tmp_path), {}, cache, FakeClient(), oshash_func=by_name(hashes),
    ).preview()

    first_row, second_row = report.rows
    assert Path(first_row.new_rel).name == second
    assert first_row.source == "fingerprint"
    assert Path(second_row.new_rel).name != second


def test_limit_truncates_files_before_hashing(tmp_path):
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        write(tmp_path / name)
    hashed = []

    def hasher(path):
        hashed.append(Path(path).name)
        return "h"

    report = TitleService(
        str(tmp_path), {}, FakeCache({"h": [SCENE]}), FakeClient(),
        limit=2, oshash_func=hasher,
    ).preview()

    assert hashed == ["a.mp4", "b.mp4"]
    assert len(report.rows) == 2


# --- progress and stdout ----------------------------------------------------


def test_progress_events_cover_all_phases(tmp_path):
    write(tmp_path / "a.mp4")
    events = []

    TitleService(
        str(tmp_path), {}, FakeCache({"h": [SCENE]}), FakeClient(),
        oshash_func=lambda path: "h", progress=events.append,
    ).preview()

    phases = [event.phase for event in events]
    assert phases[0] == "scan"
    assert "hash" in phases
    assert "fingerprint" in phases
    assert "text" in phases
    assert phases[-1] == "done"
    assert all(isinstance(event, ProgressEvent) for event in events)

    hash_event = next(event for event in events if event.phase == "hash")
    assert (hash_event.current, hash_event.total) == (1, 1)
    assert hash_event.message == "a.mp4"


def test_progress_reports_text_search(tmp_path):
    write(tmp_path / "ryan reid poking glory.mp4")
    events = []

    TitleService(
        str(tmp_path), {}, FakeCache({"h": []}),
        FakeClient(text=[TEXT_SCENE]), oshash_func=lambda path: "h",
        progress=events.append,
    ).preview()

    text_events = [event for event in events if event.phase == "text"]
    assert len(text_events) == 1
    assert text_events[0].current == 1
    assert text_events[0].total == 1
    assert text_events[0].message == "ryan reid poking glory.mp4"


def test_preview_prints_nothing(tmp_path, capsys):
    write(tmp_path / "a.mp4")
    TitleService(
        str(tmp_path), {}, FakeCache({"h": [SCENE]}), FakeClient(),
        oshash_func=lambda path: "h",
    ).preview()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_preview_returns_immutable_report(tmp_path):
    write(tmp_path / "a.mp4")
    report = TitleService(
        str(tmp_path), {}, FakeCache({"h": [SCENE]}), FakeClient(),
        oshash_func=lambda path: "h",
    ).preview()

    assert isinstance(report.rows, tuple)
    assert isinstance(report.warnings, tuple)
    with pytest.raises(Exception):
        report.had_transient_errors = True


def test_nested_paths_keep_their_relative_directory(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    write(sub / "a 26.10.03 ryan reid poking glory.mp4")

    report = TitleService(
        str(tmp_path), {}, FakeCache({"h": [SCENE]}), FakeClient(),
        recursive=True, oshash_func=lambda path: "h",
    ).preview()

    row = report.rows[0]
    assert row.old_rel == os.path.join(
        "sub", "a 26.10.03 ryan reid poking glory.mp4")
    assert row.new_rel == os.path.join(
        "sub", "BrazzersExxtra - 03.10.2026 - Ryan Reid - Poking Glory.mp4")
