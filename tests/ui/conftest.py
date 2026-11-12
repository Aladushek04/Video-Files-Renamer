"""Общие фикстуры GUI-тестов.

Tk нужен только тестам виджетов: если его нет (headless-сборка без дисплея),
эти тесты пропускаются, а тесты контроллера без Tk продолжают работать.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def ctk_root():
    """Скрытый CustomTkinter-корень; окно не показывается."""
    tkinter = pytest.importorskip("tkinter")
    ctk = pytest.importorskip("customtkinter")
    try:
        root = ctk.CTk()
    except (RuntimeError, tkinter.TclError) as exc:  # нет дисплея или сломанный Tk
        pytest.skip(f"Tk недоступен: {exc}")
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()
