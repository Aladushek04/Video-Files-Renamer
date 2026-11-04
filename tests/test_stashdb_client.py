import io
import json
import urllib.error

import pytest

from stashdb_client import (
    Performer,
    Scene,
    StashDBAuthError,
    StashDBClient,
    StashDBQueryError,
    StashDBResponseError,
    StashDBTransientError,
    Studio,
    scene_from_node,
    studio_from_node,
)


def test_scene_from_node_reads_all_fields():
    scene = scene_from_node({
        "id": "s1", "title": "Poking Glory", "release_date": "2026-10-03",
        "date": "2026-10-03", "code": "GP1", "duration": 1200,
        "studio": {"id": "st1", "name": "BrazzersExxtra",
                    "parent": {"id": "st0", "name": "Brazzers"}},
        "performers": [{"as": "Ryan Reid",
                        "performer": {"id": "p1", "name": "Ryan Reid"}}],
    })
    assert scene.studio.name == "BrazzersExxtra"
    assert scene.studio.parent_name == "Brazzers"
    assert scene.performers[0].display_name == "Ryan Reid"
    assert Scene.from_dict(scene.to_dict()) == scene


def test_scene_from_node_allows_null_studio_and_alias():
    scene = scene_from_node({
        "id": "s2", "title": None, "release_date": None, "date": None,
        "code": None, "duration": None, "studio": None,
        "performers": [{"as": "Alias", "performer": {"id": "p2", "name": "Real"}}],
    })
    assert scene.studio is None
    assert scene.performers[0].display_name == "Alias"


def test_scene_from_node_reads_flat_fields():
    scene = scene_from_node({
        "id": "s3", "title": "Title", "release_date": "2026-10-03",
        "date": "2020-01-01", "code": "ABC-123", "duration": 1800,
        "studio": None, "performers": [],
    })
    assert scene.id == "s3"
    assert scene.title == "Title"
    assert scene.release_date == "2026-10-03"
    assert scene.date == "2020-01-01"
    assert scene.code == "ABC-123"
    assert scene.duration == 1800
    assert scene.studio is None
    assert scene.performers == ()


def test_scene_from_node_keeps_missing_keys_as_none():
    scene = scene_from_node({"id": "s4"})
    assert scene.id == "s4"
    assert scene.title is None
    assert scene.release_date is None
    assert scene.date is None
    assert scene.code is None
    assert scene.duration is None
    assert scene.studio is None
    assert scene.performers == ()


def test_studio_from_node_reads_parent():
    studio = studio_from_node({
        "id": "st1", "name": "BrazzersExxtra",
        "parent": {"id": "st0", "name": "Brazzers"},
    })
    assert studio == Studio("st1", "BrazzersExxtra", "st0", "Brazzers")


def test_studio_from_node_without_parent():
    studio = studio_from_node({"id": "st2", "name": "Solo Studio"})
    assert studio.parent_id is None
    assert studio.parent_name is None


def test_performer_display_name_prefers_alias():
    assert Performer("p1", "Real Name", "Alias").display_name == "Alias"
    assert Performer("p1", "Real Name").display_name == "Real Name"


def test_empty_alias_falls_back_to_performer_name():
    scene = scene_from_node({
        "id": "s5", "studio": None,
        "performers": [{"as": "", "performer": {"id": "p3", "name": "Real"}}],
    })
    assert scene.performers[0].alias is None
    assert scene.performers[0].display_name == "Real"


def test_scene_keeps_performer_order():
    scene = scene_from_node({
        "id": "s6", "studio": None,
        "performers": [
            {"as": "A", "performer": {"id": "p1", "name": "A"}},
            {"as": "B", "performer": {"id": "p2", "name": "B"}},
            {"as": "C", "performer": {"id": "p3", "name": "C"}},
        ],
    })
    assert [p.display_name for p in scene.performers] == ["A", "B", "C"]


def test_scene_to_dict_is_json_serializable():
    scene = scene_from_node({
        "id": "s1", "title": "Poking Glory", "release_date": "2026-10-03",
        "date": "2026-10-03", "code": "GP1", "duration": 1200,
        "studio": {"id": "st1", "name": "BrazzersExxtra",
                    "parent": {"id": "st0", "name": "Brazzers"}},
        "performers": [{"as": "Ryan Reid",
                        "performer": {"id": "p1", "name": "Ryan Reid"}}],
    })
    raw = json.dumps(scene.to_dict())
    assert Scene.from_dict(json.loads(raw)) == scene


def test_scene_to_dict_keeps_nulls_and_empty_lists():
    scene = scene_from_node({"id": "s7", "studio": None, "performers": []})
    data = scene.to_dict()
    assert data["studio"] is None
    assert data["performers"] == []
    assert data["title"] is None
    assert data["duration"] is None


def test_scene_roundtrip_without_studio_and_performers():
    scene = Scene("s8", "Title", "2026-10-03", None, None, 1200, None, ())
    assert Scene.from_dict(scene.to_dict()) == scene


def transport_returning(payload):
    def _transport(req, timeout):
        return json.dumps(payload).encode()

    return _transport


def scene_node(hashes):
    data = []
    for group in hashes:
        h = group[0]["hash"]
        data.append([
            {"id": f"s-{h}", "title": h, "release_date": None, "date": None,
             "code": None, "duration": None, "studio": None, "performers": []},
        ])
    return data


def test_batches_40_and_keeps_order():
    calls = []

    def _transport(req, timeout):
        body = json.loads(req.data)
        calls.append(body)
        groups = body["variables"]["fingerprints"]
        data = scene_node(groups)
        return json.dumps({"data": {"findScenesBySceneFingerprints": data}}).encode()

    client = StashDBClient("k", delay=0, transport=_transport)
    hashes = [f"h{i:02d}" for i in range(41)]
    result = client.find_scenes_by_hashes(hashes)
    assert len(result) == 41
    assert result[0][0].id == "s-h00"
    assert len(calls) == 2
    assert len(calls[0]["variables"]["fingerprints"]) == 40
    assert calls[0]["variables"]["fingerprints"][0] == [
        {"hash": "h00", "algorithm": "OSHASH"}
    ]
    assert [g[0]["hash"] for g in calls[1]["variables"]["fingerprints"]] == ["h40"]


def test_find_scenes_deduplicates_hashes_and_aligns_with_input():
    calls = []

    def _transport(req, timeout):
        body = json.loads(req.data)
        calls.append(body)
        groups = body["variables"]["fingerprints"]
        return json.dumps(
            {"data": {"findScenesBySceneFingerprints": scene_node(groups)}}
        ).encode()

    client = StashDBClient("k", delay=0, transport=_transport)
    result = client.find_scenes_by_hashes(["a", "b", "a"])
    assert len(calls) == 1
    assert len(calls[0]["variables"]["fingerprints"]) == 2
    assert len(result) == 3
    assert result[0][0].id == "s-a"
    assert result[2] == result[0]
    assert result[1][0].id == "s-b"


def test_find_scenes_with_no_hashes_makes_no_request():
    def _transport(req, timeout):
        raise AssertionError("no request expected")

    client = StashDBClient("k", delay=0, transport=_transport)
    assert client.find_scenes_by_hashes([]) == []


def test_find_scenes_pads_missing_groups_with_empty_lists():
    def _transport(req, timeout):
        return json.dumps({"data": {"findScenesBySceneFingerprints": [
            [{"id": "s-a", "title": "A", "studio": None, "performers": []}]
        ]}}).encode()

    client = StashDBClient("k", delay=0, transport=_transport)
    result = client.find_scenes_by_hashes(["a", "b"])
    assert [len(group) for group in result] == [1, 0]


def test_search_scenes_returns_parsed_models():
    transport = transport_returning({"data": {"searchScene": [
        {"id": "s1", "title": "T", "release_date": "2026-10-03",
         "code": "C1", "duration": 60,
         "studio": {"id": "st1", "name": "A", "parent": None},
         "performers": [{"as": "Alias", "performer": {"id": "p1", "name": "Real"}}]},
    ]}})
    client = StashDBClient("k", delay=0, transport=transport)
    scenes = client.search_scenes("term")
    assert scenes[0].studio == Studio("st1", "A")
    assert scenes[0].performers[0].display_name == "Alias"


def test_request_carries_api_key_header_endpoint_and_variables():
    captured = {}

    def _transport(req, timeout):
        captured["req"] = req
        captured["timeout"] = timeout
        return json.dumps({"data": {"searchScene": []}}).encode()

    client = StashDBClient("secret", delay=0, timeout=12.5, transport=_transport)
    assert client.search_scenes("term", limit=5) == []
    req = captured["req"]
    assert req.full_url == "https://stashdb.org/graphql"
    assert req.get_method() == "POST"
    assert req.get_header("Apikey") == "secret"
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("User-agent") == "filename-cleaner/1.0"
    assert captured["timeout"] == 12.5
    body = json.loads(req.data)
    assert body["variables"] == {"term": "term", "limit": 5}
    assert "searchScene" in body["query"]


def test_auth_error_is_fatal():
    attempts = []

    def _transport(req, timeout):
        attempts.append(1)
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"")
        )

    client = StashDBClient("bad", delay=0, transport=_transport)
    with pytest.raises(StashDBAuthError):
        client.search_scenes("test")
    assert len(attempts) == 1


def test_403_is_auth_error():
    def _transport(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {}, io.BytesIO(b"")
        )

    client = StashDBClient("bad", delay=0, transport=_transport)
    with pytest.raises(StashDBAuthError):
        client.search_scenes("test")


def test_graphql_errors_become_query_error():
    transport = transport_returning({"errors": [{"message": "bad query"}]})
    client = StashDBClient("k", delay=0, transport=transport)
    with pytest.raises(StashDBQueryError, match="bad query"):
        client.search_scenes("test")


def test_graphql_not_authorized_becomes_auth_error():
    transport = transport_returning({"errors": [{"message": "Not Authorized"}]})
    client = StashDBClient("k", delay=0, transport=transport)
    with pytest.raises(StashDBAuthError):
        client.search_scenes("test")


def test_invalid_json_becomes_response_error():
    def _transport(req, timeout):
        return b"<html>oops</html>"

    client = StashDBClient("k", delay=0, transport=_transport)
    with pytest.raises(StashDBResponseError):
        client.search_scenes("test")


def test_429_retries_then_rate_limit():
    attempts = []

    def _transport(req, timeout):
        attempts.append(1)
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many", {"Retry-After": "0"}, io.BytesIO(b"")
        )

    client = StashDBClient("k", delay=0, max_attempts=2,
                           sleep=lambda s: None, transport=_transport)
    with pytest.raises(StashDBTransientError):
        client.search_scenes("test")
    assert len(attempts) == 2


def test_429_honours_retry_after_and_caps_it():
    sleeps = []

    def _transport(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many", {"Retry-After": "9999"}, io.BytesIO(b"")
        )

    client = StashDBClient("k", delay=0, max_attempts=2,
                           sleep=sleeps.append, transport=_transport)
    with pytest.raises(StashDBTransientError):
        client.search_scenes("test")
    assert sleeps == [300]


def test_5xx_retries_with_backoff_then_transient():
    attempts = []
    sleeps = []

    def _transport(req, timeout):
        attempts.append(1)
        raise urllib.error.HTTPError(
            req.full_url, 503, "Unavailable", {}, io.BytesIO(b"")
        )

    client = StashDBClient("k", delay=0, max_attempts=3,
                           sleep=sleeps.append, transport=_transport)
    with pytest.raises(StashDBTransientError):
        client.search_scenes("test")
    assert len(attempts) == 3
    assert sleeps == [1, 3]


def test_url_error_retries_then_transient():
    attempts = []
    sleeps = []

    def _transport(req, timeout):
        attempts.append(1)
        raise urllib.error.URLError("boom")

    client = StashDBClient("k", delay=0, max_attempts=2,
                           sleep=sleeps.append, transport=_transport)
    with pytest.raises(StashDBTransientError):
        client.search_scenes("test")
    assert len(attempts) == 2
    assert sleeps == [1]


def test_delay_is_applied_before_request():
    sleeps = []

    def _transport(req, timeout):
        return json.dumps({"data": {"searchScene": []}}).encode()

    client = StashDBClient("k", delay=0.25, sleep=sleeps.append,
                           transport=_transport)
    client.search_scenes("term")
    assert sleeps == [0.25]


def test_query_studios_parses_count():
    transport = transport_returning({"data": {"queryStudios": {
        "count": 2, "studios": [{"id": "1", "name": "A"}]}}})
    client = StashDBClient("k", delay=0, transport=transport)
    studios, count = client.query_studios(1, 100)
    assert count == 2 and studios[0].name == "A"


def test_query_studios_sends_page_and_per_page():
    calls = []

    def _transport(req, timeout):
        calls.append(json.loads(req.data))
        return json.dumps({"data": {"queryStudios": {
            "count": 0, "studios": []}}}).encode()

    client = StashDBClient("k", delay=0, transport=_transport)
    assert client.query_studios(3, per_page=50) == ([], 0)
    body = calls[0]
    assert body["variables"] == {"page": 3, "perPage": 50}
    assert "queryStudios" in body["query"]
