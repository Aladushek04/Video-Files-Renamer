"""Метаданные пакета: лицензия по PEP 639 (A16).

Проект объявляет `license` как SPDX-выражение, а не устаревшую таблицу
`{ text = ... }`, и не дублирует её legacy-классификатором. Такой формат
понимает setuptools 77+; нижняя граница в build-system проверяется отдельно.

Проверки читают `pyproject.toml` текстом, чтобы работать и на Python 3.10, где
ещё нет `tomllib`; настоящие метаданные колеса проверяет `test_wheel_install`.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "pyproject.toml"


def _first(pattern: str) -> str | None:
    match = re.search(pattern, CONFIG.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def test_license_is_an_spdx_expression():
    assert _first(r'^license = "([^"]+)"$') == "MIT"


def test_no_legacy_license_classifier():
    text = CONFIG.read_text(encoding="utf-8")
    assert re.findall(r'^\s*"License :: ([^"]+)"', text, re.MULTILINE) == []


def test_setuptools_floor_supports_pep639():
    requires = _first(r"^requires = \[([^\]]*)\]") or ""
    match = re.search(r"setuptools>=(\d+)", requires)
    assert match, requires
    assert int(match.group(1)) >= 77


def test_license_file_is_present():
    assert (ROOT / "LICENSE").is_file()
