"""Тесты скрапера имён исполнителей: пагинация, resume, запись и коды."""

import json

import pytest

from video_renamer.core.stashdb_client import StashDBTransientError
from video_renamer.scrapers import performers as scrape_performers


class FakeClient:
    """Клиент без сети: отдаёт страницы `queryPerformers` по номеру."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def execute(self, query, variables=None):
        page = (variables or {})["input"]["page"]
        self.calls.append(page)
        value = self.pages.get(page)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise AssertionError(f"unexpected page {page}")
        return {"queryPerformers": value}


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("STASHDB_API_KEY", "test-key")


@pytest.fixture
def client(monkeypatch):
    holder = {}

    def install(fake):
        holder["fake"] = fake
        monkeypatch.setattr(scrape_performers, "StashDBClient", lambda *args, **kwargs: fake)

    return install


def performer(identifier, name, aliases=None, **extra):
    payload = {
        "id": identifier,
        "name": name,
        "aliases": aliases or [],
        "gender": "FEMALE",
        "disambiguation": None,
        "scene_count": 1,
        "deleted": False,
        "merged_into_id": None,
        "updated": "2026-01-01T00:00:00Z",
    }
    payload.update(extra)
    return payload


def paths(tmp_path):
    return {
        "out": str(tmp_path / "performers.json"),
        "index": str(tmp_path / "performers_index.json"),
        "raw": str(tmp_path / "performers_raw.jsonl"),
        "checkpoint": str(tmp_path / "performers_checkpoint.json"),
    }


def argv(files, *extra):
    return [
        "--out",
        files["out"],
        "--index",
        files["index"],
        "--raw",
        files["raw"],
        "--checkpoint",
        files["checkpoint"],
        "--delay",
        "0",
        *extra,
    ]


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def test_dry_run_writes_nothing(client, tmp_path):
    files = paths(tmp_path)
    client(
        FakeClient(
            {
                1: {
                    "count": 3,
                    "performers": [
                        performer("1", "Veronica Leal"),
                        performer("2", "Zoe Bell"),
                    ],
                },
                2: {"count": 3, "performers": [performer("3", "Anna One")]},
            }
        )
    )

    assert scrape_performers.main(argv(files)) == 0
    assert not (tmp_path / "performers.json").exists()
    assert not (tmp_path / "performers_index.json").exists()
    assert not (tmp_path / "performers_raw.jsonl").exists()


def test_apply_writes_database_index_and_checkpoint(client, tmp_path):
    files = paths(tmp_path)
    client(
        FakeClient(
            {
                1: {
                    "count": 2,
                    "performers": [
                        performer("1", "Veronica Leal", ["Veronika Leal"]),
                        performer("2", "Single"),
                    ],
                }
            }
        )
    )

    assert scrape_performers.main(argv(files, "--apply")) == 0

    database = read_json(files["out"])
    assert database["source"] == "stashdb"
    assert database["count"] == 2
    assert [row["name"] for row in database["performers"]] == ["Single", "Veronica Leal"]

    index = read_json(files["index"])
    assert index["veronica leal"] == "Veronica Leal"
    assert index["veronika leal"] == "Veronica Leal"
    assert "single" not in index

    checkpoint = read_json(files["checkpoint"])
    assert checkpoint["complete"] is True
    assert checkpoint["page"] == 1

    raw_lines = (tmp_path / "performers_raw.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 1


def test_apply_creates_backup_of_existing_files(client, tmp_path):
    files = paths(tmp_path)
    (tmp_path / "performers.json").write_text("{}", encoding="utf-8")
    (tmp_path / "performers_index.json").write_text("{}", encoding="utf-8")
    client(FakeClient({1: {"count": 1, "performers": [performer("1", "Anna One")]}}))

    assert scrape_performers.main(argv(files, "--apply")) == 0
    assert (tmp_path / "performers.json.bak").exists()
    assert (tmp_path / "performers_index.json.bak").exists()


def test_empty_page_before_count_is_fatal(client, tmp_path):
    files = paths(tmp_path)
    client(
        FakeClient(
            {
                1: {"count": 3, "performers": [performer("1", "Anna One")]},
                2: {"count": 3, "performers": []},
            }
        )
    )

    assert scrape_performers.main(argv(files, "--apply")) == 2


def test_allow_gaps_accepts_short_scrape(client, tmp_path):
    files = paths(tmp_path)
    client(
        FakeClient(
            {
                1: {"count": 3, "performers": [performer("1", "Anna One")]},
                2: {"count": 3, "performers": []},
            }
        )
    )

    assert scrape_performers.main(argv(files, "--allow-gaps")) == 0


def test_transient_error_returns_1(client, tmp_path):
    files = paths(tmp_path)
    client(FakeClient({1: StashDBTransientError("network down")}))

    assert scrape_performers.main(argv(files)) == 1


def test_bad_payload_returns_2(client, tmp_path):
    files = paths(tmp_path)
    client(FakeClient({1: {"count": 1}}))

    assert scrape_performers.main(argv(files)) == 2
    assert not (tmp_path / "performers.json").exists()


def test_resume_continues_after_raw_page(client, tmp_path):
    files = paths(tmp_path)
    raw = tmp_path / "performers_raw.jsonl"
    raw.write_text(
        json.dumps(
            {
                "page": 1,
                "count": 2,
                "performers": [performer("1", "Anna One")],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = {
        "source": "stashdb",
        "page": 1,
        "per_page": 1000,
        "count": 2,
        "complete": False,
    }
    (tmp_path / "performers_checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    fake = FakeClient({2: {"count": 2, "performers": [performer("2", "Anna Two")]}})
    client(fake)

    assert scrape_performers.main(argv(files)) == 0
    assert fake.calls == [2]


def test_complete_checkpoint_starts_a_fresh_refresh(client, tmp_path):
    files = paths(tmp_path)
    raw = tmp_path / "performers_raw.jsonl"
    raw.write_text(
        json.dumps({"page": 1, "count": 1, "performers": [performer("1", "Old Name")]}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "performers_checkpoint.json").write_text(
        json.dumps(
            {
                "source": "stashdb",
                "page": 1,
                "per_page": 1000,
                "count": 1,
                "complete": True,
            }
        ),
        encoding="utf-8",
    )
    fake = FakeClient({1: {"count": 1, "performers": [performer("2", "New Name")]}})
    client(fake)

    assert scrape_performers.main(argv(files, "--apply")) == 0
    assert fake.calls == [1]
    database = read_json(files["out"])
    assert [row["name"] for row in database["performers"]] == ["New Name"]
    assert len(raw.read_text(encoding="utf-8").splitlines()) == 1


def test_checkpoint_page_size_mismatch_is_fatal(client, tmp_path):
    files = paths(tmp_path)
    (tmp_path / "performers_checkpoint.json").write_text(
        json.dumps({"source": "stashdb", "page": 1, "per_page": 100}), encoding="utf-8"
    )
    client(FakeClient({}))

    assert scrape_performers.main(argv(files, "--per-page", "1000")) == 2


def test_max_pages_stops_incomplete(client, tmp_path):
    files = paths(tmp_path)
    fake = FakeClient(
        {
            1: {"count": 2, "performers": [performer("1", "Anna One")]},
            2: {"count": 2, "performers": [performer("2", "Anna Two")]},
        }
    )
    client(fake)

    assert scrape_performers.main(argv(files, "--max-pages", "1", "--apply")) == 0
    assert fake.calls == [1]
    assert not (tmp_path / "performers.json").exists()
