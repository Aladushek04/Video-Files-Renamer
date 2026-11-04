"""Тесты скрапера студий: пагинация, resume, merge и CLI-коды возврата."""
import json

import pytest

import scrape_studios
from scrape_studios import (
    Checkpoint,
    ScraperGapError,
    load_checkpoint,
    load_raw,
    main,
    merge_studios,
    normalize_key,
    run_scrape,
    save_checkpoint,
    save_raw,
)
from stashdb_client import (
    StashDBAuthError,
    StashDBTransientError,
    Studio,
)


class FakeClient:
    """Клиент без сети: отдаёт заранее заданные страницы или исключения."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def query_studios(self, page, per_page=100):
        self.calls.append((page, per_page))
        value = self.pages.get(page)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise AssertionError(f"unexpected page {page}")
        return value


def install_client(monkeypatch, fake):
    """Подменяет StashDBClient, чтобы main не ходил в сеть."""
    monkeypatch.setattr(
        scrape_studios, "StashDBClient", lambda *args, **kwargs: fake
    )


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# normalize / merge
# ---------------------------------------------------------------------------

def test_key_matches_cleaner():
    assert normalize_key("BrazzersExxtra") == "brazzersexxtra"
    assert normalize_key("Hussie Pass") == "hussiepass"


def test_merge_keeps_existing_and_reports_collisions():
    merged, collisions = merge_studios(
        {"brazzers": "Brazzers"}, ["Brazzers", "Brazzers!"])
    assert merged["brazzers"] == "Brazzers"
    assert collisions["brazzers"] == ["Brazzers!"]


def test_merge_adds_new_names_and_preserves_existing_key_spelling():
    merged, collisions = merge_studios(
        {"Brazzers Exxtra": "BrazzersExxtra"}, ["Hussie Pass"])
    assert merged["Brazzers Exxtra"] == "BrazzersExxtra"
    assert merged["hussiepass"] == "Hussie Pass"
    assert collisions == {}


def test_merge_treats_existing_name_as_same_value_not_collision():
    merged, collisions = merge_studios(
        {"brazzers": "Brazzers"}, ["Brazzers", "Brazzers"])
    assert merged == {"brazzers": "Brazzers"}
    assert collisions == {}


def test_merge_skips_blank_names():
    merged, collisions = merge_studios({}, ["", "  ", "Hussie Pass"])
    assert merged == {"hussiepass": "Hussie Pass"}
    assert collisions == {}


# ---------------------------------------------------------------------------
# run_scrape
# ---------------------------------------------------------------------------

def test_paginates_to_count():
    pages = {
        1: ([Studio("1", "A"), Studio("2", "B")], 3),
        2: ([Studio("3", "C")], 3),
    }

    def fetch(client, page, per_page):
        return pages[page]

    report = run_scrape(None, start_page=1, per_page=2, max_pages=None,
                        fetch=fetch)
    assert [s.name for s in report.studios] == ["A", "B", "C"]
    assert report.complete is True
    assert report.pages_fetched == 2
    assert report.next_page == 3


def test_empty_page_inside_count_raises():
    def fetch(client, page, per_page):
        return [], 10

    with pytest.raises(ScraperGapError):
        run_scrape(None, start_page=1, per_page=2, max_pages=None, fetch=fetch)


def test_empty_page_at_zero_count_completes():
    def fetch(client, page, per_page):
        return [], 0

    report = run_scrape(None, start_page=1, per_page=2, max_pages=None,
                        fetch=fetch)
    assert report.studios == ()
    assert report.complete is True
    assert report.pages_fetched == 1


def test_run_scrape_dedupes_ids_and_calls_on_page():
    pages = {
        1: ([Studio("1", "A")], 2),
        2: ([Studio("1", "A"), Studio("2", "B")], 2),
    }
    seen = []

    def fetch(client, page, per_page):
        return pages[page]

    def on_page(page, page_studios, count):
        seen.append((page, [s.id for s in page_studios], count))

    report = run_scrape(None, start_page=1, per_page=2, max_pages=None,
                        fetch=fetch, on_page=on_page)
    assert [s.id for s in report.studios] == ["1", "2"]
    assert report.complete is True
    assert seen == [(1, ["1"], 2), (2, ["1", "2"], 2)]


def test_max_pages_stops_incomplete():
    pages = {
        1: ([Studio("1", "A")], 3),
        2: ([Studio("2", "B")], 3),
    }

    def fetch(client, page, per_page):
        return pages[page]

    report = run_scrape(None, start_page=1, per_page=2, max_pages=1,
                        fetch=fetch)
    assert [s.id for s in report.studios] == ["1"]
    assert report.complete is False
    assert report.pages_fetched == 1
    assert report.next_page == 2


def test_resume_starts_from_initial_and_next_page():
    pages = {2: ([Studio("3", "C")], 3)}

    def fetch(client, page, per_page):
        return pages[page]

    report = run_scrape(
        None,
        start_page=2,
        per_page=2,
        max_pages=None,
        initial=[Studio("1", "A"), Studio("2", "B")],
        fetch=fetch,
    )
    assert [s.name for s in report.studios] == ["A", "B", "C"]
    assert report.complete is True
    assert report.pages_fetched == 1
    assert report.next_page == 3


# ---------------------------------------------------------------------------
# checkpoint / raw persistence
# ---------------------------------------------------------------------------

def test_checkpoint_roundtrip(tmp_path):
    p = str(tmp_path / "cp.json")
    save_checkpoint(p, Checkpoint(4, 100, 500, False))
    assert load_checkpoint(p) == Checkpoint(4, 100, 500, False)
    assert load_checkpoint(str(tmp_path / "no.json")) is None


def test_checkpoint_is_written_atomically_without_leftovers(tmp_path):
    sub = tmp_path / "nested"
    p = sub / "cp.json"
    save_checkpoint(str(p), Checkpoint(1, 100, 10, False))
    assert load_checkpoint(str(p)) == Checkpoint(1, 100, 10, False)
    assert [entry.name for entry in sub.iterdir()] == ["cp.json"]


def test_checkpoint_malformed_raises(tmp_path):
    p = tmp_path / "cp.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_checkpoint(str(p))


def test_raw_roundtrip_and_missing(tmp_path):
    p = str(tmp_path / "raw.json")
    studios = [
        Studio("1", "A", "p0", "Parent"),
        Studio("2", "B"),
    ]
    save_raw(p, studios)
    assert load_raw(p) == studios
    assert load_raw(str(tmp_path / "no.json")) == []


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def test_main_without_key_returns_2(tmp_path, monkeypatch):
    monkeypatch.delenv("STASHDB_API_KEY", raising=False)
    assert main(["--out", str(tmp_path / "studios.json")]) == 2


def test_main_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    fake = FakeClient({
        1: ([Studio("1", "A"), Studio("2", "B")], 3),
        2: ([Studio("3", "C")], 3),
    })
    install_client(monkeypatch, fake)

    out = tmp_path / "studios.json"
    cp = tmp_path / "cp.json"
    raw = tmp_path / "raw.json"
    code = main([
        "--out", str(out), "--checkpoint", str(cp), "--raw", str(raw),
        "--per-page", "2", "--delay", "0",
    ])
    assert code == 0
    assert fake.calls == [(1, 2), (2, 2)]
    assert not out.exists()
    assert not cp.exists()
    assert not raw.exists()
    assert not (tmp_path / "studios.json.bak").exists()
    assert not (tmp_path / "studios_collisions.json").exists()


def test_main_apply_writes_progress_and_merges_without_overwrite(
        tmp_path, monkeypatch):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    fake = FakeClient({
        1: ([Studio("1", "Brazzers"), Studio("2", "Brazzers!")], 3),
        2: ([Studio("3", "Hussie Pass")], 3),
    })
    install_client(monkeypatch, fake)

    out = tmp_path / "studios.json"
    out.write_text(json.dumps({"manual": "Manual Studio"}),
                   encoding="utf-8")
    cp = tmp_path / "cp.json"
    raw = tmp_path / "raw.json"
    code = main([
        "--out", str(out), "--checkpoint", str(cp), "--raw", str(raw),
        "--per-page", "2", "--delay", "0", "--apply",
    ])

    assert code == 0
    data = read_json(out)
    assert data["manual"] == "Manual Studio"
    assert data["brazzers"] == "Brazzers"
    assert data["hussiepass"] == "Hussie Pass"
    assert read_json(tmp_path / "studios_collisions.json") == {
        "brazzers": ["Brazzers!"],
    }
    assert len(load_raw(str(raw))) == 3
    checkpoint = load_checkpoint(str(cp))
    assert checkpoint == Checkpoint(2, 2, 3, True)
    assert read_json(tmp_path / "studios.json.bak") == {
        "manual": "Manual Studio",
    }


def test_main_transient_error_keeps_last_page_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    fake = FakeClient({
        1: ([Studio("1", "A"), Studio("2", "B")], 3),
        2: StashDBTransientError("boom"),
    })
    install_client(monkeypatch, fake)

    out = tmp_path / "studios.json"
    cp = tmp_path / "cp.json"
    raw = tmp_path / "raw.json"
    code = main([
        "--out", str(out), "--checkpoint", str(cp), "--raw", str(raw),
        "--per-page", "2", "--delay", "0", "--apply",
    ])

    assert code == 1
    assert [s.id for s in load_raw(str(raw))] == ["1", "2"]
    assert load_checkpoint(str(cp)) == Checkpoint(1, 2, 3, False)
    assert not out.exists()


def test_main_resumes_from_checkpoint_and_raw(tmp_path, monkeypatch):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    fake = FakeClient({2: ([Studio("3", "C")], 3)})
    install_client(monkeypatch, fake)

    out = tmp_path / "studios.json"
    cp = tmp_path / "cp.json"
    raw = tmp_path / "raw.json"
    save_raw(str(raw), [Studio("1", "A"), Studio("2", "B")])
    save_checkpoint(str(cp), Checkpoint(1, 2, 3, False))

    code = main([
        "--out", str(out), "--checkpoint", str(cp), "--raw", str(raw),
        "--per-page", "2", "--delay", "0", "--apply",
    ])

    assert code == 0
    assert fake.calls == [(2, 2)]
    assert sorted(read_json(out)) == ["a", "b", "c"]
    assert [s.id for s in load_raw(str(raw))] == ["1", "2", "3"]
    assert load_checkpoint(str(cp)) == Checkpoint(2, 2, 3, True)


def test_main_per_page_mismatch_requires_reset(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    fake = FakeClient({})
    install_client(monkeypatch, fake)

    out = tmp_path / "studios.json"
    cp = tmp_path / "cp.json"
    raw = tmp_path / "raw.json"
    save_checkpoint(str(cp), Checkpoint(1, 50, 500, False))

    code = main([
        "--out", str(out), "--checkpoint", str(cp), "--raw", str(raw),
        "--per-page", "100", "--delay", "0", "--apply",
    ])

    assert code == 2
    assert "--reset" in capsys.readouterr().err
    assert fake.calls == []
    assert not out.exists()
    assert not raw.exists()


def test_main_reset_with_apply_starts_at_page_one(tmp_path, monkeypatch):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    fake = FakeClient({1: ([Studio("1", "A")], 1)})
    install_client(monkeypatch, fake)

    out = tmp_path / "studios.json"
    cp = tmp_path / "cp.json"
    raw = tmp_path / "raw.json"
    save_raw(str(raw), [Studio("9", "Old")])
    save_checkpoint(str(cp), Checkpoint(5, 50, 10, False))

    code = main([
        "--out", str(out), "--checkpoint", str(cp), "--raw", str(raw),
        "--per-page", "2", "--delay", "0", "--apply", "--reset",
    ])

    assert code == 0
    assert fake.calls == [(1, 2)]
    assert [s.id for s in load_raw(str(raw))] == ["1"]
    assert load_checkpoint(str(cp)) == Checkpoint(1, 2, 1, True)
    assert read_json(out) == {"a": "A"}


def test_main_reset_without_apply_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    fake = FakeClient({1: ([Studio("1", "A")], 1)})
    install_client(monkeypatch, fake)

    out = tmp_path / "studios.json"
    cp = tmp_path / "cp.json"
    raw = tmp_path / "raw.json"
    save_raw(str(raw), [Studio("9", "Old")])
    save_checkpoint(str(cp), Checkpoint(5, 50, 10, False))

    code = main([
        "--out", str(out), "--checkpoint", str(cp), "--raw", str(raw),
        "--per-page", "2", "--delay", "0", "--reset",
    ])

    assert code == 0
    assert fake.calls == [(1, 2)]
    assert load_raw(str(raw)) == [Studio("9", "Old")]
    assert load_checkpoint(str(cp)) == Checkpoint(5, 50, 10, False)
    assert not out.exists()


def test_main_complete_checkpoint_skips_network_and_merges_raw(
        tmp_path, monkeypatch):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    fake = FakeClient({})
    install_client(monkeypatch, fake)

    out = tmp_path / "studios.json"
    cp = tmp_path / "cp.json"
    raw = tmp_path / "raw.json"
    save_raw(str(raw), [Studio("1", "A")])
    save_checkpoint(str(cp), Checkpoint(1, 2, 1, True))

    code = main([
        "--out", str(out), "--checkpoint", str(cp), "--raw", str(raw),
        "--per-page", "2", "--delay", "0", "--apply",
    ])

    assert code == 0
    assert fake.calls == []
    assert read_json(out) == {"a": "A"}


def test_main_auth_error_returns_2_without_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    fake = FakeClient({1: StashDBAuthError("bad key")})
    install_client(monkeypatch, fake)

    out = tmp_path / "studios.json"
    cp = tmp_path / "cp.json"
    raw = tmp_path / "raw.json"
    code = main([
        "--out", str(out), "--checkpoint", str(cp), "--raw", str(raw),
        "--per-page", "2", "--delay", "0", "--apply",
    ])

    assert code == 2
    assert not out.exists()
    assert not cp.exists()
    assert not raw.exists()


def test_main_malformed_checkpoint_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    fake = FakeClient({})
    install_client(monkeypatch, fake)

    cp = tmp_path / "cp.json"
    cp.write_text("{not json", encoding="utf-8")

    code = main([
        "--out", str(tmp_path / "studios.json"),
        "--checkpoint", str(cp),
        "--raw", str(tmp_path / "raw.json"),
        "--per-page", "2", "--delay", "0", "--apply",
    ])

    assert code == 2
    assert fake.calls == []
    assert "--reset" in capsys.readouterr().err


def test_main_gap_does_not_advance_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    fake = FakeClient({
        1: ([Studio("1", "A")], 3),
        2: ([], 3),
    })
    install_client(monkeypatch, fake)

    out = tmp_path / "studios.json"
    cp = tmp_path / "cp.json"
    raw = tmp_path / "raw.json"
    code = main([
        "--out", str(out), "--checkpoint", str(cp), "--raw", str(raw),
        "--per-page", "2", "--delay", "0", "--apply",
    ])

    assert code == 2
    assert [s.id for s in load_raw(str(raw))] == ["1"]
    assert load_checkpoint(str(cp)) == Checkpoint(1, 2, 3, False)
    assert not out.exists()


def test_main_max_pages_applies_partial_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    fake = FakeClient({1: ([Studio("1", "A")], 3)})
    install_client(monkeypatch, fake)

    out = tmp_path / "studios.json"
    cp = tmp_path / "cp.json"
    raw = tmp_path / "raw.json"
    code = main([
        "--out", str(out), "--checkpoint", str(cp), "--raw", str(raw),
        "--per-page", "2", "--delay", "0", "--max-pages", "1", "--apply",
    ])

    assert code == 0
    assert fake.calls == [(1, 2)]
    assert load_checkpoint(str(cp)) == Checkpoint(1, 2, 3, False)
    assert read_json(out) == {"a": "A"}


def test_main_reset_delete_error_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STASHDB_API_KEY", "key")
    fake = FakeClient({})
    install_client(monkeypatch, fake)

    cp = tmp_path / "cp.json"
    raw = tmp_path / "raw.json"
    save_checkpoint(str(cp), Checkpoint(1, 2, 3, False))
    save_raw(str(raw), [Studio("1", "A")])

    def locked(_path):
        raise PermissionError("locked")

    monkeypatch.setattr(scrape_studios.os, "remove", locked)

    code = main([
        "--out", str(tmp_path / "studios.json"),
        "--checkpoint", str(cp), "--raw", str(raw),
        "--per-page", "2", "--delay", "0", "--apply", "--reset",
    ])

    assert code == 2
    assert fake.calls == []
    assert "locked" in capsys.readouterr().err
