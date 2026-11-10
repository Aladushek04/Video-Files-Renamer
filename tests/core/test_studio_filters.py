"""Тесты фильтра мусорных имён студий из больших stash-box источников."""

from video_renamer.core.studio_filters import filter_studio_names, is_useful_studio_name


def test_keeps_real_studio_names():
    for name in [
        "Brazzers",
        "Hussie Pass",
        "SXT0229",
        "GP3528",
        "4based (network)",
        "XArt",
        "MILF Club",
        "Olivia Del Río",
        "1234 Studios",
    ]:
        assert is_useful_studio_name(name), name


def test_drops_placeholders_and_generated_ids():
    for name in [
        "",
        "   ",
        "A",
        "AB",
        "XXX",
        "aaaa",
        "N/A",
        "Unknown",
        "No Name",
        "123456",
        "000000",
        "000000h (ManyVids)",
        "00000000jhvfl (dirtyFans)",
        "00000000000000 (ManyVids)",
        "+AddFriends (network)",
        "2024Studio",
    ]:
        assert not is_useful_studio_name(name), name


def test_filter_returns_kept_names_and_dropped_count():
    kept, dropped = filter_studio_names(
        [
            "Brazzers",
            "000000h (ManyVids)",
            "4based (network)",
            "+AddFriends (network)",
            "Hussie Pass",
        ]
    )
    assert kept == ["Brazzers", "4based (network)", "Hussie Pass"]
    assert dropped == 2
