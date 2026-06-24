"""Tests for the added features: --form, --sanitize, --junk-glob, --prune-empty."""

import json
import os
import unicodedata

from fsprep import (
    sanitize_name,
    scan,
    scan_renames,
    target_name,
    prune_empty_dirs,
)
from fsprep.normalize import classify
from fsprep.__main__ import main


# --- Normalization form ---------------------------------------------------------------


def test_target_name_forms():
    # Full-width digits fold to ASCII only under the NFK* (compatibility) forms.
    assert target_name("１２３", form="NFC") == "１２３"
    assert target_name("１２３", form="NFKC") == "123"
    # NFD decomposes; NFC composes back.
    composed = "ガ"
    assert target_name(composed, form="NFD") == unicodedata.normalize("NFD", composed)


def test_nfkc_rename_folds_fullwidth(tmp_path):
    (tmp_path / "ＡＢＣ.txt").write_text("x", encoding="utf-8")
    plan = scan_renames(str(tmp_path), form="NFKC")
    assert len(plan) == 1
    assert os.path.basename(plan[0].dst) == "ABC.txt"


def test_default_form_leaves_fullwidth_untouched(tmp_path):
    # Under the default NFC, full-width letters are already normalized -> no rename.
    (tmp_path / "ＡＢＣ.txt").write_text("x", encoding="utf-8")
    assert scan_renames(str(tmp_path)) == []


# --- Sanitize -------------------------------------------------------------------------


def test_sanitize_name_rules():
    assert sanitize_name('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h"
    assert sanitize_name("trailing.  ") == "trailing"   # trailing dots/spaces dropped
    assert sanitize_name("...") == "_"                  # would-be-empty -> "_"
    assert sanitize_name("CON") == "_CON"               # reserved device name escaped
    assert sanitize_name("nul.txt") == "_nul.txt"       # reserved with extension, any case
    assert sanitize_name("ok_name.txt") == "ok_name.txt"
    assert sanitize_name("a\tb") == "a_b"               # control char


# Note: a filename containing illegal chars / trailing dots / reserved names cannot be
# *created* on a Windows filesystem in the first place, so sanitize's effect on the rename
# plan is exercised through normalizer.classify (synthetic paths) rather than real files.
# That mirrors the real use case: sanitizing a tree that originated on Linux/macOS/an archive.


def test_sanitize_builds_clean_target():
    # Illegal chars become "_".
    v = classify("/src/a:b?.txt", "a:b?.txt", False, sanitize=True)
    assert os.path.basename(v.item.dst) == "a_b_.txt"
    # Trailing dot is stripped.
    v2 = classify("/src/name.", "name.", True, sanitize=True)
    assert os.path.basename(v2.item.dst) == "name"


def test_target_name_form_then_sanitize():
    # NFKC folds the full-width letters and colon, then sanitize rewrites the colon.
    assert target_name("ＡＢ：Ｃ", form="NFKC", sanitize=True) == "AB_C"


def test_sanitize_off_leaves_illegal_chars():
    # Without sanitize, an already-NFC name with an illegal char is not a rename target.
    v = classify("/src/a:b.txt", "a:b.txt", False, sanitize=False)
    assert v.item is None


# --- Custom junk globs ----------------------------------------------------------------


def test_junk_glob_matches(tmp_path):
    (tmp_path / "scratch.tmp").write_text("x", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    sr = scan(str(tmp_path), include_junk=True, junk_globs=["*.tmp"])
    names = {os.path.basename(j.path) for j in sr.junk}
    assert names == {"scratch.tmp"}


def test_junk_glob_case_insensitive(tmp_path):
    (tmp_path / "DATA.TMP").write_text("x", encoding="utf-8")
    sr = scan(str(tmp_path), include_junk=True, junk_globs=["*.tmp"])
    assert len(sr.junk) == 1


# --- Prune empty dirs -----------------------------------------------------------------


def test_prune_empty_cascades(tmp_path):
    # a/b/c are all empty -> all three removed bottom-up; a sibling with a file survives.
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "f.txt").write_text("x", encoding="utf-8")

    removed, errors = prune_empty_dirs(str(tmp_path))
    assert errors == []
    assert removed == 3
    assert not (tmp_path / "a").exists()
    assert (tmp_path / "keep" / "f.txt").exists()


def test_prune_does_not_remove_root(tmp_path):
    removed, errors = prune_empty_dirs(str(tmp_path))
    assert removed == 0
    assert tmp_path.exists()


def test_scan_collects_empty_dirs(tmp_path):
    (tmp_path / "empty").mkdir()
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "f.txt").write_text("x", encoding="utf-8")
    sr = scan(str(tmp_path), collect_empty=True)
    assert [os.path.basename(p) for p in sr.empty_dirs] == ["empty"]


# --- CLI integration ------------------------------------------------------------------


def test_cli_prune_empty_after_junk(tmp_path):
    # A directory that holds only junk becomes empty after cleanup, then is pruned.
    onlyjunk = tmp_path / "onlyjunk"
    onlyjunk.mkdir()
    (onlyjunk / ".DS_Store").write_text("x", encoding="utf-8")

    rc = main(["all", str(tmp_path), "--apply", "-y"])
    assert rc == 0
    assert not onlyjunk.exists()  # emptied by junk removal, then pruned


def test_cli_json_reports_form_and_empty(tmp_path, capsys):
    (tmp_path / "ＡＢＣ.txt").write_text("x", encoding="utf-8")
    (tmp_path / "empty").mkdir()

    rc = main(["all", str(tmp_path), "--json", "--form", "NFKC"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["form"] == "NFKC"
    assert out["rename_total"] == 1
    assert out["empty_total"] == 1


def test_cli_normalize_only_keeps_junk(tmp_path):
    # The 'normalize' subcommand renames but never touches junk.
    (tmp_path / "ＡＢＣ.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".DS_Store").write_text("x", encoding="utf-8")

    rc = main(["normalize", str(tmp_path), "--apply", "-y", "--form", "NFKC"])
    assert rc == 0
    assert (tmp_path / "ABC.txt").exists()
    assert (tmp_path / ".DS_Store").exists()  # junk untouched


def test_cli_clean_only_keeps_names(tmp_path):
    # The 'clean' subcommand deletes junk but never renames.
    (tmp_path / "ＡＢＣ.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".DS_Store").write_text("x", encoding="utf-8")

    rc = main(["clean", str(tmp_path), "--apply", "-y"])
    assert rc == 0
    assert not (tmp_path / ".DS_Store").exists()
    assert (tmp_path / "ＡＢＣ.txt").exists()  # full-width name left as-is


def test_cli_sanitize_and_nfkc_combined(tmp_path):
    # End-to-end form+sanitize pipeline on a name creatable on Windows: NFKC folds the
    # full-width letters; the interior space is legal and preserved by sanitize.
    (tmp_path / "ＦＯＯ .txt").write_text("x", encoding="utf-8")
    rc = main(["normalize", str(tmp_path), "--apply", "-y", "--form", "NFKC", "--sanitize"])
    assert rc == 0
    assert (tmp_path / "FOO .txt").exists()
