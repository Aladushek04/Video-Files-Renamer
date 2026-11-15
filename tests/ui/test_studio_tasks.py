"""Тесты запуска сервисов студий из TUI (без Textual и сети)."""

import json
import os
import sys
import threading
import time

import pytest

from video_renamer import paths
from video_renamer.ui import studio_tasks


def _listing(folder) -> list[tuple[str, int]]:
    """Снимок каталога без чтения содержимого: имена и mtime."""
    if not folder.is_dir():
        return []
    return sorted((item.name, item.stat().st_mtime_ns) for item in folder.iterdir())


def test_run_captures_child_output(monkeypatch):
    monkeypatch.setattr(
        studio_tasks,
        "_command",
        lambda kind: [
            sys.executable,
            "-c",
            "print('page 1: 10 studios')",
        ],
    )
    lines = []
    code = studio_tasks.run("stashdb", lines.append)
    assert code == 0
    assert lines == ["page 1: 10 studios"]


def test_missing_stdout_pipe_is_reported_before_starting_the_reader(tmp_path, monkeypatch):
    class FakeProcess:
        stdout = None
        terminated = False

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            FakeProcess.terminated = True

        def kill(self):
            pass

    def fake_popen(*_args, **_kwargs):
        return FakeProcess()

    monkeypatch.setattr(studio_tasks, "DATA_DIR", tmp_path)
    monkeypatch.setattr(studio_tasks.subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="stdout"):
        studio_tasks.run("merge")

    assert FakeProcess.terminated is True


def test_commands_use_module_entrypoints():
    assert "video_renamer.scrapers.stashdb" in studio_tasks._command("stashdb")
    assert "video_renamer.scrapers.fansdb" in studio_tasks._command("fansdb")
    assert "video_renamer.scrapers.theporndb" in studio_tasks._command("theporndb")
    merge = studio_tasks._command("merge")
    for name in studio_tasks.MERGE_INPUT_NAMES:
        assert str(studio_tasks.DATA_DIR / name) in merge
    assert "--apply" in merge


def test_project_root_is_checkout_root():
    assert studio_tasks.PROJECT_ROOT == paths.PROJECT_ROOT
    assert (studio_tasks.PROJECT_ROOT / "video_renamer").is_dir()
    assert studio_tasks.DATA_DIR == studio_tasks.PROJECT_ROOT / "data"


def test_merge_command_uses_absolute_paths():
    command = studio_tasks._command("merge")
    json_paths = [arg for arg in command if arg.endswith(".json")]

    assert json_paths
    for value in json_paths:
        assert os.path.isabs(value), value
    assert str(studio_tasks.DATA_DIR / "studios.json") in command


def test_child_gets_project_root_and_import_path(monkeypatch, tmp_path):
    script = (
        "import json, os;"
        "print(json.dumps({"
        "'cwd': os.getcwd(),"
        "'pythonpath': os.environ.get('PYTHONPATH', '')"
        "}))"
    )
    monkeypatch.setattr(studio_tasks, "_command", lambda kind: [sys.executable, "-c", script])
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    code = studio_tasks.run("stashdb", lines.append)

    assert code == 0
    info = json.loads(lines[0])
    assert info["cwd"] == str(studio_tasks.USER_ROOT)
    assert str(studio_tasks.PROJECT_ROOT) in info["pythonpath"].split(os.pathsep)


def test_merge_runs_from_other_cwd_with_temp_data(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "studios.json").write_text(json.dumps({"Alpha": "alpha"}), encoding="utf-8")
    (data / "studios_fansdb.json").write_text(json.dumps({"Beta": "beta"}), encoding="utf-8")
    (data / "studios_theporndb.json").write_text(json.dumps({"Gamma": "gamma"}), encoding="utf-8")
    project_data_before = _listing(studio_tasks.PROJECT_ROOT / "data")
    stray = studio_tasks.PROJECT_ROOT / "video_renamer" / "data"
    stray_before = _listing(stray)
    monkeypatch.setattr(studio_tasks, "DATA_DIR", data)
    monkeypatch.chdir(tmp_path)
    lines: list[str] = []

    code = studio_tasks.run("merge", lines.append)

    assert code == 0, "\n".join(lines)
    merged = json.loads((data / "studios.json").read_text(encoding="utf-8"))
    assert len(merged) == 3
    assert _listing(studio_tasks.PROJECT_ROOT / "data") == project_data_before
    assert _listing(stray) == stray_before


def test_cancel_terminates_child(monkeypatch):
    monkeypatch.setattr(
        studio_tasks,
        "_command",
        lambda kind: [
            sys.executable,
            "-c",
            "import time; print('started', flush=True); time.sleep(30)",
        ],
    )
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    started = time.monotonic()
    code = studio_tasks.run("stashdb", cancel_event=cancel)
    elapsed = time.monotonic() - started
    timer.cancel()
    assert code == studio_tasks.CANCELLED_CODE
    assert elapsed < 10


def test_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown"):
        studio_tasks.run("unknown")
