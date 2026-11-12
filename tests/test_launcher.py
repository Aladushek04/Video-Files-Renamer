"""Проверка безопасного обновления перед запуском GUI."""

from __future__ import annotations

import shutil
import subprocess

import pytest

import launcher

GIT = shutil.which("git")


def _git(cwd, *args: str) -> subprocess.CompletedProcess[str]:
    assert GIT is not None
    return subprocess.run(
        [GIT, *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_skips_without_git(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.shutil, "which", lambda _name: None)

    result = launcher.check_for_updates(tmp_path)

    assert result.status == "skipped"
    assert "git" in result.message.casefold()


def test_skips_when_disabled(tmp_path):
    result = launcher.check_for_updates(tmp_path, enabled=False)

    assert result.status == "skipped"
    assert "отключено" in result.message


@pytest.mark.skipif(GIT is None, reason="git is required for the integration test")
def test_fast_forward_pull_updates_worktree(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "init", "-b", "main", str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "file.txt").write_text("one", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "one")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    (other / "file.txt").write_text("two", encoding="utf-8")
    _git(other, "add", ".")
    _git(other, "commit", "-m", "two")
    _git(other, "push", "origin", "main")

    result = launcher.check_for_updates(work)

    assert result.status == "updated"
    assert "+1" in result.message
    assert (work / "file.txt").read_text(encoding="utf-8") == "two"


@pytest.mark.skipif(GIT is None, reason="git is required for the integration test")
def test_dirty_worktree_is_not_pulled(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "init", "-b", "main", str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "file.txt").write_text("one", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "one")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    (other / "file.txt").write_text("two", encoding="utf-8")
    _git(other, "add", ".")
    _git(other, "commit", "-m", "two")
    _git(other, "push", "origin", "main")

    (work / "file.txt").write_text("local edit", encoding="utf-8")
    result = launcher.check_for_updates(work)

    assert result.status == "skipped"
    assert "локальные изменения" in result.message
    assert (work / "file.txt").read_text(encoding="utf-8") == "local edit"
