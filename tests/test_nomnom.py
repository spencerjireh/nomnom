"""Tests for nomnom.py.

Covers the pure-logic surface: gitignore matching, repo scanning,
tree model, selection cascade, output rendering, and the destination/
summary/footer helpers extracted from the picker. The curses `pick`
loop itself is excluded — it needs a TTY.
"""

from __future__ import annotations

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

    def test_stdout_to_send(self):
        assert nomnom.cycle_destination(nomnom.Destination.STDOUT) == nomnom.Destination.SEND

    def test_send_wraps_to_file(self):
        assert nomnom.cycle_destination(nomnom.Destination.SEND) == nomnom.Destination.FILE

    def test_allowed_subset_cycles_without_stdout(self):
        allowed = (nomnom.Destination.FILE, nomnom.Destination.CLIPBOARD,
                   nomnom.Destination.SEND)
        # FILE → CLIPBOARD → SEND → FILE; STDOUT is never hit.
        d = nomnom.Destination.FILE
        for expected in (nomnom.Destination.CLIPBOARD, nomnom.Destination.SEND,
                         nomnom.Destination.FILE):
            d = nomnom.cycle_destination(d, allowed)
            assert d == expected

    def test_allowed_snaps_to_first_when_current_not_allowed(self):
        allowed = (nomnom.Destination.FILE, nomnom.Destination.CLIPBOARD)
        # STDOUT is not in allowed → snap to the first allowed entry.
        assert nomnom.cycle_destination(nomnom.Destination.STDOUT, allowed) \
            == nomnom.Destination.FILE


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
        # Wide enough for "selected: 2 files" + gap + "dest: file  tree: on"
        # but not the size/token block in between.
        narrow = nomnom.format_footer(nomnom.Destination.FILE, True, (2, 1000, 250), 50)
        assert "selected: 2 files" in narrow
        assert "dest: file" in narrow
        assert len(narrow) <= 50

    def test_wide_width_includes_everything(self):
        out = nomnom.format_footer(nomnom.Destination.FILE, True, (5, 4096, 1024), 120)
        assert "selected: 5 files" in out
        assert "dest: file" in out
        assert "tree: on" in out
        assert len(out) <= 120


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
        for v in ("Bundle", "Commit", "PR", "Review", "Rebuild",
                  "Send", "Receive", "Extensions", "Pins"):
            assert v in labels

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

    def test_known_text_extension_present(self):
        s = nomnom.ExtensionsScreen()
        text_section = next(e for label, e in s.sections if label == "Text extensions")
        assert ".py" in text_section

    def test_q_returns_back(self):
        s = nomnom.ExtensionsScreen()
        assert s.handle_key(ord("q")) == nomnom.ScreenAction.BACK


class TestPinsScreen:
    def test_empty_when_no_peers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        s = nomnom.PinsScreen()
        assert s.peers == []
        assert s.handle_key(ord("q")) == nomnom.ScreenAction.BACK

    def test_drop_removes_pin(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_known_peer("dev-1", "alice", "aa")
        nomnom._save_known_peer("dev-2", "bob", "bb")
        s = nomnom.PinsScreen()
        assert len(s.peers) == 2
        s.handle_key(ord("d"))
        assert len(s.peers) == 1
        assert "dropped" in s.message

    def test_other_keys_are_inert(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_known_peer("dev-1", "alice", "aa")
        s = nomnom.PinsScreen()
        assert s.handle_key(ord("x")) == nomnom.ScreenAction.CONTINUE


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
        assert s.destination == nomnom.Destination.SEND
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


class TestReviewScreen:
    def test_fields_include_pr_and_diff(self):
        s = nomnom.ReviewScreen()
        field_ids = [fid for fid, _ in s.fields]
        assert field_ids == ["repo", "pr", "diff", "dest"]

    def test_pr_number_accepts_only_digits(self):
        s = nomnom.ReviewScreen()
        s.field_cursor = 1  # focus PR number
        s.handle_key(ord("1"))
        s.handle_key(ord("2"))
        s.handle_key(ord("x"))  # non-digit, ignored
        s.handle_key(ord("3"))
        assert s.pr_buf == "123"

    def test_diff_toggled_by_space_when_focused(self):
        s = nomnom.ReviewScreen()
        s.field_cursor = 2  # focus diff
        assert s.include_diff is False
        s.handle_key(ord(" "))
        assert s.include_diff is True
        s.handle_key(ord(" "))
        assert s.include_diff is False

    def test_run_errors_on_missing_pr_number(self, monkeypatch):
        monkeypatch.setattr(nomnom, "cmd_review",
                            lambda *a, **k: 0)
        s = nomnom.ReviewScreen()
        s.repo_buf = "/tmp/r"
        s.pr_buf = ""
        s.handle_key(10)
        assert s.rc == 1
        assert "required" in s.error.lower()

    def test_run_passes_args(self, monkeypatch):
        called: dict = {}

        def fake_cmd_review(repo, pr_number, include_diff, *, destination):
            called["pr"] = pr_number
            called["diff"] = include_diff
            called["dest"] = destination
            return 0
        monkeypatch.setattr(nomnom, "cmd_review", fake_cmd_review)
        s = nomnom.ReviewScreen()
        s.repo_buf = "/tmp/r"
        s.pr_buf = "42"
        s.include_diff = True
        s.destination = nomnom.Destination.SEND
        s.handle_key(10)
        assert called == {"pr": 42, "diff": True,
                          "dest": nomnom.Destination.SEND}


class TestEmitGitBundleSend:
    def test_send_destination_calls_lan_send(self, monkeypatch):
        sent: dict = {}

        def fake_send(name, data):
            sent["name"] = name
            sent["bytes"] = len(data)
            return 0
        monkeypatch.setattr(nomnom, "_lan_send_bytes", fake_send)
        rc = nomnom._emit_git_bundle(
            "r", "commit", "main", [("git_status", "ok\n")], None,
            nomnom.Destination.SEND,
        )
        assert rc == 0
        assert sent["name"].startswith("r-main-commit-")
        assert sent["name"].endswith(".txt")
        assert sent["bytes"] > 0


class TestNomnomError:
    def test_require_git_repo_raises_on_non_repo(self, tmp_path):
        # tmp_path is not a git repo.
        with pytest.raises(nomnom.NomnomError):
            nomnom._require_git_repo(tmp_path)

    def test_require_gh_raises_when_missing(self, monkeypatch):
        monkeypatch.setattr(nomnom.shutil, "which", lambda _x: None)
        with pytest.raises(nomnom.NomnomError):
            nomnom._require_gh()


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

    def test_no_changes_errors(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "clean"
        make_git_repo(repo, initial={"a.txt": "hi\n"})
        monkeypatch.chdir(tmp_path)
        rc = nomnom.cmd_commit(str(repo))
        assert rc == 1
        err = capsys.readouterr().err
        assert "nothing to commit" in err

    def test_untracked_only_errors(self, tmp_path, monkeypatch, capsys):
        # Per spec: untracked-only doesn't qualify as "something to commit".
        repo = tmp_path / "ut"
        make_git_repo(repo, initial={"a.txt": "hi\n"})
        (repo / "new.txt").write_text("new\n")
        monkeypatch.chdir(tmp_path)
        rc = nomnom.cmd_commit(str(repo))
        assert rc == 1

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

    def test_detached_head_errors(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        (repo / "a.txt").write_text("2\n")
        _git(["add", "a.txt"], repo)
        _git(["commit", "-q", "-m", "second"], repo)
        _git(["checkout", "-q", "--detach", "HEAD"], repo)

        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        monkeypatch.chdir(tmp_path)
        rc = nomnom.cmd_pr(str(repo), base="main")
        assert rc == 1
        assert "detached" in capsys.readouterr().err

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


# ---------- cmd_review ----------

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


class TestReviewBundle:
    def test_missing_gh_errors(self, tmp_path, monkeypatch):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(
            nomnom.shutil, "which",
            lambda name: None if name == "gh" else "/bin/git",
        )
        monkeypatch.chdir(tmp_path)
        with pytest.raises(nomnom.NomnomError, match="requires gh"):
            nomnom.cmd_review(
                str(repo), pr_number=1, include_diff=False,
            )

    def test_invalid_pr_number_errors(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: f"/usr/bin/{name}")
        monkeypatch.chdir(tmp_path)
        rc = nomnom.cmd_review(
            str(repo), pr_number=0, include_diff=False,
        )
        assert rc == 1
        assert "must be positive" in capsys.readouterr().err

    def test_pr_not_found_surfaces_gh_stderr(
        self, tmp_path, monkeypatch, capsys,
    ):
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
        rc = nomnom.cmd_review(
            str(repo), pr_number=42, include_diff=False,
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "no pull request" in err
        assert "#42" in err

    def test_repo_view_failure_surfaces_error(
        self, tmp_path, monkeypatch, capsys,
    ):
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
        rc = nomnom.cmd_review(
            str(repo), pr_number=1, include_diff=False,
        )
        assert rc == 1
        assert "no remote configured" in capsys.readouterr().err

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
        rc = nomnom.cmd_review(
            str(repo), pr_number=7, include_diff=False,
        )
        assert rc == 0
        bundles = list(out_dir.glob("p-pr-7-review-*.txt"))
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
        rc = nomnom.cmd_review(
            str(repo), pr_number=99, include_diff=False,
        )
        assert rc == 0
        text = next(out_dir.glob("p-pr-99-review-*.txt")).read_text()
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
        nomnom.cmd_review(
            str(repo), pr_number=5, include_diff=False,
        )
        text = next(out_dir.glob("p-pr-5-review-*.txt")).read_text()
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
        nomnom.cmd_review(
            str(repo), pr_number=8, include_diff=False,
        )
        text = next(out_dir.glob("p-pr-8-review-*.txt")).read_text()
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
        nomnom.cmd_review(
            str(repo), pr_number=1, include_diff=False,
        )
        assert diff_called == []
        text = next(out_dir.glob("p-pr-1-review-*.txt")).read_text()
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
        nomnom.cmd_review(
            str(repo), pr_number=2, include_diff=True,
        )
        assert len(diff_called) == 1
        text = next(out_dir.glob("p-pr-2-review-*.txt")).read_text()
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
        rc = nomnom.cmd_review(
            str(repo), pr_number=3, include_diff=False,
            destination=nomnom.Destination.CLIPBOARD,
        )
        assert rc == 0
        assert list(out_dir.glob("p-pr-3-review-*.txt")) == []
        assert len(captured) == 1
        assert '<section name="pr_meta">' in captured[0]


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


class TestEncryptBytes:
    def test_round_trip_random(self):
        data = bytes(range(256)) * 5
        blob = nomnom.encrypt_bytes(data, "thing.bin", "pw")
        name, body = nomnom.decrypt_bytes(blob, "pw")
        assert name == "thing.bin"
        assert body == data

    def test_empty_body(self):
        blob = nomnom.encrypt_bytes(b"", "empty.txt", "pw")
        assert nomnom.decrypt_bytes(blob, "pw") == ("empty.txt", b"")

    def test_two_encrypts_differ(self):
        # Fresh salt + nonce per call means two encrypts of the same input
        # must produce different ciphertexts.
        a = nomnom.encrypt_bytes(b"same", "n", "pw")
        b = nomnom.encrypt_bytes(b"same", "n", "pw")
        assert a != b

    def test_deterministic_with_pinned_salt_and_nonce(self):
        a = nomnom.encrypt_bytes(
            b"same", "n", "pw",
            _salt=b"\x00" * 16, _nonce=b"\x01" * 12,
        )
        b = nomnom.encrypt_bytes(
            b"same", "n", "pw",
            _salt=b"\x00" * 16, _nonce=b"\x01" * 12,
        )
        assert a == b

    def test_wrong_passphrase_fails_auth(self):
        blob = nomnom.encrypt_bytes(b"secret", "f.txt", "right")
        with pytest.raises(ValueError, match="authentication failed"):
            nomnom.decrypt_bytes(blob, "wrong")

    def test_tampered_ciphertext_fails_auth(self):
        blob = bytearray(nomnom.encrypt_bytes(b"secret bytes", "f.txt", "pw"))
        # Flip a bit deep in the ciphertext region (well past the 65-byte header).
        blob[100] ^= 0x01
        with pytest.raises(ValueError, match="authentication failed"):
            nomnom.decrypt_bytes(bytes(blob), "pw")

    def test_tampered_mac_fails_auth(self):
        blob = bytearray(nomnom.encrypt_bytes(b"secret", "f.txt", "pw"))
        # MAC sits at offset 33..65.
        blob[40] ^= 0x80
        with pytest.raises(ValueError, match="authentication failed"):
            nomnom.decrypt_bytes(bytes(blob), "pw")

    def test_truncated_blob_rejected(self):
        blob = nomnom.encrypt_bytes(b"x", "f.txt", "pw")
        with pytest.raises(ValueError, match="too short"):
            nomnom.decrypt_bytes(blob[:30], "pw")

    def test_wrong_magic_rejected(self):
        blob = nomnom.encrypt_bytes(b"x", "f.txt", "pw")
        bad = b"WRONG" + blob[5:]
        with pytest.raises(ValueError, match="bad magic"):
            nomnom.decrypt_bytes(bad, "pw")

    def test_empty_passphrase_rejected(self):
        with pytest.raises(ValueError, match="passphrase"):
            nomnom.encrypt_bytes(b"x", "f.txt", "")


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


class TestIdentityKnownPeers:
    def test_identity_created_stable_and_keyed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        i1 = nomnom._load_identity()
        i2 = nomnom._load_identity()
        assert i1["device_id"] and i1["name"]
        assert i1["device_id"] == i2["device_id"]
        # long-term identity keypair present, stable, and a valid DH pair
        assert i1["ik_priv"] == i2["ik_priv"] and i1["ik_pub"] == i2["ik_pub"]
        priv, pub = int(i1["ik_priv"], 16), int(i1["ik_pub"], 16)
        assert pow(nomnom._DH_G, priv, nomnom._DH_P) == pub

    def test_legacy_identity_gets_keypair(self, tmp_path, monkeypatch):
        # An identity.json from before TOFU (no ik) is upgraded in place.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        cfg = tmp_path / "nomnom"
        cfg.mkdir()
        (cfg / "identity.json").write_text(json.dumps(
            {"device_id": "old", "name": "legacy"}))
        ident = nomnom._load_identity()
        assert ident["device_id"] == "old" and ident["name"] == "legacy"
        assert ident.get("ik_pub") and ident.get("ik_priv")
        assert json.loads((cfg / "identity.json").read_text()).get("ik_pub")

    def test_known_peer_pin_lookup_and_forget(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_known_peer("abc", "box", "deadbeef")
        assert nomnom._known_peer_ik("abc") == "deadbeef"
        assert nomnom._known_peer_ik("missing") is None
        assert nomnom._load_known_peers()["abc"]["name"] == "box"
        # re-pinning a new key keeps first_seen
        first = nomnom._load_known_peers()["abc"]["first_seen"]
        nomnom._save_known_peer("abc", "box", "feedface")
        assert nomnom._known_peer_ik("abc") == "feedface"
        assert nomnom._load_known_peers()["abc"]["first_seen"] == first
        # forget by name drops it
        assert nomnom._forget_peer("box") == ["box"]
        assert nomnom._known_peer_ik("abc") is None
        assert nomnom._forget_peer("nope") == []


class TestSessionCrypto:
    def test_dh_prime_is_2048_bit(self):
        assert nomnom._DH_P.bit_length() == 2048
        assert nomnom._DH_G == 2

    def test_dh_roundtrip(self):
        a_priv, a_pub = nomnom._dh_keypair()
        b_priv, b_pub = nomnom._dh_keypair()
        assert nomnom._dh_shared(a_priv, b_pub) == nomnom._dh_shared(b_priv, a_pub)

    def test_dh_rejects_bad_public(self):
        with pytest.raises(ValueError):
            nomnom._dh_shared(123, 1)

    def test_triple_dh_both_sides_agree(self):
        ik_i_priv, ik_i_pub = nomnom._dh_keypair()
        ek_i_priv, ek_i_pub = nomnom._dh_keypair()
        ik_r_priv, ik_r_pub = nomnom._dh_keypair()
        ek_r_priv, ek_r_pub = nomnom._dh_keypair()
        ki = nomnom._session_key_initiator(ik_i_priv, ek_i_priv, ik_i_pub,
                                           ek_i_pub, ik_r_pub, ek_r_pub)
        kr = nomnom._session_key_responder(ik_r_priv, ek_r_priv, ik_r_pub,
                                           ek_r_pub, ik_i_pub, ek_i_pub)
        assert ki == kr and len(ki) == 32

    def test_mitm_identity_swap_diverges(self):
        # An attacker who lacks the pinned identity private key cannot land on
        # the same session key: swapping the initiator identity changes it.
        ik_i_priv, ik_i_pub = nomnom._dh_keypair()
        ek_i_priv, ek_i_pub = nomnom._dh_keypair()
        ik_r_priv, ik_r_pub = nomnom._dh_keypair()
        ek_r_priv, ek_r_pub = nomnom._dh_keypair()
        good = nomnom._session_key_initiator(ik_i_priv, ek_i_priv, ik_i_pub,
                                             ek_i_pub, ik_r_pub, ek_r_pub)
        _m_priv, m_pub = nomnom._dh_keypair()
        bad = nomnom._session_key_responder(ik_r_priv, ek_r_priv, ik_r_pub,
                                            ek_r_pub, m_pub, ek_i_pub)
        assert good != bad

    def test_forward_secrecy_fresh_key_per_transfer(self):
        # Two transfers with the same identities still derive different keys,
        # because each side mints a throwaway ephemeral.
        ident = _mk_ident("J", "join")
        peer = {"ik": _mk_ident("H", "host")["ik_pub"],
                "ek": format(nomnom._dh_keypair()[1], "x")}
        k1, ek1 = nomnom._joiner_session(ident, peer)
        k2, ek2 = nomnom._joiner_session(ident, peer)
        assert ek1 != ek2 and k1 != k2

    def test_joiner_session_rejects_bad_peer_key(self):
        ident = _mk_ident("J", "join")
        with pytest.raises(ValueError):
            nomnom._joiner_session(ident, {"ik": "01", "ek": "01"})


class TestTofu:
    def test_decision_new_match_changed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert nomnom._tofu_decision("X", "aa") == "new"
        nomnom._save_known_peer("X", "box", "aa")
        assert nomnom._tofu_decision("X", "aa") == "match"
        assert nomnom._tofu_decision("X", "bb") == "changed"

    def test_fingerprint_stable_and_grouped(self):
        h = format(nomnom._dh_keypair()[1], "x")
        fp = nomnom._ik_fingerprint(h)
        assert fp == nomnom._ik_fingerprint(h)
        assert fp.count(":") == 3 and len(fp) == 19

    def test_fingerprint_handles_odd_length_hex(self):
        # `format(pub, "x")` can be odd-length when the top nibble is zero;
        # the fingerprint must still decode it rather than returning "?".
        assert nomnom._ik_fingerprint("abc").count(":") == 3
        assert nomnom._ik_fingerprint("0abc") == nomnom._ik_fingerprint("abc")
        assert nomnom._ik_fingerprint("nothex!!") == "?"

    def test_check_join_prompts_on_change(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_known_peer("H", "host", "aa")
        peer = {"id": "H", "name": "host", "ik": "bb"}
        monkeypatch.setattr("builtins.input", lambda *a: "n")
        assert nomnom._tofu_check_join(peer) is False
        monkeypatch.setattr("builtins.input", lambda *a: "y")
        assert nomnom._tofu_check_join(peer) is True

    def test_check_join_silent_when_known(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_known_peer("H", "host", "aa")

        def boom(*a):
            raise AssertionError("should not prompt for a matching key")
        monkeypatch.setattr("builtins.input", boom)
        assert nomnom._tofu_check_join({"id": "H", "name": "host", "ik": "aa"})

    def test_trust_new_auto_accepts_changed_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_known_peer("H", "host", "aa")
        peer = {"id": "H", "name": "host", "ik": "bb"}

        def boom(*a):
            raise AssertionError("trust_new should bypass the prompt")
        monkeypatch.setattr("builtins.input", boom)
        assert nomnom._tofu_check_join(peer, trust_new=True) is True


class TestPeerFilter:
    def _peer(self, **kw):
        base = {"id": "abc123def456", "name": "alice-mbp", "ip": "10.0.0.2",
                "port": 1, "role": "send", "tok": "t", "ik": "aa", "ek": "bb"}
        base.update(kw)
        return base

    def test_matches_by_name(self):
        assert nomnom._peer_matches(self._peer(), "alice-mbp")
        assert nomnom._peer_matches(self._peer(), "ALICE-MBP")

    def test_matches_by_device_id_prefix(self):
        assert nomnom._peer_matches(self._peer(), "abc123")
        assert nomnom._peer_matches(self._peer(), "abc123def456")

    def test_no_match_returns_false(self):
        assert not nomnom._peer_matches(self._peer(), "bob")

    def test_choose_with_filter_skips_prompt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        peers = [self._peer(), self._peer(id="zzz", name="bob")]

        def boom(*a):
            raise AssertionError("peer_filter should skip the prompt")
        monkeypatch.setattr("builtins.input", boom)
        picked = nomnom._lan_choose(peers, "receiver(s)", peer_filter="bob")
        assert picked["name"] == "bob"

    def test_choose_filter_no_match_errors(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        peers = [self._peer()]
        picked = nomnom._lan_choose(peers, "receiver(s)", peer_filter="nobody")
        assert picked is None
        assert "matched no" in capsys.readouterr().err

    def test_choose_filter_ambiguous_errors(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        peers = [self._peer(), self._peer(id="abc999")]  # both prefix abc
        picked = nomnom._lan_choose(peers, "receiver(s)", peer_filter="abc")
        assert picked is None
        assert "ambiguous" in capsys.readouterr().err


class TestLanBeacon:
    def test_roundtrip(self):
        pkt = nomnom._lan_encode_beacon("id1", "box", "10.0.0.1", 5000,
                                        "send", "tk", "abcd", "ef01")
        assert nomnom._lan_decode_beacon(pkt) == {
            "id": "id1", "name": "box", "ip": "10.0.0.1", "port": 5000,
            "role": "send", "tok": "tk", "ik": "abcd", "ek": "ef01"}

    @pytest.mark.parametrize("pkt", [
        b"junk",
        b"NMNMLAN2{bad json",
        b"NMNMLAN2" + json.dumps({"id": "x"}).encode(),
        # role "pair" is no longer valid in the TOFU model
        b"NMNMLAN2" + json.dumps({"id": "x", "name": "n", "ip": "i", "port": 1,
                                  "role": "pair", "tok": "t", "ik": "a",
                                  "ek": "b"}).encode(),
        b"NMNMLAN2" + json.dumps({"id": "x", "name": "n", "ip": "i", "port": 1,
                                  "role": "send", "tok": "t"}).encode(),
    ])
    def test_rejects_bad_packets(self, pkt):
        assert nomnom._lan_decode_beacon(pkt) is None


class TestLanHttp:
    """Drive the real pull/push handlers over loopback with a full triple-DH
    handshake; no known-peers file is set, so TOFU treats peers as new."""

    def _serve(self, handler, state):
        import http.server
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        srv.timeout = 0.5
        port = srv.server_address[1]
        stop = {"go": True}

        def loop():
            while stop["go"] and not state.get("done"):
                srv.handle_request()
        threading.Thread(target=loop, daemon=True).start()
        return srv, port, stop

    def _host_session(self, host_ident, state):
        ek_priv, ek_pub = nomnom._dh_keypair()
        make_session = nomnom._make_responder_session(
            host_ident, ek_priv, ek_pub, state, threading.Lock())
        return make_session, format(ek_pub, "x")

    def _peer(self, host_ident, ek_pub_hex, port):
        return {"id": host_ident["device_id"], "name": host_ident["name"],
                "ip": "127.0.0.1", "port": port, "tok": "tok",
                "ik": host_ident["ik_pub"], "ek": ek_pub_hex}

    def test_pull_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        host = _mk_ident("H", "host")
        join = _mk_ident("J", "join")
        state = {}
        make_session, host_ek = self._host_session(host, state)
        handler = nomnom._lan_make_pull_handler(
            make_session,
            lambda key: nomnom.encrypt_bytes(b"hi there", "n.txt", key.hex()),
            "tok", state)
        srv, port, stop = self._serve(handler, state)
        try:
            peer = self._peer(host, host_ek, port)
            key, ek_hex = nomnom._joiner_session(join, peer)
            got = nomnom._lan_fetch_blob("127.0.0.1", port, "tok", join,
                                         ek_hex, timeout=5)
            assert nomnom.decrypt_bytes(got, key.hex()) == ("n.txt", b"hi there")
            assert state.get("peer_id") == "J"
            assert state.get("peer_ik") == join["ik_pub"]
        finally:
            stop["go"] = False
            state["done"] = True
            srv.server_close()

    def test_pull_rejected_403(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        join = _mk_ident("J", "join")
        state = {}
        handler = nomnom._lan_make_pull_handler(
            lambda *a: None, lambda key: b"", "tok", state)
        srv, port, stop = self._serve(handler, state)
        try:
            with pytest.raises(ConnectionError):
                nomnom._lan_fetch_blob("127.0.0.1", port, "tok", join, "ab",
                                       timeout=5)
            assert not state.get("done")
        finally:
            stop["go"] = False
            srv.server_close()

    def test_pull_missing_headers_400(self, tmp_path, monkeypatch):
        import urllib.error
        import urllib.request
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        host = _mk_ident("H", "host")
        state = {}
        make_session, _ek = self._host_session(host, state)
        handler = nomnom._lan_make_pull_handler(
            make_session, lambda key: b"x", "tok", state)
        srv, port, stop = self._serve(handler, state)
        try:
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/tok", timeout=5)
            assert ei.value.code == 400
        finally:
            stop["go"] = False
            srv.server_close()

    def test_push_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        host = _mk_ident("H", "host")
        join = _mk_ident("J", "join")
        state = {}
        make_session, host_ek = self._host_session(host, state)
        handler = nomnom._lan_make_push_handler(
            make_session, state, "tok", nomnom._LAN_MAX_UPLOAD, threading.Lock())
        srv, port, stop = self._serve(handler, state)
        try:
            peer = self._peer(host, host_ek, port)
            key, ek_hex = nomnom._joiner_session(join, peer)
            blob = nomnom.encrypt_bytes(b"data", "f.bin", key.hex())
            nomnom._lan_upload_blob("127.0.0.1", port, "tok", blob, join,
                                    ek_hex, timeout=5)
            time.sleep(0.2)
            assert state.get("blob") == blob
            assert state.get("session_key") == key
            assert state.get("peer_id") == "J"
            assert nomnom.decrypt_bytes(state["blob"],
                                        state["session_key"].hex()) == (
                "f.bin", b"data")
        finally:
            stop["go"] = False
            state["done"] = True
            srv.server_close()

    def test_push_rejected_403(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        join = _mk_ident("J", "join")
        state = {}
        handler = nomnom._lan_make_push_handler(
            lambda *a: None, state, "tok", nomnom._LAN_MAX_UPLOAD,
            threading.Lock())
        srv, port, stop = self._serve(handler, state)
        try:
            with pytest.raises(ConnectionError):
                nomnom._lan_upload_blob("127.0.0.1", port, "tok", b"x" * 70,
                                        join, "ab", timeout=5)
        finally:
            stop["go"] = False
            srv.server_close()


class TestLanTransfer:
    """Full cmd round-trips on loopback. Two devices are simulated with a
    per-thread identity and a real (initially empty) known-peers store, so the
    triple-DH handshake and TOFU first-use pinning run for real (no UDP)."""

    def _setup(self, monkeypatch, tmp_path, host_thread, host_ident, join_ident):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

        def fake_identity():
            if threading.current_thread().name == host_thread:
                return host_ident
            return join_ident
        monkeypatch.setattr(nomnom, "_load_identity", fake_identity)

        rec = {}

        def fake_beacon(stop, device_id, name, ip, port, role, token, ik, ek,
                        interval=1.0):
            rec["beacon"] = {"id": device_id, "name": name, "ip": ip,
                             "port": port, "role": role, "tok": token,
                             "ik": ik, "ek": ek}
            stop.wait()
        monkeypatch.setattr(nomnom, "_lan_beacon_sender", fake_beacon)
        monkeypatch.setattr(
            nomnom, "_lan_discover",
            lambda role, **k: ([rec["beacon"]] if rec.get("beacon")
                               and rec["beacon"]["role"] == role else []))
        monkeypatch.setattr("builtins.input", lambda *a: "1")
        return rec

    def _wait_beacon(self, rec, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if "beacon" in rec:
                return
            time.sleep(0.02)
        raise AssertionError("host never beaconed")

    def test_receiver_first(self, tmp_path, monkeypatch):
        host = _mk_ident("R", "rxbox")
        join = _mk_ident("S", "txbox")
        rec = self._setup(monkeypatch, tmp_path, "rxhost", host, join)
        out = tmp_path / "out"
        out.mkdir()
        monkeypatch.chdir(out)
        src = tmp_path / "f.bin"
        payload = bytes(range(256)) * 5
        src.write_bytes(payload)

        result = {}

        def run():
            result["rc"] = nomnom.cmd_decrypt(host="127.0.0.1", timeout=10)
        th = threading.Thread(target=run, name="rxhost", daemon=True)
        th.start()
        self._wait_beacon(rec)
        rc = nomnom.cmd_encrypt(str(src), host="127.0.0.1", timeout=8)
        th.join(timeout=5)
        assert rc == 0 and result.get("rc") == 0
        assert (out / "f.bin").read_bytes() == payload
        # both sides pinned each other on first use
        peers = nomnom._load_known_peers()
        assert "R" in peers and "S" in peers

    def test_sender_first(self, tmp_path, monkeypatch):
        host = _mk_ident("S", "txbox")
        join = _mk_ident("R", "rxbox")
        rec = self._setup(monkeypatch, tmp_path, "txhost", host, join)
        out = tmp_path / "out"
        out.mkdir()
        monkeypatch.chdir(out)
        src = tmp_path / "f.bin"
        payload = b"hello world\n" * 200
        src.write_bytes(payload)

        result = {}

        def run():
            result["rc"] = nomnom.cmd_encrypt(str(src), host="127.0.0.1",
                                              timeout=10)
        th = threading.Thread(target=run, name="txhost", daemon=True)
        th.start()
        self._wait_beacon(rec)
        rc = nomnom.cmd_decrypt(host="127.0.0.1", timeout=8)
        th.join(timeout=5)
        assert rc == 0 and result.get("rc") == 0
        assert (out / "f.bin").read_bytes() == payload


class TestLanErrors:
    def test_encrypt_missing_file(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        rc = nomnom.cmd_encrypt(str(tmp_path / "nope.txt"), timeout=1)
        assert rc == 1
        assert "not a file" in capsys.readouterr().err

    def test_encrypt_no_peer(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(nomnom, "_lan_discover", lambda *a, **k: [])
        monkeypatch.setattr(nomnom, "_lan_host", lambda **k: {})
        src = tmp_path / "f.txt"
        src.write_text("x")
        rc = nomnom.cmd_encrypt(str(src), timeout=1)
        assert rc == 1
        assert "no receiver" in capsys.readouterr().err

    def test_decrypt_no_peer(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(nomnom, "_lan_discover", lambda *a, **k: [])
        monkeypatch.setattr(nomnom, "_lan_host", lambda **k: {})
        rc = nomnom.cmd_decrypt(timeout=1)
        assert rc == 1
        assert "no sender" in capsys.readouterr().err

    def test_forget_command(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        nomnom._save_known_peer("H", "host", "aa")
        assert nomnom.cmd_forget("host") == 0
        assert "forgot host" in capsys.readouterr().err
        assert nomnom._known_peer_ik("H") is None
        assert nomnom.cmd_forget("host") == 1
        assert "no known peer" in capsys.readouterr().err


@pytest.mark.skipif(
    os.environ.get("NOMNOM_E2E") != "1",
    reason="real-broadcast e2e on localhost; run with NOMNOM_E2E=1",
)
class TestLanTofuE2E:
    """Spawn the real CLI as two processes (separate config dirs) and let them
    discover and transfer over genuine UDP broadcast, trusting on first use.
    Skipped by default."""

    NOM = Path(__file__).resolve().parent.parent / "nomnom.py"

    def _env(self, cfg):
        return dict(os.environ, XDG_CONFIG_HOME=str(cfg), PYTHONUNBUFFERED="1")

    def test_transfer_first_use(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        out = tmp_path / "out"
        out.mkdir()
        src = tmp_path / "doc.txt"
        payload = b"tofu transfer\n" * 40
        src.write_bytes(payload)
        # receiver hosts first
        rcv = subprocess.Popen(
            [sys.executable, str(self.NOM), "decrypt", "--timeout", "25"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env=self._env(b), cwd=out)
        time.sleep(5)  # let it start beaconing
        snd = subprocess.Popen(
            [sys.executable, str(self.NOM), "encrypt", str(src),
             "--timeout", "20"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env=self._env(a))
        snd.communicate("1\n", timeout=40)   # pick the only receiver
        rcv.communicate(timeout=40)
        assert snd.returncode == 0 and rcv.returncode == 0
        assert (out / "doc.txt").read_bytes() == payload
        # both sides pinned each other on first use
        assert (a / "nomnom" / "known_peers.json").exists()
        assert (b / "nomnom" / "known_peers.json").exists()
