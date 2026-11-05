"""Headless smoke-тесты Textual-интерфейса (нужен textual)."""
import asyncio
from dataclasses import replace

import pytest

pytest.importorskip("textual")

from video_renamer.core.stashdb_client import (  # noqa: E402
    Performer,
    Scene,
    Studio,
)
from video_renamer.core.title_service import PreviewReport, PreviewRow  # noqa: E402
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

    def fake_task(kind, on_line):
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
