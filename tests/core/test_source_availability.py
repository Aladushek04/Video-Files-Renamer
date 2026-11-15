"""Независимая доступность источников: нет ключа StashDB — вторичные работают."""

from __future__ import annotations

import pytest

from video_renamer.core import review_actions
from video_renamer.core.fingerprint_sources import build_fingerprint_clients
from video_renamer.core.oshash import oshash_from_path
from video_renamer.core.stashdb_client import (
    StashDBAuthError,
    StashDBTransientError,
)
from video_renamer.core.title_service import TitleService


class FakeClient:
    """Вторичный источник без сети: запоминает вызовы и отдаёт заданные сцены."""

    def __init__(self, *, error: Exception | None = None):
        self.calls: list[tuple[str, list[str]]] = []
        self.text_calls: list[str] = []
        self.error = error

    def find_scenes_by_hashes(self, hashes, algorithm="OSHASH"):
        self.calls.append((algorithm, list(hashes)))
        if self.error is not None:
            raise self.error
        return [[] for _ in hashes]

    def search_scenes(self, term, limit=10):
        self.text_calls.append(term)
        if self.error is not None:
            raise self.error
        return []


class FakeCache:
    def __init__(self):
        self.fingerprints: list[tuple[str, str]] = []

    def get_source_fingerprint(self, name, algorithm, digest):
        self.fingerprints.append((name, digest))

    def put_source_fingerprint(self, name, algorithm, digest, scenes):
        pass

    def get_text_query(self, term):
        return None

    def put_text_query(self, term, scenes):
        pass

    def get_local_phash(self, oshash, size, mtime_ns=0, file_id=0):
        return None

    def put_local_phash(self, oshash, size, value, mtime_ns=0, file_id=0):
        pass

    def close(self):
        pass


def video(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"0123456789abcdef")
    return path


def service(tmp_path, *, offline=False, extra=None, cache=None):
    return TitleService(
        str(tmp_path),
        {},
        cache,
        None,
        offline=offline,
        extra_clients=extra or {},
        oshash_func=lambda path: "h",
        text_search=False,
    )


def test_secondary_source_is_queried_without_primary_key(tmp_path):
    video(tmp_path)
    fans = FakeClient()

    report = service(tmp_path, extra={"FansDB": fans}).preview()

    assert len(report.rows) == 1
    assert fans.calls == [("OSHASH", ["h"])]


def test_explicit_offline_blocks_secondary_sources(tmp_path):
    video(tmp_path)
    fans = FakeClient()

    service(tmp_path, offline=True, extra={"FansDB": fans}).preview()

    assert fans.calls == []


def test_cache_per_source_is_read_in_offline_mode(tmp_path):
    video(tmp_path)
    fans = FakeClient()
    cache = FakeCache()

    service(tmp_path, offline=True, extra={"FansDB": fans}, cache=cache).preview()

    assert ("FansDB", "h") in cache.fingerprints
    assert fans.calls == []


def test_failing_secondary_does_not_stop_the_other(tmp_path):
    video(tmp_path)
    broken = FakeClient(error=StashDBTransientError("network down"))
    working = FakeClient()

    report = service(tmp_path, extra={"FansDB": broken, "ThePornDB": working}).preview()

    assert broken.calls and working.calls
    assert report.had_transient_errors is True
    assert any("FansDB" in warning for warning in report.warnings)


def test_invalid_secondary_key_is_a_warning(tmp_path):
    video(tmp_path)
    rejected = FakeClient(error=StashDBAuthError("bad key"))

    report = service(tmp_path, extra={"FansDB": rejected}).preview()

    assert len(report.rows) == 1
    assert report.rows[0].approved is False
    assert any("FANSDB_API_KEY" in warning for warning in report.warnings)


def test_text_search_needs_primary_client(tmp_path):
    video(tmp_path)
    fans = FakeClient()

    report = service(tmp_path, extra={"FansDB": fans}).preview()

    assert report.rows[0].source == "heuristic"


def test_run_preview_keeps_secondary_sources_without_primary_key(tmp_path, monkeypatch):
    digest = oshash_from_path(str(video(tmp_path)))
    fans = FakeClient()
    monkeypatch.setattr(review_actions, "build_fingerprint_clients", lambda env: {"FansDB": fans})

    report = review_actions.run_preview(
        tmp_path,
        api_key="",
        studios={},
        performers=None,
        cache_path=tmp_path / "cache.sqlite3",
    )

    assert len(report.rows) == 1
    assert fans.calls == [("OSHASH", [digest])]


def test_run_preview_offline_blocks_secondary_sources(tmp_path, monkeypatch):
    video(tmp_path)
    fans = FakeClient()
    monkeypatch.setattr(review_actions, "build_fingerprint_clients", lambda env: {"FansDB": fans})

    review_actions.run_preview(
        tmp_path,
        api_key="",
        offline=True,
        studios={},
        performers=None,
        cache_path=tmp_path / "cache.sqlite3",
    )

    assert fans.calls == []


@pytest.mark.parametrize(
    ("stashdb", "theporndb", "fansdb"),
    [combo for combo in __import__("itertools").product([False, True], repeat=3)],
)
def test_all_key_combinations_build_only_configured_clients(stashdb, theporndb, fansdb):
    env = {}
    for present, name in (
        (stashdb, "STASHDB_API_KEY"),
        (theporndb, "THE_PORNDB_API_KEY"),
        (fansdb, "FANSDB_API_KEY"),
    ):
        if present:
            env[name] = "test-key"

    clients = build_fingerprint_clients(env)

    expected = set()
    if theporndb:
        expected.add("ThePornDB")
    if fansdb:
        expected.add("FansDB")
    assert set(clients) == expected
