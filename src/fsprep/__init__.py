"""fsprep: prepare a filesystem tree for archival/transfer.

Three passes that always run in the order clean -> normalize -> prune:

- ``clean``    -- detect and delete OS-generated junk (.DS_Store, ._* AppleDouble, ...),
                    and prune empty directories
- ``normalize`` -- rename file/folder names to a normalization form (NFC by default),
                    optionally sanitizing illegal characters

``scan`` walks the tree once and produces the plans together; ``core`` holds the shared
threaded directory walker and the error/log types used by the passes. The CLI (``fsprep``)
exposes each pass as a subcommand (``normalize`` / ``clean`` / ``prune``) plus ``all``.
"""

from __future__ import annotations

from .clean import CleanResult, JunkItem, clean_junk, match_junk
from .core import DEFAULT_WORKERS, ApplyError
from .normalize import (
    FORMS,
    RenameItem,
    RenameResult,
    apply_renames,
    needs_nfc,
    sanitize_name,
    target_name,
    to_nfc,
)
from .prune import prune_empty_dirs
from .scan import ScanResult, scan, scan_renames

__all__ = [
    "DEFAULT_WORKERS",
    "ApplyError",
    # clean
    "JunkItem",
    "CleanResult",
    "match_junk",
    "clean_junk",
    "prune_empty_dirs",
    # normalize
    "FORMS",
    "RenameItem",
    "RenameResult",
    "needs_nfc",
    "to_nfc",
    "sanitize_name",
    "target_name",
    "apply_renames",
    # scan
    "ScanResult",
    "scan",
    "scan_renames",
]
