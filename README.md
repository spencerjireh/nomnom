# nomnom

[![tests](https://github.com/spencerjireh/nomnom/actions/workflows/test.yml/badge.svg)](https://github.com/spencerjireh/nomnom/actions/workflows/test.yml)

> the LLM is hungry. feed it your repo.

A single-file Python CLI that bundles selected files from any project into one `.txt` you can paste into a chat app. Output format is similar to [repomix](https://github.com/yamadashy/repomix); the distribution model is different — one file, stdlib only, no install step.

- One file. **Stdlib only.** No `pip install`, no `uv`, no virtualenv.
- Interactive `curses` picker with color, expand/collapse, mouse, fuzzy filter, and folder cascade-check.
- Respects `.gitignore`. Skips junk dirs, binaries, symlinks, and `.env` / private keys by default.
- Output mimics repomix: `<file path="...">…</file>` blocks the LLM can chew on.

## Install

Single file, stdlib only. Drop it anywhere Python can find it:

```sh
curl -O https://raw.githubusercontent.com/spencerjireh/nomnom/main/nomnom.py
```

For a `nomnom` command on `$PATH`:

```sh
chmod +x nomnom.py
ln -s "$(pwd)/nomnom.py" ~/.local/bin/nomnom
```

## Feed it

```sh
python3 nomnom.py                # nom the current directory
python3 nomnom.py ~/code/foo     # nom a specific repo
python3 nomnom.py --copy .       # straight to your clipboard, no file
```

You'll get an interactive picker, a confirmation summary, then either a file like `foo-20260503-101415.txt` in your current dir or the bundle on your clipboard. Drag it into your favourite chat app and watch the model graze.

nomnom remembers the last selection per-repo in `~/.cache/nomnom/` — a second run on the same repo offers to reuse it.

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

## Flags

| Flag | What it does |
| --- | --- |
| `--copy` | Pipe output to the system clipboard (`pbcopy` on macOS; `wl-copy` / `xclip` / `xsel` on Linux) instead of writing a file. Falls back to a file if no tool is found. |
| `--include-secrets` | Disable the default skip of `.env*`, `*.pem`, `*.key`, `id_rsa*`, `.netrc`, `.npmrc`, `secrets.{json,yaml}`, etc. |
| `--no-color` | Render the picker without color (also honors the `NO_COLOR` env var). |

## Picker keys

| Key | Action |
| --- | --- |
| `↑` `↓` / `j` `k` | move cursor |
| `PgUp` `PgDn` / `g` `G` | page / jump to top or bottom |
| `Space` | toggle (folder = cascade to all descendants) |
| `→` `←` / `l` `h` | expand / collapse folder |
| `E` / `C` | expand all / collapse all |
| `/` | filter (Esc to clear) |
| `a` | toggle all currently visible |
| `Enter` | confirm selection and exit |
| Click | toggle a row |
| Double-click on folder | expand / collapse |
| `q` / `Ctrl-C` | cancel |

## Safety

By default nomnom won't bundle obvious secret files. Patterns: `.env`, `.env.*`, `*.pem`, `*.key`, `*.pfx`, `*.p12`, `id_rsa*`, `id_dsa*`, `id_ecdsa*`, `id_ed25519*` (but `.pub` files are fine), `.netrc`, `.npmrc`, `.pypirc`, `secrets.{json,yaml,yml}`, `credentials`, `credentials.json`. Pass `--include-secrets` if you really want them.

## Contributing extensions

The four classification lists (text extensions, binary extensions, known-text filenames, secret patterns) live inside a marker block in `nomnom.py`. nomnom can edit them itself:

```sh
nomnom register binary .lockb         # bun's lockfile
nomnom register text .rmeta .pyx      # multiple at once
nomnom register name MODULE.bazel     # extensionless filename
nomnom register secret '*.creds'      # secret pattern
nomnom unregister text .pyx           # take it back
```

Each call rewrites the marker block in `nomnom.py` (alphabetized, deduped), prints what it changed, and reminds you to review:

```sh
git diff nomnom.py
git commit -am "register .lockb as binary (bun)"
gh pr create --fill
```

Conflicts are refused: registering `.foo` as text when it's already in BINARY_EXTENSIONS errors out and tells you to unregister it first. Re-registering an existing entry is a harmless no-op. nomnom doesn't touch git itself — review and commit are yours.

## Environment

- `NO_COLOR=1` — disable color in the picker.
- `WAYLAND_DISPLAY` — when set, `--copy` prefers `wl-copy` over `xclip`.

## Requirements

- Python 3.8+
- macOS or Linux (no Windows; `curses` isn't in stock CPython there)

## Development

The runtime is stdlib-only, but the test suite uses pytest.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Or with [uv](https://github.com/astral-sh/uv):

```sh
uv venv && uv pip install -r requirements-dev.txt
uv run pytest
```

CI runs the tests on macOS and Linux against Python 3.8 – 3.12.

## License

MIT — see [LICENSE](./LICENSE).
