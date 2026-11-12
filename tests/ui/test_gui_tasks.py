"""Тесты доставки результатов из фоновых потоков в Tk-поток.

Tk здесь не нужен: `UiDispatcher` общается с виджетом только через
`after`/`after_cancel`, поэтому виджет подменяется очередью колбэков.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from video_renamer.gui.tasks import OperationCancelled, UiDispatcher, run_in_thread


class FakeWidget:
    """Заменяет Tk-виджет: хранит запланированные `after`-колбэки."""

    def __init__(self) -> None:
        self.jobs: dict[str, Callable[[], None]] = {}
        self._counter = 0

    def after(self, _interval: int, callback: Callable[[], None]) -> str:
        self._counter += 1
        job = f"after-{self._counter}"
        self.jobs[job] = callback
        return job

    def after_cancel(self, job: str) -> None:
        self.jobs.pop(job, None)

    def run_pending(self) -> None:
        """Один проход цикла Tk: выполняет запланированные колбэки."""
        for job in list(self.jobs):
            callback = self.jobs.pop(job, None)
            if callback is not None:
                callback()


def make_dispatcher() -> tuple[FakeWidget, UiDispatcher]:
    widget = FakeWidget()
    return widget, UiDispatcher(widget, interval_ms=1)


def wait_for(thread: threading.Thread) -> None:
    thread.join(timeout=5)
    assert not thread.is_alive(), "фоновый поток не завершился за 5 секунд"


def test_call_soon_runs_only_on_tk_loop():
    widget, dispatcher = make_dispatcher()
    seen: list[str] = []

    dispatcher.call_soon(seen.append, "first")
    assert seen == []

    widget.run_pending()
    assert seen == ["first"]


def test_failing_callback_does_not_stop_the_queue(capsys):
    widget, dispatcher = make_dispatcher()
    seen: list[int] = []

    def boom() -> None:
        raise RuntimeError("boom")

    dispatcher.call_soon(boom)
    dispatcher.call_soon(seen.append, 2)

    widget.run_pending()

    assert seen == [2]
    assert "RuntimeError" in capsys.readouterr().err


def test_stop_cancels_scheduled_drain_and_drops_new_calls():
    widget, dispatcher = make_dispatcher()
    seen: list[str] = []
    dispatcher.call_soon(seen.append, "queued")

    dispatcher.stop()
    assert widget.jobs == {}
    widget.run_pending()

    dispatcher.call_soon(seen.append, "late")
    widget.run_pending()

    assert seen == []


def test_run_in_thread_posts_success_result():
    widget, dispatcher = make_dispatcher()
    results: list[str] = []

    thread = run_in_thread(lambda: "done", dispatcher=dispatcher, on_success=results.append)

    assert thread.daemon is True
    wait_for(thread)
    assert results == []
    widget.run_pending()
    assert results == ["done"]


def test_run_in_thread_posts_error():
    widget, dispatcher = make_dispatcher()
    errors: list[Exception] = []

    def work():
        raise ValueError("bad input")

    thread = run_in_thread(work, dispatcher=dispatcher, on_error=errors.append)

    wait_for(thread)
    widget.run_pending()

    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)


def test_cancel_before_start_skips_work():
    widget, dispatcher = make_dispatcher()
    cancel = threading.Event()
    cancel.set()
    ran: list[int] = []
    cancelled: list[int] = []

    thread = run_in_thread(
        lambda: ran.append(1),
        dispatcher=dispatcher,
        on_success=ran.append,
        on_cancelled=lambda: cancelled.append(1),
        cancel_event=cancel,
    )

    wait_for(thread)
    widget.run_pending()

    assert ran == []
    assert cancelled == [1]


def test_cancel_after_work_reports_cancelled_instead_of_success():
    widget, dispatcher = make_dispatcher()
    cancel = threading.Event()
    succeeded: list[str] = []
    cancelled: list[int] = []

    def work() -> str:
        cancel.set()
        return "late"

    thread = run_in_thread(
        work,
        dispatcher=dispatcher,
        on_success=succeeded.append,
        on_error=lambda exc: succeeded.append(str(exc)),
        on_cancelled=lambda: cancelled.append(1),
        cancel_event=cancel,
    )

    wait_for(thread)
    widget.run_pending()

    assert succeeded == []
    assert cancelled == [1]


def test_operation_cancelled_is_shared_with_core():
    from video_renamer.core.cancellation import OperationCancelled as CoreCancelled

    assert OperationCancelled is CoreCancelled


def test_operation_cancelled_raises_maps_to_on_cancelled():
    widget, dispatcher = make_dispatcher()
    errors: list[Exception] = []
    cancelled: list[int] = []

    def work():
        raise OperationCancelled

    thread = run_in_thread(
        work,
        dispatcher=dispatcher,
        on_error=errors.append,
        on_cancelled=lambda: cancelled.append(1),
    )

    wait_for(thread)
    widget.run_pending()

    assert errors == []
    assert cancelled == [1]
