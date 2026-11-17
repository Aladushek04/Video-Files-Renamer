#!/usr/bin/env python3
"""Межпроцессная блокировка записи рядом с журналом (A02/A03).

Один и тот же замок берут `apply`, `undo` и `repair` с записью. Это не
таймаут-блокировка: файл удерживается средствами ОС (`msvcrt.locking` на
Windows, `flock` на POSIX), поэтому второй процесс не начнёт запись, пока
первый жив, и замок гарантированно снимается при завершении процесса, даже
аварийном. Данные в файле (PID, хост, время) нужны только для сообщения.

Зависимостей нет — только стандартная библиотека.
"""

from __future__ import annotations

import os
import socket
import time
from datetime import datetime, timezone

__all__ = ["LockBusy", "RenamerLock", "lock_path"]

_POLL_SECONDS = 0.05


class LockBusy(RuntimeError):
    """Журнал уже занят другим процессом."""


def lock_path(log_path: str | os.PathLike[str]) -> str:
    """`rename_log.lock` рядом с `rename_log.csv`."""
    log_path = os.path.abspath(os.fspath(log_path))
    stem, _extension = os.path.splitext(log_path)
    return f"{stem}.lock"


class RenamerLock:
    """Эксклюзивная блокировка записи; освобождается ОС при выходе процесса.

    `timeout` — сколько ждать освобождения, но не разрешение забрать замок:
    устаревших замков нет, потому что ОС снимает их сама.
    """

    def __init__(self, log_path: str | os.PathLike[str], *, timeout: float = 0.0) -> None:
        self.path = lock_path(log_path)
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> RenamerLock:  # noqa: PYI034 - typing.Self есть только с 3.11
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    def acquire(self) -> None:
        """Захватывает замок или бросает `LockBusy`; файлы не меняются."""
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + max(self.timeout, 0.0)
        while True:
            if self._try_lock(descriptor):
                break
            if time.monotonic() >= deadline:
                holder = self._holder()
                os.close(descriptor)
                message = f"журнал занят другим процессом: {self.path}"
                if holder:
                    message += f" ({holder})"
                raise LockBusy(message)
            time.sleep(_POLL_SECONDS)
        self._fd = descriptor
        self._write_holder(descriptor)

    def release(self) -> None:
        """Снимает замок; повторный вызов безопасен."""
        descriptor, self._fd = self._fd, None
        if descriptor is None:
            return
        try:
            self._unlock(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _try_lock(descriptor: int) -> bool:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    @staticmethod
    def _unlock(descriptor: int) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            # Закрытие дескриптора всё равно снимет блокировку.
            pass

    def _write_holder(self, descriptor: int) -> None:
        payload = (
            f"pid={os.getpid()} host={socket.gethostname()} "
            f"at={datetime.now(timezone.utc).isoformat()}\n"
        ).encode()
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.ftruncate(descriptor, 0)
            os.write(descriptor, payload)
        except OSError:
            pass

    def _holder(self) -> str:
        try:
            with open(self.path, encoding="utf-8", errors="replace") as handle:
                return handle.readline().strip()
        except OSError:
            return ""
