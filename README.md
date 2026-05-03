# bundler

A tiny single-file Python CLI that bundles selected files from any project into one `.txt` you can paste into a chat app. Like [repomix](https://github.com/yamadashy/repomix), but pocket-sized.

- One file. **Stdlib only.** No `pip install`, no `uv`, no virtualenv.
- Interactive `curses` picker with expand/collapse, filter, and folder cascade-check.
- Respects `.gitignore`. Skips junk dirs, binaries, and symlinks by default.
- Output mimics repomix: `<file path="...">…</file>` blocks the LLM can parse cleanly.

## Install

There is no install. Drop the file anywhere:

```sh
curl -O https://raw.githubusercontent.com/spencerjireh/python-file-copier/main/bundler.py
```

## Use

```sh
python3 bundler.py            # bundle the current directory
python3 bundler.py ~/code/foo # bundle a specific repo
```

You'll get an interactive picker, a confirmation summary, then a file like `foo-20260503-101415.txt` in your current dir.

## Picker keys

| Key | Action |
| --- | --- |
| `↑` `↓` / `j` `k` | move cursor |
| `Space` | toggle (folder = cascade to all descendants) |
| `→` `←` / `l` `h` | expand / collapse folder |
| `E` / `C` | expand all / collapse all |
| `/` | filter (Esc to clear) |
| `a` | toggle all currently visible |
| `Enter` on a file | done |
| `q` / `Ctrl-C` | cancel |

## What you get

```text
This is a packed representation of selected files from foo, bundled on …

<file_tree>
foo/
├── src/
│   └── api/
│       └── handlers.py
└── README.md
</file_tree>

<file path="src/api/handlers.py">
…
</file>
```

Bundler also stashes your last selection in `.bundler-last.json` at the repo root, so a second run offers to reuse it.

## Requirements

- Python 3.8+
- macOS or Linux (no Windows; `curses` isn't in stock CPython there)

## License

MIT
