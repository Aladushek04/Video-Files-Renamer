#!/usr/bin/env python3
"""Нормализация строк для сравнения имён, названий и студий.

Одна функция на весь проект, чтобы матчер имён и скоринг сцен сравнивали
строки одинаково: NFKD, без диакритики, нижний регистр, всё
не-алфанумерическое — в пробелы.
"""

import re
import unicodedata

__all__ = ["normalize_for_match"]

_WS = re.compile(r"\s+")


def normalize_for_match(text: str) -> str:
    """Нормализует строку: NFKD, без диакритики, lower, пробелы."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    spaced = "".join(char.lower() if char.isalnum() else " " for char in without_marks)
    return _WS.sub(" ", spaced).strip()
