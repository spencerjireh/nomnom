"""Tests for nomnom.py.

Covers the pure-logic surface: gitignore matching, repo scanning,
tree model, selection cascade, output rendering, and last-selection
persistence. The curses picker (`pick`) is excluded — it needs a TTY.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


# ---------- last selection ----------

class TestLastSelection:
    @pytest.fixture(autouse=True)
    def _fake_home(self, tmp_path, monkeypatch):
        home = tmp_path / "_home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

    @pytest.fixture
    def repo(self, tmp_path):
        r = tmp_path / "repo"
        r.mkdir()
        return r

    def test_save_writes_outside_target_repo(self, repo):
        nomnom.save_last_selection(repo, ["a.py"])
        assert not (repo / ".nomnom-last.json").exists()
        assert nomnom._cache_path_for(repo).exists()

    def test_save_and_load_roundtrip(self, repo):
        nomnom.save_last_selection(repo, ["a.py", "src/b.py"])
        out = nomnom.load_last_selection(repo)
        assert out == ["a.py", "src/b.py"]

    def test_load_missing_returns_none(self, repo):
        assert nomnom.load_last_selection(repo) is None

    def test_load_invalid_json_returns_none(self, repo):
        p = nomnom._cache_path_for(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json")
        assert nomnom.load_last_selection(repo) is None

    def test_load_wrong_shape_returns_none(self, repo):
        p = nomnom._cache_path_for(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"repo_path": str(repo.resolve()), "selected": [1, 2, 3]}
            )
        )
        assert nomnom.load_last_selection(repo) is None

    def test_load_repo_path_mismatch_returns_none(self, repo):
        p = nomnom._cache_path_for(repo)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"repo_path": "/some/other/path", "selected": ["a.py"]}
            )
        )
        assert nomnom.load_last_selection(repo) is None

    def test_save_is_deterministically_sorted(self, repo):
        nomnom.save_last_selection(repo, ["z.py", "a.py", "m.py"])
        data = json.loads(nomnom._cache_path_for(repo).read_text())
        assert data["selected"] == ["a.py", "m.py", "z.py"]
        assert data["repo_path"] == str(repo.resolve())

    def test_save_silently_skips_when_unwritable(self, repo, monkeypatch):
        monkeypatch.setattr(
            nomnom, "_cache_path_for",
            lambda _root: Path("/dev/null/nope.json"),
        )
        nomnom.save_last_selection(repo, ["a.py"])


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
        rc = nomnom.cmd_commit(str(repo), copy=False)
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
        rc = nomnom.cmd_commit(str(repo), copy=False)
        assert rc == 1
        err = capsys.readouterr().err
        assert "nothing to commit" in err

    def test_untracked_only_errors(self, tmp_path, monkeypatch, capsys):
        # Per spec: untracked-only doesn't qualify as "something to commit".
        repo = tmp_path / "ut"
        make_git_repo(repo, initial={"a.txt": "hi\n"})
        (repo / "new.txt").write_text("new\n")
        monkeypatch.chdir(tmp_path)
        rc = nomnom.cmd_commit(str(repo), copy=False)
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
        rc = nomnom.cmd_commit(str(repo), copy=False)
        assert rc == 0
        bundles = list(out_dir.glob("dh-*-commit-*.txt"))
        assert len(bundles) == 1
        # Short SHA is 7 hex chars; filename infix should not be "main".
        assert "main-commit" not in bundles[0].name

    def test_not_a_git_repo_errors(self, tmp_path, monkeypatch, capsys):
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            nomnom.cmd_commit(str(plain), copy=False)
        assert exc.value.code == 1
        assert "not a git repository" in capsys.readouterr().err


# ---------- cmd_pr ----------

class TestPrBundle:
    def test_missing_gh_errors(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "p"
        make_git_repo(repo, initial={"a.txt": "1\n"})
        monkeypatch.setattr(nomnom.shutil, "which",
                            lambda name: None if name == "gh" else "/bin/git")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            nomnom.cmd_pr(str(repo), copy=False, base=None)
        assert exc.value.code == 1
        assert "requires gh" in capsys.readouterr().err

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
        rc = nomnom.cmd_pr(str(repo), copy=False, base=None)
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
        rc = nomnom.cmd_pr(str(repo), copy=False, base=None)
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
        rc = nomnom.cmd_pr(str(repo), copy=False, base="develop")
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
        rc = nomnom.cmd_pr(str(repo), copy=False, base="main")
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
        rc = nomnom.cmd_pr(str(repo), copy=False, base=None)
        assert rc == 0
        text = next(out_dir.glob("p-feature-pr-*.txt")).read_text()
        # First 500 chars of body kept, rest replaced with ellipsis.
        assert ("x" * 500 + "…") in text
        assert ("x" * 501) not in text


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
