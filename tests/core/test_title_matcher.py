import pytest

from video_renamer.core.clean_ph_names import clean_stem
from video_renamer.core.stashdb_client import Performer, Scene, Studio
from video_renamer.core.title_matcher import (
    LocalNameInfo,
    ScoredScene,
    build_heuristic_stem,
    build_scene_stem,
    format_scene_date,
    is_valid_final_stem,
    normalize_for_match,
    parse_local_name,
    pick_text_match,
    resolve_match,
    sanitize_windows_stem,
    score_scene,
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


# --- scoring, text selection and resolution ---------------------------------


def scene(i="s1", title="Poking Glory", performers=("Ryan Reid",),
          studio="BrazzersExxtra", date="2026-10-03"):
    return Scene(i, title, date, None, None, 1,
                 Studio("st", studio) if studio else None,
                 tuple(Performer(f"p{idx}", name)
                       for idx, name in enumerate(performers)))


def local(rest="Ryan Reid Poking Glory",
          stem="BrazzersExxtra 03.10.2026 Ryan Reid Poking Glory"):
    return LocalNameInfo(stem, "03.10.2026", "BrazzersExxtra", rest)


def test_normalize_strips_punctuation_and_diacritics():
    assert normalize_for_match("Café!!  Poking-Glory") == "cafe poking glory"
    assert normalize_for_match("Río") == "rio"
    assert normalize_for_match("  ") == ""


def test_score_scene_renormalizes_missing_performer_signal():
    scored = score_scene(scene(performers=()), local(rest="Poking Glory"), {})
    assert scored.performer_hit == 0.0
    assert scored.score == pytest.approx(
        (0.50 * scored.title_sim + 0.10 + 0.10) / 0.70
    )


def test_score_scene_drops_unavailable_date_signal():
    local_no_date = LocalNameInfo(
        "BrazzersExxtra Ryan Reid Poking Glory",
        None,
        "BrazzersExxtra",
        "Ryan Reid Poking Glory",
    )
    scored = score_scene(scene(), local_no_date, {})
    assert scored.date_hit == 0.0
    assert scored.score == pytest.approx(
        (0.50 * scored.title_sim + 0.20 + 0.10) / 0.80
    )


def test_score_scene_performer_hit_is_fraction_found():
    scored = score_scene(
        scene(performers=("Ryan Reid", "Someone Else")), local(), {}
    )
    assert scored.performer_hit == pytest.approx(0.5)


def test_pick_text_match_threshold_and_guard():
    low = ScoredScene(scene(), 0.59, 1.0, 1.0, 1.0, 1.0)
    assert pick_text_match([low]) == (None, False)

    unguarded = ScoredScene(scene(), 0.70, 0.70, 0.0, 1.0, 1.0)
    assert pick_text_match([unguarded]) == (None, False)

    guarded = ScoredScene(scene(), 0.70, 0.70, 0.5, 1.0, 1.0)
    assert pick_text_match([guarded]) == (guarded, False)


def test_pick_text_match_detects_ambiguity():
    best = ScoredScene(scene("s1"), 0.95, 1.0, 1.0, 1.0, 1.0)
    close = ScoredScene(scene("s2"), 0.91, 1.0, 1.0, 1.0, 1.0)
    assert pick_text_match([best, close]) == (best, True)

    clear = ScoredScene(scene("s2"), 0.70, 1.0, 1.0, 1.0, 1.0)
    assert pick_text_match([best, clear]) == (best, False)


def test_unique_fingerprint_is_approved():
    result = resolve_match(local(), {}, [scene()], [])
    assert result.source == "fingerprint"
    assert result.confidence == 1.0
    assert result.approved is True
    assert result.scene.id == "s1"
    assert result.new_stem.endswith("Poking Glory")


def test_unique_fingerprint_without_usable_name_falls_back():
    result = resolve_match(
        LocalNameInfo("random", None, None, ""), {},
        [scene(title=None, performers=(), studio=None, date=None)], []
    )
    assert result.source == "fingerprint"
    assert result.approved is False
    assert result.new_stem
    assert "heuristic fallback" in result.note


def test_unique_fingerprint_empty_scene_ignores_local_blocks():
    result = resolve_match(
        local(), {},
        [scene(title=None, performers=(), studio=None, date=None)], []
    )
    assert result.source == "fingerprint"
    assert result.approved is False
    assert "heuristic fallback" in result.note


def test_unique_fingerprint_with_invalid_stem_falls_back():
    local_name = local()
    result = resolve_match(local_name, {}, [scene(title="A" * 300)], [])
    assert result.source == "fingerprint"
    assert result.approved is False
    assert result.new_stem == build_heuristic_stem(local_name, {})
    assert "heuristic fallback" in result.note


def test_multiple_fingerprints_pick_best_and_list_ids():
    result = resolve_match(local(), {}, [scene("s2", "Totally Different"),
                                        scene("s1")], [])
    assert result.source == "fingerprint"
    assert result.confidence == 0.99
    assert result.scene.id == "s1"
    assert result.approved is False
    assert {candidate.id for candidate in result.candidates} == {"s1", "s2"}
    assert "s1" in result.note and "s2" in result.note


def test_low_text_score_falls_back_to_heuristic():
    result = resolve_match(LocalNameInfo("x", None, None, "totally different"),
                           {}, [], [scene(title="Another Movie")])
    assert result.source == "heuristic"
    assert result.approved is False
    assert result.scene is None
    assert result.confidence == 0.0
    assert result.candidates[0].id == "s1"
    assert "s1=" in result.note


def test_heuristic_fallback_lists_only_top_three_candidates():
    scenes = [scene(f"s{i}", f"Unrelated {i}") for i in range(4)]
    result = resolve_match(
        LocalNameInfo("x", None, None, "totally different"), {}, [], scenes
    )
    assert result.source == "heuristic"
    assert len(result.candidates) == 3


def test_phash_single_scene_is_never_approved():
    result = resolve_match(local(), {}, [], [], phash_scenes=[scene()])
    assert result.source == "phash"
    assert result.approved is False
    assert result.note.startswith("phash")
    assert "требует ревью" in result.note
    assert result.scene.id == "s1"


def test_phash_multiple_scenes_list_candidates():
    result = resolve_match(local(), {}, [], [],
                           phash_scenes=[scene("a"), scene("b")])
    assert [s.id for s in result.candidates] == ["a", "b"]
    assert result.approved is False
    assert result.note.startswith("phash")


def test_phash_skips_text_search():
    result = resolve_match(local(), {}, [], [scene("t")],
                           phash_scenes=[scene("p")])
    assert result.source == "phash"
    assert result.scene.id == "p"


def test_secondary_scene_without_title_is_not_approved():
    sparse = Scene("x", None, None, None, None, None, None, (),
                   source="ThePornDB")
    result = resolve_match(local(), {}, [sparse], [])
    assert result.approved is False


def test_text_match_is_not_auto_approved():
    result = resolve_match(local("Poking Glory"), {}, [],
                           [scene(), scene("s2", "Other")])
    assert result.source == "text"
    assert result.approved is False
    assert result.scene.id == "s1"
    assert result.note.startswith("text:")


def test_text_match_ignores_performer_names_in_title_comparison():
    local_name = LocalNameInfo(
        "Brazzers Adriana Chechik Fucking Fired Pov Anal",
        None,
        "Brazzers",
        "Adriana Chechik-Fucking Fired Pov Anal",
    )
    candidate = Scene(
        "s1", "Fucking Fired", "2020-02-22", None, "4411505", 1,
        Studio("st", "Brazzers Exxtra"),
        (Performer("p1", "Manuel Ferrara"),
         Performer("p2", "Adriana Chechik")),
    )
    scored = score_scene(candidate, local_name, {})
    assert scored.title_sim >= 0.70
    assert scored.score >= 0.60
    best, ambiguous = pick_text_match([scored])
    assert best is scored
    assert ambiguous is False


def test_text_match_rejects_same_performer_wrong_studio():
    local_name = LocalNameInfo(
        "260714 thevictorbliss Milf Sneaks into Hot Tub & Seduces Owner "
        "Taylor Vixxen 1080p",
        "14.07.2026",
        None,
        "thevictorbliss Milf Sneaks into Hot Tub & Seduces Owner Taylor Vixxen",
    )
    wrong = Scene(
        "s1", "Hot, blonde MILF Taylor Vixxen: Countdown to cum",
        "2025-09-24", None, "79159", 1, Studio("st", "40 Something Mag"),
        (Performer("p1", "Taylor Vixxen"),),
    )
    scored = score_scene(wrong, local_name, {})
    assert scored.score < 0.60
    assert pick_text_match([scored]) == (None, False)


def test_compact_date_and_performers_accept_correct_scene():
    local_name = parse_local_name(
        "260625 Rossavaxx Hardcore Fucked Rossa Vaxx With A Brazilian Boy "
        "4K Orig.mp4",
        {},
    )
    assert local_name.date == "25.06.2026"
    candidate = Scene(
        "s1", "HARDCORE fucked Rossa Vaxx with a BRAZILIAN BOY",
        "2026-06-25", None, "4908160", 1, Studio("st", "Rossa Vaxx"),
        (Performer("p1", "Air Thugger"), Performer("p2", "Rossa Vaxx")),
    )
    scored = score_scene(candidate, local_name, {})
    assert scored.score >= 0.60
    assert pick_text_match([scored])[0] is scored


def test_code_signal_matches_scene_code():
    local_name = LocalNameInfo("1135 Gia", None, None, "1135 Gia")
    candidate = Scene(
        "s1", "Tryouts with Gia", "2018-01-03", None, "1135", 1,
        Studio("st", "Net Video Girls"), (Performer("p1", "Gia"),),
    )
    scored = score_scene(candidate, local_name, {})
    assert scored.code_hit == 1.0


def test_ambiguous_text_match_is_flagged():
    result = resolve_match(local("Poking Glory"), {}, [],
                           [scene("s1"), scene("s2")])
    assert result.source == "text"
    assert result.approved is False
    assert result.note == "ambiguous"
