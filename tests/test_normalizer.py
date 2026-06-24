"""Tests for the normalizer pass (NFD -> NFC rename)."""

import os
import unicodedata

from fsprep import (
    apply_renames,
    needs_nfc,
    scan_renames,
)

# NFD names containing dakuten/handakuten (mimicking macOS-origin names).
NFD_DIR = unicodedata.normalize("NFD", "がぎぐ_テスト")
NFD_FILE = unicodedata.normalize("NFD", "ぱぴぷ.txt")


def test_needs_nfc():
    assert needs_nfc(NFD_DIR) is True
    assert needs_nfc(unicodedata.normalize("NFC", NFD_DIR)) is False
    assert needs_nfc("plain_ascii.txt") is False


def test_plan_orders_children_before_parents(tmp_path):
    d = tmp_path / NFD_DIR
    d.mkdir()
    (d / NFD_FILE).write_text("x", encoding="utf-8")

    plan = scan_renames(str(tmp_path), workers=4)
    assert len(plan) == 2
    # Deeper entry (the file) comes first, the parent directory last.
    assert plan[0].kind == "file"
    assert plan[1].kind == "dir"
    assert plan[0].depth > plan[1].depth


def test_apply_renames_makes_names_nfc(tmp_path):
    d = tmp_path / NFD_DIR
    d.mkdir()
    (d / NFD_FILE).write_text("x", encoding="utf-8")

    plan = scan_renames(str(tmp_path), workers=4)
    result = apply_renames(plan, workers=4)

    assert result.renamed == 2
    assert result.errors == []
    # Walk the tree and confirm every name is now NFC.
    for root, dirs, files in os.walk(str(tmp_path)):
        for name in dirs + files:
            assert unicodedata.is_normalized("NFC", name)


def test_idempotent(tmp_path):
    d = tmp_path / NFD_DIR
    d.mkdir()
    (d / NFD_FILE).write_text("x", encoding="utf-8")

    apply_renames(scan_renames(str(tmp_path)))
    # A second pass should find nothing to do.
    assert scan_renames(str(tmp_path)) == []


def test_conflict_detected_and_skipped(tmp_path):
    # Create both an NFD name and its NFC form -> conflict.
    nfd_name = unicodedata.normalize("NFD", "ガ.txt")
    nfc_name = unicodedata.normalize("NFC", "ガ.txt")
    assert nfd_name != nfc_name
    (tmp_path / nfd_name).write_text("a", encoding="utf-8")
    (tmp_path / nfc_name).write_text("b", encoding="utf-8")

    plan = scan_renames(str(tmp_path))
    conflicts = [i for i in plan if i.status == "conflict"]
    assert len(conflicts) == 1

    result = apply_renames(plan)
    assert result.renamed == 0
    assert result.skipped == 1
    # Both files should still exist.
    assert (tmp_path / nfd_name).exists()
    assert (tmp_path / nfc_name).exists()


def test_conflict_overwrite_replaces_existing_file(tmp_path):
    # NFD source ("a") and an existing NFC destination ("b").
    nfd_name = unicodedata.normalize("NFD", "ガ.txt")
    nfc_name = unicodedata.normalize("NFC", "ガ.txt")
    (tmp_path / nfd_name).write_text("a", encoding="utf-8")
    (tmp_path / nfc_name).write_text("b", encoding="utf-8")

    plan = scan_renames(str(tmp_path))
    result = apply_renames(plan, overwrite=True)

    assert result.overwritten == 1
    assert result.renamed == 0
    # Only the NFC name remains, now holding the source's content.
    names = os.listdir(str(tmp_path))
    assert len(names) == 1
    assert unicodedata.is_normalized("NFC", names[0])
    assert (tmp_path / nfc_name).read_text(encoding="utf-8") == "a"


def test_conflict_overwrite_skips_directory(tmp_path):
    # When the destination is a directory, overwrite must not destroy it.
    nfd_name = unicodedata.normalize("NFD", "ガ")
    nfc_name = unicodedata.normalize("NFC", "ガ")
    (tmp_path / nfd_name).mkdir()
    (tmp_path / nfc_name).mkdir()
    (tmp_path / nfc_name / "keep.txt").write_text("keep", encoding="utf-8")

    plan = scan_renames(str(tmp_path))
    result = apply_renames(plan, overwrite=True)

    assert result.overwritten == 0
    assert result.skipped == 1
    # The existing NFC directory and its content are preserved.
    assert (tmp_path / nfc_name / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_apply_record_cb_logs_each_item(tmp_path):
    d = tmp_path / NFD_DIR
    d.mkdir()
    (d / NFD_FILE).write_text("x", encoding="utf-8")

    records = []
    plan = scan_renames(str(tmp_path))
    apply_renames(plan, record_cb=lambda *row: records.append(row))

    assert len(records) == 2
    assert all(r[0] == "rename" and r[1] == "renamed" for r in records)


def test_deep_nested_tree_parallel(tmp_path):
    """A multi-level, multi-directory tree is fully normalized by the parallel scan/apply."""
    expected = 0
    for d in range(8):
        sub = tmp_path / f"dir{d}" / unicodedata.normalize("NFD", f"ば{d}")
        sub.mkdir(parents=True)
        expected += 1  # the NFD directory
        for f in range(5):
            (sub / unicodedata.normalize("NFD", f"ぴ{f}.txt")).write_text("x", encoding="utf-8")
            expected += 1  # the NFD file

    plan = scan_renames(str(tmp_path), workers=16)
    assert len(plan) == expected
    # Depth is monotonic (children before parents).
    depths = [i.depth for i in plan]
    assert depths == sorted(depths, reverse=True)

    result = apply_renames(plan, workers=16)
    assert result.renamed == expected
    assert result.errors == []
    for root, dirs, files in os.walk(str(tmp_path)):
        for name in dirs + files:
            assert unicodedata.is_normalized("NFC", name)
    # Idempotent.
    assert scan_renames(str(tmp_path)) == []
