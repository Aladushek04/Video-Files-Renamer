#!/usr/bin/env python3
"""Разбор незавершённых операций по карте намерения (A02/A03).

`repair` читает `rename_intent.jsonl`, сверяет каждый шаг партии с диском и
предлагает действия. По умолчанию ничего не меняется; с `apply_changes=True`
недостающие переходы `temp → new` выполняются, а недописанные строки журнала
добавляются идемпотентно (`append_missing_log`). Неоднозначные шаги (оба
имени, ссылка вместо файла, несовпадение размера/OSHASH, занятая цель)
остаются на месте и показываются человеку.

Зависимостей нет — только стандартная библиотека.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass

from video_renamer.core.file_lock import RenamerLock
from video_renamer.core.fs_links import is_junction
from video_renamer.core.oshash import OshashError, oshash_from_path
from video_renamer.core.rename_intent import (
    IntentState,
    PlannedStep,
    open_states,
    read_intent_states,
    record_phase,
)
from video_renamer.core.rename_log import (
    LogEntry,
    append_missing_log,
    entry_id,
    read_log,
)

__all__ = [
    "OpDiagnosis",
    "RepairReport",
    "StepDiagnosis",
    "repair",
]

STATE_NOT_STARTED = "not_started"
STATE_TEMP_LEFT = "temp_left"
STATE_FINAL = "final"
STATE_AMBIGUOUS = "ambiguous"
STATE_MISSING = "missing"
STATE_UNVERIFIED = "unverified"

ACTION_NONE = "none"
ACTION_COMPLETE = "complete"
ACTION_LOG = "log"
ACTION_MANUAL = "manual"


@dataclass(frozen=True)
class StepDiagnosis:
    """Состояние одного шага партии и предлагаемое действие."""

    step: PlannedStep
    state: str
    action: str
    message: str


@dataclass(frozen=True)
class OpDiagnosis:
    """Состояние всей партии."""

    state: IntentState
    steps: tuple[StepDiagnosis, ...]
    missing_log: tuple[str, ...] = ()
    unexpected_log: tuple[str, ...] = ()

    @property
    def needs_write(self) -> bool:
        return any(step.action in {ACTION_COMPLETE, ACTION_LOG} for step in self.steps)

    @property
    def unresolved(self) -> bool:
        if self.unexpected_log:
            return True
        return any(step.action == ACTION_MANUAL for step in self.steps)


@dataclass(frozen=True)
class RepairReport:
    """Итог просмотра или восстановления."""

    diagnoses: tuple[OpDiagnosis, ...] = ()
    problems: tuple[str, ...] = ()
    applied: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()


def _inside(root: str, path: str) -> bool:
    """Лежит ли путь внутри записанного корня (без обращения к symlink)."""
    try:
        root_norm = os.path.normcase(os.path.abspath(root))
        path_norm = os.path.normcase(os.path.abspath(path))
        return os.path.commonpath([root_norm, path_norm]) == root_norm
    except ValueError:
        return False


def _identity_problem(path: str, step: PlannedStep) -> str:
    """Почему файл нельзя считать тем же, что записан в карте."""
    try:
        if os.path.islink(path) or is_junction(path):
            return "это ссылка или junction"
        if not os.path.isfile(path):
            return "не обычный файл"
        if step.size is not None:
            size = os.path.getsize(path)
            if size != step.size:
                return f"размер {size} вместо {step.size}"
        if step.oshash:
            actual = oshash_from_path(path)
            if actual != step.oshash:
                return "OSHASH не совпадает"
    except (OSError, OshashError) as exc:
        return f"файл не читается: {exc}"
    return ""


def _row_key(row: dict) -> tuple[str, str, str]:
    return str(row.get("root", "")), str(row.get("old", "")), str(row.get("new", ""))


def _step_key(step: PlannedStep) -> tuple[str, str, str]:
    return step.root, step.old_rel, step.new_rel


def _row_id(row: dict) -> str:
    return entry_id(
        str(row.get("timestamp", "")),
        str(row.get("action", "")),
        str(row.get("root", "")),
        str(row.get("old", "")),
        str(row.get("new", "")),
    )


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _occupied_by_same_batch(step: PlannedStep, steps: tuple[PlannedStep, ...]) -> bool:
    """Занят ли `old` другой целью этой же партии (обмен или цепочка)."""
    old_key = _path_key(step.old_path)
    return any(other is not step and _path_key(other.new_path) == old_key for other in steps)


def _diagnose_step(step: PlannedStep, steps: tuple[PlannedStep, ...]) -> StepDiagnosis:
    if not os.path.isdir(step.root):
        return StepDiagnosis(
            step, STATE_AMBIGUOUS, ACTION_MANUAL, f"корень недоступен: {step.root}"
        )
    outside = [
        path
        for path in (step.old_path, step.temp_path, step.new_path)
        if not _inside(step.root, path)
    ]
    if outside:
        return StepDiagnosis(
            step,
            STATE_AMBIGUOUS,
            ACTION_MANUAL,
            f"путь вне корня {step.root}: {outside[0]}",
        )

    old = os.path.lexists(step.old_path)
    temp = os.path.lexists(step.temp_path)
    new = os.path.lexists(step.new_path)

    if old and not temp and not new:
        return StepDiagnosis(step, STATE_NOT_STARTED, ACTION_NONE, "не начиналось; файл на месте")
    if not old and temp and not new:
        problem = _identity_problem(step.temp_path, step)
        if problem:
            return StepDiagnosis(
                step,
                STATE_UNVERIFIED,
                ACTION_MANUAL,
                f"временный файл не подтверждён: {step.temp_path} ({problem})",
            )
        return StepDiagnosis(
            step,
            STATE_TEMP_LEFT,
            ACTION_COMPLETE,
            f"завершить: {step.temp_path} -> {step.new_path}",
        )
    if not old and not temp and new:
        problem = _identity_problem(step.new_path, step)
        if problem:
            return StepDiagnosis(
                step,
                STATE_UNVERIFIED,
                ACTION_MANUAL,
                f"новый файл не подтверждён: {step.new_path} ({problem})",
            )
        return StepDiagnosis(step, STATE_FINAL, ACTION_NONE, "переименование выполнено")
    if new and not temp and old and _occupied_by_same_batch(step, steps):
        # Обмен/цепочка после завершения: `old` занят результатом другого шага.
        problem = _identity_problem(step.new_path, step)
        if problem:
            return StepDiagnosis(
                step,
                STATE_UNVERIFIED,
                ACTION_MANUAL,
                f"новый файл не подтверждён: {step.new_path} ({problem})",
            )
        return StepDiagnosis(
            step,
            STATE_FINAL,
            ACTION_NONE,
            "переименование выполнено (old занят другой целью этой партии)",
        )
    if not old and not temp and not new:
        return StepDiagnosis(
            step,
            STATE_MISSING,
            ACTION_MANUAL,
            f"нет ни старого, ни временного, ни нового файла: {step.new_path}",
        )
    if temp and old and not new and _occupied_by_same_batch(step, steps):
        problem = _identity_problem(step.temp_path, step)
        if problem:
            return StepDiagnosis(
                step,
                STATE_UNVERIFIED,
                ACTION_MANUAL,
                f"временный файл не подтверждён: {step.temp_path} ({problem})",
            )
        return StepDiagnosis(
            step,
            STATE_TEMP_LEFT,
            ACTION_COMPLETE,
            f"завершить: {step.temp_path} -> {step.new_path} "
            f"(old занят другой целью этой партии: {step.old_path})",
        )

    paths = []
    if old:
        paths.append(f"old={step.old_path}")
    if temp:
        paths.append(f"temp={step.temp_path}")
    if new:
        paths.append(f"new={step.new_path}")
    return StepDiagnosis(
        step,
        STATE_AMBIGUOUS,
        ACTION_MANUAL,
        "неоднозначно, файлы не трогать: " + ", ".join(paths),
    )


def diagnose(state: IntentState, log_entries: list[LogEntry]) -> OpDiagnosis:
    """Сверяет партию с диском и журналом; ничего не меняет."""
    existing = {entry.id for entry in log_entries}
    rows_by_step = {_row_key(row): row for row in state.intent.log_rows}
    steps: list[StepDiagnosis] = []
    missing: list[str] = []
    unexpected: list[str] = []

    for step in state.intent.steps:
        diagnosis = _diagnose_step(step, state.intent.steps)
        steps.append(diagnosis)
        row = rows_by_step.get(_step_key(step))
        if row is None:
            # Карта без строки журнала: восстановление не знает, что писать.
            steps[-1] = StepDiagnosis(
                step,
                STATE_AMBIGUOUS,
                ACTION_MANUAL,
                "в карте нет строки журнала для этого шага",
            )
            continue
        identifier = _row_id(row)
        if diagnosis.state == STATE_FINAL and identifier not in existing:
            steps[-1] = StepDiagnosis(
                step,
                diagnosis.state,
                ACTION_LOG,
                f"переименование выполнено; дописать журнал ({identifier})",
            )
            missing.append(identifier)
        elif diagnosis.state != STATE_FINAL and identifier in existing:
            unexpected.append(identifier)
            steps[-1] = StepDiagnosis(
                step,
                diagnosis.state,
                ACTION_MANUAL,
                f"в журнале уже есть строка {identifier}, а файл не в конечном состоянии",
            )

    return OpDiagnosis(state, tuple(steps), tuple(missing), tuple(unexpected))


def _read_log_entries(log_path: str) -> tuple[list[LogEntry], list[str]]:
    try:
        return read_log(log_path), []
    except (OSError, ValueError, csv.Error) as exc:
        return [], [f"журнал не читается: {exc}"]


def _open_diagnoses(log_path: str) -> tuple[list[OpDiagnosis], list[str]]:
    states, problems = read_intent_states(log_path)
    pending = open_states(states)
    if not pending:
        return [], problems
    entries, log_problems = _read_log_entries(log_path)
    problems.extend(log_problems)
    return [diagnose(state, entries) for state in pending], problems


def repair(log_path: str, *, apply_changes: bool) -> RepairReport:
    """Показывает состояние; с `apply_changes` безопасно доводит операции.

    Вызывать без замка нельзя, если `apply_changes=True`: общий замок с
    `apply`/`undo` обязателен.
    """
    if not apply_changes:
        diagnoses, problems = _open_diagnoses(log_path)
        return RepairReport(tuple(diagnoses), tuple(problems))

    with RenamerLock(log_path):
        diagnoses, problems = _open_diagnoses(log_path)
        if problems:
            return RepairReport(tuple(diagnoses), tuple(problems))
        applied: list[str] = []
        remaining: list[str] = []
        for diagnosis in diagnoses:
            applied.extend(_complete_steps(diagnosis))
            refreshed = _refresh(diagnosis, log_path)
            if refreshed is None:
                remaining.append(diagnosis.state.intent.op_id)
                continue
            applied.extend(_append_log(refreshed, log_path))
            final = _refresh(refreshed, log_path)
            if final is None or final.unresolved or final.missing_log:
                remaining.append(diagnosis.state.intent.op_id)
                if final is not None and final.missing_log:
                    problems.append(
                        f"{diagnosis.state.intent.op_id}: журнал не дописан: "
                        + ", ".join(final.missing_log)
                    )
                continue
            try:
                record_phase(log_path, diagnosis.state.intent.op_id, "done", ["repair"])
            except OSError as exc:
                problems.append(f"{diagnosis.state.intent.op_id}: фаза не записана: {exc}")
                remaining.append(diagnosis.state.intent.op_id)
        return RepairReport(tuple(diagnoses), tuple(problems), tuple(applied), tuple(remaining))


def _complete_steps(diagnosis: OpDiagnosis) -> list[str]:
    """Завершает шаги temp → new, проверяя цель и содержимое перед каждым."""
    applied: list[str] = []
    for step in diagnosis.steps:
        if step.action != ACTION_COMPLETE:
            continue
        target = step.step.new_path
        if os.path.lexists(target):
            applied.append(f"{step.step.old_rel}: цель уже занята: {target}")
            continue
        problem = _identity_problem(step.step.temp_path, step.step)
        if problem:
            applied.append(f"{step.step.old_rel}: временный файл не подтверждён: {problem}")
            continue
        try:
            os.rename(step.step.temp_path, target)
        except OSError as exc:
            applied.append(
                f"{step.step.old_rel}: не удалось завершить {step.step.temp_path}: {exc}"
            )
        else:
            applied.append(f"завершено: {step.step.old_rel} -> {step.step.new_rel}")
    return applied


def _refresh(diagnosis: OpDiagnosis, log_path: str) -> OpDiagnosis | None:
    entries, problems = _read_log_entries(log_path)
    if problems:
        return None
    return diagnose(diagnosis.state, entries)


def _append_log(diagnosis: OpDiagnosis, log_path: str) -> list[str]:
    """Дописывает только недостающие строки журнала для завершённых шагов."""
    if not diagnosis.missing_log:
        return []
    missing = set(diagnosis.missing_log)
    entries = [
        LogEntry(legacy=False, **row)
        for row in diagnosis.state.intent.log_rows
        if _row_id(row) in missing
    ]
    try:
        added = append_missing_log(log_path, entries)
    except (OSError, ValueError) as exc:
        return [f"журнал не записан: {exc}"]
    return [f"журнал: добавлено строк: {added}"] if added else []
