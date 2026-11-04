from clean_ph_names import clean_stem
from stashdb_client import Performer, Scene, Studio
from title_matcher import (
    LocalNameInfo,
    build_heuristic_stem,
    build_scene_stem,
    format_scene_date,
    is_valid_final_stem,
    parse_local_name,
    sanitize_windows_stem,
)


def test_dates_and_windows_sanitization():
    assert format_scene_date("2026-10-03") == "03.10.2026"
    assert format_scene_date("2026-10") == "10.2026"
    assert format_scene_date("2026") == "2026"
    assert sanitize_windows_stem('A/B: C?') == "A B C"
    assert sanitize_windows_stem("CON") == "_CON"
    assert sanitize_windows_stem("Olivia del Río") == "Olivia del Río"
    assert is_valid_final_stem("Normal")
    assert not is_valid_final_stem("CON")
    assert not is_valid_final_stem("x" * 256)


def test_format_scene_date_rejects_other_shapes():
    assert format_scene_date(None) is None
    assert format_scene_date("") is None
    assert format_scene_date("03.10.2026") is None
    assert format_scene_date("2026-10-03T00:00:00") is None
    assert format_scene_date("2026-1-3") is None
    assert format_scene_date(" 2026-10-03") is None


def test_sanitize_windows_stem_forbidden_and_reserved():
    assert sanitize_windows_stem('a<b>c:d"e/f\\g|h?i*j') == "a b c d e f g h i j"
    assert sanitize_windows_stem("  trailing. . ") == "trailing"
    assert sanitize_windows_stem("con") == "_con"
    assert sanitize_windows_stem("COM1") == "_COM1"
    assert sanitize_windows_stem("LPT9.txt") == "_LPT9.txt"
    assert sanitize_windows_stem("CONSOLE") == "CONSOLE"
    assert sanitize_windows_stem("Río") == "Río"


def test_is_valid_final_stem_rules():
    assert is_valid_final_stem("A - B")
    assert not is_valid_final_stem("")
    assert not is_valid_final_stem(" leading")
    assert not is_valid_final_stem("trailing ")
    assert not is_valid_final_stem("trailing.")
    assert not is_valid_final_stem("a/b")
    assert not is_valid_final_stem("a:b")
    assert not is_valid_final_stem("com1")
    assert not is_valid_final_stem("LPT9")
    assert is_valid_final_stem("x" * 255)


def test_scene_stem_uses_official_fields():
    scene = Scene(id="s1", title="Poking Glory", release_date="2026-10-03",
                  date=None, code="GP1", duration=1,
                  studio=Studio("st", "BrazzersExxtra"),
                  performers=(Performer("p", "Ryan Reid"),))
    local = LocalNameInfo("old", "03.10.2026", "BrazzersExxtra", "old title")
    assert build_scene_stem(scene, local, {}) == \
        "BrazzersExxtra - 03.10.2026 - Ryan Reid - Poking Glory"


def test_scene_stem_keeps_official_casing():
    scene = Scene(id="s1", title="POKING glory (Remastered)", release_date="2026-10",
                  date=None, code=None, duration=None,
                  studio=Studio("st", "brazzersExxtra"),
                  performers=(Performer("p", "ryan REID", "ryan REID"),))
    local = LocalNameInfo("x", None, "LocalStudio", "local rest")
    assert build_scene_stem(scene, local, {}) == \
        "brazzersExxtra - 10.2026 - ryan REID - POKING glory (Remastered)"


def test_scene_stem_falls_back_to_date_field_local_studio_and_rest():
    scene = Scene(id="s1", title=None, release_date=None, date="2025-07",
                  code=None, duration=None, studio=None, performers=())
    local = LocalNameInfo("x", "01.02.2024", "LocalStudio", "local title")
    assert build_scene_stem(scene, local, {}) == \
        "LocalStudio - 07.2025 - local title"


def test_scene_stem_uses_local_date_when_scene_has_none():
    scene = Scene(id="s1", title="Title", release_date=None, date=None,
                  code=None, duration=None, studio=None, performers=())
    local = LocalNameInfo("x", "01.02.2024", None, "local rest")
    assert build_scene_stem(scene, local, {}) == "01.02.2024 - Title"


def test_scene_stem_sanitizes_forbidden_characters():
    scene = Scene(id="s1", title='A/B: "C"?', release_date="2026",
                  date=None, code=None, duration=None, studio=None,
                  performers=(Performer("p", "Ryan/Reid"),))
    local = LocalNameInfo("x", None, None, "")
    assert build_scene_stem(scene, local, {}) == "2026 - Ryan Reid - A B C"


def test_parse_local_name_extracts_parts():
    studios = {"brazzersexxtra": "BrazzersExxtra"}
    local = parse_local_name(
        "BrazzersExxtra.26.10.03.ryan.reid.poking.glory.mp4", studios
    )
    assert local == LocalNameInfo(
        stem="BrazzersExxtra.26.10.03.ryan.reid.poking.glory",
        date="03.10.2026",
        studio="BrazzersExxtra",
        rest="ryan reid poking glory",
    )


def test_parse_local_name_handles_clean_name():
    studios = {"vrbangers": "Vrbangers"}
    local = parse_local_name(
        "Vrbangers - 22.12.2025 - Gabbie Carter - Title.mp4", studios
    )
    assert local.date == "22.12.2025"
    assert local.studio == "Vrbangers"
    assert local.rest == "Gabbie Carter - Title"


def test_build_heuristic_stem_matches_clean_stem():
    studios = {"brazzersexxtra": "BrazzersExxtra"}
    local = parse_local_name(
        "brazzersexxtra.26.10.03.ryan.reid.poking.glory.mp4", studios
    )
    expected = clean_stem(local.stem, studios)
    assert expected == "BrazzersExxtra - 03.10.2026 - Ryan Reid - Poking Glory"
    assert build_heuristic_stem(local, studios) == expected
