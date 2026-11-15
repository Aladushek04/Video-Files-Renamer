"""Проверка настоящего wheel: ресурсы на месте, данные пишутся вне пакета.

Собирается wheel текущего дерева, ставится в чистый venv без editable, и
пакет запускается из постороннего каталога с изолированным `sys.path`
(`python -I`). Тест медленный и требует сборщик; если wheel собрать нельзя
(нет сети для setuptools и т.п.), он пропускается с причиной.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import textwrap
import venv
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = "Scripts/python.exe" if os.name == "nt" else "bin/python"


def _run(command, *, cwd, env=None):
    return subprocess.run(
        [str(part) for part in command],
        check=False,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("wheel")
    result = _run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", out, ROOT],
        cwd=ROOT,
    )
    if result.returncode != 0:
        pytest.skip(f"wheel не собран: {result.stderr.strip()[-400:]}")
    wheels = sorted(out.glob("*.whl"))
    assert wheels, result.stdout
    return wheels[0]


@pytest.fixture(scope="module")
def installed_python(wheel, tmp_path_factory) -> Path:
    target = tmp_path_factory.mktemp("venv")
    venv.EnvBuilder(with_pip=True, clear=True).create(target)
    python = target / VENV_PYTHON
    result = _run(
        [python, "-m", "pip", "install", "--no-index", "--no-deps", wheel],
        cwd=tmp_path_factory.mktemp("install"),
    )
    if result.returncode != 0:
        pytest.skip(f"wheel не установлен: {result.stderr.strip()[-400:]}")
    return python


def _wheel_metadata(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        name = next(item for item in archive.namelist() if item.endswith(".dist-info/METADATA"))
        return archive.read(name).decode("utf-8")


@pytest.fixture(scope="module")
def floor_wheel(tmp_path_factory) -> Path:
    """Wheel, собранный именно семейством минимально заявленного setuptools."""
    out = tmp_path_factory.mktemp("floor-wheel")
    constraints = out / "constraints.txt"
    constraints.write_text("setuptools==77.0.3\n", encoding="ascii")
    env = dict(os.environ)
    env["PIP_CONSTRAINT"] = str(constraints)
    result = _run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", out, ROOT],
        cwd=ROOT,
        env=env,
    )
    if result.returncode != 0:
        pytest.skip(f"wheel с setuptools 77.0.3 не собран: {result.stderr.strip()[-400:]}")
    wheels = sorted(out.glob("*.whl"))
    assert wheels, result.stdout
    return wheels[0]


def test_wheel_contains_resources(wheel):
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    assert "video_renamer/data/studios.json" in names
    assert "video_renamer/ui/tui.tcss" in names
    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names)


def test_wheel_metadata_uses_pep639_license(wheel):
    metadata = _wheel_metadata(wheel)

    assert "License-Expression: MIT" in metadata
    assert "License-File: LICENSE" in metadata
    assert "License :: OSI Approved" not in metadata


def test_declared_setuptools_floor_builds_pep639_metadata(floor_wheel):
    """Формат лицензии понимает не только свежий, но и минимальный setuptools."""
    metadata = _wheel_metadata(floor_wheel)

    assert "License-Expression: MIT" in metadata
    assert "License-File: LICENSE" in metadata


def _build_module_available() -> bool:
    """Установлен ли пакет `build` (а не одноимённая папка от старой сборки)."""
    spec = importlib.util.find_spec("build")
    return spec is not None and spec.origin not in (None, "namespace")


def test_sdist_ships_the_license(tmp_path):
    """Исходный архив тоже собирается и содержит лицензию."""
    if not _build_module_available():
        pytest.skip("модуль build не установлен")
    out = tmp_path / "dist"
    result = _run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", out, ROOT],
        cwd=ROOT,
    )
    if result.returncode != 0:
        pytest.skip(f"sdist не собран: {result.stderr.strip()[-400:]}")

    archive = next(out.glob("*.tar.gz"))
    with tarfile.open(archive) as tar:
        names = tar.getnames()

    assert any(name.endswith("/LICENSE") for name in names)
    assert any(name.endswith("/pyproject.toml") for name in names)


def test_installed_package_runs_outside_repo(installed_python, tmp_path):
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    data_dir = tmp_path / "userdata"
    env = {
        key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    env["VIDEO_RENAMER_DATA_DIR"] = str(data_dir)
    script = textwrap.dedent(
        """
        import json
        from video_renamer import paths
        from video_renamer.core.review_actions import load_studios_catalog

        catalog = load_studios_catalog()
        tui_css = paths.PACKAGE_DIR / "ui" / "tui.tcss"
        print("RESULT=" + json.dumps({
            "checkout": paths.IN_CHECKOUT,
            "studios": str(paths.STUDIOS_PATH),
            "studios_exists": paths.STUDIOS_PATH.is_file(),
            "catalog_keys": len(catalog),
            "log": str(paths.LOG_PATH),
            "cache_dir": str(paths.CACHE_PATH.parent),
            "data_dir": str(paths.DATA_DIR),
            "tui_css": tui_css.is_file(),
            "user_inside_package": str(paths.USER_ROOT).startswith(str(paths.PACKAGE_DIR)),
        }, ensure_ascii=False))
        """
    )

    result = _run([installed_python, "-I", "-c", script], cwd=workdir, env=env)

    assert result.returncode == 0, result.stderr
    line = next(item for item in result.stdout.splitlines() if item.startswith("RESULT="))
    info = json.loads(line.removeprefix("RESULT="))
    assert info["checkout"] is False
    assert info["studios_exists"] is True
    assert str(info["studios"]).endswith("studios.json")
    assert info["catalog_keys"] > 1000
    assert info["tui_css"] is True
    assert info["user_inside_package"] is False
    for key in ("log", "cache_dir", "data_dir"):
        assert str(data_dir) in info[key], key


def test_installed_offline_preview_uses_the_catalog(installed_python, tmp_path):
    workdir = tmp_path / "outside"
    videos = tmp_path / "videos"
    workdir.mkdir()
    videos.mkdir()
    csv_path = tmp_path / "preview.csv"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "STASHDB_API_KEY"}
    }
    env["VIDEO_RENAMER_DATA_DIR"] = str(tmp_path / "userdata")

    result = _run(
        [
            installed_python,
            "-I",
            "-m",
            "video_renamer.cli",
            "preview",
            videos,
            "--offline",
            "--csv",
            csv_path,
        ],
        cwd=workdir,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "studio matching is disabled" not in (result.stdout + result.stderr)
    assert csv_path.is_file()


def test_installed_write_creates_user_dirs(installed_python, tmp_path):
    workdir = tmp_path / "outside"
    workdir.mkdir()
    data_dir = tmp_path / "custom-data"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["VIDEO_RENAMER_DATA_DIR"] = str(data_dir)
    script = textwrap.dedent(
        """
        from video_renamer.core.title_cache import TitleCache
        from video_renamer.core.rename_log import log_write_problem
        from video_renamer import paths

        cache = TitleCache(paths.CACHE_PATH)
        cache.close()
        print("LOG=" + (log_write_problem(str(paths.LOG_PATH)) or "ok"))
        """
    )

    result = _run([installed_python, "-I", "-c", script], cwd=workdir, env=env)

    assert result.returncode == 0, result.stderr
    assert "LOG=ok" in result.stdout
    assert (data_dir / "cache").is_dir()
    assert (data_dir / "rename_log.csv").is_file() is False
