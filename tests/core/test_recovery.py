"""Восстановление после сбоев apply/undo: карта, замок, repair (A02/A03)."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from video_renamer.cli import append_rename_log
from video_renamer.core import review_actions, title_service
from video_renamer.core import undo as undo_module
from video_renamer.core.file_lock import LockBusy, RenamerLock
from video_renamer.core.oshash import oshash_from_path
from video_renamer.core.rename_intent import (
    PendingOperationError,
    PlannedStep,
    intent_path,
    read_intent_states,
    write_intent,
)
from video_renamer.core.rename_log import (
    LogEntry,
    active_renames,
    append_log,
    format_log_timestamp,
    read_log,
)
from video_renamer.core.repair import repair
from video_renamer.core.review_actions import apply_rows
from video_renamer.core.title_service import PreviewRow, TitleService
from video_renamer.core.undo import run_undo, select_for_undo

ROOT = Path(__file__).resolve().parents[2]
MOMENT = datetime(2026, 11, 20, 9, 0, 0, 123456, tzinfo=timezone.utc)


def make_video(path: Path, data: bytes = b"a" * 100) -> Path:
    path.write_bytes(data)
    return path


def make_row(path: Path, new_name: str) -> PreviewRow:
    return PreviewRow(
        True,
        path.name,
        new_name,
        "fingerprint",
        1.0,
        "s1",
        "",
        oshash_from_path(str(path)),
        path.stat().st_size,
    )


def legacy_apply(root: Path, pairs: list[tuple[str, str]], moment: datetime = MOMENT) -> None:
    """Apply и журнал без карты намерения (как в 0.2.0) — для подготовки отката."""
    rows = [make_row(root / old, new) for old, new in pairs]
    report = TitleService(str(root), {}, None).apply_rows(rows, dry_run=False)
    append_rename_log(
        str(root / "rename_log.csv"),
        rows,
        report.results,
        format_log_timestamp(moment),
        root=str(root),
    )


def make_step(
    root: Path,
    old: str,
    new: str,
    *,
    temp: str = "__ren_tmp_crash",
    oshash: str | None = None,
    size: int | None = None,
) -> PlannedStep:
    return PlannedStep(
        old_path=str(root / old),
        temp_path=str(root / temp),
        new_path=str(root / new),
        old_rel=old,
        new_rel=new,
        root=str(root),
        oshash=oshash if oshash is not None else "0123456789abcdef",
        size=size,
        scene_id="s1",
        source="fingerprint",
    )


def make_log_row(root: Path, old: str, new: str, step: PlannedStep) -> dict:
    return {
        "timestamp": format_log_timestamp(MOMENT),
        "action": "rename",
        "undoes": "",
        "root": str(root),
        "old": old,
        "new": new,
        "oshash": step.oshash,
        "size": step.size,
        "scene_id": step.scene_id,
        "source": step.source,
    }


class OsProxy:
    """Подменяет `os` только внутри title_service и следит за первым rename."""

    def __init__(self, check) -> None:
        self._check = check

    def __getattr__(self, name):
        return getattr(os, name)

    def rename(self, source, target):
        self._check(Path(source), Path(target))
        return os.rename(source, target)


def test_intent_is_written_before_first_rename(tmp_path, monkeypatch):
    source = make_video(tmp_path / "a.mp4")
    log = tmp_path / "rename_log.csv"
    seen: list[tuple[str, bool]] = []

    def check(_source: Path, target: Path) -> None:
        seen.append((target.name, Path(intent_path(log)).is_file()))

    monkeypatch.setattr(title_service, "os", OsProxy(check))

    report, log_error = apply_rows(str(tmp_path), [make_row(source, "b.mp4")], log_path=str(log))

    assert report.results[0].status == "renamed"
    assert log_error == ""
    assert seen and seen[0][1] is True, "карта должна существовать до первого rename"
    assert (tmp_path / "b.mp4").exists()


def test_intent_write_failure_renames_nothing(tmp_path, monkeypatch):
    source = make_video(tmp_path / "a.mp4")
    log = tmp_path / "rename_log.csv"

    def boom(*_args, **_kwargs):
        raise OSError("intent is not writable")

    monkeypatch.setattr(review_actions, "write_intent", boom)

    report, _log_error = apply_rows(str(tmp_path), [make_row(source, "b.mp4")], log_path=str(log))

    assert report.had_errors is True
    assert "cannot record rename intent" in report.results[0].message
    assert source.exists()
    assert not (tmp_path / "b.mp4").exists()
    assert not log.exists()


def test_undo_intent_failure_renames_nothing(tmp_path, monkeypatch):
    log = tmp_path / "rename_log.csv"
    make_video(tmp_path / "a.mp4")
    legacy_apply(tmp_path, [("a.mp4", "b.mp4")])
    entry = active_renames(read_log(str(log)))[0]

    def boom(*_args, **_kwargs):
        raise OSError("intent is not writable")

    monkeypatch.setattr(undo_module, "write_intent", boom)
    report = run_undo(
        select_for_undo(read_log(str(log)), [entry.id]), log_path=str(log), dry_run=False
    )

    assert report.had_errors is True
    assert "cannot record rename intent" in report.results[0].message
    assert (tmp_path / "b.mp4").exists()
    assert not (tmp_path / "a.mp4").exists()


def test_open_intent_blocks_new_apply_and_undo(tmp_path):
    source = make_video(tmp_path / "a.mp4")
    log = tmp_path / "rename_log.csv"
    step = make_step(tmp_path, "a.mp4", "b.mp4")
    write_intent(
        str(log),
        kind="rename",
        steps=[step],
        log_rows=[make_log_row(tmp_path, "a.mp4", "b.mp4", step)],
    )

    with pytest.raises(PendingOperationError, match="repair"):
        apply_rows(str(tmp_path), [make_row(source, "b.mp4")], log_path=str(log))

    with pytest.raises(PendingOperationError, match="repair"):
        run_undo(select_for_undo([], []), log_path=str(log), dry_run=False)

    assert source.exists()


def test_lock_busy_blocks_apply_and_undo(tmp_path):
    make_video(tmp_path / "a.mp4")
    log = tmp_path / "rename_log.csv"
    legacy_apply(tmp_path, [("a.mp4", "b.mp4")])
    entry = active_renames(read_log(str(log)))[0]

    with RenamerLock(str(log)):
        with pytest.raises(LockBusy):
            apply_rows(
                str(tmp_path),
                [make_row(tmp_path / "b.mp4", "c.mp4")],
                log_path=str(log),
            )
        with pytest.raises(LockBusy):
            run_undo(
                select_for_undo(read_log(str(log)), [entry.id]),
                log_path=str(log),
                dry_run=False,
            )

    assert (tmp_path / "b.mp4").exists()
    assert not (tmp_path / "c.mp4").exists()


def test_undo_unwritable_journal_stops_before_files(tmp_path):
    log = tmp_path / "rename_log.csv"
    make_video(tmp_path / "a.mp4")
    legacy_apply(tmp_path, [("a.mp4", "b.mp4")])
    entry = active_renames(read_log(str(log)))[0]
    blocked = tmp_path / "blocked.csv"
    blocked.mkdir()

    report = run_undo(
        select_for_undo(read_log(str(log)), [entry.id]),
        log_path=str(blocked),
        dry_run=False,
    )

    assert report.had_errors is True
    assert "journal is not writable" in report.results[0].message
    assert (tmp_path / "b.mp4").exists()
    assert not (tmp_path / "a.mp4").exists()


def test_repair_completes_temp_and_is_idempotent(tmp_path):
    log = str(tmp_path / "rename_log.csv")
    source = make_video(tmp_path / "a.mp4")
    step = make_step(
        tmp_path,
        "a.mp4",
        "b.mp4",
        oshash=oshash_from_path(str(source)),
        size=source.stat().st_size,
    )
    os.rename(source, step.temp_path)
    write_intent(
        log, kind="rename", steps=[step], log_rows=[make_log_row(tmp_path, "a.mp4", "b.mp4", step)]
    )

    preview = repair(log, apply_changes=False)
    assert preview.applied == ()
    assert preview.diagnoses and preview.diagnoses[0].needs_write
    assert (tmp_path / "__ren_tmp_crash").exists()

    done = repair(log, apply_changes=True)
    assert done.remaining == ()
    assert (tmp_path / "b.mp4").exists()
    assert len(read_log(log)) == 1

    again = repair(log, apply_changes=True)
    assert again.diagnoses == ()
    assert len(read_log(log)) == 1


def test_repair_does_not_duplicate_log_after_crash_before_done(tmp_path):
    log = str(tmp_path / "rename_log.csv")
    source = make_video(tmp_path / "a.mp4")
    os.rename(source, tmp_path / "b.mp4")
    step = make_step(
        tmp_path,
        "a.mp4",
        "b.mp4",
        oshash=oshash_from_path(str(tmp_path / "b.mp4")),
        size=100,
    )
    row = make_log_row(tmp_path, "a.mp4", "b.mp4", step)
    write_intent(log, kind="rename", steps=[step], log_rows=[row])
    append_log(log, [LogEntry(legacy=False, **row)])

    report = repair(log, apply_changes=True)
    assert report.remaining == ()
    assert len(read_log(log)) == 1

    repair(log, apply_changes=True)
    assert len(read_log(log)) == 1
    states, problems = read_intent_states(log)
    assert problems == []
    assert states[0].phase == "done"


def test_repair_leaves_ambiguous_files_in_place(tmp_path):
    log = str(tmp_path / "rename_log.csv")
    make_video(tmp_path / "a.mp4")
    make_video(tmp_path / "b.mp4")
    step = make_step(
        tmp_path,
        "a.mp4",
        "b.mp4",
        oshash=oshash_from_path(str(tmp_path / "a.mp4")),
        size=100,
    )
    write_intent(
        log, kind="rename", steps=[step], log_rows=[make_log_row(tmp_path, "a.mp4", "b.mp4", step)]
    )

    report = repair(log, apply_changes=True)

    assert report.remaining
    assert (tmp_path / "a.mp4").exists()
    assert (tmp_path / "b.mp4").exists()
    assert not (tmp_path / "rename_log.csv").exists()


def test_repair_verifies_identity_before_completing(tmp_path):
    log = str(tmp_path / "rename_log.csv")
    original = make_video(tmp_path / "a.mp4")
    temp = tmp_path / "__ren_tmp_crash"
    os.rename(original, temp)
    original_oshash = oshash_from_path(str(temp))
    temp.write_bytes(b"changed" + b"x" * 93)  # тот же размер, другой OSHASH
    step = make_step(tmp_path, "a.mp4", "b.mp4", oshash=original_oshash, size=100)
    write_intent(
        log, kind="rename", steps=[step], log_rows=[make_log_row(tmp_path, "a.mp4", "b.mp4", step)]
    )

    report = repair(log, apply_changes=True)

    assert report.remaining
    assert temp.exists()
    assert not (tmp_path / "b.mp4").exists()
    assert "не подтвержд" in report.diagnoses[0].steps[0].message


def test_repair_completes_partial_swap(tmp_path):
    log = str(tmp_path / "rename_log.csv")
    a = make_video(tmp_path / "a.mp4", b"A" * 100)
    b = make_video(tmp_path / "b.mp4", b"B" * 100)
    step_a = make_step(
        tmp_path, "a.mp4", "b.mp4", temp="__ren_tmp_a", oshash=oshash_from_path(str(a)), size=100
    )
    step_b = make_step(
        tmp_path, "b.mp4", "a.mp4", temp="__ren_tmp_b", oshash=oshash_from_path(str(b)), size=100
    )
    # Обмен дошёл до второй фазы наполовину: a → b готов, исходный b ещё во временном.
    os.rename(b, step_b.temp_path)
    os.rename(a, step_a.temp_path)
    os.rename(step_a.temp_path, tmp_path / "b.mp4")
    write_intent(
        log,
        kind="rename",
        steps=[step_a, step_b],
        log_rows=[
            make_log_row(tmp_path, "a.mp4", "b.mp4", step_a),
            make_log_row(tmp_path, "b.mp4", "a.mp4", step_b),
        ],
    )

    report = repair(log, apply_changes=True)

    assert report.remaining == ()
    assert (tmp_path / "a.mp4").read_bytes() == b"B" * 100
    assert (tmp_path / "b.mp4").read_bytes() == b"A" * 100
    assert len(read_log(log)) == 2


def test_repair_closes_operation_that_never_started(tmp_path):
    log = str(tmp_path / "rename_log.csv")
    make_video(tmp_path / "a.mp4")
    step = make_step(tmp_path, "a.mp4", "b.mp4", oshash="0123456789abcdef", size=100)
    write_intent(
        log, kind="rename", steps=[step], log_rows=[make_log_row(tmp_path, "a.mp4", "b.mp4", step)]
    )

    report = repair(log, apply_changes=True)

    assert report.remaining == ()
    assert (tmp_path / "a.mp4").exists()
    assert not (tmp_path / "rename_log.csv").exists()
    states, _problems = read_intent_states(log)
    assert states[0].phase == "done"


def test_undo_records_intent_and_marks_done(tmp_path):
    log = tmp_path / "rename_log.csv"
    make_video(tmp_path / "a.mp4")
    legacy_apply(tmp_path, [("a.mp4", "b.mp4")])
    entry = active_renames(read_log(str(log)))[0]

    report = run_undo(
        select_for_undo(read_log(str(log)), [entry.id]), log_path=str(log), dry_run=False
    )

    assert report.had_errors is False
    states, problems = read_intent_states(str(log))
    assert problems == []
    assert states[-1].intent.kind == "undo"
    assert states[-1].phase == "done"


def test_process_crash_after_first_rename_is_recoverable(tmp_path):
    root = tmp_path / "videos"
    root.mkdir()
    make_video(root / "a.mp4")
    log = root / "rename_log.csv"
    script = tmp_path / "crash_apply.py"
    script.write_text(
        "\n".join(
            (
                "import os",
                f"import sys; sys.path.insert(0, {str(ROOT)!r})",
                "from video_renamer.core.review_actions import apply_rows",
                "from video_renamer.core.title_service import PreviewRow",
                "from video_renamer.core.oshash import oshash_from_path",
                "import video_renamer.core.title_service as ts",
                f"root = {str(root)!r}",
                "old = os.path.join(root, 'a.mp4')",
                "row = PreviewRow(True, 'a.mp4', 'b.mp4', 'fingerprint', 1.0, 's1', '',",
                "                 oshash_from_path(old), os.path.getsize(old))",
                "real_rename = os.rename",
                "count = {'n': 0}",
                "def crashing(source, target):",
                "    real_rename(source, target)",
                "    count['n'] += 1",
                "    if count['n'] == 1:",
                "        os._exit(9)",
                "ts.os.rename = crashing",
                "apply_rows(root, [row], log_path=os.path.join(root, 'rename_log.csv'))",
            )
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )

    assert result.returncode == 9, result.stderr
    temps = list(root.glob("__ren_tmp_*"))
    assert len(temps) == 1
    assert not (root / "b.mp4").exists()
    assert not log.exists()

    report = repair(str(log), apply_changes=True)

    assert report.remaining == ()
    assert (root / "b.mp4").exists()
    assert len(read_log(str(log))) == 1

    # Замок снят вместе с упавшим процессом: следующий apply проходит.
    make_video(root / "c.mp4", b"c" * 100)
    follow_up, _error = apply_rows(
        str(root), [make_row(root / "c.mp4", "d.mp4")], log_path=str(log)
    )
    assert follow_up.results[0].status == "renamed"


def test_failed_final_and_rollback_keeps_operation_open(tmp_path, monkeypatch):
    source = make_video(tmp_path / "a.mp4")
    log = tmp_path / "rename_log.csv"
    calls = {"n": 0}

    class FailingOs(OsProxy):
        def rename(self, source_path, target_path):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("device is gone")
            return super().rename(source_path, target_path)

    monkeypatch.setattr(title_service, "os", FailingOs(lambda *args: None))
    report, _log_error = apply_rows(str(tmp_path), [make_row(source, "b.mp4")], log_path=str(log))

    assert report.had_errors
    assert "temporary file kept" in report.results[0].message
    assert len(list(tmp_path.glob("__ren_tmp_*"))) == 1
    states, problems = read_intent_states(str(log))
    assert problems == []
    assert states[-1].phase == "needs_repair"
    assert not (tmp_path / "b.mp4").exists()

    fixed = repair(str(log), apply_changes=True)

    assert fixed.remaining == ()
    assert (tmp_path / "b.mp4").exists()
    assert len(read_log(str(log))) == 1


def test_repair_reports_corrupt_intent_file(tmp_path):
    log = str(tmp_path / "rename_log.csv")
    path = Path(intent_path(log))
    path.write_text("this is not json\n", encoding="utf-8")

    report = repair(log, apply_changes=True)

    assert report.problems
    assert report.remaining == ()
