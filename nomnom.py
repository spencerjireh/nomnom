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
import subprocess
import sys
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


def build_tree(items: list[ScanItem]) -> list[Node]:
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
    return nodes


def visible_indices(nodes: list[Node]) -> list[int]:
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
    """Make a branch name safe for use in a filename."""
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
    repo_name = root.name or "repo"
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
    repo_name = root.name or "repo"
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
    print(f"error: unknown subcommand: {verb}", file=sys.stderr)
    return 2


SUBCOMMANDS = ("register", "unregister", "commit", "pr")


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] in SUBCOMMANDS:
        return _dispatch_subcommand(sys.argv[1:])

    parser = argparse.ArgumentParser(
        description="nomnom: feed your repo to the LLM, one .txt snack at a time.",
        epilog=(
            "subcommands: register / unregister edit the auto-managed "
            "extension lists; commit / pr bundle git context for an LLM. "
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

    selected: list[str] | None = None
    last = load_last_selection(root)
    if last:
        file_rels = {it.rel for it in items if not it.is_dir}
        present = [p for p in last if p in file_rels]
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
