"""Headless smoke-тесты Textual-интерфейса (нужен textual)."""
import asyncio

import pytest

pytest.importorskip("textual")

from title_service import PreviewReport, PreviewRow  # noqa: E402
from tui import RenamerApp  # noqa: E402

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
    from title_service import ApplyReport

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
