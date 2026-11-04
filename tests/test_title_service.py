import os
from pathlib import Path

import pytest

import title_service

from stashdb_client import (
    Performer,
    Scene,
    StashDBAuthError,
    StashDBQueryError,
    StashDBTransientError,
    Studio,
)
from title_matcher import normalize_for_match, parse_local_name
from title_service import (
    PreviewRow,
    ProgressEvent,
    TitleService,
    iter_video_files,
)


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


def test_offline_text_search_uses_cache_without_network(tmp_path):
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
    assert cache.text_gets == [term]
    assert report.had_transient_errors is False
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


# --- apply ------------------------------------------------------------------


def row(old, new, approved=True, oshash="h", size=3):
    return PreviewRow(approved, old, new, "fingerprint", 1.0, "s", "",
                      oshash, size)


def apply_service(tmp_path, hasher=None):
    return TitleService(str(tmp_path), {}, None, None,
                        oshash_func=hasher or (lambda path: "h"))


def test_apply_renames_approved_and_skips_rest(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    service = TitleService(str(tmp_path), {}, None, None,
                           oshash_func=lambda p: "h")
    report = service.apply_rows(
        [row("a.mp4", "b.mp4"), row("a.mp4", "c.mp4", approved=False)],
        dry_run=False)
    assert (tmp_path / "b.mp4").exists()
    assert report.results[1].status == "skipped"


def test_apply_rejects_path_traversal(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    service = TitleService(str(tmp_path), {}, None, None,
                           oshash_func=lambda p: "h")
    report = service.apply_rows([row("a.mp4", "..\\evil.mp4")], dry_run=False)
    assert report.had_errors is True
    assert not (tmp_path.parent / "evil.mp4").exists()


def test_apply_handles_rename_chain_and_upper_extension(tmp_path):
    (tmp_path / "a.MP4").write_bytes(b"abc")
    (tmp_path / "b.MP4").write_bytes(b"abc")
    service = TitleService(str(tmp_path), {}, None, None,
                           oshash_func=lambda p: "h")
    report = service.apply_rows(
        [row("a.MP4", "b.MP4"), row("b.MP4", "c.MP4")], dry_run=False)
    assert report.had_errors is False
    assert (tmp_path / "b.MP4").exists() and (tmp_path / "c.MP4").exists()


def test_apply_rejects_reserved_name(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    service = TitleService(str(tmp_path), {}, None, None,
                           oshash_func=lambda p: "h")
    report = service.apply_rows([row("a.mp4", "CON.mp4")], dry_run=False)
    assert report.had_errors is True


def test_apply_skips_unapproved_before_any_validation(tmp_path):
    def unexpected_hash(path):
        raise AssertionError("unapproved rows must not be hashed")

    report = apply_service(tmp_path, unexpected_hash).apply_rows([
        row(str(tmp_path / "missing.mp4"), "..\\evil.mp4", approved=False),
    ], dry_run=False)

    assert report.had_errors is False
    assert report.results[0].status == "skipped"


def test_apply_rejects_absolute_paths_old_traversal_and_new_subdirs(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.mp4").write_bytes(b"abc")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"abc")
    service = apply_service(root)

    report = service.apply_rows([
        row("../outside.mp4", "renamed.mp4"),
        row(str(outside), "other.mp4"),
        row("a.mp4", str(tmp_path / "absolute.mp4")),
        row("a.mp4", "sub/new.mp4"),
    ], dry_run=False)

    assert [result.status for result in report.results] == ["error"] * 4
    assert outside.read_bytes() == b"abc"
    assert (root / "a.mp4").read_bytes() == b"abc"


def test_apply_rejects_non_regular_old_file(tmp_path):
    (tmp_path / "folder.mp4").mkdir()
    report = apply_service(tmp_path).apply_rows(
        [row("folder.mp4", "new.mp4")], dry_run=False)
    assert report.results[0].status == "error"
    assert (tmp_path / "folder.mp4").is_dir()


def test_apply_rejects_symlinked_old_file(tmp_path):
    real = tmp_path / "real.mp4"
    real.write_bytes(b"abc")
    link = tmp_path / "alias.mp4"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")

    report = apply_service(tmp_path).apply_rows(
        [row("alias.mp4", "new.mp4")], dry_run=False)

    assert report.results[0].status == "error"
    assert real.read_bytes() == b"abc"
    assert not (tmp_path / "new.mp4").exists()


def test_apply_rejects_extension_mismatch_and_overlong_name(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    report = apply_service(tmp_path).apply_rows([
        row("a.mp4", "new.mkv"),
        row("a.mp4", "x" * 252 + ".mp4"),
    ], dry_run=False)

    assert [result.status for result in report.results] == ["error", "error"]
    assert (tmp_path / "a.mp4").exists()


def test_apply_rejects_stale_size_and_oshash(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    (tmp_path / "b.mp4").write_bytes(b"abc")
    report = apply_service(tmp_path).apply_rows([
        row("a.mp4", "a-new.mp4", size=4),
        row("b.mp4", "b-new.mp4", oshash="stale"),
    ], dry_run=False)

    assert [result.status for result in report.results] == ["error", "error"]
    assert (tmp_path / "a.mp4").exists()
    assert (tmp_path / "b.mp4").exists()


def test_apply_force_bypasses_size_and_hash_checks(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")

    def unexpected_hash(path):
        raise AssertionError("force must not hash the source")

    report = apply_service(tmp_path, unexpected_hash).apply_rows(
        [row("a.mp4", "b.mp4", oshash="stale", size=999)],
        dry_run=False, force=True)

    assert report.had_errors is False
    assert report.results[0].status == "renamed"
    assert (tmp_path / "b.mp4").read_bytes() == b"abc"


def test_apply_occupied_target_errors_without_overwriting(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    (tmp_path / "b.mp4").write_bytes(b"old")
    report = apply_service(tmp_path).apply_rows(
        [row("a.mp4", "b.mp4")], dry_run=False)

    assert report.results[0].status == "error"
    assert (tmp_path / "a.mp4").read_bytes() == b"abc"
    assert (tmp_path / "b.mp4").read_bytes() == b"old"


def test_apply_suffix_collisions_uses_numeric_suffix(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    (tmp_path / "b.mp4").write_bytes(b"old")
    report = apply_service(tmp_path).apply_rows(
        [row("a.mp4", "b.mp4")], dry_run=False, suffix_collisions=True)

    assert report.had_errors is False
    assert report.results[0].new_rel == "b (2).mp4"
    assert (tmp_path / "b.mp4").read_bytes() == b"old"
    assert (tmp_path / "b (2).mp4").read_bytes() == b"abc"


def test_apply_dry_run_reports_actual_suffix_target(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    (tmp_path / "b.mp4").write_bytes(b"old")
    report = apply_service(tmp_path).apply_rows(
        [row("a.mp4", "b.mp4")], suffix_collisions=True)

    assert report.results[0].status == "renamed"
    assert report.results[0].new_rel == "b (2).mp4"
    assert not (tmp_path / "b (2).mp4").exists()


def test_apply_dry_run_validates_without_changing_files(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    report = apply_service(tmp_path).apply_rows([row("a.mp4", "b.mp4")])

    assert report.had_errors is False
    assert report.results[0].status == "renamed"
    assert isinstance(report.results, tuple)
    assert (tmp_path / "a.mp4").read_bytes() == b"abc"
    assert not (tmp_path / "b.mp4").exists()
    assert not list(tmp_path.glob("__ren_tmp_*"))
    with pytest.raises(Exception):
        report.had_errors = True


def test_apply_dry_run_invalidates_dependents_of_blocked_rows(tmp_path):
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        (tmp_path / name).write_bytes(b"abc")
    service = apply_service(tmp_path)
    rows = [row("a.mp4", "b.mp4"), row("b.mp4", "c.mp4")]

    dry_report = service.apply_rows(rows)
    assert [result.status for result in dry_report.results] == ["error", "error"]

    apply_report = service.apply_rows(rows, dry_run=False)
    assert [result.status for result in apply_report.results] == [
        result.status for result in dry_report.results
    ]
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        assert (tmp_path / name).read_bytes() == b"abc"
    assert not list(tmp_path.glob("__ren_tmp_*"))


def test_apply_validation_error_does_not_stop_other_rows(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    report = apply_service(tmp_path).apply_rows([
        row("missing.mp4", "x.mp4"),
        row("a.mp4", "b.mp4"),
    ], dry_run=False)

    assert [result.status for result in report.results] == ["error", "renamed"]
    assert report.had_errors is True
    assert (tmp_path / "b.mp4").read_bytes() == b"abc"


def test_apply_embedded_nul_does_not_stop_other_rows(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    report = apply_service(tmp_path).apply_rows([
        row("bad\0.mp4", "x.mp4"),
        row("a.mp4", "b.mp4"),
    ], dry_run=False)

    assert [result.status for result in report.results] == ["error", "renamed"]
    assert report.had_errors is True
    assert (tmp_path / "b.mp4").read_bytes() == b"abc"


def test_apply_rename_failure_rolls_back_only_failed_row(tmp_path, monkeypatch):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    (tmp_path / "c.mp4").write_bytes(b"def")
    real_rename = os.rename

    def fail_second_target(src, dst):
        if (os.path.basename(src).startswith("__ren_tmp_")
                and os.path.basename(dst) == "d.mp4"):
            raise OSError("injected rename failure")
        real_rename(src, dst)

    monkeypatch.setattr(title_service.os, "rename", fail_second_target)
    report = apply_service(tmp_path).apply_rows([
        row("a.mp4", "b.mp4"),
        row("c.mp4", "d.mp4"),
    ], dry_run=False)

    assert [result.status for result in report.results] == ["renamed", "error"]
    assert (tmp_path / "b.mp4").read_bytes() == b"abc"
    assert (tmp_path / "c.mp4").read_bytes() == b"def"
    assert not list(tmp_path.glob("__ren_tmp_*"))


def test_apply_chain_failure_restores_original_paths(tmp_path, monkeypatch):
    (tmp_path / "a.mp4").write_bytes(b"abc")
    (tmp_path / "b.mp4").write_bytes(b"def")
    real_rename = os.rename

    def fail_last_target(src, dst):
        if (os.path.basename(src).startswith("__ren_tmp_")
                and os.path.basename(dst) == "c.mp4"):
            raise OSError("injected rename failure")
        real_rename(src, dst)

    monkeypatch.setattr(title_service.os, "rename", fail_last_target)
    report = apply_service(tmp_path).apply_rows([
        row("a.mp4", "b.mp4"),
        row("b.mp4", "c.mp4"),
    ], dry_run=False)

    assert report.had_errors is True
    assert (tmp_path / "a.mp4").read_bytes() == b"abc"
    assert (tmp_path / "b.mp4").read_bytes() == b"def"
    assert not (tmp_path / "c.mp4").exists()
    assert not list(tmp_path.glob("__ren_tmp_*"))
