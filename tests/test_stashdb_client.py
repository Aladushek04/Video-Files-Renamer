import json

from stashdb_client import (
    Performer,
    Scene,
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
