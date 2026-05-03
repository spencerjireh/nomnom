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
import json
import locale
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

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
LAST_SELECTION_FILE = ".nomnom-last.json"

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
    def __init__(self, rules: list):
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
    rules: list = []
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
) -> list:
    items: list = []

    def walk(dir_abs: Path, rel_dir: str) -> None:
        try:
            entries = list(os.scandir(dir_abs))
        except OSError:
            return
        dirs: list = []
        files: list = []
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


# ---------- clipboard ----------

def detect_clipboard_cmd() -> Optional[list]:
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
    parent: Optional[int]
    children: list = field(default_factory=list)
    expanded: bool = False
    checked: bool = False


def build_tree(items: list) -> list:
    nodes: list = []
    by_rel: dict = {}
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
    return nodes


def visible_indices(nodes: list) -> list:
    out = []
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


def cascade_check(nodes: list, idx: int, value: bool) -> None:
    nodes[idx].checked = value
    for c in nodes[idx].children:
        cascade_check(nodes, c, value)


# ---------- picker ----------

def _setup_theme() -> dict:
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


def pick(nodes: list) -> Optional[set]:
    """Curses checkbox-tree picker. Returns set of checked file rels, or None on cancel."""
    if not nodes:
        return set()

    state = {"cancelled": False}

    def cascade(idx: int, value: bool) -> None:
        cascade_check(nodes, idx, value)

    def _picker(stdscr) -> None:
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

        while True:
            if filter_buf:
                q = filter_buf.lower()
                visible = [i for i, n in enumerate(nodes) if q in n.rel.lower()]
            else:
                visible = visible_indices(nodes)

            if not visible:
                stdscr.erase()
                stdscr.addstr(0, 0, "(no matches)")
                stdscr.addstr(1, 0, f"/ {filter_buf}", curses.A_DIM)
                stdscr.refresh()
                ch = stdscr.getch()
                if filter_active or filter_buf:
                    if ch == 27:
                        filter_active = False
                        filter_buf = ""
                    elif ch in (10, 13):
                        filter_active = False
                    elif ch in (curses.KEY_BACKSPACE, 127, 8):
                        filter_buf = filter_buf[:-1]
                    elif 32 <= ch < 127:
                        filter_buf += chr(ch)
                    continue
                if ch in (ord("q"), 3):
                    state["cancelled"] = True
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
                line = f"{check} {indent}{glyph}{label}"[: w - 1]
                if n.checked:
                    attr = theme["checked"]
                elif n.is_dir:
                    attr = theme["dir"]
                else:
                    attr = theme["file"]
                if vi == cursor_pos:
                    attr |= theme["cursor"]
                try:
                    stdscr.addstr(row, 0, line, attr)
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
                "a:toggle-visible  enter:done  q:quit"
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
                if ch in (10, 13):
                    filter_active = False
                elif ch == 27:
                    filter_active = False
                    filter_buf = ""
                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    filter_buf = filter_buf[:-1]
                elif 32 <= ch < 127:
                    filter_buf += chr(ch)
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
                            cascade(cursor_ni, not n.checked)
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
                cascade(cursor_ni, not nodes[cursor_ni].checked)
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
                    cascade(v, any_unchecked)
            elif ch in (10, 13):
                if nodes[cursor_ni].is_dir:
                    nodes[cursor_ni].expanded = not nodes[cursor_ni].expanded
                else:
                    return
            elif ch == ord("d"):
                return
            elif ch in (ord("q"), 3):
                state["cancelled"] = True
                return

    try:
        curses.wrapper(_picker)
    except KeyboardInterrupt:
        state["cancelled"] = True

    if state["cancelled"]:
        return None
    return {n.rel for n in nodes if n.checked and not n.is_dir}


# ---------- last selection ----------

def load_last_selection(root: Path) -> Optional[list]:
    p = root / LAST_SELECTION_FILE
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        sel = data.get("selected")
        if isinstance(sel, list) and all(isinstance(x, str) for x in sel):
            return sel
    except (OSError, json.JSONDecodeError):
        pass
    return None


def save_last_selection(root: Path, selected: list) -> None:
    try:
        (root / LAST_SELECTION_FILE).write_text(
            json.dumps({"selected": sorted(selected)}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


# ---------- output ----------

def render_ascii_tree(paths: list, repo_name: str) -> str:
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
    files: list,
    tree: Optional[str],
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


def _read_block(path: Path) -> tuple:
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


def _parse_block(src_lines: list, start: int, end: int) -> dict:
    block = "".join(src_lines[start + 1:end])
    tree = ast.parse(block)
    out: dict = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        out[target.id] = ast.literal_eval(node.value)
    return out


def _emit_block(values: dict) -> str:
    parts: list = []
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
    path: Path, src_lines: list, start: int, end: int, new_block: str
) -> None:
    new_lines = src_lines[: start + 1] + [new_block] + src_lines[end:]
    path.write_text("".join(new_lines), encoding="utf-8")


def cmd_register(
    kind: str,
    values: list,
    remove: bool = False,
    path: Optional[Path] = None,
) -> int:
    p = path if path is not None else SELF_PATH
    src_lines, start, end = _read_block(p)
    parsed = _parse_block(src_lines, start, end)

    target_name = KIND_TO_NAME[kind]
    target = parsed[target_name]
    is_list = kind in KIND_IS_LIST

    changes: list = []
    for value in values:
        if not remove:
            for other_kind, other_name in KIND_TO_NAME.items():
                if other_name == target_name:
                    continue
                if value in parsed.get(other_name, ()):
                    print(
                        f"error: {value!r} is already in {other_name}. "
                        f"run `nomnom unregister {other_kind} {value}` first.",
                        file=sys.stderr,
                    )
                    return 1
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


# ---------- main ----------

def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] in ("register", "unregister"):
        verb = sys.argv[1]
        sub = argparse.ArgumentParser(
            prog=f"nomnom {verb}",
            description=(
                f"{verb.capitalize()} an entry in the auto-managed extension "
                "lists in nomnom.py. After it runs, review with `git diff "
                "nomnom.py` and commit when happy."
            ),
        )
        sub.add_argument(
            "kind", choices=list(KIND_TO_NAME.keys()),
            help="which list to edit: text | binary | name | secret",
        )
        sub.add_argument(
            "values", nargs="+",
            help="one or more entries (e.g. .rmeta, MODULE.bazel, '*.creds')",
        )
        a = sub.parse_args(sys.argv[2:])
        return cmd_register(a.kind, a.values, remove=(verb == "unregister"))

    parser = argparse.ArgumentParser(
        description="nomnom: feed your repo to the LLM, one .txt snack at a time.",
        epilog=(
            "subcommands: register / unregister - edit the auto-managed "
            "extension lists. run `nomnom register --help` for details."
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

    repo_name = root.name or "repo"
    print(f"scanning {root} ...", file=sys.stderr)
    gi = load_gitignore(root)
    items = scan_repo(root, gi, skip_secrets=not args.include_secrets)
    file_items = [it for it in items if not it.is_dir]
    if not file_items:
        print("no files found after applying excludes.", file=sys.stderr)
        return 0
    print(f"  {len(file_items)} files, {sum(1 for it in items if it.is_dir)} dirs",
          file=sys.stderr)

    selected: Optional[list] = None
    last = load_last_selection(root)
    if last:
        present = [p for p in last if any(it.rel == p and not it.is_dir for it in items)]
        if present and confirm(f"reuse last selection ({len(present)} files)?", default=True):
            selected = sorted(present)

    if selected is None:
        nodes = build_tree(items)
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
    large: list = []
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
    print(f"  size:    {total_bytes:,} bytes")
    print(f"  ~tokens: {approx_tokens:,} (rough chars/4 estimate)")
    if args.copy:
        clip = detect_clipboard_cmd()
        if clip:
            print(f"  output:  clipboard via {clip[0]}")
        else:
            print("  output:  clipboard (no tool found; will fall back to file)")
    else:
        print(f"  output:  {out_path}")
    if large:
        print(f"  large:   {len(large)} file(s) over {LARGE_FILE_BYTES:,} bytes:")
        for rel, sz in large[:5]:
            print(f"           - {rel} ({sz:,})")
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
