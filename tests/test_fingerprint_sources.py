from video_renamer.core.fingerprint_sources import (
    FINGERPRINT_SOURCES,
    PRIMARY_SOURCE_NAME,
)
from video_renamer.scrapers.stashdb import (
    FANSDB_SOURCE,
    STASHDB_SOURCE,
    THEPORNDB_SOURCE,
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
