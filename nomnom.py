#!/usr/bin/env python3
"""nomnom.py - feed your repo to the LLM, one .txt snack at a time.

Run: python3 nomnom.py [/path/to/repo]
Stdlib only. macOS/Linux. Python 3.8+.
"""

from __future__ import annotations

import argparse
import ast
import curses
import fnmatch
import hashlib
import json
import locale
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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
LARGE_FILE_BYTES = 1_000_000
BINARY_SNIFF_BYTES = 8192
CACHE_DIR_NAME = "nomnom"

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
    regex: "re.Pattern[str]"


def _glob_to_regex(pattern: str, anchored: bool) -> "re.Pattern[str]":
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
                out.append(pattern[i:j + 1])
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
        tokens = len(content) // 4
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
    nodes[idx].checked = value
    for c in nodes[idx].children:
        cascade_check(nodes, c, value)


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


def pick(nodes: list[Node]) -> set[str] | None:
    """Curses checkbox-tree picker. Returns set of checked file rels, or None on cancel."""
    if not nodes:
        return set()

    cancelled = False

    def _picker(stdscr) -> None:
        nonlocal cancelled
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

            if not visible:
                stdscr.erase()
                stdscr.addstr(0, 0, "(no matches)")
                stdscr.addstr(1, 0, f"/ {filter_buf}", curses.A_DIM)
                stdscr.refresh()
                ch = stdscr.getch()
                if filter_active or filter_buf:
                    filter_active, filter_buf = _apply_filter_key(
                        ch, filter_active, filter_buf
                    )
                    continue
                if ch in (ord("q"), 3):
                    cancelled = True
                    return
                continue

            if cursor_ni not in visible:
                cursor_ni = visible[0]
            cursor_pos = visible.index(cursor_ni)

            h, w = stdscr.getmaxyx()
            list_h = max(1, h - 2)
            if cursor_pos < viewport:
                viewport = cursor_pos
            elif cursor_pos >= viewport + list_h:
                viewport = cursor_pos - list_h + 1

            stdscr.erase()
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
                avail = w - 1
                for cand in stat_variants:
                    if len(prefix) + gap + len(cand) <= avail:
                        chosen_suffix = cand
                        break
                if chosen_suffix:
                    pad_len = avail - len(prefix) - len(chosen_suffix)
                    main = prefix + " " * pad_len
                else:
                    main = prefix[:avail]
                if n.checked:
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

            checked_count = sum(1 for n in nodes if n.checked and not n.is_dir)
            if filter_active or filter_buf:
                try:
                    stdscr.addstr(h - 2, 0, "/ ", theme["filter"])
                    stdscr.addstr(filter_buf[: max(0, w - 3)], theme["filter"])
                except curses.error:
                    pass
            else:
                info = f"selected: {checked_count} files"
                try:
                    stdscr.addstr(h - 2, 0, info[: w - 1], theme["dim"])
                except curses.error:
                    pass
            status = (
                "space:toggle  ->/<-:expand  /:filter  E/C:expand/collapse all  "
                "a:toggle-visible  s:sort  enter:done  q:quit"
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
                        else:
                            cascade_check(nodes, cursor_ni, not n.checked)
                continue

            if ch in (curses.KEY_DOWN, ord("j")):
                cursor_pos = min(cursor_pos + 1, len(visible) - 1)
                cursor_ni = visible[cursor_pos]
            elif ch in (curses.KEY_UP, ord("k")):
                cursor_pos = max(cursor_pos - 1, 0)
                cursor_ni = visible[cursor_pos]
            elif ch == curses.KEY_NPAGE:
                cursor_pos = min(cursor_pos + list_h, len(visible) - 1)
                cursor_ni = visible[cursor_pos]
            elif ch == curses.KEY_PPAGE:
                cursor_pos = max(cursor_pos - list_h, 0)
                cursor_ni = visible[cursor_pos]
            elif ch in (curses.KEY_HOME, ord("g")):
                cursor_pos = 0
                cursor_ni = visible[0]
            elif ch in (curses.KEY_END, ord("G")):
                cursor_pos = len(visible) - 1
                cursor_ni = visible[cursor_pos]
            elif ch == ord(" "):
                cascade_check(nodes, cursor_ni, not nodes[cursor_ni].checked)
            elif ch in (curses.KEY_RIGHT, ord("l")):
                if nodes[cursor_ni].is_dir:
                    nodes[cursor_ni].expanded = True
            elif ch in (curses.KEY_LEFT, ord("h")):
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
            elif ch == ord("a"):
                any_unchecked = any(not nodes[v].checked for v in visible)
                for v in visible:
                    cascade_check(nodes, v, any_unchecked)
            elif ch == ord("s"):
                sort_mode = "size" if sort_mode == "alpha" else "alpha"
            elif ch in (10, 13):
                return
            elif ch in (ord("q"), 3):
                cancelled = True
                return

    try:
        curses.wrapper(_picker)
    except KeyboardInterrupt:
        cancelled = True

    if cancelled:
        return None
    return {n.rel for n in nodes if n.checked and not n.is_dir}


# ---------- last selection ----------

def _cache_path_for(root: Path) -> Path:
    abs_root = str(root.resolve())
    digest = hashlib.sha1(abs_root.encode("utf-8")).hexdigest()
    return Path.home() / ".cache" / CACHE_DIR_NAME / f"{digest}.json"


def load_last_selection(root: Path) -> list[str] | None:
    p = _cache_path_for(root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("repo_path") != str(root.resolve()):
            return None
        sel = data.get("selected")
        if isinstance(sel, list) and all(isinstance(x, str) for x in sel):
            return sel
    except (OSError, json.JSONDecodeError):
        pass
    return None


def save_last_selection(root: Path, selected: list[str]) -> None:
    try:
        p = _cache_path_for(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"repo_path": str(root.resolve()), "selected": sorted(selected)},
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


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


def pick_output_path(repo_name: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = Path.cwd() / f"{repo_name}-{ts}.txt"
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = base.with_name(f"{repo_name}-{ts}-{n}.txt")
        if not candidate.exists():
            return candidate
        n += 1


def _slug(s: str) -> str:
    """Make a branch name safe for use in a filename. `/` becomes `__` so
    `feat/foo` stays distinguishable from a literal `feat-foo` branch."""
    s = s.replace("/", "__")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "branch"


def pick_git_output_path(repo_name: str, branch_label: str, kind: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"{_slug(repo_name)}-{_slug(branch_label)}-{kind}-{ts}"
    base = Path.cwd() / f"{stem}.txt"
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = base.with_name(f"{stem}-{n}.txt")
        if not candidate.exists():
            return candidate
        n += 1


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
    base = cwd / name
    if not base.exists():
        return base
    n = 1
    while True:
        candidate = cwd / f"{name}-{n}"
        if not candidate.exists():
            return candidate
        n += 1


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


def _require_git_repo(root: Path) -> None:
    rc, _, _ = _run(["git", "rev-parse", "--show-toplevel"], root)
    if rc != 0:
        print(f"error: not a git repository: {root}", file=sys.stderr)
        sys.exit(1)


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
        print(
            "error: pr requires gh (https://cli.github.com). "
            "install it and retry.",
            file=sys.stderr,
        )
        sys.exit(1)


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
    copy: bool,
) -> int:
    output = render_git_bundle(repo_name, kind, branch_label, sections, tree)
    size = len(output)
    print()
    print(f"  sections: {len(sections)}")
    print(f"  size:     {size:,} bytes")

    if copy:
        clip = detect_clipboard_cmd()
        if clip:
            print(f"  output:   clipboard via {clip[0]}")
        else:
            print("  output:   clipboard (no tool found; will fall back to file)")
    else:
        out_path = pick_git_output_path(repo_name, branch_label, kind)
        print(f"  output:   {out_path}")
    if size > GIT_BUNDLE_WARN_BYTES:
        print(f"  warn:     bundle exceeds {GIT_BUNDLE_WARN_BYTES:,} bytes")
    print()

    if copy:
        if copy_to_clipboard(output):
            print(f"copied {size:,} bytes to clipboard.")
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
    print(f"wrote {out_path}")
    return 0


# ---------- prompts ----------

def confirm(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


# ---------- self-editing register / unregister ----------

KIND_TO_NAME = {
    "text":   "TEXT_EXTENSIONS",
    "binary": "BINARY_EXTENSIONS",
    "name":   "KNOWN_TEXT_NAMES",
    "secret": "SECRET_PATTERNS",
}
KIND_IS_LIST = {"secret"}
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
    for kind, name in KIND_TO_NAME.items():
        v = values.get(name)
        if v is None:
            continue
        if not first:
            parts.append("\n")
        first = False
        if kind in KIND_IS_LIST:
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


def cmd_register(
    kind: str,
    values: list[str],
    remove: bool = False,
    path: Path | None = None,
) -> int:
    p = path if path is not None else SELF_PATH
    src_lines, start, end = _read_block(p)
    parsed = _parse_block(src_lines, start, end)

    target_name = KIND_TO_NAME[kind]
    target = parsed[target_name]
    is_list = kind in KIND_IS_LIST

    if not remove:
        conflicts: list[tuple[str, str, str]] = []
        for value in values:
            for other_kind, other_name in KIND_TO_NAME.items():
                if other_name == target_name:
                    continue
                if value in parsed.get(other_name, ()):
                    conflicts.append((value, other_kind, other_name))
        if conflicts:
            for value, other_kind, other_name in conflicts:
                print(
                    f"error: {value!r} is already in {other_name}. "
                    f"run `nomnom unregister {other_kind} {value}` first.",
                    file=sys.stderr,
                )
            return 1

    changes: list[tuple[str, str]] = []
    for value in values:
        if remove:
            if value not in target:
                print(f"! {value!r} not in {target_name} (no change)")
                continue
            if is_list:
                target.remove(value)
            else:
                target.discard(value)
            changes.append(("-", value))
        else:
            if value in target:
                print(f"! {value!r} already in {target_name} (no change)")
                continue
            if is_list:
                target.append(value)
            else:
                target.add(value)
            changes.append(("+", value))

    if not changes:
        return 0

    parsed[target_name] = target
    new_block = _emit_block(parsed)
    _write_block(p, src_lines, start, end, new_block)
    for sign, value in changes:
        verb = "registered" if sign == "+" else "unregistered"
        print(f"{sign} {verb} {value!r} in {target_name}")
    print(f"\ndone. review with: git diff {p.name}")
    return 0


def cmd_commit(repo: str, copy: bool) -> int:
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 1
    if not root.name:
        print(f"error: cannot derive a repo name from {root}", file=sys.stderr)
        return 1
    repo_name = root.name
    _require_git_repo(root)

    _, staged_diff, _ = _run(["git", "diff", "--staged"], root)
    _, unstaged_diff, _ = _run(["git", "diff"], root)
    if not staged_diff.strip() and not unstaged_diff.strip():
        print(
            "error: nothing to commit (no staged or unstaged changes).",
            file=sys.stderr,
        )
        return 1

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
        repo_name, "commit", branch_label, sections, tree, copy,
    )


def cmd_pr(repo: str, copy: bool, base: str | None) -> int:
    _require_gh()
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 1
    if not root.name:
        print(f"error: cannot derive a repo name from {root}", file=sys.stderr)
        return 1
    repo_name = root.name
    _require_git_repo(root)

    branch = _current_branch(root)
    if branch is None:
        print(
            "error: HEAD is detached; check out a branch before running pr.",
            file=sys.stderr,
        )
        return 1

    if base is None:
        base = _default_base_branch(root)

    rc, log_out, log_err = _run(["git", "log", f"{base}...HEAD"], root)
    if rc != 0:
        print(
            f"error: git log {base}...HEAD failed: {log_err.strip()}",
            file=sys.stderr,
        )
        return 1
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
        repo_name, "pr", branch, sections, tree, copy,
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
    repo: str, pr_number: int, copy: bool, include_diff: bool,
) -> int:
    _require_gh()
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 1
    if not root.name:
        print(f"error: cannot derive a repo name from {root}", file=sys.stderr)
        return 1
    repo_name = root.name
    _require_git_repo(root)

    if pr_number <= 0:
        print(
            f"error: pr number must be positive, got {pr_number}",
            file=sys.stderr,
        )
        return 1

    rc, owner_repo, err = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner",
         "-q", ".nameWithOwner"],
        root,
    )
    owner_repo = owner_repo.strip()
    if rc != 0 or "/" not in owner_repo:
        print(
            f"error: could not resolve gh repo: "
            f"{err.strip() or owner_repo or 'unknown error'}",
            file=sys.stderr,
        )
        return 1
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
        print(
            f"error: gh pr view #{pr_number} failed: "
            f"{err.strip() or 'unknown error'}",
            file=sys.stderr,
        )
        return 1
    try:
        pr = json.loads(pr_view) if pr_view.strip() else {}
    except json.JSONDecodeError as e:
        print(f"error: gh pr view returned invalid json: {e}", file=sys.stderr)
        return 1

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
        repo_name, "review", branch_label, sections, tree, copy,
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
    import hmac
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


def encrypt_bytes(
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
    import hmac
    import secrets as _secrets
    if not passphrase:
        raise ValueError("passphrase must not be empty")
    salt = _salt if _salt is not None else _secrets.token_bytes(_NMNM_SALT_LEN)
    nonce = _nonce if _nonce is not None else _secrets.token_bytes(_NMNM_NONCE_LEN)
    enc_key, mac_key = _derive_keys(passphrase, salt)
    payload = _pack_payload(name, data)
    ciphertext = _stream_xor(enc_key, nonce, payload)
    mac_input = _NMNM_MAGIC + salt + nonce + ciphertext
    mac = hmac.new(mac_key, mac_input, hashlib.sha256).digest()
    return _NMNM_MAGIC + salt + nonce + mac + ciphertext


def decrypt_bytes(blob: bytes, passphrase: str) -> tuple[str, bytes]:
    """Verify and decrypt `blob` produced by `encrypt_bytes`.

    Returns (original_name, original_bytes). Raises ValueError on any
    structural problem, wrong passphrase, or tampering.
    """
    import hmac
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
    base = parent / name
    if not base.exists():
        return base
    stem = base.stem
    suffix = base.suffix
    n = 1
    while True:
        candidate = parent / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


# ---------- LAN transfer ----------
#
# Move a file between two machines on the same Wi-Fi, encrypted, with no file
# written to disk on the sending side and zero-config discovery. There is no
# pairing step: you run `encrypt`/`decrypt`, pick a peer from the discovered
# list, and the transfer happens. Trust is trust-on-first-use (TOFU), like
# SSH's known_hosts. Stdlib-only:
#   * each machine has a stable random device id + a long-term Diffie-Hellman
#     identity keypair (RFC 3526 2048-bit MODP group, plain big-ints) in
#     identity.json;
#   * the first time we transfer with a peer we record (pin) its identity
#     public key in known_peers.json. On later transfers a changed key is the
#     man-in-the-middle signature: we warn and ask before continuing;
#   * each transfer derives a fresh session key with a triple Diffie-Hellman
#     (each side contributes a throwaway ephemeral key plus its pinned identity
#     key), giving forward secrecy and authenticating the exchange against the
#     pinned identities — a MITM lacking the pinned private key cannot produce
#     a key that decrypts;
#   * a UDP limited-broadcast beacon (255.255.255.255, which routers do not
#     forward, so it stays on the local link) advertises a machine's device id,
#     name, role, http endpoint, and its identity + session-ephemeral pubkeys;
#   * `encrypt`/`decrypt` discover peers, you pick one from a list, and the
#     blob is encrypted under the session key via encrypt_bytes. Whoever runs
#     first hosts; the other joins and picks. Only ciphertext crosses the wire.

_LAN_BEACON_PORT = 48222
_LAN_BEACON_MAGIC = b"NMNMLAN2"
_LAN_BROADCAST_ADDR = "255.255.255.255"
_LAN_BEACON_INTERVAL = 1.0
_LAN_MAX_UPLOAD = 256 * 1024 * 1024  # cap on pushed ciphertext (256 MiB)
_LAN_DISCOVER_TIMEOUT = 4.0          # default discovery window, seconds
_LAN_DEVICE_HEADER = "X-Nomnom-Device"   # joiner's device id
_LAN_NAME_HEADER = "X-Nomnom-Name"       # joiner's display name
_LAN_IK_HEADER = "X-Nomnom-Ik"           # joiner's identity public key (hex)
_LAN_EK_HEADER = "X-Nomnom-Ek"           # joiner's session ephemeral pub (hex)


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
    import secrets as _secrets
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
        ident["device_id"] = _secrets.token_hex(8)
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
            path.write_text(json.dumps(ident), encoding="utf-8")
        except OSError:
            pass
    return ident


def _known_peers_path() -> Path:
    return _nomnom_config_dir() / "known_peers.json"


def _load_known_peers() -> dict:
    try:
        data = json.loads(_known_peers_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_known_peer(device_id: str, name: str, ik_pub_hex: str) -> None:
    """Pin (or re-pin) a peer's identity public key."""
    peers = _load_known_peers()
    existing = peers.get(device_id)
    rec = existing if isinstance(existing, dict) else {}
    rec["name"] = name
    rec["ik_pub"] = ik_pub_hex
    rec.setdefault("first_seen", int(time.time()))
    peers[device_id] = rec
    _known_peers_path().write_text(json.dumps(peers, indent=2),
                                   encoding="utf-8")


def _known_peer_ik(device_id: str) -> str | None:
    """Return the pinned identity public key (hex) for a peer, or None."""
    rec = _load_known_peers().get(device_id)
    if not isinstance(rec, dict):
        return None
    ik = rec.get("ik_pub")
    return ik if isinstance(ik, str) else None


def _forget_peer(needle: str) -> list:
    """Drop pins matching `needle` (a device id or name). Returns dropped names."""
    peers = _load_known_peers()
    dropped = []
    for dev_id in list(peers.keys()):
        rec = peers[dev_id]
        name = rec.get("name", "") if isinstance(rec, dict) else ""
        if needle == dev_id or needle == name:
            dropped.append(name or dev_id)
            del peers[dev_id]
    if dropped:
        _known_peers_path().write_text(json.dumps(peers, indent=2),
                                       encoding="utf-8")
    return dropped


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
    import secrets as _secrets
    priv = _secrets.randbelow(_DH_P - 3) + 2
    return priv, pow(_DH_G, priv, _DH_P)


def _dh_pub_bytes(pub: int) -> bytes:
    return pub.to_bytes(_DH_BYTES, "big")


def _dh_shared(priv: int, peer_pub: int) -> bytes:
    if not 2 <= peer_pub <= _DH_P - 2:
        raise ValueError("invalid DH public value")
    return pow(peer_pub, priv, _DH_P).to_bytes(_DH_BYTES, "big")


def _session_key(*, ik_init_pub: int, ek_init_pub: int,
                 ik_resp_pub: int, ek_resp_pub: int,
                 dh1: bytes, dh2: bytes, dh3: bytes) -> bytes:
    """Derive a transfer session key from a triple Diffie-Hellman exchange.

    The "initiator" is the joiner that picked a peer and connected; the
    "responder" is the host. The three DH terms bind both long-term identity
    keys and both throwaway ephemerals:
      dh1 = DH(initiator identity,  responder ephemeral)
      dh2 = DH(initiator ephemeral, responder identity)
      dh3 = DH(initiator ephemeral, responder ephemeral)   # forward secrecy
    Both sides hash the same transcript (all four public keys, then the three
    shared secrets in fixed order) so they arrive at an identical key.
    """
    h = hashlib.sha256()
    h.update(b"nomnom-session-v1")
    for part in (_dh_pub_bytes(ik_init_pub), _dh_pub_bytes(ek_init_pub),
                 _dh_pub_bytes(ik_resp_pub), _dh_pub_bytes(ek_resp_pub),
                 dh1, dh2, dh3):
        h.update(part)
    return h.digest()


def _session_key_initiator(ik_init_priv: int, ek_init_priv: int,
                           ik_init_pub: int, ek_init_pub: int,
                           ik_resp_pub: int, ek_resp_pub: int) -> bytes:
    """Compute the session key from the joiner (initiator) side."""
    return _session_key(
        ik_init_pub=ik_init_pub, ek_init_pub=ek_init_pub,
        ik_resp_pub=ik_resp_pub, ek_resp_pub=ek_resp_pub,
        dh1=_dh_shared(ik_init_priv, ek_resp_pub),
        dh2=_dh_shared(ek_init_priv, ik_resp_pub),
        dh3=_dh_shared(ek_init_priv, ek_resp_pub),
    )


def _session_key_responder(ik_resp_priv: int, ek_resp_priv: int,
                           ik_resp_pub: int, ek_resp_pub: int,
                           ik_init_pub: int, ek_init_pub: int) -> bytes:
    """Compute the session key from the host (responder) side."""
    return _session_key(
        ik_init_pub=ik_init_pub, ek_init_pub=ek_init_pub,
        ik_resp_pub=ik_resp_pub, ek_resp_pub=ek_resp_pub,
        dh1=_dh_shared(ek_resp_priv, ik_init_pub),
        dh2=_dh_shared(ik_resp_priv, ek_init_pub),
        dh3=_dh_shared(ek_resp_priv, ek_init_pub),
    )


def _lan_encode_beacon(device_id: str, name: str, ip: str, port: int,
                       role: str, token: str, ik: str, ek: str) -> bytes:
    body = json.dumps(
        {"id": device_id, "name": name, "ip": ip, "port": port,
         "role": role, "tok": token, "ik": ik, "ek": ek},
        ensure_ascii=False,
    ).encode("utf-8")
    return _LAN_BEACON_MAGIC + body


def _lan_decode_beacon(packet: bytes) -> dict | None:
    """Decode a beacon packet, or return None on any malformation.

    Must never raise: the discovery socket sees arbitrary UDP noise. `ik` is
    the host's identity public key and `ek` its per-session ephemeral pub
    (both hex); together they let a joiner check the TOFU pin before connecting
    and derive the session key.
    """
    if not packet.startswith(_LAN_BEACON_MAGIC):
        return None
    try:
        info = json.loads(packet[len(_LAN_BEACON_MAGIC):].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(info, dict):
        return None
    try:
        out = {
            "id": str(info["id"]),
            "name": str(info["name"]),
            "ip": str(info["ip"]),
            "port": int(info["port"]),
            "role": str(info["role"]),
            "tok": str(info["tok"]),
            "ik": str(info["ik"]),
            "ek": str(info["ek"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if out["role"] not in ("send", "recv"):
        return None
    return out


def _lan_local_ip() -> str:
    """Best-effort LAN IPv4 of the outbound interface (no packets sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def _lan_beacon_sender(stop: threading.Event, device_id: str, name: str,
                       ip: str, port: int, role: str, token: str,
                       ik: str, ek: str,
                       interval: float = _LAN_BEACON_INTERVAL) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    packet = _lan_encode_beacon(device_id, name, ip, port, role, token, ik, ek)
    try:
        while not stop.is_set():
            try:
                sock.sendto(packet, (_LAN_BROADCAST_ADDR, _LAN_BEACON_PORT))
            except OSError:
                pass  # interface flap; keep trying
            stop.wait(interval)
    finally:
        sock.close()


def _lan_listen_for_beacons(timeout: float, role: str | None = None,
                            exclude_id: str | None = None,
                            bind_host: str = "") -> list[dict]:
    """Collect unique beacons (deduped by device id) until `timeout` elapses.

    Filters by `role` if given and drops `exclude_id` (this machine's own id).
    `bind_host` is a test seam (use "127.0.0.1" for loopback).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    sock.bind((bind_host, _LAN_BEACON_PORT))
    sock.settimeout(0.5)
    seen: dict = {}
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                packet, _addr = sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            info = _lan_decode_beacon(packet)
            if info is None:
                continue
            if role is not None and info["role"] != role:
                continue
            if exclude_id is not None and info["id"] == exclude_id:
                continue
            seen[info["id"]] = info
    finally:
        sock.close()
    return list(seen.values())


def _handler_session(handler, make_session):
    """Read the joiner's identity headers and run the TOFU + triple-DH step.

    Returns (peer_id, name, session_key) on success, or None after sending the
    appropriate HTTP error (the joiner is rejected: unknown headers -> 400,
    refused by TOFU / bad keys -> 403).
    """
    peer_id = handler.headers.get(_LAN_DEVICE_HEADER, "")
    name = handler.headers.get(_LAN_NAME_HEADER, "") or peer_id
    ik_hex = handler.headers.get(_LAN_IK_HEADER, "")
    ek_hex = handler.headers.get(_LAN_EK_HEADER, "")
    if not peer_id or not ik_hex or not ek_hex:
        handler.send_error(400, "missing identity headers")
        return None
    key = make_session(peer_id, name, ik_hex, ek_hex)
    if key is None:
        handler.send_error(403, "rejected (no matching identity)")
        return None
    return peer_id, name, key


def _lan_make_pull_handler(make_session, make_blob, token: str,
                           state: dict) -> type:
    """Serve a blob encrypted under a freshly derived session key.

    `make_session(id, name, ik, ek)` runs TOFU + triple-DH and returns the
    session key (or None to reject). `make_blob(session_key)` returns the
    ciphertext to send.
    """
    import http.server

    class _PullHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != "/" + token:
                self.send_error(404)
                return
            got = _handler_session(self, make_session)
            if got is None:
                return
            _peer_id, _name, key = got
            blob = make_blob(key)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return  # dropped/partial fetch; let the receiver retry
            state["done"] = True
            state["peer"] = self.client_address[0]

    return _PullHandler


def _lan_make_push_handler(make_session, state: dict, token: str,
                           max_bytes: int, lock: threading.Lock) -> type:
    """Accept one uploaded blob, decrypting it under a derived session key.

    `make_session(id, name, ik, ek)` runs TOFU + triple-DH and returns the
    session key (or None to reject). The raw ciphertext body and the key land
    in `state` for the main thread to decrypt and write.
    """
    import http.server

    class _PushHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _recv(self):
            if self.path != "/" + token:
                self.send_error(404)
                return
            got = _handler_session(self, make_session)
            if got is None:
                return
            _peer_id, _name, key = got
            raw = self.headers.get("Content-Length")
            if raw is None:
                self.send_error(400, "missing Content-Length")
                return
            try:
                length = int(raw)
            except ValueError:
                self.send_error(400, "bad Content-Length")
                return
            if length > max_bytes:
                self.send_error(413, "payload too large")
                return
            try:
                body = self.rfile.read(length)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            if len(body) != length:
                self.send_error(400, "short read")
                return
            with lock:
                state["blob"] = body
                state["session_key"] = key
                state["peer"] = self.client_address[0]
                state["done"] = True
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = _recv
        do_PUT = _recv

    return _PushHandler


def _lan_serve_one(handler_cls: type, bind_host: str, state: dict,
                   stop: threading.Event, on_listen=None,
                   deadline: float | None = None) -> None:
    """Run a one-shot HTTP server until a transfer completes or we stop.

    Binds to an OS-assigned free port. `on_listen(port)` fires once the
    socket is bound so the caller can start advertising the real port.
    """
    import http.server
    server = http.server.ThreadingHTTPServer((bind_host, 0), handler_cls)
    server.timeout = 0.5
    if on_listen is not None:
        on_listen(server.server_address[1])
    try:
        while not state.get("done") and not stop.is_set():
            if deadline is not None and time.monotonic() > deadline:
                break
            server.handle_request()
    finally:
        server.server_close()


def _lan_identity_headers(identity: dict, ek_pub_hex: str) -> dict:
    """Headers the joiner sends so the host can run TOFU + triple-DH."""
    return {
        _LAN_DEVICE_HEADER: identity["device_id"],
        _LAN_NAME_HEADER: identity["name"],
        _LAN_IK_HEADER: identity["ik_pub"],
        _LAN_EK_HEADER: ek_pub_hex,
    }


def _lan_fetch_blob(ip: str, port: int, token: str, identity: dict,
                    ek_pub_hex: str, timeout: float = 30.0) -> bytes:
    import urllib.error
    import urllib.request
    url = f"http://{ip}:{port}/{token}"
    req = urllib.request.Request(
        url, headers=_lan_identity_headers(identity, ek_pub_hex))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise ConnectionError("the other side declined your identity") from e
        raise ConnectionError(f"fetch rejected (HTTP {e.code})") from e
    except urllib.error.URLError as e:
        raise ConnectionError(str(getattr(e, "reason", e))) from e
    except OSError as e:
        raise ConnectionError(str(e)) from e


def _lan_upload_blob(ip: str, port: int, token: str, blob: bytes,
                     identity: dict, ek_pub_hex: str,
                     timeout: float = 30.0) -> None:
    import urllib.error
    import urllib.request
    url = f"http://{ip}:{port}/{token}"
    headers = _lan_identity_headers(identity, ek_pub_hex)
    headers["Content-Type"] = "application/octet-stream"
    headers["Content-Length"] = str(len(blob))
    req = urllib.request.Request(url, data=blob, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise ConnectionError(f"upload rejected (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        if e.code == 413:
            raise ConnectionError("file too large for the receiver") from e
        if e.code == 403:
            raise ConnectionError("the other side declined your identity") from e
        raise ConnectionError(f"upload rejected (HTTP {e.code})") from e
    except urllib.error.URLError as e:
        raise ConnectionError(str(getattr(e, "reason", e))) from e
    except OSError as e:
        raise ConnectionError(str(e)) from e


def _lan_random_token() -> str:
    import secrets as _secrets
    return _secrets.token_urlsafe(8)


def _lan_warn_firewall() -> None:
    if sys.platform == "darwin":
        print("note: macOS may ask to allow incoming connections — click Allow",
              file=sys.stderr)


def _lan_discover(role: str, *, discover_timeout: float = _LAN_DISCOVER_TIMEOUT,
                  bind_host: str = "") -> list:
    """Discover peers advertising `role`, excluding this machine."""
    identity = _load_identity()
    return _lan_listen_for_beacons(discover_timeout, role=role,
                                   exclude_id=identity["device_id"],
                                   bind_host=bind_host)


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


def _tofu_confirm_change(name: str, old_hex, new_hex: str) -> bool:
    """Warn that a known peer's identity key changed and ask to continue."""
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


def _tofu_check_join(peer: dict) -> bool:
    """Joiner-side TOFU gate: prompt if the picked peer's key changed."""
    if _tofu_decision(peer["id"], peer["ik"]) == "changed":
        return _tofu_confirm_change(peer["name"], _known_peer_ik(peer["id"]),
                                    peer["ik"])
    return True


def _lan_choose(found: list, what: str) -> dict | None:
    """Print a numbered list of `found` peers and let the user pick one.

    Each peer is tagged from its TOFU status: a brand-new device, a known one,
    or one whose identity key has changed (a warning).
    """
    found = sorted(found, key=lambda b: b["name"])
    print(f"found {len(found)} {what}:", file=sys.stderr)
    for i, b in enumerate(found, 1):
        decision = _tofu_decision(b["id"], b["ik"])
        if decision == "new":
            tag = "new device"
        elif decision == "match":
            tag = "known"
        else:
            tag = "WARNING: identity key changed"
        print(f"  {i}) {b['name']}  ({b['ip']}, {tag})", file=sys.stderr)
    try:
        raw = input(f"pick [1-{len(found)}]: ").strip()
    except EOFError:
        print("error: nothing picked", file=sys.stderr)
        return None
    try:
        idx = int(raw) if raw else 1
        if not 1 <= idx <= len(found):
            raise ValueError
    except ValueError:
        print(f"error: invalid choice {raw!r}", file=sys.stderr)
        return None
    return found[idx - 1]


def _make_responder_session(identity: dict, ek_priv: int, ek_pub: int,
                            state: dict, lock: threading.Lock):
    """Build the host-side `make_session(id, name, ik, ek)` callback.

    It runs TOFU on the joiner's identity key (prompting under `lock` if the
    key changed), derives the triple-DH session key, and records the peer in
    `state` for the main thread to pin after a successful transfer. Returns the
    session key, or None to reject the joiner (HTTP 403).
    """
    ik_priv = int(identity["ik_priv"], 16)
    ik_pub = int(identity["ik_pub"], 16)

    def make_session(peer_id, name, ik_hex, ek_hex):
        try:
            peer_ik = int(ik_hex, 16)
            peer_ek = int(ek_hex, 16)
        except (ValueError, TypeError):
            return None
        decision = _tofu_decision(peer_id, ik_hex)
        if decision == "changed":
            with lock:
                if _known_peer_ik(peer_id) != ik_hex and not _tofu_confirm_change(
                        name, _known_peer_ik(peer_id), ik_hex):
                    return None
        try:
            key = _session_key_responder(ik_priv, ek_priv, ik_pub, ek_pub,
                                         peer_ik, peer_ek)
        except ValueError:
            return None
        state["peer_id"] = peer_id
        state["peer_name"] = name
        state["peer_ik"] = ik_hex
        return key

    return make_session


def _lan_host(*, role: str, handler_for, host: str | None, timeout: float,
              waiting: str):
    """Start a one-shot server + beacon and block until a transfer or stop.

    Generates a per-session ephemeral keypair (advertised in the beacon for
    forward secrecy) and a host-side `make_session` callback. `handler_for(
    token, state, make_session)` builds the request handler. Returns `state`
    (carries "done"/"peer"/"peer_id"/"peer_name"/"peer_ik" and, for receives,
    "blob"/"session_key").
    """
    identity = _load_identity()
    token = _lan_random_token()
    advertise_ip = host or _lan_local_ip()
    bind_host = host or ""
    ek_priv, ek_pub = _dh_keypair()
    ik_pub_hex = identity["ik_pub"]
    ek_pub_hex = format(ek_pub, "x")
    state: dict = {}
    stop = threading.Event()
    make_session = _make_responder_session(identity, ek_priv, ek_pub, state,
                                           threading.Lock())
    handler = handler_for(token, state, make_session)
    beacon_thread = {"t": None}

    def on_listen(port):
        _lan_warn_firewall()
        if advertise_ip == "127.0.0.1":
            print("warning: advertising 127.0.0.1 (only reachable from this "
                  "machine)", file=sys.stderr)
        print(f"listening as {identity['name']} on {advertise_ip}:{port} — "
              f"{waiting}", file=sys.stderr)
        t = threading.Thread(
            target=_lan_beacon_sender,
            args=(stop, identity["device_id"], identity["name"], advertise_ip,
                  port, role, token, ik_pub_hex, ek_pub_hex),
            daemon=True,
        )
        t.start()
        beacon_thread["t"] = t

    deadline = time.monotonic() + timeout if timeout > 0 else None
    try:
        _lan_serve_one(handler, bind_host, state, stop, on_listen, deadline)
    except OSError as e:
        print(f"error: cannot start server: {e}", file=sys.stderr)
    finally:
        stop.set()
        if beacon_thread["t"] is not None:
            beacon_thread["t"].join(timeout=2)
    return state


def _lan_write_received(blob: bytes, session_key: bytes, peer: str) -> int:
    """Decrypt a received blob and write the plaintext into the cwd."""
    try:
        name, data = decrypt_bytes(blob, session_key.hex())
    except ValueError:
        print("error: authentication failed (identity mismatch, or the file "
              "was corrupted in transit)", file=sys.stderr)
        return 1
    out = _pick_decrypted_path(Path.cwd(), name)
    try:
        out.write_bytes(data)
    except OSError as e:
        print(f"error: cannot write {out}: {e}", file=sys.stderr)
        return 1
    print(f"received {name} from {peer} -> {out.name}", file=sys.stderr)
    return 0


def cmd_forget(needle: str) -> int:
    """Drop the pinned identity key(s) for a peer (by name or device id).

    Use this to reset trust after a legitimate reinstall, so the next transfer
    re-pins the new key silently instead of warning.
    """
    dropped = _forget_peer(needle)
    if not dropped:
        print(f"error: no known peer matching {needle!r}", file=sys.stderr)
        return 1
    print(f"forgot {', '.join(dropped)} — the next transfer will re-pin.",
          file=sys.stderr)
    return 0


def _joiner_session(identity: dict, peer: dict) -> tuple:
    """Generate an ephemeral key and derive the session key with a host peer.

    Returns (session_key_bytes, ek_pub_hex). Raises ValueError if the peer
    advertised an invalid identity or ephemeral public key.
    """
    ek_priv, ek_pub = _dh_keypair()
    key = _session_key_initiator(
        int(identity["ik_priv"], 16), ek_priv,
        int(identity["ik_pub"], 16), ek_pub,
        int(peer["ik"], 16), int(peer["ek"], 16))
    return key, format(ek_pub, "x")


def _pin_from_state(state: dict) -> None:
    """Pin the peer recorded by the host-side handshake, after a transfer."""
    peer_id = state.get("peer_id")
    if peer_id and state.get("peer_ik"):
        _save_known_peer(peer_id, state.get("peer_name") or peer_id,
                         state["peer_ik"])


def cmd_encrypt(path: str, *, host: str | None = None,
                timeout: float = 0.0) -> int:
    """Send a file to another machine on the same Wi-Fi (nothing hits disk).

    If a receiver is already waiting, pick it from the list and upload. If
    none is waiting, host the file and wait for a receiver to fetch it. The
    transfer is encrypted under a fresh session key (triple-DH); the peer's
    identity is checked against the TOFU pin.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        print(f"error: not a file: {p}", file=sys.stderr)
        return 1
    try:
        data = p.read_bytes()
    except OSError as e:
        print(f"error: cannot read {p}: {e}", file=sys.stderr)
        return 1
    identity = _load_identity()
    # Short, fixed probe to see if a peer is already waiting; the wait
    # `timeout` applies only once we host.
    try:
        waiting = _lan_discover("recv", discover_timeout=_LAN_DISCOVER_TIMEOUT,
                                bind_host=host or "")
    except OSError as e:
        print(f"error: cannot listen on the network: {e}", file=sys.stderr)
        return 1

    if waiting:
        # Join a waiting receiver and upload.
        peer = _lan_choose(waiting, "receiver(s)")
        if peer is None:
            return 1
        if not _tofu_check_join(peer):
            print("aborted (identity not trusted).", file=sys.stderr)
            return 1
        try:
            key, ek_pub_hex = _joiner_session(identity, peer)
        except (ValueError, TypeError):
            print(f"error: {peer['name']} advertised an invalid key",
                  file=sys.stderr)
            return 1
        blob = encrypt_bytes(data, p.name, key.hex())
        try:
            _lan_upload_blob(peer["ip"], peer["port"], peer["tok"], blob,
                             identity, ek_pub_hex)
        except ConnectionError as e:
            print(f"error: could not send to {peer['name']} ({e})",
                  file=sys.stderr)
            return 1
        _save_known_peer(peer["id"], peer["name"], peer["ik"])
        print(f"sent {p.name} ({len(data):,} bytes) to {peer['name']}.",
              file=sys.stderr)
        return 0

    # Nobody waiting: host the file and let a receiver fetch it. The handler
    # derives the session key per requester; we encrypt the blob under it.
    def make_blob(session_key):
        return encrypt_bytes(data, p.name, session_key.hex())

    state = _lan_host(
        role="send",
        handler_for=lambda token, st, mk: _lan_make_pull_handler(
            mk, make_blob, token, st),
        host=host, timeout=timeout, waiting="waiting for a receiver...")
    if not state.get("done"):
        print("error: no receiver connected"
              + (f" within {timeout:.0f}s" if timeout > 0 else "")
              + " (run `nomnom decrypt` on the other machine)",
              file=sys.stderr)
        return 1
    _pin_from_state(state)
    peer_name = state.get("peer_name") or state.get("peer")
    print(f"sent {p.name} ({len(data):,} bytes) to {peer_name}.",
          file=sys.stderr)
    return 0


def cmd_decrypt(*, host: str | None = None, timeout: float = 0.0) -> int:
    """Receive a file from another machine, writing it into the cwd.

    If a sender is already hosting, pick it from the list and fetch. If none
    is waiting, host and wait for a sender to push to you. The peer's identity
    is checked against the TOFU pin.
    """
    identity = _load_identity()
    # Short, fixed probe to see if a peer is already waiting; the wait
    # `timeout` applies only once we host.
    try:
        waiting = _lan_discover("send", discover_timeout=_LAN_DISCOVER_TIMEOUT,
                                bind_host=host or "")
    except OSError as e:
        print(f"error: cannot listen on the network: {e}", file=sys.stderr)
        return 1

    if waiting:
        # Join a waiting sender and fetch.
        peer = _lan_choose(waiting, "sender(s)")
        if peer is None:
            return 1
        if not _tofu_check_join(peer):
            print("aborted (identity not trusted).", file=sys.stderr)
            return 1
        try:
            key, ek_pub_hex = _joiner_session(identity, peer)
        except (ValueError, TypeError):
            print(f"error: {peer['name']} advertised an invalid key",
                  file=sys.stderr)
            return 1
        try:
            blob = _lan_fetch_blob(peer["ip"], peer["port"], peer["tok"],
                                   identity, ek_pub_hex)
        except ConnectionError as e:
            print(f"error: could not fetch from {peer['name']} ({e})",
                  file=sys.stderr)
            return 1
        rc = _lan_write_received(blob, key, peer["name"])
        if rc == 0:
            _save_known_peer(peer["id"], peer["name"], peer["ik"])
        return rc

    # Nobody waiting: host and wait for a sender to push.
    state = _lan_host(
        role="recv",
        handler_for=lambda token, st, mk: _lan_make_push_handler(
            mk, st, token, _LAN_MAX_UPLOAD, threading.Lock()),
        host=host, timeout=timeout, waiting="waiting for a sender...")
    if not state.get("done"):
        print("error: no sender connected"
              + (f" within {timeout:.0f}s" if timeout > 0 else "")
              + " (run `nomnom encrypt <file>` on the other machine)",
              file=sys.stderr)
        return 1
    key = state.get("session_key")
    if key is None:
        print("error: handshake did not complete", file=sys.stderr)
        return 1
    peer_name = state.get("peer_name") or state.get("peer")
    rc = _lan_write_received(state.get("blob") or b"", key, peer_name)
    if rc == 0:
        _pin_from_state(state)
    return rc


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
        "kind", choices=list(KIND_TO_NAME),
        help="which list to edit: text | binary | name | secret",
    )
    sub.add_argument(
        "values", nargs="+",
        help="one or more entries (e.g. .rmeta, MODULE.bazel, '*.creds')",
    )
    return sub


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
    sub.add_argument(
        "--copy", action="store_true",
        help="Copy output to the system clipboard instead of writing a file.",
    )
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
    sub.add_argument(
        "--copy", action="store_true",
        help="Copy output to the system clipboard instead of writing a file.",
    )
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
    sub.add_argument(
        "--copy", action="store_true",
        help="Copy output to the system clipboard instead of writing a file.",
    )
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


def _build_forget_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom forget",
        description=(
            "Drop the pinned identity key for a known peer (matched by name or "
            "device id). Use this to reset trust after a legitimate reinstall, "
            "so the next transfer re-pins the new key without warning."
        ),
    )
    sub.add_argument("peer", help="Peer name or device id to forget.")
    return sub


def _build_encrypt_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom encrypt",
        description=(
            "Send a file to another machine on the same Wi-Fi, encrypted. "
            "Nothing is written to disk on this side. If a receiver is already "
            "waiting (`nomnom decrypt`), pick it from the list and upload; "
            "otherwise host the file and wait for one to fetch it. No pairing "
            "step: a peer is trusted on first use and pinned, and a later "
            "identity-key change is flagged. Stops after one transfer."
        ),
    )
    sub.add_argument("file", help="Path to the file to send.")
    sub.add_argument("--host", default=None,
                     help="Advanced: bind/advertise this IP (e.g. under a VPN).")
    sub.add_argument("--timeout", type=float, default=0.0,
                     help="Seconds to wait while hosting (0.0 = forever). "
                          "Discovery uses a fixed probe window and is not "
                          "controlled by this flag.")
    return sub


def _build_decrypt_parser() -> argparse.ArgumentParser:
    sub = argparse.ArgumentParser(
        prog="nomnom decrypt",
        description=(
            "Receive a file from another machine on the same Wi-Fi and write "
            "the decrypted file into the current directory. If a sender is "
            "already hosting (`nomnom encrypt <file>`), pick it from the list "
            "and fetch; otherwise wait for one to push to you. No pairing step: "
            "a peer is trusted on first use and pinned, and a later "
            "identity-key change is flagged. Stops after one transfer."
        ),
    )
    sub.add_argument("--host", default=None,
                     help="Advanced: bind/advertise this IP (e.g. under a VPN).")
    sub.add_argument("--timeout", type=float, default=0.0,
                     help="Seconds to wait while hosting (0.0 = forever). "
                          "Discovery uses a fixed probe window and is not "
                          "controlled by this flag.")
    return sub


def _dispatch_subcommand(argv: list[str]) -> int:
    # Mixing argparse subparsers with the optional `repo` positional confuses
    # argparse's positional matcher, so we sniff the verb and dispatch by hand.
    verb = argv[0]
    if verb in ("register", "unregister"):
        args = _build_subcommand_parser(verb).parse_args(argv[1:])
        return cmd_register(args.kind, args.values, remove=(verb == "unregister"))
    if verb == "commit":
        args = _build_commit_parser().parse_args(argv[1:])
        return cmd_commit(args.repo, args.copy)
    if verb == "pr":
        args = _build_pr_parser().parse_args(argv[1:])
        return cmd_pr(args.repo, args.copy, args.base)
    if verb == "review":
        args = _build_review_parser().parse_args(argv[1:])
        return cmd_review(args.repo, args.pr_number, args.copy, args.diff)
    if verb == "rebuild":
        args = _build_rebuild_parser().parse_args(argv[1:])
        return cmd_rebuild(args.bundle, args.name)
    if verb == "forget":
        args = _build_forget_parser().parse_args(argv[1:])
        return cmd_forget(args.peer)
    if verb == "encrypt":
        args = _build_encrypt_parser().parse_args(argv[1:])
        return cmd_encrypt(args.file, host=args.host, timeout=args.timeout)
    if verb == "decrypt":
        args = _build_decrypt_parser().parse_args(argv[1:])
        return cmd_decrypt(host=args.host, timeout=args.timeout)
    print(f"error: unknown subcommand: {verb}", file=sys.stderr)
    return 2


SUBCOMMANDS = (
    "register", "unregister", "commit", "pr", "review", "rebuild",
    "encrypt", "decrypt", "forget",
)


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] in SUBCOMMANDS:
        return _dispatch_subcommand(sys.argv[1:])

    parser = argparse.ArgumentParser(
        description="nomnom: feed your repo to the LLM, one .txt snack at a time.",
        epilog=(
            "subcommands: register / unregister edit the auto-managed "
            "extension lists; commit / pr / review bundle git context for "
            "an LLM; rebuild reconstructs a file tree from a bundle .txt; "
            "encrypt / decrypt move a file between two machines on the same "
            "Wi-Fi (encrypt sends, decrypt receives; pick the peer from a "
            "list, only ciphertext crosses the wire), trusting a peer on "
            "first use; forget drops a peer's pinned key. "
            "run `nomnom <subcommand> --help` for details."
        ),
    )
    parser.add_argument(
        "repo", nargs="?", default=".",
        help="Path to the project repo (default: current directory).",
    )
    parser.add_argument(
        "--copy", action="store_true",
        help="Copy output to the system clipboard instead of writing a file.",
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

    selected: list[str] | None = None
    last = load_last_selection(root)
    if last:
        file_rels = {it.rel for it in items if not it.is_dir}
        present = [p for p in last if p in file_rels]
        if present and confirm(f"reuse last selection ({len(present)} files)?", default=True):
            selected = sorted(present)

    if selected is None:
        print("reading file stats...", file=sys.stderr)
        t0 = time.monotonic()
        stats = collect_stats(root, items)
        print(f"  done ({time.monotonic() - t0:.1f}s).", file=sys.stderr)
        nodes = build_tree(items, stats=stats)
        result = pick(nodes)
        if result is None:
            print("cancelled.", file=sys.stderr)
            return 130
        if not result:
            print("no files selected.", file=sys.stderr)
            return 0
        selected = sorted(result)

    include_tree = confirm("include file tree in output?", default=True)
    tree_str = render_ascii_tree(selected, repo_name) if include_tree else None

    total_bytes = 0
    large: list[tuple[str, int]] = []
    for rel in selected:
        p = root / rel
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        total_bytes += sz
        if sz > LARGE_FILE_BYTES:
            large.append((rel, sz))

    out_path = pick_output_path(repo_name) if not args.copy else None
    approx_tokens = total_bytes // 4

    print()
    print(f"  files:   {len(selected)}")
    print(f"  size:    {_fmt_size(total_bytes)}")
    print(f"  ~tokens: {_fmt_tokens(approx_tokens)} (rough chars/4 estimate)")
    if args.copy:
        clip = detect_clipboard_cmd()
        if clip:
            print(f"  output:  clipboard via {clip[0]}")
        else:
            print("  output:  clipboard (no tool found; will fall back to file)")
    else:
        print(f"  output:  {out_path}")
    if large:
        print(f"  large:   {len(large)} file(s) over {_fmt_size(LARGE_FILE_BYTES)}:")
        for rel, sz in large[:5]:
            print(f"           - {rel} ({_fmt_size(sz)})")
        if len(large) > 5:
            print(f"           ... and {len(large) - 5} more")
    print()
    try:
        input("press enter to write, Ctrl-C to cancel: ")
    except (EOFError, KeyboardInterrupt):
        print("\ncancelled.", file=sys.stderr)
        return 130

    output = render_output(repo_name, root, selected, tree_str)

    if args.copy:
        if copy_to_clipboard(output):
            save_last_selection(root, selected)
            print(f"copied {len(output):,} bytes to clipboard.")
            return 0
        print(
            "no clipboard tool found (pbcopy/wl-copy/xclip/xsel); "
            "falling back to a file.",
            file=sys.stderr,
        )
        out_path = pick_output_path(repo_name)

    try:
        out_path.write_text(output, encoding="utf-8")
    except OSError as e:
        print(f"error writing output: {e}", file=sys.stderr)
        return 1
    save_last_selection(root, selected)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ncancelled.", file=sys.stderr)
        sys.exit(130)
