"""Shared infrastructure for the clean and normalize passes.

Both passes (OS junk cleanup and NFC rename) share the same shape: a single parallel
directory walk produces a plan, and each plan item is later applied in parallel with
per-item progress and audit logging. This module holds the pieces that are common to
both -- the error/record types and the threaded directory walker -- so the clean and
normalize modules only carry their own concern.

When the target is a network drive, the bottleneck is the round-trip latency per
directory. The walker therefore lists directories concurrently with threads to overlap
many I/O round-trips and hide latency (the GIL is released during syscalls, so this
parallelizes effectively). On fast local storage a lower worker count can be faster, so
the worker count is caller-configurable.
"""

from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass
from typing import Callable

# Default worker count, tuned for network drives: high enough to hide round-trip latency.
DEFAULT_WORKERS = 16


@dataclass
class ApplyError:
    """A single failure encountered while applying a plan (rename or delete)."""

    src: str
    dst: str  # "" for deletes (no destination)
    message: str


# A per-item record callback for the audit log: (action, status, kind, src, dst, message).
RecordCb = Callable[[str, str, str, str, str, str], None]

# Progress callback for an apply pass: (processed, total, current_path).
ProgressCb = Callable[[int, int, str], None]


def depth(path: str) -> int:
    """Path depth (number of separators); deeper paths sort larger."""
    return path.replace("\\", "/").count("/")


@dataclass
class Classified:
    """The verdict of a per-entry classifier during a walk.

    item: the plan entry to collect (RenameItem / JunkItem), or None to ignore.
    descend: whether to recurse into this entry when it is a directory. Junk directories
        are recorded but not descended into (they are removed wholesale).
    """

    item: object | None = None
    descend: bool = True


# Classifier: (path, name, is_dir) -> Classified. Must be a pure function (no shared
# state) because the walker calls it concurrently from multiple worker threads.
Classifier = Callable[[str, str, bool], Classified]


def walk(
    root: str,
    classify: Classifier,
    workers: int = DEFAULT_WORKERS,
    follow_symlinks: bool = False,
    recursive: bool = True,
    progress_cb: Callable[[int], None] | None = None,
    on_empty_dir: Callable[[str], None] | None = None,
) -> list:
    """Walk under root in parallel, returning the list of items the classifier collected.

    Directories are pushed onto a queue and listed concurrently by multiple workers; each
    worker pushes the subdirectories it should descend into back onto the queue. Symlinked
    directories are never followed (with follow_symlinks=False a symlinked directory
    reports is_dir=False, so it is treated as a leaf). With recursive=False only the direct
    children of root are considered. progress_cb, if given, is called with the running
    scanned-entry count. on_empty_dir, if given, is called with the path of each directory
    that was listed successfully and contained no entries (the root is never reported).
    """
    workers = max(1, workers)
    dirq: queue.Queue[str] = queue.Queue()
    dirq.put(root)

    pending = [1]  # directories queued but not yet fully processed.
    pending_lock = threading.Lock()
    items: list = []
    items_lock = threading.Lock()
    counter = [0]
    stop = threading.Event()

    def worker() -> None:
        while True:
            try:
                current = dirq.get(timeout=0.2)
            except queue.Empty:
                if stop.is_set():
                    return
                continue
            subs: list[str] = []
            local_items: list = []
            local_count = 0
            scanned_ok = False
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        local_count += 1
                        try:
                            is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
                        except OSError:
                            is_dir = False
                        verdict = classify(entry.path, entry.name, is_dir)
                        if verdict.item is not None:
                            local_items.append(verdict.item)
                        if is_dir and recursive and verdict.descend:
                            subs.append(entry.path)
                scanned_ok = True
            except OSError:
                # Skip directories we cannot open (permission errors, etc.).
                pass

            if on_empty_dir is not None and scanned_ok and local_count == 0 and current != root:
                on_empty_dir(current)

            with pending_lock:
                pending[0] += len(subs)
            for s in subs:
                dirq.put(s)
            with items_lock:
                items.extend(local_items)
                counter[0] += local_count
                count_now = counter[0]
            if progress_cb is not None:
                progress_cb(count_now)
            with pending_lock:
                pending[0] -= 1
                if pending[0] == 0:
                    stop.set()
            dirq.task_done()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    stop.wait()
    for t in threads:
        t.join(timeout=1.0)
    return items
