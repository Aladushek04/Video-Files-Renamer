#!/usr/bin/env python3
"""Карта намерения переименования рядом с журналом (A02/A03).

Перед первым `os.rename` партия целиком записывается в append-only JSON Lines
рядом с `rename_log.csv` (`rename_intent.jsonl`). Запись содержит исходные,
временные и целевые пути каждого шага, а также строки журнала, которые будут
добавлены после успешного переименования. По ней `repair` находит файлы,
оставшиеся под временными именами, и не дублирует строки CSV.

Формат:

    {"event": "intent", "op_id": "...", "kind": "rename", ...}
    {"event": "phase", "op_id": "...", "phase": "done"}

Фазы: `planned` (по умолчанию), `done`, `aborted`, `needs_repair`.
Терминальные — только `done` и `aborted`; всё остальное считается открытой
операцией, и новый `apply`/`undo` не начинается, пока её не разберут.

Файл аддитивен и не влияет на чтение старых журналов. Битая строка в середине
файла — проблема (нельзя доказать, что открытых операций нет); оборванная
последняя строка после падения процесса игнорируется.

Зависимостей нет — только стандартная библиотека.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

__all__ = [
    "INTENT_SUFFIX",
    "Intent",
    "IntentState",
    "PendingOperationError",
    "PlannedStep",
    "intent_path",
    "new_op_id",
    "open_states",
    "read_intent_states",
    "record_phase",
    "unresolved_steps",
    "write_intent",
]

INTENT_SUFFIX = "rename_intent.jsonl"
TERMINAL_PHASES = frozenset({"done", "aborted"})
KIND_RENAME = "rename"
KIND_UNDO = "undo"


class PendingOperationError(RuntimeError):
    """Есть незавершённая операция или intent-файл нельзя прочитать."""


@dataclass(frozen=True)
class PlannedStep:
    """Один шаг партии: абсолютные пути и данные для строки журнала."""

    old_path: str
    temp_path: str
    new_path: str
    old_rel: str
    new_rel: str
    root: str
    oshash: str = ""
    size: int | None = None
    scene_id: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_path": self.old_path,
            "temp_path": self.temp_path,
            "new_path": self.new_path,
            "old_rel": self.old_rel,
            "new_rel": self.new_rel,
            "root": self.root,
            "oshash": self.oshash,
            "size": self.size,
            "scene_id": self.scene_id,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlannedStep:
        return cls(
            old_path=str(data["old_path"]),
            temp_path=str(data["temp_path"]),
            new_path=str(data["new_path"]),
            old_rel=str(data["old_rel"]),
            new_rel=str(data["new_rel"]),
            root=str(data.get("root", "")),
            oshash=str(data.get("oshash", "")),
            size=data.get("size"),
            scene_id=str(data.get("scene_id", "")),
            source=str(data.get("source", "")),
        )


@dataclass(frozen=True)
class Intent:
    """Записанная партия: все шаги и строки журнала, которые к ней относятся."""

    op_id: str
    kind: str
    created: str
    pid: int
    host: str
    steps: tuple[PlannedStep, ...]
    log_rows: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": "intent",
            "op_id": self.op_id,
            "kind": self.kind,
            "created": self.created,
            "pid": self.pid,
            "host": self.host,
            "steps": [step.to_dict() for step in self.steps],
            "log_rows": list(self.log_rows),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Intent:
        steps = tuple(PlannedStep.from_dict(item) for item in data["steps"])
        rows = tuple(dict(item) for item in data.get("log_rows", ()))
        return cls(
            op_id=str(data["op_id"]),
            kind=str(data.get("kind", KIND_RENAME)),
            created=str(data.get("created", "")),
            pid=int(data.get("pid", 0)),
            host=str(data.get("host", "")),
            steps=steps,
            log_rows=rows,
        )


@dataclass(frozen=True)
class IntentState:
    """Партия и её последняя фаза."""

    intent: Intent
    phase: str = "planned"
    messages: tuple[str, ...] = ()

    @property
    def is_open(self) -> bool:
        return self.phase not in TERMINAL_PHASES


def intent_path(log_path: str | os.PathLike[str]) -> str:
    """Файл карты рядом с журналом: `<папка журнала>/rename_intent.jsonl`."""
    log_path = os.path.abspath(os.fspath(log_path))
    return os.path.join(os.path.dirname(log_path), INTENT_SUFFIX)


def new_op_id() -> str:
    """Идентификатор операции."""
    return uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_event(path: str, payload: dict[str, Any]) -> None:
    """Дописывает строку и сбрасывает её на диск; ошибка доступа пробрасывается."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def write_intent(
    log_path: str | os.PathLike[str],
    *,
    kind: str,
    steps: list[PlannedStep],
    log_rows: list[dict[str, Any]],
    op_id: str | None = None,
) -> Intent:
    """Записывает всю партию до первого переименования.

    Вызывающий обязан сделать это до `os.rename`: при ошибке записи файлы
    остаются нетронутыми.
    """
    intent = Intent(
        op_id=op_id or new_op_id(),
        kind=kind,
        created=_now(),
        pid=os.getpid(),
        host=socket.gethostname(),
        steps=tuple(steps),
        log_rows=tuple(log_rows),
    )
    _append_event(intent_path(log_path), intent.to_dict())
    return intent


def record_phase(
    log_path: str | os.PathLike[str],
    op_id: str,
    phase: str,
    messages: list[str] | None = None,
) -> None:
    """Дописывает фазу операции; ошибка записи не должна скрываться."""
    _append_event(
        intent_path(log_path),
        {
            "event": "phase",
            "op_id": op_id,
            "phase": phase,
            "messages": list(messages or ()),
            "ts": _now(),
        },
    )


def _read_lines(path: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Читает события; оборванная последняя строка — не ошибка, середина — да."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return [], []
    events: list[dict[str, Any]] = []
    problems: list[str] = []
    lines = text.splitlines(keepends=True)
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            if number == len(lines) and not line.endswith("\n"):
                # Хвост оборван отключением питания: сам intent уже записан
                # предыдущей строкой, а фазу можно восстановить по диску.
                continue
            problems.append(f"{path}: строка {number} повреждена")
            continue
        if not isinstance(event, dict) or "event" not in event:
            problems.append(f"{path}: строка {number} не является событием")
            continue
        events.append(event)
    return events, problems


def read_intent_states(
    log_path: str | os.PathLike[str],
) -> tuple[list[IntentState], list[str]]:
    """Все партии из файла и список проблем чтения."""
    path = intent_path(log_path)
    events, problems = _read_lines(path)
    order: list[str] = []
    states: dict[str, IntentState] = {}
    for event in events:
        kind = event.get("event")
        op_id = str(event.get("op_id", ""))
        if not op_id:
            problems.append(f"{path}: событие без op_id")
            continue
        if kind == "intent":
            try:
                intent = Intent.from_dict(event)
            except (KeyError, TypeError, ValueError) as exc:
                problems.append(f"{path}: партия {op_id} повреждена: {exc}")
                continue
            if op_id not in states:
                order.append(op_id)
            states[op_id] = IntentState(intent)
        elif kind == "phase":
            state = states.get(op_id)
            if state is None:
                problems.append(f"{path}: фаза для неизвестной партии {op_id}")
                continue
            messages = tuple(str(item) for item in event.get("messages", ()))
            states[op_id] = IntentState(
                state.intent,
                str(event.get("phase", state.phase)),
                messages,
            )
    return [states[op_id] for op_id in order], problems


def open_states(states: list[IntentState]) -> list[IntentState]:
    """Партии, которые не завершены и требуют внимания."""
    return [state for state in states if state.is_open]


def unresolved_steps(intent: Intent) -> list[str]:
    """Шаги, которые не находятся ни в исходном, ни в конечном состоянии."""
    problems: list[str] = []
    for step in intent.steps:
        old = os.path.lexists(step.old_path)
        temp = os.path.lexists(step.temp_path)
        new = os.path.lexists(step.new_path)
        if new and not old and not temp:
            continue
        if old and not temp and not new:
            continue
        if temp and not old and not new:
            problems.append(f"{step.old_rel}: файл остался под временным именем {step.temp_path}")
        elif old and new:
            problems.append(f"{step.old_rel}: есть и старое, и новое имя: нужен repair")
        else:
            problems.append(
                f"{step.old_rel}: неоднозначное состояние (old={old}, temp={temp}, new={new})"
            )
    return problems


def raise_if_blocked(log_path: str | os.PathLike[str]) -> None:
    """Не даёт начать новую операцию, пока есть незавершённая или битый файл."""
    states, problems = read_intent_states(log_path)
    if problems:
        raise PendingOperationError(
            "файл намерения повреждён, автоматическое восстановление невозможно: "
            + "; ".join(problems)
        )
    pending = open_states(states)
    if pending:
        ids = ", ".join(state.intent.op_id for state in pending)
        raise PendingOperationError(
            f"есть незавершённая операция ({ids}); выполните repair "
            f"с журналом {os.fspath(log_path)}"
        )
