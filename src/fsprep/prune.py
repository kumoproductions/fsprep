"""Empty-directory pruning: remove directories left with no contents.

This is the *prune* pass. It runs last (after clean and normalize) and walks the live tree,
because what counts as empty depends on what those passes removed (e.g. a directory that held
only junk is empty only after the clean pass). Candidate empty directories are also collected
during the scan for the preview (see scan.collect_empty / core.walk on_empty_dir), but the
actual removal here re-checks the live tree so cascades are handled correctly.
"""

from __future__ import annotations

import errno
import os

from .core import ApplyError, RecordCb


def prune_empty_dirs(
    root: str,
    recursive: bool = True,
    record_cb: RecordCb | None = None,
) -> tuple[int, list[ApplyError]]:
    """Remove empty directories under root, bottom-up. Returns (removed_count, errors).

    With recursive=True the walk is depth-first post-order (os.walk topdown=False), so a
    directory whose only contents were other now-removed empty directories is itself removed in
    the same pass. The root itself is never removed. A directory that is simply not empty is
    skipped silently; only unexpected errors (e.g. permission denied) are collected.
    """
    removed = 0
    errors: list[ApplyError] = []

    def try_rmdir(path: str) -> None:
        nonlocal removed
        try:
            os.rmdir(path)
        except OSError as exc:
            # "Not empty" is the normal skip case, not an error worth reporting.
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                errors.append(ApplyError(src=path, dst="", message=str(exc)))
            return
        removed += 1
        if record_cb is not None:
            record_cb("prune", "removed", "dir", path, "", "")

    if recursive:
        for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
            if os.path.abspath(dirpath) == os.path.abspath(root):
                continue
            try_rmdir(dirpath)
    else:
        try:
            with os.scandir(root) as it:
                children = [e.path for e in it if e.is_dir(follow_symlinks=False)]
        except OSError as exc:
            return removed, [ApplyError(src=root, dst="", message=str(exc))]
        for path in children:
            try_rmdir(path)
    return removed, errors
