"""Tests for the cleaner pass (OS junk detection and deletion)."""

import os

from fsprep import clean_junk, match_junk, scan


def test_match_junk():
    assert match_junk(".DS_Store", False) == ".DS_Store"
    assert match_junk("THUMBS.DB", False) == "Thumbs.db"  # case-insensitive
    assert match_junk("._photo.jpg", False) == "AppleDouble ._*"
    assert match_junk("__MACOSX", True) == "__MACOSX"
    assert match_junk("normal.txt", False) is None
    assert match_junk(".Spotlight-V100", False) is None  # dir-only rule, not a file


def test_match_junk_covers_veto_list():
    # Every entry in the typical NAS SMB `veto files` list must be recognized (dirs as dirs).
    veto_files = [".DS_Store", ".apdisk", "Thumbs.db", "desktop.ini"]
    veto_dirs = [
        ".AppleDouble", ".AppleDB", ".AppleDesktop", ".TemporaryItems", ".Trashes",
        ".fseventsd", ".Spotlight-V100", ".DocumentRevisions-V100", "$RECYCLE.BIN",
    ]
    for name in veto_files:
        assert match_junk(name, False) is not None, name
    for name in veto_dirs:
        assert match_junk(name, True) is not None, name
    # ._* AppleDouble sidecars (the veto `._*` glob) are matched by prefix.
    assert match_junk("._anything", False) == "AppleDouble ._*"
    # Case-insensitive, e.g. Windows recycle bin.
    assert match_junk("$recycle.bin", True) == "$RECYCLE.BIN"


def test_scan_collects_junk_and_skips_descending(tmp_path):
    (tmp_path / ".DS_Store").write_text("x", encoding="utf-8")
    (tmp_path / "._resource").write_text("x", encoding="utf-8")
    real = tmp_path / "keep.txt"
    real.write_text("data", encoding="utf-8")
    # A junk directory whose contents must NOT be scanned individually.
    macosx = tmp_path / "__MACOSX"
    macosx.mkdir()
    (macosx / "ignored_inside.txt").write_text("x", encoding="utf-8")

    sr = scan(str(tmp_path), include_junk=True)
    paths = {os.path.basename(j.path) for j in sr.junk}
    assert paths == {".DS_Store", "._resource", "__MACOSX"}
    # The junk dir is recorded once, not descended into.
    assert sum(1 for j in sr.junk if j.kind == "dir") == 1
    # Without include_junk, junk is ignored entirely.
    assert scan(str(tmp_path), include_junk=False).junk == []


def test_clean_junk_removes(tmp_path):
    (tmp_path / ".DS_Store").write_text("x", encoding="utf-8")
    (tmp_path / "._resource").write_text("x", encoding="utf-8")
    macosx = tmp_path / "__MACOSX"
    macosx.mkdir()
    (macosx / "inside.txt").write_text("x", encoding="utf-8")
    keep = tmp_path / "keep.txt"
    keep.write_text("data", encoding="utf-8")

    sr = scan(str(tmp_path), include_junk=True)
    result = clean_junk(sr.junk)

    assert result.removed_files == 2
    assert result.removed_dirs == 1
    assert result.errors == []
    # Real content survives; junk is gone.
    assert keep.exists()
    assert not (tmp_path / ".DS_Store").exists()
    assert not macosx.exists()


def test_clean_junk_record_cb_logs_each_item(tmp_path):
    (tmp_path / ".DS_Store").write_text("x", encoding="utf-8")
    (tmp_path / "._resource").write_text("x", encoding="utf-8")

    records = []
    sr = scan(str(tmp_path), include_junk=True)
    clean_junk(sr.junk, record_cb=lambda *row: records.append(row))

    assert len(records) == 2
    assert all(r[0] == "remove" and r[1] == "removed" for r in records)
