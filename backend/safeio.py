#!/usr/bin/env python3
"""Shared safe file IO for the WalkingPad backend.

All state lives under ~/.local/share/walkingpad and ~/.config/walkingpad,
both writable by any process running as the user. Readers therefore treat
paths as hostile: refuse symlinks and non-regular files, and cap bytes and
lines so a planted FIFO or giant file can neither block nor exhaust the
shell process polling stats.py. Writers use unpredictable temp names
created with O_EXCL so a planted symlink at a predictable .tmp path cannot
redirect the write into another user file.
"""

import json
import os
import stat
import tempfile
from pathlib import Path

FILE_MODE = 0o600  # state contains no secrets; still keep it owner-only


def read_text(path, max_bytes: int) -> str | None:
    """File content, or None when the file is missing, a symlink, not a
    regular file, or larger than max_bytes. The read itself is capped too,
    so a file growing after fstat still cannot overshoot."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as fh:
            fd = -1  # ownership moved to the file object
            return fh.read(max_bytes)
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def read_json(path, max_bytes: int):
    """Parsed JSON, or None when unreadable/unsafe/invalid."""
    text = read_text(path, max_bytes)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def read_lines(path, max_bytes: int, max_lines: int) -> list | None:
    """Text lines capped in bytes and count, or None when unsafe."""
    text = read_text(path, max_bytes)
    if text is None:
        return None
    return text.splitlines()[:max_lines]


def append_line(path, line: str) -> None:
    """Append one line, refusing to follow a planted symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, FILE_MODE)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def atomic_write(path, text: str) -> None:
    """Write text atomically: unpredictable O_EXCL temp file in the same
    directory (a planted symlink at a predictable tmp path cannot redirect
    the write), then rename onto path, which replaces even a planted
    symlink itself rather than its target."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
