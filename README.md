# nomnom

[![tests](https://github.com/spencerjireh/nomnom/actions/workflows/test.yml/badge.svg)](https://github.com/spencerjireh/nomnom/actions/workflows/test.yml)

Single-file Python CLI that bundles a repo into one `.txt` for an LLM. Stdlib only — no install, no venv, no deps.

## Install

```sh
curl -O https://raw.githubusercontent.com/spencerjireh/nomnom/main/nomnom.py
chmod +x nomnom.py && ln -s "$(pwd)/nomnom.py" ~/.local/bin/nomnom   # optional
```

## Bundle a repo

```sh
nomnom .
```

Opens a picker for the current directory. Pick files, hit `Enter`, get `./<repo>-<timestamp>.txt`. `.gitignore`, junk dirs (`.git`, `node_modules`, …), binaries, symlinks, and obvious secrets are skipped before the picker loads.

Bare `nomnom` on a TTY opens a launcher menu fronting every verb (bundle, `commit`, `pr`, `review`, `encrypt`, `decrypt`, `rebuild`, `register`).

Output mirrors [repomix](https://github.com/yamadashy/repomix)'s shape:

```text
<file_tree>
foo/
└── src/api/handlers.py
</file_tree>

<file path="src/api/handlers.py">
…
</file>
```

Skip the picker when you already know what you want:

```sh
nomnom --all --stdout . | pbcopy    # everything → clipboard
nomnom --include 'src/**/*.py' .    # pre-check matches in the picker
```

<details>
<summary><b>Picker keys</b></summary>

| Key | Action |
| --- | --- |
| `↑`/`↓` `j`/`k`, `PgUp`/`PgDn`, `g`/`G` | move / page / jump |
| `Space` | toggle (folder cascades) |
| `→`/`←` `l`/`h`, `E`/`C` | expand / collapse one or all |
| `/` | filter (Esc clears) |
| `a` `s` | toggle all visible / sort alpha vs size |
| `d` `t` `p` | cycle destination / toggle file-tree section / toggle preview pane |
| `Enter` / `q` | write / cancel |
| Click / dbl-click | toggle row / expand folder |

Preview pane auto-shows at terminal width ≥ 100 cols.

</details>

<details>
<summary><b>Flags</b></summary>

| Flag | Effect |
| --- | --- |
| `--all` | Skip the picker, bundle every scanned file. |
| `--include GLOB` | Gitignore-style include (repeatable). With `--all`/`--stdout`, filters; otherwise pre-checks the picker. |
| `--exclude GLOB` | Gitignore-style exclude, applied after `--include`. |
| `--stdout` | Pipe the bundle to stdout. No TTY required. |
| `--include-secrets` | Disable the default secret-file skip. |
| `--include-ignored` | Bundle gitignored files (e.g. generated protobuf). Junk dirs and secrets still apply. |
| `--no-color` | Plain picker (or set `NO_COLOR=1`). |

</details>

<details>
<summary><b>Secret files skipped by default</b></summary>

`.env`, `.env.*`, `*.pem`, `*.key`, `*.pfx`, `*.p12`, `id_rsa*`, `id_dsa*`, `id_ecdsa*`, `id_ed25519*` (but `.pub` files pass), `.netrc`, `.npmrc`, `.pypirc`, `secrets.{json,yaml,yml}`, `credentials`, `credentials.json`. Override with `--include-secrets`.

</details>

## Git context for an LLM

`commit`, `pr`, and `review` bundle git/gh state into the same `<section name="…">` shape.

```sh
nomnom commit                  # status, diffs, recent commits
nomnom pr [--base develop]     # commits since base, full diff, existing PR body
nomnom review 123 [--diff]     # PR meta, body, comments, threads, checks
```

`commit` errors on a clean tree. `pr` and `review` require [`gh`](https://cli.github.com); `pr` auto-detects the base. `review` groups inline threads by file/line, tags `[resolved]`/`[outdated]`, and keeps the full diff opt-in via `--diff` (the threads already carry hunks). Each verb accepts `--clipboard` or `--stdout`.

## Rebuild

Inverse of bundling — reconstructs the tree under cwd.

```sh
nomnom rebuild foo-20260503-101415.txt   # → ./foo/ (auto-suffixes on collision)
pbpaste | nomnom rebuild                 # from stdin
nomnom rebuild bundle.txt --name scratch # override folder name
```

Never overwrites. Path-escape attempts (absolute paths, `..` segments) are refused. Git-context bundles aren't reconstructable and error out.

## Send over LAN

`encrypt` and `decrypt` move a file between two machines on the same Wi-Fi, encrypted end-to-end. Nothing hits disk on the sender; no pairing step.

```sh
nomnom decrypt           # receiver waits (or picks a sender if one's hosting)
nomnom encrypt report.txt
```

Scripted: `--peer <name|id>` skips the pick (case-insensitive, prefix-ok); `--trust-new` auto-accepts TOFU prompts. `--host <ip>` overrides interface detection (useful under a VPN); `--timeout <s>` bounds the host wait.

<details>
<summary><b>Trust on first use</b></summary>

Each machine has a long-term identity key. The first transfer pins the peer's key in `~/.config/nomnom/known_peers.json` (`new device` in the list); later transfers show `known`. If a pinned key changes, nomnom blocks:

```text
  WARNING: the identity key for 'bob-laptop' has CHANGED.
    pinned:  b685:2bf3:e978:49de
    offered: 9c01:7a4f:21bd:0e88
  trust the new key and continue? [y/N]:
```

`nomnom forget <name|id>` clears a pin.

</details>

<details>
<summary><b>Crypto</b></summary>

Triple-DH over RFC 3526 group 14 derives a fresh session key per transfer (forward secrecy, identity-pinned auth). Stdlib only: 3DH for the key, scrypt for the message schedule, HMAC-SHA256 stream cipher in counter mode, encrypt-then-HMAC. Only ciphertext crosses the wire; discovery is a limited UDP broadcast (`255.255.255.255`) that routers don't forward.

Caveats: same network segment only; TOFU's known weakness is the very first transfer (a MitM there would be pinned silently); macOS may show a firewall prompt on the host.

</details>

## Register extensions

The text/binary/name/secret lists live inside a marker block in `nomnom.py`. nomnom edits them itself:

```sh
nomnom register binary .lockb
nomnom register text .pyx .rmeta
nomnom register name MODULE.bazel
nomnom register secret '*.creds'
nomnom unregister text .pyx
```

Each edit alphabetizes + dedupes the block and prints the diff. Conflicting kinds (text vs binary) are refused.

## Requirements

Python 3.8+. macOS or Linux (Windows stock CPython lacks `curses`).

## Development

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Or with [uv](https://github.com/astral-sh/uv): `uv venv && uv pip install -r requirements-dev.txt && uv run pytest`.

CI runs on macOS and Linux against Python 3.8 – 3.12.

## License

MIT — see [LICENSE](./LICENSE).
