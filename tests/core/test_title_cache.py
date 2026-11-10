import json
import sqlite3
from datetime import datetime, timedelta, timezone

from video_renamer.core.stashdb_client import Performer, Scene, Studio
from video_renamer.core.title_cache import TitleCache


def make_scene(i="s1"):
    return Scene(
        id=i,
        title="T",
        release_date=None,
        date=None,
        code=None,
        duration=None,
        studio=None,
        performers=(),
    )


def test_roundtrip_and_negative(tmp_path):
    cache = TitleCache(tmp_path / "c.sqlite3")
    assert cache.get_fingerprint("h") is None
    cache.put_fingerprint("h", [make_scene()])
    assert cache.get_fingerprint("h")[0].id == "s1"
    cache.put_fingerprint("missing", [])
    assert cache.get_fingerprint("missing") == []
    cache.close()


def test_expired_returns_none(tmp_path):
    now = [datetime(2026, 11, 4, tzinfo=timezone.utc)]
    cache = TitleCache(tmp_path / "c.sqlite3", clock=lambda: now[0])
    cache.put_text_query("q", [make_scene()])
    now[0] += timedelta(days=31)
    assert cache.get_text_query("q") is None
    cache.close()


def test_text_query_roundtrip_and_negative(tmp_path):
    cache = TitleCache(tmp_path / "c.sqlite3")
    assert cache.get_text_query("q") is None
    cache.put_text_query("q", [make_scene("s2")])
    assert cache.get_text_query("q")[0].id == "s2"
    cache.put_text_query("empty", [])
    assert cache.get_text_query("empty") == []
    cache.close()


def test_fingerprint_and_text_query_keys_are_independent(tmp_path):
    """The same key in both tables must not collide."""
    cache = TitleCache(tmp_path / "c.sqlite3")
    cache.put_fingerprint("same", [make_scene("from-fingerprint")])
    cache.put_text_query("same", [make_scene("from-text")])
    assert cache.get_fingerprint("same")[0].id == "from-fingerprint"
    assert cache.get_text_query("same")[0].id == "from-text"
    cache.close()


def test_source_fingerprint_is_keyed_by_source_and_algorithm(tmp_path):
    cache = TitleCache(tmp_path / "c.sqlite3")
    scene = Scene(
        id="x",
        title="T",
        release_date=None,
        date=None,
        code=None,
        duration=None,
        source="ThePornDB",
    )
    cache.put_source_fingerprint("ThePornDB", "OSHASH", "abc", [scene])
    assert cache.get_source_fingerprint("StashDB", "OSHASH", "abc") is None
    assert cache.get_source_fingerprint("ThePornDB", "PHASH", "abc") is None
    restored = cache.get_source_fingerprint("ThePornDB", "OSHASH", "abc")
    assert restored[0].id == "x"
    assert restored[0].source == "ThePornDB"
    cache.close()


def test_source_negative_result_is_kept(tmp_path):
    cache = TitleCache(tmp_path / "c.sqlite3")
    cache.put_source_fingerprint("FansDB", "PHASH", "h", [])
    assert cache.get_source_fingerprint("FansDB", "PHASH", "h") == []
    cache.close()


def test_legacy_fingerprints_migrated_once(tmp_path):
    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE fingerprints (hash TEXT PRIMARY KEY, "
        "scenes TEXT NOT NULL, fetched_at TEXT NOT NULL)"
    )
    raw.execute(
        "INSERT INTO fingerprints VALUES ('h1', '[]', ?)", (datetime.now(timezone.utc).isoformat(),)
    )
    raw.commit()
    raw.close()

    cache = TitleCache(path)
    assert cache.get_source_fingerprint("StashDB", "OSHASH", "h1") == []
    cache.close()

    again = TitleCache(path)
    assert again.get_fingerprint("h1") == []
    again.close()


def test_local_phash_is_keyed_by_content_not_path(tmp_path):
    cache = TitleCache(tmp_path / "c.sqlite3")
    cache.put_local_phash("oh", 100, "dc75c0f03795438e")
    assert cache.get_local_phash("oh", 100) == "dc75c0f03795438e"
    assert cache.get_local_phash("oh", 101) is None
    cache.close()


def test_scene_fields_survive_the_roundtrip(tmp_path):
    """Studio and performers are stored as part of the cached scene JSON."""
    scene = Scene(
        id="s3",
        title="Poking Glory",
        release_date="2026-10-03",
        date="2026-10-03",
        code="GP1",
        duration=1200,
        studio=Studio(id="st1", name="BrazzersExxtra", parent_id="st0", parent_name="Brazzers"),
        performers=(Performer(id="p1", name="Ryan Reid", alias="Ryan"),),
    )
    cache = TitleCache(tmp_path / "c.sqlite3")
    cache.put_fingerprint("h", [scene])
    restored = cache.get_fingerprint("h")[0]
    assert restored == scene
    assert restored.studio.parent_name == "Brazzers"
    assert restored.performers[0].display_name == "Ryan"
    cache.close()


def test_put_overwrites_a_previous_entry(tmp_path):
    cache = TitleCache(tmp_path / "c.sqlite3")
    cache.put_fingerprint("h", [make_scene("old")])
    cache.put_fingerprint("h", [make_scene("new")])
    scenes = cache.get_fingerprint("h")
    assert len(scenes) == 1
    assert scenes[0].id == "new"
    cache.close()


def test_entry_exactly_at_ttl_is_still_valid(tmp_path):
    """TTL is 30 days; an entry expires only once it is older than that."""
    now = [datetime(2026, 11, 4, tzinfo=timezone.utc)]
    cache = TitleCache(tmp_path / "c.sqlite3", clock=lambda: now[0])
    cache.put_fingerprint("h", [make_scene()])
    now[0] += timedelta(days=30)
    assert cache.get_fingerprint("h") is not None
    now[0] += timedelta(seconds=1)
    assert cache.get_fingerprint("h") is None
    cache.close()


def test_negative_result_also_expires(tmp_path):
    now = [datetime(2026, 11, 4, tzinfo=timezone.utc)]
    cache = TitleCache(tmp_path / "c.sqlite3", clock=lambda: now[0])
    cache.put_fingerprint("missing", [])
    now[0] += timedelta(days=31)
    assert cache.get_fingerprint("missing") is None
    cache.close()


def test_corrupt_json_is_treated_as_a_miss(tmp_path):
    """A damaged row must not raise; it is reported as 'no entry'."""
    path = tmp_path / "c.sqlite3"
    cache = TitleCache(path)
    cache.put_fingerprint("h", [make_scene()])
    cache.close()

    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE fingerprint_cache SET scenes = ? WHERE hash = ?", ("{not json", "h"))
    conn.commit()
    conn.close()

    cache = TitleCache(path)
    assert cache.get_fingerprint("h") is None
    cache.close()


def test_corrupt_timestamp_is_treated_as_a_miss(tmp_path):
    path = tmp_path / "c.sqlite3"
    cache = TitleCache(path)
    cache.put_fingerprint("h", [make_scene()])
    cache.close()

    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE fingerprint_cache SET fetched_at = ? WHERE hash = ?", ("not-a-date", "h"))
    conn.commit()
    conn.close()

    cache = TitleCache(path)
    assert cache.get_fingerprint("h") is None
    cache.close()


def test_stored_payload_is_json_list_of_scene_dicts(tmp_path):
    """The row keeps the full scene JSON, not only identifiers."""
    path = tmp_path / "c.sqlite3"
    cache = TitleCache(path)
    cache.put_fingerprint("h", [make_scene("s1")])
    cache.close()

    conn = sqlite3.connect(str(path))
    row = conn.execute(
        "SELECT scenes, fetched_at FROM fingerprint_cache WHERE hash = ?", ("h",)
    ).fetchone()
    conn.close()

    payload = json.loads(row[0])
    assert payload == [make_scene("s1").to_dict()]
    assert payload[0]["id"] == "s1"
    datetime.fromisoformat(row[1])


def test_parent_directory_is_created(tmp_path):
    path = tmp_path / "nested" / "deeper" / "c.sqlite3"
    cache = TitleCache(path)
    cache.put_fingerprint("h", [make_scene()])
    assert path.exists()
    assert cache.get_fingerprint("h")[0].id == "s1"
    cache.close()


def test_data_survives_reopening_the_cache(tmp_path):
    path = tmp_path / "c.sqlite3"
    cache = TitleCache(path)
    cache.put_fingerprint("h", [make_scene("s1")])
    cache.put_text_query("q", [make_scene("s2")])
    cache.close()

    cache = TitleCache(path)
    assert cache.get_fingerprint("h")[0].id == "s1"
    assert cache.get_text_query("q")[0].id == "s2"
    cache.close()


def test_close_twice_is_safe(tmp_path):
    cache = TitleCache(tmp_path / "c.sqlite3")
    cache.close()
    cache.close()


def test_naive_clock_is_treated_as_utc(tmp_path):
    """A clock without a timezone is interpreted as UTC."""
    now = [datetime(2026, 11, 4)]
    cache = TitleCache(tmp_path / "c.sqlite3", clock=lambda: now[0])
    cache.put_fingerprint("h", [make_scene()])
    now[0] += timedelta(days=31)
    assert cache.get_fingerprint("h") is None
    cache.close()


def test_accepts_str_path(tmp_path):
    cache = TitleCache(str(tmp_path / "c.sqlite3"))
    cache.put_fingerprint("h", [make_scene()])
    assert cache.get_fingerprint("h")[0].id == "s1"
    cache.close()
