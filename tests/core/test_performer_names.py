"""Тесты индекса имён исполнителей: сборка, поиск и вырезание имён."""

import json

import pytest

from video_renamer.core.performer_names import PerformerIndex, build_index


def performer(name, *aliases, **extra):
    payload = {
        "id": extra.pop("id", name.lower().replace(" ", "-")),
        "name": name,
        "aliases": list(aliases),
    }
    payload.update(extra)
    return payload


def make_index(*performers):
    return PerformerIndex.from_index(build_index(performers))


def test_build_index_keeps_multi_token_names_and_drops_single_token():
    index = build_index(
        [
            performer("Veronica Leal", "Veronika Leal"),
            performer("Zoe"),
            performer("Porn"),
        ]
    )
    assert index["veronica leal"] == "Veronica Leal"
    assert index["veronika leal"] == "Veronica Leal"
    assert "zoe" not in index
    assert "porn" not in index


def test_build_index_drops_ambiguous_multi_token_alias():
    index = build_index(
        [performer("Anna One", "Shared Alias"), performer("Anna Two", "Shared Alias")]
    )
    assert "shared alias" not in index
    assert index["anna one"] == "Anna One"
    assert index["anna two"] == "Anna Two"


def test_build_index_drops_numeric_and_too_long_keys():
    index = build_index(
        [
            performer("0005", "0006"),
            performer("One Two Three Four Five Six", "a b c d e f"),
        ]
    )
    assert index == {}


def test_build_index_skips_deleted_and_merged():
    index = build_index(
        [
            performer("Deleted Person", deleted=True),
            performer("Merged Person", merged_into_id="other-id"),
            performer("Live Person"),
        ]
    )
    assert index == {"live person": "Live Person"}


def test_find_names_prefers_longest_and_skips_overlaps():
    index = make_index(performer("Veronica Leal"), performer("Veronica Leal Smith"))
    matches = index.find_names("veronica leal smith scene")
    assert [match.name for match in matches] == ["Veronica Leal Smith"]
    assert (matches[0].start, matches[0].end) == (0, 3)


def test_find_names_works_across_hyphenated_token():
    index = make_index(performer("Veronica Leal"))
    matches = index.find_names("veronica-leal i tested")
    assert [match.name for match in matches] == ["Veronica Leal"]


def test_find_names_handles_apostrophe_inside_token():
    index = make_index(performer("Conan O'Neil"))
    matches = index.find_names("conan o'neil scene")
    assert [match.name for match in matches] == ["Conan O'Neil"]
    assert (matches[0].start, matches[0].end) == (0, 2)


def test_strip_names_returns_rest_and_canonical_names():
    index = make_index(performer("Veronica Leal"))
    rest, names = index.strip_names("xxxx veronica leal i tested some new dicks")
    assert rest == "xxxx i tested some new dicks"
    assert names == ("Veronica Leal",)


def test_strip_names_without_matches_keeps_text():
    index = make_index(performer("Veronica Leal"))
    assert index.strip_names("unknown title") == ("unknown title", ())


def test_load_rejects_non_object(tmp_path):
    path = tmp_path / "index.json"
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with pytest.raises(TypeError):
        PerformerIndex.load(str(path))


def test_load_if_exists_returns_none_for_missing_file(tmp_path):
    assert PerformerIndex.load_if_exists(str(tmp_path / "missing.json")) is None
