"""NFC filename normalization: rename file/folder names from NFD to NFC in place.

This is the *normalize* pass. It runs after the clean pass. macOS-origin filenames are
often stored in NFD (decomposed) Unicode form, which causes compatibility problems when
written to LTFS (e.g. dakuten/handakuten separated from their base character). Renaming
touches names only -- the file data itself is not moved, so it is a metadata-only
operation. Even at terabyte scale the cost does not depend on byte size, so progress is
reported in terms of item count (number of files/folders).

Conflicts (the NFC name already exists) are detected during the scan and skipped by
default; overwrite replaces an existing *file* destination (a directory on either side is
never overwritten).
"""

from __future__ import annotations

import os
import threading
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .core import DEFAULT_WORKERS, ApplyError, Classified, ProgressCb, RecordCb, depth


# Unicode normalization forms accepted by --form. NFC/NFD differ only in (de)composition;
# the NFK* forms additionally fold compatibility characters (e.g. full-width -> half-width,
# ligatures, circled digits), which is often desirable when preparing names for archival.
FORMS = ("NFC", "NFD", "NFKC", "NFKD")

# Characters illegal in filenames on Windows / FAT / many tape filesystems. ASCII control
# characters (< 0x20) are illegal too and are handled separately in sanitize_name.
ILLEGAL_CHARS = '<>:"|?*'

# Windows reserved device names (matched case-insensitively, with or without an extension).
_WIN_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def needs_nfc(name: str) -> bool:
    """Return True if the name component is not already NFC-normalized."""
    return not unicodedata.is_normalized("NFC", name)


def to_nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def sanitize_name(name: str) -> str:
    """Make a single name component safe/portable across Windows/FAT/tape filesystems.

    Illegal characters (``<>:"|?*`` and ASCII control chars) become ``_``; trailing spaces
    and dots are dropped (Windows silently strips them); a Windows reserved device name (CON,
    NUL, COM1, ...) is prefixed with ``_`` so it survives as an ordinary name. The result is
    guaranteed non-empty.
    """
    cleaned = "".join("_" if (ch in ILLEGAL_CHARS or ord(ch) < 0x20) else ch for ch in name)
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        return "_"
    if cleaned.split(".", 1)[0].upper() in _WIN_RESERVED:
        cleaned = "_" + cleaned
    return cleaned


def target_name(name: str, form: str = "NFC", sanitize: bool = False) -> str:
    """Compute the desired target for a name: normalize to ``form``, then optionally sanitize."""
    target = unicodedata.normalize(form, name)
    if sanitize:
        target = sanitize_name(target)
    return target


@dataclass
class RenameItem:
    """A single entry in the rename plan."""

    src: str  # absolute path before rename
    dst: str  # absolute path after rename
    kind: str  # "file" | "dir"
    status: str  # "ok" | "conflict"
    depth: int  # path depth (deeper = larger)


@dataclass
class RenameResult:
    renamed: int = 0
    overwritten: int = 0
    skipped: int = 0
    errors: list[ApplyError] = field(default_factory=list)


def classify(path: str, name: str, is_dir: bool, form: str = "NFC", sanitize: bool = False) -> Classified:
    """Classifier for the walk: collect a RenameItem when the target name differs.

    The target is the name under the requested normalization ``form``, optionally sanitized
    for portability (see sanitize_name). The lexists conflict check runs only for actual
    rename candidates (to avoid extra round-trips on network drives). Directories are always
    descended into.
    """
    target = target_name(name, form=form, sanitize=sanitize)
    if target == name:
        return Classified(item=None, descend=True)
    parent = os.path.dirname(path)
    dst = os.path.join(parent, target)
    status = "conflict" if (dst != path and os.path.lexists(dst)) else "ok"
    return Classified(
        item=RenameItem(
            src=path,
            dst=dst,
            kind="dir" if is_dir else "file",
            status=status,
            depth=depth(path),
        ),
        descend=True,
    )


def _rename_one(item: RenameItem, overwrite: bool = False) -> tuple[str, ApplyError | None]:
    """Rename a single item. Returns ("ok"|"overwritten"|"skip"|"err", ApplyError|None).

    When the destination already exists and overwrite is True, replace it atomically via
    os.replace -- but only for file-over-file. If either side is a directory, overwriting is
    unsafe, so the item is skipped.
    """
    try:
        # Re-check the destination right before applying (state may change due to other items).
        if item.dst != item.src and os.path.lexists(item.dst):
            if not overwrite:
                return "skip", None
            # Only file-over-file overwrite is safe; a directory on either side is skipped.
            if os.path.isdir(item.dst) or os.path.isdir(item.src):
                return "skip", None
            os.replace(item.src, item.dst)
            return "overwritten", None
        os.rename(item.src, item.dst)
        return "ok", None
    except OSError as exc:
        return "err", ApplyError(src=item.src, dst=item.dst, message=str(exc))


def apply_renames(
    items: list[RenameItem],
    workers: int = DEFAULT_WORKERS,
    overwrite: bool = False,
    progress_cb: ProgressCb | None = None,
    record_cb: RecordCb | None = None,
) -> RenameResult:
    """Apply the rename plan in parallel, bucketed by depth.

    Items at the same depth do not affect each other's absolute paths, so they can be
    renamed in parallel safely. Buckets are processed deepest-first, and each bucket is
    fully drained before moving on to the next (shallower) one (a barrier that guarantees
    the child -> parent order). Individual errors are collected rather than swallowed, and
    processing continues.

    With overwrite=False (default), conflict items are skipped. With overwrite=True they are
    also attempted, replacing an existing file destination (see _rename_one). record_cb, if
    given, is called once per processed item for logging.
    """
    workers = max(1, workers)
    result = RenameResult()

    buckets: dict[int, list[RenameItem]] = defaultdict(list)
    for i in items:
        if i.status == "ok" or overwrite:
            buckets[i.depth].append(i)
        else:
            # Conflict items are skipped up front when not overwriting.
            result.skipped += 1
            if record_cb is not None:
                record_cb("rename", "skipped", i.kind, i.src, i.dst, "conflict")
    total = sum(len(v) for v in buckets.values())

    processed = [0]
    lock = threading.Lock()
    # Map an internal outcome to the logged status label.
    status_label = {"ok": "renamed", "overwritten": "overwritten", "skip": "skipped", "err": "error"}

    for d in sorted(buckets, reverse=True):
        group = buckets[d]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_rename_one, it, overwrite): it for it in group}
            for fut in as_completed(futs):
                outcome, err = fut.result()
                item = futs[fut]
                with lock:
                    if outcome == "ok":
                        result.renamed += 1
                    elif outcome == "overwritten":
                        result.overwritten += 1
                    elif outcome == "skip":
                        result.skipped += 1
                    else:
                        result.errors.append(err)  # type: ignore[arg-type]
                    processed[0] += 1
                    processed_now = processed[0]
                if record_cb is not None:
                    record_cb("rename", status_label[outcome], item.kind, item.src, item.dst,
                              err.message if err else "")
                if progress_cb is not None:
                    progress_cb(processed_now, total, item.src)
    return result
