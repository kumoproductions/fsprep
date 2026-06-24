"""Single-pass scan that produces both the clean and normalize plans.

The clean (junk) and normalize (rename) passes apply in sequence, but scanning the tree
twice would double the directory round-trips -- the dominant cost on network drives. So a
single parallel walk classifies each entry against both concerns at once: junk is matched
first (and short-circuits, since junk is never renamed and junk directories are not
descended into), otherwise the entry is evaluated for NFC renaming.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import clean, normalize
from .core import DEFAULT_WORKERS, Classified, walk
from .clean import JunkItem
from .normalize import RenameItem


@dataclass
class ScanResult:
    renames: list[RenameItem] = field(default_factory=list)
    junk: list[JunkItem] = field(default_factory=list)
    empty_dirs: list[str] = field(default_factory=list)


def scan(
    root: str,
    workers: int = DEFAULT_WORKERS,
    follow_symlinks: bool = False,
    recursive: bool = True,
    include_junk: bool = False,
    include_renames: bool = True,
    form: str = "NFC",
    sanitize: bool = False,
    junk_globs: list[str] | None = None,
    collect_empty: bool = False,
    progress_cb: Callable[[int], None] | None = None,
) -> ScanResult:
    """Scan under root once; return the rename plan plus (optionally) the junk/empty lists.

    The rename plan targets the requested normalization ``form`` (optionally sanitized; see
    normalize.classify), and is only built when include_renames is True. When include_junk is
    True, OS junk (see clean.match_junk, extended by junk_globs) is collected and junk
    directories are not descended into. When collect_empty is True, directories that are empty at
    scan time are recorded (a lower bound -- more may empty out once junk is removed; the actual
    prune happens at apply time). The rename plan is returned deepest-first so children are
    renamed before their parents (renaming a parent first would invalidate the children's
    captured paths).
    """

    def classify(path: str, name: str, is_dir: bool) -> Classified:
        if include_junk:
            verdict = clean.classify(path, name, is_dir, extra_globs=junk_globs)
            if verdict.item is not None:
                return verdict
        if include_renames:
            return normalize.classify(path, name, is_dir, form=form, sanitize=sanitize)
        return Classified(item=None, descend=True)

    empty_dirs: list[str] = []
    items = walk(
        root,
        classify,
        workers=workers,
        follow_symlinks=follow_symlinks,
        recursive=recursive,
        progress_cb=progress_cb,
        on_empty_dir=empty_dirs.append if collect_empty else None,
    )

    renames = [i for i in items if isinstance(i, RenameItem)]
    junk = [i for i in items if isinstance(i, JunkItem)]
    # Deepest first (child -> parent): renaming a parent first would break children's paths.
    renames.sort(key=lambda i: i.depth, reverse=True)
    return ScanResult(renames=renames, junk=junk, empty_dirs=empty_dirs)


def scan_renames(
    root: str,
    workers: int = DEFAULT_WORKERS,
    follow_symlinks: bool = False,
    recursive: bool = True,
    form: str = "NFC",
    sanitize: bool = False,
    progress_cb: Callable[[int], None] | None = None,
) -> list[RenameItem]:
    """Convenience wrapper: return only the rename plan (no junk collection)."""
    return scan(
        root,
        workers=workers,
        follow_symlinks=follow_symlinks,
        recursive=recursive,
        include_junk=False,
        form=form,
        sanitize=sanitize,
        progress_cb=progress_cb,
    ).renames
