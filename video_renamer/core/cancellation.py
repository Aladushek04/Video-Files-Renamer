"""Общая отмена длительных операций для GUI, TUI и CLI."""

from __future__ import annotations

__all__ = ["OperationCancelled"]


class OperationCancelled(Exception):
    """Операция прервана пользователем через `cancel_event`.

    Живёт в ядре, чтобы фоновая обёртка (`gui.tasks.run_in_thread`) и сервис
    (`TitleService.preview`) понимали отмену одинаково: это не ошибка, её не
    нужно показывать красным.
    """
