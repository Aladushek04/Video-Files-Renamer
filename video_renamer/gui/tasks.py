"""Безопасная доставка результатов фоновых потоков в Tk-поток.

Tkinter нельзя обновлять из произвольного потока. Воркеры складывают
callables в очередь, а главное окно разбирает её через `after`.
"""

from __future__ import annotations

import queue
import threading
import traceback
from collections.abc import Callable
from typing import Any

from video_renamer.core.cancellation import OperationCancelled

Callback = Callable[..., Any]


class UiDispatcher:
    """Очередь вызовов, выполняемых только в главном потоке Tk."""

    def __init__(self, widget: Any, interval_ms: int = 40) -> None:
        self._widget = widget
        self._interval_ms = interval_ms
        self._queue: queue.Queue[tuple[Callback, tuple[Any, ...], dict[str, Any]]] = queue.Queue()
        self._stopped = False
        self._after_id: str | None = widget.after(interval_ms, self._drain)

    def call_soon(self, callback: Callback, *args: Any, **kwargs: Any) -> None:
        """Ставит вызов в очередь; вызывать можно из любого потока."""
        if not self._stopped:
            self._queue.put((callback, args, kwargs))

    def _drain(self) -> None:
        if self._stopped:
            return
        while True:
            try:
                callback, args, kwargs = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args, **kwargs)
            except Exception:  # ошибка одного обработчика не должна ломать цикл
                traceback.print_exc()
        try:
            self._after_id = self._widget.after(self._interval_ms, self._drain)
        except Exception:
            self._stopped = True

    def stop(self) -> None:
        self._stopped = True
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None


def run_in_thread(
    work: Callable[[], Any],
    *,
    dispatcher: UiDispatcher,
    on_success: Callable[[Any], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
    on_cancelled: Callable[[], None] | None = None,
    cancel_event: threading.Event | None = None,
    name: str = "video-renamer-worker",
) -> threading.Thread:
    """Выполняет `work` в daemon-потоке и возвращает результат в Tk-поток.

    Отмена кооперативная: `cancel_event` проверяется до старта и после
    завершения `work`; долгие операции должны сами проверять его в своих
    progress-callback'ах (см. `RenamerGUI._dispatch_progress`).
    """

    def target() -> None:
        if cancel_event is not None and cancel_event.is_set():
            if on_cancelled is not None:
                dispatcher.call_soon(on_cancelled)
            return
        try:
            result = work()
        except OperationCancelled:
            if on_cancelled is not None:
                dispatcher.call_soon(on_cancelled)
            return
        except Exception as exc:  # фоновая ошибка не должна убивать интерфейс
            if cancel_event is not None and cancel_event.is_set() and on_cancelled is not None:
                dispatcher.call_soon(on_cancelled)
            elif on_error is not None:
                dispatcher.call_soon(on_error, exc)
            else:
                traceback.print_exc()
            return
        if cancel_event is not None and cancel_event.is_set():
            if on_cancelled is not None:
                dispatcher.call_soon(on_cancelled)
            return
        if on_success is not None:
            dispatcher.call_soon(on_success, result)

    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread
