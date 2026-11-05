from video_renamer.core.clean_ph_names import (
    clean_stem,
    extract_date,
    extract_studio,
    normalize_studio_key,
    prepare_stem,
    split_actors_title,
    split_camel,
    split_name,
    strip_tech_junk,
)


def test_split_name_keeps_mp4_suffix():
    assert split_name("foo.1080p.mp4") == ("foo.1080p", ".mp4")


def test_split_name_upper_mp4_kept():
    assert split_name("foo.MP4") == ("foo", ".MP4")


def test_strip_tech_junk_cuts_quality_and_dubs():
    assert strip_tech_junk("AdultTime.Ebony.Scene.4.XXX.2160p(1)") == "AdultTime Ebony Scene 4"


def test_strip_tech_junk_keeps_remastered():
    assert "Remastered" in strip_tech_junk("Title (Remastered) 1080p")


def test_strip_tech_junk_after_underscore_separator():
    assert strip_tech_junk("Title_1080p") == "Title"


def test_unicode_stem_untouched():
    assert strip_tech_junk("Olivia del Río 1080p") == "Olivia del Río"


def test_extract_date_yy_mm_dd():
    rest, d = extract_date("brazzersexxtra 26.10.03 ryan reid")
    assert d == "03.10.2026"


def test_extract_date_yyyy_mm_dd():
    rest, d = extract_date("Title 2023.10.14 rest")
    assert d == "14.10.2023"


def test_extract_date_compact_yymmdd():
    rest, d = extract_date("260625 Rossavaxx Hardcore")
    assert d == "25.06.2026"
    assert rest == "Rossavaxx Hardcore"


def test_extract_date_ignores_invalid_compact():
    rest, d = extract_date("999999 Title")
    assert d is None
    assert rest == "999999 Title"


def test_extract_studio_from_dict():
    studios = {"brazzersexxtra": "BrazzersExxtra"}
    studio, rest = extract_studio("brazzersexxtra ryan reid", studios)
    assert studio == "BrazzersExxtra"


def test_multitoken_studio_prefix():
    studios = {"hussiepass": "HussiePass", "brazzersexxtra": "BrazzersExxtra"}
    studio, rest = extract_studio("Hussie Pass Anastasia Shades", studios)
    assert studio == "HussiePass" and rest == "Anastasia Shades"
    studio, rest = extract_studio("Brazzers Exxtra Ryan Reid", studios)
    assert studio == "BrazzersExxtra" and rest == "Ryan Reid"


def test_extract_studio_ignores_short_keys():
    studio, rest = extract_studio("abc Ryan Reid", {"abc": "Abc"})
    assert studio is None and rest == "abc Ryan Reid"


def test_normalize_studio_key_strips_separators():
    assert normalize_studio_key("Hussie Pass.26_x-2") == "hussiepass26x2"


def test_prepare_stem_returns_parts():
    studios = {"brazzersexxtra": "BrazzersExxtra"}
    rest, date, studio = prepare_stem(
        "brazzers-exxtra 26.10.03 ryan reid poking glory", studios
    )
    assert rest == "ryan reid poking glory"
    assert date == "03.10.2026"
    assert studio == "BrazzersExxtra"


def test_split_camel_joined_names():
    assert split_camel("PhillipsJenna") == "Phillips Jenna"


def test_split_actors_title_comma():
    actors, title = split_actors_title("Lily Phillips, Martina Smeraldi DOUBLE PENETRATION BANG")
    assert actors == ["Lily Phillips", "Martina Smeraldi"]


def test_clean_stem_vrbangers_example():
    studios = {"vrbangers": "Vrbangers"}
    out = clean_stem("vrbangers 22.12.2025 Gabbie Carter, Karma Rx Sorority Hookup Party Never Ends (Remastered)", studios)
    assert out == "Vrbangers - 22.12.2025 - Gabbie Carter, Karma Rx - Sorority Hookup Party Never Ends (Remastered)"


def test_eporner_prefix_stripped():
    studios = {}
    out = clean_stem("Eporner Com [4Jfvjnyhvqb] Adriana Lorenzi", studios)
    assert "Eporner" not in out and "Adriana Lorenzi" in out


def test_studio_before_camel_split():
    studios = {"hussiepass": "HussiePass"}
    out = clean_stem("HussiePass.26.07.31.Anastasia.Shades.1080p", studios)
    assert out == "HussiePass - 31.07.2026 - Anastasia Shades"


def test_lowercase_actors_detected():
    studios = {"brazzersexxtra": "BrazzersExxtra"}
    out = clean_stem("brazzersexxtra.26.10.03.ryan.reid.poking.glory", studios)
    assert out == "BrazzersExxtra - 03.10.2026 - Ryan Reid - Poking Glory"


def test_dash_date_yyyy_mm_dd():
    rest, d = extract_date("Cuckold Sessions 2019-12-01 Brittany Andrews")
    assert d == "01.12.2019"


def test_no_empty_parens_left():
    out = strip_tech_junk("Title (2160P)(1)")
    assert "(" not in out and ")" not in out


def test_part_roman_numerals_kept():
    assert "Part" in strip_tech_junk("Johnny Cj (Part Ii)(1)")


def test_particle_three_word_name():
    actors, title = split_actors_title("Olivia Del Rio 02")
    assert actors == ["Olivia Del Rio"]


def test_hyphenated_studio_normalized():
    studios = {"brazzersexxtra": "BrazzersExxtra"}
    out = clean_stem("brazzers-exxtra 26.10.03 ryan reid poking glory", studios)
    assert out == "BrazzersExxtra - 03.10.2026 - Ryan Reid - Poking Glory"
