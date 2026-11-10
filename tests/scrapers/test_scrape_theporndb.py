"""Тесты ThePornDB: список студий через findStudio по именам."""
import json

from video_renamer.scrapers.theporndb import (
    build_lookup_query,
    load_seed_names,
    main,
    parse_lookup_response,
    run_lookup,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, query, variables):
        self.calls.append((query, variables))
        return self.responses.pop(0)


def test_build_lookup_query_uses_aliases_and_variables():
    query, variables = build_lookup_query(["Brazzers", "Jules Jordan"])
    assert "s0: findStudio(name: $n0)" in query
    assert "s1: findStudio(name: $n1)" in query
    assert variables == {"n0": "Brazzers", "n1": "Jules Jordan"}


def test_parse_lookup_response_adds_parent_and_children():
    studios = parse_lookup_response({
        "s0": {
            "id": "1", "name": "Brazzers",
            "parent": {"id": "0", "name": "Brazzers Network"},
            "child_studios": [{"id": "2", "name": "Brazzers Exxtra"}],
        },
        "s1": None,
    }, 2)
    assert [studio.name for studio in studios] == [
        "Brazzers", "Brazzers Network", "Brazzers Exxtra"
    ]


def test_run_lookup_batches_names():
    fake = FakeClient([
        {"s0": {"id": "1", "name": "Alpha"}, "s1": {"id": "2", "name": "Beta"}},
        {"s0": {"id": "3", "name": "Gamma"}},
    ])
    report = run_lookup(fake, ["Alpha", "Beta", "Gamma"], batch_size=2)
    assert report.complete is True
    assert report.batches_fetched == 2
    assert [studio.name for studio in report.studios] == [
        "Alpha", "Beta", "Gamma"
    ]
    assert len(fake.calls) == 2


def test_load_seed_names_from_dict_and_list(tmp_path):
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(json.dumps({"brazzers": "Brazzers"}), encoding="utf-8")
    second.write_text(json.dumps(["Brazzers", "Hussie Pass"]), encoding="utf-8")
    assert load_seed_names([str(first), str(second)]) == [
        "Brazzers", "Hussie Pass"
    ]


def test_main_apply_writes_values_through_find_studio(tmp_path, monkeypatch):
    from video_renamer.scrapers import theporndb as module

    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"brazzers": "Brazzers"}), encoding="utf-8")
    fake = FakeClient([{"s0": {"id": "1", "name": "Brazzers"}}])

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def execute(self, query, variables):
            return fake.execute(query, variables)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("THE_PORNDB_API_KEY", "key")
    monkeypatch.setattr(module, "StashDBClient", Client)

    out = tmp_path / "studios_theporndb.json"
    checkpoint = tmp_path / "cp.json"
    raw = tmp_path / "raw.json"
    code = main([
        "--seed", str(seed),
        "--out", str(out),
        "--checkpoint", str(checkpoint),
        "--raw", str(raw),
        "--batch-size", "1", "--delay", "0", "--apply",
    ])
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "brazzers": "Brazzers"
    }
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["complete"] is True


def test_main_without_key_returns_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("THE_PORNDB_API_KEY", raising=False)
    assert main(["--out", str(tmp_path / "out.json")]) == 2
