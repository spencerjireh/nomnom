#!/usr/bin/env python3
"""nomnom.py - feed your repo to the LLM, one .txt snack at a time.

Run: python3 nomnom.py [/path/to/repo]
Stdlib only. macOS/Linux. Python 3.8+.
"""

from __future__ import annotations

import argparse
import ast
import base64
import contextlib
import curses
import enum
import fnmatch
import functools
import hashlib
import hmac
import http.client
import io
import json
import ipaddress
import locale
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass


JUNK_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env",
    "__pycache__", "dist", "build", ".next", ".turbo",
    "target", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".idea", ".vscode", ".cache", ".gradle",
}
BINARY_SNIFF_BYTES = 8192
APPROX_BYTES_PER_TOKEN = 4

# --- nomnom:extensions (auto-managed; edit with `nomnom register`) ---
TEXT_EXTENSIONS = {
    '.bash',
    '.bazel',
    '.bib',
    '.bzl',
    '.c',
    '.cc',
    '.cfg',
    '.cjs',
    '.clj',
    '.cljs',
    '.conf',
    '.cpp',
    '.csv',
    '.css',
    '.dart',
    '.env',
    '.erl',
    '.ex',
    '.exs',
    '.fish',
    '.go',
    '.gql',
    '.graphql',
    '.h',
    '.hcl',
    '.hh',
    '.hpp',
    '.htm',
    '.html',
    '.ini',
    '.java',
    '.jl',
    '.js',
    '.json',
    '.jsonc',
    '.jsx',
    '.kt',
    '.kts',
    '.less',
    '.lock',
    '.lua',
    '.m',
    '.markdown',
    '.md',
    '.mjs',
    '.mm',
    '.nix',
    '.php',
    '.pl',
    '.proto',
    '.py',
    '.pyi',
    '.r',
    '.rb',
    '.rs',
    '.rst',
    '.sass',
    '.scala',
    '.scss',
    '.sh',
    '.sql',
    '.svelte',
    '.svg',
    '.swift',
    '.tex',
    '.tf',
    '.tfvars',
    '.toml',
    '.ts',
    '.tsv',
    '.tsx',
    '.txt',
    '.vue',
    '.xml',
    '.yaml',
    '.yml',
    '.zsh',
}

BINARY_EXTENSIONS = {
    '.7z',
    '.a',
    '.avi',
    '.avif',
    '.bin',
    '.bmp',
    '.bz2',
    '.class',
    '.dat',
    '.db',
    '.dll',
    '.dylib',
    '.ear',
    '.egg',
    '.eot',
    '.exe',
    '.flac',
    '.gem',
    '.gif',
    '.gz',
    '.ico',
    '.jar',
    '.jpeg',
    '.jpg',
    '.mov',
    '.mp3',
    '.mp4',
    '.npy',
    '.npz',
    '.o',
    '.ogg',
    '.otf',
    '.pdf',
    '.pkl',
    '.png',
    '.pyc',
    '.pyo',
    '.rar',
    '.so',
    '.sqlite',
    '.sqlite3',
    '.tar',
    '.tiff',
    '.ttf',
    '.war',
    '.wav',
    '.webm',
    '.webp',
    '.whl',
    '.woff',
    '.woff2',
    '.xz',
    '.zip',
}

KNOWN_TEXT_NAMES = {
    'AUTHORS',
    'CHANGELOG',
    'Dockerfile',
    'Gemfile',
    'LICENSE',
    'Makefile',
    'NOTICE',
    'Procfile',
    'README',
    'Rakefile',
}

SECRET_PATTERNS = [
    '.env',
    '.env.*',
    '*.pem',
    '*.key',
    '*.pfx',
    '*.p12',
    'id_rsa*',
    'id_dsa*',
    'id_ecdsa*',
    'id_ed25519*',
    '.netrc',
    '.npmrc',
    '.pypirc',
    'secrets.yaml',
    'secrets.yml',
    'secrets.json',
    'credentials',
    'credentials.json',
]
# --- end nomnom:extensions ---


# ---------- gitignore ----------

@dataclass
class GitignoreRule:
    pattern: str
    negated: bool
    dir_only: bool
    base: str
    regex: re.Pattern[str]


def _glob_to_regex(pattern: str, anchored: bool) -> re.Pattern[str]:
    out = ["^"]
    if not anchored:
        out.append("(?:.*/)?")
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                if i + 2 < len(pattern) and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                inner = pattern[i + 1:j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner + "]")
                i = j + 1
                continue
        elif c == "/":
            out.append("/")
        else:
            out.append(re.escape(c))
        i += 1
    out.append("(?:/.*)?$")
    return re.compile("".join(out))


class GitignoreMatcher:
    def __init__(self, rules: list[GitignoreRule]):
        self.rules = rules

    def is_ignored(self, rel_path: str, is_dir: bool) -> bool:
        ignored = False
        for r in self.rules:
            if r.base:
                if rel_path != r.base and not rel_path.startswith(r.base + "/"):
                    continue
                local = "" if rel_path == r.base else rel_path[len(r.base) + 1:]
            else:
                local = rel_path
            if not local:
                continue
            if r.dir_only and not is_dir:
                continue
            if r.regex.match(local):
                ignored = not r.negated
        return ignored


def _build_pattern_matcher(patterns: list[str]) -> GitignoreMatcher:
    """Build a GitignoreMatcher from CLI --include / --exclude patterns.

    Trailing slashes are stripped but the rule still applies to files
    under that directory (unlike .gitignore's dir-only semantics, since a
    walker would never descend; here we're filtering a flat list). A
    leading `!` negates."""
    rules: list[GitignoreRule] = []
    for raw in patterns:
        line = raw.strip()
        if not line:
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        if line.endswith("/"):
            line = line[:-1]
        anchored = "/" in line
        if line.startswith("/"):
            line = line[1:]
        if not line:
            continue
        try:
            regex = _glob_to_regex(line, anchored)
        except re.error:
            continue
        rules.append(GitignoreRule(line, negated, False, "", regex))
    return GitignoreMatcher(rules)


def load_gitignore(root: Path) -> GitignoreMatcher:
    rules: list[GitignoreRule] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if d not in JUNK_DIRS and not (Path(dirpath) / d).is_symlink()
        ]
        if ".gitignore" not in filenames:
            continue
        try:
            text = (Path(dirpath) / ".gitignore").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
        rel_base = str(Path(dirpath).relative_to(root)).replace("\\", "/")
        if rel_base == ".":
            rel_base = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            if negated:
                line = line[1:]
            dir_only = line.endswith("/")
            if dir_only:
                line = line[:-1]
            anchored = "/" in line
            if line.startswith("/"):
                line = line[1:]
            if not line:
                continue
            try:
                regex = _glob_to_regex(line, anchored)
            except re.error:
                continue
            rules.append(GitignoreRule(line, negated, dir_only, rel_base, regex))
    return GitignoreMatcher(rules)


# ---------- scan ----------

@dataclass
class ScanItem:
    rel: str
    is_dir: bool


def _fmt_count(n: int) -> str:
    """999 / 1.0K / 12K / 1.2M / 12M (decimal, one decimal under 10)."""
    if n < 1000:
        return str(n)
    for unit in ("K", "M", "G", "T"):
        n_unit = n / 1000
        if n_unit < 1000 or unit == "T":
            return f"{n_unit:.1f}{unit}" if n_unit < 10 else f"{n_unit:.0f}{unit}"
        n = int(n_unit)
    return str(n)


def _fmt_size(n: int) -> str:
    return _fmt_count(n)


def _fmt_loc(n: int) -> str:
    return _fmt_count(n) + "L"


def _fmt_tokens(n: int) -> str:
    return "~" + _fmt_count(n) + "T"


def is_binary(path: Path) -> bool:
    name = path.name
    if name in KNOWN_TEXT_NAMES:
        return False
    ext = path.suffix.lower()
    if ext in BINARY_EXTENSIONS:
        return True
    if ext in TEXT_EXTENSIONS:
        return False
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(BINARY_SNIFF_BYTES)
    except OSError:
        return True


def is_secret_file(name: str) -> bool:
    if name.endswith(".pub"):
        return False
    return any(fnmatch.fnmatchcase(name, pat) for pat in SECRET_PATTERNS)


def scan_repo(
    root: Path,
    gi: GitignoreMatcher,
    skip_secrets: bool = True,
) -> list[ScanItem]:
    items: list[ScanItem] = []

    def walk(dir_abs: Path, rel_dir: str) -> None:
        try:
            entries = list(os.scandir(dir_abs))
        except OSError:
            return
        dirs: list[tuple[str, str, Path]] = []
        files: list[tuple[str, str, Path]] = []
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
                if e.is_dir(follow_symlinks=False):
                    if e.name in JUNK_DIRS:
                        continue
                    if gi.is_ignored(rel, is_dir=True):
                        continue
                    dirs.append((e.name, rel, Path(e.path)))
                elif e.is_file(follow_symlinks=False):
                    if gi.is_ignored(rel, is_dir=False):
                        continue
                    if skip_secrets and is_secret_file(e.name):
                        continue
                    files.append((e.name, rel, Path(e.path)))
            except OSError:
                continue
        dirs.sort()
        files.sort()
        for name, rel, abs_path in dirs:
            items.append(ScanItem(rel=rel, is_dir=True))
            walk(abs_path, rel)
        for name, rel, abs_path in files:
            if is_binary(abs_path):
                continue
            items.append(ScanItem(rel=rel, is_dir=False))

    walk(root, "")
    return items


def apply_include_exclude(
    items: list[ScanItem],
    includes: list[str],
    excludes: list[str],
) -> list[ScanItem]:
    """Filter scan items by gitignore-style --include / --exclude patterns.

    A file passes if it matches some include pattern (or includes is
    empty) and matches no exclude pattern. Directories are then re-derived
    from the surviving files' parents, so the tree builder still sees a
    valid parent chain."""
    if not includes and not excludes:
        return items
    inc = _build_pattern_matcher(includes) if includes else None
    exc = _build_pattern_matcher(excludes) if excludes else None

    keep_files: set[str] = set()
    for it in items:
        if it.is_dir:
            continue
        if inc is not None and not inc.is_ignored(it.rel, False):
            continue
        if exc is not None and exc.is_ignored(it.rel, False):
            continue
        keep_files.add(it.rel)

    needed_dirs: set[str] = set()
    for rel in keep_files:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            needed_dirs.add("/".join(parts[:i]))

    out: list[ScanItem] = []
    for it in items:
        if it.is_dir:
            if it.rel in needed_dirs:
                out.append(it)
        elif it.rel in keep_files:
            out.append(it)
    return out


def collect_stats(
    root: Path, items: list[ScanItem]
) -> dict[str, tuple[int, int, int] | None]:
    """One read pass per file. Returns rel -> (bytes, loc, tokens), or None on read error."""
    out: dict[str, tuple[int, int, int] | None] = {}
    for it in items:
        if it.is_dir:
            continue
        p = root / it.rel
        try:
            size = p.stat().st_size
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            out[it.rel] = None
            continue
        loc = content.count("\n")
        if content and not content.endswith("\n"):
            loc += 1
        tokens = len(content) // APPROX_BYTES_PER_TOKEN
        out[it.rel] = (size, loc, tokens)
    return out


# ---------- clipboard ----------

def detect_clipboard_cmd() -> list[str] | None:
    if shutil.which("pbcopy"):
        return ["pbcopy"]
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("wl-copy"):
        return ["wl-copy"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    return None


def copy_to_clipboard(text: str) -> bool:
    cmd = detect_clipboard_cmd()
    if not cmd:
        return False
    try:
        subprocess.run(cmd, input=text, text=True, check=True)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


# ---------- tree model ----------

@dataclass
class Node:
    rel: str
    name: str
    is_dir: bool
    depth: int
    parent: int | None
    children: list[int] = field(default_factory=list)
    expanded: bool = False
    checked: bool = False
    size: int = 0
    loc: int = 0
    tokens: int = 0
    read_error: bool = False


def build_tree(
    items: list[ScanItem],
    stats: dict[str, tuple[int, int, int] | None] | None = None,
) -> list[Node]:
    nodes: list[Node] = []
    by_rel: dict[str, int] = {}
    for it in items:
        parts = it.rel.split("/")
        depth = len(parts) - 1
        name = parts[-1]
        parent_rel = "/".join(parts[:-1]) if depth > 0 else ""
        parent_idx = by_rel.get(parent_rel) if parent_rel else None
        idx = len(nodes)
        nodes.append(Node(
            rel=it.rel, name=name, is_dir=it.is_dir,
            depth=depth, parent=parent_idx,
        ))
        if parent_idx is not None:
            nodes[parent_idx].children.append(idx)
        by_rel[it.rel] = idx

    if stats is not None:
        for n in nodes:
            if n.is_dir:
                continue
            s = stats.get(n.rel)
            if s is None:
                n.read_error = True
            else:
                n.size, n.loc, n.tokens = s
        # Post-order aggregate: children precede parents in nodes (parent_idx
        # is set only after the parent is appended), so reverse iteration is
        # safe for summing.
        for n in reversed(nodes):
            if n.is_dir:
                for ci in n.children:
                    c = nodes[ci]
                    n.size += c.size
                    n.loc += c.loc
                    n.tokens += c.tokens
    return nodes


def visible_indices(nodes: list[Node], sort_key=None) -> list[int]:
    """Indices of nodes whose ancestors are all expanded.

    sort_key=None preserves insertion order (alpha, dirs-first within parent).
    Otherwise it's applied within each parent group, mixing dirs and files."""
    out: list[int] = []
    if sort_key is None:
        for i, n in enumerate(nodes):
            cur = n.parent
            ok = True
            while cur is not None:
                if not nodes[cur].expanded:
                    ok = False
                    break
                cur = nodes[cur].parent
            if ok:
                out.append(i)
        return out
    roots = sorted(
        (i for i, n in enumerate(nodes) if n.parent is None),
        key=lambda i: sort_key(nodes[i]),
    )

    def walk(idx: int) -> None:
        out.append(idx)
        n = nodes[idx]
        if not n.expanded:
            return
        for ci in sorted(n.children, key=lambda c: sort_key(nodes[c])):
            walk(ci)

    for r in roots:
        walk(r)
    return out


def cascade_check(nodes: list[Node], idx: int, value: bool) -> None:
    stack = [idx]
    while stack:
        i = stack.pop()
        nodes[i].checked = value
        stack.extend(nodes[i].children)


# ---------- picker ----------

def _setup_theme() -> dict[str, int]:
    """Build a curses-attribute theme dict, respecting NO_COLOR."""
    plain = {
        "dir": 0, "file": 0, "checked": curses.A_BOLD,
        "dim": curses.A_DIM, "filter": curses.A_BOLD,
        "cursor": curses.A_REVERSE,
    }
    if os.environ.get("NO_COLOR") is not None:
        return plain
    try:
        if not curses.has_colors():
            return plain
        curses.start_color()
    except curses.error:
        return plain
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    try:
        curses.init_pair(1, curses.COLOR_CYAN, bg)
        curses.init_pair(2, curses.COLOR_GREEN, bg)
        curses.init_pair(3, curses.COLOR_YELLOW, bg)
    except curses.error:
        return plain
    return {
        "dir":     curses.color_pair(1),
        "file":    0,
        "checked": curses.color_pair(2) | curses.A_BOLD,
        "dim":     curses.A_DIM,
        "filter":  curses.color_pair(3) | curses.A_BOLD,
        "cursor":  curses.A_REVERSE,
    }


def _apply_filter_key(ch: int, filter_active: bool, filter_buf: str) -> tuple[bool, str]:
    """Update (filter_active, filter_buf) for a key pressed while filter input is open."""
    if ch in (10, 13):
        return False, filter_buf
    if ch == 27:
        return False, ""
    if ch in (curses.KEY_BACKSPACE, 127, 8):
        return filter_active, filter_buf[:-1]
    if 32 <= ch < 127:
        return filter_active, filter_buf + chr(ch)
    return filter_active, filter_buf


class Destination(enum.IntEnum):
    FILE = 0
    CLIPBOARD = 1
    STDOUT = 2
    SEND = 3


_DESTINATION_LABELS = {
    Destination.FILE: "file",
    Destination.CLIPBOARD: "clipboard",
    Destination.STDOUT: "stdout",
    Destination.SEND: "send",
}


def cycle_destination(
    d: Destination, allowed: tuple[Destination, ...] | None = None,
) -> Destination:
    """Advance `d` to the next destination.

    `allowed` restricts the cycle to a subset (used inside the TUI to hide
    STDOUT, which is meaningless when curses owns the terminal). If `d`
    isn't in `allowed`, snap to the first allowed entry."""
    if allowed is None:
        return Destination((int(d) + 1) % len(Destination))
    if d not in allowed:
        return allowed[0]
    i = allowed.index(d)
    return allowed[(i + 1) % len(allowed)]


class Verb(enum.IntEnum):
    BUNDLE = 0
    COMMIT = 1
    PR = 2
    REVIEW = 3


_VERB_LABELS = {
    Verb.BUNDLE: "bundle",
    Verb.COMMIT: "commit",
    Verb.PR: "pr",
    Verb.REVIEW: "review",
}


def cycle_verb(v: Verb, allowed: tuple[Verb, ...] | None = None) -> Verb:
    """Advance `v` to the next verb, restricted to `allowed` if given.

    Mirrors `cycle_destination`. Non-git directories pass
    `allowed=(Verb.BUNDLE,)` so cycling becomes a no-op."""
    if allowed is None:
        return Verb((int(v) + 1) % len(Verb))
    if v not in allowed:
        return allowed[0]
    i = allowed.index(v)
    return allowed[(i + 1) % len(allowed)]


def compute_summary(nodes: list[Node]) -> tuple[int, int, int]:
    """(file_count, total_bytes, approx_tokens) over checked non-dir nodes."""
    count = 0
    total_bytes = 0
    for n in nodes:
        if n.is_dir or not n.checked:
            continue
        count += 1
        total_bytes += n.size
    return count, total_bytes, total_bytes // APPROX_BYTES_PER_TOKEN


def format_footer(
    dest: Destination,
    include_tree: bool,
    summary: tuple[int, int, int],
    width: int,
    verb: Verb = Verb.BUNDLE,
) -> str:
    """Render the picker's summary/toggle row, truncated to width.

    Layout: `selected: N files | <size> ~<tok>   verb: V  dest: X  tree: on|off`.
    On narrow widths, drop the size/token block first, then the verb/dest/tree
    block; the selected-count is the last to go."""
    files, total_bytes, approx_tokens = summary
    left = f"selected: {files} files"
    stats = f" | {_fmt_size(total_bytes)} ~{_fmt_tokens(approx_tokens)}"
    right = (
        f"verb: {_VERB_LABELS[verb]}  "
        f"dest: {_DESTINATION_LABELS[dest]}  "
        f"tree: {'on' if include_tree else 'off'}"
    )
    full_left = left + stats
    gap = "  "
    if len(full_left) + len(gap) + len(right) <= width:
        pad = width - len(full_left) - len(right)
        return full_left + " " * pad + right
    if len(left) + len(gap) + len(right) <= width:
        pad = width - len(left) - len(right)
        return left + " " * pad + right
    if len(full_left) <= width:
        return full_left
    return left[:width]


class PickResult(NamedTuple):
    selected: set[str]
    destination: Destination
    include_tree: bool
    verb: Verb = Verb.BUNDLE
    review_pr: int | None = None


PREVIEW_MAX_BYTES = 2_000_000
PREVIEW_MIN_TERMINAL_WIDTH = 100


def render_preview(
    path: Path | None,
    max_lines: int,
    max_cols: int,
) -> list[str]:
    """Pure helper: return up to max_lines preview lines for `path`.

    - Binary / too-large / unreadable files return a one-line stats label.
    - Text files return their first chunk, decoded with errors="replace"
      and clipped to max_cols per line.
    - Lines are not padded; the caller handles column alignment."""
    if path is None or max_lines <= 0 or max_cols <= 0:
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return [_clip("(unreadable)", max_cols)]
    if is_binary(path):
        return [_clip(f"(binary, {_fmt_size(size)})", max_cols)]
    if size > PREVIEW_MAX_BYTES:
        return [_clip(f"(too large to preview, {_fmt_size(size)})", max_cols)]
    try:
        raw = path.read_bytes()[: min(8192, max_lines * max_cols * 2)]
    except OSError:
        return [_clip("(unreadable)", max_cols)]
    text = raw.decode("utf-8", errors="replace")
    if not text:
        return [_clip("(empty)", max_cols)]
    lines = text.splitlines() or [""]
    return [_clip(line, max_cols) for line in lines[:max_lines]]


def _clip(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    if width <= 1:
        return s[:width]
    return s[: width - 1] + "…"


def _picker_ui(
    stdscr,
    nodes: list[Node],
    root: Path | None = None,
    initial_destination: Destination = Destination.FILE,
    initial_include_tree: bool = True,
    allow_stdout: bool = True,
    allow_git_verbs: bool = False,
) -> PickResult | None:
    """Run the picker loop on an existing `stdscr`.

    `pick()` calls this through `curses.wrapper`. The launcher TUI calls it
    directly, since `curses.wrapper` cannot nest. `allow_stdout=False`
    drops STDOUT from the `d` cycle — meaningless when curses owns the
    terminal. `allow_git_verbs=True` enables the `v` verb cycle through
    Commit/PR/Review in addition to the default Bundle; non-git
    directories should leave it False so the cycle collapses to Bundle."""
    if not nodes:
        dest = initial_destination
        if not allow_stdout and dest == Destination.STDOUT:
            dest = Destination.FILE
        return PickResult(set(), dest, initial_include_tree)

    cycle_allowed: tuple[Destination, ...] | None
    if allow_stdout:
        cycle_allowed = None
    else:
        cycle_allowed = (Destination.FILE, Destination.CLIPBOARD, Destination.SEND)
        if initial_destination == Destination.STDOUT:
            initial_destination = Destination.FILE

    verb_allowed: tuple[Verb, ...] = (
        (Verb.BUNDLE, Verb.COMMIT, Verb.PR, Verb.REVIEW)
        if allow_git_verbs else (Verb.BUNDLE,)
    )

    cancelled = False
    destination = initial_destination
    include_tree = initial_include_tree
    verb = Verb.BUNDLE
    review_pr: int | None = None
    preview_visible = root is not None

    curses.curs_set(0)
    stdscr.keypad(True)
    try:
        curses.mousemask(
            curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED
        )
    except curses.error:
        pass
    theme = _setup_theme()
    cursor_ni: int = 0
    viewport = 0
    filter_active = False
    filter_buf = ""
    sort_mode = "alpha"
    preview_cache: dict[tuple[int, int, int], list[str]] = {}

    def _sort_key(n: Node):
        return (-n.size, n.name)

    while True:
        sk = _sort_key if sort_mode == "size" else None
        if filter_buf:
            q = filter_buf.lower()
            matches = [i for i, n in enumerate(nodes) if q in n.rel.lower()]
            if sk is not None:
                matches.sort(key=lambda i: sk(nodes[i]))
            visible = matches
        else:
            visible = visible_indices(nodes, sort_key=sk)

        h, w = stdscr.getmaxyx()
        list_h = max(1, h - 3)
        if visible:
            if cursor_ni not in visible:
                cursor_ni = visible[0]
            cursor_pos = visible.index(cursor_ni)
            if cursor_pos < viewport:
                viewport = cursor_pos
            elif cursor_pos >= viewport + list_h:
                viewport = cursor_pos - list_h + 1
        else:
            cursor_pos = 0
            viewport = 0

        show_preview = (
            preview_visible
            and root is not None
            and w >= PREVIEW_MIN_TERMINAL_WIDTH
        )
        if show_preview:
            tree_w = max(40, int(w * 0.6))
            preview_x = tree_w + 1
            preview_w = max(0, w - preview_x)
        else:
            tree_w = w
            preview_x = preview_w = 0

        stdscr.erase()
        if not visible:
            try:
                stdscr.addstr(0, 0, "(no matches)", theme["dim"])
            except curses.error:
                pass
        for row in range(list_h):
            vi = viewport + row
            if vi >= len(visible):
                break
            ni = visible[vi]
            n = nodes[ni]
            check = "[x]" if n.checked else "[ ]"
            indent = "" if filter_buf else "  " * n.depth
            if n.is_dir:
                glyph = "v " if n.expanded else "> "
            else:
                glyph = "  "
            label = n.rel if filter_buf else n.name
            if n.is_dir and not label.endswith("/"):
                label += "/"
            prefix = f"{check} {indent}{glyph}{label}"
            if n.read_error:
                stat_variants = ("?  ?  ?", "?  ?", "?")
            else:
                s_size = _fmt_size(n.size)
                s_loc = _fmt_loc(n.loc)
                s_tok = _fmt_tokens(n.tokens)
                stat_variants = (
                    f"{s_size}  {s_loc}  {s_tok}",
                    f"{s_size}  {s_loc}",
                    s_size,
                )
            chosen_suffix = ""
            gap = 2
            avail = tree_w - 1
            for cand in stat_variants:
                if len(prefix) + gap + len(cand) <= avail:
                    chosen_suffix = cand
                    break
            if chosen_suffix:
                pad_len = avail - len(prefix) - len(chosen_suffix)
                main = prefix + " " * pad_len
            else:
                main = prefix[:avail]
            if verb != Verb.BUNDLE:
                attr = theme["dim"]
            elif n.checked:
                attr = theme["checked"]
            elif n.is_dir:
                attr = theme["dir"]
            else:
                attr = theme["file"]
            suffix_attr = theme["dim"]
            if vi == cursor_pos:
                attr |= theme["cursor"]
                suffix_attr |= theme["cursor"]
            try:
                stdscr.addstr(row, 0, main, attr)
                if chosen_suffix:
                    stdscr.addstr(row, len(main), chosen_suffix, suffix_attr)
            except curses.error:
                pass

        if show_preview and preview_w > 0:
            cur_node = nodes[cursor_ni]
            preview_lines: list[str]
            if cur_node.is_dir or root is None:
                preview_lines = []
            else:
                cache_key = (cursor_ni, list_h, preview_w)
                cached = preview_cache.get(cache_key)
                if cached is None:
                    cached = render_preview(
                        root / cur_node.rel, list_h, preview_w,
                    )
                    preview_cache[cache_key] = cached
                preview_lines = cached
            for row, line in enumerate(preview_lines[:list_h]):
                try:
                    stdscr.addstr(row, preview_x, line, theme["dim"])
                except curses.error:
                    pass

        summary = compute_summary(nodes)
        if h >= 3:
            footer = format_footer(
                destination, include_tree, summary, max(1, w - 1), verb,
            )
            try:
                stdscr.addstr(h - 3, 0, footer, theme["dim"])
            except curses.error:
                pass
        if filter_active or filter_buf:
            try:
                stdscr.addstr(h - 2, 0, "/ ", theme["filter"])
                stdscr.addstr(filter_buf[: max(0, w - 3)], theme["filter"])
            except curses.error:
                pass
        if len(verb_allowed) > 1:
            status = (
                "space:toggle  /:filter  v:verb  d:dest  t:tree  p:preview  "
                "s:sort  a:toggle-visible  enter:run  q:quit"
            )
        else:
            status = (
                "space:toggle  /:filter  d:dest  t:tree  p:preview  "
                "s:sort  a:toggle-visible  enter:write  q:quit"
            )
        try:
            stdscr.addstr(h - 1, 0, status[: w - 1], theme["dim"])
        except curses.error:
            pass
        stdscr.refresh()

        ch = stdscr.getch()

        if ch == curses.KEY_RESIZE:
            continue

        if filter_active:
            filter_active, filter_buf = _apply_filter_key(
                ch, filter_active, filter_buf
            )
            continue

        if ch == curses.KEY_MOUSE:
            try:
                _, _mx, my, _mz, bstate = curses.getmouse()
            except curses.error:
                continue
            if 0 <= my < list_h:
                new_pos = viewport + my
                if 0 <= new_pos < len(visible):
                    cursor_pos = new_pos
                    cursor_ni = visible[cursor_pos]
                    n = nodes[cursor_ni]
                    if bstate & curses.BUTTON1_DOUBLE_CLICKED and n.is_dir:
                        n.expanded = not n.expanded
                    elif verb == Verb.BUNDLE:
                        cascade_check(nodes, cursor_ni, not n.checked)
            continue

        if visible and ch in (curses.KEY_DOWN, ord("j")):
            cursor_pos = min(cursor_pos + 1, len(visible) - 1)
            cursor_ni = visible[cursor_pos]
        elif visible and ch in (curses.KEY_UP, ord("k")):
            cursor_pos = max(cursor_pos - 1, 0)
            cursor_ni = visible[cursor_pos]
        elif visible and ch == curses.KEY_NPAGE:
            cursor_pos = min(cursor_pos + list_h, len(visible) - 1)
            cursor_ni = visible[cursor_pos]
        elif visible and ch == curses.KEY_PPAGE:
            cursor_pos = max(cursor_pos - list_h, 0)
            cursor_ni = visible[cursor_pos]
        elif visible and ch in (curses.KEY_HOME, ord("g")):
            cursor_pos = 0
            cursor_ni = visible[0]
        elif visible and ch in (curses.KEY_END, ord("G")):
            cursor_pos = len(visible) - 1
            cursor_ni = visible[cursor_pos]
        elif visible and ch == ord(" ") and verb == Verb.BUNDLE:
            cascade_check(nodes, cursor_ni, not nodes[cursor_ni].checked)
        elif visible and ch in (curses.KEY_RIGHT, ord("l")):
            if nodes[cursor_ni].is_dir:
                nodes[cursor_ni].expanded = True
        elif visible and ch in (curses.KEY_LEFT, ord("h")):
            n = nodes[cursor_ni]
            if n.is_dir and n.expanded:
                n.expanded = False
            elif n.parent is not None:
                cursor_ni = n.parent
        elif ch == ord("E"):
            for nn in nodes:
                if nn.is_dir:
                    nn.expanded = True
        elif ch == ord("C"):
            for nn in nodes:
                if nn.is_dir:
                    nn.expanded = False
        elif ch == ord("/"):
            filter_active = True
        elif ch == 27:
            filter_buf = ""
        elif ch == ord("a") and verb == Verb.BUNDLE:
            any_unchecked = any(not nodes[v].checked for v in visible)
            for v in visible:
                cascade_check(nodes, v, any_unchecked)
        elif ch == ord("s"):
            sort_mode = "size" if sort_mode == "alpha" else "alpha"
        elif ch == ord("d"):
            destination = cycle_destination(destination, cycle_allowed)
        elif ch == ord("t"):
            include_tree = not include_tree
        elif ch == ord("p"):
            preview_visible = not preview_visible
        elif ch == ord("v"):
            verb = cycle_verb(verb, verb_allowed)
        elif ch in (10, 13):
            if verb == Verb.REVIEW:
                pr = _prompt_pr_number(stdscr)
                if pr is None:
                    continue
                review_pr = pr
            break
        elif ch in (ord("q"), 3):
            cancelled = True
            break

    if cancelled:
        return None
    if verb == Verb.BUNDLE:
        selected = {n.rel for n in nodes if n.checked and not n.is_dir}
    else:
        selected = set()
    return PickResult(
        selected=selected,
        destination=destination,
        include_tree=include_tree,
        verb=verb,
        review_pr=review_pr,
    )


def pick(
    nodes: list[Node],
    root: Path | None = None,
    initial_destination: Destination = Destination.FILE,
    initial_include_tree: bool = True,
    allow_git_verbs: bool = False,
) -> PickResult | None:
    """CLI entry: run `_picker_ui` under its own `curses.wrapper`.

    The launcher TUI calls `_picker_ui` directly (curses.wrapper can't nest).
    `allow_git_verbs=True` enables the `v` verb cycle through Commit/PR/Review
    in the footer; callers should pass True only when `root` is a git repo.
    """
    if not nodes:
        return PickResult(set(), initial_destination, initial_include_tree)
    holder: dict = {"result": None}
    try:
        curses.wrapper(
            lambda stdscr: holder.__setitem__(
                "result",
                _picker_ui(stdscr, nodes, root, initial_destination,
                           initial_include_tree, allow_stdout=True,
                           allow_git_verbs=allow_git_verbs),
            )
        )
    except KeyboardInterrupt:
        return None
    return holder["result"]


# ---------- output ----------

def render_ascii_tree(paths: list[str], repo_name: str) -> str:
    tree: dict = {}
    for p in paths:
        cur = tree
        for part in p.split("/"):
            cur = cur.setdefault(part, {})
    lines = [f"{repo_name}/"]

    def walk(d: dict, prefix: str) -> None:
        entries = sorted(d.items(), key=lambda kv: (not bool(kv[1]), kv[0]))
        for i, (name, sub) in enumerate(entries):
            last = i == len(entries) - 1
            connector = "└── " if last else "├── "
            suffix = "/" if sub else ""
            lines.append(f"{prefix}{connector}{name}{suffix}")
            if sub:
                walk(sub, prefix + ("    " if last else "│   "))

    walk(tree, "")
    return "\n".join(lines)


def render_output(
    repo_name: str,
    repo_root: Path,
    files: list[str],
    tree: str | None,
) -> str:
    ts = datetime.now().isoformat(timespec="seconds")
    parts = [
        f"This is a packed representation of selected files from {repo_name}, "
        f"bundled on {ts}. Each file is wrapped in <file path=\"...\"> tags.",
        "",
    ]
    if tree:
        parts.append("<file_tree>")
        parts.append(tree)
        parts.append("</file_tree>")
        parts.append("")
    for rel in files:
        abs_p = repo_root / rel
        try:
            content = abs_p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            content = f"<<read error: {e}>>"
        parts.append(f'<file path="{rel}">')
        parts.append(content)
        if not content.endswith("\n"):
            parts.append("")
        parts.append("</file>")
        parts.append("")
    return "\n".join(parts)


def _unique_path(base: Path, *, start: int = 1) -> Path:
    """Return `base` if free, else `base` with `-{n}` inserted before the suffix.

    `start` is the first index tried on collision. The four pick-* helpers use
    different starts (file outputs begin at 2 — `foo-2.txt`; rebuild target
    dirs begin at 1 — `foo-1`)."""
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    n = start
    while True:
        candidate = base.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def pick_output_path(repo_name: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return _unique_path(Path.cwd() / f"{repo_name}-{ts}.txt", start=2)


def _slug(s: str) -> str:
    """Make a branch name safe for use in a filename. `/` becomes `__` so
    `feat/foo` stays distinguishable from a literal `feat-foo` branch."""
    s = s.replace("/", "__")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "branch"


def pick_git_output_path(repo_name: str, branch_label: str, kind: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{_slug(repo_name)}-{_slug(branch_label)}-{kind}-{ts}"
    return _unique_path(Path.cwd() / f"{stem}.txt", start=2)


_REBUILD_FILE_HEADER_RE = re.compile(
    r"^This is a packed representation of selected files from (.+?), bundled on "
)
_REBUILD_GIT_HEADER_RE = re.compile(
    r"^This is a packed representation of git context for "
)
_REBUILD_FILE_OPEN_RE = re.compile(r'^<file path="(.+)">$')


def parse_bundle(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Parse a nomnom file bundle into (repo_name, [(rel_path, content), ...]).

    Raises ValueError on a malformed bundle, a git-context bundle, or any path
    that would escape the target folder (absolute, leading slash, `..` segment).
    Known limitation: file contents containing a literal `</file>` line on its
    own line will end the block early — the bundle format itself has no escape.
    """
    if not text.strip():
        raise ValueError("input is empty")
    first_line = text.splitlines()[0] if text else ""
    if _REBUILD_GIT_HEADER_RE.match(first_line):
        raise ValueError(
            "git-context bundles are not file bundles; nothing to rebuild"
        )
    m = _REBUILD_FILE_HEADER_RE.match(first_line)
    if not m:
        raise ValueError("input does not look like a nomnom bundle")
    repo_name = m.group(1).strip()
    if not repo_name:
        raise ValueError("bundle header has an empty repo name")

    files: list[tuple[str, str]] = []
    lines = text.splitlines()
    i = 0
    n = len(lines)
    in_tree = False
    while i < n:
        line = lines[i]
        if line == "<file_tree>":
            in_tree = True
            i += 1
            continue
        if line == "</file_tree>":
            in_tree = False
            i += 1
            continue
        if in_tree:
            i += 1
            continue
        om = _REBUILD_FILE_OPEN_RE.match(line)
        if not om:
            i += 1
            continue
        rel = om.group(1)
        _validate_rebuild_path(rel)
        i += 1
        body: list[str] = []
        closed = False
        while i < n:
            if lines[i] == "</file>":
                closed = True
                i += 1
                break
            body.append(lines[i])
            i += 1
        if not closed:
            raise ValueError(f"unterminated <file> block for {rel!r}")
        # render_output always emits exactly one blank line before </file>,
        # whether or not the source content ended with a newline — the format
        # is lossy at that boundary. We default to a trailing newline (the
        # common source-file convention); files that originally lacked one
        # will gain one on rebuild.
        content = "\n".join(body)
        files.append((rel, content))

    if not files:
        raise ValueError("bundle contains no <file> blocks")
    return repo_name, files


def _validate_rebuild_path(rel: str) -> None:
    if not rel:
        raise ValueError("empty file path in bundle")
    if rel.startswith("/") or (len(rel) >= 2 and rel[1] == ":"):
        raise ValueError(f"refusing absolute path in bundle: {rel!r}")
    parts = rel.replace("\\", "/").split("/")
    if any(p in ("", "..") for p in parts):
        raise ValueError(f"refusing unsafe path in bundle: {rel!r}")


def pick_target_dir(cwd: Path, name: str) -> Path:
    return _unique_path(cwd / name, start=1)


# ---------- git/gh helpers ----------

GIT_BUNDLE_WARN_BYTES = 200_000


def _run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)
    return proc.returncode, proc.stdout, proc.stderr


class NomnomError(Exception):
    """Raised when a precondition fails. Carries a user-readable message.

    Catch at the CLI dispatcher to print + exit non-zero; catch at TUI
    entry points to render the message in a modal."""


def _require_git_repo(root: Path) -> None:
    rc, _, _ = _run(["git", "rev-parse", "--show-toplevel"], root)
    if rc != 0:
        raise NomnomError(f"not a git repository: {root}")


def _resolve_git_repo(repo: str) -> tuple[Path, str]:
    """Resolve a user-supplied repo path to (absolute_path, repo_name).

    Raises `NomnomError` on bad input so both the CLI dispatcher and the TUI
    `_execute` modal surface the message uniformly."""
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        raise NomnomError(f"not a directory: {root}")
    if not root.name:
        raise NomnomError(f"cannot derive a repo name from {root}")
    _require_git_repo(root)
    return root, root.name


def _current_branch(root: Path) -> str | None:
    rc, out, _ = _run(["git", "symbolic-ref", "--short", "HEAD"], root)
    if rc != 0:
        return None
    return out.strip() or None


def _short_sha(root: Path) -> str:
    rc, out, _ = _run(["git", "rev-parse", "--short", "HEAD"], root)
    if rc != 0:
        return "nohead"
    return out.strip() or "nohead"


def _changed_files(root: Path, base: str | None) -> list[str]:
    seen: set[str] = set()
    if base is None:
        for cmd in (
            ["git", "diff", "--name-only", "HEAD"],
            ["git", "diff", "--staged", "--name-only"],
            ["git", "ls-files", "--others", "--exclude-standard"],
        ):
            rc, out, _ = _run(cmd, root)
            if rc == 0:
                seen.update(line for line in out.splitlines() if line)
    else:
        rc, out, _ = _run(["git", "diff", "--name-only", f"{base}...HEAD"], root)
        if rc == 0:
            seen.update(line for line in out.splitlines() if line)
    return sorted(seen)


def _require_gh() -> None:
    if shutil.which("gh") is None:
        raise NomnomError(
            "pr requires gh (https://cli.github.com). install it and retry."
        )


def _default_base_branch(root: Path) -> str:
    rc, out, _ = _run(
        ["gh", "repo", "view", "--json", "defaultBranchRef",
         "-q", ".defaultBranchRef.name"],
        root,
    )
    if rc == 0 and out.strip():
        return out.strip()
    return "main"


def render_git_bundle(
    repo_name: str,
    kind: str,
    branch_label: str,
    sections: list[tuple[str, str]],
    tree: str | None,
) -> str:
    ts = datetime.now().isoformat(timespec="seconds")
    parts = [
        f"This is a packed representation of git context for {repo_name} "
        f"({kind}) on {branch_label}, bundled on {ts}. "
        f"Each piece is wrapped in <section name=\"...\"> tags.",
        "",
    ]
    if tree:
        parts.append("<file_tree>")
        parts.append(tree)
        parts.append("</file_tree>")
        parts.append("")
    for name, body in sections:
        parts.append(f'<section name="{name}">')
        parts.append(body)
        if not body.endswith("\n"):
            parts.append("")
        parts.append("</section>")
        parts.append("")
    return "\n".join(parts)


def _emit_git_bundle(
    repo_name: str,
    kind: str,
    branch_label: str,
    sections: list[tuple[str, str]],
    tree: str | None,
    destination: Destination,
    *,
    interactive: bool = True,
) -> int:
    output = render_git_bundle(repo_name, kind, branch_label, sections, tree)
    size = len(output)

    if destination == Destination.STDOUT:
        sys.stdout.write(output)
        sys.stdout.flush()
        print(f"wrote {size:,} bytes to stdout.", file=sys.stderr)
        return 0

    if destination == Destination.SEND:
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        name = f"{repo_name}-{branch_label}-{kind}-{ts}.txt"
        return _bundle_send_via_relay(
            name, output.encode("utf-8"), interactive=interactive,
        )

    print(file=sys.stderr)
    print(f"  sections: {len(sections)}", file=sys.stderr)
    print(f"  size:     {size:,} bytes", file=sys.stderr)

    if destination == Destination.CLIPBOARD:
        clip = detect_clipboard_cmd()
        if clip:
            print(f"  output:   clipboard via {clip[0]}", file=sys.stderr)
        else:
            print("  output:   clipboard (no tool found; will fall back to file)",
                  file=sys.stderr)
    else:
        out_path = pick_git_output_path(repo_name, branch_label, kind)
        print(f"  output:   {out_path}", file=sys.stderr)
    if size > GIT_BUNDLE_WARN_BYTES:
        print(f"  warn:     bundle exceeds {GIT_BUNDLE_WARN_BYTES:,} bytes",
              file=sys.stderr)
    print(file=sys.stderr)

    if destination == Destination.CLIPBOARD:
        if copy_to_clipboard(output):
            print(f"copied {size:,} bytes to clipboard.", file=sys.stderr)
            return 0
        print(
            "no clipboard tool found (pbcopy/wl-copy/xclip/xsel); "
            "falling back to a file.",
            file=sys.stderr,
        )
        out_path = pick_git_output_path(repo_name, branch_label, kind)

    try:
        out_path.write_text(output, encoding="utf-8")
    except OSError as e:
        print(f"error writing output: {e}", file=sys.stderr)
        return 1
    print(f"wrote {out_path}", file=sys.stderr)
    return 0


# ---------- prompts ----------

# ---------- self-editing register / unregister ----------

class _KindSpec(NamedTuple):
    target_name: str
    is_list: bool


KINDS: dict[str, _KindSpec] = {
    "text":   _KindSpec("TEXT_EXTENSIONS", False),
    "binary": _KindSpec("BINARY_EXTENSIONS", False),
    "name":   _KindSpec("KNOWN_TEXT_NAMES", False),
    "secret": _KindSpec("SECRET_PATTERNS", True),
}
MARKER_START = "# --- nomnom:extensions"
MARKER_END = "# --- end nomnom:extensions"
SELF_PATH = Path(__file__).resolve()


def _read_block(path: Path) -> tuple[list[str], int, int]:
    src_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = end = -1
    for i, line in enumerate(src_lines):
        if start == -1 and line.startswith(MARKER_START):
            start = i
        elif start != -1 and line.startswith(MARKER_END):
            end = i
            break
    if start == -1 or end == -1:
        raise RuntimeError(f"marker block not found in {path}")
    return src_lines, start, end


def _parse_block(src_lines: list[str], start: int, end: int) -> dict[str, object]:
    block = "".join(src_lines[start + 1:end])
    tree = ast.parse(block)
    out: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        out[target.id] = ast.literal_eval(node.value)
    return out


def _emit_block(values: dict[str, object]) -> str:
    parts: list[str] = []
    first = True
    for spec in KINDS.values():
        name = spec.target_name
        v = values.get(name)
        if v is None:
            continue
        if not first:
            parts.append("\n")
        first = False
        if spec.is_list:
            items = list(v)
            opener, closer = "[", "]"
        else:
            items = sorted(v)
            opener, closer = "{", "}"
        parts.append(f"{name} = {opener}\n")
        for item in items:
            parts.append(f"    {item!r},\n")
        parts.append(f"{closer}\n")
    return "".join(parts)


def _write_block(
    path: Path, src_lines: list[str], start: int, end: int, new_block: str
) -> None:
    new_lines = src_lines[: start + 1] + [new_block] + src_lines[end:]
    path.write_text("".join(new_lines), encoding="utf-8")


@dataclass
class RegisterResult:
    """Structured outcome of `register_values`.

    Order within each list matches the input order. `wrote` is True iff the
    marker block was rewritten (i.e. at least one add or remove applied)."""
    target_name: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    no_ops: list[str] = field(default_factory=list)
    conflicts: list[tuple[str, str]] = field(default_factory=list)  # (value, other_kind)
    wrote: bool = False


def register_values(
    kind: str,
    values: list[str],
    *,
    remove: bool = False,
    path: Path | None = None,
) -> RegisterResult:
    """Apply add/remove operations to the marker block, returning a structured
    outcome (no printing). Conflicts are detected up front and cause an
    early return with `wrote=False` and the file untouched."""
    p = path if path is not None else SELF_PATH
    src_lines, start, end = _read_block(p)
    parsed = _parse_block(src_lines, start, end)

    spec = KINDS[kind]
    target_name = spec.target_name
    target = parsed[target_name]
    is_list = spec.is_list
    result = RegisterResult(target_name=target_name)

    if not remove:
        for value in values:
            for other_kind, other_spec in KINDS.items():
                if other_kind == kind:
                    continue
                if value in parsed.get(other_spec.target_name, ()):
                    result.conflicts.append((value, other_kind))
        if result.conflicts:
            return result

    for value in values:
        if remove:
            if value not in target:
                result.no_ops.append(value)
                continue
            if is_list:
                target.remove(value)
            else:
                target.discard(value)
            result.removed.append(value)
        else:
            if value in target:
                result.no_ops.append(value)
                continue
            if is_list:
                target.append(value)
            else:
                target.add(value)
            result.added.append(value)

    if not (result.added or result.removed):
        return result

    parsed[target_name] = target
    _write_block(p, src_lines, start, end, _emit_block(parsed))
    result.wrote = True
    return result


def cmd_register(
    kind: str,
    values: list[str],
    remove: bool = False,
    path: Path | None = None,
) -> int:
    """Thin CLI wrapper around `register_values`: prints diagnostics, returns rc."""
    p = path if path is not None else SELF_PATH
    result = register_values(kind, values, remove=remove, path=p)
    if result.conflicts:
        for value, other_kind in result.conflicts:
            other_name = KINDS[other_kind].target_name
            print(
                f"error: {value!r} is already in {other_name}. "
                f"run `nomnom unregister {other_kind} {value}` first.",
                file=sys.stderr,
            )
        return 1
    for value in result.no_ops:
        if remove:
            print(f"! {value!r} not in {result.target_name} (no change)")
        else:
            print(f"! {value!r} already in {result.target_name} (no change)")
    if not result.wrote:
        return 0
    for value in result.added:
        print(f"+ registered {value!r} in {result.target_name}")
    for value in result.removed:
        print(f"- unregistered {value!r} in {result.target_name}")
    print(f"\ndone. review with: git diff {p.name}")
    return 0


def cmd_commit(repo: str, destination: Destination = Destination.FILE) -> int:
    root, repo_name = _resolve_git_repo(repo)

    _, staged_diff, _ = _run(["git", "diff", "--staged"], root)
    _, unstaged_diff, _ = _run(["git", "diff"], root)
    if not staged_diff.strip() and not unstaged_diff.strip():
        raise NomnomError("nothing to commit (no staged or unstaged changes).")

    branch = _current_branch(root)
    branch_label = branch if branch else _short_sha(root)

    _, status, _ = _run(["git", "status", "--porcelain=v1"], root)
    _, staged_stat, _ = _run(["git", "diff", "--staged", "--stat"], root)
    _, unstaged_stat, _ = _run(["git", "diff", "--stat"], root)
    _, untracked, _ = _run(
        ["git", "ls-files", "--others", "--exclude-standard"], root,
    )
    _, recent_commits, _ = _run(["git", "log", "-n", "20"], root)

    diff_summary_parts: list[str] = []
    if staged_stat.strip():
        diff_summary_parts.append("# staged\n" + staged_stat.rstrip())
    if unstaged_stat.strip():
        diff_summary_parts.append("# unstaged\n" + unstaged_stat.rstrip())
    diff_summary = "\n\n".join(diff_summary_parts)

    sections: list[tuple[str, str]] = []
    sections.append(("git_status", status.rstrip() or "(clean)"))
    if diff_summary:
        sections.append(("diff_summary", diff_summary))
    if staged_diff.strip():
        sections.append(("staged_diff", staged_diff.rstrip()))
    if unstaged_diff.strip():
        sections.append(("unstaged_diff", unstaged_diff.rstrip()))
    if untracked.strip():
        sections.append(("untracked", untracked.rstrip()))
    if recent_commits.strip():
        sections.append(("recent_commits", recent_commits.rstrip()))

    changed = _changed_files(root, base=None)
    tree = render_ascii_tree(changed, repo_name) if changed else None

    return _emit_git_bundle(
        repo_name, "commit", branch_label, sections, tree, destination,
    )


def cmd_pr(repo: str, base: str | None, destination: Destination = Destination.FILE) -> int:
    _require_gh()
    root, repo_name = _resolve_git_repo(repo)

    branch = _current_branch(root)
    if branch is None:
        raise NomnomError(
            "HEAD is detached; check out a branch before running pr."
        )

    if base is None:
        base = _default_base_branch(root)

    rc, log_out, log_err = _run(["git", "log", f"{base}...HEAD"], root)
    if rc != 0:
        raise NomnomError(f"git log {base}...HEAD failed: {log_err.strip()}")
    _, diff_stat, _ = _run(["git", "diff", f"{base}...HEAD", "--stat"], root)
    _, diff_full, _ = _run(["git", "diff", f"{base}...HEAD"], root)

    pr_rc, pr_view, _ = _run(
        ["gh", "pr", "view", "--json",
         "number,url,title,body,headRefName,baseRefName"],
        root,
    )
    existing_pr_section: str
    pr_url: str | None = None
    if pr_rc == 0 and pr_view.strip():
        try:
            data = json.loads(pr_view)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            pr_url = data.get("url")
            existing_pr_section = (
                f"#{data.get('number', '?')}: {data.get('title', '')}\n"
                f"url:  {data.get('url', '')}\n"
                f"head: {data.get('headRefName', '')}\n"
                f"base: {data.get('baseRefName', '')}\n\n"
                f"{(data.get('body') or '').rstrip()}"
            )
        else:
            existing_pr_section = "none"
    else:
        existing_pr_section = "none"

    _, merged_json, _ = _run(
        ["gh", "pr", "list", "--state", "merged", "--limit", "10",
         "--json", "title,body"],
        root,
    )
    merged_lines: list[str] = []
    if merged_json.strip():
        try:
            merged = json.loads(merged_json)
        except json.JSONDecodeError:
            merged = []
        for item in merged or []:
            title = (item.get("title") or "").strip()
            body = (item.get("body") or "").strip()
            if len(body) > 500:
                body = body[:500] + "…"
            merged_lines.append(f"## {title}\n{body}".rstrip())
    recent_merged = "\n\n".join(merged_lines) if merged_lines else "(none)"

    branch_info = f"branch: {branch}\nbase:   {base}"
    if pr_url:
        branch_info += f"\npr:     {pr_url}"

    sections: list[tuple[str, str]] = [
        ("branch_info", branch_info),
        ("commits_since_base", log_out.rstrip() or "(none)"),
    ]
    if diff_stat.strip():
        sections.append(("diff_summary", diff_stat.rstrip()))
    if diff_full.strip():
        sections.append(("diff", diff_full.rstrip()))
    sections.append(("existing_pr", existing_pr_section))
    sections.append(("recent_merged_prs", recent_merged))

    changed = _changed_files(root, base=base)
    tree = render_ascii_tree(changed, repo_name) if changed else None

    return _emit_git_bundle(
        repo_name, "pr", branch, sections, tree, destination,
    )


# ---------- review ----------

_REVIEW_TIMELINE_KEEP = {
    "review_requested",
    "assigned",
    "unassigned",
    "ready_for_review",
    "convert_to_draft",
    "merged",
    "closed",
    "reopened",
    "head_ref_force_pushed",
}

_REVIEW_THREADS_QUERY = """\
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          isResolved
          isOutdated
          path
          line
          comments(first: 50) {
            nodes {
              author { login }
              body
              createdAt
            }
          }
        }
      }
    }
  }
}
"""


def _format_pr_meta(pr: dict) -> str:
    author = ((pr.get("author") or {}).get("login")) or "?"
    labels = ", ".join(
        l.get("name", "") for l in (pr.get("labels") or []) if l
    ) or "(none)"
    milestone = ((pr.get("milestone") or {}).get("title")) or "(none)"
    return "\n".join([
        f"number:    #{pr.get('number', '?')}",
        f"title:     {pr.get('title', '')}",
        f"url:       {pr.get('url', '')}",
        f"author:    @{author}",
        f"state:     {pr.get('state', '?')}",
        f"draft:     {'true' if pr.get('isDraft') else 'false'}",
        f"head:      {pr.get('headRefName', '')}",
        f"base:      {pr.get('baseRefName', '')}",
        f"labels:    {labels}",
        f"milestone: {milestone}",
        f"created:   {pr.get('createdAt', '')}",
        f"updated:   {pr.get('updatedAt', '')}",
    ])


def _format_linked_issues(issues: list) -> str:
    if not issues:
        return ""
    lines: list[str] = []
    for it in issues:
        num = it.get("number", "?")
        title = (it.get("title") or "").strip()
        url = it.get("url") or ""
        state = it.get("state")
        head = f"#{num}"
        if state:
            head += f" [{state}]"
        line = f"{head}: {title}".rstrip()
        if url:
            line += f"  {url}"
        lines.append(line)
    return "\n".join(lines)


def _format_commits(commits: list) -> str:
    if not commits:
        return ""
    lines: list[str] = []
    for c in commits:
        sha = (c.get("oid") or "")[:7]
        head = (c.get("messageHeadline") or "").strip()
        lines.append(f"{sha}  {head}".rstrip())
    return "\n".join(lines)


def _format_diff_summary(files: list) -> str:
    if not files:
        return ""
    width = max((len(f.get("path", "")) for f in files), default=0)
    lines: list[str] = []
    total_add = total_del = 0
    for f in files:
        path = f.get("path", "")
        add = int(f.get("additions") or 0)
        rem = int(f.get("deletions") or 0)
        total_add += add
        total_del += rem
        lines.append(f"{path:<{width}}  +{add}  -{rem}")
    lines.append(f"total: {len(files)} files, +{total_add} -{total_del}")
    return "\n".join(lines)


def _format_reviews(reviews: list) -> str:
    if not reviews:
        return ""
    parts: list[str] = []
    for r in reviews:
        login = ((r.get("user") or {}).get("login")) or "?"
        state = r.get("state") or ""
        when = r.get("submitted_at") or ""
        body = (r.get("body") or "").rstrip()
        head = f"## @{login} [{state}] {when}".rstrip()
        parts.append(head + "\n" + (body if body else "(no body)"))
    return "\n\n".join(parts)


def _format_issue_comments(comments: list) -> str:
    if not comments:
        return ""
    parts: list[str] = []
    for c in comments:
        login = ((c.get("user") or {}).get("login")) or "?"
        when = c.get("created_at") or ""
        body = (c.get("body") or "").rstrip()
        head = f"## @{login} {when}".rstrip()
        parts.append(head + "\n" + (body if body else "(no body)"))
    return "\n\n".join(parts)


def _format_review_threads(graphql_result: dict) -> str:
    try:
        threads = (
            graphql_result["data"]["repository"]["pullRequest"]
            ["reviewThreads"]["nodes"]
        )
    except (KeyError, TypeError):
        return ""
    if not threads:
        return ""

    def sort_key(t: dict) -> tuple[str, int]:
        return (t.get("path") or "", t.get("line") or 0)

    parts: list[str] = []
    for t in sorted(threads, key=sort_key):
        path = t.get("path") or "?"
        line = t.get("line")
        line_label = str(line) if line is not None else "?"
        tags: list[str] = []
        if t.get("isResolved"):
            tags.append("[resolved]")
        if t.get("isOutdated"):
            tags.append("[outdated]")
        head = f"## {path}:{line_label}"
        if tags:
            head += "  " + " ".join(tags)
        comments = ((t.get("comments") or {}).get("nodes")) or []
        comment_lines: list[str] = []
        for c in comments:
            login = ((c.get("author") or {}).get("login")) or "?"
            when = c.get("createdAt") or ""
            body = (c.get("body") or "").rstrip()
            if body:
                first, *rest = body.split("\n")
                comment_lines.append(f"- @{login} {when}: {first}")
                for line_b in rest:
                    comment_lines.append(f"  {line_b}")
            else:
                comment_lines.append(f"- @{login} {when}: (no body)")
        parts.append(head + "\n" + "\n".join(comment_lines))
    return "\n\n".join(parts)


def _format_timeline(events: list) -> str:
    if not events:
        return ""
    lines: list[str] = []
    for ev in events:
        kind = ev.get("event")
        if kind not in _REVIEW_TIMELINE_KEEP:
            continue
        actor = ((ev.get("actor") or {}).get("login")) or "?"
        when = ev.get("created_at") or ""
        suffix = ""
        if kind == "review_requested":
            req = ev.get("requested_reviewer") or ev.get("requested_team") or {}
            who = req.get("login") or req.get("name") or "?"
            suffix = f": requested @{who}"
        elif kind in ("assigned", "unassigned"):
            who = ((ev.get("assignee") or {}).get("login")) or "?"
            suffix = f": @{who}"
        elif kind == "head_ref_force_pushed":
            before = (ev.get("before") or "")[:7]
            after = (ev.get("after") or "")[:7]
            if before or after:
                suffix = f": {before} -> {after}"
        lines.append(f"{when}  {kind} by @{actor}{suffix}")
    return "\n".join(lines)


def _format_checks(checks: list) -> str:
    if not checks:
        return ""
    width = max((len(c.get("name", "")) for c in checks), default=0)
    lines: list[str] = []
    for c in checks:
        name = c.get("name", "")
        state = c.get("state") or c.get("bucket") or "?"
        wf = c.get("workflow") or ""
        link = c.get("link") or ""
        lines.append(f"{state:<10} {name:<{width}}  {wf}  {link}".rstrip())
    return "\n".join(lines)


def _section(name: str, body: str) -> tuple[str, str]:
    return name, body if body else "(none)"


def cmd_review(
    repo: str, pr_number: int, include_diff: bool,
    destination: Destination = Destination.FILE,
) -> int:
    _require_gh()
    root, repo_name = _resolve_git_repo(repo)

    if pr_number <= 0:
        raise NomnomError(f"pr number must be positive, got {pr_number}")

    rc, owner_repo, err = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner",
         "-q", ".nameWithOwner"],
        root,
    )
    owner_repo = owner_repo.strip()
    if rc != 0 or "/" not in owner_repo:
        raise NomnomError(
            f"could not resolve gh repo: "
            f"{err.strip() or owner_repo or 'unknown error'}"
        )
    owner, name = owner_repo.split("/", 1)

    fields = (
        "number,url,title,body,author,state,headRefName,baseRefName,"
        "labels,milestone,isDraft,createdAt,updatedAt,commits,files,"
        "closingIssuesReferences"
    )
    rc, pr_view, err = _run(
        ["gh", "pr", "view", str(pr_number), "--json", fields], root,
    )
    if rc != 0:
        raise NomnomError(
            f"gh pr view #{pr_number} failed: "
            f"{err.strip() or 'unknown error'}"
        )
    try:
        pr = json.loads(pr_view) if pr_view.strip() else {}
    except json.JSONDecodeError as e:
        raise NomnomError(f"gh pr view returned invalid json: {e}") from e

    def _api_json(args: list[str]) -> object:
        rc, out, _ = _run(args, root)
        if rc != 0 or not out.strip():
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None

    issue_comments = _api_json([
        "gh", "api",
        f"repos/{owner}/{name}/issues/{pr_number}/comments",
        "--paginate",
    ]) or []

    reviews = _api_json([
        "gh", "api",
        f"repos/{owner}/{name}/pulls/{pr_number}/reviews",
        "--paginate",
    ]) or []

    threads_result = _api_json([
        "gh", "api", "graphql",
        "-F", f"owner={owner}",
        "-F", f"repo={name}",
        "-F", f"number={pr_number}",
        "-f", f"query={_REVIEW_THREADS_QUERY}",
    ]) or {}

    timeline = _api_json([
        "gh", "api",
        f"repos/{owner}/{name}/issues/{pr_number}/timeline",
        "--paginate",
    ]) or []

    _, checks_json, _ = _run(
        ["gh", "pr", "checks", str(pr_number),
         "--json", "bucket,name,state,workflow,link"],
        root,
    )
    try:
        checks = json.loads(checks_json) if checks_json.strip() else []
    except json.JSONDecodeError:
        checks = []

    diff_full = ""
    if include_diff:
        _, diff_full, _ = _run(
            ["gh", "pr", "diff", str(pr_number)], root,
        )

    sections: list[tuple[str, str]] = [
        _section("pr_meta", _format_pr_meta(pr)),
        _section("pr_body", (pr.get("body") or "").rstrip()),
        _section(
            "linked_issues",
            _format_linked_issues(pr.get("closingIssuesReferences") or []),
        ),
        _section("commits", _format_commits(pr.get("commits") or [])),
        _section("diff_summary", _format_diff_summary(pr.get("files") or [])),
    ]
    if include_diff:
        sections.append(_section("diff", diff_full.rstrip()))
    sections.extend([
        _section("reviews", _format_reviews(reviews)),
        _section("issue_comments", _format_issue_comments(issue_comments)),
        _section("review_comments", _format_review_threads(threads_result)),
        _section("timeline", _format_timeline(timeline)),
        _section("checks", _format_checks(checks)),
    ])

    changed = sorted({
        f.get("path") or ""
        for f in (pr.get("files") or [])
        if f.get("path")
    })
    tree = render_ascii_tree(changed, repo_name) if changed else None

    branch_label = f"pr-{pr_number}"
    return _emit_git_bundle(
        repo_name, "review", branch_label, sections, tree, destination,
    )


def cmd_rebuild(bundle_path: str | None, name: str | None) -> int:
    if bundle_path is None:
        text = sys.stdin.read()
        source_label = "<stdin>"
    else:
        p = Path(bundle_path).expanduser()
        if not p.is_file():
            print(f"error: not a file: {p}", file=sys.stderr)
            return 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"error: cannot read {p}: {e}", file=sys.stderr)
            return 1
        source_label = str(p)

    try:
        repo_name, files = parse_bundle(text)
    except ValueError as e:
        print(f"error: {e} (from {source_label})", file=sys.stderr)
        return 1

    folder_name = (name or repo_name).strip()
    if not folder_name:
        print("error: empty target folder name", file=sys.stderr)
        return 1
    # Sanitize: collapse path separators so --name foo/bar can't escape cwd.
    if "/" in folder_name or "\\" in folder_name:
        print(
            f"error: --name must not contain path separators: {folder_name!r}",
            file=sys.stderr,
        )
        return 1

    cwd = Path.cwd()
    target = pick_target_dir(cwd, folder_name)
    target_resolved = target.resolve()
    target.mkdir(parents=True)

    written = 0
    for rel, content in files:
        dest = (target / rel).resolve()
        # Belt-and-braces: even after _validate_rebuild_path, confirm the
        # resolved write target is inside the freshly created folder.
        try:
            dest.relative_to(target_resolved)
        except ValueError:
            print(
                f"error: refusing to write outside target: {rel!r}",
                file=sys.stderr,
            )
            return 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written += 1

    try:
        rel_target: Path | str = target.relative_to(cwd)
    except ValueError:
        rel_target = target
    print(f"rebuilt {written} files in {rel_target}/", file=sys.stderr)
    return 0


# ---------- encryption ----------
#
# Self-contained passphrase-based encryption for nomnom bundle .txt files
# (or any file). Stdlib-only by design: scrypt for key derivation, an
# HMAC-SHA256 keystream for the stream cipher (HMAC-SHA256 is a PRF under
# its key, so HMAC(key, nonce || counter) is a sound stream construction),
# and encrypt-then-HMAC for authentication. Wrong-passphrase / tamper /
# truncation all surface as a single `ValueError("authentication failed")`
# raised before any output file is written.

_NMNM_MAGIC = b"NMNM\x01"  # 4-byte tag + 1-byte format version
_NMNM_SALT_LEN = 16
_NMNM_NONCE_LEN = 12
_NMNM_MAC_LEN = 32
_NMNM_HEADER_LEN = len(_NMNM_MAGIC) + _NMNM_SALT_LEN + _NMNM_NONCE_LEN + _NMNM_MAC_LEN

# scrypt parameters: ~100ms on a modern laptop; fine for interactive use.
_NMNM_SCRYPT_N = 2 ** 16
_NMNM_SCRYPT_R = 8
_NMNM_SCRYPT_P = 1
_NMNM_KEY_LEN = 64  # 32 bytes enc_key + 32 bytes mac_key


def _derive_keys(passphrase: str, salt: bytes) -> tuple[bytes, bytes]:
    dk = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=_NMNM_SCRYPT_N,
        r=_NMNM_SCRYPT_R,
        p=_NMNM_SCRYPT_P,
        maxmem=128 * 1024 * 1024,
        dklen=_NMNM_KEY_LEN,
    )
    return dk[:32], dk[32:]


def _stream_xor(enc_key: bytes, nonce: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    block_size = 32  # HMAC-SHA256 output
    counter = 0
    for off in range(0, len(data), block_size):
        ks = hmac.new(
            enc_key,
            nonce + counter.to_bytes(4, "big"),
            hashlib.sha256,
        ).digest()
        chunk = data[off:off + block_size]
        for i, b in enumerate(chunk):
            out[off + i] = b ^ ks[i]
        counter += 1
    return bytes(out)


def _pack_payload(name: str, body: bytes) -> bytes:
    header = json.dumps({"name": name, "v": 1}, ensure_ascii=False).encode("utf-8")
    if len(header) > 0xFFFF:
        raise ValueError("payload header too large")
    return len(header).to_bytes(2, "big") + header + body


def _unpack_payload(payload: bytes) -> tuple[str, bytes]:
    if len(payload) < 2:
        raise ValueError("payload truncated")
    header_len = int.from_bytes(payload[:2], "big")
    if len(payload) < 2 + header_len:
        raise ValueError("payload truncated")
    try:
        header = json.loads(payload[2:2 + header_len].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"payload header is not valid JSON: {e}") from e
    name = header.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("payload header missing 'name'")
    # Defense in depth: the embedded name is supposed to be a basename.
    if "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"refusing unsafe name in payload: {name!r}")
    return name, payload[2 + header_len:]


def seal_bytes(
    data: bytes,
    name: str,
    passphrase: str,
    *,
    _salt: bytes | None = None,
    _nonce: bytes | None = None,
) -> bytes:
    """Encrypt `data` under `passphrase`, embedding `name` in the payload.

    `_salt` and `_nonce` are test hooks; production callers leave them None
    so they're freshly random per call.
    """
    if not passphrase:
        raise ValueError("passphrase must not be empty")
    salt = _salt if _salt is not None else secrets.token_bytes(_NMNM_SALT_LEN)
    nonce = _nonce if _nonce is not None else secrets.token_bytes(_NMNM_NONCE_LEN)
    enc_key, mac_key = _derive_keys(passphrase, salt)
    payload = _pack_payload(name, data)
    ciphertext = _stream_xor(enc_key, nonce, payload)
    mac_input = _NMNM_MAGIC + salt + nonce + ciphertext
    mac = hmac.new(mac_key, mac_input, hashlib.sha256).digest()
    return _NMNM_MAGIC + salt + nonce + mac + ciphertext


def open_bytes(blob: bytes, passphrase: str) -> tuple[str, bytes]:
    """Verify and decrypt `blob` produced by `seal_bytes`.

    Returns (original_name, original_bytes). Raises ValueError on any
    structural problem, wrong passphrase, or tampering.
    """
    if len(blob) < _NMNM_HEADER_LEN:
        raise ValueError("ciphertext too short")
    if blob[:len(_NMNM_MAGIC)] != _NMNM_MAGIC:
        raise ValueError("not a nomnom-encrypted file (bad magic)")
    off = len(_NMNM_MAGIC)
    salt = blob[off:off + _NMNM_SALT_LEN]; off += _NMNM_SALT_LEN
    nonce = blob[off:off + _NMNM_NONCE_LEN]; off += _NMNM_NONCE_LEN
    mac = blob[off:off + _NMNM_MAC_LEN]; off += _NMNM_MAC_LEN
    ciphertext = blob[off:]
    enc_key, mac_key = _derive_keys(passphrase, salt)
    expected = hmac.new(
        mac_key, _NMNM_MAGIC + salt + nonce + ciphertext, hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, mac):
        raise ValueError("authentication failed")
    payload = _stream_xor(enc_key, nonce, ciphertext)
    name, body = _unpack_payload(payload)
    return name, body


def _pick_decrypted_path(parent: Path, name: str) -> Path:
    return _unique_path(parent / name, start=1)


# ----- identity + known-peer (TOFU) store -----

def _nomnom_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    d = root / "nomnom"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_identity() -> dict:
    """Return this machine's identity, creating/upgrading it on first use.

    Identity is {device_id, name, ik_priv, ik_pub} where ik_* is a long-term
    Diffie-Hellman keypair (hex) used to authenticate transfers under TOFU.
    """
    path = _nomnom_config_dir() / "identity.json"
    ident: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            ident = loaded
    except (OSError, json.JSONDecodeError):
        pass
    changed = False
    if not ident.get("device_id"):
        ident["device_id"] = secrets.token_hex(8)
        changed = True
    if not ident.get("name"):
        ident["name"] = socket.gethostname() or "nomnom"
        changed = True
    if not ident.get("ik_priv") or not ident.get("ik_pub"):
        priv, pub = _dh_keypair()
        ident["ik_priv"] = format(priv, "x")
        ident["ik_pub"] = format(pub, "x")
        changed = True
    if changed:
        try:
            _atomic_write_text(path, json.dumps(ident))
        except OSError:
            pass
    return ident


_KNOWN_PEERS_SCHEMA = 2


def _known_peers_path() -> Path:
    return _nomnom_config_dir() / "known_peers.json"


def _is_inside_git_repo(root: Path) -> bool:
    """True if `root` is inside a git worktree.

    Walk parents looking for `.git` (a directory in the worktree root, or a
    file for linked worktrees). Cheaper than shelling out and avoids
    blocking the curses event loop on a subprocess.
    """
    try:
        candidate = root.resolve()
    except OSError:
        return False
    for cur in (candidate, *candidate.parents):
        if (cur / ".git").exists():
            return True
    return False


def _atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Write `content` to `path` atomically.

    Uses `tempfile.mkstemp` so concurrent writers each get a unique tmpfile
    (a fixed `*.tmp` name lets two processes race on unlink/open and clobber
    each other's update). The tmpfile is created in the destination dir so
    `os.replace` is a same-filesystem rename. `os.fchmod` enforces `mode`
    immediately; the umask window is closed before any content lands.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _save_known_peers(peers: dict) -> None:
    """Write the peer dict wrapped with the current schema version."""
    body = {"version": _KNOWN_PEERS_SCHEMA, "peers": peers}
    _atomic_write_text(_known_peers_path(), json.dumps(body, indent=2))


def _migrate_known_peers_v2(legacy: dict) -> dict:
    """Wrap a v1 flat-dict pin store in the v2 envelope.

    v1 and v2 derive the recurring session key identically (`_recurring_binding`
    is byte-identical across the two schemas), so old pins remain valid for
    `send`/`receive` and only `pair`-style first-contact metadata is
    affected. Preserving the records avoids re-pairing every device on
    upgrade. Returns the inner peers dict.
    """
    peers = {pid: rec for pid, rec in legacy.items() if isinstance(rec, dict)}
    try:
        _save_known_peers(peers)
    except OSError:
        pass
    if not _in_tui():
        sys.stderr.write(
            f"nomnom: migrated {len(peers)} pinned peer(s) to schema v2.\n",
        )
    return peers


def _load_known_peers() -> dict:
    try:
        data = json.loads(_known_peers_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if (data.get("version") == _KNOWN_PEERS_SCHEMA
            and isinstance(data.get("peers"), dict)):
        return data["peers"]
    # v1: flat {device_id: record} dict. Rewrap, don't wipe.
    return _migrate_known_peers_v2(data)


def _save_known_peer(device_id: str, name: str, ik_pub_hex: str) -> None:
    """Pin (or re-pin) a peer's identity public key."""
    peers = _load_known_peers()
    existing = peers.get(device_id)
    rec = existing if isinstance(existing, dict) else {}
    rec["name"] = name
    rec["ik_pub"] = ik_pub_hex
    rec.setdefault("first_seen", int(time.time()))
    peers[device_id] = rec
    _save_known_peers(peers)


def _known_peer_ik(device_id: str) -> str | None:
    """Return the pinned identity public key (hex) for a peer, or None."""
    rec = _load_known_peers().get(device_id)
    if not isinstance(rec, dict):
        return None
    ik = rec.get("ik_pub")
    return ik if isinstance(ik, str) else None


def _forget_peer(needle: str) -> list:
    """Drop pins matching `needle` (a device id, name, or nickname). Returns dropped names."""
    peers = _load_known_peers()
    dropped = []
    for dev_id in list(peers.keys()):
        rec = peers[dev_id]
        name = rec.get("name", "") if isinstance(rec, dict) else ""
        nickname = rec.get("nickname", "") if isinstance(rec, dict) else ""
        if needle in (dev_id, name, nickname) and needle:
            dropped.append(nickname or name or dev_id)
            del peers[dev_id]
    if dropped:
        _save_known_peers(peers)
    return dropped


def _resolve_peer(needle: str) -> list:
    """Find pinned peers matching `needle`. Returns list of (device_id, record).

    Matches against device_id, name, and nickname. Returns [] if no match,
    [(pid, rec)] if exactly one, or all matches when ambiguous.
    """
    matches = []
    for pid, rec in _load_known_peers().items():
        if not isinstance(rec, dict):
            continue
        if needle == pid:
            matches.append((pid, rec))
            continue
        name = rec.get("name", "")
        nickname = rec.get("nickname", "")
        if needle and (needle == name or needle == nickname):
            matches.append((pid, rec))
    return matches


def _set_peer_nickname(needle: str, nickname: str | None) -> tuple[str, str] | None:
    """Set or clear nickname. Returns (device_id, new_nickname) or None if no match."""
    peers = _load_known_peers()
    target = None
    for pid, rec in peers.items():
        if not isinstance(rec, dict):
            continue
        if needle in (pid, rec.get("name", ""), rec.get("nickname", "")) and needle:
            target = pid
            break
    if target is None:
        return None
    rec = peers[target]
    if not isinstance(rec, dict):
        return None
    if nickname:
        rec["nickname"] = nickname
    else:
        rec.pop("nickname", None)
    peers[target] = rec
    _save_known_peers(peers)
    return target, nickname or ""


# ----- transfer crypto: triple Diffie-Hellman over RFC 3526 group 14 -----

# RFC 3526 group 14 (2048-bit MODP prime), generator g = 2. Used for the
# long-term identity keys and the per-transfer ephemeral keys alike.
_DH_P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
_DH_G = 2
_DH_BYTES = (_DH_P.bit_length() + 7) // 8


def _dh_keypair() -> tuple[int, int]:
    priv = secrets.randbelow(_DH_P - 3) + 2
    return priv, pow(_DH_G, priv, _DH_P)


def _dh_pub_bytes(pub: int) -> bytes:
    return pub.to_bytes(_DH_BYTES, "big")


def _dh_shared(priv: int, peer_pub: int) -> bytes:
    if not 2 <= peer_pub <= _DH_P - 2:
        raise ValueError("invalid DH public value")
    return pow(peer_pub, priv, _DH_P).to_bytes(_DH_BYTES, "big")


def _session_key(*, ik_init_pub: int, ek_init_pub: int,
                 ik_resp_pub: int, ek_resp_pub: int,
                 dh1: bytes, dh2: bytes, dh3: bytes,
                 binding: bytes = b"") -> bytes:
    """Derive a transfer session key from a triple Diffie-Hellman exchange.

    The "initiator" sends first in the relay handshake; the "responder"
    answers. The three DH terms bind both long-term identity keys and both
    throwaway ephemerals:
      dh1 = DH(initiator identity,  responder ephemeral)
      dh2 = DH(initiator ephemeral, responder identity)
      dh3 = DH(initiator ephemeral, responder ephemeral)   # forward secrecy
    Both sides hash the same transcript (all four public keys, then the three
    shared secrets in fixed order) so they arrive at an identical key.

    `binding` is mixed into the transcript so an out-of-band agreement (a
    one-time pairing code, or a deterministic per-peer rendezvous tag) is
    cryptographically tied to the resulting key. Both sides must supply the
    same bytes or they will derive different keys and decryption will fail.
    """
    h = hashlib.sha256()
    h.update(b"nomnom-session-v1")
    if binding:
        h.update(b"\x00bind:")
        h.update(binding)
    for part in (_dh_pub_bytes(ik_init_pub), _dh_pub_bytes(ek_init_pub),
                 _dh_pub_bytes(ik_resp_pub), _dh_pub_bytes(ek_resp_pub),
                 dh1, dh2, dh3):
        h.update(part)
    return h.digest()


def _session_key_initiator(ik_init_priv: int, ek_init_priv: int,
                           ik_init_pub: int, ek_init_pub: int,
                           ik_resp_pub: int, ek_resp_pub: int,
                           *, binding: bytes = b"") -> bytes:
    """Compute the session key from the initiator (first-PUT) side."""
    return _session_key(
        ik_init_pub=ik_init_pub, ek_init_pub=ek_init_pub,
        ik_resp_pub=ik_resp_pub, ek_resp_pub=ek_resp_pub,
        dh1=_dh_shared(ik_init_priv, ek_resp_pub),
        dh2=_dh_shared(ek_init_priv, ik_resp_pub),
        dh3=_dh_shared(ek_init_priv, ek_resp_pub),
        binding=binding,
    )


def _session_key_responder(ik_resp_priv: int, ek_resp_priv: int,
                           ik_resp_pub: int, ek_resp_pub: int,
                           ik_init_pub: int, ek_init_pub: int,
                           *, binding: bytes = b"") -> bytes:
    """Compute the session key from the responder (answering) side."""
    return _session_key(
        ik_init_pub=ik_init_pub, ek_init_pub=ek_init_pub,
        ik_resp_pub=ik_resp_pub, ek_resp_pub=ek_resp_pub,
        dh1=_dh_shared(ek_resp_priv, ik_init_pub),
        dh2=_dh_shared(ik_resp_priv, ek_init_pub),
        dh3=_dh_shared(ek_resp_priv, ek_init_pub),
        binding=binding,
    )


# ---------- TOFU helpers ----------


def _ik_fingerprint(ik_hex: str) -> str:
    """Short, readable fingerprint of an identity public key for display."""
    try:
        # `format(pub, "x")` drops a leading zero nibble, so the hex can be
        # odd-length; pad before decoding (bytes.fromhex rejects odd lengths).
        raw = bytes.fromhex(ik_hex if len(ik_hex) % 2 == 0 else "0" + ik_hex)
    except (ValueError, TypeError):
        return "?"
    d = hashlib.sha256(raw).hexdigest()[:16]
    return ":".join(d[i:i + 4] for i in range(0, 16, 4))


def _tofu_decision(device_id: str, ik_hex: str) -> str:
    """Classify a peer's offered identity key against what we have pinned.

    Returns "new" (never seen), "match" (same as pinned), or "changed".
    """
    pinned = _known_peer_ik(device_id)
    if pinned is None:
        return "new"
    return "match" if pinned == ik_hex else "changed"


def _tofu_assert_main_thread() -> None:
    """Refuse to call input() outside the main thread.

    TUI callers route TOFU through `on_tofu` callbacks marshalled to the
    main thread via _TransferScreen._on_tofu. A worker thread that reaches
    `input()` would corrupt curses-owned stdin; better to fail loudly than
    silently hang.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "TOFU prompt invoked from non-main thread; pass on_tofu callback",
        )


def _tofu_confirm_change(name: str, old_hex, new_hex: str) -> bool:
    """Warn that a known peer's identity key changed and ask to continue."""
    _tofu_assert_main_thread()
    print("", file=sys.stderr)
    print(f"  WARNING: the identity key for {name!r} has CHANGED.",
          file=sys.stderr)
    print("  Expected if it was reinstalled or its config was wiped, but this",
          file=sys.stderr)
    print("  is also exactly what a man-in-the-middle attack looks like.",
          file=sys.stderr)
    if old_hex:
        print(f"    pinned:  {_ik_fingerprint(old_hex)}", file=sys.stderr)
    print(f"    offered: {_ik_fingerprint(new_hex)}", file=sys.stderr)
    try:
        ans = input("  trust the new key and continue? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _tofu_confirm_new(name: str, peer_id: str, new_hex: str) -> bool:
    """First-contact prompt: show fingerprint, ask before pinning.

    The relay-secret holder defines who can reach the rendezvous slot, but
    only the user can decide whether the fingerprint they see matches the
    sender they expect. Verify out-of-band when it matters.
    """
    _tofu_assert_main_thread()
    print("", file=sys.stderr)
    print(f"  first contact with {name!r} (device {peer_id}).",
          file=sys.stderr)
    print(f"    fingerprint: {_ik_fingerprint(new_hex)}", file=sys.stderr)
    print("  verify this fingerprint out-of-band with the sender if it matters.",
          file=sys.stderr)
    try:
        ans = input("  trust and pin this device? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _tofu_check_join(peer: dict) -> bool:
    """TOFU gate for a peer dict {id, ik, name}: prompt if the key changed."""
    if _tofu_decision(peer["id"], peer["ik"]) == "changed":
        return _tofu_confirm_change(
            peer["name"], _known_peer_ik(peer["id"]), peer["ik"],
        )
    return True


# ---------- Relay transport ----------
# nomnom moves files through a Cloudflare Worker the user deploys to their
# own account. The Worker is intentionally dumb: it stores HMAC-authenticated
# PUTs at slot ids, holds GETs open for up to 30 seconds (long-poll), and
# deletes slots on read.
#
# Three slots per recurring transfer: <base>_i (initiator's handshake),
# <base>_r (responder's handshake), <base>_d (ciphertext). The base is
# derived from the long-term DH shared secret between two pinned peers.
#
# First contact runs a separate identity-only pair (`_relay_pair`) at the
# per-relay rendezvous slot, then sender/receiver use the recurring slots
# above.

_RELAY_AUTH_PREFIX = "NMNM-HMAC-SHA256 "
_RELAY_MAX_BODY = 256 * 1024 * 1024          # 256 MiB; free tier caps at 100 MiB at edge
_RELAY_DEFAULT_WAIT_MS = 30_000
_RELAY_REQUEST_TIMEOUT = 35.0                # long-poll cap + a few seconds slack
_RELAY_USER_AGENT = "nomnom-relay-client/1"


# --- relay config (~/.config/nomnom/relay.json) ---


def _relay_config_path() -> Path:
    return _nomnom_config_dir() / "relay.json"


def _load_relay_config() -> dict | None:
    """Read relay.json; return None if absent or malformed."""
    try:
        data = json.loads(_relay_config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    url = data.get("url")
    secret = data.get("secret")
    if not (isinstance(url, str) and isinstance(secret, str)):
        return None
    if not url.startswith(("http://", "https://")):
        return None
    return {"url": url.rstrip("/"), "secret": secret}


def _url_resolves_private(url: str) -> str | None:
    """Return a human-readable reason if `url`'s host resolves to a
    private / link-local / loopback / metadata address, else None.

    Refuses 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8,
    169.254.0.0/16 (link-local + AWS/GCP metadata), ::1, fc00::/7, and
    fe80::/10. Resolves the host via getaddrinfo so a hostname pointing at a
    private IP is caught too.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = parts.hostname
    if not host:
        return None
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        # DNS failure isn't a rejection — the self-test will catch a
        # genuinely unreachable Worker. We only block when resolution
        # SUCCEEDS and lands inside the private/loopback ranges.
        return None
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError:
            continue
        if (
            ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_multicast or ip.is_unspecified
        ):
            return f"host {host!r} resolves to private/loopback address {addr}"
    return None


def _save_relay_config(url: str, secret: str, *, allow_private: bool = False) -> None:
    """Write relay.json via _atomic_write_text. Caller is responsible for validation.

    `allow_private=False` (default) refuses URLs whose host resolves to a
    private / loopback / link-local / metadata address — every later
    send/receive sends device metadata and ciphertext to whatever this
    URL points at, so silently accepting an internal endpoint enables SSRF
    via a socially-engineered `nomnom relay import` blob.
    """
    if not allow_private:
        reason = _url_resolves_private(url)
        if reason is not None:
            raise NomnomError(
                f"refusing relay URL: {reason}. "
                "pass --allow-private if this is intentional (e.g., a local "
                "dev Worker).",
            )
    body = json.dumps({"url": url.rstrip("/"), "secret": secret}, indent=2)
    _atomic_write_text(_relay_config_path(), body)


def _relay_clear_config() -> bool:
    try:
        _relay_config_path().unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# --- join token ---

# Format: <host>#<secret>. Host is bare (no scheme, no path, no port). We always
# assume https://. The `#` separator avoids collision with port-in-host (e.g.
# `relay.foo.com:8443`) and keeps the token paste-friendly.


def _format_join_token(host: str, secret: str) -> str:
    return f"{host}#{secret}"


def _parse_join_token(token: str) -> tuple[str, str]:
    """Parse `host#secret`. Raises NomnomError on anything malformed.

    Strict: rejects schemes, paths, ports, embedded whitespace, multiple `#`,
    or empty halves. The point is to refuse anything that isn't a bare-host
    rendezvous string so we can keep `https://{host}` substitution safe."""
    if not isinstance(token, str):
        raise NomnomError("join token must be a string")
    token = token.strip()
    if not token:
        raise NomnomError("join token is empty")
    if token.count("#") != 1:
        raise NomnomError(
            "join token must contain exactly one '#' separating host and secret",
        )
    host, secret = token.split("#", 1)
    if not host:
        raise NomnomError("join token has empty host")
    if not secret:
        raise NomnomError("join token has empty secret")
    if "://" in host or "/" in host or ":" in host:
        raise NomnomError(
            f"join token host must be bare (no scheme/port/path): {host!r}",
        )
    if any(c.isspace() for c in host) or any(c.isspace() for c in secret):
        raise NomnomError("join token must not contain whitespace")
    return host, secret


# --- HMAC + HTTP ---


def _relay_hmac_headers(secret: str, method: str, path: str) -> dict[str, str]:
    """Authorization header for a single relay request.

    The MAC covers (method, path-without-query, unix_ts). It does NOT cover
    the request body — body integrity comes from the AEAD wrapper inside
    slot_data. The relay's HMAC authenticates the client; it does not vouch
    for the payload. The query string is stripped because it carries
    transient hints (wait=...) that the Worker signs path-only.
    """
    bare_path = path.split("?", 1)[0]
    ts = str(int(time.time()))
    msg = f"{method}\n{bare_path}\n{ts}".encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {
        "Authorization": f"{_RELAY_AUTH_PREFIX}{ts}:{mac}",
        "User-Agent": _RELAY_USER_AGENT,
    }


def _relay_split_url(url: str) -> tuple[str, int, str, bool]:
    """Return (host, port, base_path, is_https) or raise NomnomError."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as e:
        raise NomnomError(f"invalid relay URL: {e}") from e
    if parsed.scheme not in ("http", "https"):
        raise NomnomError(f"relay URL must be http(s): {url!r}")
    if not parsed.hostname:
        raise NomnomError(f"relay URL has no host: {url!r}")
    is_https = parsed.scheme == "https"
    port = parsed.port or (443 if is_https else 80)
    return parsed.hostname, port, parsed.path.rstrip("/"), is_https


def _relay_open(relay: dict) -> http.client.HTTPConnection:
    host, port, _, is_https = _relay_split_url(relay["url"])
    if is_https:
        return http.client.HTTPSConnection(host, port, timeout=_RELAY_REQUEST_TIMEOUT)
    return http.client.HTTPConnection(host, port, timeout=_RELAY_REQUEST_TIMEOUT)


def _relay_full_path(relay: dict, path: str) -> str:
    _, _, base, _ = _relay_split_url(relay["url"])
    return f"{base}{path}" if base else path


def _relay_request(
    relay: dict, method: str, path: str,
    *, body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
    signed: bool = True,
) -> tuple[int, bytes]:
    """One-shot relay request. Returns (status, body). Raises NomnomError on network errors."""
    full_path = _relay_full_path(relay, path)
    headers: dict[str, str] = {}
    if signed:
        headers.update(_relay_hmac_headers(relay["secret"], method, full_path))
    if extra_headers:
        headers.update(extra_headers)
    if body is not None:
        headers["Content-Length"] = str(len(body))
        headers["Content-Type"] = "application/octet-stream"
    conn = _relay_open(relay)
    try:
        try:
            conn.request(method, full_path, body=body, headers=headers)
            resp = conn.getresponse()
            cl = resp.getheader("Content-Length")
            if cl is not None:
                try:
                    declared = int(cl)
                except ValueError as e:
                    raise NomnomError(
                        f"relay returned invalid Content-Length: {cl!r}",
                    ) from e
                if declared > _RELAY_MAX_BODY:
                    raise NomnomError(
                        f"relay returned oversized body "
                        f"({declared} > {_RELAY_MAX_BODY} bytes)",
                    )
            data = resp.read(_RELAY_MAX_BODY + 1)
            if len(data) > _RELAY_MAX_BODY:
                raise NomnomError(
                    f"relay returned oversized body "
                    f"(> {_RELAY_MAX_BODY} bytes)",
                )
            return resp.status, data
        except (OSError, http.client.HTTPException) as e:
            raise NomnomError(f"relay request failed: {e}") from e
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _relay_put_slot(relay: dict, slot_id: str, body: bytes) -> None:
    if len(body) > _RELAY_MAX_BODY:
        raise NomnomError(
            f"payload too large for relay: {len(body)} > {_RELAY_MAX_BODY} bytes",
        )
    status, data = _relay_request(relay, "PUT", f"/slots/{slot_id}", body=body)
    if status == 204:
        return
    _raise_relay_error(status, data)


def _relay_get_slot(relay: dict, slot_id: str, *, wait_ms: int = 0) -> bytes | None:
    """Returns the slot body on 200, None on 404 (poll timeout / empty)."""
    suffix = f"?wait={int(wait_ms)}" if wait_ms > 0 else ""
    status, data = _relay_request(relay, "GET", f"/slots/{slot_id}{suffix}")
    if status == 200:
        return data
    if status == 404:
        return None
    _raise_relay_error(status, data)
    return None  # unreachable


def _relay_delete_slot(relay: dict, slot_id: str) -> None:
    """Best-effort cleanup; swallows network errors."""
    try:
        _relay_request(relay, "DELETE", f"/slots/{slot_id}")
    except NomnomError:
        pass


def _relay_health(relay: dict) -> bool:
    try:
        status, data = _relay_request(relay, "GET", "/health", signed=False)
    except NomnomError:
        return False
    return status == 200 and data.strip() == b"ok"


def _raise_relay_error(status: int, body: bytes) -> None:
    reason = body.decode("utf-8", errors="replace").strip()
    if status == 401:
        if reason == "clock-skew":
            raise NomnomError(
                "relay rejected request (clock skew). sync the system clock.",
            )
        raise NomnomError(
            "relay rejected authentication (wrong secret?). "
            "run `nomnom relay test` to diagnose.",
        )
    if status == 403:
        raise NomnomError(f"relay refused slot id: {reason or '(no detail)'}")
    if status == 409:
        raise NomnomError("relay slot already occupied")
    if status == 410:
        raise NomnomError("relay slot expired")
    if status == 413:
        raise NomnomError("payload too large for relay (256 MB max; 100 MB on free tier)")
    raise NomnomError(f"relay returned HTTP {status}: {reason or '(no body)'}")


# --- cancellable long-poll worker (multi-peer receive) ---


class _RelayPollWorker:
    """One long-poll GET that another thread can cancel via socket shutdown.

    `nomnom receive` with no args races one of these against every pinned
    peer's deterministic-rendezvous slot. First to return a body wins; the
    rest are cancelled.
    """

    def __init__(self, relay: dict, slot_id: str, wait_ms: int) -> None:
        self.relay = relay
        self.slot_id = slot_id
        self.wait_ms = wait_ms
        self._conn: http.client.HTTPConnection | None = None
        self._cancelled = False
        self._lock = threading.Lock()
        # Populated when the worker's GET returns a non-200, non-404 status
        # (401 clock skew, 401 bad secret, 403, 5xx) or a network error. The
        # caller surfaces this when no peer wins the race so the user sees the
        # real reason instead of a generic "no transfer (waited 30s)".
        self.last_error: str | None = None

    def run(self) -> bytes | None:
        """Blocking. Returns body on 200, None on cancel / 404 / network error."""
        full_path = _relay_full_path(
            self.relay,
            f"/slots/{self.slot_id}?wait={int(self.wait_ms)}",
        )
        headers = _relay_hmac_headers(self.relay["secret"], "GET", full_path)
        try:
            conn = _relay_open(self.relay)
        except NomnomError as e:
            self.last_error = str(e)
            return None
        with self._lock:
            if self._cancelled:
                try:
                    conn.close()
                except Exception:
                    pass
                return None
            self._conn = conn
        try:
            conn.request("GET", full_path, headers=headers)
            resp = conn.getresponse()
            cl = resp.getheader("Content-Length")
            if cl is not None:
                try:
                    if int(cl) > _RELAY_MAX_BODY:
                        self.last_error = (
                            f"relay returned oversized body "
                            f"({cl} > {_RELAY_MAX_BODY} bytes)"
                        )
                        return None
                except ValueError:
                    self.last_error = f"relay returned invalid Content-Length: {cl!r}"
                    return None
            data = resp.read(_RELAY_MAX_BODY + 1)
            if len(data) > _RELAY_MAX_BODY:
                self.last_error = (
                    f"relay returned oversized body (> {_RELAY_MAX_BODY} bytes)"
                )
                return None
            if resp.status == 200:
                return data
            if resp.status != 404:
                reason = data.decode("utf-8", errors="replace").strip()[:200]
                self.last_error = (
                    f"relay returned HTTP {resp.status}"
                    + (f": {reason}" if reason else "")
                )
            return None
        except (OSError, http.client.HTTPException) as e:
            self.last_error = f"relay request failed: {e}"
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._conn = None

    def cancel(self) -> None:
        """Force-close the underlying socket; run() returns None on the next IO step."""
        with self._lock:
            self._cancelled = True
            conn = self._conn
        if conn is None:
            return
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            conn.close()
        except Exception:
            pass


# --- relay self-test ---


def _relay_self_test(relay: dict) -> tuple[int, str]:
    """End-to-end check: /health, then a round-trip PUT + GET on a random slot."""
    if not _relay_health(relay):
        return 1, "relay /health unreachable (URL wrong, or worker not deployed)"
    slot = "selftest-" + secrets.token_urlsafe(16).rstrip("=")[:32]
    blob = secrets.token_bytes(1024)
    start = time.monotonic()
    try:
        _relay_put_slot(relay, slot, blob)
        got = _relay_get_slot(relay, slot, wait_ms=5000)
    except NomnomError as e:
        return 1, str(e)
    if got != blob:
        return 1, "relay round-trip body mismatch (HMAC or storage wrong?)"
    elapsed_ms = int((time.monotonic() - start) * 1000)
    return 0, f"relay ok (RTT {elapsed_ms}ms)"


# --- slot derivation + bindings ---


def _slot_b64(digest: bytes) -> str:
    """URL-safe base64 with no padding."""
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _slot_recurring(my_ik_priv: int, their_ik_pub_hex: str) -> str:
    """Base slot id for a recurring (deterministic) transfer.

    Both peers compute the same shared = DH(my_ik_priv, their_ik_pub) by DH
    symmetry, so they arrive at the same slot without coordination.
    """
    their_ik_pub = int(their_ik_pub_hex, 16)
    shared = _dh_shared(my_ik_priv, their_ik_pub)
    digest = hashlib.sha256(b"nomnom-peer-rendezvous-v1" + shared).digest()
    return _slot_b64(digest)


def _recurring_binding(my_ik_pub_hex: str, their_ik_pub_hex: str) -> bytes:
    """Symmetric binding mixed into the recurring session key.

    Sorted concat ensures both peers derive identical bytes regardless of role.
    `format(int, "x")` can yield odd-length hex when the top nibble is zero;
    pad to even length before decoding.
    """
    def _h(s: str) -> bytes:
        return bytes.fromhex(s if len(s) % 2 == 0 else "0" + s)
    a = _h(my_ik_pub_hex)
    b = _h(their_ik_pub_hex)
    pair = a + b if a < b else b + a
    return b"recurring-v1" + pair


# --- first-contact rendezvous (replaces pairing-code slot/binding) ---

_FIRST_CONTACT_BINDING_TAG = b"nomnom-first-contact-v2"
_FIRST_CONTACT_RENDEZVOUS_TAG = b"nomnom-rendezvous-v1"
_FIRST_CONTACT_RESP_TAG = b"nomnom-rendezvous-resp-v1"

# scrypt parameters for the first-contact binding. N=2^16 takes ~200ms on a
# laptop; r=8/p=1/dklen=32 match common defaults. The KDF cost slows offline
# brute-force on a captured transcript by ~10^6x, so even a short random secret
# isn't an instant kill if the relay log ever leaks.
_FIRST_CONTACT_SCRYPT_N = 2 ** 16
_FIRST_CONTACT_SCRYPT_R = 8
_FIRST_CONTACT_SCRYPT_P = 1


@functools.lru_cache(maxsize=4)
def _first_contact_binding(relay_secret: str) -> bytes:
    """Per-relay first-contact binding (scrypt over the relay secret).

    Mixed into the session-key transcript so anyone not holding the relay
    secret cannot derive the same session key; trust still degrades to TOFU
    at the receiver. The scrypt KDF (vs a bare HMAC) raises the cost of an
    offline brute-force on a captured first-contact transcript when the
    relay secret is a human-memorable passphrase.

    Cached because `_slot_first_contact_init` and
    `_slot_first_contact_resp_base` both call this, and `_relay_pair`
    computes it once per pair attempt.
    """
    return hashlib.scrypt(
        password=relay_secret.encode("utf-8"),
        salt=_FIRST_CONTACT_BINDING_TAG,
        n=_FIRST_CONTACT_SCRYPT_N,
        r=_FIRST_CONTACT_SCRYPT_R,
        p=_FIRST_CONTACT_SCRYPT_P,
        # CPython's default maxmem is 32 MiB and N=2^16,r=8 needs exactly 64
        # MiB; pad so the parameters validate on 3.8 too.
        maxmem=128 * 1024 * 1024,
        dklen=32,
    )


def _slot_first_contact_init(relay_secret: str) -> str:
    """Initiator (sender) rendezvous slot. Deterministic per relay.

    Only one first contact in flight at a time per relay; the Worker
    returns 409 on the second writer (surfaced as a clean error).
    """
    digest = hashlib.sha256(
        _FIRST_CONTACT_RENDEZVOUS_TAG + _first_contact_binding(relay_secret),
    ).digest()
    return _slot_b64(digest)


def _hex_to_bytes(s: str) -> bytes:
    """Decode hex with odd-length tolerance.

    `format(int, "x")` drops a leading zero nibble, so identity-key hex
    can be odd-length. Pad before decoding. Caller is responsible for
    catching ValueError on non-hex input.
    """
    return bytes.fromhex(s if len(s) % 2 == 0 else "0" + s)


def _slot_first_contact_resp_base(relay_secret: str, sender_ik_pub_hex: str) -> str:
    """Responder slot base, keyed by the initiator's identity pubkey.

    Used by `_relay_pair_resp_slot` to fork the responder slot into a
    per-initiator namespace so concurrent pair attempts can't collide.

    Trust model: anyone holding the relay secret can read the initiator's
    PUT and race-PUT a malicious responder blob. Defenses are (a) the
    scrypt binding above raises the cost of offline derivation, and (b)
    both sides run a TOFU fingerprint prompt before any pin commits.
    """
    sender_ik_bytes = _hex_to_bytes(sender_ik_pub_hex)
    digest = hashlib.sha256(
        _FIRST_CONTACT_RESP_TAG + sender_ik_bytes + _first_contact_binding(relay_secret),
    ).digest()
    return _slot_b64(digest)


# ---------- Relay orchestrators ----------
# Send / receive run a three-message handshake over the Worker (recurring
# mode, after pairing):
#   sender PUTs <base>_i (init blob: sender's identity + ephemeral pubkeys)
#   receiver GETs <base>_i (long-poll), PUTs <base>_r (resp blob)
#   sender GETs <base>_r (long-poll), encrypts data, PUTs <base>_d
#   receiver GETs <base>_d (long-poll), decrypts, writes
# `binding` mixes the symmetric peer-pair tag into the triple-DH transcript
# so both sides need the same shared context to arrive at the same session
# key.
#
# First contact uses `_relay_pair` (identity-only, race-decided). See its
# block-comment for the protocol.

_RELAY_INIT_MAGIC = "nomnom-init-v1"
_RELAY_RESP_MAGIC = "nomnom-resp-v1"
_RELAY_PAIR_MAGIC = "nomnom-pair-v1"


def _relay_handshake_blob(identity: dict, ek_pub_hex: str, magic: str) -> bytes:
    return json.dumps({
        "magic": magic,
        "ik": identity["ik_pub"],
        "ek": ek_pub_hex,
        "device_id": identity["device_id"],
        "name": identity["name"],
    }).encode("utf-8")


def _relay_parse_handshake(raw: bytes, expect_magic: str) -> dict:
    """Decode a handshake JSON. Raises NomnomError on any malformation."""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise NomnomError(f"relay returned malformed handshake: {e}") from e
    if not isinstance(obj, dict):
        raise NomnomError("relay returned non-object handshake")
    if obj.get("magic") != expect_magic:
        raise NomnomError(
            f"handshake magic mismatch (expected {expect_magic!r}, "
            f"got {obj.get('magic')!r})",
        )
    for key in ("ik", "ek", "device_id", "name"):
        if not isinstance(obj.get(key), str) or not obj[key]:
            raise NomnomError(f"handshake missing/blank {key!r}")
    # ik/ek are odd-or-even-length hex (see _hex_to_bytes). Reject malformed
    # input here so downstream slot derivation and int() conversions can't
    # surface uncaught ValueError on a sender-controlled field.
    for key in ("ik", "ek"):
        try:
            _hex_to_bytes(obj[key])
        except ValueError as e:
            raise NomnomError(f"handshake field {key!r} is not hex") from e
    return obj


def _relay_cancelled(cancel) -> bool:
    return cancel is not None and cancel.is_set()


def _relay_run_tofu(
    decision: str, peer_id: str, peer_name: str, peer_ik_hex: str,
    *, on_tofu,
) -> bool:
    if decision == "match":
        return True
    pinned = _known_peer_ik(peer_id)
    if on_tofu is not None:
        return bool(on_tofu({
            "decision": decision,
            "peer_id": peer_id,
            "peer_name": peer_name,
            "old_ik": pinned,
            "new_ik": peer_ik_hex,
            "fingerprint": _ik_fingerprint(peer_ik_hex),
        }))
    if decision == "new":
        return _tofu_confirm_new(peer_name, peer_id, peer_ik_hex)
    return _tofu_confirm_change(peer_name, pinned, peer_ik_hex)


def _trust_new_callback():
    """Auto-accept on_tofu callback for `--trust-new`.

    Writes an audit line to stderr so operators can spot a pin that
    bypassed the prompt in scripted/cron logs.
    """
    def cb(req: dict) -> bool:
        sys.stderr.write(
            f"  auto-trusting (--trust-new): peer={req['peer_name']!r} "
            f"decision={req['decision']} fingerprint={req['fingerprint']}\n",
        )
        return True
    return cb


def _relay_pin_peer(
    peer_id: str, peer_name: str, peer_ik_hex: str, *, decision: str,
) -> None:
    """Record a successful transfer.

    On TOFU "new" or accepted "changed": save name + ik (first-contact pin or
    re-pin after an accepted key rotation). On "match": leave name and ik
    untouched so a sender can't unilaterally rename a pinned peer; only bump
    the per-transfer counters.
    """
    try:
        if decision in ("new", "changed"):
            _save_known_peer(peer_id, peer_name, peer_ik_hex)
        peers = _load_known_peers()
        rec = peers.get(peer_id)
        if isinstance(rec, dict):
            rec["last_transfer"] = int(time.time())
            try:
                prev = int(rec.get("transfer_count") or 0)
            except (TypeError, ValueError):
                prev = 0
            rec["transfer_count"] = prev + 1
            peers[peer_id] = rec
            _save_known_peers(peers)
    except OSError:
        pass


def _relay_progress(on_progress, phase: str, fraction: float) -> None:
    if on_progress is not None:
        try:
            on_progress(phase, fraction)
        except Exception:
            pass


# ---------- symmetric pair (identity-only first contact) ----------
# Two-message identity exchange, decoupled from the send/receive ciphertext
# pipeline. Both sides invoke `nomnom pair`; race-decided role:
#   PUT pair_i succeeds   -> initiator. Long-poll pair_r_<own_ik>_p.
#   PUT pair_i 409s       -> responder. GET pair_i, PUT pair_r_<their_ik>_p.
# No DH, no session key, no payload. TOFU prompt + out-of-band fingerprint
# check is the trust gate, same as before.

def _relay_pair_blob(identity: dict) -> bytes:
    return json.dumps({
        "magic": _RELAY_PAIR_MAGIC,
        "ik": identity["ik_pub"],
        "device_id": identity["device_id"],
        "name": identity["name"],
    }).encode("utf-8")


def _relay_parse_pair_blob(raw: bytes) -> dict:
    """Decode a pair blob JSON. Raises NomnomError on any malformation.

    Distinct from `_relay_parse_handshake` because pair blobs carry no
    ephemeral key — the symmetric pair flow doesn't derive a session key.
    """
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise NomnomError(f"relay returned malformed pair blob: {e}") from e
    if not isinstance(obj, dict):
        raise NomnomError("relay returned non-object pair blob")
    if obj.get("magic") != _RELAY_PAIR_MAGIC:
        raise NomnomError(
            f"pair blob magic mismatch (expected {_RELAY_PAIR_MAGIC!r}, "
            f"got {obj.get('magic')!r})",
        )
    for key in ("ik", "device_id", "name"):
        if not isinstance(obj.get(key), str) or not obj[key]:
            raise NomnomError(f"pair blob missing/blank {key!r}")
    try:
        _hex_to_bytes(obj["ik"])
    except ValueError as e:
        raise NomnomError("pair blob field 'ik' is not hex") from e
    return obj


def _relay_pair_resp_slot(relay_secret: str, initiator_ik_hex: str) -> str:
    """Responder pair slot, keyed by the initiator's identity pubkey.

    `_p` suffix forward-isolates from the legacy `_r`/`_d` first-contact
    slots so a mixed-version relay can't accidentally cross-talk.
    """
    return _slot_first_contact_resp_base(relay_secret, initiator_ik_hex) + "_p"


def _relay_pair(
    *,
    identity: dict,
    relay: dict,
    on_progress=None,
    on_tofu=None,
    on_result=None,
    cancel=None,
) -> int:
    """Identity-only pair through the relay. Returns 0 on success, 130 on cancel.

    Both sides invoke this. Role is decided by who wins the PUT race on
    the per-relay rendezvous slot; the loser falls back to responder.
    On success, fires `on_result({peer_id, peer_name, peer_ik, role})`.
    """
    slot_i = _slot_first_contact_init(relay["secret"])
    own_blob = _relay_pair_blob(identity)

    _relay_progress(on_progress, "looking for peer", 0.0)
    if _relay_cancelled(cancel):
        return 130

    try:
        _relay_put_slot(relay, slot_i, own_blob)
        is_initiator = True
    except NomnomError as e:
        if "occupied" not in str(e).lower():
            raise
        is_initiator = False

    if is_initiator:
        slot_r = _relay_pair_resp_slot(relay["secret"], identity["ik_pub"])
        _relay_progress(on_progress, "waiting for peer", 0.3)
        if _relay_cancelled(cancel):
            _relay_delete_slot(relay, slot_i)
            return 130
        resp_raw = _relay_get_slot(relay, slot_r, wait_ms=_RELAY_DEFAULT_WAIT_MS)
        if resp_raw is None:
            _relay_delete_slot(relay, slot_i)
            raise NomnomError(
                "no peer connected (waited 30s). run `nomnom pair` on the "
                "other device.",
            )
        peer = _relay_parse_pair_blob(resp_raw)
        role = "initiator"
    else:
        # Slot was observed occupied; a zero-wait GET should hit immediately.
        # Worker GETs are delete-on-read so cleanup is automatic.
        _relay_progress(on_progress, "reading peer identity", 0.3)
        init_raw = _relay_get_slot(relay, slot_i, wait_ms=0)
        if init_raw is None:
            raise NomnomError(
                "initiator vanished before we could read; retry.",
            )
        peer = _relay_parse_pair_blob(init_raw)
        role = "responder"

    decision = _tofu_decision(peer["device_id"], peer["ik"])
    _relay_progress(on_progress, "verifying peer", 0.7)
    if not _relay_run_tofu(
        decision, peer["device_id"], peer["name"], peer["ik"],
        on_tofu=on_tofu,
    ):
        # Responder declining is silent to the initiator — initiator's
        # long-poll above just times out at 30s. Same contract the old
        # first-contact flow used.
        raise NomnomError("TOFU prompt declined")

    if not is_initiator:
        slot_r = _relay_pair_resp_slot(relay["secret"], peer["ik"])
        _relay_progress(on_progress, "publishing identity", 0.85)
        if _relay_cancelled(cancel):
            return 130
        try:
            _relay_put_slot(relay, slot_r, own_blob)
        except NomnomError as e:
            if "occupied" in str(e).lower():
                raise NomnomError(
                    "responder slot occupied — another pair beat us; retry.",
                ) from e
            raise

    _relay_pin_peer(
        peer["device_id"], peer["name"], peer["ik"], decision=decision,
    )
    _relay_progress(on_progress, "done", 1.0)
    if on_result is not None:
        try:
            on_result({
                "peer_id": peer["device_id"],
                "peer_name": peer["name"],
                "peer_ik": peer["ik"],
                "role": role,
            })
        except Exception:
            pass
    return 0


def _relay_send(
    name: str, data: bytes, *,
    target_ik_hex: str,
    identity: dict,
    relay: dict,
    on_progress=None,
    on_tofu=None,
    on_result=None,
    cancel=None,
) -> int:
    """Send `data` through the relay to a pinned peer. Returns 0 on success,
    130 on cancel.

    Recurring mode only: derive the per-peer rendezvous slot from the long-term
    DH shared secret. First-contact is now handled by `_relay_pair` (identity
    exchange) followed by a normal recurring send.

    Raises NomnomError on any error path so callers (CLI / TUI worker) can
    decide where to surface the message — historically this helper wrote to
    stderr directly, which corrupts the curses TUI.
    """
    if len(data) > _RELAY_MAX_BODY:
        raise NomnomError(
            f"payload too large for relay ({len(data)} bytes; "
            f"limit {_RELAY_MAX_BODY})",
        )

    base = _slot_recurring(int(identity["ik_priv"], 16), target_ik_hex)
    slot_i, slot_r, slot_d = f"{base}_i", f"{base}_r", f"{base}_d"
    binding = _recurring_binding(identity["ik_pub"], target_ik_hex)
    init_magic = _RELAY_INIT_MAGIC

    ek_priv, ek_pub = _dh_keypair()
    ek_pub_hex = format(ek_pub, "x")
    init_blob = _relay_handshake_blob(identity, ek_pub_hex, init_magic)
    authored: list[str] = []

    def cleanup_authored():
        for sid in authored:
            _relay_delete_slot(relay, sid)

    try:
        _relay_progress(on_progress, "uploading handshake", 0.0)
        if _relay_cancelled(cancel):
            return 130
        _relay_put_slot(relay, slot_i, init_blob)
        authored.append(slot_i)

        _relay_progress(on_progress, "waiting for receiver", 0.1)
        if _relay_cancelled(cancel):
            cleanup_authored()
            return 130
        resp_blob = _relay_get_slot(relay, slot_r, wait_ms=_RELAY_DEFAULT_WAIT_MS)
        if resp_blob is None:
            cleanup_authored()
            raise NomnomError("receiver didn't connect (waited 30s)")

        resp = _relay_parse_handshake(resp_blob, _RELAY_RESP_MAGIC)
        try:
            peer_ik = int(resp["ik"], 16)
            peer_ek = int(resp["ek"], 16)
        except ValueError as e:
            cleanup_authored()
            raise NomnomError("peer sent malformed keys") from e

        decision = _tofu_decision(resp["device_id"], resp["ik"])
        if not _relay_run_tofu(
            decision, resp["device_id"], resp["name"], resp["ik"],
            on_tofu=on_tofu,
        ):
            cleanup_authored()
            raise NomnomError("TOFU prompt declined")

        _relay_progress(on_progress, "encrypting", 0.5)
        try:
            session_key = _session_key_initiator(
                int(identity["ik_priv"], 16), ek_priv,
                int(identity["ik_pub"], 16), ek_pub,
                peer_ik, peer_ek,
                binding=binding,
            )
        except ValueError as e:
            cleanup_authored()
            raise NomnomError(f"bad peer key: {e}") from e
        try:
            ciphertext = seal_bytes(data, name, session_key.hex())
        except Exception as e:
            cleanup_authored()
            raise NomnomError(f"encryption failed: {e}") from e

        _relay_progress(on_progress, "uploading payload", 0.7)
        try:
            _relay_put_slot(relay, slot_d, ciphertext)
        except NomnomError:
            cleanup_authored()
            raise
        # NOTE: slot_d is intentionally NOT added to `authored`. The receiver's
        # GET on slot_d auto-deletes on successful read; if the receiver never
        # arrives, the Worker's 5-minute TTL collects the slot. Deleting it
        # here would race the receiver's long-poll on fast networks
        # (localhost / wrangler dev) and produce "sender didn't deliver
        # payload (waited 30s)" errors.

        _relay_pin_peer(
            resp["device_id"], resp["name"], resp["ik"], decision=decision,
        )
        _relay_progress(on_progress, "done", 1.0)
        if on_result is not None:
            try:
                on_result({
                    "name": name,
                    "bytes": len(data),
                    "peer_name": resp["name"],
                })
            except Exception:
                pass
        # Free authored slots immediately so the (deterministic) rendezvous
        # slot doesn't block the next pair attempt for the Worker TTL.
        cleanup_authored()
        return 0
    except NomnomError:
        cleanup_authored()
        raise
    except KeyboardInterrupt:
        cleanup_authored()
        return 130


def _relay_recv_recurring(
    peer_id: str, peer_ik_hex: str, init_blob: bytes, *,
    identity: dict,
    relay: dict,
    on_progress=None,
    on_tofu=None,
    on_result=None,
    cancel=None,
) -> int:
    base = _slot_recurring(int(identity["ik_priv"], 16), peer_ik_hex)
    binding = _recurring_binding(identity["ik_pub"], peer_ik_hex)
    return _relay_recv_complete(
        f"{base}_i", f"{base}_r", f"{base}_d", binding,
        expect_init_magic=_RELAY_INIT_MAGIC,
        expect_recurring_peer_id=peer_id,
        identity=identity, relay=relay,
        on_progress=on_progress, on_tofu=on_tofu, on_result=on_result,
        cancel=cancel,
        prefetched_init=init_blob,
    )


def _relay_recv_complete(
    slot_i: str, slot_r: str, slot_d: str, binding: bytes, *,
    expect_init_magic: str,
    expect_recurring_peer_id: str | None,
    identity: dict, relay: dict,
    on_progress, on_tofu, cancel,
    on_result=None,
    prefetched_init: bytes | None = None,
) -> int:
    """Long-poll slot_i, decrypt the payload arriving at slot_d. Returns 0 on
    success, 130 on cancel. Raises NomnomError on any failure path so the
    caller chooses where to surface the message (stderr in CLI, state.error
    in the TUI)."""
    authored: list[str] = []

    def cleanup_authored():
        for sid in authored:
            _relay_delete_slot(relay, sid)

    try:
        _relay_progress(on_progress, "waiting for sender", 0.0)
        if prefetched_init is not None:
            init_raw = prefetched_init
        else:
            if _relay_cancelled(cancel):
                return 130
            init_raw = _relay_get_slot(relay, slot_i, wait_ms=_RELAY_DEFAULT_WAIT_MS)
            if init_raw is None:
                raise NomnomError("no sender connected (waited 30s)")

        init = _relay_parse_handshake(init_raw, expect_init_magic)
        try:
            peer_ik = int(init["ik"], 16)
            peer_ek = int(init["ek"], 16)
        except ValueError as e:
            raise NomnomError("sender sent malformed keys") from e

        if expect_recurring_peer_id is not None and init["device_id"] != expect_recurring_peer_id:
            raise NomnomError(
                f"recurring rendezvous hit by unexpected peer "
                f"{init['device_id']!r} (expected {expect_recurring_peer_id!r})",
            )

        decision = _tofu_decision(init["device_id"], init["ik"])
        if not _relay_run_tofu(
            decision, init["device_id"], init["name"], init["ik"],
            on_tofu=on_tofu,
        ):
            raise NomnomError("TOFU prompt declined")

        ek_priv, ek_pub = _dh_keypair()
        ek_pub_hex = format(ek_pub, "x")
        try:
            session_key = _session_key_responder(
                int(identity["ik_priv"], 16), ek_priv,
                int(identity["ik_pub"], 16), ek_pub,
                peer_ik, peer_ek,
                binding=binding,
            )
        except ValueError as e:
            raise NomnomError(f"bad peer key: {e}") from e

        _relay_progress(on_progress, "uploading handshake", 0.4)
        resp_blob = _relay_handshake_blob(identity, ek_pub_hex, _RELAY_RESP_MAGIC)
        if _relay_cancelled(cancel):
            return 130
        _relay_put_slot(relay, slot_r, resp_blob)
        authored.append(slot_r)

        _relay_progress(on_progress, "downloading payload", 0.6)
        if _relay_cancelled(cancel):
            cleanup_authored()
            return 130
        ciphertext = _relay_get_slot(relay, slot_d, wait_ms=_RELAY_DEFAULT_WAIT_MS)
        if ciphertext is None:
            cleanup_authored()
            raise NomnomError("sender didn't deliver payload (waited 30s)")

        _relay_progress(on_progress, "decrypting", 0.9)
        try:
            name, plaintext = open_bytes(ciphertext, session_key.hex())
        except (NomnomError, ValueError) as e:
            raise NomnomError(f"decrypt failed: {e}") from e

        out_path = _pick_decrypted_path(Path.cwd(), name)
        try:
            out_path.write_bytes(plaintext)
        except OSError as e:
            raise NomnomError(f"writing file failed: {e}") from e

        _relay_pin_peer(
            init["device_id"], init["name"], init["ik"], decision=decision,
        )
        _relay_progress(on_progress, "done", 1.0)
        if on_result is not None:
            try:
                on_result({
                    "name": name,
                    "bytes": len(plaintext),
                    "peer_name": init["name"],
                    "out_path": str(out_path),
                })
            except Exception:
                pass
        cleanup_authored()
        return 0
    except NomnomError:
        cleanup_authored()
        raise
    except KeyboardInterrupt:
        cleanup_authored()
        return 130


def _bundle_send_via_relay(
    name: str, data: bytes, *,
    interactive: bool = True,
) -> int:
    """Push a bundle through the relay to the single pinned peer.

    Bulk repo bundles aren't a good fit for first-contact pairing — the
    receiver has to be running `nomnom pair` at the exact right moment.
    So this errors when the pin state is ambiguous and tells the user to
    pair first or use `nomnom send --to PEER` from the CLI.

    `interactive=False` makes the relay-config helper non-interactive so
    the TUI can call this without curses-incompatible prompts.
    """
    try:
        relay = _ensure_relay_configured(interactive=interactive)
    except NomnomError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2
    if relay is None:
        return 2
    identity = _load_identity()
    peers = [
        (pid, rec) for pid, rec in _load_known_peers().items()
        if isinstance(rec, dict) and isinstance(rec.get("ik_pub"), str)
    ]
    if not peers:
        sys.stderr.write(
            "error: no pinned peers. run `nomnom pair` to add a "
            "device before sending a bundle.\n",
        )
        return 1
    if len(peers) > 1:
        sys.stderr.write(
            "error: multiple pinned peers; bundle SEND auto-targets only "
            "when one peer exists. use `nomnom send <bundle> --to PEER` "
            "from the CLI instead.\n",
        )
        return 1
    target_pid, target_rec = peers[0]
    target_ik = target_rec["ik_pub"]
    try:
        rc = _relay_send(
            name, data,
            target_ik_hex=target_ik,
            identity=identity, relay=relay,
        )
    except NomnomError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    if rc == 0:
        sys.stderr.write(
            f"sent {name!r} ({len(data)} bytes) to "
            f"{target_rec.get('name', target_pid)!r} via relay.\n",
        )
    return rc


def _relay_recv_pinned(
    *,
    identity: dict, relay: dict,
    on_progress=None, on_tofu=None, on_result=None, cancel=None,
) -> int:
    """Long-poll every pinned peer's deterministic init slot in parallel."""
    peers = _load_known_peers()
    if not peers:
        raise NomnomError(
            "no pinned peers. run `nomnom pair` to add a new device.",
        )

    workers: list[_RelayPollWorker] = []
    threads: list[threading.Thread] = []
    result_q: queue.Queue = queue.Queue()
    my_priv = int(identity["ik_priv"], 16)

    def _spawn(pid: str, rec: dict) -> None:
        peer_ik_hex = rec.get("ik_pub")
        if not isinstance(peer_ik_hex, str):
            return
        try:
            base = _slot_recurring(my_priv, peer_ik_hex)
        except ValueError:
            return
        w = _RelayPollWorker(relay, f"{base}_i", _RELAY_DEFAULT_WAIT_MS)
        workers.append(w)

        def _run() -> None:
            blob = w.run()
            if blob is not None:
                result_q.put((pid, peer_ik_hex, rec.get("name") or pid, blob))

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        threads.append(t)

    for pid, rec in peers.items():
        if isinstance(rec, dict):
            _spawn(pid, rec)

    if not workers:
        raise NomnomError("no usable pinned peers")

    _relay_progress(on_progress, f"waiting on {len(workers)} pinned peers", 0.0)
    deadline = time.monotonic() + _RELAY_DEFAULT_WAIT_MS / 1000.0 + 5.0
    winner = None
    cancelled = False
    while time.monotonic() < deadline:
        if _relay_cancelled(cancel):
            cancelled = True
            break
        try:
            winner = result_q.get(timeout=0.1)
            break
        except queue.Empty:
            continue
    for w in workers:
        w.cancel()
    if cancelled:
        return 130
    if winner is None:
        # If any worker recorded a structured error (401, 5xx, network),
        # surface the first one — clock skew / bad secret would otherwise
        # masquerade as a plain timeout.
        for w in workers:
            if w.last_error:
                raise NomnomError(f"no transfer: {w.last_error}")
        raise NomnomError("no transfer (waited 30s)")
    pid, peer_ik_hex, peer_name, init_blob = winner
    return _relay_recv_recurring(
        pid, peer_ik_hex, init_blob,
        identity=identity, relay=relay,
        on_progress=on_progress, on_tofu=on_tofu, on_result=on_result,
        cancel=cancel,
    )


def _read_payload_or_error(p: Path) -> bytes | None:
    """Stat-then-read with a clean error for oversize / unreadable files.

    Reads the file into memory only after the size is known to be under
    `_RELAY_MAX_BODY`, avoiding an OOM on multi-GB inputs that would otherwise
    only error inside `_relay_send`. Returns None on error (caller has already
    seen a stderr line).
    """
    try:
        sz = p.stat().st_size
    except OSError as e:
        sys.stderr.write(f"error: cannot stat {p}: {e}\n")
        return None
    if sz > _RELAY_MAX_BODY:
        sys.stderr.write(
            f"error: file too large for relay ({sz} bytes; "
            f"limit {_RELAY_MAX_BODY}).\n",
        )
        return None
    try:
        return p.read_bytes()
    except OSError as e:
        sys.stderr.write(f"error: cannot read {p}: {e}\n")
        return None


def cmd_send(path: str, *, to: str | None = None, trust_new: bool = False) -> int:
    """Send a file through the relay to a pinned peer.

    Routing:
      - --to PEER         recurring transfer to the named pinned peer.
      - 1 peer pinned     auto-target that peer.
      - 0 or N peers      error (run `nomnom pair` for first-contact, or
                          pass --to for ambiguity).
    """
    p = Path(path).expanduser()
    if not p.is_file():
        sys.stderr.write(f"error: not a file: {p}\n")
        return 1
    data = _read_payload_or_error(p)
    if data is None:
        return 1

    relay = _ensure_relay_configured()
    if relay is None:
        return 2
    identity = _load_identity()

    target_ik = None
    if to is not None:
        matches = _resolve_peer(to)
        if not matches:
            sys.stderr.write(
                f"error: no pinned peer matches {to!r}. "
                "see `nomnom peers list`.\n",
            )
            return 1
        if len(matches) > 1:
            sys.stderr.write(
                f"error: peer {to!r} is ambiguous "
                f"({len(matches)} matches). use the device id.\n",
            )
            return 1
        _pid, rec = matches[0]
        target_ik = rec.get("ik_pub")
        if not isinstance(target_ik, str):
            sys.stderr.write(f"error: peer {to!r} has no pinned ik_pub.\n")
            return 1
    else:
        peers = [
            (pid, rec) for pid, rec in _load_known_peers().items()
            if isinstance(rec, dict) and isinstance(rec.get("ik_pub"), str)
        ]
        if not peers:
            sys.stderr.write(
                "error: no pinned peers. run `nomnom pair FILE` to add "
                "a new device.\n",
            )
            return 1
        if len(peers) > 1:
            sys.stderr.write(
                "error: multiple pinned peers; pass --to PEER.\n",
            )
            return 1
        target_ik = peers[0][1]["ik_pub"]
        sys.stderr.write(
            f"sending to {peers[0][1].get('name', peers[0][0])!r}.\n",
        )

    on_tofu = _trust_new_callback() if trust_new else None
    try:
        rc = _relay_send(
            p.name, data,
            target_ik_hex=target_ik,
            identity=identity, relay=relay,
            on_tofu=on_tofu,
        )
    except NomnomError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    if rc == 0:
        sys.stderr.write(
            f"sent {p.name!r} ({len(data)} bytes).\n",
        )
    return rc


def cmd_receive(*, once: bool = False, trust_new: bool = False) -> int:
    """Receive files through the relay from pinned peers.

    Default: keep long-polling every pinned peer's deterministic rendezvous
    slot, printing one line per received file. Ctrl-C exits cleanly. Pass
    `once=True` to exit after the first received file (scripting). With
    zero pinned peers, errors: first contact goes through `nomnom pair`.
    """
    relay = _ensure_relay_configured()
    if relay is None:
        return 2
    identity = _load_identity()

    if not _load_known_peers():
        sys.stderr.write(
            "error: no pinned peers. run `nomnom pair` to add a new device.\n",
        )
        return 1

    on_tofu = _trust_new_callback() if trust_new else None
    received_any = False
    if not once:
        sys.stderr.write(
            "waiting for transfers (Ctrl-C to exit)...\n",
        )
        sys.stderr.flush()
    while True:
        result_holder: list[dict] = []

        def _on_result(r: dict) -> None:
            result_holder.append(r)

        try:
            rc = _relay_recv_pinned(
                identity=identity, relay=relay,
                on_result=_on_result, on_tofu=on_tofu,
            )
        except KeyboardInterrupt:
            sys.stderr.write("\n")
            return 0 if received_any else 130
        except NomnomError as e:
            # `_relay_recv_pinned` raises "no transfer (waited 30s)" on a
            # benign long-poll timeout — for the single-shot mode that's an
            # error ("expected a transfer, got none"), but for the watch
            # loop it's the steady-state idle case. Re-arm in watch mode.
            # The error-loaded variant ("no transfer: 401 Unauthorized")
            # carries a real reason and should always exit.
            msg = str(e)
            if not once and msg.startswith("no transfer (waited"):
                continue
            sys.stderr.write(f"error: {e}\n")
            return 1
        if rc == 0 and result_holder:
            r = result_holder[0]
            sys.stderr.write(
                f"received {r['name']!r} ({r['bytes']} bytes) "
                f"from {r['peer_name']} -> {r['out_path']}\n",
            )
            sys.stderr.flush()
            received_any = True
            if once:
                return 0
            continue
        if rc == 130:
            return 0 if received_any else 130
        if once:
            return rc
        # Non-zero return without a Ctrl-C signal: surface and exit so the
        # user isn't stuck in a tight loop against a broken relay.
        return rc


def cmd_pair(*, trust_new: bool = False) -> int:
    """Pair with a new device through the relay.

    Symmetric: both sides invoke `nomnom pair` (no file). Identity-only
    handshake; first PUT wins initiator, the other side falls back to
    responder. Trust still degrades to TOFU at the prompt.
    """
    relay = _ensure_relay_configured()
    if relay is None:
        return 2
    identity = _load_identity()
    result_holder: list[dict] = []

    def _on_result(r: dict) -> None:
        result_holder.append(r)

    on_tofu = _trust_new_callback() if trust_new else None

    sys.stderr.write(
        "waiting for the other device to run `nomnom pair` on this relay...\n",
    )
    try:
        rc = _relay_pair(
            identity=identity, relay=relay,
            on_tofu=on_tofu, on_result=_on_result,
        )
    except NomnomError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    if rc == 0 and result_holder:
        r = result_holder[0]
        sys.stderr.write(
            f"paired with {r['peer_name']!r} ({r['peer_id'][:8]}) "
            f"as {r['role']}.\n",
        )
    return rc


# ---------- relay + peers subcommand handlers ----------


def cmd_relay(args) -> int:
    action = getattr(args, "action", None)
    handlers = {
        "init": _cmd_relay_init,
        "test": _cmd_relay_test,
        "show": _cmd_relay_show,
        "clear": _cmd_relay_clear,
    }
    if action is None:
        sys.stderr.write(
            "usage: nomnom relay {init,test,show,clear}\n",
        )
        return 2
    return handlers[action](args)


def _ensure_relay_configured(*, interactive: bool = True) -> dict | None:
    """Return loaded relay config; prompt to run init if missing.

    `interactive=False` (TUI callers) skips the input() prompt entirely and
    raises NomnomError so the caller surfaces a clean error — calling input()
    while curses owns the terminal would hang the TUI. The TUI flag
    (`_in_tui()`) also forces non-interactive even if the caller forgot to
    pass it.

    None means the CLI caller should bail (rc=2) — the user declined or the
    inline setup failed.
    """
    cfg = _load_relay_config()
    if cfg is not None:
        return cfg
    hint = "run `nomnom relay init` (first device) or `nomnom join <token>` (other devices)"
    if not interactive or _in_tui():
        raise NomnomError(f"no relay configured. {hint}.")
    if not sys.stdin.isatty():
        sys.stderr.write(f"error: no relay configured. {hint}.\n")
        return None
    sys.stderr.write("no relay configured. set one up now? [Y/n]: ")
    sys.stderr.flush()
    try:
        ans = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return None
    if ans not in ("", "y", "yes"):
        sys.stderr.write(f"{hint}.\n")
        return None
    return _cmd_relay_init_interactive()


def _cmd_relay_init_interactive(*, allow_private: bool = False) -> dict | None:
    """First-device flow: prompt URL, generate secret, self-test, save, print wrangler commands."""
    sys.stderr.write(
        "relay URL (e.g. https://nomnom-relay.<account>.workers.dev): ",
    )
    sys.stderr.flush()
    try:
        url = input().strip()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\ninit cancelled.\n")
        return None
    if not url:
        sys.stderr.write("error: empty URL.\n")
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = url.rstrip("/")
    # 9 random bytes → 12 url-safe base64 chars (~72 bits). Replaces the old
    # 6-word diceware passphrase: now that the secret is generated and
    # pasted (via `relay show --token`) instead of typed, readability stops
    # mattering and entropy density wins.
    secret = secrets.token_urlsafe(9)
    candidate = {"url": url, "secret": secret}
    rc, msg = _relay_self_test(candidate)
    if rc != 0:
        sys.stderr.write(
            f"error: {msg}\n"
            "the Worker rejected the self-test. did you push this secret to it "
            "yet? see the commands below; you can re-run `nomnom relay init` "
            "after deploying.\n",
        )
        sys.stderr.write("\non the Worker side, run:\n")
        sys.stderr.write(
            f"  echo {secret!r} | npx wrangler secret put NOMNOM_HMAC_SECRET\n"
            "  npx wrangler deploy\n",
        )
        return None
    try:
        _save_relay_config(url, secret, allow_private=allow_private)
    except NomnomError as e:
        sys.stderr.write(f"error: {e}\n")
        return None
    sys.stderr.write(f"saved to {_relay_config_path()} ({msg})\n")
    sys.stderr.write("\non the Worker side, run (if you haven't already):\n")
    sys.stderr.write(
        f"  echo {secret!r} | npx wrangler secret put NOMNOM_HMAC_SECRET\n"
        "  npx wrangler deploy\n",
    )
    sys.stderr.write(
        "\nshare with another device with: nomnom relay show --token\n",
    )
    return candidate


def _cmd_relay_init(args) -> int:
    cfg = _cmd_relay_init_interactive(
        allow_private=getattr(args, "allow_private", False),
    )
    return 0 if cfg is not None else 1


def cmd_join(args) -> int:
    """Second-device flow: parse `host#secret`, self-test, save."""
    try:
        host, secret = _parse_join_token(args.token)
    except NomnomError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    url = f"https://{host}"
    candidate = {"url": url, "secret": secret}
    rc, msg = _relay_self_test(candidate)
    if rc != 0:
        sys.stderr.write(f"error: {msg}\nconfig NOT saved.\n")
        return 1
    try:
        _save_relay_config(
            url, secret,
            allow_private=getattr(args, "allow_private", False),
        )
    except NomnomError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    sys.stderr.write(f"saved to {_relay_config_path()} ({msg})\n")
    sys.stderr.write("run `nomnom pair` on both devices to add this peer.\n")
    return 0


def _cmd_relay_test(_args) -> int:
    cfg = _load_relay_config()
    if cfg is None:
        sys.stderr.write("error: no relay configured.\n")
        return 1
    rc, msg = _relay_self_test(cfg)
    sys.stderr.write(msg + "\n")
    return rc


def _cmd_relay_show(args) -> int:
    cfg = _load_relay_config()
    if cfg is None:
        sys.stderr.write("no relay configured.\n")
        return 1
    if getattr(args, "token", False):
        # Reduce the stored https://host URL back to the bare host the token
        # format expects. `_load_relay_config` already rejects http(s)-less
        # URLs and strips trailing slashes.
        host = cfg["url"].split("://", 1)[1]
        print(_format_join_token(host, cfg["secret"]))
        return 0
    redacted = cfg["secret"]
    redacted = "*" * max(0, len(redacted) - 4) + redacted[-4:] if redacted else ""
    print(f"url:    {cfg['url']}")
    print(f"secret: {redacted}")
    return 0


def _cmd_relay_clear(_args) -> int:
    cfg = _load_relay_config()
    if cfg is None:
        sys.stderr.write("no relay configured.\n")
        return 0
    try:
        ans = input(f"delete {_relay_config_path()}? [y/N]: ").strip().lower()
    except EOFError:
        ans = ""
    if ans not in ("y", "yes"):
        sys.stderr.write("aborted.\n")
        return 1
    _relay_clear_config()
    sys.stderr.write("cleared.\n")
    return 0


def _resolve_nomnom_dir() -> Path:
    """Return the config dir path WITHOUT creating it.

    `_nomnom_config_dir` has a `mkdir` side effect; `cmd_reset` needs to
    detect "nothing to reset" and refuse the prompt on a missing dir, so
    it can't use that helper.
    """
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "nomnom"


def cmd_reset(_args) -> int:
    """Wipe ~/.config/nomnom/ after y/N confirmation.

    Refuses on a non-tty stdin so a piped invocation can't accidentally
    blow away the user's pins and identity. Identity rotation invalidates
    every remote pin, so this is intentionally a one-shot foot-gun.
    """
    target = _resolve_nomnom_dir()
    if not target.exists():
        sys.stderr.write(f"nothing to reset ({target} does not exist).\n")
        return 0
    if not sys.stdin.isatty():
        sys.stderr.write(
            "error: `nomnom reset` requires a tty (refuses non-interactive "
            "stdin to avoid accidental wipe).\n",
        )
        return 2
    try:
        entries = sorted(p.name for p in target.iterdir())
    except OSError as e:
        sys.stderr.write(f"error: cannot read {target}: {e}\n")
        return 1
    if not entries:
        sys.stderr.write(f"nothing to reset ({target} is empty).\n")
        return 0
    sys.stderr.write(
        f"about to delete {target} and {len(entries)} entries "
        f"({', '.join(entries)}).\n"
        f"identity rotation will invalidate every pin on every paired device.\n",
    )
    try:
        ans = input("proceed? [y/N]: ").strip().lower()
    except EOFError:
        ans = ""
    if ans not in ("y", "yes"):
        sys.stderr.write("aborted.\n")
        return 1
    try:
        shutil.rmtree(target)
    except OSError as e:
        sys.stderr.write(f"error: rmtree failed: {e}\n")
        return 1
    sys.stderr.write(f"cleared {target}.\n")
    return 0


def cmd_peers(args) -> int:
    action = getattr(args, "action", None)
    handlers = {
        "list": _cmd_peers_list,
        "nickname": _cmd_peers_nickname,
        "forget": _cmd_peers_forget,
        "fingerprint": _cmd_peers_fingerprint,
    }
    if action is None:
        return _cmd_peers_list(args)
    return handlers[action](args)


def _cmd_peers_list(_args) -> int:
    peers = _load_known_peers()
    if not peers:
        sys.stderr.write("no pinned peers.\n")
        return 0
    rows = []
    for pid, rec in peers.items():
        if not isinstance(rec, dict):
            continue
        name = rec.get("name") or "?"
        nickname = rec.get("nickname") or ""
        fp = _ik_fingerprint(rec.get("ik_pub", ""))
        last = rec.get("last_transfer")
        last_str = (
            datetime.fromtimestamp(int(last)).strftime("%Y-%m-%d %H:%M")
            if isinstance(last, (int, float)) and last else "never"
        )
        count = rec.get("transfer_count") or 0
        rows.append((nickname or name, name, pid, fp, last_str, count))
    if not rows:
        sys.stderr.write("no pinned peers.\n")
        return 0
    width = max(len(r[0]) for r in rows)
    for nick, name, pid, fp, last, count in rows:
        primary = nick.ljust(width)
        name_str = f"({name})" if nick and nick != name else ""
        print(f"{primary}  {fp}  {pid[:12]}.. xfers={count} last={last} {name_str}")
    return 0


def _cmd_peers_nickname(args) -> int:
    result = _set_peer_nickname(args.peer, args.name or None)
    if result is None:
        sys.stderr.write(f"error: no peer matches {args.peer!r}.\n")
        return 1
    pid, nick = result
    if nick:
        sys.stderr.write(f"set nickname of {pid[:12]}.. -> {nick!r}.\n")
    else:
        sys.stderr.write(f"cleared nickname of {pid[:12]}..\n")
    return 0


def _cmd_peers_forget(args) -> int:
    dropped = _forget_peer(args.peer)
    if not dropped:
        sys.stderr.write(f"error: no peer matches {args.peer!r}.\n")
        return 1
    sys.stderr.write(f"dropped pin for {', '.join(dropped)}.\n")
    return 0


def _cmd_peers_fingerprint(args) -> int:
    matches = _resolve_peer(args.peer)
    if not matches:
        sys.stderr.write(f"error: no peer matches {args.peer!r}.\n")
        return 1
    if len(matches) > 1:
        sys.stderr.write(f"error: peer {args.peer!r} is ambiguous.\n")
        return 1
    _pid, rec = matches[0]
    print(_ik_fingerprint(rec.get("ik_pub", "")))
    return 0


# ---------- TUI app shell ----------

class ScreenAction(enum.Enum):
    CONTINUE = "continue"
    BACK = "back"
    QUIT = "quit"


class Screen:
    """Base class for TUI screens. Subclasses override render/handle_key.

    Returning another Screen instance from handle_key pushes it onto the
    stack. ScreenAction.BACK pops, ScreenAction.QUIT exits the app,
    ScreenAction.CONTINUE redraws."""

    title: str = "nomnom"
    help_lines: list[str] = []

    def render(self, stdscr) -> None:  # pragma: no cover - curses I/O
        raise NotImplementedError

    def handle_key(self, ch: int, stdscr=None):  # -> ScreenAction | Screen  # pragma: no cover
        raise NotImplementedError


def show_help_modal(stdscr, lines: list[str]) -> None:  # pragma: no cover
    """Centered modal listing keybindings. Closes on any key.

    Forces stdscr.timeout(-1) (blocking) for the duration of its getch — the
    caller may have set a non-blocking timeout for a progress poll, which
    would otherwise close the modal in ~100ms before the user can read it.
    """
    h, w = stdscr.getmaxyx()
    rows = [" nomnom keys ".center(40, "─"), *lines, "─" * 40, " press any key to close "]
    box_w = min(w - 2, max(len(r) for r in rows) + 2)
    box_h = min(h - 2, len(rows) + 2)
    y0 = max(0, (h - box_h) // 2)
    x0 = max(0, (w - box_w) // 2)
    for i in range(box_h):
        try:
            stdscr.addstr(y0 + i, x0, " " * box_w, curses.A_REVERSE)
        except curses.error:
            pass
    for i, line in enumerate(rows[: box_h - 1]):
        try:
            stdscr.addstr(y0 + 1 + i, x0 + 1, line[: box_w - 2], curses.A_REVERSE)
        except curses.error:
            pass
    stdscr.refresh()
    try:
        stdscr.timeout(-1)
    except curses.error:
        pass
    while True:
        ch = stdscr.getch()
        if ch != -1:
            return


_TUI_ACTIVE = False


def _in_tui() -> bool:
    """True while `run_app` (the curses launcher) owns the terminal.

    Helpers that would otherwise call `input()` or write raw stderr can
    branch on this to avoid corrupting the curses display.
    """
    return _TUI_ACTIVE


def run_app(initial: Screen) -> None:  # pragma: no cover - curses I/O
    """Drive a stack of Screen instances until empty or QUIT."""
    def _loop(stdscr):
        curses.curs_set(0)
        stdscr.keypad(True)
        stack: list[Screen] = [initial]
        while stack:
            current = stack[-1]
            stdscr.erase()
            current.render(stdscr)
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (curses.KEY_RESIZE, -1):
                # -1 means a non-blocking getch timed out; the active screen
                # has set its own poll interval (e.g. for a progress bar) and
                # wants the loop to re-render.
                continue
            if ch == ord("?"):
                show_help_modal(stdscr, current.help_lines)
                continue
            result = current.handle_key(ch, stdscr)
            if isinstance(result, Screen):
                stack.append(result)
            elif result == ScreenAction.BACK:
                stack.pop()
            elif result == ScreenAction.QUIT:
                return

    global _TUI_ACTIVE
    _TUI_ACTIVE = True
    try:
        curses.wrapper(_loop)
    finally:
        _TUI_ACTIVE = False


class LauncherScreen(Screen):
    title = "nomnom"
    help_lines = [
        "j/k or ↑/↓   move",
        "Enter        open selected verb",
        "q            quit",
    ]

    def __init__(self) -> None:
        self.cursor = 0
        self.tiles: list[tuple[str, str]] = [
            ("Bundle",     "Pick files and write a bundle .txt"),
            ("Send",       "Encrypt a file and send it to a pinned peer"),
            ("Receive",    "Listen for transfers (auto-resumes after each)"),
            ("Pair",       "Pair with a new device over the relay"),
            ("Commit",     "Bundle staged/unstaged diffs + recent commits"),
            ("PR",         "Bundle commits since base + diff for an LLM"),
            ("Review",     "Bundle a PR's meta, diff, and comments"),
            ("Rebuild",    "Reconstruct a file tree from a bundle .txt"),
            ("Pins",       "Manage TOFU-pinned peers"),
            ("Extensions", "Edit the text/binary/name/secret lists"),
        ]

    def render(self, stdscr) -> None:  # pragma: no cover - curses I/O
        theme = _setup_theme()
        h, w = stdscr.getmaxyx()
        header = " nomnom — feed your repo to the LLM "
        try:
            stdscr.addstr(0, 0, header.ljust(max(1, w - 1)), theme["filter"])
        except curses.error:
            pass
        for i, (label, desc) in enumerate(self.tiles):
            row = 2 + i
            if row >= h - 1:
                break
            attr = theme["cursor"] if i == self.cursor else theme["dim"]
            line = f"  {label:<12}  {desc}"
            try:
                stdscr.addstr(row, 0, line[: max(1, w - 1)], attr)
            except curses.error:
                pass
        try:
            stdscr.addstr(
                h - 1, 0,
                "j/k:move  enter:open  ?:help  q:quit"[: max(1, w - 1)],
                theme["dim"],
            )
        except curses.error:
            pass

    def handle_key(self, ch: int, stdscr=None):
        if ch in (curses.KEY_DOWN, ord("j")):
            self.cursor = (self.cursor + 1) % len(self.tiles)
            return ScreenAction.CONTINUE
        if ch in (curses.KEY_UP, ord("k")):
            self.cursor = (self.cursor - 1) % len(self.tiles)
            return ScreenAction.CONTINUE
        if ch in (ord("q"), 3, 27):
            return ScreenAction.QUIT
        if ch in (10, 13):
            return _launcher_open(self.tiles[self.cursor][0])
        return ScreenAction.CONTINUE


def _launcher_open(verb: str):
    """Map a launcher tile label to either a Screen to push, or a TODO note.

    Slices 5-8 replace the placeholder rows with real screens."""
    if verb == "Extensions":
        return ExtensionsScreen()
    if verb == "Pins":
        return PinsScreen()
    if verb == "Rebuild":
        return RebuildScreen()
    if verb == "Bundle":
        return BundleScreen()
    if verb == "Commit":
        return CommitScreen()
    if verb == "PR":
        return PRScreen()
    if verb == "Review":
        return ReviewScreen()
    if verb == "Send":
        return SendScreen()
    if verb == "Receive":
        return ReceiveScreen()
    if verb == "Pair":
        return PairScreen()
    return PlaceholderScreen(
        verb,
        f"`nomnom {verb.lower()}` works from the shell. A dedicated TUI "
        "screen for this verb arrives in a follow-up slice.",
    )


class PlaceholderScreen(Screen):
    help_lines = ["Esc / q     back to launcher"]

    def __init__(self, title: str, body: str) -> None:
        self.title = title
        self.body = body

    def render(self, stdscr) -> None:  # pragma: no cover - curses I/O
        theme = _setup_theme()
        h, w = stdscr.getmaxyx()
        try:
            stdscr.addstr(0, 0, f" {self.title} ".ljust(max(1, w - 1)), theme["filter"])
        except curses.error:
            pass
        for i, line in enumerate(_wrap(self.body, max(10, w - 4))):
            row = 2 + i
            if row >= h - 1:
                break
            try:
                stdscr.addstr(row, 2, line, 0)
            except curses.error:
                pass
        try:
            stdscr.addstr(
                h - 1, 0, "esc/q:back  ?:help"[: max(1, w - 1)], theme["dim"],
            )
        except curses.error:
            pass

    def handle_key(self, ch: int, stdscr=None):
        if ch in (ord("q"), 3, 27):
            return ScreenAction.BACK
        return ScreenAction.CONTINUE


class RebuildScreen(Screen):
    """Three-step rebuild flow inside the TUI: enter path, preview, confirm.

    Step 1 ('input'): user types a bundle path. Enter parses it.
    Step 2 ('preview'): tree of files to write + target dir; Enter writes,
                        Esc returns to step 1.
    Step 3 ('done'):    write succeeded or failed; Esc back to launcher."""

    title = "Rebuild"
    help_lines = [
        "type a bundle path; enter to parse",
        "in preview: enter to write, esc to re-enter the path",
        "esc / q from input mode returns to launcher",
    ]

    def __init__(self) -> None:
        self.step = "input"
        self.path_buf = ""
        self.error = ""
        self.bundle_text = ""
        self.repo_name = ""
        self.files: list[tuple[str, str]] = []
        self.target: Path | None = None
        self.message = ""

    def _parse(self) -> None:
        p = Path(self.path_buf.strip()).expanduser()
        if not p.is_file():
            self.error = f"not a file: {p}"
            return
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self.error = f"cannot read {p}: {e}"
            return
        try:
            repo_name, files = parse_bundle(text)
        except ValueError as e:
            self.error = str(e)
            return
        self.bundle_text = text
        self.repo_name = repo_name
        self.files = files
        self.target = pick_target_dir(Path.cwd(), repo_name)
        self.error = ""
        self.step = "preview"

    def _write(self) -> None:
        assert self.target is not None
        target_resolved = self.target.resolve()
        try:
            self.target.mkdir(parents=True)
        except OSError as e:
            self.error = f"cannot create {self.target}: {e}"
            self.step = "done"
            return
        for rel, content in self.files:
            dest = (self.target / rel).resolve()
            try:
                dest.relative_to(target_resolved)
            except ValueError:
                self.error = f"refusing to write outside target: {rel!r}"
                self.step = "done"
                return
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_text(content, encoding="utf-8")
            except OSError as e:
                self.error = f"cannot write {dest}: {e}"
                self.step = "done"
                return
        try:
            shown = self.target.relative_to(Path.cwd())
        except ValueError:
            shown = self.target
        self.message = f"wrote {len(self.files)} files into {shown}"
        self.step = "done"

    def render(self, stdscr) -> None:  # pragma: no cover - curses I/O
        theme = _setup_theme()
        h, w = stdscr.getmaxyx()
        try:
            stdscr.addstr(0, 0, f" {self.title} ".ljust(max(1, w - 1)),
                          theme["filter"])
        except curses.error:
            pass
        if self.step == "input":
            _text_input_field(stdscr, 2, 2, "Bundle path:", self.path_buf,
                              theme, max(1, w - 4))
            if self.error:
                try:
                    stdscr.addstr(5, 2, f"error: {self.error}", theme["dim"])
                except curses.error:
                    pass
            footer = "enter:parse  esc/q:back  ?:help"
        elif self.step == "preview":
            tree = render_ascii_tree([rel for rel, _ in self.files], self.repo_name)
            lines = tree.splitlines()
            try:
                stdscr.addstr(2, 2, f"target: {self.target}", theme["dim"])
                stdscr.addstr(3, 2, f"files:  {len(self.files)}", theme["dim"])
            except curses.error:
                pass
            for i, line in enumerate(lines):
                if 5 + i >= h - 2:
                    break
                try:
                    stdscr.addstr(5 + i, 2, line[: max(1, w - 3)], 0)
                except curses.error:
                    pass
            footer = "enter:write  esc:re-enter path  ?:help"
        else:
            try:
                stdscr.addstr(2, 2,
                              self.error and f"error: {self.error}" or self.message,
                              theme["dim"])
            except curses.error:
                pass
            footer = "esc/q:back"
        try:
            stdscr.addstr(h - 1, 0, footer[: max(1, w - 1)], theme["dim"])
        except curses.error:
            pass

    def handle_key(self, ch: int, stdscr=None):
        if self.step == "input":
            if ch in (ord("q"), 3, 27):
                return ScreenAction.BACK
            if ch in (10, 13):
                self._parse()
                return ScreenAction.CONTINUE
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                self.path_buf = self.path_buf[:-1]
                return ScreenAction.CONTINUE
            if 32 <= ch < 127:
                self.path_buf += chr(ch)
            return ScreenAction.CONTINUE
        if self.step == "preview":
            if ch in (10, 13):
                self._write()
                return ScreenAction.CONTINUE
            if ch in (ord("q"), 3, 27):
                self.step = "input"
                self.bundle_text = ""
                self.repo_name = ""
                self.files = []
                self.target = None
            return ScreenAction.CONTINUE
        # done
        if ch in (ord("q"), 3, 27, 10, 13):
            return ScreenAction.BACK
        return ScreenAction.CONTINUE


class ExtensionsScreen(Screen):
    """Editable view of the four auto-managed extension lists.

    Section cursor (h/l) selects a list; entry cursor (j/k) selects an
    item within it. `a` opens an add input, `d` deletes the focused
    entry (with confirm). Edits are applied via `cmd_register`, which
    rewrites the marker block in nomnom.py."""

    title = "Extensions"
    help_lines = [
        "h/l or ←/→   switch section",
        "j/k or ↑/↓   move within section",
        "a            add a new value",
        "d            delete the focused entry (confirms)",
        "esc / q      back to launcher",
    ]

    # (display label, register kind, value-source iterable)
    _SECTION_DEFS = (
        ("Text extensions",   "text",   "TEXT_EXTENSIONS"),
        ("Binary extensions", "binary", "BINARY_EXTENSIONS"),
        ("Known text names",  "name",   "KNOWN_TEXT_NAMES"),
        ("Secret patterns",   "secret", "SECRET_PATTERNS"),
    )

    def __init__(self) -> None:
        self.step = "view"
        self.section_cursor = 0
        self.entry_cursor = 0
        self.add_buf = ""
        self.sections = self._load_sections()
        self.message = ""
        self.error = ""

    @classmethod
    def _load_sections(cls) -> list[tuple[str, str, list[str]]]:
        """Return [(label, kind, sorted entries), ...]."""
        # Pull current contents via globals so cmd_register's marker-block
        # rewrite is reflected on reload.
        g = globals()
        return [
            (label, kind, sorted(g[name]))
            for label, kind, name in cls._SECTION_DEFS
        ]

    def _current(self) -> tuple[str, str, list[str]]:
        return self.sections[self.section_cursor]

    def _capture_register(self, kind: str, value: str, *, remove: bool) -> int:
        """Apply a register/unregister via the structured `register_values` API
        and translate the outcome into self.message (info) or self.error."""
        self.message = ""
        self.error = ""
        result = register_values(kind, [value], remove=remove)
        if result.conflicts:
            _, other_kind = result.conflicts[0]
            other_name = KINDS[other_kind].target_name
            self.error = (
                f"{value!r} is already in {other_name}. "
                f"unregister it from {other_kind} first."
            )
            rc = 1
        elif result.added:
            self.message = f"added {value!r} to {result.target_name}"
            rc = 0
        elif result.removed:
            self.message = f"removed {value!r} from {result.target_name}"
            rc = 0
        elif result.no_ops and remove:
            self.message = f"{value!r} not in {result.target_name} (no change)"
            rc = 0
        elif result.no_ops:
            self.message = f"{value!r} already in {result.target_name} (no change)"
            rc = 0
        else:
            self.error = "register failed"
            rc = 1
        self.sections = self._load_sections()
        _, _, entries = self._current()
        if not entries:
            self.entry_cursor = 0
        elif self.entry_cursor >= len(entries):
            self.entry_cursor = len(entries) - 1
        return rc

    def render(self, stdscr) -> None:  # pragma: no cover - curses I/O
        theme = _setup_theme()
        h, w = stdscr.getmaxyx()
        try:
            stdscr.addstr(0, 0, f" {self.title} ".ljust(max(1, w - 1)),
                          theme["filter"])
        except curses.error:
            pass

        if self.step == "add":
            label, kind, _ = self._current()
            _text_input_field(stdscr, 2, 2,
                              f"Add to {label} ({kind}):",
                              self.add_buf, theme, max(1, w - 4))
            if self.error:
                try:
                    stdscr.addstr(5, 2, f"error: {self.error}", theme["dim"])
                except curses.error:
                    pass
            footer = "enter:add  esc:cancel  ?:help"
            try:
                stdscr.addstr(h - 1, 0, footer[: max(1, w - 1)], theme["dim"])
            except curses.error:
                pass
            return

        # view step
        row = 2
        for si, (label, kind, entries) in enumerate(self.sections):
            if row >= h - 4:
                break
            is_active_section = (si == self.section_cursor)
            head_attr = theme["cursor"] if is_active_section else theme["dim"]
            head = f"  {label} ({kind})  [{len(entries)}]"
            try:
                stdscr.addstr(row, 0, head[: max(1, w - 1)], head_attr)
            except curses.error:
                pass
            row += 1
            if is_active_section and entries:
                # Render entries on a wrapped line, highlight focused one.
                # We render one entry per cell-row for clarity if it fits.
                ec = max(0, min(self.entry_cursor, len(entries) - 1))
                line_parts = []
                for ei, e in enumerate(entries):
                    if ei == ec:
                        line_parts.append(f"[{e}]")
                    else:
                        line_parts.append(e)
                line = "    " + "  ".join(line_parts)
            else:
                line = "    " + "  ".join(entries) if entries else "    (empty)"
            try:
                stdscr.addstr(row, 0, line[: max(1, w - 1)], 0)
            except curses.error:
                pass
            row += 2

        msg_row = h - 3
        if self.message:
            try:
                stdscr.addstr(msg_row, 0, self.message[: max(1, w - 1)],
                              theme["dim"])
            except curses.error:
                pass
        if self.error:
            try:
                stdscr.addstr(msg_row - 1, 0, f"error: {self.error}"[: max(1, w - 1)],
                              theme["dim"])
            except curses.error:
                pass
        try:
            stdscr.addstr(
                h - 1, 0,
                "h/l:section  j/k:entry  a:add  d:delete  esc/q:back  ?:help"
                [: max(1, w - 1)],
                theme["dim"],
            )
        except curses.error:
            pass

    def handle_key(self, ch: int, stdscr=None):
        if self.step == "add":
            if ch in (ord("q"), 3, 27):
                self.step = "view"
                self.add_buf = ""
                self.error = ""
                return ScreenAction.CONTINUE
            if ch in (10, 13):
                value = self.add_buf.strip()
                if not value:
                    self.error = "empty value"
                    return ScreenAction.CONTINUE
                _, kind, _ = self._current()
                self._capture_register(kind, value, remove=False)
                if not self.error:
                    self.add_buf = ""
                    self.step = "view"
                return ScreenAction.CONTINUE
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                self.add_buf = self.add_buf[:-1]
                return ScreenAction.CONTINUE
            if 32 <= ch < 127:
                self.add_buf += chr(ch)
            return ScreenAction.CONTINUE

        # view step
        if ch in (ord("q"), 3, 27):
            return ScreenAction.BACK
        if ch in (curses.KEY_RIGHT, ord("l")):
            self.section_cursor = (self.section_cursor + 1) % len(self.sections)
            self.entry_cursor = 0
            return ScreenAction.CONTINUE
        if ch in (curses.KEY_LEFT, ord("h")):
            self.section_cursor = (self.section_cursor - 1) % len(self.sections)
            self.entry_cursor = 0
            return ScreenAction.CONTINUE
        if ch in (curses.KEY_DOWN, ord("j")):
            _, _, entries = self._current()
            if entries:
                self.entry_cursor = (self.entry_cursor + 1) % len(entries)
            return ScreenAction.CONTINUE
        if ch in (curses.KEY_UP, ord("k")):
            _, _, entries = self._current()
            if entries:
                self.entry_cursor = (self.entry_cursor - 1) % len(entries)
            return ScreenAction.CONTINUE
        if ch == ord("a"):
            self.step = "add"
            self.add_buf = ""
            self.error = ""
            self.message = ""
            return ScreenAction.CONTINUE
        if ch == ord("d"):
            label, kind, entries = self._current()
            if not entries:
                self.message = "(empty section)"
                return ScreenAction.CONTINUE
            value = entries[max(0, min(self.entry_cursor, len(entries) - 1))]
            if stdscr is not None:
                ok = _confirm_modal(
                    stdscr,
                    f" delete {value!r} from {label}? ",
                    [f"This rewrites the marker block in nomnom.py."],
                    default=False,
                )
                if not ok:
                    self.message = "delete cancelled."
                    return ScreenAction.CONTINUE
            self._capture_register(kind, value, remove=True)
            return ScreenAction.CONTINUE
        return ScreenAction.CONTINUE


class PinsScreen(Screen):
    """List TOFU-pinned LAN peers; `d` drops the cursored pin."""

    title = "TOFU pins"
    help_lines = [
        "j/k or ↑/↓   move",
        "d            drop cursored pin (confirms)",
        "esc / q      back to launcher",
    ]

    def __init__(self) -> None:
        self.cursor = 0
        self.message = ""
        self.peers = self._load_peers()

    @staticmethod
    def _load_peers() -> list[tuple[str, str, str]]:
        """Return (device_id, name, fingerprint), sorted by name."""
        peers = _load_known_peers()
        out: list[tuple[str, str, str]] = []
        for pid, rec in peers.items():
            name = rec.get("name") or pid
            ik = rec.get("ik_pub") or ""
            out.append((pid, name, _ik_fingerprint(ik)))
        out.sort(key=lambda r: (r[1].lower(), r[0]))
        return out

    def render(self, stdscr) -> None:  # pragma: no cover - curses I/O
        theme = _setup_theme()
        h, w = stdscr.getmaxyx()
        try:
            stdscr.addstr(0, 0, f" {self.title} ".ljust(max(1, w - 1)),
                          theme["filter"])
        except curses.error:
            pass
        if not self.peers:
            try:
                stdscr.addstr(2, 2, "(no pinned peers yet)", theme["dim"])
            except curses.error:
                pass
        for i, (pid, name, fp) in enumerate(self.peers):
            row = 2 + i
            if row >= h - 2:
                break
            attr = theme["cursor"] if i == self.cursor else 0
            line = f"  {name:<20}  {fp}  {pid[:16]}"
            try:
                stdscr.addstr(row, 0, line[: max(1, w - 1)], attr)
            except curses.error:
                pass
        if self.message:
            try:
                stdscr.addstr(h - 2, 0, self.message[: max(1, w - 1)],
                              theme["dim"])
            except curses.error:
                pass
        try:
            stdscr.addstr(
                h - 1, 0,
                "j/k:move  d:drop  esc/q:back  ?:help"[: max(1, w - 1)],
                theme["dim"],
            )
        except curses.error:
            pass

    def handle_key(self, ch: int, stdscr=None):
        if ch in (ord("q"), 3, 27):
            return ScreenAction.BACK
        if not self.peers:
            return ScreenAction.CONTINUE
        if ch in (curses.KEY_DOWN, ord("j")):
            self.cursor = (self.cursor + 1) % len(self.peers)
        elif ch in (curses.KEY_UP, ord("k")):
            self.cursor = (self.cursor - 1) % len(self.peers)
        elif ch == ord("d"):
            pid, name, _fp = self.peers[self.cursor]
            dropped = _forget_peer(pid)
            self.peers = self._load_peers()
            if self.cursor >= len(self.peers) and self.peers:
                self.cursor = len(self.peers) - 1
            self.message = f"dropped {', '.join(dropped) or name}."
        return ScreenAction.CONTINUE


class BundleScreen(Screen):
    """Two-step bundle flow inside the TUI: enter a repo path, then pick.

    Step 1 ('path'): user types the repo to bundle (default cwd). Enter
                     validates `is_dir`, scans, then opens the picker.
    Step 2 ('done'): writes the bundle via `_emit_bundle` (file/clipboard
                     /send only — STDOUT is hidden from the picker cycle
                     because curses owns the terminal). Esc returns to
                     the launcher."""

    title = "Bundle"
    help_lines = [
        "type a repo path; enter to scan",
        "in picker: d cycles file/clipboard/send, t toggles tree, p preview",
        "esc / q from input mode returns to launcher",
    ]

    def __init__(self) -> None:
        self.step = "path"
        self.path_buf = str(Path.cwd())
        self.error = ""
        self.messages: list[str] = []

    def _scan_and_pick(self, stdscr) -> bool:
        """Validate path → scan → pick → emit. Returns False to send the
        screen back to the launcher (cancelled), True to stay (error or
        done state). All status is recorded on self for `render` to show."""
        self.error = ""
        self.messages = []
        root = Path(self.path_buf.strip()).expanduser().resolve()
        if not root.is_dir():
            self.error = f"not a directory: {root}"
            return True
        repo_name = root.name
        if not repo_name:
            self.error = f"cannot derive a repo name from {root}"
            return True

        gi = load_gitignore(root)
        items = scan_repo(root, gi, skip_secrets=True)
        file_items = [it for it in items if not it.is_dir]
        if not file_items:
            self.error = "no files found after applying excludes."
            return True

        stats = collect_stats(root, items)
        nodes = build_tree(items, stats=stats)

        allow_git_verbs = _is_inside_git_repo(root)
        result = _picker_ui(
            stdscr, nodes, root=root,
            allow_stdout=False, allow_git_verbs=allow_git_verbs,
        )
        if result is None:
            return False

        if result.verb != Verb.BUNDLE:
            self._run_git_verb(result, root)
            self.step = "done"
            return True

        if not result.selected:
            self.error = "no files selected."
            self.step = "done"
            return True

        selected = sorted(result.selected)

        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), \
             contextlib.redirect_stderr(err_buf):
            rc, lines = _emit_bundle(
                repo_name, root, selected, result.include_tree,
                result.destination,
                interactive=False,
            )
        captured = (err_buf.getvalue() + out_buf.getvalue()).rstrip("\n")
        self.messages = list(lines)
        if captured:
            self.messages += captured.splitlines()
        if rc != 0 and self.messages:
            self.error = self.messages[-1]
        self.step = "done"
        return True

    def _run_git_verb(self, result, root: Path) -> None:
        """Run cmd_commit / cmd_pr / cmd_review without disturbing curses.

        The handlers print progress to stdout/stderr; redirect both into
        StringIO buffers and route them through `self.messages` so the
        BundleScreen's render path owns the screen state.
        """
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        rc = 0
        try:
            with contextlib.redirect_stdout(out_buf), \
                 contextlib.redirect_stderr(err_buf):
                if result.verb == Verb.COMMIT:
                    rc = cmd_commit(str(root), destination=result.destination)
                elif result.verb == Verb.PR:
                    rc = cmd_pr(str(root), None, destination=result.destination)
                elif result.verb == Verb.REVIEW:
                    if result.review_pr is None:
                        raise NomnomError(
                            "review verb selected without a PR number",
                        )
                    rc = cmd_review(
                        str(root), result.review_pr, True,
                        destination=result.destination,
                    )
        except NomnomError as e:
            self.error = str(e)
        captured = (err_buf.getvalue() + out_buf.getvalue()).rstrip("\n")
        if captured:
            self.messages += captured.splitlines()
        if rc != 0 and not self.error and self.messages:
            self.error = self.messages[-1]

    def render(self, stdscr) -> None:  # pragma: no cover - curses I/O
        theme = _setup_theme()
        h, w = stdscr.getmaxyx()
        try:
            stdscr.addstr(0, 0, f" {self.title} ".ljust(max(1, w - 1)),
                          theme["filter"])
        except curses.error:
            pass
        if self.step == "path":
            _text_input_field(stdscr, 2, 2, "Repo path:", self.path_buf, theme,
                              max(1, w - 4))
            if self.error:
                try:
                    stdscr.addstr(5, 2, f"error: {self.error}", theme["dim"])
                except curses.error:
                    pass
            footer = "enter:scan  esc/q:back  ?:help"
        else:  # done
            row = 2
            for line in self.messages:
                if row >= h - 1:
                    break
                try:
                    stdscr.addstr(row, 2, line[: max(1, w - 3)], 0)
                except curses.error:
                    pass
                row += 1
            if self.error:
                try:
                    stdscr.addstr(row + 1, 2, f"error: {self.error}",
                                  theme["dim"])
                except curses.error:
                    pass
            footer = "esc/q:back  ?:help"
        try:
            stdscr.addstr(h - 1, 0, footer[: max(1, w - 1)], theme["dim"])
        except curses.error:
            pass

    def handle_key(self, ch: int, stdscr=None):
        if self.step == "path":
            if ch in (ord("q"), 3, 27):
                return ScreenAction.BACK
            if ch in (10, 13):
                if stdscr is None:
                    return ScreenAction.CONTINUE
                stay = self._scan_and_pick(stdscr)
                return ScreenAction.CONTINUE if stay else ScreenAction.BACK
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                self.path_buf = self.path_buf[:-1]
                return ScreenAction.CONTINUE
            if 32 <= ch < 127:
                self.path_buf += chr(ch)
            return ScreenAction.CONTINUE
        # done
        if ch in (ord("q"), 3, 27, 10, 13):
            return ScreenAction.BACK
        return ScreenAction.CONTINUE


class _GitContextScreen(Screen):
    """Shared base for Commit / PR / Review screens.

    State machine: `inputs` → `done`. The inputs step renders a field
    list driven by `fields`; Tab/arrows cycle the focus, `d` cycles
    destination from anywhere. Enter captures stdout/stderr around the
    subclass's `_run_cmd` and stores the result for the done panel."""

    verb = ""
    # Field ids that accept free-text input (rendered with `> ` prompt).
    # Anything else is treated as a read-only status row driven by a hotkey.
    _TEXT_FIDS: tuple[str, ...] = ("repo",)

    def __init__(self) -> None:
        self.step = "inputs"
        self.field_cursor = 0
        self.repo_buf = str(Path.cwd())
        self.destination = Destination.FILE
        self.error = ""
        self.output_lines: list[str] = []
        self.rc = 0

    @property
    def fields(self) -> list[tuple[str, str]]:
        """(field_id, label) — drives cursor + rendering."""
        return [("repo", "Repo path:"), ("dest", "Destination:")]

    def _cycle_dest(self) -> None:
        self.destination = cycle_destination(
            self.destination,
            (Destination.FILE, Destination.CLIPBOARD, Destination.SEND),
        )

    def _run_cmd(self) -> int:
        """Subclass hook: dispatch to cmd_commit/pr/review with collected
        inputs. May raise NomnomError; stdout/stderr are captured by the
        caller."""
        raise NotImplementedError

    def _execute(self) -> None:
        self.error = ""
        self.output_lines = []
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(out_buf), \
                 contextlib.redirect_stderr(err_buf):
                self.rc = self._run_cmd()
        except NomnomError as e:
            self.rc = 1
            self.error = str(e)
        captured = (err_buf.getvalue() + out_buf.getvalue()).rstrip("\n")
        if captured:
            self.output_lines = captured.splitlines()
        self.step = "done"

    def _field_value(self, fid: str) -> str:
        if fid == "repo":
            return self.repo_buf or "(empty)"
        if fid == "dest":
            return _DESTINATION_LABELS[self.destination]
        return ""

    def _edit_field(self, fid: str, ch: int) -> None:
        if fid == "repo":
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                self.repo_buf = self.repo_buf[:-1]
            elif 32 <= ch < 127:
                self.repo_buf += chr(ch)

    def render(self, stdscr) -> None:  # pragma: no cover - curses I/O
        theme = _setup_theme()
        h, w = stdscr.getmaxyx()
        try:
            stdscr.addstr(0, 0, f" {self.verb} ".ljust(max(1, w - 1)),
                          theme["filter"])
        except curses.error:
            pass
        if self.step == "inputs":
            row = 2
            for i, (fid, label) in enumerate(self.fields):
                _text_input_field(
                    stdscr, row, 2, label, self._field_value(fid), theme,
                    width=max(1, w - 4),
                    active=(i == self.field_cursor),
                    read_only=fid not in self._TEXT_FIDS,
                )
                row += 3
            if self.error:
                try:
                    stdscr.addstr(row, 2, f"error: {self.error}", theme["dim"])
                except curses.error:
                    pass
            footer = ("tab:next-field  enter:run  d:dest  "
                      "esc/q:back  ?:help")
        else:  # done
            try:
                rc_attr = theme["dim"] if self.rc == 0 else theme["filter"]
                stdscr.addstr(2, 2, f"rc: {self.rc}", rc_attr)
            except curses.error:
                pass
            if self.error:
                try:
                    stdscr.addstr(3, 2, f"error: {self.error}", theme["dim"])
                except curses.error:
                    pass
            row = 5 if self.error else 4
            for line in self.output_lines:
                if row >= h - 1:
                    break
                try:
                    stdscr.addstr(row, 2, line[: max(1, w - 3)], 0)
                except curses.error:
                    pass
                row += 1
            footer = "esc/q:back  ?:help"
        try:
            stdscr.addstr(h - 1, 0, footer[: max(1, w - 1)], theme["dim"])
        except curses.error:
            pass

    def handle_key(self, ch: int, stdscr=None):
        if self.step == "inputs":
            if ch in (ord("q"), 3, 27):
                return ScreenAction.BACK
            if ch in (9, curses.KEY_DOWN):
                self.field_cursor = (self.field_cursor + 1) % len(self.fields)
                return ScreenAction.CONTINUE
            if ch == curses.KEY_UP:
                self.field_cursor = (self.field_cursor - 1) % len(self.fields)
                return ScreenAction.CONTINUE
            if ch == ord("d"):
                self._cycle_dest()
                return ScreenAction.CONTINUE
            if ch in (10, 13):
                self._execute()
                return ScreenAction.CONTINUE
            fid = self.fields[self.field_cursor][0]
            self._edit_field(fid, ch)
            return ScreenAction.CONTINUE
        # done
        if ch in (ord("q"), 3, 27, 10, 13):
            return ScreenAction.BACK
        return ScreenAction.CONTINUE


class CommitScreen(_GitContextScreen):
    """Bundle staged + unstaged diffs + recent commits via `cmd_commit`."""

    title = "Commit"
    verb = "Commit"
    help_lines = [
        "tab/arrows  move between fields",
        "type        edit the path",
        "d           cycle destination (file/clipboard/send)",
        "enter       run; esc/q to go back",
    ]

    def _run_cmd(self) -> int:
        return cmd_commit(self.repo_buf.strip(), destination=self.destination)


class PRScreen(_GitContextScreen):
    """Bundle commits-since-base + diff via `cmd_pr`."""

    title = "PR"
    verb = "PR"
    _TEXT_FIDS = ("repo", "base")
    help_lines = [
        "tab/arrows  move between fields",
        "type        edit the path or base branch",
        "d           cycle destination (file/clipboard/send)",
        "enter       run; esc/q to go back",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.base_buf = ""

    @property
    def fields(self) -> list[tuple[str, str]]:
        return [
            ("repo", "Repo path:"),
            ("base", "Base branch:"),
            ("dest", "Destination:"),
        ]

    def _field_value(self, fid: str) -> str:
        if fid == "base":
            return self.base_buf or "(default)"
        return super()._field_value(fid)

    def _edit_field(self, fid: str, ch: int) -> None:
        if fid == "base":
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                self.base_buf = self.base_buf[:-1]
            elif 32 <= ch < 127:
                self.base_buf += chr(ch)
            return
        super()._edit_field(fid, ch)

    def _run_cmd(self) -> int:
        base = self.base_buf.strip() or None
        return cmd_pr(self.repo_buf.strip(), base, destination=self.destination)


class ReviewScreen(_GitContextScreen):
    """Bundle PR meta + diff + threads via `cmd_review`."""

    title = "Review"
    verb = "Review"
    _TEXT_FIDS = ("repo", "pr")
    help_lines = [
        "tab/arrows  move between fields",
        "type        edit fields (PR number is digits-only)",
        "space       toggle the diff field when focused",
        "d           cycle destination (file/clipboard/send)",
        "enter       run; esc/q to go back",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.pr_buf = ""
        self.include_diff = False

    @property
    def fields(self) -> list[tuple[str, str]]:
        return [
            ("repo", "Repo path:"),
            ("pr", "PR number:"),
            ("diff", "Include diff:"),
            ("dest", "Destination:"),
        ]

    def _field_value(self, fid: str) -> str:
        if fid == "pr":
            return self.pr_buf or "(required)"
        if fid == "diff":
            return "yes" if self.include_diff else "no"
        return super()._field_value(fid)

    def _edit_field(self, fid: str, ch: int) -> None:
        if fid == "pr":
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                self.pr_buf = self.pr_buf[:-1]
            elif ord("0") <= ch <= ord("9"):
                self.pr_buf += chr(ch)
            return
        if fid == "diff":
            if ch == ord(" "):
                self.include_diff = not self.include_diff
            return
        super()._edit_field(fid, ch)

    def _run_cmd(self) -> int:
        pr_text = self.pr_buf.strip()
        if not pr_text:
            raise NomnomError("PR number is required")
        try:
            n = int(pr_text)
        except ValueError:
            raise NomnomError(f"PR number must be an integer: {pr_text!r}")
        return cmd_review(
            self.repo_buf.strip(), n, self.include_diff,
            destination=self.destination,
        )


@dataclass
class _RelayTransferState:
    """Shared state between the TUI main thread and the background relay worker.

    The main thread reads to render, sets the cancel_event to abort, and answers
    TOFU prompts via tofu_answer_event. The worker writes phase/progress, then
    sets `finished=True` when the transfer ends (success or failure).
    """
    phase: str = "ready"
    progress: float = 0.0
    error: str = ""
    rc: int = -1
    finished: bool = False
    tofu_request: dict | None = None
    tofu_answer: bool | None = None
    tofu_answer_event: threading.Event = field(default_factory=threading.Event)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    received_path: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)


def _progress_bar(stdscr, y: int, x: int, width: int,
                  fraction: float, label: str, theme) -> None:  # pragma: no cover
    """Render `[#######-------] 42%  label` within `width` columns."""
    pct = f" {int(round(max(0.0, min(1.0, fraction)) * 100)):3d}%"
    label_part = f"  {label}" if label else ""
    inner = max(1, width - len(pct) - 2 - len(label_part))
    filled = int(round(max(0.0, min(1.0, fraction)) * inner))
    bar = "#" * filled + "-" * (inner - filled)
    text = f"[{bar}]{pct}{label_part}"
    try:
        stdscr.addstr(y, x, text[:width], theme["dim"])
    except curses.error:
        pass


class _TransferScreen(Screen):
    """Base for SendScreen + ReceiveScreen.

    Stays in curses throughout. Runs the relay flow on a daemon thread,
    coordinates TOFU prompts through `_confirm_modal`, and re-renders every
    100ms so the progress bar updates.
    """

    POLL_INTERVAL_MS = 100

    def __init__(self) -> None:
        self.state = _RelayTransferState()
        self.thread: threading.Thread | None = None
        self.step = "ready"

    def _on_progress(self, phase: str, fraction: float) -> None:
        with self.state._lock:
            self.state.phase = phase
            self.state.progress = max(0.0, min(1.0, fraction))

    def _on_result(self, result: dict) -> None:
        with self.state._lock:
            self.state.received_path = result.get("out_path", "")

    def _on_tofu(self, req: dict) -> bool:
        # Clear BEFORE publishing the request: if the main thread sees
        # tofu_request and runs the modal + tofu_answer_event.set() between
        # the publish and the clear, the worker would wipe its own wake
        # signal and block on wait() forever.
        self.state.tofu_answer_event.clear()
        with self.state._lock:
            self.state.tofu_request = req
        # Block until main thread answers (no busy wait).
        self.state.tofu_answer_event.wait()
        with self.state._lock:
            decision = self.state.tofu_answer
            self.state.tofu_request = None
            self.state.tofu_answer = None
        return bool(decision)

    def _start_worker(self, fn, *args, **kwargs) -> None:
        """Launch the relay flow on a daemon thread, marking the screen running."""
        self.step = "running"

        def runner():
            try:
                rc = fn(*args, **kwargs)
            except KeyboardInterrupt:
                rc = 130
            except NomnomError as e:
                with self.state._lock:
                    self.state.error = str(e)
                rc = 1
            except Exception as e:
                with self.state._lock:
                    self.state.error = f"{type(e).__name__}: {e}"
                rc = 1
            with self.state._lock:
                self.state.rc = rc
                self.state.finished = True

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()

    def _check_tofu_modal(self, stdscr) -> None:  # pragma: no cover - curses I/O
        with self.state._lock:
            req = self.state.tofu_request
        if req is None:
            return
        if req.get("decision") == "changed":
            title = " TOFU mismatch "
            lines = [
                f"peer:        {req['peer_name']}",
                f"device id:   {req['peer_id']}",
                f"new fp:      {req['fingerprint']}",
            ]
            if req.get("old_ik"):
                lines.append(f"pinned fp:   {_ik_fingerprint(req['old_ik'])}")
            lines.append("verify out-of-band before accepting.")
        else:
            title = " trust new device "
            lines = [
                f"peer:        {req['peer_name']}",
                f"device id:   {req['peer_id']}",
                f"fingerprint: {req['fingerprint']}",
            ]
        ok = _confirm_modal(stdscr, title, lines, default=False)
        with self.state._lock:
            self.state.tofu_answer = ok
        self.state.tofu_answer_event.set()

    def _check_finished(self) -> None:
        with self.state._lock:
            if self.state.finished and self.step == "running":
                self.step = "done"

    def _reset_transfer_state(self) -> None:
        """Replace shared state for a fresh iteration.

        Used by screens that loop (SendScreen toast→path, ReceiveScreen
        idle→running→idle): a stale `cancel_event` or `tofu_answer_event`
        from a previous transfer would otherwise leak into the next one and
        either fire spurious cancels or deadlock TOFU waits.

        Does NOT touch `self.thread` — the ReceiveScreen's loop runner
        calls this between iterations of its own loop, so nulling the
        handle would invalidate the join() that callers (or test fixtures)
        rely on.
        """
        self.state = _RelayTransferState()

    def _render_running(self, stdscr, theme, y: int, w: int) -> None:  # pragma: no cover
        with self.state._lock:
            phase = self.state.phase
            fraction = self.state.progress
        try:
            stdscr.addstr(y, 2, phase[: max(1, w - 4)], 0)
        except curses.error:
            pass
        _progress_bar(stdscr, y + 2, 2, max(1, w - 4), fraction, "", theme)

    def _render_done(self, stdscr, theme, y: int, w: int) -> None:  # pragma: no cover
        with self.state._lock:
            rc = self.state.rc
            err = self.state.error
            received = self.state.received_path
        rc_attr = theme["dim"] if rc == 0 else theme["filter"]
        try:
            stdscr.addstr(y, 2, f"rc: {rc}", rc_attr)
        except curses.error:
            pass
        row = y + 2
        if received:
            try:
                stdscr.addstr(row, 2, f"saved: {received}"[: max(1, w - 4)], 0)
            except curses.error:
                pass
            row += 1
        if err:
            try:
                stdscr.addstr(row, 2, f"error: {err}"[: max(1, w - 4)], theme["dim"])
            except curses.error:
                pass

    def _set_render_timeout(self, stdscr) -> None:  # pragma: no cover
        # Steps that need a periodic re-render to make progress without input:
        # 'running' (progress bar) and 'toast' (auto-reset deadline). Other
        # steps block on getch.
        if self.step in ("running", "toast", "idle"):
            try:
                stdscr.timeout(self.POLL_INTERVAL_MS)
            except curses.error:
                pass
        else:
            try:
                stdscr.timeout(-1)
            except curses.error:
                pass

    def _cancel_and_back(self) -> None:
        self.state.cancel_event.set()
        # also unblock any TOFU wait so the worker thread can exit
        with self.state._lock:
            if self.state.tofu_request is not None:
                self.state.tofu_answer = False
        self.state.tofu_answer_event.set()


class SendScreen(_TransferScreen):
    """Send a file through the relay. Stays in curses; progress live.

    State machine: 'path' → optional 'peer-pick' (≥2 peers) → 'running'
    → 'toast' (rc=0, ~1.5s, then back to 'path') or 'done' (rc≠0).
    """

    title = "Send"
    verb = "Send"
    help_lines = [
        "type a file path to send",
        "enter starts the transfer (relay is required)",
        "with 2+ pinned peers, pick the target on the next screen",
        "esc cancels (cleans up any half-sent slots)",
        "after a successful send, the screen auto-resets to send another",
    ]
    TOAST_DURATION_S = 1.5

    def __init__(self) -> None:
        super().__init__()
        self.step = "path"
        self.path_buf = ""
        self.error = ""
        # peer-picker state — populated when self.step == "peer-pick"
        self._peers: list[tuple[str, dict]] = []
        self._cursor = 0
        self._pending_send: tuple[str, bytes, dict, dict] | None = None
        # Last-used peer ik_pub, set on each successful _launch_send. Used to
        # pre-position the cursor on the next peer-pick screen so a burst of
        # sends to the same person doesn't make the user re-pick every time.
        self._last_target_ik: str | None = None
        # Monotonic deadline for the toast → path auto-reset. 0.0 means no
        # toast is active.
        self._toast_until: float = 0.0
        # Last completed transfer summary, for the toast line. (name, bytes)
        self._last_sent: tuple[str, int] | None = None

    def _begin_send(self) -> None:
        self.error = ""
        path = Path(self.path_buf.strip()).expanduser()
        if not path.is_file():
            self.error = f"not a file: {path}"
            return
        try:
            data = path.read_bytes()
        except OSError as e:
            self.error = f"cannot read {path}: {e}"
            return
        relay = _load_relay_config()
        if relay is None:
            self.error = "no relay configured — run `nomnom relay init` or `nomnom join <token>` first."
            return
        identity = _load_identity()
        peers = [
            (pid, rec) for pid, rec in _load_known_peers().items()
            if isinstance(rec, dict) and isinstance(rec.get("ik_pub"), str)
        ]
        self._pending_send = (path.name, data, relay, identity)
        if not peers:
            self.error = (
                "no pinned peers — open the Pair screen to add a new device."
            )
            self._pending_send = None
            return
        if len(peers) == 1:
            self._launch_send(peers[0][1]["ik_pub"])
            return
        # Most-recently-used first; last_transfer is set by _relay_pin_peer.
        # Match the renderer's tolerance for malformed persisted values: a
        # non-numeric last_transfer sorts as "never seen" instead of crashing.
        def _last_transfer_key(pr: tuple[str, dict]) -> int:
            last = pr[1].get("last_transfer")
            return -int(last) if isinstance(last, (int, float)) else 0

        self._peers = sorted(peers, key=_last_transfer_key)
        # If the user just paired with a device, drop the cursor on it so
        # "+ pair new device" → enter → send lands on the new peer.
        # Otherwise, fall back to the last-used peer from this session.
        just_paired = PairScreen.last_paired_peer_id
        PairScreen.last_paired_peer_id = None
        self._cursor = 0
        target_ik_pref = None
        if just_paired:
            for i, (pid, _rec) in enumerate(self._peers):
                if pid == just_paired:
                    self._cursor = i
                    target_ik_pref = self._peers[i][1].get("ik_pub")
                    break
        if target_ik_pref is None and self._last_target_ik is not None:
            for i, (_pid, rec) in enumerate(self._peers):
                if rec.get("ik_pub") == self._last_target_ik:
                    self._cursor = i
                    break
        self.step = "peer-pick"

    def _launch_send(self, target_ik: str) -> None:
        if self._pending_send is None:
            self.error = "internal: no pending send"
            return
        name, data, relay, identity = self._pending_send
        self._pending_send = None
        # Stash for next iteration's peer-pick pre-selection; also held in
        # `self._last_sent` once the worker finishes so the toast can render
        # the file name + size after _pending_send has already been cleared.
        self._last_target_ik = target_ik
        self._last_sent = (name, len(data))
        self._start_worker(
            _relay_send, name, data,
            target_ik_hex=target_ik,
            identity=identity, relay=relay,
            on_progress=self._on_progress,
            on_tofu=self._on_tofu,
            cancel=self.state.cancel_event,
        )

    def _check_finished(self) -> None:
        # Override so a clean send transitions into a brief toast rather
        # than terminating on 'done'. rc≠0 keeps the original behavior.
        with self.state._lock:
            if self.state.finished and self.step == "running":
                if self.state.rc == 0:
                    self.step = "toast"
                    self._toast_until = time.monotonic() + self.TOAST_DURATION_S
                else:
                    self.step = "done"

    def _reset_for_next_send(self) -> None:
        """Wipe per-transfer scratch and return to the path field."""
        self._reset_transfer_state()
        self.path_buf = ""
        self.error = ""
        self._pending_send = None
        self._peers = []
        self._cursor = 0
        self._toast_until = 0.0
        self.step = "path"

    def _render_toast(self, stdscr, theme, y: int, w: int) -> None:  # pragma: no cover
        if self._last_sent is None:
            line = "sent."
        else:
            name, size = self._last_sent
            line = f"sent {name!r} ({size} bytes)."
        try:
            stdscr.addstr(y, 2, line[: max(1, w - 4)], theme["checked"])
        except curses.error:
            pass
        try:
            stdscr.addstr(y + 2, 2,
                          "ready for another send..."[: max(1, w - 4)],
                          theme["dim"])
        except curses.error:
            pass

    def render(self, stdscr) -> None:  # pragma: no cover - curses I/O
        self._check_finished()
        if self.step == "toast" and time.monotonic() >= self._toast_until:
            self._reset_for_next_send()
        theme = _setup_theme()
        self._set_render_timeout(stdscr)
        h, w = stdscr.getmaxyx()
        try:
            stdscr.addstr(0, 0, f" {self.title} ".ljust(max(1, w - 1)),
                          theme["filter"])
        except curses.error:
            pass
        if self.step == "path":
            _text_input_field(stdscr, 2, 2, "File to send:", self.path_buf,
                              theme, max(1, w - 4))
            if self.error:
                try:
                    stdscr.addstr(5, 2, f"error: {self.error}"[: max(1, w - 4)],
                                  theme["dim"])
                except curses.error:
                    pass
            footer = "enter:send  esc/q:back  ?:help"
        elif self.step == "peer-pick":
            self._render_peer_pick(stdscr, theme, w)
            footer = "↑/↓ j/k:move  enter:send  esc:back  ?:help"
        elif self.step == "running":
            self._render_running(stdscr, theme, 2, w)
            self._check_tofu_modal(stdscr)
            footer = "esc:cancel  ?:help"
        elif self.step == "toast":
            self._render_toast(stdscr, theme, 2, w)
            footer = "any key:next  esc/q:back  ?:help"
        else:
            self._render_done(stdscr, theme, 2, w)
            footer = "esc/q:back  ?:help"
        try:
            stdscr.addstr(h - 1, 0, footer[: max(1, w - 1)], theme["dim"])
        except curses.error:
            pass

    def _render_peer_pick(self, stdscr, theme, w: int) -> None:  # pragma: no cover
        try:
            stdscr.addstr(2, 2, "Send to:", theme["dim"])
        except curses.error:
            pass
        rows = list(self._peers) + [("+pair", None)]
        for i, item in enumerate(rows):
            row_y = 4 + i
            attr = theme["cursor"] if i == self._cursor else 0
            if item[1] is None:
                line = "  + pair new device"
            else:
                pid, rec = item
                label = rec.get("nickname") or rec.get("name") or pid
                last = rec.get("last_transfer")
                last_str = (
                    datetime.fromtimestamp(int(last)).strftime("%Y-%m-%d %H:%M")
                    if isinstance(last, (int, float)) and last else "never"
                )
                line = f"  {label:<24}  last used: {last_str}"
            try:
                stdscr.addstr(row_y, 2, line[: max(1, w - 4)], attr)
            except curses.error:
                pass

    def handle_key(self, ch: int, stdscr=None):
        if self.step == "path":
            if ch in (ord("q"), 3, 27):
                return ScreenAction.BACK
            if ch in (10, 13):
                self._begin_send()
                return ScreenAction.CONTINUE
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                self.path_buf = self.path_buf[:-1]
                return ScreenAction.CONTINUE
            if 32 <= ch < 127:
                self.path_buf += chr(ch)
            return ScreenAction.CONTINUE
        if self.step == "peer-pick":
            row_count = len(self._peers) + 1  # +1 for "+ pair new device"
            if ch in (ord("q"), 27):
                self.step = "path"
                self._pending_send = None
                return ScreenAction.CONTINUE
            if ch in (curses.KEY_DOWN, ord("j")):
                self._cursor = (self._cursor + 1) % row_count
                return ScreenAction.CONTINUE
            if ch in (curses.KEY_UP, ord("k")):
                self._cursor = (self._cursor - 1) % row_count
                return ScreenAction.CONTINUE
            if ch in (10, 13):
                if self._cursor < len(self._peers):
                    self._launch_send(self._peers[self._cursor][1]["ik_pub"])
                else:
                    # "+ pair new device" — push PairScreen. Pair is now
                    # identity-only, so the pending file stays buffered;
                    # user comes back to the path field afterwards.
                    self._pending_send = None
                    self.step = "path"
                    return PairScreen()
            return ScreenAction.CONTINUE
        if self.step == "running":
            if ch in (27,):
                self._cancel_and_back()
            return ScreenAction.CONTINUE
        if self.step == "toast":
            if ch in (ord("q"), 3, 27):
                return ScreenAction.BACK
            # Any other key: short-circuit the 1.5s timer.
            self._reset_for_next_send()
            return ScreenAction.CONTINUE
        if ch in (ord("q"), 3, 27, 10, 13):
            return ScreenAction.BACK
        return ScreenAction.CONTINUE


class ReceiveScreen(_TransferScreen):
    """Listen for transfers until the user exits.

    State machine: 'blocked' (preflight failed — show error, q to back) or
    'watching' (worker loop alive, log accumulates). Within 'watching', the
    underlying long-poll/transfer phase is read from `state.progress` /
    `state.phase`: progress == 0.0 means idle (long-poll), > 0.0 means a
    transfer is in flight.

    Esc semantics:
    - idle (progress == 0.0): exit to launcher.
    - running (progress > 0.0): cancel that transfer; the worker loop
      swallows rc=130, resets primitives, and re-arms.
    """

    title = "Receive"
    verb = "Receive"
    help_lines = [
        "auto-starts listening for pinned peers",
        "received files accumulate in the on-screen log",
        "esc during a transfer cancels that file; esc while idle exits",
    ]
    LOG_MAX = 20

    def __init__(self) -> None:
        super().__init__()
        self.error = ""
        self._log: list[dict] = []
        self._log_lock = threading.Lock()
        self._loop_exit = threading.Event()
        self._stopped_reason = ""
        relay = _load_relay_config()
        if relay is None:
            self.step = "blocked"
            self.error = "no relay configured — run `nomnom relay init` or `nomnom join <token>` first."
            return
        if not _load_known_peers():
            self.step = "blocked"
            self.error = "no pinned peers — open the Pair screen to add a new device."
            return
        self.step = "watching"
        identity = _load_identity()
        self._start_watch_worker(identity, relay)

    def _start_watch_worker(self, identity: dict, relay: dict) -> None:
        def _on_result(result: dict) -> None:
            row = {
                "name": result.get("name", "?"),
                "bytes": result.get("bytes", 0),
                "peer_name": result.get("peer_name", "?"),
                "out_path": result.get("out_path", ""),
                "ts": time.time(),
            }
            with self._log_lock:
                self._log.append(row)
                if len(self._log) > self.LOG_MAX:
                    del self._log[: len(self._log) - self.LOG_MAX]

        def runner() -> None:
            while not self._loop_exit.is_set():
                # Per-iteration: fresh cancel_event / tofu_event / progress.
                self._reset_transfer_state()
                try:
                    rc = _relay_recv_pinned(
                        identity=identity, relay=relay,
                        on_progress=self._on_progress,
                        on_tofu=self._on_tofu,
                        on_result=_on_result,
                        cancel=self.state.cancel_event,
                    )
                except NomnomError as e:
                    # Benign 30s timeout is the steady-state idle case in
                    # the watch loop — re-arm. The error-loaded variant
                    # ("no transfer: ...") carries a real reason and exits.
                    if str(e).startswith("no transfer (waited"):
                        continue
                    self._stopped_reason = str(e)
                    self._loop_exit.set()
                    return
                except Exception as e:
                    self._stopped_reason = f"{type(e).__name__}: {e}"
                    self._loop_exit.set()
                    return
                # rc==130: cancel_event was set (either an in-flight cancel
                # from the user, or our own _loop_exit propagation). The
                # while header handles whether to re-arm.
                # rc!=0 & rc!=130: surface and exit.
                if rc not in (0, 130):
                    self._stopped_reason = f"relay returned rc={rc}"
                    self._loop_exit.set()
                    return

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()

    def _is_transferring(self) -> bool:
        with self.state._lock:
            return self.state.progress > 0.0

    def _render_log(self, stdscr, theme, y: int, w: int) -> int:  # pragma: no cover
        with self._log_lock:
            entries = list(reversed(self._log))  # newest at top
        if not entries:
            try:
                stdscr.addstr(y, 2, "(no transfers yet)"[: max(1, w - 4)],
                              theme["dim"])
            except curses.error:
                pass
            return y + 1
        for i, row in enumerate(entries):
            ts = datetime.fromtimestamp(int(row["ts"])).strftime("%H:%M:%S")
            line = (
                f"[{ts}] {row['name']} {row['bytes']}B from "
                f"{row['peer_name']} -> {row['out_path']}"
            )
            try:
                stdscr.addstr(y + i, 2, line[: max(1, w - 4)], 0)
            except curses.error:
                pass
        return y + len(entries)

    def render(self, stdscr) -> None:  # pragma: no cover - curses I/O
        theme = _setup_theme()
        self._set_render_timeout(stdscr)
        h, w = stdscr.getmaxyx()
        try:
            stdscr.addstr(0, 0, f" {self.title} ".ljust(max(1, w - 1)),
                          theme["filter"])
        except curses.error:
            pass

        if self.step == "blocked":
            try:
                stdscr.addstr(2, 2, f"error: {self.error}"[: max(1, w - 4)],
                              theme["dim"])
            except curses.error:
                pass
            footer = "esc/q:back  ?:help"
        else:
            log_height = max(1, min(self.LOG_MAX, h - 7))
            self._render_log(stdscr, theme, 2, w)
            status_y = 2 + log_height + 1
            if self._loop_exit.is_set():
                msg = (
                    f"listener stopped: {self._stopped_reason}"
                    if self._stopped_reason else "listener stopped."
                )
                try:
                    stdscr.addstr(status_y, 2, msg[: max(1, w - 4)],
                                  theme["filter"])
                except curses.error:
                    pass
                footer = "esc/q:back  ?:help"
            elif self._is_transferring():
                self._render_running(stdscr, theme, status_y, w)
                self._check_tofu_modal(stdscr)
                footer = "esc:cancel transfer  ?:help"
            else:
                try:
                    stdscr.addstr(status_y, 2,
                                  "waiting for transfers..."[: max(1, w - 4)],
                                  theme["dim"])
                except curses.error:
                    pass
                footer = "esc/q:back  ?:help"
        try:
            stdscr.addstr(h - 1, 0, footer[: max(1, w - 1)], theme["dim"])
        except curses.error:
            pass

    def handle_key(self, ch: int, stdscr=None):
        if self.step == "blocked" or self._loop_exit.is_set():
            if ch in (ord("q"), 3, 27, 10, 13):
                self._loop_exit.set()
                # Wake any in-flight long-poll so the worker thread can exit.
                self._cancel_and_back()
                return ScreenAction.BACK
            return ScreenAction.CONTINUE
        if ch == 27:
            if self._is_transferring():
                # Cancel just this transfer; the worker loop re-arms.
                self._cancel_and_back()
                return ScreenAction.CONTINUE
            # Idle: exit. cancel_event wakes the long-poll so the worker
            # thread can see _loop_exit and return promptly.
            self._loop_exit.set()
            self._cancel_and_back()
            return ScreenAction.BACK
        if ch in (ord("q"), 3):
            self._loop_exit.set()
            self._cancel_and_back()
            return ScreenAction.BACK
        return ScreenAction.CONTINUE


class PairScreen(_TransferScreen):
    """Pair with a new device. Symmetric: both sides open this screen and
    race for the per-relay rendezvous slot. TOFU prompts gate trust on
    both ends; pinning is local so asymmetric decline outcomes are fine."""

    title = "Pair"
    verb = "Pair"
    help_lines = [
        "both devices open this screen at the same time",
        "TOFU prompt shows the other side's fingerprint",
        "esc cancels",
    ]

    # Stash the most-recently-paired peer's device_id so SendScreen can
    # auto-highlight it on next entry. Class attribute (vs module-level)
    # keeps the contract narrow: only PairScreen writes, only SendScreen
    # reads + clears.
    last_paired_peer_id: str | None = None

    def __init__(self) -> None:
        super().__init__()
        relay = _load_relay_config()
        if relay is None:
            self.step = "done"
            with self.state._lock:
                self.state.error = (
                    "no relay configured — run `nomnom relay init` or `nomnom join <token>` first."
                )
                self.state.rc = 2
                self.state.finished = True
            return
        identity = _load_identity()
        self._start_worker(
            _relay_pair,
            identity=identity, relay=relay,
            on_progress=self._on_progress,
            on_tofu=self._on_tofu,
            on_result=self._on_result,
            cancel=self.state.cancel_event,
        )

    def _on_result(self, result: dict) -> None:
        with self.state._lock:
            self.state.received_path = (
                f"paired with {result.get('peer_name', '?')!r} "
                f"({(result.get('peer_id') or '')[:8]}) "
                f"as {result.get('role', '?')}"
            )
        PairScreen.last_paired_peer_id = result.get("peer_id") or None

    def render(self, stdscr) -> None:  # pragma: no cover - curses I/O
        self._check_finished()
        theme = _setup_theme()
        self._set_render_timeout(stdscr)
        h, w = stdscr.getmaxyx()
        try:
            stdscr.addstr(0, 0, f" {self.title} ".ljust(max(1, w - 1)),
                          theme["filter"])
        except curses.error:
            pass
        if self.step == "running":
            self._render_running(stdscr, theme, 2, w)
            self._check_tofu_modal(stdscr)
            footer = "esc:cancel  ?:help"
        else:
            self._render_done(stdscr, theme, 2, w)
            footer = "esc/q:back  ?:help"
        try:
            stdscr.addstr(h - 1, 0, footer[: max(1, w - 1)], theme["dim"])
        except curses.error:
            pass

    def handle_key(self, ch: int, stdscr=None):
        if self.step == "running":
            if ch in (27,):
                self._cancel_and_back()
            return ScreenAction.CONTINUE
        if ch in (ord("q"), 3, 27, 10, 13):
            return ScreenAction.BACK
        return ScreenAction.CONTINUE


def _wrap(text: str, width: int) -> list[str]:
    """Simple word-wrap; preserves paragraph order, drops nothing."""
    if width <= 0:
        return [text]
    out: list[str] = []
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            cand = (line + " " + word).strip()
            if len(cand) <= width:
                line = cand
            else:
                if line:
                    out.append(line)
                line = word
        out.append(line)
    return out


def _text_input_field(
    stdscr, y: int, x: int, label: str, buf: str, theme,
    width: int = -1,
    *,
    active: bool = False,
    read_only: bool = False,
) -> None:  # pragma: no cover - curses I/O
    """Render `label` on row `y` and the value on row `y+1` at column `x`.

    Used by every screen that takes a free-text field (path, PR number,
    extension value). The caller owns the input state; this helper only
    draws. Clips long buffers to `width` (use the screen's available
    columns).

    `active=True` highlights the label so the focused row stands out.
    `read_only=True` drops the leading `> ` prompt — use for status/toggle
    rows (destination, diff toggle) that the user interacts with via a
    dedicated key, not by typing."""
    try:
        label_attr = theme["cursor"] if active else theme["dim"]
        stdscr.addstr(y, x, label, label_attr)
        line = buf if read_only else "> " + buf
        if width > 0:
            line = line[:width]
        stdscr.addstr(y + 1, x, line, 0)
    except curses.error:
        pass


def _confirm_modal(
    stdscr, title: str, lines: list[str], default: bool = False,
) -> bool:  # pragma: no cover - curses I/O
    """Centered modal with a y/N (or Y/n) gate. Blocks on getch.

    Returns True for `y`/`Y`, False for `n`/`N`/Esc, and `default` on
    Enter. Useful for destructive confirmations from inside a Screen."""
    h, w = stdscr.getmaxyx()
    prompt = " [Y/n] " if default else " [y/N] "
    body = [title.center(50, "─"), *lines, "─" * 50, prompt]
    box_w = min(max(1, w - 2), max(len(r) for r in body) + 4)
    box_h = min(max(1, h - 2), len(body) + 2)
    y0 = max(0, (h - box_h) // 2)
    x0 = max(0, (w - box_w) // 2)
    for i in range(box_h):
        try:
            stdscr.addstr(y0 + i, x0, " " * box_w, curses.A_REVERSE)
        except curses.error:
            pass
    for i, line in enumerate(body[: box_h - 1]):
        try:
            stdscr.addstr(y0 + 1 + i, x0 + 2,
                          line[: max(0, box_w - 4)], curses.A_REVERSE)
        except curses.error:
            pass
    stdscr.refresh()
    try:
        stdscr.timeout(-1)
    except curses.error:
        pass
    while True:
        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27, 3):
            return False
        if ch in (10, 13):
            return default


def _prompt_pr_number(stdscr) -> int | None:  # pragma: no cover - curses I/O
    """Centered overlay collecting a digits-only PR number.

    Returns the int on Enter (when the buffer is non-empty), None on Esc."""
    buf = ""
    try:
        stdscr.timeout(-1)
    except curses.error:
        pass
    while True:
        h, w = stdscr.getmaxyx()
        title = " PR number ".center(50, "─")
        prompt = f"  > {buf}_"
        body = [title, "", prompt, "", "─" * 50, "  enter: confirm   esc: cancel"]
        box_w = min(max(1, w - 2), max(len(r) for r in body) + 4)
        box_h = min(max(1, h - 2), len(body) + 2)
        y0 = max(0, (h - box_h) // 2)
        x0 = max(0, (w - box_w) // 2)
        for i in range(box_h):
            try:
                stdscr.addstr(y0 + i, x0, " " * box_w, curses.A_REVERSE)
            except curses.error:
                pass
        for i, line in enumerate(body[: box_h - 1]):
            try:
                stdscr.addstr(y0 + 1 + i, x0 + 2,
                              line[: max(0, box_w - 4)], curses.A_REVERSE)
            except curses.error:
                pass
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch in (27, 3):
            return None
        if ch in (10, 13):
            if buf:
                try:
                    n = int(buf)
                except ValueError:
                    buf = ""
                    continue
                if n > 0:
                    return n
            continue
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            buf = buf[:-1]
            continue
        if ord("0") <= ch <= ord("9"):
            buf += chr(ch)


# ---------- main ----------

def _build_subcommand_parser(verb: str) -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog=f"nomnom {verb}",
        description=(
            f"{verb.capitalize()} an entry in the auto-managed extension "
            "lists in nomnom.py. After it runs, review with `git diff "
            "nomnom.py` and commit when happy."
        ),
    )
    sub.add_argument(
        "kind", choices=list(KINDS),
        help="which list to edit: text | binary | name | secret",
    )
    sub.add_argument(
        "values", nargs="+",
        help="one or more entries (e.g. .rmeta, MODULE.bazel, '*.creds')",
    )
    return sub


def _add_destination_flags(sub: argparse.ArgumentParser) -> None:
    """Attach `--clipboard` / `--stdout` to a verb parser; default is FILE."""
    grp = sub.add_mutually_exclusive_group()
    grp.add_argument(
        "--clipboard", action="store_true",
        help="Copy output to the system clipboard instead of writing a file.",
    )
    grp.add_argument(
        "--stdout", action="store_true",
        help="Pipe output to stdout (script-friendly, no TTY required).",
    )


def _destination_from_args(args) -> Destination:
    if getattr(args, "stdout", False):
        return Destination.STDOUT
    if getattr(args, "clipboard", False):
        return Destination.CLIPBOARD
    return Destination.FILE


def _build_commit_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom commit",
        description=(
            "Bundle git context (status, diffs, recent commits) into a .txt "
            "for an LLM to draft a commit message."
        ),
    )
    sub.add_argument(
        "repo", nargs="?", default=".",
        help="Path to the project repo (default: current directory).",
    )
    _add_destination_flags(sub)
    return sub


def _build_pr_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom pr",
        description=(
            "Bundle git + gh context (commits since base, full diff, "
            "existing PR body) into a .txt for an LLM to draft a PR body."
        ),
    )
    sub.add_argument(
        "repo", nargs="?", default=".",
        help="Path to the project repo (default: current directory).",
    )
    _add_destination_flags(sub)
    sub.add_argument(
        "--base", default=None,
        help="Base branch to diff against (default: gh repo default branch).",
    )
    return sub


def _build_review_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom review",
        description=(
            "Bundle gh context for an existing PR (title, body, comments, "
            "reviews, inline review threads, checks) into a .txt for an LLM "
            "to reason about the review."
        ),
    )
    sub.add_argument(
        "pr_number", type=int,
        help="PR number to fetch (resolves against the current repo's gh remote).",
    )
    sub.add_argument(
        "repo", nargs="?", default=".",
        help="Path to the project repo (default: current directory).",
    )
    _add_destination_flags(sub)
    sub.add_argument(
        "--diff", action="store_true",
        help=(
            "Include the full diff (off by default; inline review comments "
            "carry their own diff hunks)."
        ),
    )
    return sub


def _build_rebuild_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom rebuild",
        description=(
            "Reconstruct a file tree from a nomnom bundle (the .txt output of "
            "the main `nomnom` command). Creates a new folder under the "
            "current directory; auto-suffixes -1, -2, ... on name collisions."
        ),
    )
    sub.add_argument(
        "bundle", nargs="?", default=None,
        help="Path to a bundle .txt (default: read from stdin).",
    )
    sub.add_argument(
        "--name", default=None,
        help=(
            "Override the target folder name (default: the repo name from "
            "the bundle's header)."
        ),
    )
    return sub


def _build_send_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom send",
        description=(
            "Send a file to a pinned peer through the Cloudflare relay. "
            "Nothing is written to disk on this side. With exactly one peer "
            "pinned, auto-targets it. With multiple peers, pass --to PEER. "
            "With zero peers, run `nomnom pair` instead. The relay sees "
            "only ciphertext."
        ),
    )
    sub.add_argument("file", help="Path to the file to send.")
    sub.add_argument(
        "--to", default=None, metavar="PEER",
        help="Send to this pinned peer (device id, name, or nickname).",
    )
    sub.add_argument(
        "--trust-new", action="store_true",
        help=(
            "Auto-accept TOFU prompts (new pins, key rotations). Scriptable "
            "but loses the explicit gate; an audit line is still written "
            "to stderr."
        ),
    )
    return sub


def _build_receive_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom receive",
        description=(
            "Long-poll every pinned peer's rendezvous slot and write each "
            "received file into the current directory. Default: keep "
            "listening (Ctrl-C exits). With zero pinned peers, errors: "
            "run `nomnom pair` to add a new device."
        ),
    )
    sub.add_argument(
        "--once", action="store_true",
        help=(
            "Exit after the first received file. Use for scripting; the "
            "default is to keep listening for subsequent transfers."
        ),
    )
    sub.add_argument(
        "--trust-new", action="store_true",
        help=(
            "Auto-accept TOFU prompts on key rotation. Scriptable but loses "
            "the explicit gate; an audit line is still written to stderr."
        ),
    )
    return sub


def _build_reset_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="nomnom reset",
        description=(
            "Wipe ~/.config/nomnom/ (identity, pinned peers, relay config) "
            "after a y/N prompt. Identity rotation invalidates every remote "
            "pin, so peer devices will see a TOFU mismatch on the next "
            "transfer."
        ),
    )


def _build_pair_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom pair",
        description=(
            "Pair with a new device through the relay. Symmetric: both "
            "devices run `nomnom pair` and exchange identities at the "
            "per-relay first-contact slot. Trust is gated by a TOFU "
            "fingerprint prompt on both sides. Both sides must share the "
            "relay HMAC secret. Send a file separately afterwards with "
            "`nomnom send` / `nomnom receive`."
        ),
    )
    sub.add_argument(
        "--trust-new", action="store_true",
        help=(
            "Auto-accept the first-contact TOFU prompt (scriptable but loses "
            "verification). An audit line is still written to stderr."
        ),
    )
    return sub


def _build_relay_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom relay",
        description=(
            "Manage the Cloudflare relay configuration. The relay is a Worker "
            "you deploy to your own Cloudflare account (see relay-worker/)."
        ),
    )
    sp = sub.add_subparsers(dest="action", metavar="ACTION")
    private_help = (
        "Allow URLs that resolve to private / loopback / link-local "
        "addresses (e.g., a local dev Worker). Default refuses them to "
        "block SSRF via a hostile relay blob."
    )
    p_init = sp.add_parser(
        "init",
        help=(
            "First-device setup: prompt URL, generate HMAC secret, save, "
            "print the wrangler commands to push the secret + deploy."
        ),
    )
    p_init.add_argument("--allow-private", action="store_true", help=private_help)
    sp.add_parser("test", help="Round-trip check: hits /health then PUT + GET.")
    p_show = sp.add_parser("show", help="Print current config (secret redacted).")
    p_show.add_argument(
        "--token", action="store_true",
        help=(
            "Print the shareable `host#secret` join token for `nomnom join` "
            "on another device. The secret is NOT redacted."
        ),
    )
    sp.add_parser("clear", help="Delete relay.json after confirmation.")
    return sub


def _build_join_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom join",
        description=(
            "Join an existing nomnom relay using a `host#secret` token "
            "produced by `nomnom relay show --token` on the first device. "
            "Self-tests the relay before saving."
        ),
    )
    sub.add_argument("token", help="Join token of the form `host#secret`.")
    sub.add_argument(
        "--allow-private", action="store_true",
        help=(
            "Allow tokens whose host resolves to a private / loopback / "
            "link-local address. Default refuses to block SSRF."
        ),
    )
    return sub


def _build_peers_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom peers",
        description="Inspect and manage the TOFU peer pins.",
    )
    sp = sub.add_subparsers(dest="action", metavar="ACTION")
    sp.add_parser("list", help="Show all pinned peers.")
    p_nick = sp.add_parser("nickname", help="Set or clear a peer nickname.")
    p_nick.add_argument("peer", help="Peer device id, name, or current nickname.")
    p_nick.add_argument("name", nargs="?", default="",
                        help="New nickname; pass an empty string to clear.")
    p_forget = sp.add_parser("forget", help="Drop a pinned peer.")
    p_forget.add_argument("peer", help="Peer device id, name, or nickname.")
    p_fp = sp.add_parser("fingerprint",
                         help="Print a peer's identity fingerprint for OOB verification.")
    p_fp.add_argument("peer", help="Peer device id, name, or nickname.")
    return sub


def _refuse_copy_subcommand() -> int:
    print(
        "error: --copy was removed. Use --clipboard for the same effect, "
        "or --stdout | pbcopy to pipe.",
        file=sys.stderr,
    )
    return 2


def _refuse_copy_bare() -> int:
    print(
        "error: --copy was removed. Press `d` in the picker to cycle to "
        "the clipboard destination, or pipe `--stdout` into pbcopy/"
        "wl-copy/xclip.",
        file=sys.stderr,
    )
    return 2


# Verb → (argparser factory, handler). `main()` gates entry on this dict's keys,
# so the dispatcher does a direct lookup with no fallback branch.
_DISPATCH = {
    "register": (
        lambda: _build_subcommand_parser("register"),
        lambda a: cmd_register(a.kind, a.values, remove=False),
    ),
    "unregister": (
        lambda: _build_subcommand_parser("unregister"),
        lambda a: cmd_register(a.kind, a.values, remove=True),
    ),
    "commit": (
        _build_commit_parser,
        lambda a: cmd_commit(a.repo, destination=_destination_from_args(a)),
    ),
    "pr": (
        _build_pr_parser,
        lambda a: cmd_pr(a.repo, a.base, destination=_destination_from_args(a)),
    ),
    "review": (
        _build_review_parser,
        lambda a: cmd_review(a.repo, a.pr_number, a.diff,
                             destination=_destination_from_args(a)),
    ),
    "rebuild": (
        _build_rebuild_parser,
        lambda a: cmd_rebuild(a.bundle, a.name),
    ),
    "send": (
        _build_send_parser,
        lambda a: cmd_send(a.file, to=a.to, trust_new=a.trust_new),
    ),
    "receive": (
        _build_receive_parser,
        lambda a: cmd_receive(once=a.once, trust_new=a.trust_new),
    ),
    "pair": (
        _build_pair_parser,
        lambda a: cmd_pair(trust_new=a.trust_new),
    ),
    "relay": (
        _build_relay_parser,
        cmd_relay,
    ),
    "join": (
        _build_join_parser,
        cmd_join,
    ),
    "peers": (
        _build_peers_parser,
        cmd_peers,
    ),
    "reset": (
        _build_reset_parser,
        cmd_reset,
    ),
}

SUBCOMMANDS = tuple(_DISPATCH)


def _dispatch_subcommand(argv: list[str]) -> int:
    # Mixing argparse subparsers with the optional `repo` positional confuses
    # argparse's positional matcher, so we sniff the verb and dispatch by hand.
    if "--copy" in argv[1:]:
        return _refuse_copy_subcommand()
    parser_factory, handler = _DISPATCH[argv[0]]
    try:
        return handler(parser_factory().parse_args(argv[1:]))
    except NomnomError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _emit_bundle(
    repo_name: str,
    root: Path,
    selected: list[str],
    include_tree: bool,
    destination: Destination,
    *,
    interactive: bool = True,
) -> tuple[int, list[str]]:
    """Render the bundle and dispatch to the chosen destination.

    Returns (rc, status lines). The CLI prints the lines to stderr; the
    TUI displays them on the done screen. STDOUT and SEND may still
    write to their own streams as a side effect of dispatching."""
    tree_str = render_ascii_tree(selected, repo_name) if include_tree else None
    output = render_output(repo_name, root, selected, tree_str)

    if destination == Destination.STDOUT:
        sys.stdout.write(output)
        sys.stdout.flush()
        return 0, [f"wrote {len(output):,} bytes to stdout."]

    if destination == Destination.SEND:
        name = f"{repo_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        rc = _bundle_send_via_relay(
            name, output.encode("utf-8"),
            interactive=interactive,
        )
        return rc, []

    messages: list[str] = []
    if destination == Destination.CLIPBOARD:
        if copy_to_clipboard(output):
            return 0, [f"copied {len(output):,} bytes to clipboard."]
        messages.append(
            "no clipboard tool found (pbcopy/wl-copy/xclip/xsel); "
            "falling back to a file."
        )

    out_path = pick_output_path(repo_name)
    try:
        out_path.write_text(output, encoding="utf-8")
    except OSError as e:
        messages.append(f"error writing output: {e}")
        return 1, messages
    messages.append(f"wrote {out_path}")
    return 0, messages


def main() -> int:
    # Trip any pin-store migration up front so the notice lands on stderr
    # before either entry point claims the terminal (curses or CLI).
    _load_known_peers()

    if len(sys.argv) >= 2 and sys.argv[1] in SUBCOMMANDS:
        return _dispatch_subcommand(sys.argv[1:])

    if len(sys.argv) == 1 and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            run_app(LauncherScreen())
        except NomnomError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    if "--copy" in sys.argv[1:]:
        return _refuse_copy_bare()

    parser = argparse.ArgumentParser(
        description="nomnom: feed your repo to the LLM, one .txt snack at a time.",
        epilog=(
            "subcommands: register / unregister edit the auto-managed "
            "extension lists; commit / pr / review bundle git context for "
            "an LLM; rebuild reconstructs a file tree from a bundle .txt; "
            "relay init sets up a new relay on this machine; join <token> "
            "joins an existing relay from another device; pair adds a new "
            "device through the relay; send / receive move files between "
            "pinned machines via the Cloudflare Worker relay you deploy "
            "yourself (receive listens until Ctrl-C); peers manages pinned "
            "identities; reset wipes ~/.config/nomnom/. "
            "run `nomnom <subcommand> --help`."
        ),
    )
    parser.add_argument(
        "repo", nargs="?", default=".",
        help="Path to the project repo (default: current directory).",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Skip the picker and bundle every scanned file. Combine with "
             "--include / --exclude to script a filtered bundle.",
    )
    parser.add_argument(
        "--include", action="append", default=[], metavar="GLOB",
        help="Gitignore-style include pattern (repeatable). Without --all/"
             "--stdout, matching files are pre-selected in the picker.",
    )
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB",
        help="Gitignore-style exclude pattern (repeatable). Applied after "
             "--include.",
    )
    parser.add_argument(
        "--stdout", action="store_true",
        help="Skip the picker and write the bundle to stdout. Implies a "
             "fully-scriptable run (no TTY required).",
    )
    parser.add_argument(
        "--include-secrets", action="store_true",
        help="Disable the default skip of .env, *.pem, id_rsa*, etc.",
    )
    parser.add_argument(
        "--include-ignored", action="store_true",
        help="Bundle files normally excluded by .gitignore rules.",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable colored output (also honors the NO_COLOR env var).",
    )
    args = parser.parse_args()

    if args.no_color:
        os.environ["NO_COLOR"] = "1"

    root = Path(args.repo).expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 1
    if not root.name:
        print(f"error: cannot derive a repo name from {root}", file=sys.stderr)
        return 1

    skip_picker = args.all or args.stdout
    if not skip_picker and not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "error: nomnom needs a TTY for the interactive picker. "
            "Use --all / --include / --stdout to script.",
            file=sys.stderr,
        )
        return 1

    repo_name = root.name
    print(f"scanning {root} ...", file=sys.stderr)
    gi = GitignoreMatcher([]) if args.include_ignored else load_gitignore(root)
    items = scan_repo(root, gi, skip_secrets=not args.include_secrets)
    file_items = [it for it in items if not it.is_dir]
    if not file_items:
        print("no files found after applying excludes.", file=sys.stderr)
        return 0
    print(f"  {len(file_items)} files, {sum(1 for it in items if it.is_dir)} dirs",
          file=sys.stderr)

    matched_rels: set[str] | None = None
    if args.include or args.exclude:
        filtered = apply_include_exclude(items, args.include, args.exclude)
        matched_rels = {it.rel for it in filtered if not it.is_dir}
        if not matched_rels:
            print("no files matched --include / --exclude.", file=sys.stderr)
            return 0

    if skip_picker:
        if matched_rels is not None:
            selected = sorted(matched_rels)
        else:
            selected = sorted(it.rel for it in file_items)
        include_tree = True
        destination = Destination.STDOUT if args.stdout else Destination.FILE
    else:
        print("reading file stats...", file=sys.stderr)
        t0 = time.monotonic()
        stats = collect_stats(root, items)
        print(f"  done ({time.monotonic() - t0:.1f}s).", file=sys.stderr)
        nodes = build_tree(items, stats=stats)
        if matched_rels is not None:
            for n in nodes:
                if not n.is_dir and n.rel in matched_rels:
                    n.checked = True
        allow_git_verbs = _is_inside_git_repo(root)
        result = pick(nodes, root=root, allow_git_verbs=allow_git_verbs)
        if result is None:
            print("cancelled.", file=sys.stderr)
            return 130
        if result.verb != Verb.BUNDLE:
            if matched_rels is not None:
                print(
                    f"warning: --include / --exclude are ignored for verb "
                    f"{result.verb.name.lower()}.",
                    file=sys.stderr,
                )
            try:
                if result.verb == Verb.COMMIT:
                    return cmd_commit(str(root), destination=result.destination)
                if result.verb == Verb.PR:
                    return cmd_pr(str(root), None, destination=result.destination)
                if result.verb == Verb.REVIEW:
                    if result.review_pr is None:
                        raise NomnomError(
                            "review verb selected without a PR number",
                        )
                    return cmd_review(
                        str(root), result.review_pr, True,
                        destination=result.destination,
                    )
            except NomnomError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
        if not result.selected:
            print("no files selected.", file=sys.stderr)
            return 0
        selected = sorted(result.selected)
        include_tree = result.include_tree
        destination = result.destination

    rc, lines = _emit_bundle(repo_name, root, selected, include_tree, destination)
    for line in lines:
        print(line, file=sys.stderr)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ncancelled.", file=sys.stderr)
        sys.exit(130)
