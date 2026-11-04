from clean_ph_names import (
    apply_renames,
    build_new_name,
    clean_filename,
    clean_stem,
    extract_date,
    extract_studio,
    load_studios,
    normalize_studio_key,
    prepare_stem,
    preview_directory,
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


def test_unicode_stem_untouched():
    assert strip_tech_junk("Olivia del Río 1080p") == "Olivia del Río"


def test_extract_date_yy_mm_dd():
    rest, d = extract_date("brazzersexxtra 26.10.03 ryan reid")
    assert d == "03.10.2026"


def test_extract_date_yyyy_mm_dd():
    rest, d = extract_date("Title 2023.10.14 rest")
    assert d == "14.10.2023"


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


def test_preview_idempotent_and_collision(tmp_path):
    (tmp_path / "a 1080p.mp4").touch()
    (tmp_path / "a XXX.mp4").touch()
    rows = preview_directory(str(tmp_path), {})
    assert rows[0][1] != rows[1][1]
    assert rows[0][1].endswith(".mp4")


def test_preview_idempotent_clean_name(tmp_path):
    (tmp_path / "Vrbangers - 22.12.2025 - Gabbie Carter - Title.mp4").touch()
    rows = preview_directory(str(tmp_path), {"vrbangers": "Vrbangers"})
    assert rows[0][0] == rows[0][1]


def test_preview_truncates_very_long(tmp_path):
    out = clean_filename("a" * 250 + ".mp4", {})
    assert len(out) <= 255 and out.endswith(".mp4")


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


def test_long_name_truncation_real():
    out = clean_filename("a" * 300 + " 1080p.mp4", {})
    assert len(out) <= 255 and out.endswith(".mp4")


def test_recursive_preview_keeps_subdir(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b 1080p.mp4").touch()
    rows = preview_directory(str(tmp_path), {}, recursive=True)
    assert len(rows) == 1
    old, new = rows[0]
    assert "sub" in old and "sub" in new
    assert new.endswith(".mp4")


def test_truncation_collision_stays_short(tmp_path):
    long1 = "a" * 240 + " 1080p.mp4"
    long2 = "a" * 240 + " XXX.mp4"
    (tmp_path / long1).touch()
    (tmp_path / long2).touch()
    rows = preview_directory(str(tmp_path), {})
    assert all(len(new) <= 255 and new.endswith(".mp4") for _, new in rows)
    assert rows[0][1] != rows[1][1]


def test_apply_overlapping_names_safe(tmp_path):
    (tmp_path / "x 1080p.mp4").touch()
    (tmp_path / "X.mp4").touch()
    rows = preview_directory(str(tmp_path), {})
    apply_renames(str(tmp_path), rows)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert "X.mp4" in names
    assert any(n.startswith("X (") and n.endswith(".mp4") for n in names)
