"""Tests for bundler.py.

Covers the pure-logic surface: gitignore matching, repo scanning,
tree model, selection cascade, output rendering, and last-selection
persistence. The curses picker (`pick`) is excluded — it needs a TTY.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import bundler


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
        regex = bundler._glob_to_regex(pattern, anchored=False)
        assert bool(regex.match(path)) is expected

    @pytest.mark.parametrize("pattern,path,expected", [
        ("build", "build", True),
        ("build", "src/build", False),
        ("a/b", "a/b", True),
        ("a/b", "x/a/b", False),
    ])
    def test_anchored(self, pattern, path, expected):
        regex = bundler._glob_to_regex(pattern, anchored=True)
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
        regex = bundler._glob_to_regex(pattern, anchored=True)
        assert bool(regex.match(path)) is expected

    def test_path_with_descendants_is_treated_as_match(self):
        # The matcher uses (?:/.*)?$ so "build" matches "build/anything"
        regex = bundler._glob_to_regex("build", anchored=True)
        assert regex.match("build/x.txt")


class TestGitignoreMatcher:
    def test_simple_glob_unanchored(self, tmp_path):
        (tmp_path / ".gitignore").write_text("*.log\n")
        gi = bundler.load_gitignore(tmp_path)
        assert gi.is_ignored("foo.log", is_dir=False)
        assert gi.is_ignored("src/foo.log", is_dir=False)
        assert not gi.is_ignored("foo.txt", is_dir=False)

    def test_anchored_pattern(self, tmp_path):
        (tmp_path / ".gitignore").write_text("/build\n")
        gi = bundler.load_gitignore(tmp_path)
        assert gi.is_ignored("build", is_dir=True)
        assert not gi.is_ignored("src/build", is_dir=True)

    def test_dir_only_matches_dir_not_file(self, tmp_path):
        (tmp_path / ".gitignore").write_text("foo/\n")
        gi = bundler.load_gitignore(tmp_path)
        assert gi.is_ignored("foo", is_dir=True)
        assert not gi.is_ignored("foo", is_dir=False)

    def test_negation(self, tmp_path):
        (tmp_path / ".gitignore").write_text("*.log\n!keep.log\n")
        gi = bundler.load_gitignore(tmp_path)
        assert gi.is_ignored("debug.log", is_dir=False)
        assert not gi.is_ignored("keep.log", is_dir=False)

    def test_blank_lines_and_comments(self, tmp_path):
        (tmp_path / ".gitignore").write_text("\n# comment\n\n*.tmp\n")
        gi = bundler.load_gitignore(tmp_path)
        assert gi.is_ignored("x.tmp", is_dir=False)
        assert not gi.is_ignored("x.txt", is_dir=False)

    def test_nested_gitignore_is_scoped_to_subtree(self, tmp_path):
        (tmp_path / ".gitignore").write_text("a.txt\n")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / ".gitignore").write_text("b.txt\n")
        gi = bundler.load_gitignore(tmp_path)
        assert gi.is_ignored("a.txt", is_dir=False)
        assert gi.is_ignored("sub/a.txt", is_dir=False)
        assert gi.is_ignored("sub/b.txt", is_dir=False)
        assert not gi.is_ignored("b.txt", is_dir=False)

    def test_no_gitignore_means_nothing_ignored(self, tmp_path):
        gi = bundler.load_gitignore(tmp_path)
        assert not gi.is_ignored("anything.txt", is_dir=False)
        assert not gi.is_ignored("any/dir", is_dir=True)


# ---------- is_binary ----------

class TestIsBinary:
    def test_text_file_is_not_binary(self, tmp_path):
        p = tmp_path / "t.txt"
        p.write_text("hello world\n")
        assert bundler.is_binary(p) is False

    def test_null_byte_makes_binary(self, tmp_path):
        p = tmp_path / "b.bin"
        p.write_bytes(b"hello\x00world")
        assert bundler.is_binary(p) is True

    def test_unreadable_treated_as_binary(self, tmp_path):
        # A path that doesn't exist returns True (treated as skip).
        assert bundler.is_binary(tmp_path / "missing") is True


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
        gi = bundler.load_gitignore(tmp_path)
        items = bundler.scan_repo(tmp_path, gi)
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
        gi = bundler.load_gitignore(tmp_path)
        items = bundler.scan_repo(tmp_path, gi)
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
        gi = bundler.load_gitignore(tmp_path)
        paths = rels(bundler.scan_repo(tmp_path, gi))
        assert "secrets" not in paths
        assert "secrets/key.txt" not in paths
        assert "app.log" not in paths
        assert "app.py" in paths

    def test_binary_files_skipped(self, tmp_path):
        make_repo(tmp_path, {
            "code.py": "print('hi')",
            "blob.bin": b"\x00\x01\x02\x03",
        })
        gi = bundler.load_gitignore(tmp_path)
        paths = rels(bundler.scan_repo(tmp_path, gi))
        assert "code.py" in paths
        assert "blob.bin" not in paths

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks unreliable on Windows")
    def test_symlinks_skipped(self, tmp_path):
        make_repo(tmp_path, {"real.py": "x"})
        os.symlink(tmp_path / "real.py", tmp_path / "link.py")
        gi = bundler.load_gitignore(tmp_path)
        paths = rels(bundler.scan_repo(tmp_path, gi))
        assert "real.py" in paths
        assert "link.py" not in paths

    def test_empty_repo(self, tmp_path):
        gi = bundler.load_gitignore(tmp_path)
        assert bundler.scan_repo(tmp_path, gi) == []


# ---------- build_tree / visible_indices ----------

class TestBuildTree:
    def _items(self, *pairs):
        return [bundler.ScanItem(rel=r, is_dir=is_dir) for r, is_dir in pairs]

    def test_parent_child_links(self):
        nodes = bundler.build_tree(self._items(
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
        nodes = bundler.build_tree(self._items(
            ("a", True), ("a/b", True), ("a/b/c.py", False),
        ))
        assert [n.depth for n in nodes] == [0, 1, 2]

    def test_default_collapsed(self):
        nodes = bundler.build_tree(self._items(
            ("src", True), ("src/main.py", False),
        ))
        assert all(not n.expanded for n in nodes)


class TestVisibleIndices:
    def test_root_always_visible(self):
        nodes = bundler.build_tree([
            bundler.ScanItem(rel="a", is_dir=True),
            bundler.ScanItem(rel="b.py", is_dir=False),
        ])
        assert bundler.visible_indices(nodes) == [0, 1]

    def test_collapsed_dir_hides_children(self):
        nodes = bundler.build_tree([
            bundler.ScanItem(rel="src", is_dir=True),
            bundler.ScanItem(rel="src/a.py", is_dir=False),
            bundler.ScanItem(rel="src/b.py", is_dir=False),
        ])
        # src collapsed (default) -> only src visible
        assert bundler.visible_indices(nodes) == [0]
        nodes[0].expanded = True
        assert bundler.visible_indices(nodes) == [0, 1, 2]


# ---------- cascade_check ----------

class TestCascadeCheck:
    def _three_level(self):
        items = [
            bundler.ScanItem(rel="src", is_dir=True),
            bundler.ScanItem(rel="src/api", is_dir=True),
            bundler.ScanItem(rel="src/api/h.py", is_dir=False),
            bundler.ScanItem(rel="src/api/m.py", is_dir=False),
            bundler.ScanItem(rel="src/u.py", is_dir=False),
            bundler.ScanItem(rel="README.md", is_dir=False),
        ]
        return bundler.build_tree(items)

    def test_cascade_check_dir_sets_all_descendants(self):
        nodes = self._three_level()
        bundler.cascade_check(nodes, 0, True)  # src
        for n in nodes[:5]:
            assert n.checked is True
        assert nodes[5].checked is False  # README is sibling

    def test_cascade_uncheck_resets_all_descendants(self):
        nodes = self._three_level()
        for n in nodes:
            n.checked = True
        bundler.cascade_check(nodes, 1, False)  # src/api
        assert nodes[1].checked is False
        assert nodes[2].checked is False
        assert nodes[3].checked is False
        assert nodes[0].checked is True  # parent untouched
        assert nodes[4].checked is True  # sibling untouched

    def test_cascade_on_leaf_only_affects_leaf(self):
        nodes = self._three_level()
        bundler.cascade_check(nodes, 5, True)
        assert nodes[5].checked is True
        assert all(n.checked is False for n in nodes[:5])


# ---------- render_ascii_tree ----------

class TestRenderAsciiTree:
    def test_single_file(self):
        out = bundler.render_ascii_tree(["a.py"], "repo")
        assert out == "repo/\n└── a.py"

    def test_dirs_before_files(self):
        out = bundler.render_ascii_tree(
            ["README.md", "src/main.py"], "repo"
        )
        lines = out.splitlines()
        assert lines[0] == "repo/"
        # src/ should appear before README.md (dirs first)
        assert lines.index("├── src/") < lines.index("└── README.md")

    def test_nested_indentation(self):
        out = bundler.render_ascii_tree(
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
        out = bundler.render_output("myrepo", tmp_path, ["a.py"], None)
        assert "packed representation of selected files from myrepo" in out
        assert '<file path="a.py">' in out
        assert "print('a')" in out
        assert "</file>" in out

    def test_includes_tree_when_provided(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        out = bundler.render_output(
            "myrepo", tmp_path, ["a.py"], "myrepo/\n└── a.py"
        )
        assert "<file_tree>" in out
        assert "</file_tree>" in out
        assert "└── a.py" in out

    def test_omits_tree_when_none(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        out = bundler.render_output("myrepo", tmp_path, ["a.py"], None)
        assert "<file_tree>" not in out

    def test_handles_non_utf8(self, tmp_path):
        (tmp_path / "weird.txt").write_bytes(b"valid\xff\xfeinvalid utf8")
        out = bundler.render_output("r", tmp_path, ["weird.txt"], None)
        assert '<file path="weird.txt">' in out
        # Should not raise; content goes through errors="replace"
        assert "valid" in out

    def test_read_error_inlined(self, tmp_path):
        out = bundler.render_output("r", tmp_path, ["does-not-exist.py"], None)
        assert "<<read error" in out


# ---------- pick_output_path ----------

class TestPickOutputPath:
    def test_unique_when_no_collision(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p = bundler.pick_output_path("repo")
        assert p.parent == tmp_path
        assert p.name.startswith("repo-") and p.name.endswith(".txt")

    def test_appends_suffix_on_collision(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p1 = bundler.pick_output_path("repo")
        p1.write_text("first")
        p2 = bundler.pick_output_path("repo")
        # Either timestamp differs (extremely fast tests run in same second though),
        # or a -2 suffix was added.
        assert p2 != p1
        assert p2.parent == tmp_path


# ---------- last selection ----------

class TestLastSelection:
    def test_save_and_load_roundtrip(self, tmp_path):
        bundler.save_last_selection(tmp_path, ["a.py", "src/b.py"])
        out = bundler.load_last_selection(tmp_path)
        assert out == ["a.py", "src/b.py"]

    def test_load_missing_returns_none(self, tmp_path):
        assert bundler.load_last_selection(tmp_path) is None

    def test_load_invalid_json_returns_none(self, tmp_path):
        (tmp_path / bundler.LAST_SELECTION_FILE).write_text("{not json")
        assert bundler.load_last_selection(tmp_path) is None

    def test_load_wrong_shape_returns_none(self, tmp_path):
        (tmp_path / bundler.LAST_SELECTION_FILE).write_text(
            json.dumps({"selected": [1, 2, 3]})
        )
        assert bundler.load_last_selection(tmp_path) is None

    def test_save_is_deterministically_sorted(self, tmp_path):
        bundler.save_last_selection(tmp_path, ["z.py", "a.py", "m.py"])
        data = json.loads(
            (tmp_path / bundler.LAST_SELECTION_FILE).read_text()
        )
        assert data["selected"] == ["a.py", "m.py", "z.py"]
