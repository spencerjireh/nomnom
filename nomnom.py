#!/usr/bin/env python3
"""nomnom.py - feed your repo to the LLM, one .txt snack at a time.

Run: python3 nomnom.py [/path/to/repo]
Stdlib only. macOS/Linux. Python 3.8+.
"""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
import contextlib
import curses
import enum
import fnmatch
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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, NoReturn, Tuple, TypeVar

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


def _parse_ignore_line(
    raw: str, base: str, *, dir_only_semantics: bool,
) -> GitignoreRule | None:
    """Parse one gitignore-style line into a GitignoreRule, or None to skip.

    Shared by `.gitignore` parsing and CLI --include/--exclude so the glob
    grammar stays in lock-step. `dir_only_semantics=False` (CLI filtering a
    flat list) strips a trailing slash but never marks the rule dir-only,
    since there's no walker to stop from descending."""
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
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
        return None
    try:
        regex = _glob_to_regex(line, anchored)
    except re.error:
        return None
    return GitignoreRule(line, negated, dir_only and dir_only_semantics, base, regex)


def _build_pattern_matcher(patterns: list[str]) -> GitignoreMatcher:
    """Build a GitignoreMatcher from CLI --include / --exclude patterns.

    Trailing slashes are stripped but the rule still applies to files
    under that directory (unlike .gitignore's dir-only semantics, since a
    walker would never descend; here we're filtering a flat list). A
    leading `!` negates."""
    rules = [
        rule for raw in patterns
        if (rule := _parse_ignore_line(raw, "", dir_only_semantics=False))
        is not None
    ]
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
            rule = _parse_ignore_line(raw, rel_base, dir_only_semantics=True)
            if rule is not None:
                rules.append(rule)
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
    value = float(n)
    for unit in ("K", "M", "G", "T"):
        value /= 1000
        if value < 1000 or unit == "T":
            return f"{value:.1f}{unit}" if value < 10 else f"{value:.0f}{unit}"
    raise AssertionError("unreachable")  # the "T" iteration always returns


def _fmt_size(n: int) -> str:
    return _fmt_count(n) + "B"


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
        for _name, rel, abs_path in dirs:
            items.append(ScanItem(rel=rel, is_dir=True))
            walk(abs_path, rel)
        for _name, rel, abs_path in files:
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


_E = TypeVar("_E", bound=enum.IntEnum)


def _cycle(value: _E, members: type[_E], allowed: tuple[_E, ...] | None) -> _E:
    """Advance `value` to the next member, restricted to `allowed` if given.

    If `value` isn't in `allowed`, snap to the first allowed entry."""
    if allowed is None:
        return members((int(value) + 1) % len(members))
    if value not in allowed:
        return allowed[0]
    return allowed[(allowed.index(value) + 1) % len(allowed)]


def cycle_destination(
    d: Destination, allowed: tuple[Destination, ...] | None = None,
) -> Destination:
    """Advance `d` to the next destination.

    `allowed` restricts the cycle to a subset (used inside the TUI to hide
    STDOUT, which is meaningless when curses owns the terminal)."""
    return _cycle(d, Destination, allowed)


class Verb(enum.IntEnum):
    BUNDLE = 0
    COMMIT = 1
    PR = 2
    ITEM = 3


def cycle_verb(v: Verb, allowed: tuple[Verb, ...] | None = None) -> Verb:
    """Advance `v` to the next verb, restricted to `allowed` if given.

    Non-git directories pass `allowed=(Verb.BUNDLE,)` so cycling is a no-op."""
    return _cycle(v, Verb, allowed)


def compute_summary(nodes: list[Node]) -> tuple[int, int, int]:
    """(file_count, total_bytes, approx_tokens) over checked non-dir nodes."""
    count = 0
    total_bytes = 0
    total_tokens = 0
    for n in nodes:
        if n.is_dir or not n.checked:
            continue
        count += 1
        total_bytes += n.size
        total_tokens += n.tokens
    return count, total_bytes, total_tokens


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
    stats = f" | {_fmt_size(total_bytes)} {_fmt_tokens(approx_tokens)}"
    right = (
        f"verb: {verb.name.lower()}  "
        f"dest: {dest.name.lower()}  "
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
    item_kind: str | None = None
    item_id: str | None = None


# Sentinel returned by `_picker_ui` (only when `allow_more=True`) to signal the
# user pressed `m` for the "more" overlay. Keeps the return contract as
# None=cancel/quit, PickResult=emit, PICKER_MORE=excursion — without perturbing
# PickResult's value-compared shape.
PICKER_MORE = object()


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
        with open(path, "rb") as f:
            raw = f.read(min(8192, max_lines * max_cols * 2))
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
    allow_more: bool = False,
    banner: str | None = None,
    initial_verb: Verb = Verb.BUNDLE,
):
    """Run the picker loop on an existing `stdscr`.

    Returns a `PickResult` (Enter), `None` (q / cancel), or the `PICKER_MORE`
    sentinel (only when `allow_more=True` and the user pressed `m`).

    `pick()` calls this through `curses.wrapper`. The interactive picker loop
    (`_run_picker_loop`) calls it directly, since `curses.wrapper` cannot nest.
    `allow_stdout=False` drops STDOUT from the `d` cycle — meaningless when
    curses owns the terminal. `allow_git_verbs=True` enables the `v` verb cycle
    through Commit/PR/Item in addition to the default Bundle; non-git
    directories should leave it False so the cycle collapses to Bundle.
    `allow_more=True` enables the `m` key (returns `PICKER_MORE`). `banner` is a
    transient one-line status (e.g. "bundled → …") drawn above the footer.
    `initial_verb` seeds the `v` cycle so the loop can carry the verb forward
    (snapped back to Bundle when git verbs are unavailable)."""
    if not nodes:
        dest = initial_destination
        if not allow_stdout and dest == Destination.STDOUT:
            dest = Destination.FILE
        return PickResult(set(), dest, initial_include_tree)

    # STDOUT only when a TTY-free pipe is allowed (CLI); SEND only when a
    # default feed exists to broadcast to.
    base = [Destination.FILE, Destination.CLIPBOARD]
    if allow_stdout:
        base.append(Destination.STDOUT)
    if _has_send_target():
        base.append(Destination.SEND)
    cycle_allowed: tuple[Destination, ...] = tuple(base)
    if initial_destination not in cycle_allowed:
        initial_destination = Destination.FILE

    verb_allowed: tuple[Verb, ...] = (
        (Verb.BUNDLE, Verb.COMMIT, Verb.PR, Verb.ITEM)
        if allow_git_verbs else (Verb.BUNDLE,)
    )

    cancelled = False
    destination = initial_destination
    include_tree = initial_include_tree
    # Snap a carried-over verb back to Bundle when git verbs aren't available
    # (e.g. re-entering the picker in a non-git dir).
    verb = initial_verb if initial_verb in verb_allowed else Verb.BUNDLE
    item_kind: str | None = None
    item_id: str | None = None
    preview_visible = root is not None

    curses.curs_set(0)
    stdscr.keypad(True)
    # Force blocking getch. A prior "more" excursion (e.g. ReceiveScreen) may
    # have left a non-blocking timeout set; without this the re-entered picker
    # would spin at 100% CPU. No-op on the one-shot `pick()` path.
    stdscr.timeout(-1)
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
        elif banner:
            # Transient "last action" notice; shares row h-2 with the filter
            # input, so only drawn when the filter is inactive and empty.
            try:
                stdscr.addstr(h - 2, 0, banner[: max(1, w - 1)], theme["filter"])
            except curses.error:
                pass
        more_hint = "  m:more" if allow_more else ""
        if len(verb_allowed) > 1:
            status = (
                "space:toggle  /:filter  v:verb  d:dest  t:tree  p:preview  "
                "s:sort  a:toggle-visible  enter:run" + more_hint + "  q:quit"
            )
        else:
            status = (
                "space:toggle  /:filter  d:dest  t:tree  p:preview  "
                "s:sort  a:toggle-visible  enter:write" + more_hint + "  q:quit"
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
        elif ch == ord("m") and allow_more:
            return PICKER_MORE
        elif ch in (10, 13):
            if verb == Verb.ITEM:
                if root is None:
                    continue
                picked = _prompt_item_id(stdscr, root)
                if picked is None:
                    continue
                item_kind, item_id = picked
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
        item_kind=item_kind,
        item_id=item_id,
    )


def pick(
    nodes: list[Node],
    root: Path | None = None,
    initial_destination: Destination = Destination.FILE,
    initial_include_tree: bool = True,
    allow_git_verbs: bool = False,
) -> PickResult | None:
    """CLI entry: run `_picker_ui` under its own `curses.wrapper`.

    The interactive picker loop (`_run_picker_loop`) calls `_picker_ui`
    directly instead (curses.wrapper can't nest).
    `allow_git_verbs=True` enables the `v` verb cycle through Commit/PR/Item
    in the footer; callers should pass True only when `root` is a git repo.
    """
    if not nodes:
        return PickResult(set(), initial_destination, initial_include_tree)
    # curses.wrapper passes *args/**kwds through and returns the callable's
    # value (and restores the terminal on exception), so no accumulator needed.
    try:
        return curses.wrapper(
            _picker_ui, nodes, root, initial_destination, initial_include_tree,
            allow_stdout=True, allow_git_verbs=allow_git_verbs,
        )
    except KeyboardInterrupt:
        return None


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


def pick_item_output_path(repo_name: str, kind: str, ident: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{_slug(repo_name)}-{kind}-{_slug(ident)}-{ts}"
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


def _write_bundle_files(
    target: Path, files: list[tuple[str, str]],
) -> tuple[int, str | None]:
    """Create `target` and write the parsed bundle files into it.

    Returns (files_written, error). On error, writing stops; partial output
    may remain. Shared by cmd_rebuild and the TUI RebuildScreen so OSError
    handling and the path-escape re-check stay in lock-step.
    """
    target_resolved = target.resolve()
    try:
        target.mkdir(parents=True)
    except OSError as e:
        return 0, f"cannot create {target}: {e}"
    written = 0
    for rel, content in files:
        dest = (target / rel).resolve()
        # Belt-and-braces: even after _validate_rebuild_path, confirm the
        # resolved write target is inside the freshly created folder.
        try:
            dest.relative_to(target_resolved)
        except ValueError:
            return written, f"refusing to write outside target: {rel!r}"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        except OSError as e:
            return written, f"cannot write {dest}: {e}"
        written += 1
    return written, None


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


class NomnomTransportError(NomnomError):
    """A transient relay/network failure fetching a resource — the operation
    might succeed on retry. The receive loops use this to distinguish "couldn't
    fetch this slot right now" (leave the cursor put and retry) from a permanent
    rejection like tamper or a 404 (advance past it). Being a NomnomError, it is
    still caught by the CLI dispatcher's generic handler."""


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
    item_id: str | None = None,
) -> int:
    output = render_git_bundle(repo_name, kind, branch_label, sections, tree)
    size = len(output)

    def _out_path() -> Path:
        if item_id is not None:
            return pick_item_output_path(repo_name, kind, item_id)
        return pick_git_output_path(repo_name, branch_label, kind)

    if destination == Destination.STDOUT:
        sys.stdout.write(output)
        sys.stdout.flush()
        print(f"wrote {size:,} bytes to stdout.", file=sys.stderr)
        return 0

    if destination == Destination.SEND:
        rc, lines = _emit_to_feed(output.encode("utf-8"), _out_path().name)
        for ln in lines:
            print(ln, file=sys.stderr)
        return rc

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
        out_path = _out_path()
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
        out_path = _out_path()

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
    # Rewriting the running file doesn't reload the module, so sync the live
    # global too — otherwise the in-session scan/display keeps the old
    # patterns (e.g. a just-added secret pattern wouldn't take effect until
    # restart). Only when we wrote our own source, never a test/other path.
    if p == SELF_PATH:
        globals()[target_name] = target
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


# ---------- item: pr (formerly review) ----------

_PR_TIMELINE_KEEP = frozenset({
    "review_requested",
    "assigned",
    "unassigned",
    "ready_for_review",
    "convert_to_draft",
    "merged",
    "closed",
    "reopened",
    "head_ref_force_pushed",
})

_PR_REVIEW_THREADS_QUERY = """\
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


def _kv_block(pairs: list[tuple[str, str]]) -> str:
    """Render aligned `key: value` lines.

    The value column is computed from the longest key, so adding a field can't
    silently misalign the block the way the old hand-counted padding could."""
    width = max((len(k) for k, _ in pairs), default=0)
    return "\n".join(f"{k + ':':<{width + 1}} {v}" for k, v in pairs)


def _format_pr_meta(pr: dict) -> str:
    author = ((pr.get("author") or {}).get("login")) or "?"
    labels = ", ".join(
        l.get("name", "") for l in (pr.get("labels") or []) if l
    ) or "(none)"
    milestone = ((pr.get("milestone") or {}).get("title")) or "(none)"
    return _kv_block([
        ("number", f"#{pr.get('number', '?')}"),
        ("title", pr.get("title", "")),
        ("url", pr.get("url", "")),
        ("author", f"@{author}"),
        ("state", pr.get("state", "?")),
        ("draft", "true" if pr.get("isDraft") else "false"),
        ("head", pr.get("headRefName", "")),
        ("base", pr.get("baseRefName", "")),
        ("labels", labels),
        ("milestone", milestone),
        ("created", pr.get("createdAt", "")),
        ("updated", pr.get("updatedAt", "")),
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


def _format_comment_blocks(items: list, head_fn) -> str:
    """Render `## <header>\\n<body-or-(no body)>` blocks joined by blank lines.

    Shared by the review/issue-comment/commit-comment formatters, which differ
    only in how each block's header line is built (passed as `head_fn`)."""
    if not items:
        return ""
    parts: list[str] = []
    for c in items:
        body = (c.get("body") or "").rstrip()
        parts.append(head_fn(c) + "\n" + (body or "(no body)"))
    return "\n\n".join(parts)


def _format_reviews(reviews: list) -> str:
    def head(r: dict) -> str:
        login = ((r.get("user") or {}).get("login")) or "?"
        return f"## @{login} [{r.get('state') or ''}] {r.get('submitted_at') or ''}".rstrip()
    return _format_comment_blocks(reviews, head)


def _format_issue_comments(comments: list) -> str:
    def head(c: dict) -> str:
        login = ((c.get("user") or {}).get("login")) or "?"
        return f"## @{login} {c.get('created_at') or ''}".rstrip()
    return _format_comment_blocks(comments, head)


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


_ISSUE_TIMELINE_KEEP = frozenset({
    "assigned",
    "unassigned",
    "labeled",
    "unlabeled",
    "milestoned",
    "demilestoned",
    "renamed",
    "cross-referenced",
    "referenced",
    "closed",
    "reopened",
    "locked",
    "unlocked",
    "pinned",
    "unpinned",
    "transferred",
    "moved_columns_in_project",
    "added_to_project",
    "removed_from_project",
})


def _format_timeline(events: list, keep: frozenset[str]) -> str:
    if not events:
        return ""
    lines: list[str] = []
    for ev in events:
        kind = ev.get("event")
        if kind not in keep:
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
        elif kind in ("labeled", "unlabeled"):
            label = ((ev.get("label") or {}).get("name")) or "?"
            suffix = f": {label}"
        elif kind in ("milestoned", "demilestoned"):
            title = ((ev.get("milestone") or {}).get("title")) or "?"
            suffix = f": {title}"
        elif kind == "renamed":
            ren = ev.get("rename") or {}
            suffix = f": {ren.get('from', '?')!r} -> {ren.get('to', '?')!r}"
        elif kind == "cross-referenced":
            src = ((ev.get("source") or {}).get("issue") or {})
            num = src.get("number")
            ttl = src.get("title") or ""
            url = src.get("html_url") or ""
            suffix = f": #{num} {ttl}  {url}".rstrip()
        elif kind == "referenced":
            sha = (ev.get("commit_id") or "")[:7]
            url = ev.get("commit_url") or ""
            suffix = f": commit {sha}  {url}".rstrip()
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


def cmd_item_pr(
    repo: str, pr_number: int | None, include_diff: bool,
    destination: Destination = Destination.FILE,
) -> int:
    _require_gh()
    root, repo_name = _resolve_git_repo(repo)

    if pr_number is None:
        rc, view_out, err = _run(
            ["gh", "pr", "view", "--json", "number", "-q", ".number"], root,
        )
        if rc != 0 or not view_out.strip():
            raise NomnomError(
                "no PR found for current branch; pass a PR number explicitly"
            )
        try:
            pr_number = int(view_out.strip())
        except ValueError as e:
            raise NomnomError(
                f"gh pr view returned non-numeric: {view_out.strip()!r}"
            ) from e

    _require_positive("pr number", pr_number)

    owner, name = _resolve_owner_repo(root)

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

    issue_comments = _gh_api_json(root, [
        "gh", "api",
        f"repos/{owner}/{name}/issues/{pr_number}/comments",
        "--paginate",
    ]) or []

    reviews = _gh_api_json(root, [
        "gh", "api",
        f"repos/{owner}/{name}/pulls/{pr_number}/reviews",
        "--paginate",
    ]) or []

    threads_result = _gh_api_json(root, [
        "gh", "api", "graphql",
        "-F", f"owner={owner}",
        "-F", f"repo={name}",
        "-F", f"number={pr_number}",
        "-f", f"query={_PR_REVIEW_THREADS_QUERY}",
    ]) or {}

    timeline = _gh_api_json(root, [
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
        _section("timeline", _format_timeline(timeline, _PR_TIMELINE_KEEP)),
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
        repo_name, "pr", branch_label, sections, tree, destination,
        item_id=str(pr_number),
    )


# ---------- item: shared helpers ----------


def _require_positive(label: str, n: int) -> None:
    """Guard an item id; `label` names the kind (e.g. 'pr number', 'run id')."""
    if n <= 0:
        raise NomnomError(f"{label} must be positive, got {n}")


def _resolve_owner_repo(root: Path) -> tuple[str, str]:
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
    return owner, name


def _gh_api_json(root: Path, args: list[str]) -> object:
    rc, out, err = _run(args, root)
    if rc != 0:
        msg = err.strip() or out.strip() or "(no error message)"
        print(
            f"warn: {' '.join(args[:3])} … exited {rc}: {msg}",
            file=sys.stderr,
        )
        return None
    if not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


# ---------- item: issue ----------


_ISSUE_LINKED_PRS_QUERY = """\
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      closedByPullRequestsReferences(first: 30, includeClosedPrs: true) {
        nodes { number title url state }
      }
    }
  }
}
"""


def _format_issue_meta(issue: dict) -> str:
    user = ((issue.get("user") or {}).get("login")) or "?"
    labels = ", ".join(
        (l.get("name") if isinstance(l, dict) else l) or ""
        for l in (issue.get("labels") or [])
    ) or "(none)"
    assignees = ", ".join(
        f"@{(a.get('login') or '?')}"
        for a in (issue.get("assignees") or [])
    ) or "(none)"
    milestone = ((issue.get("milestone") or {}).get("title")) or "(none)"
    return _kv_block([
        ("number", f"#{issue.get('number', '?')}"),
        ("title", issue.get("title", "")),
        ("url", issue.get("html_url", "")),
        ("author", f"@{user}"),
        ("state", issue.get("state", "?")),
        ("labels", labels),
        ("milestone", milestone),
        ("assignees", assignees),
        ("created", issue.get("created_at", "")),
        ("updated", issue.get("updated_at", "")),
    ])


def cmd_item_issue(
    repo: str, issue_number: int,
    destination: Destination = Destination.FILE,
) -> int:
    _require_gh()
    root, repo_name = _resolve_git_repo(repo)

    _require_positive("issue number", issue_number)

    owner, name = _resolve_owner_repo(root)

    issue = _gh_api_json(root, [
        "gh", "api", f"repos/{owner}/{name}/issues/{issue_number}",
    ])
    if not isinstance(issue, dict) or not issue:
        raise NomnomError(
            f"gh api /issues/{issue_number} returned no data"
        )
    if isinstance(issue.get("pull_request"), dict):
        raise NomnomError(
            f"#{issue_number} is a pull request; "
            f"use `nomnom item pr {issue_number}`"
        )

    comments = _gh_api_json(root, [
        "gh", "api",
        f"repos/{owner}/{name}/issues/{issue_number}/comments",
        "--paginate",
    ]) or []

    timeline = _gh_api_json(root, [
        "gh", "api",
        f"repos/{owner}/{name}/issues/{issue_number}/timeline",
        "--paginate",
    ]) or []

    linked_data = _gh_api_json(root, [
        "gh", "api", "graphql",
        "-F", f"owner={owner}",
        "-F", f"repo={name}",
        "-F", f"number={issue_number}",
        "-f", f"query={_ISSUE_LINKED_PRS_QUERY}",
    ]) or {}
    try:
        linked_prs = (
            linked_data["data"]["repository"]["issue"]
            ["closedByPullRequestsReferences"]["nodes"]
        ) or []
    except (KeyError, TypeError):
        linked_prs = []

    sections: list[tuple[str, str]] = [
        _section("issue_meta", _format_issue_meta(issue)),
        _section("issue_body", (issue.get("body") or "").rstrip()),
        _section("linked_prs", _format_linked_issues(linked_prs)),
        _section("issue_comments", _format_issue_comments(comments)),
        _section("timeline", _format_timeline(timeline, _ISSUE_TIMELINE_KEEP)),
    ]

    branch_label = f"issue-{issue_number}"
    return _emit_git_bundle(
        repo_name, "issue", branch_label, sections, None, destination,
        item_id=str(issue_number),
    )


# ---------- item: discussion ----------


_DISCUSSION_QUERY = """\
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    discussion(number: $number) {
      number
      title
      url
      author { login }
      createdAt
      updatedAt
      category { name }
      labels(first: 20) { nodes { name } }
      body
      answer { id author { login } body createdAt }
      comments(first: 50) {
        nodes {
          id
          author { login }
          body
          createdAt
          replies(first: 20) {
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


def _format_discussion_meta(d: dict) -> str:
    author = ((d.get("author") or {}).get("login")) or "?"
    category = ((d.get("category") or {}).get("name")) or "(none)"
    labels = ", ".join(
        n.get("name") or ""
        for n in ((d.get("labels") or {}).get("nodes") or [])
    ) or "(none)"
    return _kv_block([
        ("number", f"#{d.get('number', '?')}"),
        ("title", d.get("title", "")),
        ("url", d.get("url", "")),
        ("author", f"@{author}"),
        ("category", category),
        ("labels", labels),
        ("created", d.get("createdAt", "")),
        ("updated", d.get("updatedAt", "")),
    ])


def _format_discussion_comments(d: dict) -> str:
    nodes = ((d.get("comments") or {}).get("nodes")) or []
    if not nodes:
        return ""
    answer = d.get("answer") or {}
    answer_id = answer.get("id") if isinstance(answer, dict) else None
    parts: list[str] = []
    for c in nodes:
        login = ((c.get("author") or {}).get("login")) or "?"
        when = c.get("createdAt") or ""
        body = (c.get("body") or "").rstrip()
        tag = "  [answer]" if c.get("id") == answer_id and answer_id else ""
        head = f"## @{login} {when}{tag}".rstrip()
        parts.append(head + "\n" + (body if body else "(no body)"))
        replies = ((c.get("replies") or {}).get("nodes")) or []
        for r in replies:
            r_login = ((r.get("author") or {}).get("login")) or "?"
            r_when = r.get("createdAt") or ""
            r_body = (r.get("body") or "").rstrip()
            indented = "\n".join(
                "    " + line for line in (r_body or "(no body)").split("\n")
            )
            parts.append(f"    > @{r_login} {r_when}\n{indented}")
    return "\n\n".join(parts)


def _format_discussion_answer(d: dict) -> str:
    answer = d.get("answer")
    if not isinstance(answer, dict) or not answer:
        return ""
    login = ((answer.get("author") or {}).get("login")) or "?"
    when = answer.get("createdAt") or ""
    body = (answer.get("body") or "").rstrip()
    return f"@{login} {when}\n{body if body else '(no body)'}"


def cmd_item_discussion(
    repo: str, discussion_number: int,
    destination: Destination = Destination.FILE,
) -> int:
    _require_gh()
    root, repo_name = _resolve_git_repo(repo)

    _require_positive("discussion number", discussion_number)

    owner, name = _resolve_owner_repo(root)

    result = _gh_api_json(root, [
        "gh", "api", "graphql",
        "-F", f"owner={owner}",
        "-F", f"repo={name}",
        "-F", f"number={discussion_number}",
        "-f", f"query={_DISCUSSION_QUERY}",
    ]) or {}
    try:
        d = result["data"]["repository"]["discussion"]
    except (KeyError, TypeError):
        d = None
    if not isinstance(d, dict) or not d:
        raise NomnomError(
            f"discussion #{discussion_number} not found "
            f"(or discussions are disabled for this repo)"
        )

    sections: list[tuple[str, str]] = [
        _section("discussion_meta", _format_discussion_meta(d)),
        _section("discussion_body", (d.get("body") or "").rstrip()),
        _section("answer", _format_discussion_answer(d)),
        _section("comments", _format_discussion_comments(d)),
    ]

    branch_label = f"discussion-{discussion_number}"
    return _emit_git_bundle(
        repo_name, "discussion", branch_label, sections, None, destination,
        item_id=str(discussion_number),
    )


# ---------- item: commit ----------


_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _format_commit_meta(commit: dict) -> str:
    sha = commit.get("sha", "?")
    info = commit.get("commit") or {}
    author = info.get("author") or {}
    committer = info.get("committer") or {}
    parents = ", ".join(
        (p.get("sha", "") or "")[:7] for p in (commit.get("parents") or [])
    ) or "(none)"
    stats = commit.get("stats") or {}
    return _kv_block([
        ("sha", sha),
        ("short", sha[:7] if sha != "?" else "?"),
        ("url", commit.get("html_url", "")),
        ("author", f"{author.get('name', '?')} <{author.get('email', '')}>"),
        ("authored", author.get("date", "")),
        ("committer", f"{committer.get('name', '?')} <{committer.get('email', '')}>"),
        ("committed", committer.get("date", "")),
        ("parents", parents),
        ("stats", f"+{stats.get('additions', 0)} -{stats.get('deletions', 0)} "
                  f"({stats.get('total', 0)})"),
    ])


def _format_commit_comments(comments: list) -> str:
    def head(c: dict) -> str:
        login = ((c.get("user") or {}).get("login")) or "?"
        when = c.get("created_at") or ""
        path = c.get("path") or ""
        line = c.get("line") or c.get("position")
        if path:
            return (
                f"## {path}:{line if line is not None else '?'}  "
                f"@{login} {when}".rstrip()
            )
        return f"## @{login} {when}".rstrip()
    return _format_comment_blocks(comments, head)


def _commit_diff(root: Path, owner: str, name: str, sha: str) -> str:
    rc, out, _ = _run(["git", "show", "--format=", sha], root)
    if rc == 0:
        return out
    rc, out, _ = _run([
        "gh", "api", f"repos/{owner}/{name}/commits/{sha}",
        "-H", "Accept: application/vnd.github.diff",
    ], root)
    return out if rc == 0 else ""


def cmd_item_commit(
    repo: str, sha: str, include_diff: bool,
    destination: Destination = Destination.FILE,
) -> int:
    _require_gh()
    root, repo_name = _resolve_git_repo(repo)

    sha = sha.strip()
    if not _COMMIT_SHA_RE.match(sha):
        raise NomnomError(
            f"commit sha must be 7-40 hex chars, got {sha!r}"
        )

    owner, name = _resolve_owner_repo(root)

    commit = _gh_api_json(root, [
        "gh", "api", f"repos/{owner}/{name}/commits/{sha}",
    ])
    if not isinstance(commit, dict) or not commit:
        raise NomnomError(
            f"gh api /commits/{sha} returned no data"
        )

    comments = _gh_api_json(root, [
        "gh", "api", f"repos/{owner}/{name}/commits/{sha}/comments",
        "--paginate",
    ]) or []

    files_for_summary = [
        {
            "path": f.get("filename", "") or "",
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
        }
        for f in (commit.get("files") or [])
    ]

    diff_full = ""
    if include_diff:
        diff_full = _commit_diff(root, owner, name, sha)

    message = ((commit.get("commit") or {}).get("message") or "").rstrip()

    sections: list[tuple[str, str]] = [
        _section("commit_meta", _format_commit_meta(commit)),
        _section("commit_message", message),
        _section("diff_summary", _format_diff_summary(files_for_summary)),
    ]
    if include_diff:
        sections.append(_section("diff", diff_full.rstrip()))
    sections.append(_section("commit_comments", _format_commit_comments(comments)))

    changed = sorted({
        (f.get("filename") or "")
        for f in (commit.get("files") or [])
        if f.get("filename")
    })
    tree = render_ascii_tree(changed, repo_name) if changed else None

    sha7 = (commit.get("sha") or sha)[:7]
    branch_label = f"commit-{sha7}"
    return _emit_git_bundle(
        repo_name, "commit", branch_label, sections, tree, destination,
        item_id=sha7,
    )


# ---------- item: release ----------


def _format_release_meta(rel: dict) -> str:
    author = ((rel.get("author") or {}).get("login")) or "?"
    return _kv_block([
        ("tag", rel.get("tag_name", "?")),
        ("name", rel.get("name", "") or "(none)"),
        ("url", rel.get("html_url", "")),
        ("author", f"@{author}"),
        ("target", rel.get("target_commitish", "")),
        ("published", rel.get("published_at", "")),
        ("created", rel.get("created_at", "")),
        ("draft", "true" if rel.get("draft") else "false"),
        ("prerelease", "true" if rel.get("prerelease") else "false"),
    ])


def _format_release_assets(assets: list) -> str:
    if not assets:
        return ""
    lines: list[str] = []
    for a in assets:
        name = a.get("name", "") or "?"
        size = a.get("size", 0) or 0
        url = a.get("browser_download_url", "") or ""
        downloads = a.get("download_count", 0) or 0
        lines.append(
            f"{name}  ({_fmt_size(size)})  downloads:{downloads}  {url}".rstrip()
        )
    return "\n".join(lines)


def cmd_item_release(
    repo: str, tag_or_id: str,
    destination: Destination = Destination.FILE,
) -> int:
    _require_gh()
    root, repo_name = _resolve_git_repo(repo)

    tag_or_id = tag_or_id.strip()
    if not tag_or_id:
        raise NomnomError("release tag must not be empty")

    owner, name = _resolve_owner_repo(root)

    encoded_tag = urllib.parse.quote(tag_or_id, safe="")
    release = _gh_api_json(root, [
        "gh", "api", f"repos/{owner}/{name}/releases/tags/{encoded_tag}",
    ])
    if not isinstance(release, dict) and tag_or_id.isdigit():
        release = _gh_api_json(root, [
            "gh", "api", f"repos/{owner}/{name}/releases/{tag_or_id}",
        ])
    if not isinstance(release, dict) or not release:
        raise NomnomError(f"release {tag_or_id!r} not found")

    sections: list[tuple[str, str]] = [
        _section("release_meta", _format_release_meta(release)),
        _section("release_notes", (release.get("body") or "").rstrip()),
        _section("assets", _format_release_assets(release.get("assets") or [])),
    ]

    tag = release.get("tag_name") or tag_or_id
    branch_label = f"release-{tag}"
    return _emit_git_bundle(
        repo_name, "release", branch_label, sections, None, destination,
        item_id=tag,
    )


# ---------- item: workflow run / job ----------


def _format_run_meta(run: dict) -> str:
    sha = (run.get("headSha") or "")[:7]
    return _kv_block([
        ("id", f"{run.get('databaseId') or run.get('id', '?')}"),
        ("number", f"{run.get('number', '?')}"),
        ("workflow", run.get("workflowName", "")),
        ("title", run.get("displayTitle", "")),
        ("url", run.get("url", "")),
        ("branch", run.get("headBranch", "")),
        ("sha", sha),
        ("event", run.get("event", "")),
        ("status", run.get("status", "")),
        ("conclusion", run.get("conclusion", "")),
        ("attempt", f"{run.get('attempt', '?')}"),
        ("created", run.get("createdAt", "")),
        ("updated", run.get("updatedAt", "")),
    ])


def _format_run_jobs(jobs: list) -> str:
    if not jobs:
        return ""
    width = max((len(j.get("name", "") or "?") for j in jobs), default=0)
    lines: list[str] = []
    for j in jobs:
        name = j.get("name", "") or "?"
        status = j.get("status", "") or ""
        conclusion = j.get("conclusion", "") or ""
        url = j.get("url", "") or ""
        started = j.get("startedAt", "")
        completed = j.get("completedAt", "")
        label = conclusion or status or "?"
        lines.append(
            f"{label:<12} {name:<{width}}  {started} -> {completed}  {url}".rstrip()
        )
    return "\n".join(lines)


def _format_job_meta(job: dict) -> str:
    return _kv_block([
        ("id", f"{job.get('id', '?')}"),
        ("name", job.get("name", "")),
        ("run_id", f"{job.get('run_id', '?')}"),
        ("run_url", job.get("run_url", "")),
        ("url", job.get("html_url", "")),
        ("workflow", job.get("workflow_name", "")),
        ("status", job.get("status", "")),
        ("conclusion", job.get("conclusion", "")),
        ("started", job.get("started_at", "")),
        ("completed", job.get("completed_at", "")),
        ("runner", job.get("runner_name", "")),
    ])


def _format_job_steps(steps: list) -> str:
    if not steps:
        return ""
    width = max((len(s.get("name", "") or "?") for s in steps), default=0)
    lines: list[str] = []
    for s in steps:
        n = s.get("name", "") or "?"
        st = s.get("status", "") or ""
        co = s.get("conclusion", "") or ""
        label = co or st or "?"
        lines.append(f"{label:<12} {n:<{width}}")
    return "\n".join(lines)


def _format_log_slice(log_text: str, max_lines_per_step: int = 200) -> str:
    if not log_text.strip():
        return ""
    groups: list[tuple[str, list[str]]] = []
    current_key: str | None = None
    for line in log_text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 2 and parts[0]:
            key = f"{parts[0]} / {parts[1]}"
        else:
            key = current_key or "(unknown)"
        if key != current_key:
            groups.append((key, []))
            current_key = key
        groups[-1][1].append(line)

    out: list[str] = []
    for key, lines in groups:
        if len(lines) > max_lines_per_step:
            kept = lines[-max_lines_per_step:]
            out.append(
                f"--- {key} (last {max_lines_per_step} of "
                f"{len(lines)} lines) ---"
            )
            out.extend(kept)
        else:
            out.append(f"--- {key} ({len(lines)} lines) ---")
            out.extend(lines)
    return "\n".join(out)


def cmd_item_run(
    repo: str, run_id: int, all_logs: bool = False,
    destination: Destination = Destination.FILE,
) -> int:
    _require_gh()
    root, repo_name = _resolve_git_repo(repo)
    _require_positive("run id", run_id)

    fields = (
        "databaseId,number,attempt,status,conclusion,event,workflowName,"
        "displayTitle,headBranch,headSha,jobs,createdAt,updatedAt,url"
    )
    rc, run_view, err = _run(
        ["gh", "run", "view", str(run_id), "--json", fields], root,
    )
    if rc != 0:
        raise NomnomError(
            f"gh run view {run_id} failed: "
            f"{err.strip() or 'unknown error'}"
        )
    try:
        run = json.loads(run_view) if run_view.strip() else {}
    except json.JSONDecodeError as e:
        raise NomnomError(f"gh run view returned invalid json: {e}") from e

    log_flag = "--log" if all_logs else "--log-failed"
    _, log_text, _ = _run(
        ["gh", "run", "view", str(run_id), log_flag], root,
    )
    log_section = log_text if all_logs else _format_log_slice(log_text)

    sections: list[tuple[str, str]] = [
        _section("run_meta", _format_run_meta(run)),
        _section("jobs", _format_run_jobs(run.get("jobs") or [])),
        _section("logs" if all_logs else "failed_logs", log_section),
    ]

    branch_label = f"run-{run_id}"
    return _emit_git_bundle(
        repo_name, "run", branch_label, sections, None, destination,
        item_id=str(run_id),
    )


def cmd_item_job(
    repo: str, job_id: int, all_logs: bool = False,
    destination: Destination = Destination.FILE,
) -> int:
    _require_gh()
    root, repo_name = _resolve_git_repo(repo)
    _require_positive("job id", job_id)

    owner, name = _resolve_owner_repo(root)

    job = _gh_api_json(root, [
        "gh", "api", f"repos/{owner}/{name}/actions/jobs/{job_id}",
    ])
    if not isinstance(job, dict) or not job:
        raise NomnomError(
            f"gh api /actions/jobs/{job_id} returned no data"
        )

    log_text = ""
    run_id = job.get("run_id")
    if run_id:
        log_flag = "--log" if all_logs else "--log-failed"
        _, log_text, _ = _run(
            ["gh", "run", "view", str(run_id),
             "--job", str(job_id), log_flag],
            root,
        )
    log_section = log_text if all_logs else _format_log_slice(log_text)

    sections: list[tuple[str, str]] = [
        _section("job_meta", _format_job_meta(job)),
        _section("steps", _format_job_steps(job.get("steps") or [])),
        _section("logs" if all_logs else "failed_logs", log_section),
    ]

    branch_label = f"job-{job_id}"
    return _emit_git_bundle(
        repo_name, "job", branch_label, sections, None, destination,
        item_id=str(job_id),
    )


# ---------- item: dispatcher + auto-detect ----------


ITEM_KINDS = ("pr", "issue", "discussion", "commit", "release", "run", "job")


def _classify_item_id(value: str) -> str | None:
    """Infer kind from `value`'s shape. Returns None for ambiguous numeric ids."""
    value = value.strip()
    if not value:
        return None
    if _COMMIT_SHA_RE.match(value):
        return "commit"  # hex-shaped: assume commit; a rare hex tag needs `item release`
    if not value.isdigit():
        return "release"
    return None


def _probe_numeric_id(
    root: Path, owner: str, name: str, n: int,
) -> list[tuple[str, str]]:
    """Probe /issues/{n} and /actions/runs/{n} in parallel. Returns [(kind, label)].

    /issues/{n} serves both issues and PRs (PRs have a `pull_request` key),
    so we don't need a separate /pulls/{n} fetch.
    """
    results: dict[str, object] = {"issue": None, "run": None}

    def _fetch(key: str, path: str) -> None:
        rc, out, _ = _run(
            ["gh", "api", f"repos/{owner}/{name}/{path}"], root,
        )
        if rc != 0 or not out.strip():
            return
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict):
            results[key] = data

    threads = [
        threading.Thread(target=_fetch, args=("issue", f"issues/{n}")),
        threading.Thread(target=_fetch, args=("run", f"actions/runs/{n}")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    matches: list[tuple[str, str]] = []
    issue = results["issue"]
    if isinstance(issue, dict):
        title = issue.get("title") or ""
        if isinstance(issue.get("pull_request"), dict):
            matches.append(("pr", title))
        else:
            matches.append(("issue", title))
    run = results["run"]
    if isinstance(run, dict):
        label = (
            run.get("name") or run.get("display_title")
            or run.get("workflow_name") or ""
        )
        matches.append(("run", label))
    return matches


def _parse_item_int(kind: str, ident: str) -> int:
    try:
        return int(ident)
    except (TypeError, ValueError) as e:
        raise NomnomError(f"invalid {kind} id: {ident!r}") from e


def _dispatch_item_kind(
    repo: str, kind: str, ident: str,
    include_diff: bool, all_logs: bool, destination: Destination,
) -> int:
    if kind == "pr":
        return cmd_item_pr(
            repo, _parse_item_int("pr", ident), include_diff, destination,
        )
    if kind == "issue":
        return cmd_item_issue(
            repo, _parse_item_int("issue", ident), destination,
        )
    if kind == "discussion":
        return cmd_item_discussion(
            repo, _parse_item_int("discussion", ident), destination,
        )
    if kind == "commit":
        return cmd_item_commit(repo, ident, include_diff, destination)
    if kind == "release":
        return cmd_item_release(repo, ident, destination)
    if kind == "run":
        return cmd_item_run(
            repo, _parse_item_int("run", ident), all_logs, destination,
        )
    if kind == "job":
        return cmd_item_job(
            repo, _parse_item_int("job", ident), all_logs, destination,
        )
    raise NomnomError(f"unknown item kind: {kind!r}")


def cmd_item(
    repo: str,
    kind_or_id: str,
    ident: str | None = None,
    include_diff: bool = False,
    all_logs: bool = False,
    destination: Destination = Destination.FILE,
) -> int:
    _require_gh()

    if kind_or_id in ITEM_KINDS:
        kind = kind_or_id
        if ident is None:
            if kind == "pr":
                return cmd_item_pr(repo, None, include_diff, destination)
            raise NomnomError(f"`item {kind}` requires an id")
        return _dispatch_item_kind(
            repo, kind, ident, include_diff, all_logs, destination,
        )

    if ident is not None:
        raise NomnomError(
            f"unknown kind {kind_or_id!r}; "
            f"valid: {', '.join(ITEM_KINDS)}"
        )

    value = kind_or_id.strip()
    if not value:
        raise NomnomError("item id required")

    inferred = _classify_item_id(value)
    if inferred == "commit":
        return cmd_item_commit(repo, value, include_diff, destination)
    if inferred == "release":
        return cmd_item_release(repo, value, destination)

    if not value.isdigit():
        raise NomnomError(f"could not infer kind from {value!r}")

    n = int(value)
    root, _ = _resolve_git_repo(repo)
    owner, name = _resolve_owner_repo(root)
    matches = _probe_numeric_id(root, owner, name, n)
    if not matches:
        raise NomnomError(
            f"no PR, issue, or workflow run found for #{n}"
        )
    if len(matches) > 1:
        opts = " / ".join(
            f"{k} (#{n}: {label})" for k, label in matches
        )
        raise NomnomError(
            f"ambiguous: #{n} matches multiple kinds: {opts}. "
            f"Disambiguate with `nomnom item <kind> {n}`"
        )
    kind = matches[0][0]
    return _dispatch_item_kind(
        repo, kind, value, include_diff, all_logs, destination,
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
    written, err = _write_bundle_files(target, files)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 1

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
    block_size = 32  # HMAC-SHA256 output
    if len(data) > block_size * (2 ** 32):
        # The block counter is a 32-bit BE value; past 2**32 blocks it would
        # wrap and reuse keystream. Unreachable for real payloads, but guard it.
        raise ValueError("stream_xor: input too large for 32-bit block counter")
    out = bytearray(len(data))
    counter = 0
    for off in range(0, len(data), block_size):
        ks = hmac.new(
            enc_key,
            nonce + counter.to_bytes(4, "big"),
            hashlib.sha256,
        ).digest()
        chunk = data[off:off + block_size]
        # XOR the whole block at once at C speed rather than byte-by-byte; the
        # fixed width preserves leading zero bytes.
        xored = int.from_bytes(chunk, "big") ^ int.from_bytes(ks[:len(chunk)], "big")
        out[off:off + len(chunk)] = xored.to_bytes(len(chunk), "big")
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


# ----- HKDF (RFC 5869) -----

def _hkdf(*, salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256: Extract-then-Expand. Returns `length` bytes (<=255*32)."""
    if length > 255 * 32:
        raise ValueError("hkdf: length too large")
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out = b""
    t = b""
    counter = 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        out += t
        counter += 1
    return out[:length]


# ----- Ed25519 (RFC 8032 reference, pure Python) -----
#
# A minimal Ed25519 implementation for sender authentication on feed posts.
# Sign / verify take ~50ms each on CPython. The interface (sign / verify
# / pub-from-seed) is a strict subset of PyNaCl's signing module so the
# logic is easy to spot-check against the spec.
#
# UNAUDITED, and deliberately so: nomnom is a single-file, stdlib-only CLI, so we
# don't pull in libsodium/PyNaCl. This is the *reference* that generates the
# interop test vectors the browser client (which uses the audited @noble/curves)
# is checked against — so the cross-impl agreement tests, including the
# adversarial verify vectors (tests/test_nomnom.py::TestEd25519AdversarialVectors
# and nomnom-web's feeds.vectors.test.ts), are the safety net that this and the
# audited library can't diverge unnoticed. `ed25519_verify` uses the strict
# cofactorless equation [S]B == R + [k]A with canonical-S enforcement (s < L);
# that is a deliberate choice and must keep matching @noble/curves.

_ED_P = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P
_ED_I = pow(2, (_ED_P - 1) // 4, _ED_P)

# Extended Edwards point: (X, Y, Z, T). Affine = (X/Z, Y/Z); T = XY/Z.
# Module-level type alias (runtime expression): typing.Tuple keeps this working
# on Python 3.8, where `tuple[...]` is not yet subscriptable at runtime.
_EdPoint = Tuple[int, int, int, int]


def _ed_h(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _ed_recover_x(y: int, sign: int) -> int | None:
    if y >= _ED_P:
        return None
    x2 = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_P - 2, _ED_P) % _ED_P
    if x2 == 0:
        return 0 if sign == 0 else None
    x = pow(x2, (_ED_P + 3) // 8, _ED_P)
    if (x * x - x2) % _ED_P != 0:
        x = x * _ED_I % _ED_P
    if (x * x - x2) % _ED_P != 0:
        return None
    if (x & 1) != sign:
        x = _ED_P - x
    return x


# Generator B = (Bx, By) on curve -x^2 + y^2 = 1 + d x^2 y^2 (Edwards).
_ED_BY = 4 * pow(5, _ED_P - 2, _ED_P) % _ED_P
_ED_BX = _ed_recover_x(_ED_BY, 0)
if _ED_BX is None:
    raise RuntimeError("ed25519: failed to recover base point x")
_ED_B = (_ED_BX % _ED_P, _ED_BY % _ED_P, 1, _ED_BX * _ED_BY % _ED_P)


def _ed_point_add(P: _EdPoint, Q: _EdPoint) -> _EdPoint:
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % _ED_P
    b = (y1 + x1) * (y2 + x2) % _ED_P
    c = 2 * t1 * t2 * _ED_D % _ED_P
    d = 2 * z1 * z2 % _ED_P
    e = b - a
    f = d - c
    g = d + c
    h = b + a
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed_scalar_mult(P: _EdPoint, n: int) -> _EdPoint:
    Q: _EdPoint = (0, 1, 1, 0)  # identity
    while n > 0:
        if n & 1:
            Q = _ed_point_add(Q, P)
        P = _ed_point_add(P, P)
        n >>= 1
    return Q


def _ed_point_encode(P: _EdPoint) -> bytes:
    x, y, z, _ = P
    zi = pow(z, _ED_P - 2, _ED_P)
    x = x * zi % _ED_P
    y = y * zi % _ED_P
    return ((y & ((1 << 255) - 1)) | ((x & 1) << 255)).to_bytes(32, "little")


def _ed_point_decode(s: bytes) -> _EdPoint | None:
    if len(s) != 32:
        return None
    raw = int.from_bytes(s, "little")
    sign = (raw >> 255) & 1
    y = raw & ((1 << 255) - 1)
    x = _ed_recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _ED_P)


def _ed_clamp(seed_hash: bytes) -> int:
    a = bytearray(seed_hash[:32])
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return int.from_bytes(a, "little")


def ed25519_pub_from_seed(seed: bytes) -> bytes:
    """Derive the 32-byte public key from a 32-byte Ed25519 seed."""
    if len(seed) != 32:
        raise ValueError("ed25519: seed must be 32 bytes")
    h = _ed_h(seed)
    a = _ed_clamp(h)
    return _ed_point_encode(_ed_scalar_mult(_ED_B, a))


def ed25519_keypair() -> tuple[bytes, bytes]:
    """Return (seed, pub) where seed is 32 random bytes, pub is 32 bytes."""
    seed = secrets.token_bytes(32)
    return seed, ed25519_pub_from_seed(seed)


def ed25519_sign(msg: bytes, seed: bytes) -> bytes:
    """Sign `msg` under the 32-byte Ed25519 seed. Returns 64 bytes."""
    if len(seed) != 32:
        raise ValueError("ed25519: seed must be 32 bytes")
    h = _ed_h(seed)
    a = _ed_clamp(h)
    prefix = h[32:64]
    pub = _ed_point_encode(_ed_scalar_mult(_ED_B, a))
    r = int.from_bytes(_ed_h(prefix + msg), "little") % _ED_L
    R = _ed_scalar_mult(_ED_B, r)
    R_enc = _ed_point_encode(R)
    k = int.from_bytes(_ed_h(R_enc + pub + msg), "little") % _ED_L
    s = (r + k * a) % _ED_L
    return R_enc + s.to_bytes(32, "little")


def ed25519_verify(msg: bytes, sig: bytes, pub: bytes) -> bool:
    """Verify a 64-byte signature on `msg` under a 32-byte pubkey."""
    if len(sig) != 64 or len(pub) != 32:
        return False
    R = _ed_point_decode(sig[:32])
    A = _ed_point_decode(pub)
    if R is None or A is None:
        return False
    s = int.from_bytes(sig[32:64], "little")
    if s >= _ED_L:
        return False
    k = int.from_bytes(_ed_h(sig[:32] + pub + msg), "little") % _ED_L
    sB = _ed_scalar_mult(_ED_B, s)
    kA = _ed_scalar_mult(A, k)
    rhs = _ed_point_add(R, kA)
    return _ed_point_encode(sB) == _ed_point_encode(rhs)


# ----- identity + known-peer (TOFU) store -----

def _nomnom_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    d = root / "nomnom"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_identity() -> dict:
    """Return this machine's identity, creating/upgrading it on first use.

    Identity holds:
      - device_id, name
      - sig_priv / sig_pub: Ed25519 seed + public key (hex), used to sign
        outgoing feed posts so receivers can verify a post under the URL-token
        crypto came from this machine and not from another URL holder.
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
    if not ident.get("sig_priv") or not ident.get("sig_pub"):
        seed, pub = ed25519_keypair()
        ident["sig_priv"] = seed.hex()
        ident["sig_pub"] = pub.hex()
        changed = True
    if changed:
        try:
            _atomic_write_text(path, json.dumps(ident))
        except OSError as e:
            # A fresh signing key that never reaches disk means peers will see
            # a new fingerprint next run and TOFU-reprompt — warn rather than
            # fail silently. (TUI redirects stderr, so this only shows in CLI.)
            sys.stderr.write(f"warning: could not persist identity: {e}\n")
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
            # Flush to disk before the rename so a crash can't leave a
            # truncated identity/peer-pin store (the whole point of the helper
            # is durability, not just atomicity of the name swap).
            f.flush()
            os.fsync(f.fileno())
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

    Pre-feed-v2 the pin store was a flat {device_id: record} dict; the v2
    wrapper adds a `version` envelope. Records are preserved verbatim so a
    feeds-v2 client still recognizes identities pinned by older releases.
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


def _peer_matches(pid: str, rec: object, needle: str) -> bool:
    """True if non-empty `needle` matches a peer's device id, name, or nickname."""
    if not needle:
        return False
    if needle == pid:
        return True
    if not isinstance(rec, dict):
        return False
    return needle in (rec.get("name", ""), rec.get("nickname", ""))


def _forget_peer(needle: str) -> list:
    """Drop pins matching `needle` (a device id, name, or nickname). Returns dropped names."""
    peers = _load_known_peers()
    dropped = []
    for dev_id in list(peers.keys()):
        rec = peers[dev_id]
        if _peer_matches(dev_id, rec, needle):
            name = rec.get("name", "") if isinstance(rec, dict) else ""
            nickname = rec.get("nickname", "") if isinstance(rec, dict) else ""
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
        if _peer_matches(pid, rec, needle):
            matches.append((pid, rec))
    return matches


def _set_peer_nickname(needle: str, nickname: str | None) -> tuple[str, str] | None:
    """Set or clear nickname. Returns (device_id, new_nickname) or None if no match."""
    peers = _load_known_peers()
    target = None
    for pid, rec in peers.items():
        if not isinstance(rec, dict):
            continue
        if _peer_matches(pid, rec, needle):
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


# ---------- TOFU helpers ----------


def _ik_fingerprint(ik_hex: str) -> str:
    """Short, readable fingerprint of an identity public key for display."""
    try:
        # `format(pub, "x")` drops a leading zero nibble, so the hex can be
        # odd-length; pad before decoding (bytes.fromhex rejects odd lengths).
        raw = bytes.fromhex(ik_hex if len(ik_hex) % 2 == 0 else "0" + ik_hex)
    except (ValueError, TypeError):
        return "?"  # display-only sentinel; never compared against a real fingerprint
    d = hashlib.sha256(raw).hexdigest()[:16]
    return ":".join(d[i:i + 4] for i in range(0, 16, 4))


def _tofu_assert_main_thread() -> None:
    """Refuse to call input() outside the main thread.

    A worker thread reaching `input()` would corrupt curses-owned stdin or
    block the receive long-poll; better to fail loudly than silently hang.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "TOFU prompt invoked from non-main thread; pass on_tofu callback",
        )


# ----- feed TOFU (global identity pinning, keyed by Ed25519 sig_pub) -----
#
# Feed posts are signed by the sender's Ed25519 identity key. We pin those
# identities globally (across all feeds) so the same person stays trusted no
# matter how many feeds they join. The synthetic peer id is `feed-<sig_pub16>`
# so v2-pinned identities don't collide with any legacy DH pins still on disk.


def _feed_peer_id(sig_pub_hex: str) -> str:
    return f"feed-{sig_pub_hex[:16]}"


def _find_pinned_sig(sig_pub_hex: str) -> tuple[str, dict] | None:
    """Return (peer_id, record) for an existing global pin on this sig_pub."""
    if not sig_pub_hex:
        return None
    for pid, rec in _load_known_peers().items():
        if isinstance(rec, dict) and rec.get("sig_pub") == sig_pub_hex:
            return pid, rec
    return None


def _save_feed_pin(sig_pub_hex: str, name: str) -> None:
    """Pin (or refresh) a feed member's Ed25519 identity globally."""
    peers = _load_known_peers()
    existing = _find_pinned_sig(sig_pub_hex)
    if existing is not None:
        pid, rec = existing
        rec["name"] = name
        peers[pid] = rec
    else:
        pid = _feed_peer_id(sig_pub_hex)
        peers[pid] = {
            "name": name,
            "sig_pub": sig_pub_hex,
            "first_seen": int(time.time()),
        }
    _save_known_peers(peers)


def _tofu_check_feed_member(
    card: dict, *, trust_new: bool = False,
) -> bool:
    """Run TOFU on a feed member card. Returns True if trusted, False if declined.

    The card looks like {member_id, identity_pubkey, name}. We pin globally by
    identity_pubkey (sig_pub_hex). On first-ever sighting we either auto-pin
    (trust_new) or prompt; subsequent feeds where the same identity appears
    are silent. A mismatch (same person's sig_pub seen with a DIFFERENT
    fingerprint we already pinned) can't happen since the pin IS keyed by
    sig_pub — a different sig_pub is a different identity from our POV.
    """
    sig_pub = card.get("identity_pubkey")
    name = card.get("name") or "(no name)"
    if not isinstance(sig_pub, str) or not sig_pub:
        return False
    if _find_pinned_sig(sig_pub) is not None:
        return True
    if trust_new:
        _save_feed_pin(sig_pub, name)
        if not _in_tui():
            sys.stderr.write(
                f"audit: TOFU pinned {name!r} (fingerprint "
                f"{_ik_fingerprint(sig_pub)})\n",
            )
        return True
    # Inside the curses TUI, stdin/stderr are owned by curses (and may be
    # captured), so input() can't prompt. A handler installed via
    # `_tui_tofu` renders a modal instead and returns the user's choice.
    handler = _TUI_TOFU_HANDLER
    if handler is not None:
        if handler(card):
            _save_feed_pin(sig_pub, name)
            return True
        return False
    _tofu_assert_main_thread()
    sys.stderr.write("\n")
    sys.stderr.write(f"  first contact with {name!r}.\n")
    sys.stderr.write(f"    fingerprint: {_ik_fingerprint(sig_pub)}\n")
    sys.stderr.write(
        "  verify out-of-band if it matters.\n",
    )
    sys.stderr.write("  trust and pin this device? [y/N]: ")
    sys.stderr.flush()
    try:
        ans = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if ans in ("y", "yes"):
        _save_feed_pin(sig_pub, name)
        return True
    return False


# ---------- feed crypto ----------
#
# Feeds v2 transport.
#
# The user-facing URL is host/f/<token>. The token is the credential, and
# every member of the feed derives the same symmetric AEAD key from it. The
# Worker authenticates per-request signatures the same way (HMAC keyed by the
# feed_key), so anyone holding the URL can talk to the Worker for that feed
# but nothing else on the relay.
#
# Sender authenticity comes from a separate Ed25519 signature over each post's
# transcript: posts unsigned (or signed by a key that doesn't match the
# claimed member's pinned identity) are dropped.

_FEED_MAGIC = b"NMNF\x01"  # 4-byte tag + 1-byte format version
_FEED_NONCE_LEN = 12
_FEED_MAC_LEN = 32
_FEED_HEADER_LEN = len(_FEED_MAGIC) + _FEED_NONCE_LEN + _FEED_MAC_LEN
_FEED_KEY_SALT = b"nomnom-feed-v1"
_FEED_ENC_INFO = b"nomnom-feed-enc"
_FEED_MAC_INFO = b"nomnom-feed-mac"
_FEED_SIG_DOMAIN = b"nomnom-feed-sig-v1"


def _urlsafe_b64decode_loose(s: str) -> bytes:
    """Decode URL-safe base64, tolerating missing padding."""
    s = s.strip()
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


_FEED_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,32}$")


def _feed_key_from_token(token: str) -> bytes:
    """Derive the 32-byte feed key from the URL token.

    Mirrors the Worker's HKDF-SHA256 derivation: salt=`nomnom-feed-v1`,
    ikm=raw bytes of the URL token, info=the token's string form. Both ends
    must agree byte-for-byte; the Worker derives the same key on every
    /feeds/:id/* request to verify signatures.
    """
    if not token:
        raise ValueError("feed token must not be empty")
    if not _FEED_TOKEN_RE.match(token):
        raise ValueError(
            "feed token must be 8-32 url-safe base64 chars",
        )
    try:
        ikm = _urlsafe_b64decode_loose(token)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"feed token is not valid url-safe base64: {e}") from e
    return _hkdf(salt=_FEED_KEY_SALT, ikm=ikm, info=token.encode("ascii"), length=32)


def _feed_subkeys(feed_key: bytes) -> tuple[bytes, bytes]:
    """Derive (enc_key, mac_key) from the feed key via HKDF-Expand."""
    enc = _hkdf(salt=b"", ikm=feed_key, info=_FEED_ENC_INFO, length=32)
    mac = _hkdf(salt=b"", ikm=feed_key, info=_FEED_MAC_INFO, length=32)
    return enc, mac


def _feed_request_mac(feed_key: bytes, method: str, path: str, ts: int) -> str:
    """Compute the per-request signature for /feeds/:id/* endpoints."""
    msg = f"{method}\n{path}\n{ts}".encode("ascii")
    return hmac.new(feed_key, msg, hashlib.sha256).hexdigest()


def _feed_sig_transcript(
    *,
    feed_id: str,
    sender_member_id: str,
    sender_sig_pub_hex: str,
    filename: str,
    file_size: int,
    content_hash: bytes,
    posted_at: int,
    nonce: bytes,
) -> bytes:
    """Hash the bound-together fields a sender signs over.

    Includes the AEAD nonce so a captured signature can't be replayed against
    a different ciphertext for the same logical message.
    """
    parts = [
        _FEED_SIG_DOMAIN,
        feed_id.encode("ascii"),
        sender_member_id.encode("ascii"),
        bytes.fromhex(sender_sig_pub_hex),
        filename.encode("utf-8"),
        file_size.to_bytes(8, "big"),
        content_hash,
        posted_at.to_bytes(8, "big"),
        nonce,
    ]
    h = hashlib.sha256()
    for p in parts:
        h.update(len(p).to_bytes(2, "big"))
        h.update(p)
    return h.digest()


def _feed_pack_header(header: dict) -> bytes:
    """Pack a JSON header with a 2-byte length prefix (max 64 KB)."""
    raw = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > 0xFFFF:
        raise ValueError("feed header too large")
    return len(raw).to_bytes(2, "big") + raw


def _feed_unpack_header(plaintext: bytes) -> tuple[dict, bytes]:
    """Inverse of _feed_pack_header. Returns (header_dict, body_bytes)."""
    if len(plaintext) < 2:
        raise ValueError("feed plaintext truncated")
    n = int.from_bytes(plaintext[:2], "big")
    if len(plaintext) < 2 + n:
        raise ValueError("feed plaintext truncated")
    try:
        header = json.loads(plaintext[2:2 + n].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"feed header is not valid JSON: {e}") from e
    if not isinstance(header, dict):
        raise ValueError("feed header is not a JSON object")
    return header, plaintext[2 + n:]


def feed_seal(
    *,
    feed_key: bytes,
    feed_id: str,
    sender_member_id: str,
    sender_sig_priv_hex: str,
    sender_sig_pub_hex: str,
    filename: str,
    body: bytes,
    posted_at: int | None = None,
    _nonce: bytes | None = None,
) -> bytes:
    """Encrypt and authenticate a single feed post.

    Wire format:
        magic(5) || nonce(12) || mac(32) || ciphertext

    The ciphertext encrypts a length-prefixed JSON header followed by the raw
    file body. The header carries the sender's identity, the file metadata,
    and an Ed25519 signature over a transcript that includes the nonce.

    `_nonce` is a test hook; production callers leave it None.
    """
    # Bind the stamped public key to the signing key here — the one place we
    # hold both halves. A mismatched (priv, pub) pair would otherwise produce a
    # post every receiver silently rejects; fail locally instead.
    if bytes.fromhex(sender_sig_pub_hex) != ed25519_pub_from_seed(
        bytes.fromhex(sender_sig_priv_hex),
    ):
        raise ValueError("feed_seal: sig_pub does not match sig_priv")
    nonce = _nonce if _nonce is not None else secrets.token_bytes(_FEED_NONCE_LEN)
    content_hash = hashlib.sha256(body).digest()
    when = posted_at if posted_at is not None else int(time.time())
    transcript = _feed_sig_transcript(
        feed_id=feed_id,
        sender_member_id=sender_member_id,
        sender_sig_pub_hex=sender_sig_pub_hex,
        filename=filename,
        file_size=len(body),
        content_hash=content_hash,
        posted_at=when,
        nonce=nonce,
    )
    sig = ed25519_sign(transcript, bytes.fromhex(sender_sig_priv_hex))
    header = {
        "v": 1,
        "fid": feed_id,
        "smid": sender_member_id,
        "sik": sender_sig_pub_hex,
        "fn": filename,
        "fs": len(body),
        "ch": content_hash.hex(),
        "pa": when,
        "sig": sig.hex(),
    }
    plaintext = _feed_pack_header(header) + body
    enc_key, mac_key = _feed_subkeys(feed_key)
    ciphertext = _stream_xor(enc_key, nonce, plaintext)
    mac = hmac.new(
        mac_key, _FEED_MAGIC + nonce + ciphertext, hashlib.sha256,
    ).digest()
    return _FEED_MAGIC + nonce + mac + ciphertext


def feed_open(
    *,
    feed_key: bytes,
    feed_id: str,
    blob: bytes,
    expect_member_id: str | None = None,
    expect_sig_pub_hex: str | None = None,
) -> tuple[dict, bytes]:
    """Verify and decrypt a single feed post.

    Returns (header_dict, body_bytes). Raises ValueError on tamper, signature
    mismatch, or content-hash mismatch. `expect_member_id` /
    `expect_sig_pub_hex` are optional callsite assertions: if set, the
    decoded post must match them or verification fails.
    """
    if len(blob) < _FEED_HEADER_LEN:
        raise ValueError("feed blob too short")
    if blob[:len(_FEED_MAGIC)] != _FEED_MAGIC:
        raise ValueError("not a feed post (bad magic)")
    off = len(_FEED_MAGIC)
    nonce = blob[off:off + _FEED_NONCE_LEN]; off += _FEED_NONCE_LEN
    mac = blob[off:off + _FEED_MAC_LEN]; off += _FEED_MAC_LEN
    ciphertext = blob[off:]
    enc_key, mac_key = _feed_subkeys(feed_key)
    expected = hmac.new(
        mac_key, _FEED_MAGIC + nonce + ciphertext, hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, mac):
        raise ValueError("feed authentication failed")
    plaintext = _stream_xor(enc_key, nonce, ciphertext)
    header, body = _feed_unpack_header(plaintext)
    try:
        sender_member_id = header["smid"]
        sender_sig_pub_hex = header["sik"]
        filename = header["fn"]
        file_size = header["fs"]
        content_hash_hex = header["ch"]
        posted_at = header["pa"]
        sig_hex = header["sig"]
    except KeyError as e:
        raise ValueError(f"feed header missing field: {e}") from e
    if (not isinstance(sender_member_id, str)
            or not isinstance(sender_sig_pub_hex, str)
            or not isinstance(filename, str)
            or not isinstance(file_size, int)
            or not isinstance(content_hash_hex, str)
            or not isinstance(posted_at, int)
            or not isinstance(sig_hex, str)):
        raise ValueError("feed header has wrong field types")
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise ValueError(f"refusing unsafe filename in feed post: {filename!r}")
    if file_size != len(body):
        raise ValueError(
            f"feed file_size mismatch: header says {file_size}, body has {len(body)}",
        )
    actual_hash = hashlib.sha256(body).digest()
    try:
        claimed_hash = bytes.fromhex(content_hash_hex)
    except ValueError as e:
        raise ValueError("feed content_hash is not hex") from e
    if not hmac.compare_digest(actual_hash, claimed_hash):
        raise ValueError("feed content_hash mismatch (body corrupted)")
    if expect_member_id is not None and sender_member_id != expect_member_id:
        raise ValueError("feed sender_member_id mismatch")
    if expect_sig_pub_hex is not None and sender_sig_pub_hex != expect_sig_pub_hex:
        raise ValueError("feed sender_sig_pub mismatch")
    try:
        sig = bytes.fromhex(sig_hex)
        pub = bytes.fromhex(sender_sig_pub_hex)
    except ValueError as e:
        raise ValueError(f"feed signature/pubkey not hex: {e}") from e
    transcript = _feed_sig_transcript(
        feed_id=feed_id,
        sender_member_id=sender_member_id,
        sender_sig_pub_hex=sender_sig_pub_hex,
        filename=filename,
        file_size=file_size,
        content_hash=actual_hash,
        posted_at=posted_at,
        nonce=nonce,
    )
    if not ed25519_verify(transcript, sig, pub):
        raise ValueError("feed sender signature failed")
    return header, body


# ---------- feeds.json local store ----------
#
# Local persistence of joined feeds. One small file at ~/.config/nomnom/feeds.json
# tracks every feed this device has opened or joined: nickname, URL token,
# expiry, the member id this device uses inside the feed, a roster cache (for
# UI / TOFU prompts), and the last-seen post timestamp (for resume).
#
# Schema:
#   {
#     "version": 1,
#     "default": "<nickname>" | null,
#     "feeds": [Feed.to_dict(), ...]
#   }

_FEEDS_CONFIG_SCHEMA = 1

# nomnom has exactly one "channel": a single permanent feed shared across all of
# a user's own devices. It's stored as the lone entry in feeds.json under this
# fixed name. `_PERMANENT_TTL_SEC` matches the relay Worker's raised TTL cap.
_CHANNEL_NAME = "channel"
_PERMANENT_TTL_SEC = 3650 * 86_400  # ~10 years


@dataclass
class Feed:
    """A feed this device has joined.

    `feed_id` and `feed_token` are currently the same string — they're kept
    distinct for forward compatibility if the URL anatomy ever splits the
    identifier from the credential (e.g. fragment-style URLs).
    """

    name: str
    feed_id: str
    feed_token: str
    url: str
    expires_at: int
    joined_at: int
    member_id: str
    members_cache: list[dict] = field(default_factory=list)
    last_post_ts: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Feed:
        if not isinstance(d, dict):
            raise ValueError("feed entry is not a JSON object")
        try:
            return cls(
                name=str(d["name"]),
                feed_id=str(d["feed_id"]),
                feed_token=str(d.get("feed_token", d["feed_id"])),
                url=str(d["url"]),
                expires_at=int(d["expires_at"]),
                joined_at=int(d["joined_at"]),
                member_id=str(d["member_id"]),
                members_cache=list(d.get("members_cache", [])),
                last_post_ts=int(d.get("last_post_ts", 0)),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"feed entry missing/invalid field: {e}") from e


def _feeds_config_path() -> Path:
    return _nomnom_config_dir() / "feeds.json"


def _empty_feeds_config() -> dict:
    return {"version": _FEEDS_CONFIG_SCHEMA, "default": None, "feeds": []}


def _load_feeds_config() -> dict:
    """Return the current feeds config; empty config on missing/malformed file."""
    path = _feeds_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_feeds_config()
    if not isinstance(data, dict):
        return _empty_feeds_config()
    if data.get("version") != _FEEDS_CONFIG_SCHEMA:
        return _empty_feeds_config()
    raw_feeds = data.get("feeds") if isinstance(data.get("feeds"), list) else []
    feeds: list[Feed] = []
    for entry in raw_feeds:
        try:
            feeds.append(Feed.from_dict(entry))
        except ValueError:
            continue
    default = data.get("default")
    if not isinstance(default, str):
        default = None
    # Default must point at an existing feed; otherwise drop it.
    if default is not None and not any(f.name == default for f in feeds):
        default = None
    return {
        "version": _FEEDS_CONFIG_SCHEMA,
        "default": default,
        "feeds": feeds,
    }


def _save_feeds_config(config: dict) -> None:
    """Write the feeds config atomically. Accepts the shape returned by _load."""
    feeds = config.get("feeds") or []
    body = {
        "version": _FEEDS_CONFIG_SCHEMA,
        "default": config.get("default"),
        "feeds": [
            f.to_dict() if isinstance(f, Feed) else f
            for f in feeds
        ],
    }
    _atomic_write_text(_feeds_config_path(), json.dumps(body, indent=2))


def _find_feed(config: dict, nickname: str) -> Feed | None:
    """Return the Feed with the given nickname, or None."""
    for f in config.get("feeds") or []:
        if isinstance(f, Feed) and f.name == nickname:
            return f
    return None


def _default_feed(config: dict) -> Feed | None:
    """Return the configured default feed, or None.

    Legacy: the single-channel model uses `feeds[0]` via `_the_channel`; the
    `default` field and this helper are retained for back-compat with configs
    written by the old multi-feed code."""
    name = config.get("default")
    if not isinstance(name, str):
        return None
    return _find_feed(config, name)


def _format_feed_url(host: str, feed_id: str) -> str:
    """Build the shareable feed URL. Always https://."""
    if "://" not in host:
        host = f"https://{host}"
    return f"{host.rstrip('/')}/f/{feed_id}"


def _parse_feed_url(url: str) -> tuple[str, str]:
    """Split a feed URL into (host, feed_id). Strict: requires /f/<token>."""
    raw = url.strip()
    if not raw:
        raise NomnomError("feed url must not be empty")
    if raw.startswith("https://"):
        rest = raw[len("https://"):]
    elif raw.startswith("http://"):
        rest = raw[len("http://"):]
    else:
        rest = raw
    if "/f/" not in rest:
        raise NomnomError("feed url must contain '/f/<token>'")
    host, _, token = rest.partition("/f/")
    if not host or "/" in token or not token:
        raise NomnomError("malformed feed url")
    if not _FEED_TOKEN_RE.match(token):
        raise NomnomError(
            "feed token in url must be 8-32 url-safe base64 chars",
        )
    return host, token


def _add_or_replace_feed(config: dict, feed: Feed) -> dict:
    """Insert `feed` into config (replacing any existing entry with the same name).

    If this is the first feed, mark it as the default. Returns the updated
    config dict (mutated in place for convenience).
    """
    feeds = config.get("feeds") or []
    feeds = [f for f in feeds if not (isinstance(f, Feed) and f.name == feed.name)]
    feeds.append(feed)
    config["feeds"] = feeds
    if not config.get("default") and len(feeds) == 1:
        config["default"] = feed.name
    return config


def _persist_members_cache(feed: Feed) -> None:
    """Write feed.members_cache through to the stored copy in feeds.json."""
    cfg = _load_feeds_config()
    existing = _find_feed(cfg, feed.name)
    if existing is not None:
        existing.members_cache = feed.members_cache
        _save_feeds_config(cfg)


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
    out = {"url": url.rstrip("/"), "secret": secret}
    # Carry the user's explicit private-address opt-in (e.g. a local dev
    # Worker) so the per-request SSRF re-check in _relay_open honors it.
    if data.get("allow_private") is True:
        out["allow_private"] = True
    return out


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
    config = {"url": url.rstrip("/"), "secret": secret}
    # Persist the opt-in so _relay_open's per-request re-check doesn't reject a
    # local dev Worker the user deliberately allowed.
    if allow_private:
        config["allow_private"] = True
    _atomic_write_text(_relay_config_path(), json.dumps(config, indent=2))


def _relay_clear_config() -> None:
    with contextlib.suppress(OSError):
        _relay_config_path().unlink()


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


def _assert_relay_url_allowed(relay: dict) -> None:
    """Re-run the private-address guard on every request, not just at import.

    `_save_relay_config` checks once at write time, but each request re-resolves
    the host at connect — so a name that was public at import can later resolve
    to loopback/metadata (DNS rebinding / TOCTOU). Checking here, the one
    chokepoint every request flows through, closes that gap. (A stronger fix
    pins the vetted literal IP and connects to it; deferred as follow-up.)
    """
    if relay.get("allow_private"):
        return
    reason = _url_resolves_private(relay["url"])
    if reason is not None:
        raise NomnomError(f"refusing relay request: {reason}")


def _relay_open(
    relay: dict, *, timeout: float = _RELAY_REQUEST_TIMEOUT,
) -> http.client.HTTPConnection:
    _assert_relay_url_allowed(relay)
    host, port, _, is_https = _relay_split_url(relay["url"])
    if is_https:
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _relay_full_path(relay: dict, path: str) -> str:
    _, _, base, _ = _relay_split_url(relay["url"])
    return f"{base}{path}" if base else path


def _http_send(
    relay: dict, method: str, full_path: str,
    *, body: bytes | None, headers: dict[str, str],
    content_type: str = "application/octet-stream",
) -> tuple[int, bytes]:
    """Open a connection, send one request, return (status, body).

    Shared core for the HMAC (`_relay_request`) and feed-key (`_feed_request`)
    schemes — they differ only in which auth headers they build. Enforces the
    response-size ceiling (declared Content-Length and actual bytes) and maps
    network errors to NomnomError. `_relay_open` is the injection seam for
    tests (monkeypatch it to feed canned responses without a live socket)."""
    if body is not None:
        headers.setdefault("Content-Length", str(len(body)))
        headers.setdefault("Content-Type", content_type)
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


def _relay_request(
    relay: dict, method: str, path: str,
    *, body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
    signed: bool = True,
) -> tuple[int, bytes]:
    """One-shot HMAC-signed relay request. Returns (status, body)."""
    full_path = _relay_full_path(relay, path)
    headers: dict[str, str] = {}
    if signed:
        headers.update(_relay_hmac_headers(relay["secret"], method, full_path))
    if extra_headers:
        headers.update(extra_headers)
    return _http_send(relay, method, full_path, body=body, headers=headers)


def _qs(**params: int) -> str:
    """Build `?k=v&...` from positive int kwargs; empty string if none apply.

    Zero/negative params are omitted, so `since=0` can't be sent explicitly —
    the Worker treats an absent `since` as 0, which is the same thing."""
    parts = [f"{k}={int(v)}" for k, v in params.items() if v and v > 0]
    return ("?" + "&".join(parts)) if parts else ""


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
    status, data = _relay_request(relay, "GET", f"/slots/{slot_id}{_qs(wait=wait_ms)}")
    if status == 200:
        return data
    if status == 404:
        return None
    _raise_relay_error(status, data)


def _relay_delete_slot(relay: dict, slot_id: str) -> None:
    """Best-effort cleanup; swallows network errors."""
    try:
        _relay_request(relay, "DELETE", f"/slots/{slot_id}")
    except NomnomError:
        pass


# --- feed-key-signed HTTP (parallel to the relay HMAC scheme above) ---


_FEED_AUTH_PREFIX = "NMNM-FEEDKEY-SHA256 "


def _feed_relay_dict(host: str) -> dict:
    """Build a relay-shaped dict for feed-key-signed requests.

    Reuses the connection helpers (_relay_open, _relay_full_path) but skips
    the HMAC scheme — the URL token IS the credential for /feeds/:id/*.
    """
    if "://" not in host:
        host = f"https://{host}"
    relay = {"url": host.rstrip("/")}
    # Honor the configured relay's private-address opt-in when the feed lives on
    # that same host, so _relay_open's re-check doesn't reject a local dev relay.
    cfg = _load_relay_config()
    if cfg and cfg.get("allow_private"):
        cfg_host = urllib.parse.urlsplit(cfg["url"]).hostname
        if cfg_host and cfg_host == urllib.parse.urlsplit(relay["url"]).hostname:
            relay["allow_private"] = True
    return relay


def _feed_auth_headers(feed_key: bytes, method: str, path: str) -> dict[str, str]:
    """Authorization header for one /feeds/:id/* request.

    Same shape as `_relay_hmac_headers` (method + path + unix_ts), keyed by
    the per-feed key. The Worker re-derives the same key from the URL token
    and verifies on receipt. Query strings are stripped so the signed path
    matches the Worker's `url.pathname`.
    """
    bare_path = path.split("?", 1)[0]
    ts = str(int(time.time()))
    msg = f"{method}\n{bare_path}\n{ts}".encode("utf-8")
    mac = hmac.new(feed_key, msg, hashlib.sha256).hexdigest()
    return {
        "Authorization": f"{_FEED_AUTH_PREFIX}{ts}:{mac}",
        "User-Agent": _RELAY_USER_AGENT,
    }


def _feed_request(
    host: str,
    feed_key: bytes,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    content_type: str = "application/octet-stream",
) -> tuple[int, bytes]:
    """One-shot feed-key-signed request. Returns (status, body)."""
    relay = _feed_relay_dict(host)
    full_path = _relay_full_path(relay, path)
    headers = _feed_auth_headers(feed_key, method, full_path)
    return _http_send(
        relay, method, full_path,
        body=body, headers=headers, content_type=content_type,
    )


def _decode_relay_reason(body: bytes) -> str:
    """Extract `error` from a JSON body, else the raw text (back-compat)."""
    text = body.decode("utf-8", errors="replace").strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, str):
                return err
    return text


def _raise_feed_error(status: int, body: bytes) -> NoReturn:
    reason = _decode_relay_reason(body)
    if status == 401:
        if reason == "clock-skew":
            raise NomnomError(
                "relay rejected feed request (clock skew). sync the system clock.",
            )
        raise NomnomError(
            "relay rejected feed-key signature. the feed URL may be wrong or stale.",
        )
    # Malformed ids arrive as 400 (older relays sent 403); both stay specific.
    if status in (400, 403):
        raise NomnomError(f"relay refused feed request: {reason or '(no detail)'}")
    if status == 404:
        raise NomnomError("feed not found on relay")
    if status == 409:
        raise NomnomError(f"relay returned 409: {reason or '(no detail)'}")
    if status == 410:
        raise NomnomError("feed has expired on the relay")
    if status == 413:
        raise NomnomError("payload too large for relay")
    raise NomnomError(f"relay returned HTTP {status}: {reason or '(no body)'}")


def _relay_mint_feed(
    relay: dict, *, ttl_seconds: int, member_card: dict,
) -> dict:
    """POST /feeds — HMAC-gated. Returns {feed_id, expires_at, created_at}."""
    body = json.dumps(
        {"ttl_seconds": int(ttl_seconds), "member_card": member_card},
        separators=(",", ":"),
    ).encode("utf-8")
    status, data = _relay_request(
        relay, "POST", "/feeds", body=body,
        extra_headers={"Content-Type": "application/json"},
    )
    if status == 201:
        try:
            return json.loads(data)
        except json.JSONDecodeError as e:
            raise NomnomError(f"relay returned bad JSON: {e}") from e
    _raise_relay_error(status, data)


def _feed_json(
    host: str, feed_key: bytes, method: str, path: str,
    *, ok: int = 200, body: bytes | None = None,
    content_type: str = "application/octet-stream",
) -> dict:
    """Feed-key request that returns parsed JSON on `ok`, else raises.

    Folds the parse-or-raise tail repeated across the feed GET/POST endpoints."""
    status, data = _feed_request(
        host, feed_key, method, path, body=body, content_type=content_type,
    )
    if status == ok:
        try:
            return json.loads(data)
        except json.JSONDecodeError as e:
            raise NomnomError(f"relay returned bad JSON: {e}") from e
    _raise_feed_error(status, data)


def _relay_get_feed_meta(host: str, feed_id: str, feed_key: bytes) -> dict:
    return _feed_json(host, feed_key, "GET", f"/feeds/{feed_id}/meta")


def _relay_put_member(
    host: str, feed_id: str, feed_key: bytes, member_id: str, card: dict,
) -> None:
    body = json.dumps(card, separators=(",", ":")).encode("utf-8")
    status, data = _feed_request(
        host, feed_key, "PUT", f"/feeds/{feed_id}/members/{member_id}",
        body=body, content_type="application/json",
    )
    if status == 204:
        return
    _raise_feed_error(status, data)


def _relay_delete_member(
    host: str, feed_id: str, feed_key: bytes, member_id: str,
) -> None:
    try:
        status, data = _feed_request(
            host, feed_key, "DELETE", f"/feeds/{feed_id}/members/{member_id}",
        )
    except NomnomError:
        return  # leave is best-effort
    if status not in (204, 404):
        _raise_feed_error(status, data)


def _relay_list_members(
    host: str, feed_id: str, feed_key: bytes,
    *, since_ts: int = 0, wait_ms: int = 0,
) -> dict:
    return _feed_json(
        host, feed_key, "GET",
        f"/feeds/{feed_id}/members{_qs(wait=wait_ms, since=since_ts)}",
    )


def _relay_extend_feed(
    host: str, feed_id: str, feed_key: bytes, new_ttl_seconds: int,
) -> dict:
    body = json.dumps({"new_ttl_seconds": int(new_ttl_seconds)}).encode("utf-8")
    return _feed_json(
        host, feed_key, "POST", f"/feeds/{feed_id}/extend",
        body=body, content_type="application/json",
    )


def _relay_close_feed(host: str, feed_id: str, feed_key: bytes) -> None:
    status, data = _feed_request(
        host, feed_key, "DELETE", f"/feeds/{feed_id}",
    )
    if status in (204, 404):
        return
    _raise_feed_error(status, data)


def _relay_put_feed_slot(
    host: str, feed_id: str, feed_key: bytes, slot_id: str, body: bytes,
) -> None:
    if len(body) > _RELAY_MAX_BODY:
        raise NomnomError(
            f"payload too large for relay: {len(body)} > {_RELAY_MAX_BODY} bytes",
        )
    status, data = _feed_request(
        host, feed_key, "PUT", f"/feeds/{feed_id}/slots/{slot_id}",
        body=body,
    )
    if status == 204:
        return
    _raise_feed_error(status, data)


def _relay_get_feed_slot(
    host: str, feed_id: str, feed_key: bytes, slot_id: str,
    *, wait_ms: int = 0,
) -> bytes | None:
    # 200 -> bytes, 404 -> None (slot gone; a permanent, advance-safe outcome).
    # Everything else — a network error or a non-404 status — is a transient
    # fetch failure for THIS slot: surface it as NomnomTransportError so the
    # receive loops leave their cursor put and retry, mirroring the web client
    # (which never advances on a slot GET error). Advancing here would silently
    # and permanently drop an unread post.
    try:
        status, data = _feed_request(
            host, feed_key, "GET",
            f"/feeds/{feed_id}/slots/{slot_id}{_qs(wait=wait_ms)}",
        )
    except NomnomError as e:
        raise NomnomTransportError(str(e)) from e
    if status == 200:
        return data
    if status == 404:
        return None
    try:
        _raise_feed_error(status, data)
    except NomnomError as e:
        raise NomnomTransportError(str(e)) from e


def _relay_list_feed_slots(
    host: str, feed_id: str, feed_key: bytes,
    *, since_ts: int = 0, wait_ms: int = 0,
) -> list:
    parsed = _feed_json(
        host, feed_key, "GET",
        f"/feeds/{feed_id}/slots{_qs(wait=wait_ms, since=since_ts)}",
    )
    return parsed.get("slots") or []


# --- SSE slot stream (real-time push; stdlib-only streaming GET) ---

_STREAM_SOCKET_TIMEOUT = 40.0   # > the relay's 20s SSE heartbeat; detects a dead link
_STREAM_RECONNECT_S = 1.0       # backoff before reopening after a drop / the ~4min cap
_STREAM_MAX_LINE = 64 * 1024    # cap a single SSE line (frames are tiny JSON)


class _StreamUnsupported(Exception):
    """The relay has no /stream endpoint (predates SSE). Caller long-polls."""


def _feed_stream_lines(host: str, feed_key: bytes, path: str):
    """Yield decoded SSE lines from one long-lived feed-key-signed GET.

    Reads the response line by line (chunk-aware via HTTPResponse.readline) so
    notifications surface as they arrive. Raises `_StreamUnsupported` on 404 (no
    endpoint) and `NomnomError` on other non-200s or an initial connect failure.
    A mid-stream read error or EOF (the relay's ~4min cap) ends the generator;
    the caller reconnects.
    """
    relay = _feed_relay_dict(host)
    full_path = _relay_full_path(relay, path)
    headers = _feed_auth_headers(feed_key, "GET", full_path)
    headers["Accept"] = "text/event-stream"
    try:
        conn = _relay_open(relay, timeout=_STREAM_SOCKET_TIMEOUT)
    except (OSError, http.client.HTTPException) as e:
        raise NomnomError(f"relay stream connect failed: {e}") from e
    try:
        try:
            conn.request("GET", full_path, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            raise NomnomError(f"relay stream request failed: {e}") from e
        if resp.status == 404:
            try:
                resp.read()
            except (OSError, http.client.HTTPException):
                pass
            raise _StreamUnsupported()
        if resp.status != 200:
            body = resp.read(_RELAY_MAX_BODY + 1)
            _raise_feed_error(resp.status, body)
        while True:
            try:
                line = resp.readline(_STREAM_MAX_LINE)
            except (OSError, http.client.HTTPException):
                return  # transient drop — caller reconnects
            if not line:
                return  # EOF: server closed (cap reached) — caller reconnects
            yield line.decode("utf-8", errors="replace").rstrip("\r\n")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _relay_stream_feed_slots(
    host: str, feed_id: str, feed_key: bytes,
    *, since_fn, stop: "threading.Event | None" = None,
):
    """Yield `{slot_id, created_at}` as posts arrive, over SSE, reconnecting.

    `since_fn()` is read at each (re)connect so replay resumes from the caller's
    current cursor (the feed-key MAC is freshly signed each time, keeping its
    timestamp inside the relay's skew window). Stops when `stop` is set.
    Propagates `_StreamUnsupported` / `NomnomError`; a transient drop or the
    server's ~4min cap reconnects after a short backoff.
    """
    while stop is None or not stop.is_set():
        since = max(0, int(since_fn()))
        path = f"/feeds/{feed_id}/stream{_qs(since=since)}"
        for line in _feed_stream_lines(host, feed_key, path):
            if stop is not None and stop.is_set():
                return
            if not line.startswith("data:"):
                continue  # comment (": ping"/": ok"), id line, or blank
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            slot_id = obj.get("slot_id")
            if not isinstance(slot_id, str):
                continue
            created_at = obj.get("created_at")
            yield {
                "slot_id": slot_id,
                "created_at": int(created_at)
                if isinstance(created_at, (int, float)) else 0,
            }
        # Stream ended (drop or cap). Back off, then reconnect from the cursor.
        if stop is None:
            time.sleep(_STREAM_RECONNECT_S)
        elif stop.wait(_STREAM_RECONNECT_S):
            return


def _relay_health(relay: dict) -> bool:
    try:
        status, data = _relay_request(relay, "GET", "/health", signed=False)
    except NomnomError:
        return False
    return status == 200 and data.strip() == b"ok"


def _raise_relay_error(status: int, body: bytes) -> NoReturn:
    reason = _decode_relay_reason(body)
    if status == 401:
        if reason == "clock-skew":
            raise NomnomError(
                "relay rejected request (clock skew). sync the system clock.",
            )
        raise NomnomError(
            "relay rejected authentication (wrong secret?). "
            "run `nomnom relay test` to diagnose.",
        )
    # Malformed ids arrive as 400 (older relays sent 403); both stay specific.
    if status in (400, 403):
        raise NomnomError(f"relay refused slot id: {reason or '(no detail)'}")
    if status == 409:
        raise NomnomError("relay slot already occupied")
    if status == 410:
        raise NomnomError("relay slot expired")
    if status == 413:
        raise NomnomError("payload too large for relay (256 MB max; 100 MB on free tier)")
    raise NomnomError(f"relay returned HTTP {status}: {reason or '(no body)'}")


# --- relay self-test ---


def _relay_self_test(relay: dict) -> tuple[int, str]:
    """End-to-end check: /health, then a round-trip PUT + GET on a random slot."""
    if not _relay_health(relay):
        return 1, "relay /health unreachable (URL wrong, or worker not deployed)"
    slot = "selftest-" + secrets.token_urlsafe(16)
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


# Two-message identity exchange, decoupled from the send/receive ciphertext
# pipeline. Both sides invoke `nomnom pair`; race-decided role:
#   PUT pair_i succeeds   -> initiator. Long-poll pair_r_<own_ik>_p.
#   PUT pair_i 409s       -> responder. GET pair_i, PUT pair_r_<their_ik>_p.
# No DH, no session key, no payload. TOFU prompt + out-of-band fingerprint
# check is the trust gate, same as before.


def _the_channel() -> Feed | None:
    """Return the single channel feed, or None if this device isn't set up."""
    feeds = _load_feeds_config().get("feeds") or []
    return feeds[0] if feeds else None


def _no_channel_hint() -> str:
    return (
        "no channel yet. run `nomnom init` (first device) or "
        "`nomnom join <secret>` (other devices)."
    )


def _resolve_target_feed() -> Feed | None:
    """Return the channel for send/receive, printing a friendly error on miss."""
    feed = _the_channel()
    if feed is None:
        sys.stderr.write(f"error: {_no_channel_hint()}\n")
    return feed


def _refresh_roster_with_tofu(
    feed: Feed, host: str, feed_key: bytes, *, trust_new: bool,
) -> list[dict] | None:
    """Fetch the live roster and prompt TOFU on any newly-seen identities.

    Returns the fresh roster on success, None if the relay errored. Updates
    `feed.members_cache` in place but does NOT persist — the caller decides
    when to write feeds.json.
    """
    try:
        resp = _relay_list_members(host, feed.feed_id, feed_key)
    except NomnomError as e:
        sys.stderr.write(f"error: {e}\n")
        return None
    roster = resp.get("members") or []
    known_in_cache = {
        m.get("identity_pubkey")
        for m in (feed.members_cache or [])
        if isinstance(m, dict)
    }
    for m in roster:
        if not isinstance(m, dict):
            continue
        if m.get("member_id") == feed.member_id:
            continue
        sig_pub = m.get("identity_pubkey")
        if sig_pub in known_in_cache:
            # Seen locally before; either already pinned or previously declined.
            continue
        _tofu_check_feed_member(m, trust_new=trust_new)
    feed.members_cache = roster
    return roster


def _feed_send_bytes(
    target: "Feed", data: bytes, filename: str, *, trust_new: bool = False,
) -> list[str]:
    """Encrypt and broadcast `data` into `target`. Returns status lines.

    Refreshes the roster (running TOFU on any newly-seen identities) before
    sealing, so member counts are accurate and senders are authenticated.
    Raises NomnomError on oversize input or a relay failure.
    """
    if len(data) > _RELAY_MAX_BODY:
        raise NomnomError(
            f"too large for relay ({len(data)} bytes; limit {_RELAY_MAX_BODY}).",
        )
    feed_key = _feed_key_from_token(target.feed_token)
    host, _ = _parse_feed_url(target.url)

    # Always-fresh roster on send: accurate member counts + TOFU on new identities.
    roster = _refresh_roster_with_tofu(target, host, feed_key, trust_new=trust_new)
    if roster is None:
        raise NomnomError("could not refresh feed roster.")
    others = [m for m in roster if m.get("member_id") != target.member_id]
    lines: list[str] = []
    if not others:
        lines.append(
            "warning: your channel has no other devices yet; the file will "
            "wait until one joins with `nomnom join`.",
        )

    ident = _load_identity()
    blob = feed_seal(
        feed_key=feed_key,
        feed_id=target.feed_id,
        sender_member_id=target.member_id,
        sender_sig_priv_hex=ident["sig_priv"],
        sender_sig_pub_hex=ident["sig_pub"],
        filename=filename,
        body=data,
    )
    slot_id = secrets.token_urlsafe(12)
    _relay_put_feed_slot(host, target.feed_id, feed_key, slot_id, blob)
    # Persist the cache refresh.
    _persist_members_cache(target)
    lines.append(
        f"sent {filename!r} ({len(data)} bytes) to your channel "
        f"({len(others)} device(s)).",
    )
    return lines


def _has_send_target() -> bool:
    """True when a channel is configured (so SEND is a usable destination)."""
    try:
        return _the_channel() is not None
    except Exception:
        return False


def _emit_to_feed(data: bytes, filename: str) -> tuple[int, list[str]]:
    """Broadcast already-rendered bytes to the channel. (rc, status lines).

    The SEND destination for bundle/commit/pr/item routes here. Targets the
    single channel. Never raises.
    """
    target = _the_channel()
    if target is None:
        return 1, [_no_channel_hint()]
    try:
        return 0, _feed_send_bytes(target, data, _safe_filename(filename))
    except NomnomError as e:
        return 1, [f"send failed: {e}"]


def cmd_send(path: str, *, trust_new: bool = False) -> int:
    """Broadcast a file to every other device on your channel."""
    p = Path(path).expanduser()
    if not p.is_file():
        sys.stderr.write(f"error: not a file: {p}\n")
        return 1
    try:
        size = p.stat().st_size
    except OSError as e:
        sys.stderr.write(f"error: cannot stat {p}: {e}\n")
        return 1
    if size > _RELAY_MAX_BODY:
        sys.stderr.write(
            f"error: file too large for relay ({size} bytes; "
            f"limit {_RELAY_MAX_BODY}).\n",
        )
        return 1
    try:
        data = p.read_bytes()
    except OSError as e:
        sys.stderr.write(f"error: cannot read {p}: {e}\n")
        return 1

    target = _resolve_target_feed()
    if target is None:
        return 1
    # NomnomError propagates to the dispatcher, which prints + returns 1.
    lines = _feed_send_bytes(target, data, p.name, trust_new=trust_new)
    for ln in lines:
        sys.stderr.write(ln + "\n")
    return 0


def _safe_filename(name: str) -> str:
    """Strip directory components from a filename; refuse path-escape attempts."""
    base = os.path.basename(name)
    if not base or base in (".", "..") or "/" in base or "\\" in base:
        raise NomnomError(f"refusing unsafe filename: {name!r}")
    return base


def _receive_one_post(
    *, feed: Feed, host: str, feed_key: bytes, slot_id: str,
) -> tuple[str, int, str, str] | None:
    """Fetch and verify a single post. Returns (filename, bytes, sender_name, out_path).

    Returns None when the slot disappeared (already cleaned up) or the post
    is this device's own broadcast (nothing to write back). Raises
    NomnomError on tamper / signature failure / unknown sender.
    """
    raw = _relay_get_feed_slot(host, feed.feed_id, feed_key, slot_id)
    if raw is None:
        return None
    # Resolve sender from cached roster first; refresh if missing.
    try:
        header, body = feed_open(
            feed_key=feed_key, feed_id=feed.feed_id, blob=raw,
        )
    except ValueError as e:
        raise NomnomError(f"feed post rejected: {e}") from e
    sender_id = header.get("smid")
    if sender_id == feed.member_id:
        return None  # our own broadcast comes back on the feed; don't rewrite it
    sender_pub = header.get("sik")
    sender_name = "(unknown)"
    for m in feed.members_cache or []:
        if m.get("member_id") == sender_id:
            sender_name = m.get("name", sender_name)
            # Cross-check that the cached identity matches.
            if m.get("identity_pubkey") and m.get("identity_pubkey") != sender_pub:
                raise NomnomError(
                    f"feed post sender {sender_id!r} identity key changed "
                    "from roster cache (possible spoof or rotation); "
                    "re-fetch roster",
                )
            break
    filename = _safe_filename(header.get("fn") or "post")
    out = _pick_decrypted_path(Path.cwd(), filename)
    out.write_bytes(body)
    return (filename, len(body), sender_name, str(out))


def _receive_and_report(
    *, feed: Feed, host: str, feed_key: bytes, slot_id: str,
) -> bool:
    """Fetch/verify/write one post and narrate to stderr. True iff a file landed.

    Shared by the SSE and long-poll receive paths so tamper handling
    (raise -> warn + skip) stays identical between them.

    Propagates NomnomTransportError (a transient fetch failure) so the caller can
    leave its cursor put and retry; a permanent rejection (tamper/spoof) is warned
    and swallowed (returns False), which the caller treats as advance-safe.
    """
    try:
        res = _receive_one_post(
            feed=feed, host=host, feed_key=feed_key, slot_id=slot_id,
        )
    except NomnomTransportError:
        raise
    except NomnomError as e:
        sys.stderr.write(f"warning: {e}\n")
        return False
    if res is None:
        return False
    filename, nbytes, sender, out_path = res
    sys.stderr.write(
        f"received {filename!r} ({nbytes} bytes) from {sender} -> {out_path}\n",
    )
    sys.stderr.flush()
    return True


def _receive_refresh_roster(
    feed: Feed, host: str, feed_key: bytes, trust_new: bool,
) -> None:
    """Refresh the roster (running TOFU prompts) and persist it onto `feed`."""
    roster = _refresh_roster_with_tofu(feed, host, feed_key, trust_new=trust_new)
    if roster is not None:
        _persist_members_cache(feed)


def _receive_persist_ts(feed: Feed, last_ts: int) -> None:
    """Persist last_post_ts so a restart doesn't re-fetch already-seen posts."""
    cfg = _load_feeds_config()
    existing = _find_feed(cfg, feed.name)
    if existing is not None and existing.last_post_ts != last_ts:
        existing.last_post_ts = last_ts
        _save_feeds_config(cfg)


def _cmd_receive_stream(
    target: Feed, host: str, feed_key: bytes, last_ts: int, trust_new: bool,
) -> int:
    """Watch via the SSE /stream endpoint (real-time push).

    Reuses `_receive_one_post` for fetch/verify/write. Refreshes the roster (+
    TOFU) before each post so new senders resolve. Raises `_StreamUnsupported`
    if the relay has no /stream (caller falls back to long-poll); handles
    Ctrl-C and relay errors internally.
    """
    received_any = False
    try:
        # Prime the roster so the very first post can name its sender.
        _receive_refresh_roster(target, host, feed_key, trust_new)
        for entry in _relay_stream_feed_slots(
            host, target.feed_id, feed_key, since_fn=lambda: last_ts,
        ):
            slot_id = entry.get("slot_id")
            created_at = int(entry.get("created_at") or 0)
            if not slot_id or created_at <= last_ts:
                if created_at > last_ts:
                    last_ts = created_at
                    _receive_persist_ts(target, last_ts)
                continue
            # Refresh before each post so a just-joined sender resolves + TOFUs.
            _receive_refresh_roster(target, host, feed_key, trust_new)
            try:
                landed = _receive_and_report(
                    feed=target, host=host, feed_key=feed_key, slot_id=slot_id,
                )
            except NomnomTransportError as e:
                # Transport hiccup fetching this slot — do NOT advance the cursor
                # or we'd permanently drop an unread post. Leaving last_ts put
                # lets the next event re-surface it.
                sys.stderr.write(f"warning: {e}\n")
                continue
            if landed:
                received_any = True
            if created_at > last_ts:
                last_ts = created_at
            _receive_persist_ts(target, last_ts)
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        return 0 if received_any else 130
    except NomnomError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    return 0


def cmd_receive(*, once: bool = False, trust_new: bool = False) -> int:
    """Watch your channel for new files; write each to cwd.

    Continuous mode pushes via the SSE /stream endpoint (falling back to the
    /slots long-poll if the relay has no /stream). `--once` uses the long-poll
    directly. Each new post is decrypted, signature-verified, and written to
    disk (collisions auto-rename). Ctrl-C exits cleanly.
    """
    target = _resolve_target_feed()
    if target is None:
        return 1
    feed_key = _feed_key_from_token(target.feed_token)
    host, _ = _parse_feed_url(target.url)

    received_any = False
    if not once:
        sys.stderr.write("watching for files (Ctrl-C to exit)...\n")
        sys.stderr.flush()

    last_ts = target.last_post_ts
    if not once:
        try:
            return _cmd_receive_stream(target, host, feed_key, last_ts, trust_new)
        except _StreamUnsupported:
            sys.stderr.write(
                "note: relay has no /stream endpoint; using long-poll.\n",
            )
            sys.stderr.flush()
            # The stream path persisted its cursor into feeds.json, not into
            # our local `target`; re-read so the long-poll resumes where the
            # stream left off instead of replaying already-received posts.
            ch = _the_channel()
            if ch is not None:
                last_ts = ch.last_post_ts
            # fall through to the long-poll loop below
    while True:
        # Roster refresh + TOFU prompts before each slot long-poll. Up to a
        # 30s lag between a new member joining and the prompt firing, but
        # interleaving keeps the I/O on a single thread.
        _receive_refresh_roster(target, host, feed_key, trust_new)
        try:
            slots = _relay_list_feed_slots(
                host, target.feed_id, feed_key,
                since_ts=last_ts, wait_ms=30_000,
            )
        except KeyboardInterrupt:
            sys.stderr.write("\n")
            return 0 if received_any else 130
        except NomnomError as e:
            sys.stderr.write(f"error: {e}\n")
            return 1
        if not slots:
            if once:
                sys.stderr.write("no transfer (waited 30s)\n")
                return 0 if received_any else 1
            continue
        # Iterate in chronological order; the Worker already sorts by created_at.
        for entry in slots:
            slot_id = entry.get("slot_id")
            created_at = int(entry.get("created_at") or 0)
            landed = False
            if slot_id:
                try:
                    landed = _receive_and_report(
                        feed=target, host=host, feed_key=feed_key, slot_id=slot_id,
                    )
                except NomnomTransportError as e:
                    # Transport hiccup fetching this slot — leave the cursor put
                    # so the next long-poll re-lists it instead of skipping it
                    # forever. Advancing here would permanently drop the post.
                    sys.stderr.write(f"warning: {e}\n")
                    continue
                if landed:
                    received_any = True
            last_ts = max(last_ts, created_at)
            _receive_persist_ts(target, last_ts)
            if once and landed:
                return 0


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
    hint = "run `nomnom init` (first device) or `nomnom join <secret>` (other devices)"
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


def _wrangler_deploy_hint(secret: str) -> str:
    """The two-line `wrangler secret put / deploy` hint shown on the Worker side."""
    return (
        f"  echo {secret!r} | npx wrangler secret put NOMNOM_HMAC_SECRET\n"
        "  npx wrangler deploy\n"
    )


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
        sys.stderr.write(_wrangler_deploy_hint(secret))
        return None
    try:
        _save_relay_config(url, secret, allow_private=allow_private)
    except NomnomError as e:
        sys.stderr.write(f"error: {e}\n")
        return None
    sys.stderr.write(f"saved to {_relay_config_path()} ({msg})\n")
    sys.stderr.write("\non the Worker side, run (if you haven't already):\n")
    sys.stderr.write(_wrangler_deploy_hint(secret))
    return candidate


def _cmd_relay_init(args) -> int:
    cfg = _cmd_relay_init_interactive(
        allow_private=getattr(args, "allow_private", False),
    )
    return 0 if cfg is not None else 1


def cmd_init(args) -> int:
    """Set up the relay (first device) and create your one permanent channel.

    Run once, on the device that owns the relay. Configures relay.json if it's
    missing, mints a single long-lived feed (the "channel"), stores it, and
    prints the channel secret to paste on your other devices via `nomnom join`.
    """
    if _the_channel() is not None:
        sys.stderr.write(
            "error: a channel already exists on this device. run "
            "`nomnom channel` to show its secret, or `nomnom reset` to "
            "start over.\n",
        )
        return 1

    relay = _load_relay_config()
    if relay is None:
        if not sys.stdin.isatty():
            sys.stderr.write(
                "error: no relay configured, and stdin is not a tty so the "
                "setup prompts can't run. run `nomnom init` interactively on "
                "the device that owns the relay.\n",
            )
            return 1
        relay = _cmd_relay_init_interactive(
            allow_private=getattr(args, "allow_private", False),
        )
        if relay is None:
            return 1

    ident = _load_identity()
    member_id = secrets.token_hex(16)
    card = {
        "member_id": member_id,
        "identity_pubkey": ident["sig_pub"],
        "name": ident["name"],
    }
    # NomnomError propagates to the dispatcher, which prints + returns 1.
    result = _relay_mint_feed(
        relay, ttl_seconds=_PERMANENT_TTL_SEC, member_card=card,
    )

    feed_id = str(result.get("feed_id") or "")
    if not feed_id:
        sys.stderr.write("error: relay did not return a feed_id.\n")
        return 1
    created_at = int(result.get("created_at") or time.time())
    expires_at = int(
        result.get("expires_at") or (created_at + _PERMANENT_TTL_SEC),
    )
    host = _relay_split_url(relay["url"])[0]
    feed = Feed(
        name=_CHANNEL_NAME,
        feed_id=feed_id,
        feed_token=feed_id,
        url=_format_feed_url(host, feed_id),
        expires_at=expires_at,
        joined_at=created_at,
        member_id=member_id,
        members_cache=[
            {
                "member_id": member_id,
                "identity_pubkey": ident["sig_pub"],
                "name": ident["name"],
                "joined_at": created_at,
            },
        ],
    )
    cfg = _load_feeds_config()
    cfg["feeds"] = [feed]
    cfg["default"] = _CHANNEL_NAME
    _save_feeds_config(cfg)

    sys.stderr.write(
        "channel created. paste this secret on your other devices "
        "(`nomnom join <secret>`):\n",
    )
    print(feed.url)
    return 0


def _refresh_channel_roster(feed: Feed) -> list[dict]:
    """Fetch the channel roster and persist it onto `feed` and the stored copy.

    Shared by `cmd_channel` and the TUI ChannelScreen so both cache identically
    (mirrors the CLI/TUI parity goal of `_join_channel`). Raises NomnomError on
    relay failure."""
    feed_key = _feed_key_from_token(feed.feed_token)
    host, _ = _parse_feed_url(feed.url)
    members = _relay_list_members(host, feed.feed_id, feed_key).get("members") or []
    feed.members_cache = members
    _persist_members_cache(feed)
    return members


def cmd_channel(_args) -> int:
    """Show your channel secret (to add devices) and the device roster."""
    feed = _the_channel()
    if feed is None:
        sys.stderr.write(f"error: {_no_channel_hint()}\n")
        return 1
    sys.stderr.write(
        "channel secret (paste on another device with `nomnom join`):\n",
    )
    print(feed.url)
    try:
        members = _refresh_channel_roster(feed)
    except NomnomError as e:
        sys.stderr.write(f"\nwarning: could not fetch devices: {e}\n")
        return 0
    sys.stderr.write(f"\ndevices ({len(members)}):\n")
    for m in members:
        marker = " *" if m.get("member_id") == feed.member_id else "  "
        fp = (
            _ik_fingerprint(m.get("identity_pubkey", ""))
            if m.get("identity_pubkey") else "(no key)"
        )
        sys.stderr.write(f"{marker} {m.get('name', '?'):<28} {fp}\n")
    return 0


def _join_channel(secret: str) -> "Feed":
    """Add this device to the channel named by `secret` (a feed URL).

    Performs the relay handshake, replaces any channel already stored on this
    device, persists feeds.json, and returns the stored Feed. Raises
    NomnomError on a malformed secret or a relay failure. Shared by `cmd_join`
    (CLI) and the TUI ChannelScreen so both behave identically.
    """
    host, feed_id = _parse_feed_url(secret)
    feed_key = _feed_key_from_token(feed_id)
    # Probe the channel exists + the secret is correct before publishing.
    meta = _relay_get_feed_meta(host, feed_id, feed_key)
    ident = _load_identity()
    member_id = secrets.token_hex(16)
    card = {
        "member_id": member_id,
        "identity_pubkey": ident["sig_pub"],
        "name": ident["name"],
    }
    _relay_put_member(host, feed_id, feed_key, member_id, card)
    roster = _relay_list_members(host, feed_id, feed_key)
    feed = Feed(
        name=_CHANNEL_NAME,
        feed_id=feed_id,
        feed_token=feed_id,
        url=_format_feed_url(host, feed_id),
        expires_at=int(meta.get("expires_at") or 0),
        joined_at=int(time.time()),
        member_id=member_id,
        members_cache=list(roster.get("members") or []),
    )
    cfg = _load_feeds_config()
    cfg["feeds"] = [feed]
    cfg["default"] = _CHANNEL_NAME
    _save_feeds_config(cfg)
    return feed


def cmd_join(args) -> int:
    """Add this device to your channel by pasting its secret.

    Doesn't require relay.json — the channel secret (the feed URL) is the
    credential for /feeds/:id/* endpoints. Replaces any channel already
    configured on this device, so re-pairing is just running this again.
    """
    # NomnomError propagates to the dispatcher, which prints + returns 1.
    feed = _join_channel(args.secret)
    sys.stderr.write(
        f"joined the channel ({len(feed.members_cache)} device(s)).\n",
    )
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
        f"this wipes identity, pinned identities, joined feeds, and relay "
        f"config. identity rotation invalidates every remote pin; rejoining "
        f"feeds requires a fresh URL from another member.\n",
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

    def handle_key(self, ch: int, stdscr=None) -> ScreenAction | Screen:  # pragma: no cover
        raise NotImplementedError

    def on_idle(self, stdscr) -> ScreenAction | Screen:  # pragma: no cover
        """Called when getch() times out (the screen set a non-blocking
        timeout). Screens that poll (e.g. the receiver) do their work here;
        the default is a no-op redraw."""
        return ScreenAction.CONTINUE


def show_help_modal(stdscr, lines: list[str]) -> None:  # pragma: no cover
    """Centered modal listing keybindings. Closes on any key.

    Forces stdscr.timeout(-1) (blocking) for the duration of its getch — the
    caller may have set a non-blocking timeout for a progress poll, which
    would otherwise close the modal in ~100ms before the user can read it.
    """
    body = [" nomnom keys ".center(40, "─"), *lines, "─" * 40, " press any key to close "]
    _draw_overlay(stdscr, body, min_w=40)
    _modal_wait_key(stdscr)


_TUI_ACTIVE = False

# When set (inside the curses TUI), `_tofu_check_feed_member` calls this
# instead of input() to prompt for first-contact trust. Installed via
# `_tui_tofu`; takes a member card dict and returns True to pin.
_TUI_TOFU_HANDLER = None


def _in_tui() -> bool:
    """True while the curses TUI (the picker loop or a Screen) owns the terminal.

    Helpers that would otherwise call `input()` or write raw stderr can
    branch on this to avoid corrupting the curses display.
    """
    return _TUI_ACTIVE


@contextlib.contextmanager
def _tui_tofu(stdscr):  # pragma: no cover - curses I/O
    """Install a curses TOFU prompt for the duration of a feed operation.

    Feed send/receive may surface a first-contact prompt; inside curses that
    must be a modal, not input(). Restores the previous handler on exit so
    nesting is safe.
    """
    global _TUI_TOFU_HANDLER
    prev = _TUI_TOFU_HANDLER
    _TUI_TOFU_HANDLER = lambda card: _tofu_modal(stdscr, card)
    try:
        yield
    finally:
        _TUI_TOFU_HANDLER = prev


def _tofu_modal(stdscr, card: dict) -> bool:  # pragma: no cover - curses I/O
    """Blocking y/N modal for a first-contact feed identity. Returns True to pin."""
    name = card.get("name") or "(no name)"
    fp = _ik_fingerprint(card.get("identity_pubkey") or "")
    body = [
        " first contact ".center(40, "─"),
        f" {name}",
        f"   fingerprint: {fp}",
        " verify out-of-band if it matters.",
        "─" * 40,
        " trust and pin this device?  [y/N] ",
    ]
    _draw_overlay(stdscr, body, min_w=40)
    return _modal_yn(stdscr, default=False)


def _drive_screens(stdscr, initial: Screen) -> None:  # pragma: no cover - curses I/O
    """Drive a stack of Screen instances on an existing `stdscr` until the
    stack empties (BACK past the root) or a screen returns QUIT.

    Runs without its own `curses.wrapper`, so `_run_picker_loop` can call it
    from inside its own wrapper for a "more" excursion (curses.wrapper cannot
    nest)."""
    curses.curs_set(0)
    stdscr.keypad(True)
    stack: list[Screen] = [initial]
    while stack:
        current = stack[-1]
        stdscr.erase()
        current.render(stdscr)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch == curses.KEY_RESIZE:
            continue
        if ch == -1:
            # Non-blocking getch timed out; the active screen set its own
            # poll interval and gets an idle tick to do work (e.g. the
            # receiver long-polls; a progress bar just redraws).
            result = current.on_idle(stdscr)
        elif ch == ord("?"):
            show_help_modal(stdscr, current.help_lines)
            continue
        else:
            result = current.handle_key(ch, stdscr)
        if isinstance(result, Screen):
            stack.append(result)
        elif result == ScreenAction.BACK:
            stack.pop()
        elif result == ScreenAction.QUIT:
            return


# Verbs the file picker can't reach on its own (Bundle/Send are the picker's
# `d` destinations; Commit/PR/Item are its `v` verb cycle). These live behind
# the picker's `m` "more" overlay.
_MORE_VERBS: tuple[str, ...] = ("Receive", "Channel", "Rebuild", "Extensions")

_MORE_DESCS: dict[str, str] = {
    "Receive":    "Watch your channel for incoming files",
    "Channel":    "Show the channel secret + devices; add this device",
    "Rebuild":    "Reconstruct a file tree from a bundle .txt",
    "Extensions": "Edit the text/binary/name/secret lists",
}


def _open_more_verb(name: str) -> Screen:
    """Map a `_MORE_VERBS` label to the Screen to drive."""
    if name == "Receive":
        return ReceiveScreen()
    if name == "Channel":
        return ChannelScreen()
    if name == "Rebuild":
        return RebuildScreen()
    if name == "Extensions":
        return ExtensionsScreen()
    raise AssertionError(
        f"unmapped more-menu verb: {name!r}",
    )  # every label in _MORE_VERBS is handled above


def _more_menu(stdscr) -> str | None:  # pragma: no cover - curses I/O
    """Centered overlay listing `_MORE_VERBS`. Returns the chosen label on
    Enter, or None on Esc/q. Modeled on `_prompt_item_id`."""
    cursor = 0
    try:
        stdscr.timeout(-1)
    except curses.error:
        pass
    while True:
        body = [" more ".center(50, "─"), ""]
        for i, verb in enumerate(_MORE_VERBS):
            marker = ">" if i == cursor else " "
            body.append(f" {marker} {verb:<12} {_MORE_DESCS[verb]}")
        body.extend(["", "─" * 50, "  j/k: move   enter: open   esc: back"])
        _draw_overlay(stdscr, body)
        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch in (27, 3, ord("q")):
            return None
        if ch in (curses.KEY_DOWN, ord("j")):
            cursor = (cursor + 1) % len(_MORE_VERBS)
        elif ch in (curses.KEY_UP, ord("k")):
            cursor = (cursor - 1) % len(_MORE_VERBS)
        elif ch in (10, 13):
            return _MORE_VERBS[cursor]


def _scan_to_nodes(root: Path) -> tuple[list[Node], list[ScanItem]]:
    """Scan `root` into picker nodes with the bundle defaults (gitignore
    honored, secrets skipped). Returns (nodes, file_items); callers treat an
    empty `file_items` as "nothing to pick". Shared by the bare-picker entry
    and the post-excursion rescan so their scan behavior can't drift."""
    gi = load_gitignore(root)
    items = scan_repo(root, gi, skip_secrets=True)
    file_items = [it for it in items if not it.is_dir]
    if not file_items:
        return [], []
    stats = collect_stats(root, items)
    return build_tree(items, stats=stats), file_items


def _emit_picker_result(stdscr, result: PickResult, root: Path):
    """Run the picker's chosen action (bundle emit or git verb) with stdout/
    stderr captured so curses keeps the screen. Returns (messages, error,
    banner): `messages` = captured status lines, `error` = non-empty on
    failure, `banner` = a one-line notice for the next picker render.

    stdout/stderr are redirected so the handlers' progress prints don't
    corrupt the display; `_tui_tofu` hosts any first-contact SEND prompt as a
    modal since those streams can't host `input()`."""
    repo_name = root.name
    if result.verb == Verb.BUNDLE and not result.selected:
        return [], "", "no files selected."

    messages: list[str] = []
    error = ""
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    rc = 0
    try:
        with _tui_tofu(stdscr), \
             contextlib.redirect_stdout(out_buf), \
             contextlib.redirect_stderr(err_buf):
            if result.verb == Verb.BUNDLE:
                rc, lines = _emit_bundle(
                    repo_name, root, sorted(result.selected),
                    result.include_tree, result.destination,
                )
                messages = list(lines)
            else:
                rc = _run_picker_verb(result, root, include_diff=False)
    except NomnomError as e:
        error = str(e)
    captured = (err_buf.getvalue() + out_buf.getvalue()).rstrip("\n")
    if captured:
        messages += captured.splitlines()
    if rc != 0 and not error and messages:
        error = messages[-1]
    banner = error or (messages[-1] if messages else "done.")
    return messages, error, banner


def _result_modal(stdscr, messages: list[str], error: str) -> None:  # pragma: no cover - curses I/O
    """Blocking modal showing an action's full (possibly multi-line) output.

    The picker's status row only fits one line, so failures and multi-line
    notices get the whole message here rather than a truncated one-liner."""
    title = " error " if error else " result "
    body = [title.center(50, "─"), *messages, "─" * 50, " press any key to continue "]
    _draw_overlay(stdscr, body)
    _modal_wait_key(stdscr)


def _run_picker_loop(stdscr, root: Path, nodes: list[Node]) -> None:  # pragma: no cover - curses I/O
    """Interactive home: run the file picker, emit on Enter, and loop back
    into the picker with the selection intact. `q` exits; `m` opens the "more"
    overlay and drives the chosen Screen, then returns to the picker."""
    destination = Destination.FILE
    include_tree = True
    verb = Verb.BUNDLE
    banner: str | None = None
    allow_git = _is_inside_git_repo(root)
    while True:
        result = _picker_ui(
            stdscr, nodes, root=root,
            initial_destination=destination, initial_include_tree=include_tree,
            allow_stdout=False, allow_git_verbs=allow_git,
            allow_more=True, banner=banner, initial_verb=verb,
        )
        if result is None:  # q → quit the app
            return
        if result is PICKER_MORE:
            choice = _more_menu(stdscr)
            if choice is not None:
                _drive_screens(stdscr, _open_more_verb(choice))
                # Rescan so the picker reflects filesystem/exclude changes the
                # excursion made: Receive/Rebuild write files into cwd;
                # Extensions can add secret/binary patterns that must drop
                # files from the next bundle. Keep the old nodes if the rescan
                # comes back empty (would otherwise busy-loop on an empty list).
                if choice in ("Receive", "Rebuild", "Extensions"):
                    fresh, files = _scan_to_nodes(root)
                    if files:
                        nodes = fresh
            banner = None
            # The next _picker_ui call re-asserts timeout/keypad/curs_set on
            # entry, so no terminal-state fixups are needed here.
            continue
        messages, error, banner = _emit_picker_result(stdscr, result, root)
        # Surface multi-line output / failures in full; the one-line banner
        # alone would truncate them to the last line.
        if error or len(messages) > 1:
            _result_modal(stdscr, messages or [banner], error)
        # Destination/tree/verb reset inside _picker_ui each call; carry them
        # forward so a second action doesn't silently revert to file/bundle.
        destination = result.destination
        include_tree = result.include_tree
        verb = result.verb


class ReceiveScreen(Screen):
    """Watch your channel for incoming posts, writing each to cwd.

    A background daemon thread streams new-slot notifications over SSE and
    drops them on a queue; `on_idle` (the curses thread) drains the queue and
    does the fetch/verify/write — keeping all curses + TOFU work on the main
    thread. If the relay has no /stream, it falls back to the cooperative
    long-poll. Keys stay responsive; Esc/q stops. First-contact prompts surface
    as a modal via `_tui_tofu`.
    """

    title = "Receive"
    help_lines = [
        "watching your channel; files land in the current directory",
        "esc / q     stop and return to the picker",
    ]
    _POLL_MS = 700

    def __init__(self) -> None:
        self.received: list[str] = []
        self.status = ""
        self.error = ""
        self.feed = _the_channel()
        self.host = ""
        self.feed_key = b""
        self.last_ts = 0
        self._queue: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._stream_failed = False  # relay lacks /stream → cooperative long-poll
        if self.feed is None:
            self.error = _no_channel_hint()
            return
        try:
            self.feed_key = _feed_key_from_token(self.feed.feed_token)
            self.host, _ = _parse_feed_url(self.feed.url)
        except NomnomError as e:
            self.error = str(e)
            self.feed = None
            return
        self.last_ts = self.feed.last_post_ts
        self.status = "watching your channel..."

    def render(self, stdscr) -> None:  # pragma: no cover - curses I/O
        theme = _setup_theme()
        try:
            stdscr.timeout(self._POLL_MS)
        except curses.error:
            pass
        h, w = stdscr.getmaxyx()
        try:
            stdscr.addstr(0, 0, f" {self.title} ".ljust(max(1, w - 1)),
                          theme["filter"])
        except curses.error:
            pass
        if self.error:
            for i, line in enumerate(_wrap(self.error, max(10, w - 4))):
                try:
                    stdscr.addstr(2 + i, 2, line, theme["dim"])
                except curses.error:
                    pass
        else:
            if not self.received:
                try:
                    stdscr.addstr(2, 2, "nothing received yet — waiting...",
                                  theme["dim"])
                except curses.error:
                    pass
            # Show the most recent arrivals that fit.
            visible = self.received[-(h - 5):] if h > 6 else self.received[-1:]
            for i, line in enumerate(visible):
                row = 2 + i
                if row >= h - 2:
                    break
                try:
                    stdscr.addstr(row, 2, line[: max(1, w - 3)], 0)
                except curses.error:
                    pass
            if self.status:
                try:
                    stdscr.addstr(h - 2, 2, self.status[: max(1, w - 3)],
                                  theme["dim"])
                except curses.error:
                    pass
        footer = "esc/q:stop  ?:help" if not self.error else "esc/q:back"
        try:
            stdscr.addstr(h - 1, 0, footer[: max(1, w - 1)], theme["dim"])
        except curses.error:
            pass

    def _stream_worker(self) -> None:  # pragma: no cover - background thread/IO
        """Stream new-slot notifications onto the queue. Network I/O only — all
        curses/TOFU/decrypt work stays on the main (curses) thread."""
        try:
            for entry in _relay_stream_feed_slots(
                self.host, self.feed.feed_id, self.feed_key,
                since_fn=lambda: self.last_ts, stop=self._stop,
            ):
                if self._stop.is_set():
                    break
                self._queue.put(("slot", entry))
        except _StreamUnsupported:
            self._queue.put(("unsupported", None))
        except NomnomError as e:
            self._queue.put(("error", str(e)))
        except Exception as e:  # keep the TUI alive on unexpected stream faults
            self._queue.put(("error", str(e)))

    def _ensure_stream(self) -> None:  # pragma: no cover - curses I/O
        if self._thread is None and not self._stop.is_set():
            self._thread = threading.Thread(target=self._stream_worker, daemon=True)
            self._thread.start()

    def _handle_slot(self, slot_id: str) -> None:  # pragma: no cover - curses I/O
        """Fetch/verify/write one slot. Caller wraps this in `_tui_tofu` +
        redirect_stderr so prompts modal correctly and narration stays off-screen."""
        # Updates self.feed.members_cache in place (TUI intentionally doesn't
        # persist per-slot; ReceiveScreen persists the cursor separately).
        _refresh_roster_with_tofu(
            self.feed, self.host, self.feed_key, trust_new=False,
        )
        try:
            res = _receive_one_post(
                feed=self.feed, host=self.host,
                feed_key=self.feed_key, slot_id=slot_id,
            )
        except (NomnomError, OSError) as e:
            self.status = f"dropped a post: {e}"
            return
        if res is not None:
            filename, nbytes, sender, out_path = res
            self.received.append(
                f"{filename} ({nbytes:,} bytes) from {sender} -> {out_path}",
            )
            self.status = f"received {filename!r}"

    def _persist_ts(self) -> None:  # pragma: no cover - curses I/O
        _receive_persist_ts(self.feed, self.last_ts)

    def _poll_once(self, stdscr) -> ScreenAction:  # pragma: no cover - curses I/O
        """Cooperative long-poll fallback when the relay has no /stream."""
        err_buf = io.StringIO()
        with _tui_tofu(stdscr), contextlib.redirect_stderr(err_buf):
            try:
                slots = _relay_list_feed_slots(
                    self.host, self.feed.feed_id, self.feed_key,
                    since_ts=self.last_ts, wait_ms=self._POLL_MS,
                )
            except NomnomError as e:
                self.status = f"relay error: {e}"
                return ScreenAction.CONTINUE
            for entry in slots:
                slot_id = entry.get("slot_id")
                created_at = int(entry.get("created_at") or 0)
                if slot_id:
                    self._handle_slot(slot_id)
                if created_at > self.last_ts:
                    self.last_ts = created_at
        self._persist_ts()
        return ScreenAction.CONTINUE

    def on_idle(self, stdscr) -> ScreenAction:  # pragma: no cover - curses I/O
        if self.feed is None:
            return ScreenAction.CONTINUE
        if self._stream_failed:
            return self._poll_once(stdscr)
        self._ensure_stream()
        # Drain whatever the stream thread queued since the last tick.
        items = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not items:
            return ScreenAction.CONTINUE
        # Redirect stderr: the feed helpers narrate to it, which would corrupt
        # curses. The TOFU modal draws via curses directly, so it's unaffected.
        err_buf = io.StringIO()
        with _tui_tofu(stdscr), contextlib.redirect_stderr(err_buf):
            for kind, payload in items:
                if kind == "unsupported":
                    self._stream_failed = True
                    self.status = "relay has no /stream; using long-poll"
                    continue
                if kind == "error":
                    self.status = f"stream error: {payload}"
                    continue
                entry = payload
                slot_id = entry.get("slot_id")
                created_at = int(entry.get("created_at") or 0)
                if slot_id and created_at > self.last_ts:
                    self._handle_slot(slot_id)
                if created_at > self.last_ts:
                    self.last_ts = created_at
        self._persist_ts()
        return ScreenAction.CONTINUE

    def handle_key(self, ch: int, stdscr=None):
        if ch in (ord("q"), 3, 27):
            self._stop.set()  # wind the stream thread down (daemon; exits on next read)
            if stdscr is not None:
                try:
                    stdscr.timeout(-1)
                except curses.error:
                    pass
            return ScreenAction.BACK
        return ScreenAction.CONTINUE


class RebuildScreen(Screen):
    """Three-step rebuild flow inside the TUI: enter path, preview, confirm.

    Step 1 ('input'): user types a bundle path. Enter parses it.
    Step 2 ('preview'): tree of files to write + target dir; Enter writes,
                        Esc returns to step 1.
    Step 3 ('done'):    write succeeded or failed; Esc back to the picker."""

    title = "Rebuild"
    help_lines = [
        "type a bundle path; enter to parse",
        "in preview: enter to write, esc to re-enter the path",
        "esc / q from input mode returns to the picker",
    ]

    def __init__(self) -> None:
        self.step = "input"
        self.path_buf = ""
        self.error = ""
        self.message = ""
        self._reset_parse_state()

    def _reset_parse_state(self) -> None:
        """Clear the parsed-bundle fields (shared by __init__ and the
        preview->input back path, so a new field can't be forgotten in one)."""
        self.bundle_text = ""
        self.repo_name = ""
        self.files: list[tuple[str, str]] = []
        self.target: Path | None = None

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
        written, err = _write_bundle_files(self.target, self.files)
        if err is not None:
            self.error = err
            self.step = "done"
            return
        try:
            shown = self.target.relative_to(Path.cwd())
        except ValueError:
            shown = self.target
        self.message = f"wrote {written} files into {shown}"
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
                              f"error: {self.error}" if self.error else self.message,
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
                self._reset_parse_state()
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
        "esc / q      back to the picker",
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


class ChannelScreen(Screen):
    """Show the channel secret + devices, and pair this device from the TUI.

    Closes the old bootstrap gap: when no channel exists, the screen opens
    straight into 'join' mode so a freshly-installed device can paste its
    secret without dropping to the shell. With a channel configured it shows
    the secret to copy, the device roster, and a re-pair / leave action.
    """

    title = "Channel"
    help_lines = [
        "c            copy the channel secret",
        "r            refresh the device list from the relay",
        "j            paste a secret to add (re-pair) this device",
        "l            leave the channel on this device",
        "esc / q      back to the picker",
    ]

    def __init__(self) -> None:
        self.feed = _the_channel()
        self.error = ""
        self.message = ""
        self.buf = ""
        # No channel yet → go straight to paste-a-secret so the TUI can
        # bootstrap a new device on its own.
        self.mode = "view" if self.feed is not None else "join"
        self.devices: list[dict] = (
            list(self.feed.members_cache) if self.feed else []
        )

    def render(self, stdscr) -> None:  # pragma: no cover - curses I/O
        theme = _setup_theme()
        h, w = stdscr.getmaxyx()
        try:
            stdscr.addstr(
                0, 0, f" {self.title} ".ljust(max(1, w - 1)), theme["filter"],
            )
        except curses.error:
            pass
        if self.mode == "join":
            self._render_join(stdscr, theme, h, w)
        else:
            self._render_view(stdscr, theme, h, w)
        if self.error:
            try:
                stdscr.addstr(
                    h - 3, 0, ("error: " + self.error)[: max(1, w - 1)],
                    theme["filter"],
                )
            except curses.error:
                pass
        if self.message:
            try:
                stdscr.addstr(
                    h - 2, 0, self.message[: max(1, w - 1)], theme["dim"],
                )
            except curses.error:
                pass
        footer = (
            "enter:add  esc:cancel"
            if self.mode == "join"
            else "c:copy  r:refresh  j:re-pair  l:leave  esc/q:back"
        )
        try:
            stdscr.addstr(h - 1, 0, footer[: max(1, w - 1)], theme["dim"])
        except curses.error:
            pass

    def _render_join(self, stdscr, theme, h, w):  # pragma: no cover
        prompt = "paste your channel secret, then Enter:"
        try:
            stdscr.addstr(2, 2, prompt[: max(1, w - 4)], theme["dim"])
            stdscr.addstr(4, 2, self.buf[-(w - 4):][: max(1, w - 4)], 0)
        except curses.error:
            pass

    def _render_view(self, stdscr, theme, h, w):  # pragma: no cover
        try:
            stdscr.addstr(2, 2, "secret (paste on another device):"[: w - 4],
                          theme["dim"])
            stdscr.addstr(3, 2, (self.feed.url if self.feed else "")[: w - 4], 0)
            stdscr.addstr(5, 2, f"devices ({len(self.devices)}):"[: w - 4], 0)
        except curses.error:
            pass
        for i, m in enumerate(self.devices):
            row = 6 + i
            if row >= h - 3:
                break
            name = m.get("name", "?")
            sig_pub = m.get("identity_pubkey", "")
            fp = _ik_fingerprint(sig_pub) if sig_pub else "?"
            mine = " (you)" if (
                self.feed and m.get("member_id") == self.feed.member_id
            ) else ""
            line = f"  {name:<22}  {fp}{mine}"
            try:
                stdscr.addstr(row, 0, line[: max(1, w - 1)], 0)
            except curses.error:
                pass

    def _handle_join_key(self, ch: int):
        if ch in (3, 27):  # Esc / Ctrl-C cancels
            self.buf = ""
            if self.feed is not None:
                self.mode = "view"
                return ScreenAction.CONTINUE
            return ScreenAction.BACK
        if ch in (10, 13):  # Enter submits
            secret = self.buf.strip()
            self.buf = ""
            if not secret:
                return ScreenAction.CONTINUE
            try:
                self.feed = _join_channel(secret)
            except NomnomError as e:
                self.error = str(e)
                return ScreenAction.CONTINUE
            self.devices = list(self.feed.members_cache)
            self.error = ""
            self.message = f"joined ({len(self.devices)} device(s))"
            self.mode = "view"
            return ScreenAction.CONTINUE
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            self.buf = self.buf[:-1]
            return ScreenAction.CONTINUE
        if 32 <= ch < 127:
            self.buf += chr(ch)
        return ScreenAction.CONTINUE

    def handle_key(self, ch: int, stdscr=None):
        if self.mode == "join":
            return self._handle_join_key(ch)
        if ch in (ord("q"), 3, 27):
            return ScreenAction.BACK
        if ch == ord("j"):
            self.mode = "join"
            self.buf = ""
            self.message = ""
            self.error = ""
        elif ch == ord("c") and self.feed is not None:
            if copy_to_clipboard(self.feed.url):
                self.message = "secret copied to clipboard"
            else:
                self.message = "no clipboard tool (pbcopy/wl-copy/xclip)"
        elif ch == ord("r") and self.feed is not None:
            try:
                self.devices = _refresh_channel_roster(self.feed)
                self.error = ""
                self.message = f"{len(self.devices)} device(s)"
            except NomnomError as e:
                self.error = str(e)
        elif ch == ord("l") and self.feed is not None:
            try:
                feed_key = _feed_key_from_token(self.feed.feed_token)
                host, _ = _parse_feed_url(self.feed.url)
                _relay_delete_member(
                    host, self.feed.feed_id, feed_key, self.feed.member_id,
                )
            except NomnomError as e:
                self.error = str(e)
                return ScreenAction.CONTINUE
            cfg = _load_feeds_config()
            cfg["feeds"] = []
            cfg["default"] = None
            _save_feeds_config(cfg)
            self.feed = None
            self.devices = []
            self.message = "left the channel; paste a secret to re-pair"
            self.mode = "join"
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
    width: int,
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
        stdscr.addstr(y + 1, x, line[:width], 0)
    except curses.error:
        pass


def _confirm_modal(
    stdscr, title: str, lines: list[str], default: bool = False,
) -> bool:  # pragma: no cover - curses I/O
    """Centered modal with a y/N (or Y/n) gate. Blocks on getch.

    Returns True for `y`/`Y`, False for `n`/`N`/Esc, and `default` on
    Enter. Useful for destructive confirmations from inside a Screen."""
    prompt = " [Y/n] " if default else " [y/N] "
    body = [title.center(50, "─"), *lines, "─" * 50, prompt]
    _draw_overlay(stdscr, body)
    return _modal_yn(stdscr, default=default)


def _draw_overlay(
    stdscr, body: list[str], min_w: int = 50,
) -> None:  # pragma: no cover - curses I/O
    h, w = stdscr.getmaxyx()
    box_w = min(max(1, w - 2), max(max(len(r) for r in body) + 4, min_w))
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
            stdscr.addstr(
                y0 + 1 + i, x0 + 2,
                line[: max(0, box_w - 4)], curses.A_REVERSE,
            )
        except curses.error:
            pass
    stdscr.refresh()


def _modal_wait_key(stdscr) -> int:  # pragma: no cover - curses I/O
    """Block (forcing timeout(-1)) until a real keypress and return it.

    The caller may have set a non-blocking timeout for a progress poll; force
    blocking so a modal doesn't dismiss itself before it can be read."""
    try:
        stdscr.timeout(-1)
    except curses.error:
        pass
    while True:
        ch = stdscr.getch()
        if ch != -1:
            return ch


def _modal_yn(stdscr, default: bool) -> bool:  # pragma: no cover - curses I/O
    """Block on a y/N gate: y/Y->True, n/N/q/Esc/Ctrl-C->False, Enter->default."""
    try:
        stdscr.timeout(-1)
    except curses.error:
        pass
    while True:
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), ord("q"), 27, 3):
            return False
        if ch in (10, 13):
            return default


def _prompt_item_id(
    stdscr, root: Path,
) -> tuple[str, str] | None:  # pragma: no cover - curses I/O
    """Centered overlay collecting an item id; auto-detect the kind.

    Returns (kind, ident) on Enter, None on Esc. Hex strings route to
    `commit`, non-numeric strings to `release`, pure numbers run a
    parallel /issues/{n} + /actions/runs/{n} probe to disambiguate
    pr / issue / run (shown as a picker on multiple matches).
    """
    buf = ""
    status = ""
    try:
        stdscr.timeout(-1)
    except curses.error:
        pass
    while True:
        title = " item id ".center(50, "─")
        prompt = f"  > {buf}_"
        body = [
            title, "", prompt, "",
            "  number / sha / tag — nomnom infers the kind",
            "  or `<kind> <id>` (e.g. `discussion 7`, `job 42`)",
        ]
        if status:
            body.append(f"  {status}")
        body.extend([
            "", "─" * 50, "  enter: confirm   esc: cancel",
        ])
        _draw_overlay(stdscr, body)
        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch in (27, 3):
            return None
        if ch in (10, 13):
            value = buf.strip()
            if not value:
                continue
            parts = value.split(maxsplit=1)
            if len(parts) == 2 and parts[0] in ITEM_KINDS:
                return (parts[0], parts[1])
            inferred = _classify_item_id(value)
            if inferred == "commit":
                return ("commit", value)
            if inferred == "release":
                return ("release", value)
            try:
                n = int(value)
            except ValueError:
                status = "invalid id"
                continue
            if n <= 0:
                status = "id must be positive"
                continue
            try:
                owner, name = _resolve_owner_repo(root)
            except NomnomError as e:
                status = str(e)
                continue
            status = "checking github…"
            _draw_overlay(stdscr, [
                title, "", prompt, "", f"  {status}",
                "", "─" * 50, "  enter: confirm   esc: cancel",
            ])
            matches = _probe_numeric_id(root, owner, name, n)
            if not matches:
                status = f"no pr/issue/run found for #{n}"
                continue
            if len(matches) == 1:
                return (matches[0][0], value)
            chosen = _pick_item_kind(stdscr, n, matches)
            if chosen is None:
                status = ""
                continue
            return (chosen, value)
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            buf = buf[:-1]
            status = ""
            continue
        if 32 <= ch < 127:
            buf += chr(ch)
            status = ""


def _pick_item_kind(
    stdscr, n: int, matches: list[tuple[str, str]],
) -> str | None:  # pragma: no cover - curses I/O
    """Show a picker for ambiguous numeric ids; return chosen kind or None."""
    try:
        stdscr.timeout(-1)
    except curses.error:
        pass
    sel = 0
    while True:
        title = " disambiguate ".center(60, "─")
        rows = [title, ""]
        for i, (kind, label) in enumerate(matches):
            mark = ">" if i == sel else " "
            row = f"  {mark} {i + 1}. {kind} #{n}: {label}"
            rows.append(row[:60])
        rows.extend([
            "", "─" * 60,
            "  1-9 / ↑↓ select   enter: confirm   esc: cancel",
        ])
        _draw_overlay(stdscr, rows, min_w=60)
        ch = stdscr.getch()
        if ch == -1:
            continue
        if ch in (27, 3):
            return None
        if ch in (10, 13):
            return matches[sel][0]
        if ord("1") <= ch <= ord("9"):
            idx = ch - ord("1")
            if idx < len(matches):
                return matches[idx][0]
        if ch in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(matches)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(matches)


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


def _add_repo_positional(sub: argparse.ArgumentParser, *, flag: bool = False) -> None:
    """Attach the shared repo argument: a `repo` positional, or `--repo` flag."""
    help_text = "Path to the project repo (default: current directory)."
    if flag:
        sub.add_argument("--repo", default=".", help=help_text)
    else:
        sub.add_argument("repo", nargs="?", default=".", help=help_text)


def _build_commit_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom commit",
        description=(
            "Bundle git context (status, diffs, recent commits) into a .txt "
            "for an LLM to draft a commit message."
        ),
    )
    _add_repo_positional(sub)
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
    _add_repo_positional(sub)
    _add_destination_flags(sub)
    sub.add_argument(
        "--base", default=None,
        help="Base branch to diff against (default: gh repo default branch).",
    )
    return sub


def _build_item_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom item",
        description=(
            "Bundle gh context for a GitHub item (pr / issue / discussion / "
            "commit / release / workflow run / job) into a .txt for an LLM. "
            "Pass a bare id and nomnom infers the kind; or pass `<kind> <id>` "
            "explicitly (required for run/discussion/job, since their ids "
            "live in separate namespaces from pr/issue numbers)."
        ),
    )
    sub.add_argument(
        "kind_or_id",
        help=(
            "Either a bare id (number / commit sha / tag) for auto-detect, "
            "or one of: " + ", ".join(ITEM_KINDS) + "."
        ),
    )
    sub.add_argument(
        "ident", nargs="?", default=None,
        help="Id when the first argument names a kind (e.g. `pr 123`).",
    )
    _add_repo_positional(sub, flag=True)
    sub.add_argument(
        "--diff", action="store_true",
        help=(
            "Include the full diff for pr/commit (off by default; inline "
            "review comments carry their own diff hunks)."
        ),
    )
    sub.add_argument(
        "--all-logs", action="store_true",
        help=(
            "For run/job: include all logs instead of only the failing "
            "step output."
        ),
    )
    _add_destination_flags(sub)
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
            "Send a file to every other device on your channel. The relay "
            "sees only ciphertext; the sender's identity is signed per post."
        ),
    )
    sub.add_argument("file", help="Path to the file to send.")
    sub.add_argument(
        "--trust-new", action="store_true",
        help=(
            "Auto-accept TOFU prompts on first sight of a device's identity. "
            "Scriptable but loses the explicit gate."
        ),
    )
    return sub


def _build_receive_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom receive",
        description=(
            "Watch your channel for new files and write each into the current "
            "directory. Default: keep listening until Ctrl-C. Pass --once to "
            "exit after the first received file."
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
            "Auto-accept TOFU prompts on first sight of a device's identity. "
            "Scriptable but loses the explicit gate."
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
            "Print the bare `host#secret` (relay HMAC, NOT redacted). Lets "
            "another owner device run `nomnom init` against the same relay; "
            "regular devices just need the channel secret from `nomnom channel`."
        ),
    )
    sp.add_parser("clear", help="Delete relay.json after confirmation.")
    return sub


def _build_join_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom join",
        description=(
            "Add this device to your channel by pasting its secret (the "
            "host/f/<token> URL from `nomnom init` or `nomnom channel`). The "
            "secret alone is the credential; no relay HMAC is needed here. "
            "Replaces any channel already configured on this device."
        ),
    )
    sub.add_argument("secret", help="Channel secret (host/f/<token>).")
    return sub


def _build_init_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom init",
        description=(
            "Set up the relay (first device) and create your one permanent "
            "channel. Run once, on the device that owns the relay. Prints the "
            "channel secret to paste on your other devices with `nomnom join`."
        ),
    )
    sub.add_argument(
        "--allow-private", action="store_true",
        help=(
            "Allow a relay URL that resolves to a private / loopback / "
            "link-local address (e.g., a local dev Worker)."
        ),
    )
    return sub


def _build_channel_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="nomnom channel",
        description=(
            "Show your channel secret (paste on another device with "
            "`nomnom join`) and the list of devices currently on it."
        ),
    )


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
    "item": (
        _build_item_parser,
        lambda a: cmd_item(
            a.repo, a.kind_or_id, a.ident,
            include_diff=a.diff,
            all_logs=a.all_logs,
            destination=_destination_from_args(a),
        ),
    ),
    "rebuild": (
        _build_rebuild_parser,
        lambda a: cmd_rebuild(a.bundle, a.name),
    ),
    "send": (
        _build_send_parser,
        lambda a: cmd_send(a.file, trust_new=a.trust_new),
    ),
    "receive": (
        _build_receive_parser,
        lambda a: cmd_receive(once=a.once, trust_new=a.trust_new),
    ),
    "init": (
        _build_init_parser,
        cmd_init,
    ),
    "join": (
        _build_join_parser,
        cmd_join,
    ),
    "channel": (
        _build_channel_parser,
        cmd_channel,
    ),
    "relay": (
        _build_relay_parser,
        cmd_relay,
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
) -> tuple[int, list[str]]:
    """Render the bundle and dispatch to the chosen destination.

    Returns (rc, status lines). The CLI prints the lines to stderr; the
    TUI surfaces them via the picker banner/result modal. STDOUT and SEND may still
    write to their own streams as a side effect of dispatching."""
    tree_str = render_ascii_tree(selected, repo_name) if include_tree else None
    output = render_output(repo_name, root, selected, tree_str)

    if destination == Destination.STDOUT:
        sys.stdout.write(output)
        sys.stdout.flush()
        return 0, [f"wrote {len(output):,} bytes to stdout."]

    if destination == Destination.SEND:
        return _emit_to_feed(output.encode("utf-8"), pick_output_path(repo_name).name)

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


_RETIRED_VERBS = {
    "pair": (
        "`nomnom pair` was retired. To add another device, run `nomnom init` "
        "on the first device and share the printed channel secret; then run "
        "`nomnom join <secret>` on the other device."
    ),
    "open": (
        "`nomnom open` was retired. nomnom now has one permanent channel: run "
        "`nomnom init` once on the first device, then `nomnom join <secret>` "
        "on the others."
    ),
    "feeds": (
        "`nomnom feeds` was retired. There's one channel now — use "
        "`nomnom channel` to show its secret and the devices on it."
    ),
    "encrypt": (
        "`nomnom encrypt` was renamed in v1.5 and removed. "
        "Use `nomnom send` (send to your channel)."
    ),
    "decrypt": (
        "`nomnom decrypt` was renamed in v1.5 and removed. "
        "Use `nomnom receive` (watch your channel)."
    ),
}


def _maybe_print_v2_migration_notice() -> None:
    """Print a one-shot heads-up when legacy pins exist but no feeds do.

    Detects v1/v2 pin records (no `sig_pub` field) and points the user at
    the new `init`/`join` flow. Best-effort — failure to print never blocks
    startup.
    """
    try:
        peers = _load_known_peers()
    except Exception:
        return
    legacy = [
        rec for rec in peers.values()
        if isinstance(rec, dict) and not rec.get("sig_pub")
    ]
    if not legacy:
        return
    cfg = _load_feeds_config()
    if cfg["feeds"]:
        return
    sys.stderr.write(
        f"nomnom: pair is retired. "
        f"{len(legacy)} legacy pin(s) preserved as identity records but no "
        "longer transport. run `nomnom init` or `nomnom join <secret>` to "
        "send/receive.\n",
    )


def _run_picker_verb(result: PickResult, root: Path, *, include_diff: bool) -> int:
    """Dispatch a non-BUNDLE picker verb (COMMIT/PR/ITEM) to its cmd_* handler.

    Shared by the bare-CLI path (`include_diff=True`) and the TUI picker loop
    (`include_diff=False`); both must stay in lock-step on how ITEM validates."""
    if result.verb == Verb.COMMIT:
        return cmd_commit(str(root), destination=result.destination)
    if result.verb == Verb.PR:
        return cmd_pr(str(root), None, destination=result.destination)
    if result.verb == Verb.ITEM:
        if result.item_kind is None or result.item_id is None:
            raise NomnomError("item verb selected without an id")
        return cmd_item(
            str(root), result.item_kind, result.item_id,
            include_diff=include_diff, destination=result.destination,
        )
    raise NomnomError(f"unsupported verb {result.verb}")


def main() -> int:
    # The notice helper loads the pin store, tripping any migration up front
    # so its message lands on stderr before either entry point claims the
    # terminal (curses or CLI).
    _maybe_print_v2_migration_notice()

    if len(sys.argv) >= 2 and sys.argv[1] in _RETIRED_VERBS:
        print(f"error: {_RETIRED_VERBS[sys.argv[1]]}", file=sys.stderr)
        return 2

    if len(sys.argv) >= 2 and sys.argv[1] in SUBCOMMANDS:
        return _dispatch_subcommand(sys.argv[1:])

    if len(sys.argv) == 1 and sys.stdin.isatty() and sys.stdout.isatty():
        # Interactive home: the file picker on the current directory. Scan
        # BEFORE curses so progress prints stay off the display and the
        # empty-guard can bail without ever entering the picker loop.
        root = Path.cwd()
        if not root.name:
            print(f"error: cannot derive a repo name from {root}", file=sys.stderr)
            return 1
        try:
            print(f"scanning {root} ...", file=sys.stderr)
            nodes, file_items = _scan_to_nodes(root)
        except NomnomError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if not file_items:
            # A picker with no file nodes returns immediately and would
            # busy-loop the driver; bail here instead. Git verbs live in the
            # picker's `v` cycle, so point at their CLI form when there's
            # nothing to pick in a git repo.
            print("no files found after applying excludes.", file=sys.stderr)
            if _is_inside_git_repo(root):
                print(
                    "  (for git context, run `nomnom commit` / `pr` / `item`.)",
                    file=sys.stderr,
                )
            return 0
        global _TUI_ACTIVE
        _TUI_ACTIVE = True
        try:
            curses.wrapper(_run_picker_loop, root, nodes)
        except KeyboardInterrupt:
            return 130
        except NomnomError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        finally:
            _TUI_ACTIVE = False
        return 0

    if "--copy" in sys.argv[1:]:
        return _refuse_copy_bare()

    parser = argparse.ArgumentParser(
        description="nomnom: feed your repo to the LLM, one .txt snack at a time.",
        epilog=(
            "subcommands: register / unregister edit the auto-managed "
            "extension lists; commit / pr / item bundle git context for "
            "an LLM; rebuild reconstructs a file tree from a bundle .txt; "
            "init sets up the relay and creates your one permanent channel "
            "(first device); join <secret> adds another device to it; "
            "channel shows the secret + devices; send / receive move files "
            "across your devices via the Cloudflare Worker relay you deploy "
            "yourself (receive listens until Ctrl-C); relay manages the relay "
            "config; peers manages pinned identities; reset wipes "
            "~/.config/nomnom/. run `nomnom <subcommand> --help`."
        ),
    )
    _add_repo_positional(parser)
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
                return _run_picker_verb(result, root, include_diff=True)
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
