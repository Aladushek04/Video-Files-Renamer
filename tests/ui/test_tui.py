"""Headless smoke-тесты Textual-интерфейса (нужен textual)."""

import asyncio
import threading
import time
from dataclasses import replace

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, Input, OptionList

from video_renamer.core.oshash import oshash_from_path
from video_renamer.core.stashdb_client import (
    Performer,
    Scene,
    Studio,
)
from video_renamer.core.title_service import (
    ApplyReport,
    ApplyResult,
    PreviewReport,
    PreviewRow,
)
from video_renamer.ui.tui import DirectoryPicker, RenamerApp

ROWS = (
    PreviewRow(
        True,
        "old1.mp4",
        "Brazzers - 01.01.2025 - Title.mp4",
        "fingerprint",
        1.0,
        "s1",
        "",
        "h1",
        10,
    ),
    PreviewRow(
        False,
        "old2.mp4",
        "New Text.mp4",
        "text",
        0.76,
        "s2",
        "text: score=0.760",
        "h2",
        10,
    ),
    PreviewRow(
        False,
        "old3.mp4",
        "Heuristic.mp4",
        "heuristic",
        0.0,
        "",
        "no match",
        "h3",
        10,
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
        candidates=(pick_scene("alt1", "First Choice"), pick_scene("alt2", "Second Choice")),
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
    append_log(
        str(log),
        [
            LogEntry(
                timestamp=format_log_timestamp(),
                action="rename",
                undoes="",
                root=str(tmp_path),
                old=old,
                new=new,
                oshash=oshash_from_path(str(tmp_path / new)),
                size=100,
                scene_id="s1",
                source="fingerprint",
                legacy=False,
            )
        ],
    )
    return log


async def _wait_undo_rows(app, pilot, expected: int = 1):
    """Ждёт, пока экран журнала загрузит строки.

    `on_mount` читает журнал синхронно, но под нагрузкой экран может открыться
    позже, чем тест нажмёт пробел: без ожидания выбор остаётся пустым.
    """
    for _ in range(100):
        tables = app.screen.query("#undo-table")
        if tables and tables.first().row_count >= expected:
            return tables.first()
        await pilot.pause()
    raise AssertionError("таблица журнала не заполнилась")


async def _select_undo_row(app, pilot) -> None:
    """Отмечает первую строку журнала и ждёт, пока отметка действительно появится.

    Курсор и фокус таблицы могут отставать от строк: пробел, отправленный
    слишком рано, уходит в никуда, и выбора нет. Повторяем, пока ячейка не
    станет `[x]`, чтобы тест не зависел от скорости отрисовки.
    """
    table = await _wait_undo_rows(app, pilot)
    for _ in range(50):
        table.focus()
        table.move_cursor(row=0)
        await pilot.press("space")
        await pilot.pause()
        if str(table.get_row_at(0)[0]) == "[x]":
            return
    raise AssertionError("строка журнала не отмечена")


async def _wait_status(app, pilot, text: str) -> None:
    """Ждёт появления текста в статусе текущего экрана журнала."""
    for _ in range(100):
        message = getattr(app.screen, "status_message", "")
        if text in message:
            return
        await pilot.pause()
    message = getattr(app.screen, "status_message", "")
    raise AssertionError(f"в статусе нет {text!r}: {message!r}")


def test_undo_screen_lists_active_rows(tmp_path):
    (tmp_path / "b.mp4").write_bytes(b"b" * 100)
    log = _rename_log_with(tmp_path)

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path), log_path=str(log))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#table")
            await pilot.press("u")
            await pilot.pause()
            table = await _wait_undo_rows(app, pilot)
            assert table.row_count == 1

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
        app = RenamerApp(folder=str(tmp_path), log_path=str(legacy))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#table")
            await pilot.press("u")
            await _select_undo_row(app, pilot)
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
        app = RenamerApp(folder=str(tmp_path), log_path=str(log))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#table")
            await pilot.press("u")
            await _select_undo_row(app, pilot)
            await pilot.click("#undo-run")
            await pilot.pause()
            await pilot.click("#undo-confirm")
            await app.workers.wait_for_complete()
            await _wait_status(app, pilot, "Откачено: 1")
            assert (tmp_path / "a.mp4").exists()
            assert not (tmp_path / "b.mp4").exists()

    asyncio.run(run())


def test_undo_screen_survives_unreadable_log(tmp_path, monkeypatch):
    import csv as csv_module

    from video_renamer.ui import tui as tui_module

    def broken(path):
        raise csv_module.Error("line too long")

    monkeypatch.setattr(tui_module, "read_log", broken)
    log = tmp_path / "rename_log.csv"
    log.write_text("x", encoding="utf-8")

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path), log_path=str(log))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#table")
            await pilot.press("u")
            await pilot.pause()
            assert "Не удалось прочитать журнал" in app.screen.status_message

    asyncio.run(run())


def test_undo_worker_error_is_shown_not_fatal(tmp_path, monkeypatch):
    from video_renamer.core.undo import run_undo as real_run_undo
    from video_renamer.ui import tui as tui_module

    def failing(selection, *, log_path, dry_run, now=None):
        if dry_run:
            return real_run_undo(selection, log_path=log_path, dry_run=True, now=now)
        raise RuntimeError("disk gone")

    monkeypatch.setattr(tui_module, "run_undo", failing)
    (tmp_path / "b.mp4").write_bytes(b"b" * 100)
    log = _rename_log_with(tmp_path)

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path), log_path=str(log))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#table")
            await pilot.press("u")
            await _select_undo_row(app, pilot)
            await pilot.click("#undo-run")
            await pilot.pause()
            await pilot.click("#undo-confirm")
            await app.workers.wait_for_complete()
            assert app.is_running
            await _wait_status(app, pilot, "Откат не выполнен: disk gone")

    asyncio.run(run())


def test_undo_shows_unwritten_journal_rows(tmp_path, monkeypatch):
    from video_renamer.core.undo import UndoReport, UndoResult
    from video_renamer.core.undo import run_undo as real_run_undo
    from video_renamer.ui import tui as tui_module

    def unwritten(selection, *, log_path, dry_run, now=None):
        if dry_run:
            return real_run_undo(selection, log_path=log_path, dry_run=True, now=now)
        assert selection.targets, "строка журнала не выбрана"
        target = selection.targets[0].entry
        return UndoReport(
            (
                UndoResult(
                    target.id,
                    target.new,
                    target.old,
                    "journal-missing",
                    "откат выполнен, журнал не записан: locked",
                ),
            ),
            True,
        )

    monkeypatch.setattr(tui_module, "run_undo", unwritten)
    (tmp_path / "b.mp4").write_bytes(b"b" * 100)
    log = _rename_log_with(tmp_path)

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path), log_path=str(log))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#table")
            await pilot.press("u")
            await _select_undo_row(app, pilot)
            await pilot.click("#undo-run")
            await pilot.pause()
            await pilot.click("#undo-confirm")
            await app.workers.wait_for_complete()
            await _wait_status(app, pilot, "журнал не записан")

    asyncio.run(run())


def test_undo_screen_ignores_callbacks_after_close(tmp_path):
    from video_renamer.ui.tui import UndoLogScreen

    screen = UndoLogScreen(str(tmp_path / "x.csv"), str(tmp_path))
    screen._undo_done(1, [])
    screen._undo_failed("late error")


def test_legacy_confirm_shows_root_folder(tmp_path):
    (tmp_path / "b.mp4").write_bytes(b"b" * 100)
    legacy = tmp_path / "rename_log.csv"
    legacy.write_text(
        "timestamp;old;new;oshash;scene_id;source\n"
        f"2026-11-04T12:00:00Z;a.mp4;b.mp4;{oshash_from_path(str(tmp_path / 'b.mp4'))};s;fingerprint\n",
        encoding="utf-8-sig",
    )

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path), log_path=str(legacy))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#table")
            await pilot.press("u")
            await pilot.pause()
            await pilot.click("#undo-table")
            await pilot.press("space")
            await pilot.click("#undo-run")
            await pilot.pause()
            assert str(tmp_path) in app.screen.body_text

    asyncio.run(run())


def test_app_apply_modal_runs_fake_apply(tmp_path):
    from video_renamer.core.title_service import ApplyReport

    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=ROWS, warnings=(), had_transient_errors=False
            ),
            apply_factory=lambda rows: ApplyReport(results=(), had_errors=False),
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


def test_confirm_apply_explains_rename_is_immediate_with_undo_conditions(tmp_path):
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
            # Честный контракт: действие немедленное, откат — только через
            # журнал и при неизменённых файлах.
            assert "отменить его нельзя" not in body
            assert "выполняется сразу" in body
            assert "журнал" in body.casefold()
            assert "не менялись" in body
            assert "без rename_log" not in body

    asyncio.run(run())


def test_apply_log_failure_does_not_hide_results(tmp_path, monkeypatch):
    from video_renamer.core import review_actions

    def broken_log(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(review_actions, "append_rename_log", broken_log)
    video = tmp_path / "old1.mp4"
    video.write_bytes(b"abcdefghij")
    row = PreviewRow(
        True,
        "old1.mp4",
        "new1.mp4",
        "fingerprint",
        1.0,
        "s1",
        "",
        oshash_from_path(str(video)),
        10,
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
        PreviewRow(True, "ok.mp4", "ok-new.mp4", "fingerprint", 1.0, "s1", "", "h1", 10),
        PreviewRow(True, "bad.mp4", "bad-new.mp4", "text", 0.8, "s2", "", "h2", 10),
        PreviewRow(False, "pending.mp4", "pending-new.mp4", "heuristic", 0.0, "", "", "h3", 10),
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
            assert [row.old_rel for row in app.model.rows] == ["bad.mp4", "pending.mp4"]
            assert app.model.rows[0].approved is True
            assert app.query_one("#table").row_count == 2
            # Неодобренная строка не должна считаться ошибкой apply.
            status = str(app.query_one("#status").render())
            assert status == "Применено: 1; строк с ошибками: 1"

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


def test_tui_shows_missing_and_broken_catalog_distinctly(tmp_path, monkeypatch):
    from video_renamer.core import review_actions

    missing = tmp_path / "missing.json"
    monkeypatch.setattr(review_actions, "resolve_studios_path", lambda: missing)

    async def run_missing() -> None:
        app = RenamerApp(folder=str(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            status = str(app.query_one("#db-status").render())
            assert "не найден" in status

    asyncio.run(run_missing())

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(review_actions, "resolve_studios_path", lambda: broken)

    async def run_broken() -> None:
        app = RenamerApp(folder=str(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            status = str(app.query_one("#db-status").render())
            assert "повреждён" in status
            assert "не найден" not in status

    asyncio.run(run_broken())


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


TOOLBAR_ACTIONS = (
    "#filter",
    "#candidates",
    "#edit",
    "#approve",
    "#reject",
    "#apply",
)


def test_toolbar_actions_fit_the_minimum_window(tmp_path):
    """При 140×40 ни одно действие не выходит за правый край экрана."""

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            width = app.screen.size.width
            for selector in TOOLBAR_ACTIONS:
                widget = app.query_one(selector)
                region = widget.region
                assert widget.display, selector
                assert region.width > 0 and region.height > 0, selector
                assert region.x >= 0, selector
                assert region.right <= width, (selector, region, width)

    asyncio.run(run())


def test_toolbar_actions_are_reachable_by_tab(tmp_path):
    """Каждое действие можно получить только клавиатурой, не только мышью."""

    async def run() -> None:
        scene = Scene("s1", "Title", "2026-01-01", None, None, None, Studio("st", "Studio"), ())
        row = PreviewRow(True, "a.mp4", "b.mp4", "text", 0.9, "s1", "", "h1", 10, (scene,))
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=(row,), warnings=(), had_transient_errors=False
            ),
        )
        async with app.run_test(size=(140, 40)) as pilot:
            # Загружаем строки: без выбора и одобрения часть действий disabled
            # и, как положено, не попадает в цепочку фокуса.
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            wanted = {
                "#browse",
                "#preview",
                "#studios",
                *(selector for selector in TOOLBAR_ACTIONS),
            }
            seen: set[str] = set()
            for _ in range(80):
                await pilot.press("tab")
                await pilot.pause()
                focused = app.focused
                if focused is not None and focused.id:
                    seen.add(f"#{focused.id}")
                if wanted <= seen:
                    break
            assert wanted <= seen, wanted - seen

    asyncio.run(run())


def test_apply_confirmation_promises_undo_only_with_journal(tmp_path):
    report = ApplyReport(
        results=(ApplyResult("a.mp4", "b.mp4", "renamed", ""),),
        had_errors=False,
    )

    async def run() -> None:
        app = RenamerApp(
            folder=str(tmp_path),
            preview_factory=lambda folder, progress: PreviewReport(
                rows=(PreviewRow(True, "a.mp4", "b.mp4", "fingerprint", 1.0, "s1", "", "h1", 100),),
                warnings=(),
                had_transient_errors=False,
            ),
            apply_factory=lambda approved: report,
        )
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#apply")
            await pilot.pause()
            body = str(app.screen.query_one("#confirm-body").render())
            assert "отменить его нельзя" not in body
            assert "журнал" in body.casefold()
            assert "не менялись" in body
            await pilot.click("#confirm-cancel")
            await pilot.pause()
            assert not (tmp_path / "b.mp4").exists()

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
                "#browse",
                "#folder",
                "#filter",
                "#approve",
                "#reject",
                "#edit",
                "#candidates",
                "#studios",
                "#preview",
                "#apply",
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
    import video_renamer.ui.tui as tui_module

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


def test_folder_input_is_empty_and_has_placeholder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def run() -> None:
        app = RenamerApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            folder = app.query_one("#folder", Input)
            assert folder.value == ""
            assert folder.placeholder == "Выберите папку"

    asyncio.run(run())


def test_directory_picker_navigates_into_subfolder_and_chooses_it(tmp_path):
    target = tmp_path / "Videos"
    target.mkdir()

    async def run() -> None:
        app = RenamerApp(folder=str(tmp_path))
        async with app.run_test(size=(140, 40)) as pilot:
            app.action_browse()
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, DirectoryPicker)

            options = picker.query_one("#picker-list", OptionList)
            index = next(
                i
                for i in range(options.option_count)
                if options.get_option_at_index(i).id == str(target)
            )
            options.highlighted = index
            options.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert picker.path == target

            await pilot.click("#picker-choose")
            await pilot.pause()
            assert app.folder == target
            assert app.query_one("#folder", Input).value == str(target)

    asyncio.run(run())


def test_directory_picker_goes_up_to_parent(tmp_path):
    start = tmp_path / "child"
    start.mkdir()

    async def run() -> None:
        app = RenamerApp(folder=str(start))
        async with app.run_test(size=(140, 40)) as pilot:
            app.action_browse()
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, DirectoryPicker)
            options = picker.query_one("#picker-list", OptionList)
            options.highlighted = 0
            options.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert picker.path == tmp_path

    asyncio.run(run())


def test_studio_dialog_cancels_task(tmp_path, monkeypatch):
    import video_renamer.ui.tui as tui_module

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


def _folders(tmp_path):
    folder_a = tmp_path / "A"
    folder_b = tmp_path / "B"
    folder_a.mkdir()
    folder_b.mkdir()
    return folder_a, folder_b


def _preview_rows(folder, progress):
    return PreviewReport(rows=ROWS, warnings=(), had_transient_errors=False)


async def _submit_folder(pilot, app, folder: str) -> None:
    folder_input = app.query_one("#folder", Input)
    folder_input.focus()
    folder_input.value = folder
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


def test_folder_switch_with_rows_asks_and_discards(tmp_path):
    folder_a, folder_b = _folders(tmp_path)

    async def run() -> None:
        app = RenamerApp(folder=str(folder_a), preview_factory=_preview_rows)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.model.folder == str(folder_a)
            assert app.query_one("#table").row_count == len(ROWS)

            await _submit_folder(pilot, app, str(folder_b))
            assert app.screen.query_one("#folder-dialog")

            await pilot.click("#folder-confirm")
            await pilot.pause()

            assert app.model.rows == []
            assert app.model.folder == ""
            assert app.folder == folder_b
            assert app.query_one("#folder", Input).value == str(folder_b)
            assert app.query_one("#table").row_count == 0
            assert app.query_one("#apply", Button).disabled

    asyncio.run(run())


def test_cancelled_folder_switch_keeps_review(tmp_path):
    folder_a, folder_b = _folders(tmp_path)

    async def run() -> None:
        app = RenamerApp(folder=str(folder_a), preview_factory=_preview_rows)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()

            await _submit_folder(pilot, app, str(folder_b))
            await pilot.click("#folder-cancel")
            await pilot.pause()

            assert app.folder == folder_a
            assert app.model.folder == str(folder_a)
            assert len(app.model.rows) == len(ROWS)
            assert app.query_one("#folder", Input).value == str(folder_a)
            assert not app.query_one("#apply", Button).disabled

    asyncio.run(run())


def test_same_folder_submit_keeps_review(tmp_path):
    folder_a, _folder_b = _folders(tmp_path)

    async def run() -> None:
        app = RenamerApp(folder=str(folder_a), preview_factory=_preview_rows)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()

            await _submit_folder(pilot, app, str(folder_a) + "/.")

            assert not app.query("#folder-dialog")
            assert app.model.folder == str(folder_a)
            assert len(app.model.rows) == len(ROWS)

    asyncio.run(run())


def test_failed_preview_for_new_folder_leaves_no_stale_review(tmp_path):
    folder_a, folder_b = _folders(tmp_path)

    def preview(folder, progress):
        if folder == str(folder_a):
            return PreviewReport(rows=ROWS, warnings=(), had_transient_errors=False)
        raise RuntimeError("network down")

    async def run() -> None:
        app = RenamerApp(folder=str(folder_a), preview_factory=preview)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.model.folder == str(folder_a)

            await _submit_folder(pilot, app, str(folder_b))
            await pilot.click("#folder-confirm")
            await pilot.pause()
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert app.folder == folder_b
            assert app.model.rows == []
            assert app.model.folder == ""
            assert app.query_one("#table").row_count == 0
            assert app.query_one("#apply", Button).disabled

    asyncio.run(run())


def test_preview_for_switched_folder_does_not_rebind_old_rows(tmp_path):
    folder_a, folder_b = _folders(tmp_path)

    async def run() -> None:
        app = RenamerApp(folder=str(folder_a), preview_factory=_preview_rows)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.click("#preview")
            await app.workers.wait_for_complete()
            await pilot.pause()

            app.folder = folder_b
            app._preview_done(
                str(folder_a),
                PreviewReport(rows=ROWS, warnings=(), had_transient_errors=False),
            )
            await pilot.pause()

            assert app.model.folder == str(folder_a)
            assert len(app.model.rows) == len(ROWS)
            app.action_apply()
            await pilot.pause()
            assert not app.query("#confirm-dialog")

    asyncio.run(run())
