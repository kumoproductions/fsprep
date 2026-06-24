"""Integration tests for the subcommand CLI (normalize / clean / prune / all)."""

import json
import os
import unicodedata

import fsprep.__main__ as cli
from fsprep.__main__ import main


class _FakeStdin:
    """Stand-in for an interactive terminal (isatty() -> True)."""

    def isatty(self):
        return True


def _interactive(monkeypatch, answers):
    """Simulate an interactive tty feeding the given prompt answers in order."""
    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin())
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(it))


def test_all_cleans_before_rename_for_junk_under_nfd_dir(tmp_path):
    # A junk file lives inside an NFD-named directory that will be renamed.
    nfd_dir = tmp_path / unicodedata.normalize("NFD", "がぎ")
    nfd_dir.mkdir()
    (nfd_dir / unicodedata.normalize("NFD", "ぱ.txt")).write_text("real", encoding="utf-8")
    (nfd_dir / "._ぱ.txt").write_text("junk", encoding="utf-8")
    (tmp_path / ".DS_Store").write_text("junk", encoding="utf-8")

    rc = main(["all", str(tmp_path), "--apply", "-y"])
    assert rc == 0

    # No junk anywhere, and every remaining name is NFC.
    leftover = []
    for root, dirs, files in os.walk(str(tmp_path)):
        for n in dirs + files:
            assert unicodedata.is_normalized("NFC", n)
            if n == ".DS_Store" or n.startswith("._"):
                leftover.append(n)
    assert leftover == []


def test_dry_run_makes_no_changes(tmp_path):
    # --dry-run must preview only: nothing renamed, no junk deleted, nothing pruned.
    nfd_dir = tmp_path / unicodedata.normalize("NFD", "がぎ")
    nfd_dir.mkdir()
    (tmp_path / ".DS_Store").write_text("junk", encoding="utf-8")

    rc = main(["all", str(tmp_path), "--dry-run"])
    assert rc == 0

    # Everything is untouched.
    assert nfd_dir.exists()
    assert (tmp_path / ".DS_Store").exists()


def test_all_json_preview_lists_renames_and_junk(tmp_path, capsys):
    nfd_dir = tmp_path / unicodedata.normalize("NFD", "がぎ")
    nfd_dir.mkdir()
    (nfd_dir / "keep.txt").write_text("x", encoding="utf-8")  # keep the dir non-empty
    (tmp_path / ".DS_Store").write_text("junk", encoding="utf-8")

    rc = main(["all", str(tmp_path), "--json"])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "preview"
    assert out["command"] == "all"
    assert out["rename_total"] == 1
    assert out["junk_total"] == 1
    # Untouched (preview only).
    assert nfd_dir.exists()
    assert (tmp_path / ".DS_Store").exists()


def test_normalize_json_apply_keeps_junk(tmp_path, capsys):
    # The 'normalize' subcommand renames but does not look at junk at all.
    nfd_dir = tmp_path / unicodedata.normalize("NFD", "がぎ")
    nfd_dir.mkdir()
    (tmp_path / ".DS_Store").write_text("junk", encoding="utf-8")

    rc = main(["normalize", str(tmp_path), "--json", "--apply"])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert "clean" not in out  # nothing cleaned
    assert out["rename"]["renamed"] == 1
    assert (tmp_path / ".DS_Store").exists()  # junk survives


def test_clean_interactive_offers_junk_deletion(tmp_path, monkeypatch):
    nfd_dir = tmp_path / unicodedata.normalize("NFD", "がぎ")
    nfd_dir.mkdir()
    (tmp_path / ".DS_Store").write_text("junk", encoding="utf-8")

    _interactive(monkeypatch, ["y"])  # delete junk? yes
    rc = main(["clean", str(tmp_path)])
    assert rc == 0

    assert not (tmp_path / ".DS_Store").exists()  # deleted via the offer
    assert nfd_dir.exists()  # clean never renames


def test_clean_interactive_decline_keeps_junk(tmp_path, monkeypatch):
    (tmp_path / ".DS_Store").write_text("junk", encoding="utf-8")

    _interactive(monkeypatch, ["n"])  # delete junk? no
    rc = main(["clean", str(tmp_path)])
    assert rc == 0

    assert (tmp_path / ".DS_Store").exists()  # kept


def test_all_interactive_prompts_each_pass(tmp_path, monkeypatch):
    # 'all' confirms junk, rename, then prune in order.
    nfd_dir = tmp_path / unicodedata.normalize("NFD", "がぎ")
    nfd_dir.mkdir()
    (nfd_dir / "keep.txt").write_text("x", encoding="utf-8")  # keeps nfd_dir from being pruned
    (tmp_path / ".DS_Store").write_text("junk", encoding="utf-8")
    (tmp_path / "empty").mkdir()

    _interactive(monkeypatch, ["y", "y", "y"])  # junk? rename? prune?
    rc = main(["all", str(tmp_path)])
    assert rc == 0

    assert not (tmp_path / ".DS_Store").exists()                       # cleaned
    assert (tmp_path / unicodedata.normalize("NFC", "がぎ")).exists()  # renamed
    assert not (tmp_path / "empty").exists()                          # pruned


def test_prune_subcommand(tmp_path):
    (tmp_path / "empty").mkdir()
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "f.txt").write_text("x", encoding="utf-8")

    rc = main(["prune", str(tmp_path), "--apply", "-y"])
    assert rc == 0
    assert not (tmp_path / "empty").exists()
    assert (tmp_path / "full").exists()
