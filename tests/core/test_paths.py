"""Пути ресурсов и пользовательских данных.

Проверяется разделение: поставляемый справочник живёт в пакете, изменяемые
файлы — в пользовательском каталоге; в checkout сохраняются прежние пути.
"""

from __future__ import annotations

import os

import pytest

from video_renamer import paths


def test_checkout_keeps_existing_locations():
    """В checkout журнал, кэш и data/ остаются там, где были."""
    assert paths.IN_CHECKOUT is True
    assert paths.USER_ROOT == paths.PROJECT_ROOT
    assert paths.DATA_DIR == paths.PROJECT_ROOT / "data"
    assert paths.CACHE_PATH == paths.PROJECT_ROOT / "cache" / "title_fixer.sqlite3"
    assert paths.LOG_PATH == paths.PROJECT_ROOT / "rename_log.csv"


def test_packaged_catalog_ships_inside_the_package():
    assert paths.PACKAGED_STUDIOS_PATH.is_file()
    assert paths.PACKAGED_STUDIOS_PATH.is_relative_to(paths.PACKAGE_DIR)
    assert paths.PACKAGED_STUDIOS_PATH.stat().st_size > 1_000_000


def test_user_catalog_wins_over_packaged(tmp_path, monkeypatch):
    user = tmp_path / "studios.json"
    user.write_text('{"A": "a"}', encoding="utf-8")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)

    assert paths.user_studios_path() == user
    assert paths.resolve_studios_path() == user


def test_missing_user_catalog_falls_back_to_packaged(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)

    assert paths.resolve_studios_path() == paths.PACKAGED_STUDIOS_PATH


def test_data_dir_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_RENAMER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "IN_CHECKOUT", False)

    assert paths._default_user_root() == tmp_path


@pytest.mark.skipif(os.name != "nt", reason="ветка LOCALAPPDATA только в Windows")
def test_installed_root_is_outside_the_package(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDEO_RENAMER_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(paths, "IN_CHECKOUT", False)

    root = paths._default_user_root()

    assert root == tmp_path / "VideoFilesRenamer"
    assert not root.is_relative_to(paths.PACKAGE_DIR)
