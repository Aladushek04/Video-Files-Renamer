from video_renamer.core.fingerprint_sources import (
    FINGERPRINT_SOURCES,
    PRIMARY_SOURCE_NAME,
    merge_scenes,
)
from video_renamer.core.stashdb_client import Scene, Studio
from video_renamer.scrapers.stashdb import (
    FANSDB_SOURCE,
    STASHDB_SOURCE,
    THEPORNDB_SOURCE,
)


def scene(scene_id, title, date=None, studio=None, source="StashDB"):
    return Scene(
        id=scene_id, title=title, release_date=date, date=None, code=None,
        duration=None,
        studio=Studio(id="st", name=studio) if studio else None,
        source=source,
    )


def test_fingerprint_sources_order_and_primary_name():
    names = [source.name for source in FINGERPRINT_SOURCES]
    assert names == ["StashDB", "ThePornDB", "FansDB"]
    assert PRIMARY_SOURCE_NAME == "StashDB"


def test_fingerprint_sources_endpoints_match_scrapers():
    by_name = {source.name: source for source in FINGERPRINT_SOURCES}
    assert by_name["StashDB"].endpoint == STASHDB_SOURCE.endpoint
    assert by_name["ThePornDB"].endpoint == THEPORNDB_SOURCE.endpoint
    assert by_name["FansDB"].endpoint == FANSDB_SOURCE.endpoint


def test_fingerprint_sources_key_env_names_match_scrapers():
    by_name = {source.name: source for source in FINGERPRINT_SOURCES}
    assert by_name["StashDB"].api_key_env == "STASHDB_API_KEY"
    assert by_name["ThePornDB"].api_key_env == "THE_PORNDB_API_KEY"
    assert by_name["FansDB"].api_key_env == "FANSDB_API_KEY"


def test_merge_scenes_collapses_duplicates_across_sources():
    stash = [scene("a", "Same Title", "2026-01-02", "Studio X")]
    porn = [scene("b", "Same  Title!", "2026-01-02", "studio x",
                  source="ThePornDB")]
    assert [s.id for s in merge_scenes([stash, porn])] == ["a"]


def test_merge_scenes_keeps_distinct_scenes():
    merged = merge_scenes([
        [scene("a", "One", "2026-01-02", "S")],
        [scene("b", "Two", "2026-01-02", "S", source="ThePornDB")],
    ])
    assert [s.id for s in merged] == ["a", "b"]


def test_merge_scenes_handles_missing_fields():
    merged = merge_scenes([[scene("a", None, source="FansDB")]])
    assert [s.id for s in merged] == ["a"]


def test_merge_scenes_never_collapses_scenes_without_title():
    merged = merge_scenes([
        [scene("a", None, "2026-01-02", "S")],
        [scene("b", "", "2026-01-02", "S", source="ThePornDB")],
    ])
    assert [s.id for s in merged] == ["a", "b"]


def test_merge_scenes_keeps_first_source_order():
    merged = merge_scenes([
        [scene("a", "One", "2026-01-02")],
        [scene("b", "Two", "2026-01-02", source="ThePornDB")],
        [scene("c", "Three", "2026-01-02", source="FansDB")],
    ])
    assert [s.id for s in merged] == ["a", "b", "c"]
