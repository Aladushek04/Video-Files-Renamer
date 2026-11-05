"""Headless smoke-тесты Textual-интерфейса (нужен textual)."""
import asyncio
import threading
import time
from dataclasses import replace

import pytest

pytest.importorskip("textual")

from video_renamer.core.stashdb_client import (  # noqa: E402
    Performer,
    Scene,
    Studio,
)
from video_renamer.core.oshash import oshash_from_path  # noqa: E402
from video_renamer.core.title_service import (  # noqa: E402
    ApplyReport,
    ApplyResult,
    PreviewReport,
    PreviewRow,
)
from video_renamer.tui import RenamerApp  # noqa: E402

ROWS = (
    PreviewRow(
        True, "old1.mp4", "Brazzers - 01.01.2025 - Title.mp4",
        "fingerprint", 1.0, "s1", "", "h1", 10,
    ),
    PreviewRow(
        False, "old2.mp4", "New Text.mp4", "text", 0.76, "s2",
        "text: score=0.760", "h2", 10,
    ),
    PreviewRow(
        False, "old3.mp4", "Heuristic.mp4", "heuristic", 0.0, "",
        "no match", "h3", 10,
    ),
)


def pick_scene(scene_id: str, title: str) -> Scene:
    return Scene(
        scene_id,
        title,
        "2026-01-02",
        None,
        None,
        1,
        Studio("st", "Studio"),
        (Performer("p", "Actor"),),
    )


ROWS_WITH_CANDIDATES = (
    ROWS[0],
    replace(
        ROWS[1],
        candidates=(pick_scene("alt1", "First Choice"),
                    pick_scene("alt2", "Second Choice")),
    ),
    ROWS[2],
)


def test_app_previews_and_toggles_rows(tmp_path):
    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=ROWS, warnings=(), had_transient_errors=False
            ),
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#table").row_count == 3
            assert app.model.stats()["approved"] == 1
            assert app.model.stats()["pending"] == 2

            await pilot.click("#table")
            await pilot.press("space")
            await pilot.pause()
            assert app.model.stats()["approved"] == 0

    asyncio.run(run())


def _rename_log_with(tmp_path, old="a.mp4", new="b.mp4"):
    from video_renamer.core.rename_log import LogEntry, append_log, format_log_timestamp

    log = tmp_path / "rename_log.csv"
    append_log(str(log), [LogEntry(
        timestamp=format_log_timestamp(), action="rename", undoes="",
        root=str(tmp_path), old=old, new=new, oshash=oshash_from_path(str(tmp_path / new)),
        size=100, scene_id="s1", source="fingerprint", legacy=False,
    )])
    return log


def test_undo_screen_lists_active_rows(tmp_path):
    (tmp_path / "b.mp4").write_bytes(b"b" * 100)
    log = _rename_log_with(tmp_path)

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path), undo_log_path=str(log))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#table")
            await pilot.press("u")
            await pilot.pause()
            assert app.screen.query_one("#undo-table").row_count == 1

    asyncio.run(run())


def test_legacy_undo_needs_checkbox(tmp_path):
    (tmp_path / "b.mp4").write_bytes(b"b" * 100)
    legacy = tmp_path / "rename_log.csv"
    legacy.write_text(
        "timestamp;old;new;oshash;scene_id;source\n"
        f"2026-11-04T12:00:00Z;a.mp4;b.mp4;{oshash_from_path(str(tmp_path / 'b.mp4'))};s;fingerprint\n",
        encoding="utf-8-sig",
    )

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path), undo_log_path=str(legacy))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#table")
            await pilot.press("u")
            await pilot.pause()
            await pilot.click("#undo-table")
            await pilot.press("space")
            await pilot.click("#undo-run")
            await pilot.pause()
            assert app.screen.query_one("#undo-confirm").disabled is True
            await pilot.click("#undo-legacy-ack")
            await pilot.pause()
            assert app.screen.query_one("#undo-confirm").disabled is False

    asyncio.run(run())


def test_undo_confirmed_restores_file(tmp_path):
    (tmp_path / "b.mp4").write_bytes(b"b" * 100)
    log = _rename_log_with(tmp_path)

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path), undo_log_path=str(log))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#table")
            await pilot.press("u")
            await pilot.pause()
            await pilot.click("#undo-table")
            await pilot.press("space")
            await pilot.click("#undo-run")
            await pilot.pause()
            await pilot.click("#undo-confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert (tmp_path / "a.mp4").exists()
            assert not (tmp_path / "b.mp4").exists()

    asyncio.run(run())


def test_app_apply_modal_runs_fake_apply(tmp_path):
    from video_renamer.core.title_service import ApplyReport

    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=ROWS, warnings=(), had_transient_errors=False
            ),
            apply_factory=lambda rows: ApplyReport(
                results=(), had_errors=False
            ),
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#apply")
            await pilot.pause()
            assert app.screen.query_one("#confirm-dialog")
            await pilot.click("#confirm-apply")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen.query_one("#result-dialog")

    asyncio.run(run())


def test_confirm_apply_explains_rename_is_irreversible(tmp_path):
    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=ROWS, warnings=(), had_transient_errors=False
            ),
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#apply")
            await pilot.pause()
            body = str(app.screen.query_one("#confirm-body").render())
            assert "нельзя" in body
            assert "без rename_log" not in body

    asyncio.run(run())


def test_apply_log_failure_does_not_hide_results(tmp_path, monkeypatch):
    import video_renamer.tui as tui_module

    def broken_log(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tui_module, "append_rename_log", broken_log)
    video = tmp_path / "old1.mp4"
    video.write_bytes(b"abcdefghij")
    row = PreviewRow(
        True, "old1.mp4", "new1.mp4", "fingerprint", 1.0, "s1", "",
        oshash_from_path(str(video)), 10,
    )

    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=(row,), warnings=(), had_transient_errors=False
            ),
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#apply")
            await pilot.pause()
            await pilot.click("#confirm-apply")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert (tmp_path / "new1.mp4").exists()
            assert app.screen.query_one("#result-dialog")
            assert "disk full" in str(app.screen.query_one("#log-error").render())

    asyncio.run(run())


def test_apply_keeps_failed_rows_for_retry(tmp_path):
    rows = (
        PreviewRow(True, "ok.mp4", "ok-new.mp4", "fingerprint", 1.0, "s1",
                   "", "h1", 10),
        PreviewRow(True, "bad.mp4", "bad-new.mp4", "text", 0.8, "s2",
                   "", "h2", 10),
        PreviewRow(False, "pending.mp4", "pending-new.mp4", "heuristic",
                   0.0, "", "", "h3", 10),
    )
    report = ApplyReport(
        results=(
            ApplyResult("ok.mp4", "ok-new.mp4", "renamed", ""),
            ApplyResult("bad.mp4", "bad-new.mp4", "error", "target is occupied"),
        ),
        had_errors=True,
    )

    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=rows, warnings=(), had_transient_errors=False
            ),
            apply_factory=lambda approved: report,
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#apply")
            await pilot.pause()
            await pilot.click("#confirm-apply")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert [row.old_rel for row in app.model.rows] == [
                "bad.mp4", "pending.mp4"
            ]
            assert app.model.rows[0].approved is True
            assert app.query_one("#table").row_count == 2

    asyncio.run(run())


def test_tui_finds_project_data_outside_project_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            status = str(app.query_one("#db-status").render())
            assert "не найден" not in status

    asyncio.run(run())


def test_table_fits_recommended_window(tmp_path):
    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=ROWS, warnings=(), had_transient_errors=False
            ),
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#table")
            assert table.virtual_size.width <= table.size.width

    asyncio.run(run())


def test_busy_blocks_edit_and_approval(tmp_path):
    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=ROWS_WITH_CANDIDATES,
                warnings=(),
                had_transient_errors=False,
            ),
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.model.rows[0].approved is True
            app._set_busy(True)
            app.action_toggle()
            app.action_edit()
            await pilot.pause()
            assert app.model.rows[0].approved is True
            assert not app.query("#name-dialog")
            for selector in (
                "#browse", "#folder", "#filter", "#approve", "#reject",
                "#edit", "#candidates", "#studios", "#preview", "#apply",
            ):
                assert app.query_one(selector).disabled, selector

    asyncio.run(run())


def test_preview_failure_keeps_apply_disabled(tmp_path):
    def boom(folder, progress):
        raise RuntimeError("boom")

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path), preview_factory=boom)
        async with app.run_test(size=(140, 40)) as pilot:
            assert app.query_one("#apply").disabled is True
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#apply").disabled is True

    asyncio.run(run())


def test_quit_asks_before_discarding_rows(tmp_path):
    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=ROWS, warnings=(), had_transient_errors=False
            ),
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            assert app.screen.query_one("#quit-dialog")
            await pilot.click("#quit-cancel")
            await pilot.pause()
            assert not app.query("#quit-dialog")
            assert len(app.model.rows) == 3

    asyncio.run(run())


def test_name_editor_accepts_uppercase_extension(tmp_path):
    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=ROWS, warnings=(), had_transient_errors=False
            ),
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#table")
            await pilot.click("#edit")
            await pilot.pause()
            app.screen.query_one("#new-name").value = "Custom.MP4"
            await pilot.click("#name-save")
            await pilot.pause()
            assert app.model.rows[0].new_rel == "Custom.MP4"
            assert app.model.rows[0].approved is True

    asyncio.run(run())


def test_name_editor_reports_too_long_name(tmp_path):
    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=ROWS, warnings=(), had_transient_errors=False
            ),
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#table")
            await pilot.click("#edit")
            await pilot.pause()
            app.screen.query_one("#new-name").value = "x" * 252 + ".mp4"
            await pilot.click("#name-save")
            await pilot.pause()
            message = str(app.screen.query_one("#name-error").render())
            assert "255" in message
            assert "Windows" not in message

    asyncio.run(run())


def test_edit_modal_updates_name_and_approves(tmp_path):
    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=ROWS, warnings=(), had_transient_errors=False
            ),
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#table")
            await pilot.click("#edit")
            await pilot.pause()
            assert app.screen.query_one("#name-dialog")
            app.screen.query_one("#new-name").value = "Custom.mp4"
            await pilot.click("#name-save")
            await pilot.pause()
            assert app.model.rows[0].new_rel == "Custom.mp4"
            assert app.model.rows[0].approved is True

    asyncio.run(run())


def test_candidate_picker_chooses_alternative(tmp_path):
    class FakeService:
        def build_alternative(self, row, scene, rows):
            return replace(
                row,
                new_rel=f"Chosen {scene.id}.mp4",
                scene_id=scene.id,
                source="text",
                approved=True,
            )

    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=ROWS_WITH_CANDIDATES,
                warnings=(),
                had_transient_errors=False,
            ),
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            app._service = FakeService()
            await pilot.click("#table")
            await pilot.press("down")
            await pilot.click("#candidates")
            await pilot.pause()
            assert app.screen.query_one("#candidate-dialog")
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert app.model.rows[1].new_rel.startswith("Chosen alt")
            assert app.model.rows[1].approved is True

    asyncio.run(run())


def test_studio_dialog_runs_task(tmp_path, monkeypatch):
    import video_renamer.tui as tui_module

    seen = []

    def fake_task(kind, on_line, cancel_event=None):
        seen.append(kind)
        on_line("fake studio output")
        return 0

    monkeypatch.setattr(tui_module, "run_studio_task", fake_task)

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#studios")
            await pilot.pause()
            assert app.screen.query_one("#studio-dialog")
            await pilot.click("#studio-stashdb")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert seen == ["stashdb"]
            await pilot.click("#studio-close")
            await pilot.pause()
            assert not app.query("#studio-dialog")

    asyncio.run(run())


def test_studio_dialog_cancels_task(tmp_path, monkeypatch):
    import video_renamer.tui as tui_module

    started = threading.Event()

    def fake_task(kind, on_line, cancel_event=None):
        started.set()
        while not cancel_event.is_set():
            time.sleep(0.02)
        return 130

    monkeypatch.setattr(tui_module, "run_studio_task", fake_task)

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#studios")
            await pilot.pause()
            await pilot.click("#studio-stashdb")
            await asyncio.wait_for(asyncio.to_thread(started.wait), 2)
            await pilot.click("#studio-cancel")
            await app.workers.wait_for_complete()
            await pilot.pause()
            screen = app.screen
            assert screen.query_one("#studio-close").disabled is False
            assert screen.query_one("#studio-cancel").disabled is True
            await pilot.click("#studio-close")
            await pilot.pause()
            assert not app.query("#studio-dialog")

    asyncio.run(run())
