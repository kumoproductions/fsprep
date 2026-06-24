"""OS junk cleanup: detect and delete OS-generated cruft so it never reaches the tape.

This is the *clean* pass. It runs before the normalize (rename) pass so that deleting junk
that lives inside an NFD-named directory happens while the captured paths are still valid --
a rename of an ancestor directory would otherwise invalidate them.

Junk is matched by name (case-insensitively). A junk *directory* is recorded once and not
descended into; it is removed wholesale with the rest of the plan.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .core import DEFAULT_WORKERS, ApplyError, Classified, ProgressCb, RecordCb

# OS junk recognized for cleanup. Matched case-insensitively; the value is the label
# shown in previews/logs. This set is a superset of the typical NAS SMB `veto files` list,
# so anything blocked at the share also gets swept here, plus a few extras the share never
# sees (e.g. __MACOSX from extracted zips).
#
# Exact file names:
_JUNK_FILES = {
    # macOS
    ".ds_store": ".DS_Store",
    ".localized": ".localized",          # not in veto; harmless extra
    ".apdisk": ".apdisk",
    ".volumeicon.icns": ".VolumeIcon.icns",  # not in veto; harmless extra
    # Windows
    "thumbs.db": "Thumbs.db",
    "ehthumbs.db": "ehthumbs.db",        # not in veto; harmless extra
    "desktop.ini": "desktop.ini",
}
# Exact directory names (removed recursively, not descended into):
_JUNK_DIRS = {
    # macOS / AFP (netatalk)
    ".spotlight-v100": ".Spotlight-V100",
    ".trashes": ".Trashes",
    ".fseventsd": ".fseventsd",
    ".temporaryitems": ".TemporaryItems",
    ".documentrevisions-v100": ".DocumentRevisions-V100",
    ".appledouble": ".AppleDouble",
    ".appledb": ".AppleDB",
    ".appledesktop": ".AppleDesktop",
    "__macosx": "__MACOSX",              # not in veto; created by extracting zips
    # Windows
    "$recycle.bin": "$RECYCLE.BIN",
}


@dataclass
class JunkItem:
    """An OS-generated junk file/folder slated for deletion."""

    path: str
    kind: str  # "file" | "dir"
    reason: str  # which rule matched (e.g. ".DS_Store", "AppleDouble ._*")


@dataclass
class CleanResult:
    removed_files: int = 0
    removed_dirs: int = 0
    errors: list[ApplyError] = field(default_factory=list)


def match_junk(name: str, is_dir: bool, extra_globs: list[str] | None = None) -> str | None:
    """Return the matched rule label if the name is known OS junk, else None.

    extra_globs are user-supplied fnmatch patterns (matched case-insensitively) applied to
    both files and directories, on top of the built-in OS junk set.
    """
    low = name.lower()
    if is_dir:
        label = _JUNK_DIRS.get(low)
    elif low in _JUNK_FILES:
        label = _JUNK_FILES[low]
    elif name.startswith("._"):
        # AppleDouble resource-fork sidecars: "._" + original name.
        label = "AppleDouble ._*"
    else:
        label = None
    if label is not None:
        return label
    if extra_globs:
        for pat in extra_globs:
            if fnmatch.fnmatch(low, pat.lower()):
                return f"glob {pat}"
    return None


def classify(path: str, name: str, is_dir: bool, extra_globs: list[str] | None = None) -> Classified:
    """Classifier for the walk: collect junk and never descend into a junk directory."""
    reason = match_junk(name, is_dir, extra_globs)
    if reason is None:
        return Classified(item=None, descend=True)
    return Classified(
        item=JunkItem(path=path, kind="dir" if is_dir else "file", reason=reason),
        descend=False,
    )


def _remove_one(item: JunkItem) -> tuple[str, ApplyError | None]:
    """Delete a single junk item. Returns ("removed"|"err", ApplyError|None)."""
    try:
        if item.kind == "dir":
            shutil.rmtree(item.path)
        else:
            os.remove(item.path)
        return "removed", None
    except FileNotFoundError:
        return "removed", None  # already gone -> treat as success.
    except OSError as exc:
        return "err", ApplyError(src=item.path, dst="", message=str(exc))


def clean_junk(
    junk: list[JunkItem],
    workers: int = DEFAULT_WORKERS,
    progress_cb: ProgressCb | None = None,
    record_cb: RecordCb | None = None,
) -> CleanResult:
    """Delete junk items in parallel (order-independent; items never nest).

    Files are removed with os.remove, directories recursively with shutil.rmtree. Errors
    are collected and processing continues. record_cb, if given, is called once per item.
    """
    workers = max(1, workers)
    result = CleanResult()
    total = len(junk)
    if total == 0:
        return result
    processed = [0]
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=min(workers, total)) as ex:
        futs = {ex.submit(_remove_one, it): it for it in junk}
        for fut in as_completed(futs):
            outcome, err = fut.result()
            item = futs[fut]
            with lock:
                if outcome == "removed":
                    if item.kind == "dir":
                        result.removed_dirs += 1
                    else:
                        result.removed_files += 1
                else:
                    result.errors.append(err)  # type: ignore[arg-type]
                processed[0] += 1
                processed_now = processed[0]
            if record_cb is not None:
                record_cb("remove", "removed" if outcome == "removed" else "error",
                          item.kind, item.path, "", err.message if err else "")
            if progress_cb is not None:
                progress_cb(processed_now, total, item.path)
    return result
