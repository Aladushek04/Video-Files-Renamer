"""Тесты загрузчика `.env`: парсинг, приоритет окружения, отсутствие файла."""

import os

from video_renamer.core.env_file import load_env_file


def test_loads_plain_export_quotes_and_comments(tmp_path, monkeypatch):
    monkeypatch.delenv("A_PLAIN", raising=False)
    monkeypatch.delenv("B_EXPORT", raising=False)
    monkeypatch.delenv("C_DOUBLE", raising=False)
    monkeypatch.delenv("D_SINGLE", raising=False)
    monkeypatch.delenv("E_COMMENT", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "A_PLAIN=one\n"
        "export B_EXPORT=two\n"
        'C_DOUBLE="three four"\n'
        "D_SINGLE='five six'\n"
        "E_COMMENT=seven # trailing comment\n"
        "\n",
        encoding="utf-8",
    )
    loaded = load_env_file(env)
    assert loaded["A_PLAIN"] == "one"
    assert os.environ["A_PLAIN"] == "one"
    assert os.environ["B_EXPORT"] == "two"
    assert os.environ["C_DOUBLE"] == "three four"
    assert os.environ["D_SINGLE"] == "five six"
    assert os.environ["E_COMMENT"] == "seven"


def test_existing_environment_wins_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEP_ME", "from-process")
    env = tmp_path / ".env"
    env.write_text("KEEP_ME=from-file\n", encoding="utf-8")
    load_env_file(env)
    assert os.environ["KEEP_ME"] == "from-process"


def test_override_true_replaces_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OVERRIDE_ME", "old")
    env = tmp_path / ".env"
    env.write_text("OVERRIDE_ME=new\n", encoding="utf-8")
    load_env_file(env, override=True)
    assert os.environ["OVERRIDE_ME"] == "new"


def test_missing_file_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    assert load_env_file(tmp_path / "nope.env") == {}
    assert "NO_SUCH_KEY" not in os.environ


def test_default_searches_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("FROM_CWD=yes\n", encoding="utf-8")
    monkeypatch.delenv("FROM_CWD", raising=False)
    load_env_file()
    assert os.environ["FROM_CWD"] == "yes"


def test_invalid_lines_are_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOD", raising=False)
    env = tmp_path / ".env"
    env.write_text("not a pair\nBAD-KEY=value\nGOOD=ok\n", encoding="utf-8")
    loaded = load_env_file(env)
    assert loaded == {"GOOD": "ok"}
    assert os.environ["GOOD"] == "ok"


def test_scraper_main_reads_api_key_from_dotenv(tmp_path, monkeypatch):
    """CLI без STASHDB_API_KEY в процессе берёт ключ из .env."""
    from video_renamer.core.stashdb_client import Studio
    from video_renamer.scrapers import stashdb as scrape_studios

    seen = {}

    class FakeClient:
        def __init__(self, api_key, **kwargs):
            seen["api_key"] = api_key

        def query_studios(self, page, per_page=100):
            return [Studio("1", "LoadedFromEnv")], 1

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STASHDB_API_KEY", raising=False)
    (tmp_path / ".env").write_text("STASHDB_API_KEY=abc123\n", encoding="utf-8")
    monkeypatch.setattr(scrape_studios, "StashDBClient", FakeClient)

    code = scrape_studios.main(
        [
            "--out",
            str(tmp_path / "studios.json"),
            "--checkpoint",
            str(tmp_path / "checkpoint.json"),
            "--raw",
            str(tmp_path / "raw.json"),
            "--apply",
            "--max-pages",
            "1",
        ]
    )
    assert code == 0
    assert seen["api_key"] == "abc123"
    assert "LoadedFromEnv" in (tmp_path / "studios.json").read_text(encoding="utf-8")


import pytest

from video_renamer.core.env_file import reset_tracking


@pytest.fixture(autouse=True)
def _fresh_tracking():
    reset_tracking()
    yield
    reset_tracking()


def test_reload_updates_value_loaded_by_app(tmp_path, monkeypatch):
    monkeypatch.delenv("RELOAD_ME", raising=False)
    env = tmp_path / ".env"
    env.write_text("RELOAD_ME=first-test-key\n", encoding="utf-8")
    load_env_file(env)
    assert os.environ["RELOAD_ME"] == "first-test-key"

    env.write_text("RELOAD_ME=second-test-key\n", encoding="utf-8")
    load_env_file(env)

    assert os.environ["RELOAD_ME"] == "second-test-key"


def test_removed_line_clears_value_loaded_by_app(tmp_path, monkeypatch):
    monkeypatch.delenv("KEEP_LINE", raising=False)
    monkeypatch.delenv("DROP_LINE", raising=False)
    env = tmp_path / ".env"
    env.write_text("KEEP_LINE=one\nDROP_LINE=two\n", encoding="utf-8")
    load_env_file(env)

    env.write_text("KEEP_LINE=one\n", encoding="utf-8")
    load_env_file(env)

    assert os.environ["KEEP_LINE"] == "one"
    assert "DROP_LINE" not in os.environ


def test_removed_file_clears_value_loaded_by_app(tmp_path, monkeypatch):
    monkeypatch.delenv("FILE_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("FILE_KEY=value\n", encoding="utf-8")
    load_env_file(env)
    env.unlink()

    load_env_file(env)

    assert "FILE_KEY" not in os.environ


def test_external_value_wins_and_survives_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTERNAL_KEY", "from-process")
    env = tmp_path / ".env"
    env.write_text("EXTERNAL_KEY=from-file-v1\n", encoding="utf-8")
    load_env_file(env)
    assert os.environ["EXTERNAL_KEY"] == "from-process"

    env.write_text("EXTERNAL_KEY=from-file-v2\n", encoding="utf-8")
    load_env_file(env)

    assert os.environ["EXTERNAL_KEY"] == "from-process"


def test_value_changed_after_load_is_not_overwritten_or_deleted(tmp_path, monkeypatch):
    monkeypatch.delenv("TAKE_OVER", raising=False)
    env = tmp_path / ".env"
    env.write_text("TAKE_OVER=from-file\n", encoding="utf-8")
    load_env_file(env)
    monkeypatch.setenv("TAKE_OVER", "manual")

    env.write_text("TAKE_OVER=from-file-v2\n", encoding="utf-8")
    load_env_file(env)
    assert os.environ["TAKE_OVER"] == "manual"

    env.write_text("OTHER=value\n", encoding="utf-8")
    load_env_file(env)
    assert os.environ["TAKE_OVER"] == "manual"


def test_override_replaces_external_value(tmp_path, monkeypatch):
    monkeypatch.setenv("FORCED", "external")
    env = tmp_path / ".env"
    env.write_text("FORCED=file\n", encoding="utf-8")

    load_env_file(env, override=True)

    assert os.environ["FORCED"] == "file"


def test_tracking_is_per_mapping(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SCOPED=one\n", encoding="utf-8")
    custom: dict[str, str] = {}

    load_env_file(env, environ=custom)
    env.write_text("SCOPED=two\n", encoding="utf-8")
    load_env_file(env, environ=custom)

    assert custom == {"SCOPED": "two"}
    assert "SCOPED" not in os.environ


def test_project_env_follows_cwd_and_clears_removed_file(tmp_path, monkeypatch):
    from video_renamer import paths

    project = tmp_path / "project"
    user = tmp_path / "user"
    work = tmp_path / "work"
    for folder in (project, user, work):
        folder.mkdir()
    monkeypatch.setattr(paths, "PROJECT_ROOT", project)
    monkeypatch.setattr(paths, "USER_ROOT", user)
    monkeypatch.chdir(work)
    monkeypatch.delenv("CWD_KEY", raising=False)
    (work / ".env").write_text("CWD_KEY=one\n", encoding="utf-8")

    paths.load_project_env()
    assert os.environ["CWD_KEY"] == "one"

    (work / ".env").unlink()
    paths.load_project_env()
    assert "CWD_KEY" not in os.environ
