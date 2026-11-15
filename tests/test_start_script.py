"""Тесты PowerShell-лаунчера: код возврата CLI не смешивается с его выводом.

Скрипт запускается отдельным процессом `powershell -File`: проверяется настоящее
поведение `Invoke-Cli`, `Invoke-Preview` и `Invoke-Apply`, а не изолированное
сравнение. Внешние CLI-вызовы подменяются: либо заглушкой `python.cmd` на PATH,
либо переопределённой функцией `Invoke-Cli` внутри тестового сценария.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "start.ps1"

pytestmark = pytest.mark.skipif(
    POWERSHELL is None or os.name != "nt",
    reason="нужен Windows PowerShell",
)


def _run(harness: Path, tmp_path: Path, *, path_prefix: Path | None = None, env=None):
    environment = os.environ.copy()
    environment["VIDEO_RENAMER_PS_NO_MAIN"] = "1"
    if path_prefix is not None:
        environment["PATH"] = os.pathsep.join([str(path_prefix), environment.get("PATH", "")])
    environment.update(env or {})
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
        ],
        check=False,
        cwd=str(tmp_path),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def _harness(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "harness.ps1"
    path.write_text(
        textwrap.dedent(
            f"""
            $ErrorActionPreference = "Stop"
            $env:VIDEO_RENAMER_PS_NO_MAIN = "1"
            . "{SCRIPT}"

            """
        )
        + textwrap.dedent(body),
        encoding="utf-8-sig",
    )
    return path


def _stub_python(tmp_path: Path) -> Path:
    """`python.cmd` и `py.cmd` с заданным выводом и кодом через переменные среды."""
    directory = tmp_path / "stub"
    directory.mkdir()
    body = (
        "@echo off\r\n"
        'if defined STUB_ARGS_FILE echo %* > "%STUB_ARGS_FILE%"\r\n'
        "if defined STUB_TEXT echo %STUB_TEXT%\r\n"
        "if defined STUB_TEXT2 echo %STUB_TEXT2%\r\n"
        "if defined STUB_STDERR echo %STUB_STDERR% 1>&2\r\n"
        "exit /b %STUB_EXIT%\r\n"
    )
    for name in ("python.cmd", "py.cmd"):
        (directory / name).write_text(body, encoding="ascii")
    return directory


def _last_line(result: subprocess.CompletedProcess[str], prefix: str) -> str:
    for line in reversed(result.stdout.splitlines()):
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise AssertionError(f"нет строки {prefix!r} в выводе:\n{result.stdout}\n{result.stderr}")


def test_invoke_cli_returns_only_exit_code(tmp_path):
    stub = _stub_python(tmp_path)
    harness = _harness(
        tmp_path,
        """
        $code = Invoke-Cli @("preview", "D:\\Videos with space", "--csv", "preview.csv")
        "CODE=" + $code
        "TYPE=" + $code.GetType().Name
        "NEZERO=" + ($code -ne 0)
        """,
    )

    result = _run(
        harness,
        tmp_path,
        path_prefix=stub,
        env={"STUB_EXIT": "0", "STUB_TEXT": "1 row written to preview.csv"},
    )

    assert result.returncode == 0, result.stderr
    assert "1 row written to preview.csv" in result.stdout
    assert _last_line(result, "CODE=") == "0"
    assert _last_line(result, "TYPE=") == "Int32"
    assert _last_line(result, "NEZERO=") == "False"


def test_invoke_cli_reports_multiline_output_and_stderr(tmp_path):
    stub = _stub_python(tmp_path)
    harness = _harness(
        tmp_path,
        """
        $code = Invoke-Cli @("apply", "preview.csv", "--root", "D:\\Videos", "--apply")
        "CODE=" + $code
        """,
    )

    result = _run(
        harness,
        tmp_path,
        path_prefix=stub,
        env={
            "STUB_EXIT": "2",
            "STUB_TEXT": "first line",
            "STUB_TEXT2": "second line",
            "STUB_STDERR": "error: cannot read csv",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "first line" in result.stdout
    assert "second line" in result.stdout
    assert "error: cannot read csv" in (result.stdout + result.stderr)
    assert _last_line(result, "CODE=") == "2"


def test_invoke_cli_with_empty_output_keeps_exit_code(tmp_path):
    stub = _stub_python(tmp_path)
    harness = _harness(
        tmp_path,
        """
        $code = Invoke-Cli @("preview", "D:\\Videos", "--csv", "preview.csv")
        "CODE=" + $code
        "TYPE=" + $code.GetType().Name
        """,
    )

    result = _run(harness, tmp_path, path_prefix=stub, env={"STUB_EXIT": "1"})

    assert result.returncode == 0, result.stderr
    assert _last_line(result, "CODE=") == "1"
    assert _last_line(result, "TYPE=") == "Int32"


def test_invoke_cli_passes_arguments_with_spaces(tmp_path):
    stub = _stub_python(tmp_path)
    args_file = tmp_path / "args.txt"
    harness = _harness(
        tmp_path,
        """
        $null = Invoke-Cli @("preview", "D:\\Videos with space\\Подпапка", "--csv", "preview.csv")
        "DONE"
        """,
    )

    result = _run(
        harness,
        tmp_path,
        path_prefix=stub,
        env={"STUB_EXIT": "0", "STUB_ARGS_FILE": str(args_file)},
    )

    assert "DONE" in result.stdout
    captured = args_file.read_text(encoding="utf-8", errors="replace")
    assert "Videos with space" in captured
    assert "--csv preview.csv" in captured


def test_invoke_cli_supports_py_launcher(tmp_path):
    stub = _stub_python(tmp_path)
    harness = _harness(
        tmp_path,
        """
        $python = "py"
        $code = Invoke-Cli @("preview", "D:\\Videos", "--csv", "preview.csv")
        "CODE=" + $code
        "TYPE=" + $code.GetType().Name
        """,
    )

    result = _run(
        harness,
        tmp_path,
        path_prefix=stub,
        env={"STUB_EXIT": "0", "STUB_TEXT": "1 row written"},
    )

    assert result.returncode == 0, result.stderr
    assert "1 row written" in result.stdout
    assert _last_line(result, "CODE=") == "0"
    assert _last_line(result, "TYPE=") == "Int32"


def test_preview_passes_unicode_path_unchanged(tmp_path):
    folder = "D:\\Видео с пробелом\\Подпапка «Тест»"
    harness = _preview_harness(
        tmp_path,
        f'''
        $ok = Invoke-Preview "{folder}"
        "OK=" + $ok
        "CALL=" + ($script:calls[0] -join "|")
        ''',
        code=0,
    )

    result = _run(harness, tmp_path)

    assert result.returncode == 0, result.stderr
    assert _last_line(result, "OK=") == "True"
    assert folder in _last_line(result, "CALL=")


def _preview_harness(tmp_path: Path, body: str, *, code: int, preview: bool = False) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    if preview:
        (project / "preview.csv").write_text("old,new\n", encoding="utf-8")
    return _harness(
        tmp_path,
        f"""
        $projectRoot = "{project}"
        $script:code = {code}
        $script:calls = @()
        $script:opened = @()
        function Open-Path {{ param([string]$Path) $script:opened += $Path }}
        function Wait-Enter {{ param([string]$Prompt) }}
        function Invoke-Cli {{ param([string[]]$CliArgs) $script:calls += ,$CliArgs; return $script:code }}
        """
        + textwrap.dedent(body),
    )


def test_preview_success_opens_generated_csv_and_returns_true(tmp_path):
    harness = _preview_harness(
        tmp_path,
        """
        $ok = Invoke-Preview "D:\\Videos with space"
        "OK=" + $ok
        "CALL=" + ($script:calls[0] -join "|")
        "OPENED=" + ($script:opened -join "|")
        """,
        code=0,
    )

    result = _run(harness, tmp_path)

    assert result.returncode == 0, result.stderr
    assert _last_line(result, "OK=") == "True"
    assert "preview|D:\\Videos with space|--csv|preview.csv" in _last_line(result, "CALL=")
    assert _last_line(result, "OPENED=").endswith("preview.csv")


def test_preview_failure_is_reported_and_returns_false(tmp_path):
    harness = _preview_harness(
        tmp_path,
        """
        $ok = Invoke-Preview "D:\\Videos"
        "OK=" + $ok
        "OPENED=" + ($script:opened -join "|")
        """,
        code=2,
    )

    result = _run(harness, tmp_path)

    assert result.returncode == 0, result.stderr
    assert _last_line(result, "OK=") == "False"
    assert "Ошибка preview (код 2)" in result.stdout
    assert _last_line(result, "OPENED=") == ""


def test_apply_success_reports_journal_without_error(tmp_path):
    harness = _preview_harness(
        tmp_path,
        """
        Invoke-Apply "D:\\Videos"
        "CALL=" + ($script:calls[0] -join "|")
        "OPENED=" + ($script:opened -join "|")
        """,
        code=0,
        preview=True,
    )

    result = _run(harness, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Ошибка apply" not in result.stdout
    assert "Готово. Журнал: rename_log.csv" in result.stdout
    assert "apply|preview.csv|--root|D:\\Videos|--apply" in _last_line(result, "CALL=")
    assert _last_line(result, "OPENED=").endswith("rename_log.csv")


def test_apply_failure_keeps_error_message(tmp_path):
    harness = _preview_harness(
        tmp_path,
        """
        Invoke-Apply "D:\\Videos"
        "OPENED=" + ($script:opened -join "|")
        """,
        code=1,
        preview=True,
    )

    result = _run(harness, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Ошибка apply (код 1)" in result.stdout
    assert _last_line(result, "OPENED=") == ""


def test_apply_without_preview_csv_does_not_call_cli(tmp_path):
    harness = _preview_harness(
        tmp_path,
        """
        Invoke-Apply "D:\\Videos"
        "CALLS=" + $script:calls.Count
        """,
        code=0,
    )

    result = _run(harness, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "preview.csv не найден" in result.stdout
    assert _last_line(result, "CALLS=") == "0"
