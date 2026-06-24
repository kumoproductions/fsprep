"""fsprep CLI.

Prepare a filesystem tree for archival/transfer. Each pass is a subcommand, and ``all`` runs
the full suite in the fixed order clean -> normalize -> prune:

1. clean     -- delete OS junk (.DS_Store, ._* AppleDouble, Thumbs.db, ...) and custom globs
2. normalize -- rename names to a normalization form (NFC by default), optionally sanitized
3. prune     -- remove empty directories (bottom-up)

Junk is removed before renaming so the captured paths stay valid, and pruning runs last
against the live tree. The tool defaults to a dry run (preview); use --apply (or confirm
interactively) to make changes, or -n/--dry-run to force preview-only. Depends only on the
Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time

from .clean import JunkItem, clean_junk
from .core import DEFAULT_WORKERS
from .normalize import FORMS, RenameItem, apply_renames
from .prune import prune_empty_dirs
from .scan import scan


def _eprint(msg: str = "", end: str = "\n") -> None:
    """Write progress/info to stderr (so it does not pollute --json stdout)."""
    sys.stderr.write(msg + end)
    sys.stderr.flush()


def _force_utf8_io() -> None:
    """Ensure stdout/stderr use UTF-8 so non-ASCII names survive redirection on Windows.

    When output is piped or redirected on Windows, Python defaults to the locale code page
    (e.g. cp1252), which cannot encode most non-ASCII filenames and would raise
    UnicodeEncodeError on the JSON/preview output. Streams that do not support reconfigure
    (e.g. pytest's capture) are left untouched.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def _fmt(n: int) -> str:
    return f"{n:,}"


class _Throttle:
    """Time-based output rate limiter (safe to call from multiple threads)."""

    def __init__(self, interval: float = 0.1) -> None:
        self.interval = interval
        self._last = 0.0
        self._lock = threading.Lock()

    def ready(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._last >= self.interval:
                self._last = now
                return True
            return False


class _LogWriter:
    """Streaming CSV operation log (thread-safe)."""

    COLUMNS = ["action", "status", "kind", "src", "dst", "message"]

    def __init__(self, path: str) -> None:
        self._f = open(path, "w", newline="", encoding="utf-8")
        self._w = csv.writer(self._f)
        self._w.writerow(self.COLUMNS)
        self._lock = threading.Lock()

    def record(self, action: str, status: str, kind: str, src: str, dst: str, message: str) -> None:
        with self._lock:
            self._w.writerow([action, status, kind, src, dst, message])

    def close(self) -> None:
        self._f.close()


def _make_progress(label: str):
    """Build a progress callback that renders a single-line bar on stderr."""
    throttle = _Throttle()
    start = time.monotonic()

    def cb(processed: int, tot: int, current: str) -> None:
        if tot == 0:
            return
        if processed != tot and not throttle.ready():
            return
        pct = processed / tot if tot else 1.0
        bar_w = 24
        filled = int(bar_w * pct)
        bar = "#" * filled + "-" * (bar_w - filled)
        elapsed = time.monotonic() - start
        rate = f"  ({processed / elapsed:,.0f}/s)" if elapsed > 0.3 else ""
        _eprint(f"\r{label} {bar} {pct * 100:3.0f}%  {_fmt(processed)}/{_fmt(tot)}{rate}   ", end="")

    return cb


# --- Scan -----------------------------------------------------------------------------


def _scan(path: str, workers: int, recursive: bool, include_junk: bool, quiet: bool,
          *, include_renames: bool = True, form: str = "NFC", sanitize: bool = False,
          junk_globs: list[str] | None = None, collect_empty: bool = False):
    throttle = _Throttle()
    start = time.monotonic()

    def on_count(n: int) -> None:
        if not quiet and throttle.ready():
            _eprint(f"\rScanning... {_fmt(n)} entries", end="")

    result = scan(path, workers=workers, recursive=recursive, include_junk=include_junk,
                  include_renames=include_renames, form=form, sanitize=sanitize,
                  junk_globs=junk_globs, collect_empty=collect_empty, progress_cb=on_count)
    if not quiet:
        dt = time.monotonic() - start
        parts = []
        if include_renames:
            parts.append(f"{_fmt(len(result.renames))} rename target(s)")
        if include_junk:
            parts.append(f"{_fmt(len(result.junk))} junk")
        if collect_empty:
            parts.append(f"{_fmt(len(result.empty_dirs))} empty dir(s)")
        _eprint(f"\rScan complete: {', '.join(parts) or 'nothing'} ({dt:.1f}s)" + " " * 12)
    return result


# --- Preview --------------------------------------------------------------------------


def _print_preview(items: list[RenameItem], limit: int, show_conflicts: bool) -> None:
    conflicts = [i for i in items if i.status == "conflict"]
    ok = [i for i in items if i.status == "ok"]
    _eprint(f"Rename targets: {_fmt(len(items))}  (renamable {_fmt(len(ok))} / conflicts {_fmt(len(conflicts))})")
    if items:
        shown = items if show_conflicts else ok
        head = shown if limit <= 0 else shown[:limit]
        for it in head:
            tag = "dir " if it.kind == "dir" else "file"
            mark = "  [conflict]" if it.status == "conflict" else ""
            _eprint(f"  [{tag}] {it.src}  ->  {it.dst}{mark}")
        if limit > 0 and len(shown) > limit:
            _eprint(f"  ... and {_fmt(len(shown) - limit)} more (use --full to show all)")
        if conflicts and not show_conflicts:
            _eprint(f"{_fmt(len(conflicts))} conflict(s): the target name already exists (use --show-conflicts to list them)")


def _print_junk_preview(junk: list[JunkItem], limit: int) -> None:
    _eprint(f"Junk to delete: {_fmt(len(junk))}")
    head = junk if limit <= 0 else junk[:limit]
    for it in head:
        tag = "dir " if it.kind == "dir" else "file"
        _eprint(f"  [{tag}] {it.path}  ({it.reason})")
    if limit > 0 and len(junk) > limit:
        _eprint(f"  ... and {_fmt(len(junk) - limit)} more (use --full to show all)")


def _print_empty_preview(empty_dirs: list[str], limit: int) -> None:
    _eprint(f"Empty dirs to prune: {_fmt(len(empty_dirs))} (now; more may empty out after cleanup)")
    head = empty_dirs if limit <= 0 else empty_dirs[:limit]
    for p in head:
        _eprint(f"  [dir ] {p}")
    if limit > 0 and len(empty_dirs) > limit:
        _eprint(f"  ... and {_fmt(len(empty_dirs) - limit)} more (use --full to show all)")


# --- Prompts --------------------------------------------------------------------------


def _prompt_conflict(conflicts: int) -> str | None:
    """Ask how to handle conflicts. Returns "skip" | "overwrite" | None (cancel)."""
    if not sys.stdin.isatty():
        return "skip"
    while True:
        try:
            ans = input(
                f"{_fmt(conflicts)} conflict(s) found. [s]kip / [o]verwrite existing / [c]ancel? [s]: "
            ).strip().lower()
        except EOFError:
            return "skip"
        if ans in ("", "s", "skip"):
            return "skip"
        if ans in ("o", "overwrite"):
            return "overwrite"
        if ans in ("c", "cancel"):
            return None


def _prompt_yes_no(question: str) -> bool:
    """Ask a single yes/no question (default no). Returns False on EOF/non-tty."""
    if not sys.stdin.isatty():
        return False
    try:
        ans = input(f"{question} [y/N]: ")
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes")


# --- Outcome --------------------------------------------------------------------------


def _print_outcome(rres, cres, pres, quiet: bool) -> int:
    """Report the outcome. pres is (removed_dirs, errors) from the prune pass, or None."""
    errors = (cres.errors if cres else []) + (rres.errors if rres else []) + (list(pres[1]) if pres else [])
    if quiet:
        out: dict = {"mode": "apply"}
        if cres is not None:
            out["clean"] = {
                "removed_files": cres.removed_files, "removed_dirs": cres.removed_dirs,
                "errors": [vars(e) for e in cres.errors],
            }
        if rres is not None:
            out["rename"] = {
                "renamed": rres.renamed, "overwritten": rres.overwritten,
                "skipped": rres.skipped, "errors": [vars(e) for e in rres.errors],
            }
        if pres is not None:
            out["prune"] = {"removed_dirs": pres[0], "errors": [vars(e) for e in pres[1]]}
        print(json.dumps(out, ensure_ascii=False))
        return 1 if errors else 0
    if cres is not None:
        _eprint(f"Removed junk: {_fmt(cres.removed_files)} file(s) + {_fmt(cres.removed_dirs)} dir(s)"
                f" / errors {_fmt(len(cres.errors))}")
    if rres is not None:
        _eprint(f"Renamed {_fmt(rres.renamed)} / overwritten {_fmt(rres.overwritten)}"
                f" / skipped {_fmt(rres.skipped)} / errors {_fmt(len(rres.errors))}")
    if pres is not None:
        _eprint(f"Pruned empty: {_fmt(pres[0])} dir(s) / errors {_fmt(len(pres[1]))}")
    for e in errors[:20]:
        _eprint(f"  [error] {e.src}{(' -> ' + e.dst) if e.dst else ''}: {e.message}")
    if len(errors) > 20:
        _eprint(f"  ... and {_fmt(len(errors) - 20)} more error(s)")
    return 1 if errors else 0


# --- Apply (clean -> normalize -> prune) ----------------------------------------------


def _apply(
    renames: list[RenameItem],
    junk: list[JunkItem],
    *,
    overwrite: bool,
    workers: int,
    quiet: bool,
    log_path: str | None,
    prune_root: str | None = None,
    recursive: bool = True,
) -> int:
    """Run the passes in order (clean junk, rename, then prune) and report the outcome.

    Junk is removed BEFORE renaming: renaming an ancestor directory would invalidate the
    junk paths captured at scan time. Empty-directory pruning runs LAST, against the live
    tree, because what is empty depends on the earlier passes. Progress bars are suppressed
    in quiet (--json) mode.
    """
    log = _LogWriter(log_path) if log_path else None
    rcb = log.record if log else None
    try:
        cres = None
        if junk:
            cres = clean_junk(
                junk, workers=workers,
                progress_cb=None if quiet else _make_progress("Cleaning"), record_cb=rcb,
            )
            if not quiet:
                _eprint()
        rres = None
        if renames:
            rres = apply_renames(
                renames, workers=workers, overwrite=overwrite,
                progress_cb=None if quiet else _make_progress("Renaming"), record_cb=rcb,
            )
            if not quiet:
                _eprint()
        pres = None
        if prune_root:
            pres = prune_empty_dirs(prune_root, recursive=recursive, record_cb=rcb)
        rc = _print_outcome(rres, cres, pres, quiet=quiet)
    finally:
        if log:
            log.close()
    if log and not quiet:
        _eprint(f"Log written: {log_path}")
    return rc


# --- Entry point ----------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    """Options shared by every subcommand."""
    p.add_argument("path", help="target folder")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="make changes without prompting (dry run otherwise; an interactive terminal still offers to proceed)")
    mode.add_argument("-n", "--dry-run", action="store_true",
                      help="preview only: never prompt and never make changes")
    p.add_argument("-w", "--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"worker count (default {DEFAULT_WORKERS}; higher for network, lower for local)")
    p.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--log", metavar="FILE", default=None, help="write a CSV operation log of all changes")
    p.add_argument("--no-recursive", action="store_true", help="direct children only (do not descend into subfolders)")
    p.add_argument("--full", action="store_true", help="show the full preview (default is the first 50)")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout (never prompts)")


def _add_normalize_opts(p: argparse.ArgumentParser) -> None:
    """Options for the name-normalization pass (normalize / all)."""
    p.add_argument("--form", choices=list(FORMS), default="NFC",
                   help="unicode normalization form for names (default NFC; NFKC/NFKD also fold"
                   " compatibility characters, e.g. full-width -> half-width)")
    p.add_argument("--sanitize", action="store_true",
                   help="also rewrite characters illegal on Windows/LTFS (<>:\"|?* and control chars),"
                   " strip trailing dots/spaces, and escape reserved names (CON, NUL, ...)")
    p.add_argument("--on-conflict", choices=["skip", "overwrite"], default=None,
                   help="how to handle conflicts (default: skip; 'overwrite' replaces existing files and is destructive)")
    p.add_argument("--show-conflicts", action="store_true", help="include conflict items in the preview")


def _add_clean_opts(p: argparse.ArgumentParser) -> None:
    """Options for the junk-cleanup pass (clean / all)."""
    p.add_argument("--junk-glob", action="append", metavar="GLOB", default=None,
                   help="additional junk name pattern to delete (case-insensitive fnmatch, repeatable),"
                   " e.g. --junk-glob '*.tmp'")


def _run(args: argparse.Namespace, *, do_normalize: bool, do_clean: bool, do_prune: bool) -> int:
    """Shared driver for all subcommands: scan, preview, decide, then apply the enabled passes."""
    if not os.path.isdir(args.path):
        _eprint(f"Error: folder not found: {args.path}")
        return 2

    quiet = args.json
    recursive = not args.no_recursive
    form = getattr(args, "form", "NFC")
    sanitize = getattr(args, "sanitize", False)
    junk_globs = getattr(args, "junk_glob", None)
    on_conflict = getattr(args, "on_conflict", None)
    show_conflicts = getattr(args, "show_conflicts", False)
    limit = 0 if args.full else 50

    sr = _scan(args.path, workers=args.workers, recursive=recursive,
               include_junk=do_clean, include_renames=do_normalize, collect_empty=do_prune,
               quiet=quiet, form=form, sanitize=sanitize, junk_globs=junk_globs)
    renames = sr.renames if do_normalize else []
    junk = sr.junk if do_clean else []
    empty_dirs = sr.empty_dirs if do_prune else []
    ok = sum(1 for i in renames if i.status == "ok")
    conflicts = sum(1 for i in renames if i.status == "conflict")
    interactive = sys.stdin.isatty() and not args.json

    # --- JSON mode: never prompts. Preview unless --apply. ---
    if args.json:
        if args.apply:
            overwrite = on_conflict == "overwrite"
            return _apply(renames, junk, overwrite=overwrite, workers=args.workers,
                          quiet=True, log_path=args.log,
                          prune_root=args.path if do_prune else None, recursive=recursive)
        out: dict = {"mode": "preview", "command": args.command, "path": args.path}
        if do_normalize:
            out.update(form=form, sanitize=sanitize, renames=[vars(i) for i in renames],
                       rename_total=len(renames), conflicts=conflicts)
        if do_clean:
            out.update(junk=[vars(j) for j in junk], junk_total=len(junk))
        if do_prune:
            out.update(empty_dirs=empty_dirs, empty_total=len(empty_dirs))
        print(json.dumps(out, ensure_ascii=False))
        return 0

    # --- Human mode: show the preview (junk, renames, then empty dirs). ---
    if do_clean and junk:
        _print_junk_preview(junk, limit=limit)
    if do_normalize:
        _print_preview(renames, limit=limit, show_conflicts=show_conflicts)
    if do_prune and empty_dirs:
        _print_empty_preview(empty_dirs, limit=limit)

    has_clean = do_clean and bool(junk)
    has_rename = do_normalize and (ok > 0 or conflicts > 0)
    # Cleaning can empty out directories, so prune has potential work whenever we also clean.
    has_prune = do_prune and (bool(empty_dirs) or has_clean)
    if not has_clean and not has_rename and not has_prune:
        _eprint("Nothing to do.")
        return 0

    # Preview-only when --dry-run, or in non-interactive runs without --apply.
    if args.dry_run or (not args.apply and not interactive):
        if not args.dry_run:
            _eprint("Preview only. Re-run with --apply to make changes.")
        return 0

    # Promptable = the interactive default flow. --apply and -y are promptless (apply defaults).
    promptable = interactive and not args.apply and not args.yes

    # 1. Junk deletion.
    if do_clean and junk:
        do_clean_now = _prompt_yes_no(f"Delete the {_fmt(len(junk))} junk item(s) above?") if promptable else True
    else:
        do_clean_now = False

    # 2. Conflict handling: explicit flag wins; otherwise ask in the interactive flow.
    if on_conflict is not None:
        overwrite = on_conflict == "overwrite"
    elif do_normalize and conflicts and promptable:
        choice = _prompt_conflict(conflicts)
        if choice is None:
            _eprint("Aborted.")
            return 1
        overwrite = choice == "overwrite"
    else:
        overwrite = False

    # 3. Rename confirmation.
    rename_total = sum(1 for i in renames if i.status == "ok" or overwrite) if do_normalize else 0
    if rename_total and promptable:
        ow = f" ({_fmt(conflicts)} OVERWRITE existing, data lost)" if overwrite else ""
        do_rename = _prompt_yes_no(f"Rename {_fmt(rename_total)} item(s){ow}?")
    else:
        do_rename = rename_total > 0

    # 4. Empty-dir pruning.
    if do_prune:
        do_prune_now = _prompt_yes_no("Prune empty directories?") if promptable else True
    else:
        do_prune_now = False

    if not do_clean_now and not do_rename and not do_prune_now:
        if do_normalize and rename_total == 0 and conflicts:
            _eprint("Nothing to rename (all targets are conflicts; use overwrite to replace them).")
        else:
            _eprint("Nothing to do.")
        return 0

    return _apply(
        renames if do_rename else [],
        junk if do_clean_now else [],
        overwrite=overwrite, workers=args.workers, quiet=False, log_path=args.log,
        prune_root=args.path if do_prune_now else None, recursive=recursive,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fsprep",
        description="Prepare a filesystem tree for archival/transfer. Each pass is a subcommand;"
        " 'all' runs the full suite (clean -> normalize -> prune). Dry run by default; use"
        " --apply (or confirm interactively) to make changes.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{normalize,clean,prune,all}")

    pn = sub.add_parser("normalize", aliases=["norm"],
                        help="rename names to a normalization form (NFC by default), optionally sanitized")
    _add_common(pn)
    _add_normalize_opts(pn)

    pc = sub.add_parser("clean", help="delete OS-generated junk files/folders")
    _add_common(pc)
    _add_clean_opts(pc)

    pp = sub.add_parser("prune", help="remove empty directories (bottom-up)")
    _add_common(pp)

    pa = sub.add_parser("all", help="full suite: clean -> normalize -> prune")
    _add_common(pa)
    _add_normalize_opts(pa)
    _add_clean_opts(pa)

    args = parser.parse_args(argv)
    _force_utf8_io()

    cmd = args.command
    do_normalize = cmd in ("normalize", "norm", "all")
    do_clean = cmd in ("clean", "all")
    do_prune = cmd in ("prune", "all")
    return _run(args, do_normalize=do_normalize, do_clean=do_clean, do_prune=do_prune)


if __name__ == "__main__":
    sys.exit(main())
