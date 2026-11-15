"""Распознавание junction, совместимое с Python 3.10+.

`os.path.isjunction` появился только в Python 3.12. Проект заявляет 3.10+ и
обязан по-прежнему видеть junction: обход папки и переименование через junction
запрещены так же, как через symlink. На старых версиях Windows тег reparse
point читается через `FindFirstFileW`, поэтому защита не подменяется на
безусловный `False`.
"""

from __future__ import annotations

import os

__all__ = ["is_junction"]

_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def is_junction(path: str | os.PathLike[str]) -> bool:
    """True, если путь — junction; на платформах без junction — False."""
    native = getattr(os.path, "isjunction", None)
    if native is not None:
        return bool(native(path))
    return junction_from_reparse_tag(path)


def junction_from_reparse_tag(path: str | os.PathLike[str]) -> bool:
    """Резервная проверка через WIN32_FIND_DATAW (Windows, Python < 3.12)."""
    if os.name != "nt":
        return False

    import ctypes
    from ctypes import wintypes

    class Win32FindDataW(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("dwReserved0", wintypes.DWORD),
            ("dwReserved1", wintypes.DWORD),
            ("cFileName", wintypes.WCHAR * 260),
            ("cAlternateFileName", wintypes.WCHAR * 14),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.FindFirstFileW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(Win32FindDataW),
    ]
    kernel32.FindFirstFileW.restype = wintypes.HANDLE
    kernel32.FindClose.argtypes = [wintypes.HANDLE]
    kernel32.FindClose.restype = wintypes.BOOL

    data = Win32FindDataW()
    handle = kernel32.FindFirstFileW(os.fspath(path), ctypes.byref(data))
    if not handle or handle == wintypes.HANDLE(-1).value:
        return False
    kernel32.FindClose(handle)
    is_reparse = bool(data.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT)
    return is_reparse and data.dwReserved0 == _IO_REPARSE_TAG_MOUNT_POINT
