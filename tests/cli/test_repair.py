"""CLI `repair`: просмотр, восстановление и идемпотентность (A02/A03)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from video_renamer.cli import main, write_preview_csv
from video_renamer.core.file_lock import RenamerLock
from video_renamer.core.oshash import oshash_from_path
from video_renamer.core.rename_intent import (
    PlannedStep,
    intent_path,
    read_intent_states,
    write_intent,
)
from video_renamer.core.rename_log import LogEntry, append_log, format_log_timestamp, read_log
from video_renamer.core.title_service import PreviewRow

MOMENT = datetime(2026, 11, 21, 12, 0, 0, 500000, tzinfo=timezone.utc)


def make_video(path: Path, data: bytes = b"a" * 100) -> Path:
    path.write_bytes(data)
    return path


def make_step(root: Path, old: str, new: str, temp: str, *, oshash: str, size: int) -> PlannedStep:
    return PlannedStep(
        old_path=str(root / old),
        temp_path=str(root / temp),
        new_path=str(root / new),
        old_rel=old,
        new_rel=new,
        root=str(root),
        oshash=oshash,
        size=size,
        scene_id="s1",
        source="fingerprint",
    )


def make_row(root: Path, step: PlannedStep) -> dict:
    return {
        "timestamp": format_log_timestamp(MOMENT),
        "action": "rename",
        "undoes": "",
        "root": str(root),
        "old": step.old_rel,
        "new": step.new_rel,
        "oshash": step.oshash,
        "size": step.size,
        "scene_id": step.scene_id,
        "source": step.source,
    }


def crashed_temp_state(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "rename_log.csv"
    source = make_video(tmp_path / "a.mp4")
    step = make_step(
        tmp_path,
        "a.mp4",
        "b.mp4",
        "__ren_tmp_crash",
        oshash=oshash_from_path(str(source)),
        size=source.stat().st_size,
    )
    os.rename(source, step.temp_path)
    write_intent(str(log), kind="rename", steps=[step], log_rows=[make_row(tmp_path, step)])
    return log, step


def test_repair_without_intents_exits_zero(tmp_path, capsys):
    code = main(["repair", "--log", str(tmp_path / "rename_log.csv")])

    assert code == 0
    assert "незавершённых операций нет" in capsys.readouterr().out


def test_repair_preview_does_not_touch_files(tmp_path, capsys):
    log, _step = crashed_temp_state(tmp_path)

    code = main(["repair", "--log", str(log)])
    output = capsys.readouterr().out

    assert code == 1
    assert "завершить" in output
    assert "__ren_tmp_crash" in output
    assert "изменения не вносились" in output
    assert (tmp_path / "__ren_tmp_crash").exists()
    assert not (tmp_path / "b.mp4").exists()
    assert not log.exists()


def test_repair_apply_finishes_and_is_idempotent(tmp_path, capsys):
    log, _step = crashed_temp_state(tmp_path)

    assert main(["repair", "--log", str(log), "--apply"]) == 0
    first = capsys.readouterr().out
    assert "завершено: a.mp4 -> b.mp4" in first
    assert (tmp_path / "b.mp4").exists()
    assert len(read_log(str(log))) == 1

    assert main(["repair", "--log", str(log), "--apply"]) == 0
    second = capsys.readouterr().out
    assert "незавершённых операций нет" in second
    assert len(read_log(str(log))) == 1


def test_repair_does_not_duplicate_csv_after_crash_before_done(tmp_path, capsys):
    root = tmp_path
    log = root / "rename_log.csv"
    make_video(root / "a.mp4")
    os.rename(root / "a.mp4", root / "b.mp4")
    step = make_step(
        root,
        "a.mp4",
        "b.mp4",
        "__ren_tmp_crash",
        oshash=oshash_from_path(str(root / "b.mp4")),
        size=100,
    )
    row = make_row(root, step)
    write_intent(str(log), kind="rename", steps=[step], log_rows=[row])
    append_log(str(log), [LogEntry(legacy=False, **row)])

    assert main(["repair", "--log", str(log), "--apply"]) == 0
    capsys.readouterr()
    assert main(["repair", "--log", str(log), "--apply"]) == 0
    capsys.readouterr()

    assert len(read_log(str(log))) == 1
    states, problems = read_intent_states(str(log))
    assert problems == []
    assert states[0].phase == "done"


def test_repair_leaves_ambiguous_state_for_a_human(tmp_path, capsys):
    root = tmp_path
    log = root / "rename_log.csv"
    make_video(root / "a.mp4")
    make_video(root / "b.mp4")
    step = make_step(
        root,
        "a.mp4",
        "b.mp4",
        "__ren_tmp_crash",
        oshash=oshash_from_path(str(root / "a.mp4")),
        size=100,
    )
    write_intent(str(log), kind="rename", steps=[step], log_rows=[make_row(root, step)])

    code = main(["repair", "--log", str(log), "--apply"])
    captured = capsys.readouterr()

    assert code == 1
    assert "остались незавершённые операции" in captured.err
    assert (root / "a.mp4").exists()
    assert (root / "b.mp4").exists()
    assert not log.exists()


def test_repair_exits_2_when_lock_is_busy(tmp_path, capsys):
    log, _step = crashed_temp_state(tmp_path)

    with RenamerLock(str(log)):
        code = main(["repair", "--log", str(log), "--apply"])

    assert code == 2
    assert "занят" in capsys.readouterr().err
    assert (tmp_path / "__ren_tmp_crash").exists()


def test_apply_cli_refuses_open_operation(tmp_path, capsys):
    root = tmp_path
    source = make_video(root / "a.mp4")
    step = make_step(
        root,
        "a.mp4",
        "b.mp4",
        "__ren_tmp_crash",
        oshash=oshash_from_path(str(source)),
        size=source.stat().st_size,
    )
    write_intent(
        str(root / "rename_log.csv"),
        kind="rename",
        steps=[step],
        log_rows=[make_row(root, step)],
    )
    csv_path = root / "preview.csv"
    write_preview_csv(
        str(csv_path),
        [
            PreviewRow(
                True,
                "a.mp4",
                "b.mp4",
                "fingerprint",
                1.0,
                "s1",
                "",
                step.oshash,
                source.stat().st_size,
            )
        ],
    )

    code = main(
        [
            "apply",
            str(csv_path),
            "--root",
            str(root),
            "--log",
            str(root / "rename_log.csv"),
            "--apply",
        ]
    )

    assert code == 2
    assert "незавершённая" in capsys.readouterr().err
    assert source.exists()


def test_repair_exits_2_on_corrupt_intent(tmp_path, capsys):
    log = tmp_path / "rename_log.csv"
    Path(intent_path(str(log))).write_text("broken\n", encoding="utf-8")

    code = main(["repair", "--log", str(log), "--apply"])

    assert code == 2
    assert "поврежд" in capsys.readouterr().err
