"""File locking and permissions on POSIX and Windows."""
from __future__ import annotations

import os

IS_WINDOWS = os.name == "nt"


def safe_chmod(path, mode: int) -> bool:
    """chmod on POSIX; Windows has no mode bits (the profile ACL already restricts it)."""
    if IS_WINDOWS:
        return False
    try:
        os.chmod(path, mode)
        return True
    except OSError:
        return False


def try_lock_exclusive(fd: int) -> None:
    """Non-blocking exclusive lock on ``fd``; BlockingIOError when someone else holds it."""
    if IS_WINDOWS:
        import errno
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(errno.EAGAIN, "lock is held") from exc
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(fd: int) -> None:
    if IS_WINDOWS:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)
