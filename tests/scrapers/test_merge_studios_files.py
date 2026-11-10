"""Тесты слияния нескольких JSON-справочников студий."""
import json

from video_renamer.scrapers.merge_studios import merge_studio_dicts, merge_studio_files


def test_merge_studio_dicts_keeps_first_seen_and_reports_collisions():
    merged, collisions = merge_studio_dicts([
        {"brazzers": "Brazzers", "manual": "Manual Studio"},
        {"brazzers": "BRAZZERS", "hussiepass": "Hussie Pass"},
    ])
    assert merged["brazzers"] == "Brazzers"
    assert merged["manual"] == "Manual Studio"
    assert merged["hussiepass"] == "Hussie Pass"
    assert collisions["brazzers"] == ["BRAZZERS"]


def test_merge_studio_files_apply_writes_output(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    out = tmp_path / "studios.json"
    first.write_text(json.dumps({"brazzers": "Brazzers"}), encoding="utf-8")
    second.write_text(json.dumps({
        "brazzers": "BRAZZERS",
        "hussiepass": "Hussie Pass",
    }), encoding="utf-8")

    report = merge_studio_files(
        [str(first), str(second)], str(out), apply=True
    )
    assert report["merged_keys"] == 2
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "brazzers": "Brazzers",
        "hussiepass": "Hussie Pass",
    }
    assert json.loads(
        (tmp_path / "studios_collisions.json").read_text(encoding="utf-8")
    )["brazzers"] == ["BRAZZERS"]


def test_merge_studio_files_dry_run_does_not_write(tmp_path):
    first = tmp_path / "first.json"
    first.write_text(json.dumps({"brazzers": "Brazzers"}), encoding="utf-8")
    out = tmp_path / "studios.json"

    report = merge_studio_files([str(first)], str(out), apply=False)
    assert report["merged_keys"] == 1
    assert not out.exists()
