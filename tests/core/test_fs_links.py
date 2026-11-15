"""Junction распознаются и на Python 3.10–3.11, где нет `os.path.isjunction`."""

from __future__ import annotations

import os
import subprocess

import pytest

from video_renamer.core.fs_links import is_junction, junction_from_reparse_tag
from video_renamer.core.title_service import iter_video_files

WINDOWS = os.name == "nt"


def make_junction(link, target) -> None:
    """Создаёт junction; тест пропускается с указанием причины, если нельзя."""
    if not WINDOWS:
        pytest.skip("junction есть только в Windows")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
        encoding="oem",
        errors="replace",
    )
    if result.returncode != 0:
        pytest.skip(
            "не удалось создать junction (нужна NTFS и право создавать каталоги): "
            f"{result.stdout.strip()} {result.stderr.strip()}".strip()
        )


def test_regular_directory_and_file_are_not_junctions(tmp_path):
    folder = tmp_path / "videos"
    folder.mkdir()
    video = folder / "a.mp4"
    video.write_bytes(b"x")

    assert is_junction(folder) is False
    assert is_junction(video) is False


def test_missing_path_is_not_a_junction(tmp_path):
    assert is_junction(tmp_path / "missing") is False


def test_junction_is_detected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    make_junction(link, target)

    assert is_junction(link) is True


def test_junction_is_detected_without_native_helper(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    make_junction(link, target)
    monkeypatch.delattr(os.path, "isjunction", raising=False)

    assert is_junction(link) is True


def test_plain_directory_is_not_detected_by_fallback(tmp_path, monkeypatch):
    folder = tmp_path / "videos"
    folder.mkdir()
    monkeypatch.delattr(os.path, "isjunction", raising=False)

    assert is_junction(folder) is False
    assert is_junction(tmp_path / "missing") is False


def test_fallback_returns_false_on_other_platforms(tmp_path, monkeypatch):
    monkeypatch.delattr(os.path, "isjunction", raising=False)
    monkeypatch.setattr(os, "name", "posix")

    assert is_junction(tmp_path) is False


def test_symlink_is_not_reported_as_junction(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink недоступен: {exc}")

    assert os.path.islink(link) is True
    assert is_junction(link) is False


def test_native_and_fallback_agree(tmp_path):
    native = getattr(os.path, "isjunction", None)
    if native is None:
        pytest.skip("os.path.isjunction отсутствует: проверяется только fallback")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    make_junction(link, target)
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")

    for path in (target, link, video, tmp_path / "missing"):
        assert native(path) == junction_from_reparse_tag(path), path


@pytest.mark.parametrize("use_native", [True, False])
def test_walk_skips_junction_to_outside(tmp_path, monkeypatch, use_native):
    if use_native and getattr(os.path, "isjunction", None) is None:
        pytest.skip("os.path.isjunction отсутствует")
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "inside.mp4"
    inside.write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.mp4").write_bytes(b"outside")
    make_junction(root / "jump", outside)
    if not use_native:
        monkeypatch.delattr(os.path, "isjunction", raising=False)

    found = list(iter_video_files(str(root), recursive=True))

    assert found == [str(inside)]


def test_walk_skips_junction_pointing_into_root(tmp_path, monkeypatch):
    root = tmp_path / "videos"
    root.mkdir()
    (root / "inner.mp4").write_bytes(b"inner")
    outer = tmp_path / "outer"
    outer.mkdir()
    own = outer / "own.mp4"
    own.write_bytes(b"own")
    make_junction(outer / "jump", root)
    monkeypatch.delattr(os.path, "isjunction", raising=False)

    found = list(iter_video_files(str(outer), recursive=True))

    assert found == [str(own)]


def test_walk_still_enters_regular_subdirectories(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    inside = root / "inside.mp4"
    inside.write_bytes(b"inside")
    nested = root / "sub" / "nested.mp4"
    nested.write_bytes(b"nested")

    found = list(iter_video_files(str(root), recursive=True))

    assert found == [str(inside), str(nested)]
