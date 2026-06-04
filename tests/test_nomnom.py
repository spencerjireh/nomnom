"""Tests for nomnom.py.

Covers the pure-logic surface: gitignore matching, repo scanning,
tree model, selection cascade, output rendering, and the destination/
summary/footer helpers extracted from the picker. The curses `pick`
loop itself is excluded — it needs a TTY.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import nomnom


# ---------- helpers ----------


def _tofu_yes(_req: dict) -> bool:
    """Auto-accept TOFU callback for tests that exercise the happy path
    without an interactive prompt. Receivers must pass this when running
    in a worker thread, since the main-thread guard refuses input() off
    the main thread.
    """
    return True


def make_repo(root: Path, layout: dict) -> None:
    """Create a synthetic file tree.

    layout: nested dict where leaves are str (text content) or bytes (binary).
    """
    for name, val in layout.items():
        p = root / name
        if isinstance(val, dict):
            p.mkdir(parents=True, exist_ok=True)
            make_repo(p, val)
        elif isinstance(val, bytes):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(val)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(val, encoding="utf-8")


def rels(items) -> list:
    return [it.rel for it in items]


# ---------- _glob_to_regex / GitignoreMatcher ----------

class TestGlobToRegex:
    @pytest.mark.parametrize("pattern,path,expected", [
        ("*.log", "foo.log", True),
        ("*.log", "src/foo.log", True),
        ("*.log", "foo.txt", False),
        ("foo", "foo", True),
        ("foo", "bar/foo", True),
        ("foo", "foobar", False),
        ("foo?", "fooz", True),
        ("foo?", "foozz", False),
        ("foo[12]", "foo1", True),
        ("foo[12]", "foo3", False),
    ])
    def test_unanchored(self, pattern, path, expected):
        regex = nomnom._glob_to_regex(pattern, anchored=False)
        assert bool(regex.match(path)) is expected

    @pytest.mark.parametrize("pattern,path,expected", [
        ("build", "build", True),
        ("build", "src/build", False),
        ("a/b", "a/b", True),
        ("a/b", "x/a/b", False),
    ])
    def test_anchored(self, pattern, path, expected):
        regex = nomnom._glob_to_regex(pattern, anchored=True)
        assert bool(regex.match(path)) is expected

    @pytest.mark.parametrize("pattern,path,expected", [
        ("**/foo", "foo", True),
        ("**/foo", "a/foo", True),
        ("**/foo", "a/b/foo", True),
        ("a/**/b", "a/b", True),
        ("a/**/b", "a/x/b", True),
        ("a/**/b", "a/x/y/b", True),
    ])
    def test_double_star(self, pattern, path, expected):
        regex = nomnom._glob_to_regex(pattern, anchored=True)
        assert bool(regex.match(path)) is expected

    def test_path_with_descendants_is_treated_as_match(self):
        # The matcher uses (?:/.*)?$ so "build" matches "build/anything"
        regex = nomnom._glob_to_regex("build", anchored=True)
        assert regex.match("build/x.txt")

    @pytest.mark.parametrize("pattern,path,expected", [
        # `[!...]` is gitignore-style negation; translates to Python `[^...]`.
        ("foo[!12]", "foo3", True),
        ("foo[!12]", "foo1", False),
        ("foo[!12]", "foo2", False),
        ("[!a-c]", "d", True),
        ("[!a-c]", "b", False),
        # Regression: literal char-class still works after the translation.
        ("foo[12]", "foo1", True),
        ("foo[12]", "foo3", False),
    ])
    def test_bracket_negation(self, pattern, path, expected):
        regex = nomnom._glob_to_regex(pattern, anchored=True)
        assert bool(regex.match(path)) is expected


class TestGitignoreMatcher:
    def test_simple_glob_unanchored(self, tmp_path):
        (tmp_path / ".gitignore").write_text("*.log\n")
        gi = nomnom.load_gitignore(tmp_path)
        assert gi.is_ignored("foo.log", is_dir=False)
        assert gi.is_ignored("src/foo.log", is_dir=False)
        assert not gi.is_ignored("foo.txt", is_dir=False)

    def test_anchored_pattern(self, tmp_path):
        (tmp_path / ".gitignore").write_text("/build\n")
        gi = nomnom.load_gitignore(tmp_path)
        assert gi.is_ignored("build", is_dir=True)
        assert not gi.is_ignored("src/build", is_dir=True)

    def test_dir_only_matches_dir_not_file(self, tmp_path):
        (tmp_path / ".gitignore").write_text("foo/\n")
        gi = nomnom.load_gitignore(tmp_path)
        assert gi.is_ignored("foo", is_dir=True)
        assert not gi.is_ignored("foo", is_dir=False)

    def test_negation(self, tmp_path):
        (tmp_path / ".gitignore").write_text("*.log\n!keep.log\n")
        gi = nomnom.load_gitignore(tmp_path)
        assert gi.is_ignored("debug.log", is_dir=False)
        assert not gi.is_ignored("keep.log", is_dir=False)

    def test_blank_lines_and_comments(self, tmp_path):
        (tmp_path / ".gitignore").write_text("\n# comment\n\n*.tmp\n")
        gi = nomnom.load_gitignore(tmp_path)
        assert gi.is_ignored("x.tmp", is_dir=False)
        assert not gi.is_ignored("x.txt", is_dir=False)

    def test_nested_gitignore_is_scoped_to_subtree(self, tmp_path):
        (tmp_path / ".gitignore").write_text("a.txt\n")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / ".gitignore").write_text("b.txt\n")
        gi = nomnom.load_gitignore(tmp_path)
        assert gi.is_ignored("a.txt", is_dir=False)
        assert gi.is_ignored("sub/a.txt", is_dir=False)
        assert gi.is_ignored("sub/b.txt", is_dir=False)
        assert not gi.is_ignored("b.txt", is_dir=False)

    def test_no_gitignore_means_nothing_ignored(self, tmp_path):
        gi = nomnom.load_gitignore(tmp_path)
        assert not gi.is_ignored("anything.txt", is_dir=False)
        assert not gi.is_ignored("any/dir", is_dir=True)


# ---------- is_binary ----------

class TestIsBinary:
    def test_text_file_is_not_binary(self, tmp_path):
        p = tmp_path / "t.txt"
        p.write_text("hello world\n")
        assert nomnom.is_binary(p) is False

    def test_null_byte_makes_binary(self, tmp_path):
        p = tmp_path / "b.bin"
        p.write_bytes(b"hello\x00world")
        assert nomnom.is_binary(p) is True

    def test_unreadable_treated_as_binary(self, tmp_path):
        # A path that doesn't exist returns True (treated as skip).
        assert nomnom.is_binary(tmp_path / "missing") is True


# ---------- scan_repo ----------

class TestScanRepo:
    def test_basic_layout_dirs_first_dfs(self, tmp_path):
        make_repo(tmp_path, {
            "src": {
                "api": {"handlers.py": "x"},
                "utils.py": "y",
            },
            "README.md": "readme",
        })
        gi = nomnom.load_gitignore(tmp_path)
        items = nomnom.scan_repo(tmp_path, gi)
        assert rels(items) == [
            "src",
            "src/api",
            "src/api/handlers.py",
            "src/utils.py",
            "README.md",
        ]

    def test_junk_dirs_pruned(self, tmp_path):
        make_repo(tmp_path, {
            "src": {"main.py": "x"},
            "node_modules": {"pkg.txt": "junk"},
            "__pycache__": {"a.pyc": "junk"},
            ".git": {"HEAD": "ref"},
            ".venv": {"bin": {"activate": "junk"}},
        })
        gi = nomnom.load_gitignore(tmp_path)
        items = nomnom.scan_repo(tmp_path, gi)
        paths = rels(items)
        assert "node_modules" not in paths
        assert "__pycache__" not in paths
        assert ".git" not in paths
        assert ".venv" not in paths
        assert "src/main.py" in paths

    def test_gitignore_respected(self, tmp_path):
        make_repo(tmp_path, {
            ".gitignore": "secrets/\n*.log\n",
            "secrets": {"key.txt": "shh"},
            "app.log": "noise",
            "app.py": "code",
        })
        gi = nomnom.load_gitignore(tmp_path)
        paths = rels(nomnom.scan_repo(tmp_path, gi))
        assert "secrets" not in paths
        assert "secrets/key.txt" not in paths
        assert "app.log" not in paths
        assert "app.py" in paths

    def test_include_ignored_bypasses_gitignore(self, tmp_path):
        make_repo(tmp_path, {
            ".gitignore": "secrets/\n*.log\n",
            "secrets": {"key.txt": "shh"},
            "app.log": "noise",
            "app.py": "code",
        })
        paths = rels(nomnom.scan_repo(tmp_path, nomnom.GitignoreMatcher([])))
        assert "secrets" in paths
        assert "secrets/key.txt" in paths
        assert "app.log" in paths
        assert "app.py" in paths

    def test_binary_files_skipped(self, tmp_path):
        make_repo(tmp_path, {
            "code.py": "print('hi')",
            "blob.bin": b"\x00\x01\x02\x03",
        })
        gi = nomnom.load_gitignore(tmp_path)
        paths = rels(nomnom.scan_repo(tmp_path, gi))
        assert "code.py" in paths
        assert "blob.bin" not in paths

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks unreliable on Windows")
    def test_symlinks_skipped(self, tmp_path):
        make_repo(tmp_path, {"real.py": "x"})
        os.symlink(tmp_path / "real.py", tmp_path / "link.py")
        gi = nomnom.load_gitignore(tmp_path)
        paths = rels(nomnom.scan_repo(tmp_path, gi))
        assert "real.py" in paths
        assert "link.py" not in paths

    def test_empty_repo(self, tmp_path):
        gi = nomnom.load_gitignore(tmp_path)
        assert nomnom.scan_repo(tmp_path, gi) == []


# ---------- build_tree / visible_indices ----------

class TestBuildTree:
    def _items(self, *pairs):
        return [nomnom.ScanItem(rel=r, is_dir=is_dir) for r, is_dir in pairs]

    def test_parent_child_links(self):
        nodes = nomnom.build_tree(self._items(
            ("src", True),
            ("src/api", True),
            ("src/api/handlers.py", False),
            ("README.md", False),
        ))
        by_rel = {n.rel: i for i, n in enumerate(nodes)}
        assert nodes[by_rel["src"]].parent is None
        assert nodes[by_rel["src/api"]].parent == by_rel["src"]
        assert nodes[by_rel["src/api/handlers.py"]].parent == by_rel["src/api"]
        assert nodes[by_rel["README.md"]].parent is None
        assert by_rel["src/api"] in nodes[by_rel["src"]].children
        assert by_rel["src/api/handlers.py"] in nodes[by_rel["src/api"]].children

    def test_depth(self):
        nodes = nomnom.build_tree(self._items(
            ("a", True), ("a/b", True), ("a/b/c.py", False),
        ))
        assert [n.depth for n in nodes] == [0, 1, 2]

    def test_default_collapsed(self):
        nodes = nomnom.build_tree(self._items(
            ("src", True), ("src/main.py", False),
        ))
        assert all(not n.expanded for n in nodes)


class TestVisibleIndices:
    def test_root_always_visible(self):
        nodes = nomnom.build_tree([
            nomnom.ScanItem(rel="a", is_dir=True),
            nomnom.ScanItem(rel="b.py", is_dir=False),
        ])
        assert nomnom.visible_indices(nodes) == [0, 1]

    def test_collapsed_dir_hides_children(self):
        nodes = nomnom.build_tree([
            nomnom.ScanItem(rel="src", is_dir=True),
            nomnom.ScanItem(rel="src/a.py", is_dir=False),
            nomnom.ScanItem(rel="src/b.py", is_dir=False),
        ])
        # src collapsed (default) -> only src visible
        assert nomnom.visible_indices(nodes) == [0]
        nodes[0].expanded = True
        assert nomnom.visible_indices(nodes) == [0, 1, 2]


# ---------- cascade_check ----------

class TestCascadeCheck:
    def _three_level(self):
        items = [
            nomnom.ScanItem(rel="src", is_dir=True),
            nomnom.ScanItem(rel="src/api", is_dir=True),
            nomnom.ScanItem(rel="src/api/h.py", is_dir=False),
            nomnom.ScanItem(rel="src/api/m.py", is_dir=False),
            nomnom.ScanItem(rel="src/u.py", is_dir=False),
            nomnom.ScanItem(rel="README.md", is_dir=False),
        ]
        return nomnom.build_tree(items)

    def test_cascade_check_dir_sets_all_descendants(self):
        nodes = self._three_level()
        nomnom.cascade_check(nodes, 0, True)  # src
        for n in nodes[:5]:
            assert n.checked is True
        assert nodes[5].checked is False  # README is sibling

    def test_cascade_uncheck_resets_all_descendants(self):
        nodes = self._three_level()
        for n in nodes:
            n.checked = True
        nomnom.cascade_check(nodes, 1, False)  # src/api
        assert nodes[1].checked is False
        assert nodes[2].checked is False
        assert nodes[3].checked is False
        assert nodes[0].checked is True  # parent untouched
        assert nodes[4].checked is True  # sibling untouched

    def test_cascade_on_leaf_only_affects_leaf(self):
        nodes = self._three_level()
        nomnom.cascade_check(nodes, 5, True)
        assert nodes[5].checked is True
        assert all(n.checked is False for n in nodes[:5])

    def test_cascade_check_iterative_no_recursion_error(self):
        # Realistic repos aren't this deep, but the iterative implementation
        # should not hit Python's recursion limit on a 2000-deep chain.
        items = [nomnom.ScanItem(rel=f"d{i}", is_dir=True) for i in range(1)]
        # Build a 2000-deep linear path: d0/d0/.../d0
        path_parts: list[str] = []
        items = []
        for i in range(2000):
            path_parts.append(f"d{i}")
            items.append(nomnom.ScanItem(rel="/".join(path_parts), is_dir=True))
        nodes = nomnom.build_tree(items)
        nomnom.cascade_check(nodes, 0, True)
        assert all(n.checked for n in nodes)


# ---------- render_ascii_tree ----------

class TestRenderAsciiTree:
    def test_single_file(self):
        out = nomnom.render_ascii_tree(["a.py"], "repo")
        assert out == "repo/\n└── a.py"

    def test_dirs_before_files(self):
        out = nomnom.render_ascii_tree(
            ["README.md", "src/main.py"], "repo"
        )
        lines = out.splitlines()
        assert lines[0] == "repo/"
        # src/ should appear before README.md (dirs first)
        assert lines.index("├── src/") < lines.index("└── README.md")

    def test_nested_indentation(self):
        out = nomnom.render_ascii_tree(
            ["src/api/handlers.py", "src/api/models.py"], "repo"
        )
        assert "repo/" in out
        assert "src/" in out
        assert "api/" in out
        assert "handlers.py" in out
        assert "models.py" in out


# ---------- render_output ----------

class TestRenderOutput:
    def test_includes_header_and_files(self, tmp_path):
        (tmp_path / "a.py").write_text("print('a')\n")
        out = nomnom.render_output("myrepo", tmp_path, ["a.py"], None)
        assert "packed representation of selected files from myrepo" in out
        assert '<file path="a.py">' in out
        assert "print('a')" in out
        assert "</file>" in out

    def test_includes_tree_when_provided(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        out = nomnom.render_output(
            "myrepo", tmp_path, ["a.py"], "myrepo/\n└── a.py"
        )
        assert "<file_tree>" in out
        assert "</file_tree>" in out
        assert "└── a.py" in out

    def test_omits_tree_when_none(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        out = nomnom.render_output("myrepo", tmp_path, ["a.py"], None)
        assert "<file_tree>" not in out

    def test_handles_non_utf8(self, tmp_path):
        (tmp_path / "weird.txt").write_bytes(b"valid\xff\xfeinvalid utf8")
        out = nomnom.render_output("r", tmp_path, ["weird.txt"], None)
        assert '<file path="weird.txt">' in out
        # Should not raise; content goes through errors="replace"
        assert "valid" in out

    def test_read_error_inlined(self, tmp_path):
        out = nomnom.render_output("r", tmp_path, ["does-not-exist.py"], None)
        assert "<<read error" in out


# ---------- _unique_path ----------

class TestUniquePath:
    def test_returns_base_when_free(self, tmp_path):
        base = tmp_path / "foo.txt"
        assert nomnom._unique_path(base) == base

    def test_appends_minus_n_on_collision(self, tmp_path):
        base = tmp_path / "foo.txt"
        base.touch()
        assert nomnom._unique_path(base) == tmp_path / "foo-1.txt"
        (tmp_path / "foo-1.txt").touch()
        assert nomnom._unique_path(base) == tmp_path / "foo-2.txt"

    def test_respects_start_param(self, tmp_path):
        # File-output callers start at 2 so the first collision suffix is `-2`,
        # not `-1`. The directory callers start at 1.
        base = tmp_path / "out.txt"
        base.touch()
        assert nomnom._unique_path(base, start=2) == tmp_path / "out-2.txt"

    def test_no_suffix_path(self, tmp_path):
        # Directory-style: no extension, suffix is "".
        base = tmp_path / "repo"
        base.mkdir()
        assert nomnom._unique_path(base, start=1) == tmp_path / "repo-1"

    def test_keeps_compound_suffix_as_simple_suffix(self, tmp_path):
        # Path.with_name uses .suffix (single trailing .ext) — confirm this
        # is acceptable for our callers (none rely on `.tar.gz`-style names).
        base = tmp_path / "x.tar.gz"
        base.touch()
        # The suffix is `.gz` and the stem is `x.tar`; collision yields x.tar-1.gz.
        assert nomnom._unique_path(base) == tmp_path / "x.tar-1.gz"


# ---------- pick_output_path ----------

class TestPickOutputPath:
    def test_unique_when_no_collision(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p = nomnom.pick_output_path("repo")
        assert p.parent == tmp_path
        assert p.name.startswith("repo-") and p.name.endswith(".txt")

    def test_appends_suffix_on_collision(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Freeze the timestamp so both calls collide and the suffix path runs.
        fixed = "20260503-120000"

        class _FrozenDatetime:
            @staticmethod
            def now():
                class _T:
                    def strftime(self, _fmt):
                        return fixed
                return _T()

        monkeypatch.setattr(nomnom, "datetime", _FrozenDatetime)
        p1 = nomnom.pick_output_path("repo")
        assert p1.name == f"repo-{fixed}.txt"
        p1.write_text("first")
        p2 = nomnom.pick_output_path("repo")
        assert p2.name == f"repo-{fixed}-2.txt"
        p2.write_text("second")
        p3 = nomnom.pick_output_path("repo")
        assert p3.name == f"repo-{fixed}-3.txt"


# ---------- picker state helpers ----------

class TestCycleDestination:
    def test_file_to_clipboard(self):
        assert nomnom.cycle_destination(nomnom.Destination.FILE) == nomnom.Destination.CLIPBOARD

    def test_clipboard_to_stdout(self):
        assert nomnom.cycle_destination(nomnom.Destination.CLIPBOARD) == nomnom.Destination.STDOUT

    def test_stdout_wraps_to_file(self):
        assert nomnom.cycle_destination(nomnom.Destination.STDOUT) == nomnom.Destination.FILE

    def test_allowed_subset_cycles_without_stdout(self):
        allowed = (nomnom.Destination.FILE, nomnom.Destination.CLIPBOARD)
        # FILE → CLIPBOARD → FILE; STDOUT is never hit.
        d = nomnom.Destination.FILE
        for expected in (nomnom.Destination.CLIPBOARD, nomnom.Destination.FILE):
            d = nomnom.cycle_destination(d, allowed)
            assert d == expected

    def test_allowed_snaps_to_first_when_current_not_allowed(self):
        allowed = (nomnom.Destination.FILE, nomnom.Destination.CLIPBOARD)
        # STDOUT is not in allowed → snap to the first allowed entry.
        assert nomnom.cycle_destination(nomnom.Destination.STDOUT, allowed) \
            == nomnom.Destination.FILE


class TestCycleVerb:
    def test_full_cycle(self):
        v = nomnom.Verb.BUNDLE
        for expected in (nomnom.Verb.COMMIT, nomnom.Verb.PR,
                         nomnom.Verb.ITEM, nomnom.Verb.BUNDLE):
            v = nomnom.cycle_verb(v)
            assert v == expected

    def test_allowed_bundle_only_is_a_no_op(self):
        allowed = (nomnom.Verb.BUNDLE,)
        assert nomnom.cycle_verb(nomnom.Verb.BUNDLE, allowed) == nomnom.Verb.BUNDLE

    def test_allowed_snaps_to_first_when_current_not_allowed(self):
        allowed = (nomnom.Verb.BUNDLE,)
        # If we somehow start on COMMIT but only BUNDLE is allowed, snap back.
        assert nomnom.cycle_verb(nomnom.Verb.COMMIT, allowed) == nomnom.Verb.BUNDLE


class TestComputeSummary:
    def _node(self, rel, *, is_dir=False, checked=False, size=0):
        return nomnom.Node(
            rel=rel, name=rel.rsplit("/", 1)[-1], is_dir=is_dir,
            depth=rel.count("/"), parent=None,
            checked=checked, size=size,
        )

    def test_empty_selection(self):
        nodes = [self._node("a.py", size=100), self._node("b.py", size=200)]
        assert nomnom.compute_summary(nodes) == (0, 0, 0)

    def test_mixed_excludes_dirs(self):
        nodes = [
            self._node("src", is_dir=True, checked=True, size=999),
            self._node("src/a.py", checked=True, size=400),
            self._node("src/b.py", checked=False, size=800),
            self._node("c.py", checked=True, size=400),
        ]
        # Two checked files, 800 bytes total, 200 approx tokens.
        assert nomnom.compute_summary(nodes) == (2, 800, 200)

    def test_all_checked(self):
        nodes = [
            self._node("a.py", checked=True, size=400),
            self._node("b.py", checked=True, size=600),
        ]
        assert nomnom.compute_summary(nodes) == (2, 1000, 250)


class TestFormatFooter:
    def test_each_destination_label(self):
        for dest, label in [
            (nomnom.Destination.FILE, "dest: file"),
            (nomnom.Destination.CLIPBOARD, "dest: clipboard"),
            (nomnom.Destination.STDOUT, "dest: stdout"),
        ]:
            out = nomnom.format_footer(dest, True, (3, 1000, 250), 120)
            assert label in out

    def test_tree_toggle_flips(self):
        on = nomnom.format_footer(nomnom.Destination.FILE, True, (1, 100, 25), 120)
        off = nomnom.format_footer(nomnom.Destination.FILE, False, (1, 100, 25), 120)
        assert "tree: on" in on
        assert "tree: off" in off

    def test_narrow_width_drops_stats_block(self):
        # Wide enough for "selected: 2 files" + gap + the right block
        # (verb: bundle  dest: file  tree: on) but not the size/token block
        # in between.
        narrow = nomnom.format_footer(nomnom.Destination.FILE, True, (2, 1000, 250), 60)
        assert "selected: 2 files" in narrow
        assert "dest: file" in narrow
        assert "verb: bundle" in narrow
        assert len(narrow) <= 60

    def test_wide_width_includes_everything(self):
        out = nomnom.format_footer(nomnom.Destination.FILE, True, (5, 4096, 1024), 120)
        assert "selected: 5 files" in out
        assert "dest: file" in out
        assert "tree: on" in out
        assert "verb: bundle" in out
        assert len(out) <= 120

    def test_verb_label_appears(self):
        for verb, label in [
            (nomnom.Verb.BUNDLE, "verb: bundle"),
            (nomnom.Verb.COMMIT, "verb: commit"),
            (nomnom.Verb.PR, "verb: pr"),
            (nomnom.Verb.ITEM, "verb: item"),
        ]:
            out = nomnom.format_footer(
                nomnom.Destination.FILE, True, (3, 1000, 250), 120, verb,
            )
            assert label in out

    def test_default_verb_is_bundle(self):
        # Existing callers that don't pass a verb should still get a footer
        # with "verb: bundle" implied by the default.
        out = nomnom.format_footer(nomnom.Destination.FILE, True, (1, 100, 25), 120)
        assert "verb: bundle" in out


# ---------- --include / --exclude filters ----------

class TestApplyIncludeExclude:
    def _items(self, *rels_and_dirs):
        out = []
        for rel, is_dir in rels_and_dirs:
            out.append(nomnom.ScanItem(rel=rel, is_dir=is_dir))
        return out

    def test_empty_patterns_passthrough(self):
        items = self._items(("a.py", False), ("src", True), ("src/b.py", False))
        assert nomnom.apply_include_exclude(items, [], []) == items

    def test_include_filters_files_and_keeps_parent_dirs(self):
        items = self._items(
            ("src", True), ("src/a.py", False), ("src/a.js", False),
            ("tests", True), ("tests/test_a.py", False),
        )
        out = nomnom.apply_include_exclude(items, ["*.py"], [])
        rels = [(it.rel, it.is_dir) for it in out]
        assert ("src/a.py", False) in rels
        assert ("tests/test_a.py", False) in rels
        assert ("src/a.js", False) not in rels
        # Parent dirs survive because their files survive.
        assert ("src", True) in rels
        assert ("tests", True) in rels

    def test_exclude_drops_matching(self):
        items = self._items(
            ("src/a.py", False), ("src/test_a.py", False),
        )
        out = nomnom.apply_include_exclude(items, [], ["test_*"])
        rels = [it.rel for it in out]
        assert "src/a.py" in rels
        assert "src/test_a.py" not in rels

    def test_include_then_exclude(self):
        items = self._items(
            ("src/a.py", False), ("src/test_a.py", False),
            ("src/b.js", False),
        )
        out = nomnom.apply_include_exclude(items, ["*.py"], ["test_*"])
        rels = [it.rel for it in out]
        assert rels == ["src/a.py"] or "src/a.py" in rels
        assert "src/test_a.py" not in rels
        assert "src/b.js" not in rels

    def test_deep_double_star(self):
        items = self._items(
            ("a", True), ("a/b", True), ("a/b/c.py", False),
            ("a/d.js", False),
        )
        out = nomnom.apply_include_exclude(items, ["**/*.py"], [])
        rels = [it.rel for it in out if not it.is_dir]
        assert rels == ["a/b/c.py"]

    def test_anchored_pattern(self):
        items = self._items(
            ("src/a.py", False), ("nested/src/a.py", False),
        )
        # Leading slash anchors at repo root.
        out = nomnom.apply_include_exclude(items, ["/src/"], [])
        rels = [it.rel for it in out if not it.is_dir]
        assert "src/a.py" in rels
        assert "nested/src/a.py" not in rels


# ---------- launcher screen ----------

class TestLauncherScreen:
    def test_tiles_include_every_verb(self):
        s = nomnom.LauncherScreen()
        labels = [t[0] for t in s.tiles]
        for v in ("Bundle", "Commit", "PR", "Item", "Rebuild",
                  "Send", "Receive", "Extensions", "Feeds"):
            assert v in labels

    def test_pair_tile_removed_in_v2(self):
        s = nomnom.LauncherScreen()
        labels = [t[0] for t in s.tiles]
        assert "Pair" not in labels
        assert "Pins" not in labels

    def test_cursor_wraps_down(self):
        s = nomnom.LauncherScreen()
        # Move past the end; should wrap to top.
        for _ in range(len(s.tiles)):
            s.handle_key(ord("j"))
        assert s.cursor == 0

    def test_cursor_wraps_up(self):
        s = nomnom.LauncherScreen()
        s.handle_key(ord("k"))
        assert s.cursor == len(s.tiles) - 1

    def test_q_quits(self):
        s = nomnom.LauncherScreen()
        assert s.handle_key(ord("q")) == nomnom.ScreenAction.QUIT
        assert s.handle_key(27) == nomnom.ScreenAction.QUIT  # Esc

    def test_enter_pushes_a_screen(self):
        s = nomnom.LauncherScreen()
        result = s.handle_key(10)
        assert isinstance(result, nomnom.Screen)


class TestPlaceholderScreen:
    def test_q_returns_back(self):
        s = nomnom.PlaceholderScreen("Test", "body")
        assert s.handle_key(ord("q")) == nomnom.ScreenAction.BACK
        assert s.handle_key(27) == nomnom.ScreenAction.BACK

    def test_other_keys_continue(self):
        s = nomnom.PlaceholderScreen("Test", "body")
        assert s.handle_key(ord("x")) == nomnom.ScreenAction.CONTINUE


class TestRebuildScreen:
    def _make_bundle(self, tmp_path, repo="r", files=(("a.py", "x\n"),)) -> Path:
        body = (
            f"This is a packed representation of selected files from {repo}, "
            "bundled on 2026-05-23T10:00:00.\n\n"
        )
        for rel, content in files:
            body += f'<file path="{rel}">\n{content}</file>\n\n'
        p = tmp_path / "bundle.txt"
        p.write_text(body, encoding="utf-8")
        return p

    def test_parse_populates_files(self, tmp_path, monkeypatch):
        bundle = self._make_bundle(tmp_path)
        monkeypatch.chdir(tmp_path)
        s = nomnom.RebuildScreen()
        s.path_buf = str(bundle)
        s._parse()
        assert s.step == "preview"
        assert s.repo_name == "r"
        assert [rel for rel, _ in s.files] == ["a.py"]
        assert s.target is not None

    def test_parse_invalid_path_records_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        s = nomnom.RebuildScreen()
        s.path_buf = str(tmp_path / "missing.txt")
        s._parse()
        assert s.step == "input"
        assert "not a file" in s.error

    def test_write_creates_files(self, tmp_path, monkeypatch):
        bundle = self._make_bundle(tmp_path, files=(("a.py", "hi\n"),))
        cwd = tmp_path / "out"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        s = nomnom.RebuildScreen()
        s.path_buf = str(bundle)
        s._parse()
        s._write()
        assert s.step == "done"
        assert (cwd / "r" / "a.py").read_text().rstrip("\n") == "hi"

    def test_q_in_input_returns_back(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        s = nomnom.RebuildScreen()
        assert s.handle_key(ord("q")) == nomnom.ScreenAction.BACK


class TestExtensionsScreen:
    def test_loads_all_four_sections(self):
        s = nomnom.ExtensionsScreen()
        labels = [section[0] for section in s.sections]
        assert labels == [
            "Text extensions", "Binary extensions",
            "Known text names", "Secret patterns",
        ]

    def test_section_tuple_carries_kind(self):
        s = nomnom.ExtensionsScreen()
        kinds = [section[1] for section in s.sections]
        assert kinds == ["text", "binary", "name", "secret"]

    def test_known_text_extension_present(self):
        s = nomnom.ExtensionsScreen()
        text_section = next(
            entries for label, _kind, entries in s.sections
            if label == "Text extensions"
        )
        assert ".py" in text_section

    def test_q_returns_back(self):
        s = nomnom.ExtensionsScreen()
        assert s.handle_key(ord("q")) == nomnom.ScreenAction.BACK

    def test_h_l_switch_sections(self):
        s = nomnom.ExtensionsScreen()
        assert s.section_cursor == 0
        s.handle_key(ord("l"))
        assert s.section_cursor == 1
        s.handle_key(ord("h"))
        assert s.section_cursor == 0

    def test_j_k_move_within_section(self):
        s = nomnom.ExtensionsScreen()
        _, _, entries = s._current()
        assert len(entries) > 1
        s.handle_key(ord("j"))
        assert s.entry_cursor == 1
        s.handle_key(ord("k"))
        assert s.entry_cursor == 0

    def test_a_enters_add_step(self):
        s = nomnom.ExtensionsScreen()
        s.handle_key(ord("a"))
        assert s.step == "add"
        s.handle_key(ord("."))
        s.handle_key(ord("x"))
        s.handle_key(ord("y"))
        s.handle_key(ord("z"))
        assert s.add_buf == ".xyz"

    def test_add_dispatches_to_register_values(self, monkeypatch):
        called: dict = {}

        def fake_register(kind, values, *, remove=False, path=None):
            called["kind"] = kind
            called["values"] = list(values)
            called["remove"] = remove
            return nomnom.RegisterResult(
                target_name="TEXT_EXTENSIONS", added=list(values), wrote=True,
            )
        monkeypatch.setattr(nomnom, "register_values", fake_register)
        # Make _load_sections cheap so it doesn't reflect real globals.
        monkeypatch.setattr(
            nomnom.ExtensionsScreen, "_load_sections",
            classmethod(lambda cls: [
                ("Text extensions", "text", [".py"]),
                ("Binary extensions", "binary", []),
                ("Known text names", "name", []),
                ("Secret patterns", "secret", []),
            ]),
        )
        s = nomnom.ExtensionsScreen()
        s.step = "add"
        s.add_buf = ".pyx"
        s.handle_key(10)  # Enter
        assert called == {"kind": "text", "values": [".pyx"], "remove": False}
        assert s.step == "view"

    def test_add_empty_value_keeps_step(self, monkeypatch):
        s = nomnom.ExtensionsScreen()
        s.step = "add"
        s.add_buf = ""
        s.handle_key(10)
        assert s.step == "add"
        assert "empty" in s.error

    def test_add_esc_cancels(self):
        s = nomnom.ExtensionsScreen()
        s.step = "add"
        s.add_buf = ".pyx"
        s.handle_key(27)
        assert s.step == "view"
        assert s.add_buf == ""

    def test_delete_without_stdscr_skips_confirm(self, monkeypatch):
        # When stdscr is None (tests), confirm modal is skipped and the
        # delete proceeds — verifies the dispatch path. (Real usage always
        # has stdscr.)
        removed: dict = {}

        def fake_register(kind, values, *, remove=False, path=None):
            removed["kind"] = kind
            removed["values"] = list(values)
            removed["remove"] = remove
            return nomnom.RegisterResult(
                target_name="TEXT_EXTENSIONS", removed=list(values), wrote=True,
            )
        monkeypatch.setattr(nomnom, "register_values", fake_register)
        monkeypatch.setattr(
            nomnom.ExtensionsScreen, "_load_sections",
            classmethod(lambda cls: [
                ("Text extensions", "text", [".py"]),
                ("Binary extensions", "binary", []),
                ("Known text names", "name", []),
                ("Secret patterns", "secret", []),
            ]),
        )
        s = nomnom.ExtensionsScreen()
        s.handle_key(ord("d"), stdscr=None)
        assert removed == {"kind": "text", "values": [".py"], "remove": True}


class TestBundleScreen:
    def test_init_defaults_to_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        s = nomnom.BundleScreen()
        assert s.step == "path"
        assert s.path_buf == str(tmp_path)
        assert s.error == ""

    def test_path_edit_appends_chars(self):
        s = nomnom.BundleScreen()
        s.path_buf = ""
        s.handle_key(ord("/"))
        s.handle_key(ord("a"))
        assert s.path_buf == "/a"

    def test_path_edit_backspace(self):
        s = nomnom.BundleScreen()
        s.path_buf = "/abc"
        s.handle_key(127)  # backspace
        assert s.path_buf == "/ab"

    def test_q_in_path_returns_back(self):
        s = nomnom.BundleScreen()
        assert s.handle_key(ord("q")) == nomnom.ScreenAction.BACK
        assert s.handle_key(27) == nomnom.ScreenAction.BACK

    def test_enter_without_stdscr_is_a_noop(self):
        s = nomnom.BundleScreen()
        assert s.handle_key(10) == nomnom.ScreenAction.CONTINUE
        # _scan_and_pick should not have been entered.
        assert s.step == "path"

    def test_scan_invalid_path_sets_error(self, tmp_path):
        s = nomnom.BundleScreen()
        s.path_buf = str(tmp_path / "does-not-exist")
        stay = s._scan_and_pick(stdscr=None)  # _picker_ui won't be reached
        assert stay is True
        assert s.step == "path"
        assert "not a directory" in s.error

    def test_scan_empty_repo_sets_error(self, tmp_path):
        # Empty dir → scan returns no file items → records error before
        # touching _picker_ui, so a None stdscr is fine.
        s = nomnom.BundleScreen()
        s.path_buf = str(tmp_path)
        stay = s._scan_and_pick(stdscr=None)
        assert stay is True
        assert s.step == "path"
        assert "no files found" in s.error

    def test_scan_happy_path(self, tmp_path, monkeypatch):
        make_repo(tmp_path, {"a.py": "print('a')\n"})
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)

        # Stub the picker to return a deterministic selection.
        def fake_picker(stdscr, nodes, **kw):
            return nomnom.PickResult({"a.py"}, nomnom.Destination.FILE, True)
        monkeypatch.setattr(nomnom, "_picker_ui", fake_picker)

        s = nomnom.BundleScreen()
        s.path_buf = str(tmp_path)
        stay = s._scan_and_pick(stdscr=object())  # opaque, not used by fake
        assert stay is True
        assert s.step == "done"
        # File written into out_dir (cwd) by _emit_bundle.
        bundles = list(out_dir.glob(f"{tmp_path.name}-*.txt"))
        assert len(bundles) == 1
        assert any("wrote" in m for m in s.messages)

    def test_scan_cancelled_picker_goes_back(self, tmp_path, monkeypatch):
        make_repo(tmp_path, {"a.py": "x\n"})
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(nomnom, "_picker_ui",
                            lambda *a, **kw: None)  # cancel
        s = nomnom.BundleScreen()
        s.path_buf = str(tmp_path)
        stay = s._scan_and_pick(stdscr=object())
        assert stay is False
        assert s.step == "path"  # unchanged; caller sends to launcher

    def test_done_state_esc_returns_back(self):
        s = nomnom.BundleScreen()
        s.step = "done"
        s.messages = ["wrote foo.txt"]
        assert s.handle_key(27) == nomnom.ScreenAction.BACK
        assert s.handle_key(ord("q")) == nomnom.ScreenAction.BACK


class TestEmitBundle:
    def test_file_destination_writes(self, tmp_path, monkeypatch):
        make_repo(tmp_path, {"a.py": "print('a')\n"})
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc, lines = nomnom._emit_bundle(
            tmp_path.name, tmp_path, ["a.py"], True, nomnom.Destination.FILE,
        )
        assert rc == 0
        bundles = list(out_dir.glob(f"{tmp_path.name}-*.txt"))
        assert len(bundles) == 1
        assert "wrote " in lines[-1]

    def test_clipboard_falls_back_to_file_when_no_tool(self, tmp_path, monkeypatch):
        make_repo(tmp_path, {"a.py": "x\n"})
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        monkeypatch.setattr(nomnom, "copy_to_clipboard", lambda _t: False)
        rc, lines = nomnom._emit_bundle(
            tmp_path.name, tmp_path, ["a.py"], True,
            nomnom.Destination.CLIPBOARD,
        )
        assert rc == 0
        assert any("no clipboard tool" in line for line in lines)
        assert any("wrote " in line for line in lines)

    def test_stdout_writes_to_stdout(self, tmp_path, capsys):
        make_repo(tmp_path, {"a.py": "x\n"})
        rc, lines = nomnom._emit_bundle(
            tmp_path.name, tmp_path, ["a.py"], False,
            nomnom.Destination.STDOUT,
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "<file path=\"a.py\">" in captured.out
        assert any("stdout" in line for line in lines)


class TestCommitScreen:
    def test_init_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        s = nomnom.CommitScreen()
        assert s.step == "inputs"
        assert s.field_cursor == 0
        assert s.repo_buf == str(tmp_path)
        assert s.destination == nomnom.Destination.FILE

    def test_tab_cycles_fields(self):
        s = nomnom.CommitScreen()
        n = len(s.fields)
        for _ in range(n):
            s.handle_key(9)  # Tab
        assert s.field_cursor == 0

    def test_d_cycles_destination(self):
        s = nomnom.CommitScreen()
        s.handle_key(ord("d"))
        assert s.destination == nomnom.Destination.CLIPBOARD
        s.handle_key(ord("d"))
        assert s.destination == nomnom.Destination.FILE  # wraps

    def test_path_edit_appends_chars(self):
        s = nomnom.CommitScreen()
        s.repo_buf = ""
        s.handle_key(ord("/"))
        s.handle_key(ord("a"))
        assert s.repo_buf == "/a"

    def test_q_returns_back(self):
        s = nomnom.CommitScreen()
        assert s.handle_key(ord("q")) == nomnom.ScreenAction.BACK

    def test_execute_captures_stdout_stderr(self, monkeypatch):
        s = nomnom.CommitScreen()
        s.repo_buf = "/tmp/some-repo"

        def fake_cmd_commit(repo, *, destination):
            print("stdout line")
            print("stderr line", file=sys.stderr)
            return 0
        monkeypatch.setattr(nomnom, "cmd_commit", fake_cmd_commit)
        s.handle_key(10)  # Enter
        assert s.step == "done"
        assert s.rc == 0
        assert any("stdout line" in line for line in s.output_lines)
        assert any("stderr line" in line for line in s.output_lines)

    def test_execute_handles_nomnom_error(self, monkeypatch):
        s = nomnom.CommitScreen()
        s.repo_buf = "/tmp/some-repo"

        def boom(repo, *, destination):
            raise nomnom.NomnomError("not a git repository: /tmp/some-repo")
        monkeypatch.setattr(nomnom, "cmd_commit", boom)
        s.handle_key(10)
        assert s.rc == 1
        assert "not a git repository" in s.error
        assert s.step == "done"


class TestPRScreen:
    def test_base_field_present(self):
        s = nomnom.PRScreen()
        field_ids = [fid for fid, _ in s.fields]
        assert field_ids == ["repo", "base", "dest"]

    def test_base_field_edits_when_focused(self):
        s = nomnom.PRScreen()
        # Cursor to "base" (index 1).
        s.handle_key(9)
        assert s.field_cursor == 1
        s.handle_key(ord("d"))  # 'd' would normally cycle dest — but
        # since 'd' is intercepted first, dest cycles even when on base.
        # We verify by testing base editing with non-d chars.
        s.field_cursor = 1
        s.base_buf = ""
        s.handle_key(ord("m"))
        s.handle_key(ord("a"))
        s.handle_key(ord("i"))
        s.handle_key(ord("n"))
        assert s.base_buf == "main"

    def test_run_passes_base_to_cmd_pr(self, monkeypatch):
        called: dict = {}

        def fake_cmd_pr(repo, base, *, destination):
            called["repo"] = repo
            called["base"] = base
            called["destination"] = destination
            return 0
        monkeypatch.setattr(nomnom, "cmd_pr", fake_cmd_pr)
        s = nomnom.PRScreen()
        s.repo_buf = "/tmp/r"
        s.base_buf = "develop"
        s.destination = nomnom.Destination.CLIPBOARD
        s.handle_key(10)
        assert called == {"repo": "/tmp/r", "base": "develop",
                          "destination": nomnom.Destination.CLIPBOARD}

    def test_empty_base_passes_none(self, monkeypatch):
        called: dict = {}

        def fake_cmd_pr(repo, base, *, destination):
            called["base"] = base
            return 0
        monkeypatch.setattr(nomnom, "cmd_pr", fake_cmd_pr)
        s = nomnom.PRScreen()
        s.repo_buf = "/tmp/r"
        s.base_buf = ""
        s.handle_key(10)
        assert called["base"] is None


class TestItemScreen:
    def test_fields_include_id_and_diff(self):
        s = nomnom.ItemScreen()
        field_ids = [fid for fid, _ in s.fields]
        assert field_ids == ["repo", "id", "diff", "dest"]

    def test_id_accepts_printable_chars(self):
        s = nomnom.ItemScreen()
        s.field_cursor = 1  # focus id
        for ch in "v1.2.3":
            s.handle_key(ord(ch))
        assert s.id_buf == "v1.2.3"

    def test_diff_toggled_by_space_when_focused(self):
        s = nomnom.ItemScreen()
        s.field_cursor = 2  # focus diff
        assert s.include_diff is False
        s.handle_key(ord(" "))
        assert s.include_diff is True
        s.handle_key(ord(" "))
        assert s.include_diff is False

    def test_run_errors_on_missing_id(self, monkeypatch):
        monkeypatch.setattr(nomnom, "cmd_item",
                            lambda *a, **k: 0)
        s = nomnom.ItemScreen()
        s.repo_buf = "/tmp/r"
        s.id_buf = ""
        s.handle_key(10)
        assert s.rc == 1
        assert "required" in s.error.lower()

    def test_run_passes_args(self, monkeypatch):
        called: dict = {}

        def fake_cmd_item(
            repo, kind_or_id, ident=None, *,
            include_diff=False, all_logs=False, destination,
        ):
            called["repo"] = repo
            called["kind_or_id"] = kind_or_id
            called["ident"] = ident
            called["diff"] = include_diff
            called["dest"] = destination
            return 0
        monkeypatch.setattr(nomnom, "cmd_item", fake_cmd_item)
        s = nomnom.ItemScreen()
        s.repo_buf = "/tmp/r"
        s.id_buf = "42"
        s.include_diff = True
        s.destination = nomnom.Destination.CLIPBOARD
        s.handle_key(10)
        assert called == {
            "repo": "/tmp/r", "kind_or_id": "42", "ident": None,
            "diff": True, "dest": nomnom.Destination.CLIPBOARD,
        }


class _FakeStdscr:
    """Just enough stdscr surface for SendScreen / ReceiveScreen tests."""
    def clear(self) -> None:
        pass
    def erase(self) -> None:
        pass
    def refresh(self) -> None:
        pass


class TestNomnomError:
    def test_require_git_repo_raises_on_non_repo(self, tmp_path):
        # tmp_path is not a git repo.
        with pytest.raises(nomnom.NomnomError):
            nomnom._require_git_repo(tmp_path)

    def test_require_gh_raises_when_missing(self, monkeypatch):
        monkeypatch.setattr(nomnom.shutil, "which", lambda _x: None)
        with pytest.raises(nomnom.NomnomError):
            nomnom._require_gh()


# ---------- _resolve_git_repo ----------

class TestResolveGitRepo:
    def test_raises_on_not_a_directory(self, tmp_path):
        missing = tmp_path / "nope"
        with pytest.raises(nomnom.NomnomError, match="not a directory"):
            nomnom._resolve_git_repo(str(missing))

    def test_raises_on_non_git_directory(self, tmp_path):
        # A real dir but no .git — _require_git_repo should raise.
        with pytest.raises(nomnom.NomnomError, match="not a git repository"):
            nomnom._resolve_git_repo(str(tmp_path))

    def test_returns_path_and_name_on_happy_path(self, tmp_path):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        root, name = nomnom._resolve_git_repo(str(repo))
        assert root == repo.resolve()
        assert name == "myrepo"


# ---------- preview pane ----------

class TestRenderPreview:
    def test_text_head(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("line1\nline2\nline3\nline4\n")
        out = nomnom.render_preview(p, max_lines=3, max_cols=80)
        assert out == ["line1", "line2", "line3"]

    def test_binary_returns_stats_only(self, tmp_path):
        p = tmp_path / "blob.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        out = nomnom.render_preview(p, max_lines=10, max_cols=80)
        assert len(out) == 1
        assert "binary" in out[0]

    def test_oversize_returns_stats_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nomnom, "PREVIEW_MAX_BYTES", 10)
        p = tmp_path / "big.txt"
        p.write_text("a" * 100)
        out = nomnom.render_preview(p, max_lines=10, max_cols=80)
        assert len(out) == 1
        assert "too large" in out[0]

    def test_missing_file_returns_unreadable(self, tmp_path):
        out = nomnom.render_preview(tmp_path / "nope.txt", max_lines=10, max_cols=80)
        assert out == ["(unreadable)"]

    def test_truncation_respects_max_cols(self, tmp_path):
        p = tmp_path / "long.txt"
        p.write_text("a" * 200 + "\n")
        out = nomnom.render_preview(p, max_lines=1, max_cols=20)
        assert len(out) == 1
        assert len(out[0]) <= 20

    def test_empty_file_returns_empty_label(self, tmp_path):
        p = tmp_path / "empty.txt"
        p.write_text("")
        out = nomnom.render_preview(p, max_lines=5, max_cols=40)
        assert out == ["(empty)"]


# ---------- scripted bundle CLI (--all / --include / --stdout) ----------

NOMNOM_PATH = Path(__file__).resolve().parent.parent / "nomnom.py"


class TestScriptedBundle:
    def test_all_stdout_emits_bundle_to_stdout(self, tmp_path):
        make_repo(tmp_path, {"a.py": "print('a')\n", "b.py": "print('b')\n"})
        r = subprocess.run(
            [sys.executable, str(NOMNOM_PATH), "--all", "--stdout", str(tmp_path)],
            capture_output=True, text=True, check=True,
        )
        assert "<file path=\"a.py\">" in r.stdout
        assert "<file path=\"b.py\">" in r.stdout
        assert "wrote " in r.stderr

    def test_all_writes_file_to_cwd(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        make_repo(repo, {"a.py": "print('a')\n"})
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        r = subprocess.run(
            [sys.executable, str(NOMNOM_PATH), "--all", str(repo)],
            capture_output=True, text=True, cwd=out_dir, check=True,
        )
        bundles = list(out_dir.glob("repo-*.txt"))
        assert len(bundles) == 1
        body = bundles[0].read_text()
        assert "<file path=\"a.py\">" in body
        assert "wrote " in r.stderr

    def test_include_filters_via_all_stdout(self, tmp_path):
        make_repo(tmp_path, {
            "src": {"a.py": "py\n", "b.js": "js\n"},
            "test_a.py": "t\n",
        })
        r = subprocess.run(
            [sys.executable, str(NOMNOM_PATH), "--all", "--stdout",
             "--include", "*.py", "--exclude", "test_*", str(tmp_path)],
            capture_output=True, text=True, check=True,
        )
        assert "src/a.py" in r.stdout
        assert "src/b.js" not in r.stdout
        assert "test_a.py" not in r.stdout

    def test_no_match_exits_cleanly(self, tmp_path):
        make_repo(tmp_path, {"a.js": "js\n"})
        r = subprocess.run(
            [sys.executable, str(NOMNOM_PATH), "--all", "--stdout",
             "--include", "*.py", str(tmp_path)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert "no files matched" in r.stderr
        assert r.stdout == ""

    def test_non_tty_without_flags_errors(self, tmp_path):
        make_repo(tmp_path, {"a.py": "x\n"})
        r = subprocess.run(
            [sys.executable, str(NOMNOM_PATH), str(tmp_path)],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
        )
        assert r.returncode == 1
        assert "needs a TTY" in r.stderr


# ---------- is_binary extension shortcuts ----------

class TestIsBinaryExtensions:
    def test_known_text_extension_skips_byte_sniff(self, tmp_path):
        p = tmp_path / "trick.py"
        p.write_bytes(b"print('hi')\x00")
        assert nomnom.is_binary(p) is False

    def test_known_binary_extension_skipped_without_reading(self, tmp_path):
        assert nomnom.is_binary(tmp_path / "missing.png") is True

    def test_known_text_name_no_extension(self, tmp_path):
        p = tmp_path / "Makefile"
        p.write_text("all:\n\techo hi\n")
        assert nomnom.is_binary(p) is False

    def test_unknown_extension_falls_through_to_sniff(self, tmp_path):
        text = tmp_path / "data.xyz"
        text.write_text("some text content")
        assert nomnom.is_binary(text) is False

        binary = tmp_path / "blob.xyz"
        binary.write_bytes(b"\x00\x01\x02")
        assert nomnom.is_binary(binary) is True


# ---------- secret files ----------

class TestIsSecretFile:
    @pytest.mark.parametrize("name", [
        ".env",
        ".env.production",
        ".env.local",
        "cert.pem",
        "private.key",
        "id_rsa",
        "id_ed25519",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "secrets.yaml",
        "credentials",
        "credentials.json",
        "keystore.pfx",
    ])
    def test_secret_names_match(self, name):
        assert nomnom.is_secret_file(name) is True

    @pytest.mark.parametrize("name", [
        "env.example",
        "keys.txt",
        "id_rsa.pub",
        "README.md",
        "config.yaml",
    ])
    def test_safe_names_do_not_match(self, name):
        assert nomnom.is_secret_file(name) is False


class TestScanReposSkipSecrets:
    def _layout(self):
        return {
            ".env": "DB_PASS=hunter2",
            "cert.pem": "-----BEGIN-----\n",
            "id_rsa": "-----BEGIN OPENSSH-----\n",
            "id_rsa.pub": "ssh-rsa AAAA...",
            "app.py": "print('hi')",
        }

    def test_secrets_skipped_by_default(self, tmp_path):
        make_repo(tmp_path, self._layout())
        gi = nomnom.load_gitignore(tmp_path)
        paths = rels(nomnom.scan_repo(tmp_path, gi))
        assert ".env" not in paths
        assert "cert.pem" not in paths
        assert "id_rsa" not in paths
        assert "id_rsa.pub" in paths
        assert "app.py" in paths

    def test_include_secrets_returns_them(self, tmp_path):
        make_repo(tmp_path, self._layout())
        gi = nomnom.load_gitignore(tmp_path)
        paths = rels(nomnom.scan_repo(tmp_path, gi, skip_secrets=False))
        assert ".env" in paths
        assert "cert.pem" in paths
        assert "id_rsa" in paths
        assert "id_rsa.pub" in paths
        assert "app.py" in paths


# ---------- clipboard ----------

class TestClipboardDetect:
    def test_pbcopy_wins_when_present(self, monkeypatch):
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(
            nomnom.shutil, "which",
            lambda c: "/usr/bin/pbcopy" if c == "pbcopy" else None,
        )
        assert nomnom.detect_clipboard_cmd() == ["pbcopy"]

    def test_xclip_on_x11(self, monkeypatch):
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        avail = {"xclip": "/usr/bin/xclip"}
        monkeypatch.setattr(nomnom.shutil, "which", lambda c: avail.get(c))
        assert nomnom.detect_clipboard_cmd() == [
            "xclip", "-selection", "clipboard"
        ]

    def test_wayland_prefers_wl_copy(self, monkeypatch):
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        avail = {"wl-copy": "/usr/bin/wl-copy", "xclip": "/usr/bin/xclip"}
        monkeypatch.setattr(nomnom.shutil, "which", lambda c: avail.get(c))
        assert nomnom.detect_clipboard_cmd() == ["wl-copy"]

    def test_xsel_fallback_when_only_xsel(self, monkeypatch):
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        avail = {"xsel": "/usr/bin/xsel"}
        monkeypatch.setattr(nomnom.shutil, "which", lambda c: avail.get(c))
        assert nomnom.detect_clipboard_cmd() == [
            "xsel", "--clipboard", "--input"
        ]

    def test_returns_none_when_nothing_available(self, monkeypatch):
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(nomnom.shutil, "which", lambda c: None)
        assert nomnom.detect_clipboard_cmd() is None


class TestCopyToClipboard:
    def test_passes_text_via_stdin(self, monkeypatch):
        captured = {}

        def fake_run(cmd, input, text, check):
            captured.update(cmd=cmd, input=input, text=text, check=check)
            class R: pass
            return R()

        monkeypatch.setattr(nomnom, "detect_clipboard_cmd", lambda: ["pbcopy"])
        monkeypatch.setattr(nomnom.subprocess, "run", fake_run)
        ok = nomnom.copy_to_clipboard("hello world")
        assert ok is True
        assert captured["cmd"] == ["pbcopy"]
        assert captured["input"] == "hello world"
        assert captured["text"] is True
        assert captured["check"] is True

    def test_returns_false_when_no_tool(self, monkeypatch):
        monkeypatch.setattr(nomnom, "detect_clipboard_cmd", lambda: None)
        assert nomnom.copy_to_clipboard("x") is False

    def test_returns_false_on_subprocess_error(self, monkeypatch):
        import subprocess as sp

        def fake_run(*a, **kw):
            raise sp.CalledProcessError(1, ["pbcopy"])

        monkeypatch.setattr(nomnom, "detect_clipboard_cmd", lambda: ["pbcopy"])
        monkeypatch.setattr(nomnom.subprocess, "run", fake_run)
        assert nomnom.copy_to_clipboard("x") is False


# ---------- register / unregister ----------

def make_fixture(
    tmp_path,
    text=None,
    binary=None,
    names=None,
    secrets=None,
    prefix="# header\n",
    suffix="# footer\n",
):
    """Build a fixture .py file with a canonical marker block."""
    text = text if text is not None else {".py"}
    binary = binary if binary is not None else {".png"}
    names = names if names is not None else {"Makefile"}
    secrets = secrets if secrets is not None else [".env"]
    parsed = {
        "TEXT_EXTENSIONS": text,
        "BINARY_EXTENSIONS": binary,
        "KNOWN_TEXT_NAMES": names,
        "SECRET_PATTERNS": secrets,
    }
    block = nomnom._emit_block(parsed)
    p = tmp_path / "fixture.py"
    p.write_text(
        f"{prefix}# --- nomnom:extensions ---\n{block}# --- end nomnom:extensions ---\n{suffix}",
        encoding="utf-8",
    )
    return p


class TestRegisterValues:
    """Direct tests of the structured-API surface. The cmd_register tests below
    exercise the printing wrapper; this class pins the underlying contract."""

    def test_adds_new_value(self, tmp_path):
        p = make_fixture(tmp_path)
        result = nomnom.register_values("text", [".new"], path=p)
        assert result.added == [".new"]
        assert result.removed == []
        assert result.no_ops == []
        assert result.conflicts == []
        assert result.wrote is True
        assert result.target_name == "TEXT_EXTENSIONS"

    def test_add_existing_is_no_op(self, tmp_path):
        p = make_fixture(tmp_path)
        before = p.read_bytes()
        result = nomnom.register_values("text", [".py"], path=p)
        assert result.added == []
        assert result.no_ops == [".py"]
        assert result.wrote is False
        assert p.read_bytes() == before  # untouched

    def test_remove_existing(self, tmp_path):
        p = make_fixture(tmp_path, text={".py", ".rs"})
        result = nomnom.register_values("text", [".rs"], remove=True, path=p)
        assert result.removed == [".rs"]
        assert result.wrote is True

    def test_remove_missing_is_no_op(self, tmp_path):
        p = make_fixture(tmp_path)
        before = p.read_bytes()
        result = nomnom.register_values("text", [".never"], remove=True, path=p)
        assert result.removed == []
        assert result.no_ops == [".never"]
        assert result.wrote is False
        assert p.read_bytes() == before

    def test_conflict_returns_early_no_write(self, tmp_path):
        p = make_fixture(tmp_path, text={".py"}, binary={".png"})
        before = p.read_bytes()
        result = nomnom.register_values("text", [".png"], path=p)
        assert result.conflicts == [(".png", "binary")]
        assert result.added == []
        assert result.wrote is False
        assert p.read_bytes() == before

    def test_secret_kind_is_list_preserves_order(self, tmp_path):
        p = make_fixture(tmp_path, secrets=[".env", "*.pem"])
        result = nomnom.register_values("secret", ["*.creds"], path=p)
        assert result.added == ["*.creds"]
        # The list-kind preserves insertion order in the rewritten block.
        content = p.read_text()
        assert content.index("'.env',") < content.index("'*.pem',") < content.index("'*.creds',")

    def test_multi_value_mixes_added_and_no_ops(self, tmp_path):
        p = make_fixture(tmp_path, text={".py"})
        result = nomnom.register_values("text", [".py", ".new"], path=p)
        assert result.no_ops == [".py"]
        assert result.added == [".new"]
        assert result.wrote is True


class TestRegister:
    def test_register_text_adds_and_sorts(self, tmp_path):
        p = make_fixture(tmp_path)
        rc = nomnom.cmd_register("text", [".zzz", ".aaa"], path=p)
        assert rc == 0
        content = p.read_text()
        assert "'.aaa'," in content
        assert "'.zzz'," in content
        # sorted: .aaa before .py before .zzz
        assert content.index("'.aaa',") < content.index("'.py',") < content.index("'.zzz',")

    def test_idempotent_no_rewrite(self, tmp_path, capsys):
        p = make_fixture(tmp_path)
        before = p.read_bytes()
        rc = nomnom.cmd_register("text", [".py"], path=p)
        assert rc == 0
        out = capsys.readouterr().out
        assert "already in TEXT_EXTENSIONS" in out
        assert p.read_bytes() == before

    def test_unregister_removes(self, tmp_path):
        p = make_fixture(tmp_path, text={".py", ".rs"})
        rc = nomnom.cmd_register("text", [".rs"], remove=True, path=p)
        assert rc == 0
        content = p.read_text()
        assert "'.rs'," not in content
        assert "'.py'," in content

    def test_unregister_missing_is_noop(self, tmp_path, capsys):
        p = make_fixture(tmp_path)
        before = p.read_bytes()
        rc = nomnom.cmd_register("text", [".never"], remove=True, path=p)
        assert rc == 0
        out = capsys.readouterr().out
        assert "not in TEXT_EXTENSIONS" in out
        assert p.read_bytes() == before

    def test_conflict_refuses(self, tmp_path, capsys):
        p = make_fixture(tmp_path, text={".py"}, binary={".png"})
        before = p.read_bytes()
        rc = nomnom.cmd_register("text", [".png"], path=p)
        assert rc == 1
        err = capsys.readouterr().err
        assert "already in BINARY_EXTENSIONS" in err
        assert "unregister binary .png" in err
        assert p.read_bytes() == before

    def test_conflict_with_trailing_valid_writes_nothing(self, tmp_path, capsys):
        # A conflicting value followed by a valid one must not write a partial
        # change. After the failure, registering the valid value alone should
        # still succeed.
        p = make_fixture(tmp_path, text={".py"}, binary={".png"})
        before = p.read_bytes()
        rc = nomnom.cmd_register("text", [".png", ".new"], path=p)
        assert rc == 1
        assert p.read_bytes() == before
        rc = nomnom.cmd_register("text", [".new"], path=p)
        assert rc == 0
        assert "'.new'," in p.read_text()

    def test_multi_conflict_reports_all(self, tmp_path, capsys):
        p = make_fixture(tmp_path, text={".py"}, binary={".png", ".jpg"})
        rc = nomnom.cmd_register("text", [".png", ".jpg"], path=p)
        assert rc == 1
        err = capsys.readouterr().err
        assert "'.png'" in err
        assert "'.jpg'" in err

    def test_register_secret_preserves_list_order(self, tmp_path):
        p = make_fixture(tmp_path, secrets=[".env", "*.pem"])
        rc = nomnom.cmd_register("secret", ["*.creds"], path=p)
        assert rc == 0
        content = p.read_text()
        # New entry appended; existing order kept
        assert content.index("'.env',") < content.index("'*.pem',") < content.index("'*.creds',")

    def test_missing_marker_raises(self, tmp_path):
        p = tmp_path / "no_markers.py"
        p.write_text("TEXT_EXTENSIONS = {'.py'}\n")
        with pytest.raises(RuntimeError, match="marker block"):
            nomnom.cmd_register("text", [".x"], path=p)

    def test_multi_value_call(self, tmp_path):
        p = make_fixture(tmp_path)
        rc = nomnom.cmd_register("text", [".a", ".b", ".c"], path=p)
        assert rc == 0
        content = p.read_text()
        for ext in (".a", ".b", ".c"):
            assert f"'{ext}'," in content

    def test_register_name_with_dot(self, tmp_path):
        p = make_fixture(tmp_path)
        rc = nomnom.cmd_register("name", ["MODULE.bazel"], path=p)
        assert rc == 0
        assert "'MODULE.bazel'," in p.read_text()

    def test_round_trip_byte_equal(self, tmp_path):
        p = make_fixture(tmp_path)
        original = p.read_bytes()
        nomnom.cmd_register("text", [".new"], path=p)
        nomnom.cmd_register("text", [".new"], remove=True, path=p)
        assert p.read_bytes() == original

    def test_only_marker_block_modified(self, tmp_path):
        p = make_fixture(
            tmp_path,
            prefix="import os\nFOO = 1\ndef untouched():\n    return 42\n\n",
            suffix="\nBAR = 2\nclass Other:\n    pass\n",
        )
        nomnom.cmd_register("text", [".new"], path=p)
        content = p.read_text()
        assert "import os" in content
        assert "FOO = 1" in content
        assert "def untouched():" in content
        assert "BAR = 2" in content
        assert "class Other:" in content
        assert "'.new'," in content

    def test_real_nomnom_py_has_marker_block(self):
        # Smoke test: the actual shipping nomnom.py must be parseable by our
        # tooling, otherwise running `nomnom register` against it would fail.
        src_lines, start, end = nomnom._read_block(nomnom.SELF_PATH)
        parsed = nomnom._parse_block(src_lines, start, end)
        assert "TEXT_EXTENSIONS" in parsed
        assert "BINARY_EXTENSIONS" in parsed
        assert "KNOWN_TEXT_NAMES" in parsed
        assert "SECRET_PATTERNS" in parsed
        assert ".py" in parsed["TEXT_EXTENSIONS"]
        assert ".png" in parsed["BINARY_EXTENSIONS"]


# ---------- git/gh bundle helpers ----------

def _git(args: list[str], cwd: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "nomnom-test",
        "GIT_AUTHOR_EMAIL": "test@nomnom.local",
        "GIT_COMMITTER_NAME": "nomnom-test",
        "GIT_COMMITTER_EMAIL": "test@nomnom.local",
    }
    subprocess.run(
        ["git", *args], cwd=str(cwd), env=env,
        check=True, capture_output=True, text=True,
    )


def make_git_repo(
    root: Path, *, initial: dict | None = None, message: str = "init",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@nomnom.local"], root)
    _git(["config", "user.name", "nomnom-test"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    if initial:
        make_repo(root, initial)
        _git(["add", "."], root)
        _git(["commit", "-q", "-m", message], root)


def _make_run_stub(table: dict[tuple[str, ...], tuple[int, str, str]]):
    """Return a stub for nomnom._run that matches by command tuple prefix.

    Captures the original _run at call time so installing the stub via
    monkeypatch doesn't make the fallback recurse.
    """
    original = nomnom._run

    def stub(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
        for k, v in table.items():
            if tuple(cmd[: len(k)]) == k:
                return v
        return original(cmd, cwd)
    return stub


# ---------- cmd_commit ----------

class TestCommitBundle:
    def test_happy_path_writes_bundle(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "myrepo"
        make_git_repo(repo, initial={"a.txt": "one\n", "b.txt": "two\n"})
        # Stage one change, leave another unstaged.
        (repo / "a.txt").write_text("one-edited\n")
        _git(["add", "a.txt"], repo)
        (repo / "b.txt").write_text("two-edited\n")

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_commit(str(repo))
        assert rc == 0

        bundles = list(out_dir.glob("myrepo-main-commit-*.txt"))
        assert len(bundles) == 1
        text = bundles[0].read_text()
        assert "<section name=\"git_status\">" in text
        assert "<section name=\"diff_summary\">" in text
        assert "<section name=\"staged_diff\">" in text
        assert "<section name=\"unstaged_diff\">" in text
        assert "<section name=\"recent_commits\">" in text
        assert "<file_tree>" in text
        assert "a.txt" in text and "b.txt" in text

    def test_no_changes_errors(self, tmp_path, monkeypatch):
        repo = tmp_path / "clean"
        make_git_repo(repo, initial={"a.txt": "hi\n"})
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError, match="nothing to commit"):
            nomnom.cmd_commit(str(repo))

    def test_untracked_only_errors(self, tmp_path, monkeypatch):
        # Per spec: untracked-only doesn't qualify as "something to commit".
        repo = tmp_path / "ut"
        make_git_repo(repo, initial={"a.txt": "hi\n"})
        (repo / "new.txt").write_text("new\n")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError, match="nothing to commit"):
            nomnom.cmd_commit(str(repo))

    def test_detached_head_uses_sha_in_filename(
        self, tmp_path, monkeypatch,
    ):
        repo = tmp_path / "dh"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        (repo / "a.txt").write_text("2\n")
        _git(["add", "a.txt"], repo)
        _git(["commit", "-q", "-m", "second"], repo)
        # Detach.
        _git(["checkout", "-q", "--detach", "HEAD"], repo)
        (repo / "a.txt").write_text("3\n")
        _git(["add", "a.txt"], repo)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_commit(str(repo))
        assert rc == 0
        bundles = list(out_dir.glob("dh-*-commit-*.txt"))
        assert len(bundles) == 1
        # Short SHA is 7 hex chars; filename infix should not be "main".
        assert "main-commit" not in bundles[0].name

    def test_not_a_git_repo_errors(self, tmp_path, monkeypatch):
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError, match="not a git repository"):
            nomnom.cmd_commit(str(plain))

    def test_stdout_destination_pipes_bundle(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "r"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        (repo / "a.txt").write_text("2\n")  # creates unstaged diff
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_commit(str(repo), destination=nomnom.Destination.STDOUT)
        assert rc == 0
        captured = capsys.readouterr()
        assert '<section name="git_status">' in captured.out
        # No file written when piping.
        assert list(out_dir.glob("r-*-commit-*.txt")) == []
        # Status info goes to stderr now.
        assert "wrote" in captured.err


# ---------- cmd_pr ----------

class TestPrBundle:
    def test_missing_gh_errors(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: None if name == "gh" else "/bin/git")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError, match="requires gh"):
            nomnom.cmd_pr(str(repo), base=None)

    def test_happy_path_no_existing_pr(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        _git(["checkout", "-q", "-b", "feature"], repo)
        (repo / "a.txt").write_text("2\n")
        _git(["add", "a.txt"], repo)
        _git(["commit", "-q", "-m", "feature change"], repo)

        # Pretend gh exists.
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        # Stub gh calls; let git calls fall through.
        stub = _make_run_stub({
            ("gh", "repo", "view"): (0, "main\n", ""),
            ("gh", "pr", "view"): (1, "", "no pull requests found"),
            ("gh", "pr", "list"): (0, "[]", ""),
        })
        monkeypatch.setattr(nomnom, "_run", stub)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_pr(str(repo), base=None)
        assert rc == 0
        bundles = list(out_dir.glob("p-feature-pr-*.txt"))
        assert len(bundles) == 1
        text = bundles[0].read_text()
        assert "<section name=\"existing_pr\">\nnone\n\n</section>" in text
        assert "<section name=\"branch_info\">" in text
        assert "base:   main" in text
        assert "<section name=\"diff\">" in text

    def test_existing_pr_renders_body(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        _git(["checkout", "-q", "-b", "feature"], repo)
        (repo / "a.txt").write_text("2\n")
        _git(["add", "a.txt"], repo)
        _git(["commit", "-q", "-m", "feature change"], repo)

        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        pr_payload = json.dumps({
            "number": 42,
            "url": "https://github.com/x/y/pull/42",
            "title": "Add feature",
            "body": "describe the feature here",
            "headRefName": "feature",
            "baseRefName": "main",
        })
        stub = _make_run_stub({
            ("gh", "repo", "view"): (0, "main\n", ""),
            ("gh", "pr", "view"): (0, pr_payload, ""),
            ("gh", "pr", "list"): (0, "[]", ""),
        })
        monkeypatch.setattr(nomnom, "_run", stub)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_pr(str(repo), base=None)
        assert rc == 0
        text = next(out_dir.glob("p-feature-pr-*.txt")).read_text()
        assert "#42: Add feature" in text
        assert "describe the feature here" in text
        assert "https://github.com/x/y/pull/42" in text

    def test_base_override_skips_default_lookup(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        _git(["checkout", "-q", "-b", "develop"], repo)
        _git(["checkout", "-q", "-b", "feature"], repo)
        (repo / "a.txt").write_text("2\n")
        _git(["add", "a.txt"], repo)
        _git(["commit", "-q", "-m", "x"], repo)

        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        calls: list[list[str]] = []
        real_run = nomnom._run

        def tracking(cmd: list[str], cwd: Path):
            calls.append(list(cmd))
            if cmd[:3] == ["gh", "repo", "view"]:
                pytest.fail("default base lookup should be skipped when --base given")
            if cmd[:3] == ["gh", "pr", "view"]:
                return 1, "", "no pr"
            if cmd[:3] == ["gh", "pr", "list"]:
                return 0, "[]", ""
            return real_run(cmd, cwd)

        monkeypatch.setattr(nomnom, "_run", tracking)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_pr(str(repo), base="develop")
        assert rc == 0
        text = next(out_dir.glob("p-feature-pr-*.txt")).read_text()
        assert "base:   develop" in text

    def test_detached_head_errors(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        (repo / "a.txt").write_text("2\n")
        _git(["add", "a.txt"], repo)
        _git(["commit", "-q", "-m", "second"], repo)
        _git(["checkout", "-q", "--detach", "HEAD"], repo)

        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError, match="detached"):
            nomnom.cmd_pr(str(repo), base="main")

    def test_recent_merged_pr_body_truncation(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        _git(["checkout", "-q", "-b", "feature"], repo)
        (repo / "a.txt").write_text("2\n")
        _git(["add", "a.txt"], repo)
        _git(["commit", "-q", "-m", "x"], repo)

        long_body = "x" * 800
        merged_payload = json.dumps([
            {"title": "Old PR", "body": long_body},
        ])
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        stub = _make_run_stub({
            ("gh", "repo", "view"): (0, "main\n", ""),
            ("gh", "pr", "view"): (1, "", "no pr"),
            ("gh", "pr", "list"): (0, merged_payload, ""),
        })
        monkeypatch.setattr(nomnom, "_run", stub)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_pr(str(repo), base=None)
        assert rc == 0
        text = next(out_dir.glob("p-feature-pr-*.txt")).read_text()
        # First 500 chars of body kept, rest replaced with ellipsis.
        assert ("x" * 500 + "…") in text
        assert ("x" * 501) not in text


# ---------- cmd_item_pr ----------

def _review_stub(payloads: dict, original_run, diff_called: list | None = None):
    """Stub for nomnom._run that routes review-mode gh calls.

    `payloads` keys (each value is `(rc, stdout, stderr)`):
      "repo_view", "pr_view", "comments", "reviews", "graphql",
      "timeline", "checks", "diff" (optional).
    Anything else falls through to the captured original `_run`.
    """
    def stub(cmd, cwd):
        t = tuple(cmd)
        if t[:3] == ("gh", "repo", "view"):
            return payloads["repo_view"]
        if t[:3] == ("gh", "pr", "view"):
            return payloads["pr_view"]
        if t[:3] == ("gh", "pr", "checks"):
            return payloads.get("checks", (0, "[]", ""))
        if t[:3] == ("gh", "pr", "diff"):
            if diff_called is not None:
                diff_called.append(list(cmd))
            return payloads.get("diff", (0, "", ""))
        if t[:3] == ("gh", "api", "graphql"):
            return payloads.get("graphql", (0, "{}", ""))
        if len(t) >= 3 and t[0] == "gh" and t[1] == "api":
            url = t[2]
            if url.endswith("/comments"):
                return payloads.get("comments", (0, "[]", ""))
            if url.endswith("/reviews"):
                return payloads.get("reviews", (0, "[]", ""))
            if url.endswith("/timeline"):
                return payloads.get("timeline", (0, "[]", ""))
        return original_run(cmd, cwd)
    return stub


def _minimal_pr_payload(number: int = 1) -> str:
    return json.dumps({
        "number": number, "url": f"https://github.com/owner/name/pull/{number}",
        "title": "t", "body": "",
        "author": {"login": "a"}, "state": "OPEN",
        "headRefName": "f", "baseRefName": "main",
        "labels": [], "milestone": None, "isDraft": False,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "commits": [], "files": [], "closingIssuesReferences": [],
    })


class TestItemPrBundle:
    def test_missing_gh_errors(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(
            nomnom.shutil, "which",
            lambda name: None if name == "gh" else "/bin/git",
        )
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError, match="requires gh"):
            nomnom.cmd_item_pr(
                str(repo), pr_number=1, include_diff=False,
            )

    def test_invalid_pr_number_errors(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError, match="must be positive"):
            nomnom.cmd_item_pr(
                str(repo), pr_number=0, include_diff=False,
            )

    def test_pr_not_found_surfaces_gh_stderr(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        original_run = nomnom._run
        stub = _review_stub(
            {
                "repo_view": (0, "owner/name\n", ""),
                "pr_view": (1, "", "no pull request with this number"),
            },
            original_run,
        )
        monkeypatch.setattr(nomnom, "_run", stub)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError) as ei:
            nomnom.cmd_item_pr(
                str(repo), pr_number=42, include_diff=False,
            )
        msg = str(ei.value)
        assert "no pull request" in msg
        assert "#42" in msg

    def test_repo_view_failure_surfaces_error(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        original_run = nomnom._run
        stub = _review_stub(
            {
                "repo_view": (1, "", "no remote configured"),
                "pr_view": (0, "{}", ""),
            },
            original_run,
        )
        monkeypatch.setattr(nomnom, "_run", stub)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError, match="no remote configured"):
            nomnom.cmd_item_pr(
                str(repo), pr_number=1, include_diff=False,
            )

    def test_happy_path_minimal_pr(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        original_run = nomnom._run
        stub = _review_stub(
            {
                "repo_view": (0, "owner/name\n", ""),
                "pr_view": (0, _minimal_pr_payload(7), ""),
            },
            original_run,
        )
        monkeypatch.setattr(nomnom, "_run", stub)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_item_pr(
            str(repo), pr_number=7, include_diff=False,
        )
        assert rc == 0
        bundles = list(out_dir.glob("p-pr-7-*.txt"))
        assert len(bundles) == 1
        text = bundles[0].read_text()
        for sec in (
            "pr_meta", "pr_body", "linked_issues", "commits",
            "diff_summary", "reviews", "issue_comments",
            "review_comments", "timeline", "checks",
        ):
            assert f'<section name="{sec}">' in text
        # All optional sections fall back to (none).
        assert '<section name="linked_issues">\n(none)\n\n</section>' in text
        assert '<section name="diff">' not in text
        # File tree omitted when there are no changed files.
        assert "<file_tree>" not in text

    def test_full_pr_renders_all_sections(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        pr_payload = json.dumps({
            "number": 99,
            "url": "https://github.com/owner/name/pull/99",
            "title": "Big change", "body": "describe the change",
            "author": {"login": "alice"}, "state": "OPEN",
            "headRefName": "feat", "baseRefName": "main",
            "labels": [{"name": "bug"}, {"name": "ui"}],
            "milestone": {"title": "v1.0"},
            "isDraft": False,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-02T00:00:00Z",
            "commits": [
                {"oid": "abc1234deadbeef", "messageHeadline": "first commit"},
                {"oid": "def5678cafebabe", "messageHeadline": "second commit"},
            ],
            "files": [
                {"path": "src/a.py", "additions": 5, "deletions": 1},
                {"path": "src/b.py", "additions": 3, "deletions": 0},
            ],
            "closingIssuesReferences": [
                {"number": 12, "title": "old bug",
                 "url": "https://github.com/owner/name/issues/12",
                 "state": "OPEN"},
            ],
        })
        comments_payload = json.dumps([
            {"user": {"login": "bob"}, "body": "lgtm",
             "created_at": "2026-01-01T01:00:00Z"},
        ])
        reviews_payload = json.dumps([
            {"user": {"login": "carol"}, "state": "APPROVED",
             "submitted_at": "2026-01-01T02:00:00Z", "body": "ship it"},
        ])
        threads_payload = json.dumps({
            "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
                {
                    "isResolved": True, "isOutdated": False,
                    "path": "src/a.py", "line": 42,
                    "comments": {"nodes": [
                        {"author": {"login": "carol"},
                         "body": "rename this",
                         "createdAt": "2026-01-01T02:30:00Z"},
                    ]},
                },
                {
                    "isResolved": False, "isOutdated": True,
                    "path": "src/b.py", "line": 7,
                    "comments": {"nodes": [
                        {"author": {"login": "dave"},
                         "body": "old context",
                         "createdAt": "2026-01-01T03:00:00Z"},
                    ]},
                },
            ]}}}}
        })
        timeline_payload = json.dumps([
            {"event": "labeled", "actor": {"login": "alice"},
             "created_at": "2026-01-01T00:30:00Z",
             "label": {"name": "ui"}},
            {"event": "review_requested", "actor": {"login": "alice"},
             "created_at": "2026-01-01T01:30:00Z",
             "requested_reviewer": {"login": "carol"}},
        ])
        checks_payload = json.dumps([
            {"name": "build", "state": "SUCCESS",
             "workflow": "CI", "link": "https://ci.example/build"},
        ])
        original_run = nomnom._run
        stub = _review_stub(
            {
                "repo_view": (0, "owner/name\n", ""),
                "pr_view": (0, pr_payload, ""),
                "comments": (0, comments_payload, ""),
                "reviews": (0, reviews_payload, ""),
                "graphql": (0, threads_payload, ""),
                "timeline": (0, timeline_payload, ""),
                "checks": (0, checks_payload, ""),
            },
            original_run,
        )
        monkeypatch.setattr(nomnom, "_run", stub)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_item_pr(
            str(repo), pr_number=99, include_diff=False,
        )
        assert rc == 0
        text = next(out_dir.glob("p-pr-99-*.txt")).read_text()
        # Meta fields
        assert "Big change" in text
        assert "@alice" in text
        assert "bug, ui" in text
        assert "v1.0" in text
        # Body
        assert "describe the change" in text
        # Linked issues
        assert "#12" in text and "old bug" in text
        # Commits (short sha + message)
        assert "abc1234" in text and "first commit" in text
        # Diff summary
        assert "src/a.py" in text and "+5" in text
        assert "total: 2 files" in text
        # Reviews
        assert "@carol" in text and "[APPROVED]" in text and "ship it" in text
        # Issue comments
        assert "@bob" in text and "lgtm" in text
        # Review threads (grouped, tagged)
        assert "## src/a.py:42" in text
        assert "[resolved]" in text
        assert "## src/b.py:7" in text
        assert "[outdated]" in text
        # Timeline filtered: review_requested kept, labeled dropped
        assert "review_requested by @alice" in text
        assert "requested @carol" in text
        assert "labeled" not in text
        # Checks
        assert "build" in text and "SUCCESS" in text
        # File tree built from changed files
        assert "<file_tree>" in text
        assert "src/" in text

    def test_review_comments_grouped_by_file_then_line(
        self, tmp_path, monkeypatch,
    ):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        threads_payload = json.dumps({
            "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
                {"isResolved": False, "isOutdated": False,
                 "path": "src/b.py", "line": 5,
                 "comments": {"nodes": [
                     {"author": {"login": "x"}, "body": "b5",
                      "createdAt": "t"}]}},
                {"isResolved": False, "isOutdated": False,
                 "path": "src/a.py", "line": 20,
                 "comments": {"nodes": [
                     {"author": {"login": "x"}, "body": "a20",
                      "createdAt": "t"}]}},
                {"isResolved": False, "isOutdated": False,
                 "path": "src/a.py", "line": 10,
                 "comments": {"nodes": [
                     {"author": {"login": "x"}, "body": "a10",
                      "createdAt": "t"}]}},
            ]}}}}
        })
        original_run = nomnom._run
        stub = _review_stub(
            {
                "repo_view": (0, "owner/name\n", ""),
                "pr_view": (0, _minimal_pr_payload(5), ""),
                "graphql": (0, threads_payload, ""),
            },
            original_run,
        )
        monkeypatch.setattr(nomnom, "_run", stub)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        nomnom.cmd_item_pr(
            str(repo), pr_number=5, include_diff=False,
        )
        text = next(out_dir.glob("p-pr-5-*.txt")).read_text()
        i_a10 = text.index("## src/a.py:10")
        i_a20 = text.index("## src/a.py:20")
        i_b5 = text.index("## src/b.py:5")
        assert i_a10 < i_a20 < i_b5

    def test_timeline_filters_noise(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        timeline_payload = json.dumps([
            {"event": "labeled", "actor": {"login": "a"},
             "created_at": "t1"},
            {"event": "subscribed", "actor": {"login": "a"},
             "created_at": "t2"},
            {"event": "mentioned", "actor": {"login": "a"},
             "created_at": "t3"},
            {"event": "ready_for_review", "actor": {"login": "a"},
             "created_at": "t4"},
            {"event": "head_ref_force_pushed", "actor": {"login": "a"},
             "created_at": "t5",
             "before": "abcdef0aaaaaaaa", "after": "1234567bbbbbbbb"},
        ])
        original_run = nomnom._run
        stub = _review_stub(
            {
                "repo_view": (0, "owner/name\n", ""),
                "pr_view": (0, _minimal_pr_payload(8), ""),
                "timeline": (0, timeline_payload, ""),
            },
            original_run,
        )
        monkeypatch.setattr(nomnom, "_run", stub)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        nomnom.cmd_item_pr(
            str(repo), pr_number=8, include_diff=False,
        )
        text = next(out_dir.glob("p-pr-8-*.txt")).read_text()
        assert "ready_for_review" in text
        assert "head_ref_force_pushed" in text
        assert "abcdef0 -> 1234567" in text
        assert "labeled" not in text
        assert "subscribed" not in text
        assert "mentioned" not in text

    def test_diff_off_by_default(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        original_run = nomnom._run
        diff_called: list = []
        stub = _review_stub(
            {
                "repo_view": (0, "owner/name\n", ""),
                "pr_view": (0, _minimal_pr_payload(1), ""),
                "diff": (0, "diff content", ""),
            },
            original_run,
            diff_called=diff_called,
        )
        monkeypatch.setattr(nomnom, "_run", stub)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        nomnom.cmd_item_pr(
            str(repo), pr_number=1, include_diff=False,
        )
        assert diff_called == []
        text = next(out_dir.glob("p-pr-1-*.txt")).read_text()
        assert '<section name="diff">' not in text

    def test_diff_flag_includes_diff(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        original_run = nomnom._run
        diff_called: list = []
        stub = _review_stub(
            {
                "repo_view": (0, "owner/name\n", ""),
                "pr_view": (0, _minimal_pr_payload(2), ""),
                "diff": (0, "diff --git a b\n+x\n", ""),
            },
            original_run,
            diff_called=diff_called,
        )
        monkeypatch.setattr(nomnom, "_run", stub)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        nomnom.cmd_item_pr(
            str(repo), pr_number=2, include_diff=True,
        )
        assert len(diff_called) == 1
        text = next(out_dir.glob("p-pr-2-*.txt")).read_text()
        assert '<section name="diff">' in text
        assert "diff --git" in text

    def test_copy_flag_writes_to_clipboard(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        original_run = nomnom._run
        stub = _review_stub(
            {
                "repo_view": (0, "owner/name\n", ""),
                "pr_view": (0, _minimal_pr_payload(3), ""),
            },
            original_run,
        )
        monkeypatch.setattr(nomnom, "_run", stub)

        captured: list[str] = []
        monkeypatch.setattr(
            nomnom, "copy_to_clipboard",
            lambda text: (captured.append(text), True)[1],
        )

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_item_pr(
            str(repo), pr_number=3, include_diff=False,
            destination=nomnom.Destination.CLIPBOARD,
        )
        assert rc == 0
        assert list(out_dir.glob("p-pr-3-*.txt")) == []
        assert len(captured) == 1
        assert '<section name="pr_meta">' in captured[0]


# ---------- cmd_item_issue ----------


class TestItemIssueBundle:
    def test_happy_path(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        issue_payload = json.dumps({
            "number": 42, "title": "broken thing",
            "html_url": "https://github.com/owner/name/issues/42",
            "body": "describe the issue",
            "user": {"login": "alice"},
            "state": "open",
            "labels": [{"name": "bug"}],
            "milestone": {"title": "v1.0"},
            "assignees": [{"login": "bob"}],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        })
        comments_payload = json.dumps([
            {"user": {"login": "carol"}, "body": "+1",
             "created_at": "2026-01-01T01:00:00Z"},
        ])
        timeline_payload = json.dumps([
            {"event": "labeled", "actor": {"login": "alice"},
             "created_at": "2026-01-01T00:30:00Z",
             "label": {"name": "bug"}},
            {"event": "cross-referenced", "actor": {"login": "bob"},
             "created_at": "2026-01-01T02:00:00Z",
             "source": {"issue": {"number": 100, "title": "related pr",
                                  "html_url": "https://example.com/pr"}}},
        ])
        graphql_payload = json.dumps({
            "data": {"repository": {"issue": {
                "closedByPullRequestsReferences": {"nodes": [
                    {"number": 50, "title": "fix it",
                     "url": "https://github.com/owner/name/pull/50",
                     "state": "OPEN"},
                ]}
            }}}
        })
        original = nomnom._run

        def stub(cmd, cwd):
            t = tuple(cmd)
            if t[:3] == ("gh", "repo", "view"):
                return (0, "owner/name\n", "")
            if t[:3] == ("gh", "api", "graphql"):
                return (0, graphql_payload, "")
            if t[:2] == ("gh", "api"):
                url = t[2]
                if url.endswith("/comments"):
                    return (0, comments_payload, "")
                if url.endswith("/timeline"):
                    return (0, timeline_payload, "")
                return (0, issue_payload, "")
            return original(cmd, cwd)

        monkeypatch.setattr(nomnom, "_run", stub)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_item_issue(str(repo), 42)
        assert rc == 0
        bundles = list(out_dir.glob("p-issue-42-*.txt"))
        assert len(bundles) == 1
        text = bundles[0].read_text()
        for sec in ("issue_meta", "issue_body", "linked_prs",
                    "issue_comments", "timeline"):
            assert f'<section name="{sec}">' in text
        assert "broken thing" in text
        assert "@alice" in text
        assert "#50" in text and "fix it" in text
        assert "+1" in text
        # cross-referenced kept in _ISSUE_TIMELINE_KEEP
        assert "cross-referenced" in text
        # labeled also kept for issues
        assert "labeled" in text

    def test_rejects_pr_number(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        pr_as_issue = json.dumps({
            "number": 5, "title": "a pr",
            "pull_request": {"url": "https://example.com"},
        })
        original = nomnom._run

        def stub(cmd, cwd):
            t = tuple(cmd)
            if t[:3] == ("gh", "repo", "view"):
                return (0, "owner/name\n", "")
            if t[:2] == ("gh", "api"):
                return (0, pr_as_issue, "")
            return original(cmd, cwd)

        monkeypatch.setattr(nomnom, "_run", stub)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError, match="pull request"):
            nomnom.cmd_item_issue(str(repo), 5)


# ---------- cmd_item_discussion ----------


class TestItemDiscussionBundle:
    def test_happy_path(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        graphql_payload = json.dumps({
            "data": {"repository": {"discussion": {
                "number": 7, "title": "best practices?",
                "url": "https://github.com/owner/name/discussions/7",
                "author": {"login": "alice"},
                "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-02T00:00:00Z",
                "category": {"name": "Q&A"},
                "labels": {"nodes": [{"name": "help"}]},
                "body": "how do I do X?",
                "answer": {
                    "id": "C_abc",
                    "author": {"login": "bob"},
                    "body": "use the foo flag",
                    "createdAt": "2026-01-01T01:00:00Z",
                },
                "comments": {"nodes": [
                    {
                        "id": "C_abc",
                        "author": {"login": "bob"},
                        "body": "use the foo flag",
                        "createdAt": "2026-01-01T01:00:00Z",
                        "replies": {"nodes": [
                            {"author": {"login": "alice"},
                             "body": "thanks",
                             "createdAt": "2026-01-01T01:30:00Z"},
                        ]},
                    },
                ]},
            }}}
        })
        original = nomnom._run

        def stub(cmd, cwd):
            t = tuple(cmd)
            if t[:3] == ("gh", "repo", "view"):
                return (0, "owner/name\n", "")
            if t[:3] == ("gh", "api", "graphql"):
                return (0, graphql_payload, "")
            return original(cmd, cwd)

        monkeypatch.setattr(nomnom, "_run", stub)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_item_discussion(str(repo), 7)
        assert rc == 0
        bundles = list(out_dir.glob("p-discussion-7-*.txt"))
        assert len(bundles) == 1
        text = bundles[0].read_text()
        for sec in ("discussion_meta", "discussion_body",
                    "answer", "comments"):
            assert f'<section name="{sec}">' in text
        assert "best practices?" in text
        assert "Q&A" in text
        assert "use the foo flag" in text
        assert "[answer]" in text
        assert "thanks" in text  # nested reply

    def test_not_found_errors(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        original = nomnom._run

        def stub(cmd, cwd):
            t = tuple(cmd)
            if t[:3] == ("gh", "repo", "view"):
                return (0, "owner/name\n", "")
            if t[:3] == ("gh", "api", "graphql"):
                return (0, "{}", "")
            return original(cmd, cwd)

        monkeypatch.setattr(nomnom, "_run", stub)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError, match="not found"):
            nomnom.cmd_item_discussion(str(repo), 999)


# ---------- cmd_item_commit ----------


class TestItemCommitBundle:
    def test_happy_path(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        commit_payload = json.dumps({
            "sha": "abc1234deadbeef0000000000000000000000000",
            "html_url": "https://github.com/owner/name/commit/abc1234",
            "commit": {
                "author": {"name": "Alice", "email": "a@example.com",
                           "date": "2026-01-01T00:00:00Z"},
                "committer": {"name": "Alice", "email": "a@example.com",
                              "date": "2026-01-01T00:00:00Z"},
                "message": "fix the thing",
            },
            "parents": [{"sha": "def5678aaaaaaaaaa"}],
            "stats": {"additions": 10, "deletions": 2, "total": 12},
            "files": [
                {"filename": "src/a.py", "additions": 5, "deletions": 1},
                {"filename": "src/b.py", "additions": 5, "deletions": 1},
            ],
        })
        comments_payload = json.dumps([
            {"user": {"login": "bob"}, "body": "nice fix",
             "created_at": "2026-01-01T01:00:00Z",
             "path": "src/a.py", "line": 10},
        ])
        original = nomnom._run

        def stub(cmd, cwd):
            t = tuple(cmd)
            if t[:3] == ("gh", "repo", "view"):
                return (0, "owner/name\n", "")
            if t[:2] == ("git", "show"):
                return (0, "diff --git a b\n+x\n", "")
            if t[:2] == ("gh", "api"):
                url = t[2]
                if url.endswith("/comments"):
                    return (0, comments_payload, "")
                return (0, commit_payload, "")
            return original(cmd, cwd)

        monkeypatch.setattr(nomnom, "_run", stub)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_item_commit(
            str(repo), "abc1234deadbeef0000000000000000000000000",
            include_diff=True,
        )
        assert rc == 0
        bundles = list(out_dir.glob("p-commit-abc1234-*.txt"))
        assert len(bundles) == 1
        text = bundles[0].read_text()
        for sec in ("commit_meta", "commit_message", "diff_summary",
                    "diff", "commit_comments"):
            assert f'<section name="{sec}">' in text
        assert "fix the thing" in text
        assert "Alice" in text
        assert "diff --git" in text
        assert "src/a.py" in text and "+5" in text
        assert "nice fix" in text

    def test_invalid_sha_errors(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError, match="7-40 hex"):
            nomnom.cmd_item_commit(str(repo), "xyz", include_diff=False)


# ---------- cmd_item_release ----------


class TestItemReleaseBundle:
    def test_happy_path(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        release_payload = json.dumps({
            "tag_name": "v1.2.3",
            "name": "Version 1.2.3",
            "html_url": "https://github.com/owner/name/releases/tag/v1.2.3",
            "author": {"login": "alice"},
            "target_commitish": "main",
            "published_at": "2026-01-01T00:00:00Z",
            "created_at": "2026-01-01T00:00:00Z",
            "draft": False,
            "prerelease": False,
            "body": "release notes here",
            "assets": [
                {"name": "binary.tar.gz", "size": 1048576,
                 "download_count": 42,
                 "browser_download_url": "https://example.com/binary.tar.gz"},
            ],
        })
        original = nomnom._run

        def stub(cmd, cwd):
            t = tuple(cmd)
            if t[:3] == ("gh", "repo", "view"):
                return (0, "owner/name\n", "")
            if t[:2] == ("gh", "api"):
                return (0, release_payload, "")
            return original(cmd, cwd)

        monkeypatch.setattr(nomnom, "_run", stub)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_item_release(str(repo), "v1.2.3")
        assert rc == 0
        bundles = list(out_dir.glob("p-release-v1.2.3-*.txt"))
        assert len(bundles) == 1
        text = bundles[0].read_text()
        for sec in ("release_meta", "release_notes", "assets"):
            assert f'<section name="{sec}">' in text
        assert "Version 1.2.3" in text
        assert "v1.2.3" in text
        assert "release notes here" in text
        assert "binary.tar.gz" in text
        assert "downloads:42" in text

    def test_not_found_errors(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        original = nomnom._run

        def stub(cmd, cwd):
            t = tuple(cmd)
            if t[:3] == ("gh", "repo", "view"):
                return (0, "owner/name\n", "")
            if t[:2] == ("gh", "api"):
                return (1, "", "not found")
            return original(cmd, cwd)

        monkeypatch.setattr(nomnom, "_run", stub)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError, match="not found"):
            nomnom.cmd_item_release(str(repo), "v9.9.9")


# ---------- cmd_item_run / cmd_item_job ----------


class TestItemRunBundle:
    def test_happy_path_failed_logs(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        run_payload = json.dumps({
            "databaseId": 555, "number": 12, "attempt": 1,
            "status": "completed", "conclusion": "failure",
            "event": "push", "workflowName": "CI",
            "displayTitle": "Fix things",
            "headBranch": "main",
            "headSha": "abc1234deadbeef",
            "jobs": [
                {"name": "build", "status": "completed",
                 "conclusion": "failure",
                 "url": "https://ex/build",
                 "startedAt": "2026-01-01T00:00:00Z",
                 "completedAt": "2026-01-01T00:05:00Z"},
            ],
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:05:00Z",
            "url": "https://github.com/owner/name/actions/runs/555",
        })
        # Synthetic log lines with the job/step prefix format
        log_lines = "\n".join(
            f"build\tcompile\t2026-01-01T00:00:{i:02d}Z line {i}"
            for i in range(5)
        )
        original = nomnom._run
        log_flags: list = []

        def stub(cmd, cwd):
            t = tuple(cmd)
            if t[:3] == ("gh", "run", "view") and "--json" in cmd:
                return (0, run_payload, "")
            if t[:3] == ("gh", "run", "view"):
                log_flags.append([c for c in cmd if c.startswith("--log")])
                return (0, log_lines, "")
            return original(cmd, cwd)

        monkeypatch.setattr(nomnom, "_run", stub)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_item_run(str(repo), 555, all_logs=False)
        assert rc == 0
        # Default: failing-step output only via --log-failed
        assert ["--log-failed"] in log_flags
        bundles = list(out_dir.glob("p-run-555-*.txt"))
        assert len(bundles) == 1
        text = bundles[0].read_text()
        for sec in ("run_meta", "jobs", "failed_logs"):
            assert f'<section name="{sec}">' in text
        assert "CI" in text
        assert "build" in text
        assert "failure" in text
        assert "line 0" in text  # log content kept

    def test_all_logs_flag(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        original = nomnom._run
        log_flags: list = []

        def stub(cmd, cwd):
            t = tuple(cmd)
            if t[:3] == ("gh", "run", "view") and "--json" in cmd:
                return (0, json.dumps({
                    "databaseId": 1, "number": 1, "status": "completed",
                    "conclusion": "success", "jobs": [],
                }), "")
            if t[:3] == ("gh", "run", "view"):
                log_flags.append([c for c in cmd if c.startswith("--log")])
                return (0, "all the logs", "")
            return original(cmd, cwd)

        monkeypatch.setattr(nomnom, "_run", stub)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        nomnom.cmd_item_run(str(repo), 1, all_logs=True)
        assert ["--log"] in log_flags
        text = next(out_dir.glob("p-run-1-*.txt")).read_text()
        assert '<section name="logs">' in text


class TestItemJobBundle:
    def test_happy_path(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        job_payload = json.dumps({
            "id": 9876, "name": "build",
            "run_id": 555,
            "run_url": "https://example.com/runs/555",
            "html_url": "https://example.com/jobs/9876",
            "workflow_name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:05:00Z",
            "runner_name": "ubuntu-latest",
            "steps": [
                {"name": "checkout", "status": "completed",
                 "conclusion": "success"},
                {"name": "compile", "status": "completed",
                 "conclusion": "failure"},
            ],
        })
        original = nomnom._run

        def stub(cmd, cwd):
            t = tuple(cmd)
            if t[:3] == ("gh", "repo", "view"):
                return (0, "owner/name\n", "")
            if t[:2] == ("gh", "api"):
                return (0, job_payload, "")
            if t[:3] == ("gh", "run", "view"):
                return (0, "build\tcompile\tt error log\n", "")
            return original(cmd, cwd)

        monkeypatch.setattr(nomnom, "_run", stub)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)
        rc = nomnom.cmd_item_job(str(repo), 9876, all_logs=False)
        assert rc == 0
        bundles = list(out_dir.glob("p-job-9876-*.txt"))
        assert len(bundles) == 1
        text = bundles[0].read_text()
        for sec in ("job_meta", "steps", "failed_logs"):
            assert f'<section name="{sec}">' in text
        assert "build" in text
        assert "compile" in text
        assert "failure" in text


# ---------- cmd_item dispatcher + id resolver ----------


class TestItemDispatcher:
    def _force_kind_stubs(self, monkeypatch):
        called: dict = {}

        def stub_pr(repo, n, diff, destination=nomnom.Destination.FILE):
            called["pr"] = (repo, n, diff)
            return 0

        def stub_issue(repo, n, destination=nomnom.Destination.FILE):
            called["issue"] = (repo, n)
            return 0

        def stub_commit(repo, sha, diff, destination=nomnom.Destination.FILE):
            called["commit"] = (repo, sha, diff)
            return 0

        def stub_release(repo, tag, destination=nomnom.Destination.FILE):
            called["release"] = (repo, tag)
            return 0

        def stub_run(repo, n, all_logs=False, destination=nomnom.Destination.FILE):
            called["run"] = (repo, n, all_logs)
            return 0

        def stub_job(repo, n, all_logs=False, destination=nomnom.Destination.FILE):
            called["job"] = (repo, n, all_logs)
            return 0

        def stub_discussion(repo, n, destination=nomnom.Destination.FILE):
            called["discussion"] = (repo, n)
            return 0

        monkeypatch.setattr(nomnom, "cmd_item_pr", stub_pr)
        monkeypatch.setattr(nomnom, "cmd_item_issue", stub_issue)
        monkeypatch.setattr(nomnom, "cmd_item_commit", stub_commit)
        monkeypatch.setattr(nomnom, "cmd_item_release", stub_release)
        monkeypatch.setattr(nomnom, "cmd_item_run", stub_run)
        monkeypatch.setattr(nomnom, "cmd_item_job", stub_job)
        monkeypatch.setattr(nomnom, "cmd_item_discussion", stub_discussion)
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        return called

    def test_explicit_kind_routes(self, monkeypatch):
        called = self._force_kind_stubs(monkeypatch)
        nomnom.cmd_item(".", "issue", "42")
        assert called["issue"] == (".", 42)

    def test_explicit_pr_no_id_passes_none(self, monkeypatch):
        called = self._force_kind_stubs(monkeypatch)
        nomnom.cmd_item(".", "pr", None)
        assert called["pr"] == (".", None, False)

    def test_explicit_non_pr_no_id_errors(self, monkeypatch):
        self._force_kind_stubs(monkeypatch)
        with pytest.raises(nomnom.NomnomError, match="requires an id"):
            nomnom.cmd_item(".", "issue", None)

    def test_hex_short_circuits_to_commit(self, monkeypatch):
        called = self._force_kind_stubs(monkeypatch)
        nomnom.cmd_item(".", "abc1234def", None, include_diff=True)
        assert called["commit"] == (".", "abc1234def", True)

    def test_tag_short_circuits_to_release(self, monkeypatch):
        called = self._force_kind_stubs(monkeypatch)
        nomnom.cmd_item(".", "v1.2.3", None)
        assert called["release"] == (".", "v1.2.3")

    def test_unknown_kind_with_ident_errors(self, monkeypatch):
        self._force_kind_stubs(monkeypatch)
        with pytest.raises(nomnom.NomnomError, match="unknown kind"):
            nomnom.cmd_item(".", "foobar", "123")


class TestProbeNumericId:
    def test_solo_pr_match(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        issue_as_pr = json.dumps({
            "number": 7, "title": "fix",
            "pull_request": {"url": "..."},
        })

        def stub(cmd, cwd):
            t = tuple(cmd)
            if t[:2] == ("gh", "api") and "/issues/7" in t[2]:
                return (0, issue_as_pr, "")
            if t[:2] == ("gh", "api") and "/actions/runs/7" in t[2]:
                return (1, "", "not found")
            return (1, "", "")

        monkeypatch.setattr(nomnom, "_run", stub)
        matches = nomnom._probe_numeric_id(repo, "owner", "name", 7)
        assert matches == [("pr", "fix")]

    def test_solo_issue_match(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        plain_issue = json.dumps({"number": 8, "title": "bug"})

        def stub(cmd, cwd):
            t = tuple(cmd)
            if t[:2] == ("gh", "api") and "/issues/8" in t[2]:
                return (0, plain_issue, "")
            return (1, "", "")

        monkeypatch.setattr(nomnom, "_run", stub)
        matches = nomnom._probe_numeric_id(repo, "owner", "name", 8)
        assert matches == [("issue", "bug")]

    def test_no_matches(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})

        def stub(cmd, cwd):
            return (1, "", "")

        monkeypatch.setattr(nomnom, "_run", stub)
        assert nomnom._probe_numeric_id(repo, "owner", "name", 99) == []

    def test_ambiguous_issue_and_run(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        issue = json.dumps({"number": 5, "title": "report"})
        run = json.dumps({
            "name": "Release", "display_title": "ship",
            "workflow_name": "Release",
        })

        def stub(cmd, cwd):
            t = tuple(cmd)
            if t[:2] == ("gh", "api") and "/issues/5" in t[2]:
                return (0, issue, "")
            if t[:2] == ("gh", "api") and "/actions/runs/5" in t[2]:
                return (0, run, "")
            return (1, "", "")

        monkeypatch.setattr(nomnom, "_run", stub)
        matches = nomnom._probe_numeric_id(repo, "owner", "name", 5)
        kinds = [m[0] for m in matches]
        assert "issue" in kinds and "run" in kinds


class TestClassifyItemId:
    @pytest.mark.parametrize("value,expected", [
        ("abc1234", "commit"),
        ("abc1234deadbeef0000000000000000000000000", "commit"),
        ("v1.2.3", "release"),
        ("rel-2026", "release"),
        ("123", None),  # ambiguous numeric
        ("", None),
        ("0123abc1234567", "commit"),  # 14-char hex still counts as commit
    ])
    def test_classify(self, value, expected):
        assert nomnom._classify_item_id(value) == expected


# ---------- pick_item_output_path ----------


class TestPickItemOutputPath:
    def test_includes_kind_and_ident(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p = nomnom.pick_item_output_path("repo", "pr", "123")
        assert p.parent == tmp_path
        assert p.name.startswith("repo-pr-123-")
        assert p.suffix == ".txt"

    def test_slugifies_tag(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p = nomnom.pick_item_output_path("repo", "release", "v1/2.3")
        # forward slashes become double-underscore via _slug
        assert "v1__2.3" in p.name


# ---------- pick_git_output_path ----------

class TestPickGitOutputPath:
    def test_includes_branch_kind_and_ts(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p = nomnom.pick_git_output_path("repo", "feature/x", "pr")
        assert p.parent == tmp_path
        # `/` in branch becomes `__` so the path stays in cwd while
        # preserving distinctness from a literal `feature-x` branch.
        assert p.name.startswith("repo-feature__x-pr-")
        assert p.suffix == ".txt"

    def test_collision_handling(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        first = nomnom.pick_git_output_path("r", "main", "commit")
        first.write_text("x")
        second = nomnom.pick_git_output_path("r", "main", "commit")
        assert second != first
        assert second.name.endswith("-2.txt")


# ---------- render_git_bundle ----------

class TestRenderGitBundle:
    def test_preamble_and_sections(self):
        out = nomnom.render_git_bundle(
            "repo", "commit", "main",
            [("a", "alpha"), ("b", "beta\n")], tree=None,
        )
        assert "git context for repo (commit) on main" in out
        assert '<section name="a">\nalpha\n\n</section>' in out
        assert '<section name="b">\nbeta\n\n</section>' in out
        assert "<file_tree>" not in out

    def test_tree_inclusion(self):
        tree = nomnom.render_ascii_tree(["a.txt"], "repo")
        out = nomnom.render_git_bundle(
            "repo", "pr", "feature", [("x", "y")], tree=tree,
        )
        assert "<file_tree>" in out
        assert "</file_tree>" in out


# ---------- parse_bundle / pick_target_dir / cmd_rebuild ----------

class TestParseBundle:
    def test_round_trip_simple(self, tmp_path):
        make_repo(tmp_path, {
            "README.md": "# hi\n",
            "src": {"a.py": "print('a')\n"},
        })
        out = nomnom.render_output(
            "myrepo", tmp_path, ["README.md", "src/a.py"], None,
        )
        repo_name, files = nomnom.parse_bundle(out)
        assert repo_name == "myrepo"
        as_dict = dict(files)
        assert as_dict == {
            "README.md": "# hi\n",
            "src/a.py": "print('a')\n",
        }

    def test_round_trip_with_tree(self, tmp_path):
        make_repo(tmp_path, {"a.py": "x\n"})
        tree = nomnom.render_ascii_tree(["a.py"], "myrepo")
        out = nomnom.render_output("myrepo", tmp_path, ["a.py"], tree)
        repo_name, files = nomnom.parse_bundle(out)
        assert repo_name == "myrepo"
        assert files == [("a.py", "x\n")]

    def test_no_trailing_newline_gains_one(self, tmp_path):
        # render_output's output is lossy at the trailing-newline boundary:
        # "x" and "x\n" produce identical bundle bytes. parse_bundle defaults
        # to keeping a trailing newline, so a file written without one will
        # gain one on rebuild. Documented limitation.
        (tmp_path / "a.py").write_text("no-newline")
        out = nomnom.render_output("r", tmp_path, ["a.py"], None)
        _, files = nomnom.parse_bundle(out)
        assert files == [("a.py", "no-newline\n")]

    def test_rejects_git_bundle(self):
        out = nomnom.render_git_bundle(
            "repo", "commit", "main", [("status", "clean")], tree=None,
        )
        with pytest.raises(ValueError, match="git-context"):
            nomnom.parse_bundle(out)

    def test_rejects_unknown_header(self):
        with pytest.raises(ValueError, match="does not look like"):
            nomnom.parse_bundle("just some random text\n")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            nomnom.parse_bundle("")

    def test_rejects_no_files(self):
        # Valid preamble but no <file> blocks.
        text = (
            "This is a packed representation of selected files from repo, "
            "bundled on 2026-05-11T12:00:00. Each file is wrapped in "
            '<file path="..."> tags.\n\n'
        )
        with pytest.raises(ValueError, match="no <file> blocks"):
            nomnom.parse_bundle(text)

    def test_rejects_absolute_path(self):
        text = (
            "This is a packed representation of selected files from repo, "
            "bundled on 2026-05-11T12:00:00. Each file is wrapped in "
            '<file path="..."> tags.\n\n'
            '<file path="/etc/passwd">\nx\n</file>\n'
        )
        with pytest.raises(ValueError, match="absolute path"):
            nomnom.parse_bundle(text)

    def test_rejects_parent_traversal(self):
        text = (
            "This is a packed representation of selected files from repo, "
            "bundled on 2026-05-11T12:00:00. Each file is wrapped in "
            '<file path="..."> tags.\n\n'
            '<file path="../evil">\nx\n</file>\n'
        )
        with pytest.raises(ValueError, match="unsafe path"):
            nomnom.parse_bundle(text)

    def test_unterminated_file_block(self):
        text = (
            "This is a packed representation of selected files from repo, "
            "bundled on 2026-05-11T12:00:00. Each file is wrapped in "
            '<file path="..."> tags.\n\n'
            '<file path="a.py">\nprint(1)\n'
        )
        with pytest.raises(ValueError, match="unterminated"):
            nomnom.parse_bundle(text)


class TestPickTargetDir:
    def test_returns_base_when_free(self, tmp_path):
        assert nomnom.pick_target_dir(tmp_path, "repo") == tmp_path / "repo"

    def test_suffixes_on_collision(self, tmp_path):
        (tmp_path / "repo").mkdir()
        assert nomnom.pick_target_dir(tmp_path, "repo") == tmp_path / "repo-1"
        (tmp_path / "repo-1").mkdir()
        assert nomnom.pick_target_dir(tmp_path, "repo") == tmp_path / "repo-2"


class TestCmdRebuild:
    def _bundle(self, tmp_path, layout, repo="myrepo"):
        for rel, content in layout.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return nomnom.render_output(repo, tmp_path, list(layout), None)

    def test_writes_files_under_named_folder(self, tmp_path, monkeypatch, capsys):
        src = tmp_path / "src"
        src.mkdir()
        bundle = self._bundle(src, {
            "README.md": "# hi\n",
            "pkg/a.py": "print('a')\n",
        })
        bundle_file = tmp_path / "bundle.txt"
        bundle_file.write_text(bundle, encoding="utf-8")

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)

        rc = nomnom.cmd_rebuild(str(bundle_file), None)
        assert rc == 0
        assert (out_dir / "myrepo" / "README.md").read_text() == "# hi\n"
        assert (out_dir / "myrepo" / "pkg" / "a.py").read_text() == "print('a')\n"
        err = capsys.readouterr().err
        assert "rebuilt 2 files in myrepo/" in err

    def test_name_override(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        bundle = self._bundle(src, {"a.py": "x\n"})
        bundle_file = tmp_path / "bundle.txt"
        bundle_file.write_text(bundle, encoding="utf-8")

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)

        rc = nomnom.cmd_rebuild(str(bundle_file), "custom")
        assert rc == 0
        assert (out_dir / "custom" / "a.py").read_text() == "x\n"
        assert not (out_dir / "myrepo").exists()

    def test_conflict_suffixes(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        bundle = self._bundle(src, {"a.py": "x\n"})
        bundle_file = tmp_path / "bundle.txt"
        bundle_file.write_text(bundle, encoding="utf-8")

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "myrepo").mkdir()  # pre-existing collision
        monkeypatch.chdir(out_dir)

        rc = nomnom.cmd_rebuild(str(bundle_file), None)
        assert rc == 0
        assert (out_dir / "myrepo-1" / "a.py").read_text() == "x\n"
        # Original folder untouched.
        assert list((out_dir / "myrepo").iterdir()) == []

    def test_stdin_fallback(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        bundle = self._bundle(src, {"a.py": "x\n"})

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)

        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(bundle))
        rc = nomnom.cmd_rebuild(None, None)
        assert rc == 0
        assert (out_dir / "myrepo" / "a.py").read_text() == "x\n"

    def test_rejects_git_bundle(self, tmp_path, monkeypatch, capsys):
        git_out = nomnom.render_git_bundle(
            "repo", "commit", "main", [("status", "clean")], tree=None,
        )
        bundle_file = tmp_path / "git.txt"
        bundle_file.write_text(git_out, encoding="utf-8")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)

        rc = nomnom.cmd_rebuild(str(bundle_file), None)
        assert rc == 1
        err = capsys.readouterr().err
        assert "git-context" in err
        # Nothing should have been created.
        assert list(out_dir.iterdir()) == []

    def test_missing_file_arg(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        rc = nomnom.cmd_rebuild(str(tmp_path / "nope.txt"), None)
        assert rc == 1
        assert "not a file" in capsys.readouterr().err

    def test_name_with_separator_rejected(self, tmp_path, monkeypatch, capsys):
        src = tmp_path / "src"
        src.mkdir()
        bundle = self._bundle(src, {"a.py": "x\n"})
        bundle_file = tmp_path / "bundle.txt"
        bundle_file.write_text(bundle, encoding="utf-8")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.chdir(out_dir)

        rc = nomnom.cmd_rebuild(str(bundle_file), "foo/bar")
        assert rc == 1
        assert "path separator" in capsys.readouterr().err
        assert list(out_dir.iterdir()) == []


# ---------- encryption (wire format) ----------

class TestPayloadPackUnpack:
    def test_round_trip_text(self):
        blob = nomnom._pack_payload("hi.txt", b"hello\n")
        name, body = nomnom._unpack_payload(blob)
        assert name == "hi.txt"
        assert body == b"hello\n"

    def test_round_trip_binary(self):
        body = bytes(range(256))
        blob = nomnom._pack_payload("weird.bin", body)
        n, b = nomnom._unpack_payload(blob)
        assert n == "weird.bin"
        assert b == body

    def test_truncated_payload_rejected(self):
        with pytest.raises(ValueError, match="truncated"):
            nomnom._unpack_payload(b"\x00")
        # Claims 1000-byte header but supplies none.
        with pytest.raises(ValueError, match="truncated"):
            nomnom._unpack_payload((1000).to_bytes(2, "big") + b"")

    def test_unsafe_name_rejected(self):
        bad = nomnom._pack_payload("../evil", b"x")
        with pytest.raises(ValueError, match="unsafe name"):
            nomnom._unpack_payload(bad)

    def test_missing_name_rejected(self):
        # Hand-craft a header without a "name" field.
        import json as _json
        header = _json.dumps({"v": 1}).encode()
        blob = len(header).to_bytes(2, "big") + header + b"body"
        with pytest.raises(ValueError, match="missing 'name'"):
            nomnom._unpack_payload(blob)


class TestSealBytes:
    def test_round_trip_random(self):
        data = bytes(range(256)) * 5
        blob = nomnom.seal_bytes(data, "thing.bin", "pw")
        name, body = nomnom.open_bytes(blob, "pw")
        assert name == "thing.bin"
        assert body == data

    def test_empty_body(self):
        blob = nomnom.seal_bytes(b"", "empty.txt", "pw")
        assert nomnom.open_bytes(blob, "pw") == ("empty.txt", b"")

    def test_two_encrypts_differ(self):
        # Fresh salt + nonce per call means two encrypts of the same input
        # must produce different ciphertexts.
        a = nomnom.seal_bytes(b"same", "n", "pw")
        b = nomnom.seal_bytes(b"same", "n", "pw")
        assert a != b

    def test_deterministic_with_pinned_salt_and_nonce(self):
        a = nomnom.seal_bytes(
            b"same", "n", "pw",
            _salt=b"\x00" * 16, _nonce=b"\x01" * 12,
        )
        b = nomnom.seal_bytes(
            b"same", "n", "pw",
            _salt=b"\x00" * 16, _nonce=b"\x01" * 12,
        )
        assert a == b

    def test_wrong_passphrase_fails_auth(self):
        blob = nomnom.seal_bytes(b"secret", "f.txt", "right")
        with pytest.raises(ValueError, match="authentication failed"):
            nomnom.open_bytes(blob, "wrong")

    def test_tampered_ciphertext_fails_auth(self):
        blob = bytearray(nomnom.seal_bytes(b"secret bytes", "f.txt", "pw"))
        # Flip a bit deep in the ciphertext region (well past the 65-byte header).
        blob[100] ^= 0x01
        with pytest.raises(ValueError, match="authentication failed"):
            nomnom.open_bytes(bytes(blob), "pw")

    def test_tampered_mac_fails_auth(self):
        blob = bytearray(nomnom.seal_bytes(b"secret", "f.txt", "pw"))
        # MAC sits at offset 33..65.
        blob[40] ^= 0x80
        with pytest.raises(ValueError, match="authentication failed"):
            nomnom.open_bytes(bytes(blob), "pw")

    def test_truncated_blob_rejected(self):
        blob = nomnom.seal_bytes(b"x", "f.txt", "pw")
        with pytest.raises(ValueError, match="too short"):
            nomnom.open_bytes(blob[:30], "pw")

    def test_wrong_magic_rejected(self):
        blob = nomnom.seal_bytes(b"x", "f.txt", "pw")
        bad = b"WRONG" + blob[5:]
        with pytest.raises(ValueError, match="bad magic"):
            nomnom.open_bytes(bad, "pw")

    def test_empty_passphrase_rejected(self):
        with pytest.raises(ValueError, match="passphrase"):
            nomnom.seal_bytes(b"x", "f.txt", "")


class TestPickDecryptedPath:
    def test_returns_base_when_free(self, tmp_path):
        assert nomnom._pick_decrypted_path(tmp_path, "a.txt") == tmp_path / "a.txt"

    def test_suffixes_on_collision(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        assert nomnom._pick_decrypted_path(tmp_path, "a.txt") == tmp_path / "a-1.txt"
        (tmp_path / "a-1.txt").write_text("y")
        assert nomnom._pick_decrypted_path(tmp_path, "a.txt") == tmp_path / "a-2.txt"


# ---------- LAN transfer (trust-on-first-use model) ----------

def _mk_ident(device_id, name):
    """Build an identity dict with a real long-term DH keypair."""
    priv, pub = nomnom._dh_keypair()
    return {"device_id": device_id, "name": name,
            "ik_priv": format(priv, "x"), "ik_pub": format(pub, "x")}


class TestAtomicWrite:
    """`_atomic_write_text` survives a crash mid-write."""

    def test_partial_write_does_not_clobber_existing(self, tmp_path,
                                                     monkeypatch):
        target = tmp_path / "stored.json"
        target.write_text('{"version":2,"peers":{"old":{"ik_pub":"ab"}}}')

        # Make the inner write raise after open but before rename.
        real_fdopen = os.fdopen

        def boom_fdopen(fd, *args, **kwargs):
            # Close the descriptor to avoid leaks, then explode.
            os.close(fd)
            raise OSError("disk full")

        monkeypatch.setattr(os, "fdopen", boom_fdopen)
        with pytest.raises(OSError, match="disk full"):
            nomnom._atomic_write_text(target, '{"version":2,"peers":{}}')
        # Original file is untouched.
        assert "old" in target.read_text()

    def test_completed_write_replaces_atomically(self, tmp_path):
        target = tmp_path / "stored.json"
        target.write_text('{"version":2,"peers":{}}')
        nomnom._atomic_write_text(target, '{"version":2,"peers":{"new":1}}')
        assert "new" in target.read_text()


class TestCmdReceiveNoFeed:
    """cmd_receive errors when no feeds are joined and no --feed specified."""

    def test_no_default_feed_errors(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        rc = nomnom.cmd_receive()
        assert rc == 1
        err = capsys.readouterr().err.lower()
        assert "no default feed" in err
        assert "nomnom open" in err

    def test_named_feed_missing_errors(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        rc = nomnom.cmd_receive(feed="ghost")
        assert rc == 1
        assert "no feed named 'ghost'" in capsys.readouterr().err


class TestRetiredVerbs:
    """`nomnom pair` and friends print a helpful migration message."""

    def test_pair_is_retired(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["nomnom", "pair"])
        rc = nomnom.main()
        assert rc == 2
        err = capsys.readouterr().err
        assert "pair" in err.lower()
        assert "open" in err.lower()
        assert "join" in err.lower()

    def test_encrypt_is_retired(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["nomnom", "encrypt"])
        rc = nomnom.main()
        assert rc == 2
        assert "send" in capsys.readouterr().err.lower()

    def test_decrypt_is_retired(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["nomnom", "decrypt"])
        rc = nomnom.main()
        assert rc == 2
        assert "receive" in capsys.readouterr().err.lower()

    def test_pair_not_in_subcommands(self):
        assert "pair" not in nomnom.SUBCOMMANDS
        assert "open" in nomnom.SUBCOMMANDS
        assert "feeds" in nomnom.SUBCOMMANDS
        assert "join" in nomnom.SUBCOMMANDS


class TestV2MigrationNotice:
    def test_notice_fires_for_legacy_pin_and_no_feeds(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_known_peers({
            "dev_legacy": {
                "name": "alice-legacy", "ik_pub": "aa" * 32,
                "first_seen": 0,
            },
        })
        nomnom._maybe_print_v2_migration_notice()
        err = capsys.readouterr().err
        assert "v2 introduces feeds" in err
        assert "legacy pin" in err

    def test_no_notice_when_feeds_exist(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_known_peers({
            "dev_legacy": {
                "name": "alice-legacy", "ik_pub": "aa" * 32,
                "first_seen": 0,
            },
        })
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, _make_feed(name="home"))
        nomnom._save_feeds_config(cfg)
        nomnom._maybe_print_v2_migration_notice()
        assert capsys.readouterr().err == ""

    def test_no_notice_for_v2_pin(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_feed_pin("aa" * 32, "alice")
        nomnom._maybe_print_v2_migration_notice()
        # Only sig_pub-bearing records exist → no legacy notice.
        assert capsys.readouterr().err == ""


class TestFeedsScreen:
    def test_empty_when_no_feeds(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        screen = nomnom.FeedsScreen()
        assert screen.feeds == []

    def test_loads_feeds_sorted_by_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, _make_feed(name="zebra"))
        nomnom._add_or_replace_feed(cfg, _make_feed(name="alpha", feed_id="abcDEF99_-zy"))
        nomnom._save_feeds_config(cfg)
        screen = nomnom.FeedsScreen()
        assert [f.name for f, _ in screen.feeds] == ["alpha", "zebra"]

    def test_marks_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, _make_feed(name="home"))
        nomnom._save_feeds_config(cfg)
        screen = nomnom.FeedsScreen()
        assert any(is_default for _, is_default in screen.feeds)

    def test_esc_returns_back(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        screen = nomnom.FeedsScreen()
        assert screen.handle_key(27) == nomnom.ScreenAction.BACK
        assert screen.handle_key(ord("q")) == nomnom.ScreenAction.BACK

    def test_d_sets_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, _make_feed(name="home"))
        nomnom._add_or_replace_feed(cfg, _make_feed(name="work", feed_id="abcDEF99_-zy"))
        nomnom._save_feeds_config(cfg)
        screen = nomnom.FeedsScreen()
        # cursor starts at 0 ("home" — alphabetical); 'd' makes it default.
        screen.cursor = 1
        screen.handle_key(ord("d"))
        assert nomnom._load_feeds_config()["default"] == "work"


class TestFeedTofu:
    def test_check_auto_pins_with_trust_new(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        card = {"member_id": "m1", "identity_pubkey": "aa" * 32, "name": "alice"}
        assert nomnom._tofu_check_feed_member(card, trust_new=True) is True
        assert nomnom._find_pinned_sig("aa" * 32) is not None

    def test_check_silent_on_already_pinned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_feed_pin("aa" * 32, "alice")
        # input() must not be called.
        monkeypatch.setattr(
            "builtins.input",
            lambda *a, **k: pytest.fail("TOFU prompt fired on already-pinned"),
        )
        card = {"member_id": "m1", "identity_pubkey": "aa" * 32, "name": "alice"}
        assert nomnom._tofu_check_feed_member(card) is True

    def test_check_prompts_and_accepts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
        card = {"member_id": "m1", "identity_pubkey": "aa" * 32, "name": "alice"}
        assert nomnom._tofu_check_feed_member(card) is True
        assert nomnom._find_pinned_sig("aa" * 32) is not None

    def test_check_prompts_and_declines(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
        card = {"member_id": "m1", "identity_pubkey": "aa" * 32, "name": "alice"}
        assert nomnom._tofu_check_feed_member(card) is False
        assert nomnom._find_pinned_sig("aa" * 32) is None

    def test_check_eof_treated_as_decline(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        def raise_eof(*_a, **_k):
            raise EOFError

        monkeypatch.setattr("builtins.input", raise_eof)
        card = {"member_id": "m1", "identity_pubkey": "aa" * 32, "name": "alice"}
        assert nomnom._tofu_check_feed_member(card) is False

    def test_pin_global_across_feeds(self, tmp_path, monkeypatch):
        # Pinning under one feed makes the identity recognized in any later feed.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        card_feed_a = {"member_id": "alice-in-a", "identity_pubkey": "aa" * 32, "name": "alice"}
        card_feed_b = {"member_id": "alice-in-b", "identity_pubkey": "aa" * 32, "name": "alice"}
        nomnom._tofu_check_feed_member(card_feed_a, trust_new=True)
        # Different feed, same identity → silent (input must not fire).
        monkeypatch.setattr(
            "builtins.input",
            lambda *a, **k: pytest.fail("TOFU re-prompted across feeds"),
        )
        assert nomnom._tofu_check_feed_member(card_feed_b) is True


class TestCmdSendFeed:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        ident = nomnom._load_identity()
        member_id = "a" * 32
        token = nomnom.secrets.token_urlsafe(9)
        feed = nomnom.Feed(
            name="home",
            feed_id=token,
            feed_token=token,
            url=f"https://relay.example.com/f/{token}",
            expires_at=2_000_000_000,
            joined_at=1_700_000_000,
            member_id=member_id,
            members_cache=[
                {"member_id": member_id, "identity_pubkey": ident["sig_pub"], "name": ident["name"]},
            ],
        )
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, feed)
        nomnom._save_feeds_config(cfg)
        # Track what gets PUT to the Worker.
        posts: list = []

        def fake_list_members(host, fid, fkey, *, since_ts=0, wait_ms=0):
            return {"members": feed.members_cache}

        def fake_put_slot(host, fid, fkey, slot_id, body):
            posts.append({"slot_id": slot_id, "body": body, "feed_id": fid})

        monkeypatch.setattr(nomnom, "_relay_list_members", fake_list_members)
        monkeypatch.setattr(nomnom, "_relay_put_feed_slot", fake_put_slot)
        return tmp_path, feed, posts

    def test_send_posts_to_default_feed(self, env, capsys):
        tmp_path, feed, posts = env
        f = tmp_path / "hello.txt"
        f.write_text("hi from cmd_send")
        rc = nomnom.cmd_send(str(f))
        assert rc == 0
        assert len(posts) == 1
        assert posts[0]["feed_id"] == feed.feed_id
        # Verify the post decrypts to our payload.
        feed_key = nomnom._feed_key_from_token(feed.feed_token)
        header, body = nomnom.feed_open(
            feed_key=feed_key, feed_id=feed.feed_id, blob=posts[0]["body"],
        )
        assert body == b"hi from cmd_send"
        assert header["fn"] == "hello.txt"
        assert header["smid"] == feed.member_id

    def test_send_warns_on_lonely_feed(self, env, capsys):
        tmp_path, feed, _ = env
        f = tmp_path / "ghost.txt"
        f.write_bytes(b"x")
        rc = nomnom.cmd_send(str(f))
        assert rc == 0
        err = capsys.readouterr().err
        assert "no other members" in err

    def test_send_explicit_feed(self, env, capsys):
        tmp_path, feed, posts = env
        f = tmp_path / "hi.txt"
        f.write_bytes(b"x")
        rc = nomnom.cmd_send(str(f), feed="home")
        assert rc == 0
        assert len(posts) == 1

    def test_send_unknown_feed_errors(self, env, capsys):
        tmp_path, _, _ = env
        f = tmp_path / "a.txt"
        f.write_bytes(b"x")
        rc = nomnom.cmd_send(str(f), feed="missing")
        assert rc == 1
        assert "no feed named 'missing'" in capsys.readouterr().err

    def test_send_nonexistent_file_errors(self, env, capsys):
        rc = nomnom.cmd_send("/nope/does/not/exist.txt")
        assert rc == 1


class TestCmdReceiveFeed:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        # Set up "alice" as the receiver and "bob" as the sender.
        alice_ident = nomnom._load_identity()
        bob_seed, bob_pub = nomnom.ed25519_keypair()
        bob_member = "b" * 32
        alice_member = "a" * 32
        token = nomnom.secrets.token_urlsafe(9)
        feed = nomnom.Feed(
            name="home",
            feed_id=token,
            feed_token=token,
            url=f"https://relay.example.com/f/{token}",
            expires_at=2_000_000_000,
            joined_at=1_700_000_000,
            member_id=alice_member,
            members_cache=[
                {"member_id": alice_member, "identity_pubkey": alice_ident["sig_pub"], "name": "alice"},
                {"member_id": bob_member, "identity_pubkey": bob_pub.hex(), "name": "bob"},
            ],
        )
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, feed)
        nomnom._save_feeds_config(cfg)

        feed_key = nomnom._feed_key_from_token(token)
        bob_post = nomnom.feed_seal(
            feed_key=feed_key,
            feed_id=token,
            sender_member_id=bob_member,
            sender_sig_priv_hex=bob_seed.hex(),
            sender_sig_pub_hex=bob_pub.hex(),
            filename="from-bob.txt",
            body=b"hello alice",
        )

        slots: list = [{"slot_id": "slot-1", "created_at": 1, "body": bob_post}]

        def fake_list_slots(host, fid, fkey, *, since_ts=0, wait_ms=0):
            fresh = [s for s in slots if s["created_at"] > since_ts]
            if not fresh and wait_ms > 0:
                # Simulate a 30s timeout returning empty (avoid actually sleeping).
                return []
            return [{"slot_id": s["slot_id"], "created_at": s["created_at"]} for s in fresh]

        def fake_get_slot(host, fid, fkey, slot_id, *, wait_ms=0):
            for s in slots:
                if s["slot_id"] == slot_id:
                    return s["body"]
            return None

        monkeypatch.setattr(nomnom, "_relay_list_feed_slots", fake_list_slots)
        monkeypatch.setattr(nomnom, "_relay_get_feed_slot", fake_get_slot)
        return tmp_path, feed, slots

    def test_receive_once_decodes_post(self, env, capsys, monkeypatch):
        tmp_path, feed, slots = env
        monkeypatch.chdir(tmp_path)
        rc = nomnom.cmd_receive(once=True)
        assert rc == 0
        err = capsys.readouterr().err
        assert "from-bob.txt" in err
        assert "from bob" in err
        assert (tmp_path / "from-bob.txt").read_bytes() == b"hello alice"

    def test_receive_skips_own_post(self, env, capsys, monkeypatch):
        tmp_path, feed, slots = env
        monkeypatch.chdir(tmp_path)
        # Add a post authored by alice (this device); should be skipped.
        alice_ident = nomnom._load_identity()
        feed_key = nomnom._feed_key_from_token(feed.feed_token)
        alice_post = nomnom.feed_seal(
            feed_key=feed_key,
            feed_id=feed.feed_id,
            sender_member_id=feed.member_id,
            sender_sig_priv_hex=alice_ident["sig_priv"],
            sender_sig_pub_hex=alice_ident["sig_pub"],
            filename="echo.txt",
            body=b"my own",
        )
        slots.insert(0, {"slot_id": "slot-0", "created_at": 0.5, "body": alice_post})
        rc = nomnom.cmd_receive(once=True)
        assert rc == 0
        # Only bob's file was written; alice's echo was skipped.
        assert not (tmp_path / "echo.txt").exists()
        assert (tmp_path / "from-bob.txt").exists()

    def test_receive_once_no_post_returns_failure(
        self, tmp_path, monkeypatch, capsys,
    ):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        token = nomnom.secrets.token_urlsafe(9)
        feed = nomnom.Feed(
            name="home", feed_id=token, feed_token=token,
            url=f"https://relay.example.com/f/{token}",
            expires_at=2_000_000_000, joined_at=1_700_000_000,
            member_id="a" * 32, members_cache=[],
        )
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, feed)
        nomnom._save_feeds_config(cfg)
        monkeypatch.setattr(
            nomnom, "_relay_list_feed_slots",
            lambda *a, **k: [],
        )
        rc = nomnom.cmd_receive(once=True)
        assert rc == 1
        assert "no transfer" in capsys.readouterr().err


class TestJoinToken:
    def test_format_then_parse_round_trip(self):
        host = "relay.spencerjireh.com"
        secret = "k4n2pX9qLm3T"
        token = nomnom._format_join_token(host, secret)
        assert token == f"{host}#{secret}"
        assert nomnom._parse_join_token(token) == (host, secret)

    def test_parse_strips_outer_whitespace(self):
        host, secret = nomnom._parse_join_token("  host.example#abc  ")
        assert host == "host.example"
        assert secret == "abc"

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "    ",
            "no-hash-here",
            "two##hashes",
            "a#b#c",
            "#secretonly",
            "hostonly#",
            "https://host.example#abc",
            "host.example/path#abc",
            "host.example:8443#abc",
            "host with space#abc",
            "host.example#bad secret",
        ],
    )
    def test_parse_rejects_malformed(self, token):
        with pytest.raises(nomnom.NomnomError):
            nomnom._parse_join_token(token)

    def test_parse_rejects_non_string(self):
        with pytest.raises(nomnom.NomnomError):
            nomnom._parse_join_token(None)  # type: ignore[arg-type]


class TestRelayConfig:
    def test_load_returns_none_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert nomnom._load_relay_config() is None

    def test_save_then_load_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_relay_config("https://relay.example/", "secret-abc")
        cfg = nomnom._load_relay_config()
        assert cfg is not None
        # trailing slash stripped
        assert cfg["url"] == "https://relay.example"
        assert cfg["secret"] == "secret-abc"

    def test_saved_file_is_mode_600(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_relay_config("https://relay.example", "x")
        path = tmp_path / "nomnom" / "relay.json"
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_load_rejects_malformed_url(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        # write a bad URL by hand (bypassing _save_relay_config)
        (tmp_path / "nomnom").mkdir()
        (tmp_path / "nomnom" / "relay.json").write_text(
            '{"url": "ftp://nope", "secret": "x"}', encoding="utf-8",
        )
        assert nomnom._load_relay_config() is None

    def test_clear_removes_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_relay_config("https://relay.example", "x")
        assert nomnom._relay_clear_config() is True
        assert nomnom._load_relay_config() is None
        # second call is a no-op
        assert nomnom._relay_clear_config() is False

class TestRelayHmac:
    def test_headers_have_expected_shape(self):
        h = nomnom._relay_hmac_headers("secret-xyz", "PUT", "/slots/abc")
        assert h["User-Agent"].startswith("nomnom-relay-client/")
        auth = h["Authorization"]
        assert auth.startswith("NMNM-HMAC-SHA256 ")
        rest = auth[len("NMNM-HMAC-SHA256 "):]
        ts, mac = rest.split(":", 1)
        assert ts.isdigit() and int(ts) > 0
        # 64 hex chars = 32 byte HMAC-SHA256
        assert len(mac) == 64 and all(c in "0123456789abcdef" for c in mac)

    def test_signature_is_deterministic_for_fixed_ts(self, monkeypatch):
        # Pin time so the timestamp is stable, then check the MAC matches a
        # hand-computed value.
        monkeypatch.setattr(nomnom.time, "time", lambda: 1_700_000_000)
        h = nomnom._relay_hmac_headers("topsecret", "GET", "/slots/foo")
        msg = b"GET\n/slots/foo\n1700000000"
        import hmac as _hmac
        import hashlib as _h
        expected = _hmac.new(b"topsecret", msg, _h.sha256).hexdigest()
        assert h["Authorization"].endswith(":" + expected)


class _MockRelay:
    """An in-process Worker stand-in. Stores slots in a dict; HMACs the same
    way the real Worker does. Used by integration tests to exercise the
    Python HTTP client without touching Cloudflare."""

    def __init__(self, secret: str = "test-secret"):
        import hashlib as _h
        import hmac as _hm
        import http.server
        import socketserver
        import threading as _t
        import time as _time

        self.secret = secret
        self.slots: dict = {}
        self.lock = _t.Lock()
        self.put_event = _t.Event()

        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a, **kw):  # silence test noise
                pass

            def _verify(self):
                if self.path == "/health":
                    return True
                auth = self.headers.get("Authorization", "")
                prefix = "NMNM-HMAC-SHA256 "
                if not auth.startswith(prefix):
                    self.send_error(401, "missing-mac"); return False
                rest = auth[len(prefix):]
                if ":" not in rest:
                    self.send_error(401, "bad-mac"); return False
                ts, mac = rest.split(":", 1)
                try:
                    ts_int = int(ts)
                except ValueError:
                    self.send_error(401, "bad-mac"); return False
                if abs(int(_time.time()) - ts_int) > 300:
                    self.send_error(401, "clock-skew"); return False
                # Path on the request line excludes query for HMAC
                path_only = self.path.split("?", 1)[0]
                msg = f"{self.command}\n{path_only}\n{ts}".encode("utf-8")
                expected = _hm.new(relay.secret.encode(), msg,
                                   _h.sha256).hexdigest()
                if not _hm.compare_digest(expected, mac.lower()):
                    self.send_error(401, "bad-mac"); return False
                return True

            def do_GET(self):
                if self.path == "/health":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"ok")
                    return
                if not self._verify():
                    return
                path = self.path.split("?", 1)[0]
                q = self.path.split("?", 1)[1] if "?" in self.path else ""
                wait_ms = 0
                for kv in q.split("&"):
                    if kv.startswith("wait="):
                        try:
                            wait_ms = int(kv.split("=", 1)[1])
                        except ValueError:
                            pass
                m = re.match(r"^/slots/([A-Za-z0-9_-]+)$", path)
                if not m:
                    self.send_error(404, "not-found"); return
                key = m.group(1)
                deadline = _time.time() + (wait_ms / 1000.0)
                while True:
                    with relay.lock:
                        val = relay.slots.pop(key, None)
                    if val is not None:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(len(val)))
                        self.end_headers()
                        self.wfile.write(val)
                        return
                    remaining = deadline - _time.time()
                    if remaining <= 0:
                        self.send_error(404, "not-found"); return
                    relay.put_event.wait(min(0.1, remaining))
                    relay.put_event.clear()

            def do_PUT(self):
                if not _verify_self(self): return
                path = self.path.split("?", 1)[0]
                m = re.match(r"^/slots/([A-Za-z0-9_-]+)$", path)
                if not m:
                    self.send_error(404, "not-found"); return
                key = m.group(1)
                clen = int(self.headers.get("Content-Length", "0"))
                if clen > 256 * 1024 * 1024:
                    self.send_error(413, "too-large"); return
                body = self.rfile.read(clen) if clen > 0 else b""
                with relay.lock:
                    if key in relay.slots:
                        self.send_error(409, "occupied"); return
                    relay.slots[key] = body
                relay.put_event.set()
                self.send_response(204)
                self.end_headers()

            def do_DELETE(self):
                if not self._verify(): return
                path = self.path.split("?", 1)[0]
                m = re.match(r"^/slots/([A-Za-z0-9_-]+)$", path)
                if m:
                    with relay.lock:
                        relay.slots.pop(m.group(1), None)
                self.send_response(204)
                self.end_headers()

        # local helper because Python doesn't let the inner class call self._verify
        # cleanly from another method when send_error closes the connection
        def _verify_self(h):
            return h._verify()

        import re  # imported locally so module-level imports stay tidy

        # ThreadingTCPServer so concurrent requests don't serialize: a
        # long-poll GET must not block a subsequent PUT.
        class _ThreadingTCPServer(socketserver.ThreadingMixIn,
                                  socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = _ThreadingTCPServer(("127.0.0.1", 0), Handler,
                                     bind_and_activate=True)
        self.server = server
        self.port = server.server_address[1]
        self.thread = _t.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def cfg(self) -> dict:
        return {"url": self.url(), "secret": self.secret}

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def mock_relay():
    relay = _MockRelay()
    yield relay
    relay.stop()


class TestRelayHttp:
    def test_health_works_without_auth(self, mock_relay):
        assert nomnom._relay_health(mock_relay.cfg()) is True

    def test_put_get_round_trip(self, mock_relay):
        cfg = mock_relay.cfg()
        nomnom._relay_put_slot(cfg, "abc-xyz", b"hello world")
        got = nomnom._relay_get_slot(cfg, "abc-xyz")
        assert got == b"hello world"
        # delete-on-read: second GET returns None
        assert nomnom._relay_get_slot(cfg, "abc-xyz") is None

    def test_put_conflict_returns_error(self, mock_relay):
        cfg = mock_relay.cfg()
        nomnom._relay_put_slot(cfg, "dupe", b"first")
        with pytest.raises(nomnom.NomnomError) as exc:
            nomnom._relay_put_slot(cfg, "dupe", b"second")
        assert "occupied" in str(exc.value)

    def test_bad_secret_is_401(self, mock_relay):
        cfg = dict(mock_relay.cfg()); cfg["secret"] = "wrong"
        with pytest.raises(nomnom.NomnomError) as exc:
            nomnom._relay_put_slot(cfg, "abc", b"x")
        assert "auth" in str(exc.value).lower() or "secret" in str(exc.value).lower()

    def test_oversized_body_rejected_client_side(self, mock_relay):
        cfg = mock_relay.cfg()
        with pytest.raises(nomnom.NomnomError) as exc:
            nomnom._relay_put_slot(cfg, "abc", b"\x00" * (nomnom._RELAY_MAX_BODY + 1))
        assert "too large" in str(exc.value).lower()

    def test_delete_is_idempotent(self, mock_relay):
        cfg = mock_relay.cfg()
        nomnom._relay_delete_slot(cfg, "never-existed")  # does not raise

    def test_self_test_round_trip(self, mock_relay):
        rc, msg = nomnom._relay_self_test(mock_relay.cfg())
        assert rc == 0 and "ok" in msg

    def test_long_poll_returns_on_arrival(self, mock_relay):
        """Receiver starts polling first; sender PUTs; receiver should wake up."""
        cfg = mock_relay.cfg()
        result_q: list = []

        def receiver():
            result_q.append(nomnom._relay_get_slot(cfg, "lp", wait_ms=3000))

        t = threading.Thread(target=receiver, daemon=True)
        t.start()
        time.sleep(0.2)  # let the poller establish
        nomnom._relay_put_slot(cfg, "lp", b"delivered")
        t.join(timeout=2.0)
        assert result_q == [b"delivered"]

    def test_long_poll_timeout_returns_none(self, mock_relay):
        got = nomnom._relay_get_slot(mock_relay.cfg(), "never", wait_ms=300)
        assert got is None

class TestDefenseInDepth:
    """Regression coverage for Commit D — content-length cap, SSRF allowlist,
    tmp-file permission race, poll-worker error surfacing."""

    def test_save_relay_config_refuses_loopback_by_default(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        with pytest.raises(nomnom.NomnomError, match="private/loopback"):
            nomnom._save_relay_config("http://127.0.0.1:8787", "secret")

    def test_save_relay_config_refuses_metadata_address(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        with pytest.raises(nomnom.NomnomError, match="private/loopback"):
            nomnom._save_relay_config("http://169.254.169.254/", "secret")

    def test_save_relay_config_accepts_loopback_with_allow_private(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_relay_config(
            "http://127.0.0.1:8787", "secret", allow_private=True,
        )
        cfg = nomnom._load_relay_config()
        assert cfg["url"] == "http://127.0.0.1:8787"

    def test_save_relay_config_unresolvable_host_is_allowed(
        self, tmp_path, monkeypatch,
    ):
        """DNS failure should not block save — self-test catches genuinely
        broken Workers, and a hostname that doesn't resolve isn't an SSRF
        vector by itself."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_relay_config("https://relay.example.invalid", "secret")
        cfg = nomnom._load_relay_config()
        assert cfg["secret"] == "secret"

    def test_save_relay_config_tmp_file_never_world_readable(
        self, tmp_path, monkeypatch,
    ):
        """The tmp file must be mode 0o600 before any content lands, never
        the default umask 0o644. _atomic_write_text calls fchmod immediately
        after mkstemp so the umask window is closed before write()."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        captured_modes: list = []
        original_fchmod = os.fchmod

        def watched_fchmod(fd, mode):
            captured_modes.append(mode)
            return original_fchmod(fd, mode)

        monkeypatch.setattr(nomnom.os, "fchmod", watched_fchmod)
        nomnom._save_relay_config(
            "http://127.0.0.1:8787", "secret", allow_private=True,
        )
        assert captured_modes == [0o600]

    def test_relay_request_rejects_oversized_content_length(
        self, mock_relay, monkeypatch,
    ):
        """Hostile relay declaring a huge body must be refused before read()."""
        cfg = mock_relay.cfg()

        class _BadResp:
            status = 200
            def getheader(self, name, default=None):
                if name.lower() == "content-length":
                    return str(nomnom._RELAY_MAX_BODY + 1024)
                return default
            def read(self, *_a, **_kw):
                raise AssertionError("should not be called")

        class _BadConn:
            def request(self, *_a, **_kw): pass
            def getresponse(self): return _BadResp()
            def close(self): pass

        monkeypatch.setattr(nomnom, "_relay_open", lambda _r: _BadConn())
        with pytest.raises(nomnom.NomnomError, match="oversized body"):
            nomnom._relay_request(cfg, "GET", "/slots/x")

class TestCmdReset:
    """`nomnom reset` wipes ~/.config/nomnom/ after y/N, refuses on non-tty."""

    @staticmethod
    def _seed(tmp_path) -> Path:
        d = tmp_path / "nomnom"
        d.mkdir(parents=True)
        (d / "identity.json").write_text('{"device_id":"dev-x"}')
        (d / "known_peers.json").write_text('{"version":2,"peers":{}}')
        (d / "relay.json").write_text('{"url":"https://x","secret":"s"}')
        return d

    def test_wipes_config_dir_on_yes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        seeded = self._seed(tmp_path)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: "y")
        rc = nomnom.cmd_reset(None)
        assert rc == 0
        assert not seeded.exists()

    def test_declines_on_no(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        seeded = self._seed(tmp_path)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: "n")
        rc = nomnom.cmd_reset(None)
        assert rc == 1
        assert (seeded / "identity.json").exists()

    def test_refuses_non_tty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        seeded = self._seed(tmp_path)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        rc = nomnom.cmd_reset(None)
        assert rc == 2
        assert "tty" in capsys.readouterr().err.lower()
        assert (seeded / "identity.json").exists()

    def test_no_op_when_dir_missing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        # No seed — dir doesn't exist.
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        # input must NOT be called.
        monkeypatch.setattr(
            "builtins.input",
            lambda *_a, **_k: pytest.fail("input prompted on empty reset"),
        )
        rc = nomnom.cmd_reset(None)
        assert rc == 0
        assert "nothing to reset" in capsys.readouterr().err.lower()

    def test_no_op_when_dir_empty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "nomnom").mkdir(parents=True)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "builtins.input",
            lambda *_a, **_k: pytest.fail("input prompted on empty reset"),
        )
        rc = nomnom.cmd_reset(None)
        assert rc == 0
        assert "nothing to reset" in capsys.readouterr().err.lower()


# ---------- feed crypto (v2) ----------


class TestHkdf:
    def test_known_answer_rfc5869_test_case_1(self):
        # RFC 5869 §A.1 test case 1 for HKDF-SHA256.
        ikm = bytes.fromhex("0b" * 22)
        salt = bytes.fromhex("000102030405060708090a0b0c")
        info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
        out = nomnom._hkdf(salt=salt, ikm=ikm, info=info, length=42)
        assert out.hex() == (
            "3cb25f25faacd57a90434f64d0362f2a"
            "2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
            "34007208d5b887185865"
        )

    def test_deterministic(self):
        a = nomnom._hkdf(salt=b"s", ikm=b"k", info=b"i", length=32)
        b = nomnom._hkdf(salt=b"s", ikm=b"k", info=b"i", length=32)
        assert a == b

    def test_length_varies(self):
        full = nomnom._hkdf(salt=b"s", ikm=b"k", info=b"i", length=64)
        half = nomnom._hkdf(salt=b"s", ikm=b"k", info=b"i", length=32)
        assert full[:32] == half

    def test_info_changes_output(self):
        a = nomnom._hkdf(salt=b"s", ikm=b"k", info=b"x", length=32)
        b = nomnom._hkdf(salt=b"s", ikm=b"k", info=b"y", length=32)
        assert a != b


class TestEd25519:
    def test_rfc8032_test_vector_1(self):
        # RFC 8032 §7.1 test 1: empty message.
        seed = bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc4"
            "4449c5697b326919703bac031cae7f60"
        )
        expected_pub = bytes.fromhex(
            "d75a980182b10ab7d54bfed3c964073a"
            "0ee172f3daa62325af021a68f707511a"
        )
        expected_sig = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a"
            "84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46b"
            "d25bf5f0595bbe24655141438e7a100b"
        )
        pub = nomnom.ed25519_pub_from_seed(seed)
        assert pub == expected_pub
        sig = nomnom.ed25519_sign(b"", seed)
        assert sig == expected_sig
        assert nomnom.ed25519_verify(b"", sig, pub)

    def test_sign_verify_roundtrip(self):
        seed, pub = nomnom.ed25519_keypair()
        msg = b"hello feeds"
        sig = nomnom.ed25519_sign(msg, seed)
        assert nomnom.ed25519_verify(msg, sig, pub)

    def test_verify_rejects_tampered_message(self):
        seed, pub = nomnom.ed25519_keypair()
        sig = nomnom.ed25519_sign(b"original", seed)
        assert not nomnom.ed25519_verify(b"tampered", sig, pub)

    def test_verify_rejects_wrong_key(self):
        seed1, _ = nomnom.ed25519_keypair()
        _, pub2 = nomnom.ed25519_keypair()
        sig = nomnom.ed25519_sign(b"msg", seed1)
        assert not nomnom.ed25519_verify(b"msg", sig, pub2)

    def test_verify_rejects_bad_sizes(self):
        seed, pub = nomnom.ed25519_keypair()
        sig = nomnom.ed25519_sign(b"msg", seed)
        assert not nomnom.ed25519_verify(b"msg", sig[:63], pub)
        assert not nomnom.ed25519_verify(b"msg", sig, pub[:31])

    def test_keygen_yields_distinct_keys(self):
        a, _ = nomnom.ed25519_keypair()
        b, _ = nomnom.ed25519_keypair()
        assert a != b


class TestFeedKeyDerivation:
    def test_deterministic(self):
        token = "k4n2pX9qLm3T"
        a = nomnom._feed_key_from_token(token)
        b = nomnom._feed_key_from_token(token)
        assert a == b
        assert len(a) == 32

    def test_different_tokens_yield_different_keys(self):
        a = nomnom._feed_key_from_token("k4n2pX9qLm3T")
        b = nomnom._feed_key_from_token("k4n2pX9qLm3U")
        assert a != b

    def test_rejects_empty_token(self):
        with pytest.raises(ValueError):
            nomnom._feed_key_from_token("")

    def test_rejects_non_base64_token(self):
        with pytest.raises(ValueError):
            nomnom._feed_key_from_token("not!base64!at!all")

    def test_request_mac_matches_worker_scheme(self):
        # Same shape the Worker's verifyFeedKey expects.
        key = b"\x01" * 32
        mac1 = nomnom._feed_request_mac(key, "GET", "/feeds/abc/meta", 100)
        mac2 = nomnom._feed_request_mac(key, "GET", "/feeds/abc/meta", 100)
        assert mac1 == mac2
        mac3 = nomnom._feed_request_mac(key, "GET", "/feeds/abc/meta", 101)
        assert mac3 != mac1


class TestFeedSeal:
    def _seed_and_pub(self) -> tuple[str, str]:
        seed, pub = nomnom.ed25519_keypair()
        return seed.hex(), pub.hex()

    def test_seal_open_roundtrip(self):
        feed_key = nomnom._feed_key_from_token("k4n2pX9qLm3T")
        sig_priv, sig_pub = self._seed_and_pub()
        body = b"file contents here"
        blob = nomnom.feed_seal(
            feed_key=feed_key,
            feed_id="k4n2pX9qLm3T",
            sender_member_id="mem-abc",
            sender_sig_priv_hex=sig_priv,
            sender_sig_pub_hex=sig_pub,
            filename="hello.txt",
            body=body,
        )
        header, recovered = nomnom.feed_open(
            feed_key=feed_key,
            feed_id="k4n2pX9qLm3T",
            blob=blob,
        )
        assert recovered == body
        assert header["fn"] == "hello.txt"
        assert header["smid"] == "mem-abc"
        assert header["sik"] == sig_pub
        assert header["fs"] == len(body)

    def test_open_rejects_wrong_feed_key(self):
        feed_key = nomnom._feed_key_from_token("k4n2pX9qLm3T")
        wrong_key = nomnom._feed_key_from_token("k4n2pX9qLm3U")
        sig_priv, sig_pub = self._seed_and_pub()
        blob = nomnom.feed_seal(
            feed_key=feed_key,
            feed_id="k4n2pX9qLm3T",
            sender_member_id="mem-abc",
            sender_sig_priv_hex=sig_priv,
            sender_sig_pub_hex=sig_pub,
            filename="hello.txt",
            body=b"data",
        )
        with pytest.raises(ValueError, match="authentication failed"):
            nomnom.feed_open(feed_key=wrong_key, feed_id="k4n2pX9qLm3T", blob=blob)

    def test_open_rejects_wrong_feed_id(self):
        feed_key = nomnom._feed_key_from_token("k4n2pX9qLm3T")
        sig_priv, sig_pub = self._seed_and_pub()
        blob = nomnom.feed_seal(
            feed_key=feed_key,
            feed_id="k4n2pX9qLm3T",
            sender_member_id="mem-abc",
            sender_sig_priv_hex=sig_priv,
            sender_sig_pub_hex=sig_pub,
            filename="hello.txt",
            body=b"data",
        )
        # Decryption succeeds (same feed_key), but the transcript signature
        # binds to feed_id "k4n2pX9qLm3T" — opening under a different feed_id
        # fails signature verification.
        with pytest.raises(ValueError, match="sender signature failed"):
            nomnom.feed_open(feed_key=feed_key, feed_id="other-feed", blob=blob)

    def test_open_rejects_body_tamper(self):
        feed_key = nomnom._feed_key_from_token("k4n2pX9qLm3T")
        sig_priv, sig_pub = self._seed_and_pub()
        blob = nomnom.feed_seal(
            feed_key=feed_key,
            feed_id="k4n2pX9qLm3T",
            sender_member_id="mem-abc",
            sender_sig_priv_hex=sig_priv,
            sender_sig_pub_hex=sig_pub,
            filename="hello.txt",
            body=b"data",
        )
        tampered = bytearray(blob)
        tampered[-1] ^= 0xFF
        with pytest.raises(ValueError, match="authentication failed"):
            nomnom.feed_open(
                feed_key=feed_key, feed_id="k4n2pX9qLm3T", blob=bytes(tampered),
            )

    def test_open_rejects_truncated(self):
        feed_key = nomnom._feed_key_from_token("k4n2pX9qLm3T")
        with pytest.raises(ValueError, match="too short"):
            nomnom.feed_open(
                feed_key=feed_key, feed_id="k4n2pX9qLm3T", blob=b"\x00" * 5,
            )

    def test_open_with_expected_member_id_match(self):
        feed_key = nomnom._feed_key_from_token("k4n2pX9qLm3T")
        sig_priv, sig_pub = self._seed_and_pub()
        blob = nomnom.feed_seal(
            feed_key=feed_key,
            feed_id="k4n2pX9qLm3T",
            sender_member_id="mem-abc",
            sender_sig_priv_hex=sig_priv,
            sender_sig_pub_hex=sig_pub,
            filename="hello.txt",
            body=b"data",
        )
        nomnom.feed_open(
            feed_key=feed_key, feed_id="k4n2pX9qLm3T", blob=blob,
            expect_member_id="mem-abc",
        )
        with pytest.raises(ValueError, match="sender_member_id mismatch"):
            nomnom.feed_open(
                feed_key=feed_key, feed_id="k4n2pX9qLm3T", blob=blob,
                expect_member_id="mem-other",
            )


class TestIdentitySigKeys:
    def test_load_identity_adds_sig_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        ident = nomnom._load_identity()
        assert "sig_priv" in ident
        assert "sig_pub" in ident
        assert len(bytes.fromhex(ident["sig_priv"])) == 32
        assert len(bytes.fromhex(ident["sig_pub"])) == 32
        # Pub key is the deterministic Ed25519 derivation of the priv seed.
        derived = nomnom.ed25519_pub_from_seed(bytes.fromhex(ident["sig_priv"]))
        assert derived.hex() == ident["sig_pub"]

    def test_load_identity_preserves_sig_keys_across_calls(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        first = nomnom._load_identity()
        second = nomnom._load_identity()
        assert first["sig_priv"] == second["sig_priv"]
        assert first["sig_pub"] == second["sig_pub"]


# ---------- feeds.json store ----------


def _make_feed(name: str = "home", feed_id: str = "k4n2pX9qLm3T") -> nomnom.Feed:
    return nomnom.Feed(
        name=name,
        feed_id=feed_id,
        feed_token=feed_id,
        url=f"https://relay.example.com/f/{feed_id}",
        expires_at=2_000_000_000,
        joined_at=1_700_000_000,
        member_id="a" * 32,
    )


class TestFeedDataclass:
    def test_to_from_dict_roundtrip(self):
        f = _make_feed()
        d = f.to_dict()
        g = nomnom.Feed.from_dict(d)
        assert g == f

    def test_from_dict_rejects_missing_field(self):
        d = _make_feed().to_dict()
        del d["url"]
        with pytest.raises(ValueError, match="missing/invalid"):
            nomnom.Feed.from_dict(d)

    def test_from_dict_rejects_non_dict(self):
        with pytest.raises(ValueError, match="not a JSON object"):
            nomnom.Feed.from_dict("not-a-dict")

    def test_feed_token_defaults_to_feed_id(self):
        d = _make_feed().to_dict()
        del d["feed_token"]
        g = nomnom.Feed.from_dict(d)
        assert g.feed_token == g.feed_id


class TestFeedsConfig:
    def test_load_empty_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        cfg = nomnom._load_feeds_config()
        assert cfg["default"] is None
        assert cfg["feeds"] == []

    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, _make_feed())
        nomnom._save_feeds_config(cfg)
        loaded = nomnom._load_feeds_config()
        assert loaded["default"] == "home"
        assert len(loaded["feeds"]) == 1
        assert loaded["feeds"][0].name == "home"
        assert loaded["feeds"][0].feed_id == "k4n2pX9qLm3T"

    def test_first_feed_becomes_default(self):
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, _make_feed(name="home"))
        assert cfg["default"] == "home"

    def test_second_feed_does_not_steal_default(self):
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, _make_feed(name="home"))
        nomnom._add_or_replace_feed(
            cfg, _make_feed(name="work", feed_id="abcDEFghi123"),
        )
        assert cfg["default"] == "home"

    def test_replace_existing_by_name(self):
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, _make_feed(name="home"))
        replacement = _make_feed(name="home", feed_id="zzzz0000xxxx")
        nomnom._add_or_replace_feed(cfg, replacement)
        assert len(cfg["feeds"]) == 1
        assert cfg["feeds"][0].feed_id == "zzzz0000xxxx"

    def test_load_drops_default_pointing_at_missing_feed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        path = nomnom._feeds_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 1,
            "default": "nonexistent",
            "feeds": [_make_feed(name="home").to_dict()],
        }))
        cfg = nomnom._load_feeds_config()
        assert cfg["default"] is None

    def test_load_rejects_wrong_schema_version(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        path = nomnom._feeds_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 999, "feeds": []}))
        cfg = nomnom._load_feeds_config()
        assert cfg["default"] is None
        assert cfg["feeds"] == []

    def test_load_skips_corrupt_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        path = nomnom._feeds_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 1,
            "default": None,
            "feeds": [
                {"name": "broken"},
                _make_feed(name="ok").to_dict(),
            ],
        }))
        cfg = nomnom._load_feeds_config()
        assert len(cfg["feeds"]) == 1
        assert cfg["feeds"][0].name == "ok"

    def test_find_feed_match(self):
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, _make_feed(name="home"))
        f = nomnom._find_feed(cfg, "home")
        assert f is not None
        assert f.name == "home"

    def test_find_feed_miss(self):
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, _make_feed(name="home"))
        assert nomnom._find_feed(cfg, "work") is None

    def test_default_feed_returns_dataclass(self):
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, _make_feed(name="home"))
        d = nomnom._default_feed(cfg)
        assert isinstance(d, nomnom.Feed)
        assert d.name == "home"


class TestFeedNicknameValidation:
    @pytest.mark.parametrize("name", ["home", "feed-1", "a", "abc-def-ghi"])
    def test_accepts_valid(self, name):
        nomnom._validate_feed_nickname(name)  # must not raise

    @pytest.mark.parametrize(
        "name",
        ["", "Home", "feed_1", "-leading", "feed!", "ÜberFeed", "x" * 33],
    )
    def test_rejects_invalid(self, name):
        with pytest.raises(nomnom.NomnomError):
            nomnom._validate_feed_nickname(name)


class TestAutogenFeedNickname:
    def test_first_nickname_is_feed_1(self):
        assert nomnom._autogen_feed_nickname([]) == "feed-1"

    def test_skips_taken_names(self):
        feeds = [
            _make_feed(name="feed-1"),
            _make_feed(name="feed-2", feed_id="aaaaaaaa1234"),
        ]
        assert nomnom._autogen_feed_nickname(feeds) == "feed-3"


class TestCmdOpen:
    @pytest.fixture
    def fake_relay_and_identity(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_relay_config(
            "https://relay.example.com", "test-secret", allow_private=True,
        )
        captured: dict = {}

        def fake_mint(relay, *, ttl_seconds, member_card):
            captured["relay"] = relay
            captured["ttl"] = ttl_seconds
            captured["card"] = member_card
            return {
                "feed_id": "abcDEF12_-xy",
                "expires_at": 1_700_000_000 + ttl_seconds,
                "created_at": 1_700_000_000,
            }

        monkeypatch.setattr(nomnom, "_relay_mint_feed", fake_mint)
        yield captured

    def test_open_mints_feed_and_writes_config(
        self, fake_relay_and_identity, tmp_path, capsys,
    ):
        args = argparse.Namespace(name=None, ttl=3600, default=False)
        rc = nomnom.cmd_open(args)
        assert rc == 0
        cfg = nomnom._load_feeds_config()
        assert len(cfg["feeds"]) == 1
        feed = cfg["feeds"][0]
        assert feed.feed_id == "abcDEF12_-xy"
        assert feed.url == "https://relay.example.com/f/abcDEF12_-xy"
        assert cfg["default"] == feed.name  # first feed auto-defaults
        out = capsys.readouterr()
        assert "https://relay.example.com/f/abcDEF12_-xy" in out.out
        # Member card contains the identity sig pubkey, not the DH key.
        ident = nomnom._load_identity()
        assert fake_relay_and_identity["card"]["identity_pubkey"] == ident["sig_pub"]

    def test_open_respects_explicit_name(
        self, fake_relay_and_identity, capsys,
    ):
        args = argparse.Namespace(name="standup", ttl=3600, default=False)
        rc = nomnom.cmd_open(args)
        assert rc == 0
        cfg = nomnom._load_feeds_config()
        assert cfg["feeds"][0].name == "standup"

    def test_open_rejects_duplicate_name(
        self, fake_relay_and_identity, capsys,
    ):
        nomnom.cmd_open(argparse.Namespace(name="alpha", ttl=3600, default=False))
        rc = nomnom.cmd_open(
            argparse.Namespace(name="alpha", ttl=3600, default=False),
        )
        assert rc == 1
        assert "already exists" in capsys.readouterr().err

    def test_open_default_flag(self, fake_relay_and_identity, capsys):
        nomnom.cmd_open(argparse.Namespace(name="first", ttl=3600, default=False))
        # Need a new fake feed_id for the second mint.
        nomnom._relay_mint_feed = lambda relay, *, ttl_seconds, member_card: {  # type: ignore
            "feed_id": "ZZZ987654321",
            "expires_at": 1_700_000_000 + ttl_seconds,
            "created_at": 1_700_000_000,
        }
        nomnom.cmd_open(
            argparse.Namespace(name="second", ttl=3600, default=True),
        )
        cfg = nomnom._load_feeds_config()
        assert cfg["default"] == "second"

    def test_open_aborts_when_no_relay(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        # Refuse the interactive prompt: simulate no TTY.
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        rc = nomnom.cmd_open(
            argparse.Namespace(name=None, ttl=3600, default=False),
        )
        assert rc == 1


class TestCmdJoin:
    @pytest.fixture
    def fake_feed_endpoints(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        roster: list[dict] = []

        def fake_meta(host, feed_id, feed_key):
            return {"expires_at": 1_800_000_000, "created_at": 1_700_000_000}

        def fake_put_member(host, feed_id, feed_key, member_id, card):
            roster.append({**card, "joined_at": int(time.time())})

        def fake_list_members(host, feed_id, feed_key, *, since_ts=0, wait_ms=0):
            return {"members": list(roster)}

        monkeypatch.setattr(nomnom, "_relay_get_feed_meta", fake_meta)
        monkeypatch.setattr(nomnom, "_relay_put_member", fake_put_member)
        monkeypatch.setattr(nomnom, "_relay_list_members", fake_list_members)
        return roster

    def test_join_saves_feed_with_member_card(
        self, fake_feed_endpoints, capsys,
    ):
        url = "https://relay.example.com/f/abcDEF12_-xy"
        rc = nomnom.cmd_join(argparse.Namespace(url=url, name=None, default=False))
        assert rc == 0
        cfg = nomnom._load_feeds_config()
        assert len(cfg["feeds"]) == 1
        feed = cfg["feeds"][0]
        assert feed.url == url
        assert feed.feed_id == "abcDEF12_-xy"
        assert feed.member_id  # was published
        assert len(feed.members_cache) == 1

    def test_join_rejects_malformed_url(self, fake_feed_endpoints, capsys):
        rc = nomnom.cmd_join(
            argparse.Namespace(url="not-a-url", name=None, default=False),
        )
        assert rc == 1


class TestCmdFeedsList:
    def test_lists_default_marker(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, _make_feed(name="home"))
        nomnom._add_or_replace_feed(
            cfg, _make_feed(name="work", feed_id="abcDEF99_-zy"),
        )
        nomnom._save_feeds_config(cfg)
        rc = nomnom.cmd_feeds(argparse.Namespace(action="list"))
        assert rc == 0
        out = capsys.readouterr().out
        # default marker on the first feed
        assert " * home" in out
        assert "   work" in out

    def test_empty_message(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        rc = nomnom.cmd_feeds(argparse.Namespace(action="list"))
        assert rc == 0
        assert "no feeds" in capsys.readouterr().err


class TestCmdFeedsActions:
    @pytest.fixture
    def feed_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        cfg = nomnom._empty_feeds_config()
        nomnom._add_or_replace_feed(cfg, _make_feed(name="home"))
        nomnom._save_feeds_config(cfg)
        return tmp_path

    def test_url_prints_feed_url(self, feed_config, capsys):
        rc = nomnom.cmd_feeds(argparse.Namespace(action="url", name="home"))
        assert rc == 0
        assert "https://relay.example.com/f/" in capsys.readouterr().out

    def test_url_missing_feed(self, feed_config, capsys):
        rc = nomnom.cmd_feeds(argparse.Namespace(action="url", name="missing"))
        assert rc == 1

    def test_default_updates_default(self, feed_config, capsys):
        # Add a second feed first.
        cfg = nomnom._load_feeds_config()
        nomnom._add_or_replace_feed(
            cfg, _make_feed(name="work", feed_id="abcDEF99_-zy"),
        )
        nomnom._save_feeds_config(cfg)
        rc = nomnom.cmd_feeds(argparse.Namespace(action="default", name="work"))
        assert rc == 0
        assert nomnom._load_feeds_config()["default"] == "work"

    def test_rename_updates_name(self, feed_config, capsys):
        rc = nomnom.cmd_feeds(argparse.Namespace(
            action="rename", name="home", new_name="house",
        ))
        assert rc == 0
        cfg = nomnom._load_feeds_config()
        assert cfg["feeds"][0].name == "house"
        assert cfg["default"] == "house"  # default tracked the rename

    def test_rename_rejects_existing_target(self, feed_config, capsys):
        cfg = nomnom._load_feeds_config()
        nomnom._add_or_replace_feed(
            cfg, _make_feed(name="work", feed_id="abcDEF99_-zy"),
        )
        nomnom._save_feeds_config(cfg)
        rc = nomnom.cmd_feeds(argparse.Namespace(
            action="rename", name="home", new_name="work",
        ))
        assert rc == 1

    def test_leave_removes_feed_and_calls_relay(
        self, feed_config, monkeypatch, capsys,
    ):
        called = {}
        monkeypatch.setattr(
            nomnom, "_relay_delete_member",
            lambda host, fid, fkey, mid: called.setdefault("ok", True),
        )
        rc = nomnom.cmd_feeds(argparse.Namespace(action="leave", name="home"))
        assert rc == 0
        cfg = nomnom._load_feeds_config()
        assert cfg["feeds"] == []
        assert cfg["default"] is None
        assert called.get("ok") is True

    def test_extend_calls_relay_and_updates_local(
        self, feed_config, monkeypatch, capsys,
    ):
        monkeypatch.setattr(
            nomnom, "_relay_extend_feed",
            lambda host, fid, fkey, ttl: {"expires_at": 3_000_000_000},
        )
        rc = nomnom.cmd_feeds(argparse.Namespace(
            action="extend", name="home", ttl=7200,
        ))
        assert rc == 0
        cfg = nomnom._load_feeds_config()
        assert cfg["feeds"][0].expires_at == 3_000_000_000

    def test_extend_rejects_short_ttl(self, feed_config, capsys):
        rc = nomnom.cmd_feeds(argparse.Namespace(
            action="extend", name="home", ttl=30,
        ))
        assert rc == 1


class TestFeedUrl:
    def test_format_with_bare_host(self):
        url = nomnom._format_feed_url("relay.example.com", "abc12345")
        assert url == "https://relay.example.com/f/abc12345"

    def test_format_with_scheme(self):
        url = nomnom._format_feed_url("https://relay.example.com/", "abc12345")
        assert url == "https://relay.example.com/f/abc12345"

    def test_parse_https(self):
        host, token = nomnom._parse_feed_url("https://relay.example.com/f/abc12345")
        assert host == "relay.example.com"
        assert token == "abc12345"

    def test_parse_without_scheme(self):
        host, token = nomnom._parse_feed_url("relay.example.com/f/abc12345")
        assert host == "relay.example.com"
        assert token == "abc12345"

    def test_parse_rejects_missing_f_path(self):
        with pytest.raises(nomnom.NomnomError, match="/f/"):
            nomnom._parse_feed_url("https://relay.example.com/")

    def test_parse_rejects_empty(self):
        with pytest.raises(nomnom.NomnomError, match="empty"):
            nomnom._parse_feed_url("")

    def test_parse_rejects_bad_token(self):
        with pytest.raises(nomnom.NomnomError, match="url-safe base64"):
            nomnom._parse_feed_url("https://relay.example.com/f/has spaces")

    def test_parse_rejects_extra_path(self):
        with pytest.raises(nomnom.NomnomError, match="malformed"):
            nomnom._parse_feed_url("https://relay.example.com/f/abc/extra")

