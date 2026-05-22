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
| `--include-ignored` | Bundle files normally excluded by `.gitignore` rules. Useful when you need generated code (e.g. protobuf output) that's gitignored. Junk dirs (`.git`, `node_modules`, etc.) and the secrets filter still apply. |
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

## Git workflow helpers

Three extra subcommands dump git/gh context into a `.txt` for an LLM. `commit` and `pr` help you *draft* a message; `review` helps you *read* an existing PR.

```sh
nomnom commit                  # status, diffs, recent commits
nomnom pr                      # commits since base, full diff, existing PR body
nomnom pr --base develop       # PR against a non-default base
nomnom commit --copy           # straight to clipboard
nomnom review 123              # title, body, comments, reviews, threads, checks
nomnom review 123 --diff       # also include the full diff
```

Each section is wrapped in a `<section name="...">` block (mirroring the file bundle's `<file path="...">` shape) and prefixed with a `<file_tree>` of the changed files.

`commit` errors out if there are no staged or unstaged changes. `pr` and `review` require the [`gh`](https://cli.github.com) CLI. `pr` auto-detects the default base branch via `gh repo view` and looks up an existing PR for the current branch via `gh pr view` (the section is `none` if there isn't one yet). `review` pulls a specific PR by number — meta, body, linked issues, commits, diff summary, reviews, top-level comments, inline review threads (grouped by file then line, tagged `[resolved]` / `[outdated]`), curated timeline events, and CI checks. The full diff is opt-in (`--diff`) since inline review comments already carry their own diff hunks. Output filenames look like `<repo>-<branch>-commit-<ts>.txt`, `<repo>-<branch>-pr-<ts>.txt`, and `<repo>-pr-<n>-review-<ts>.txt` (detached HEAD substitutes a short SHA for the branch name).

## Rebuild a bundle

`nomnom rebuild` is the inverse of bundling: feed it a bundle `.txt` (saved file or piped from stdin) and it reconstructs the file tree under your current directory.

```sh
nomnom rebuild foo-20260503-101415.txt   # → ./foo/ (auto-suffixes to foo-1/ if it exists)
pbpaste | nomnom rebuild                 # read from clipboard via stdin
nomnom rebuild bundle.txt --name scratch # override the target folder name
```

The folder name comes from the `<repo>` token in the bundle's header; `--name` overrides it. Collisions auto-suffix `-1`, `-2`, ... — nomnom never overwrites an existing folder. Git-context bundles (`commit` / `pr` / `review` outputs) aren't invertible into files and are rejected with a clear error. Paths in the bundle that try to escape the target folder (absolute paths, `..` segments) are refused.

## Send over LAN

`encrypt` and `decrypt` move a file between two machines **on the same Wi-Fi**, encrypted — nothing is written to disk on the sending side, and there's no code, passphrase, or IP to type. **No pairing step:** you run the command, pick the peer from a list of discovered devices, and the transfer happens.

```sh
# Receiver
nomnom decrypt                      # waits, or pick a sender if one's already hosting
#   found 1 sender:
#     1) alice-mbp  (192.168.1.42, known)
#   pick [1-1]:

# Sender
nomnom encrypt report.txt           # → received on the other machine as ./report.txt
#   found 1 receiver:
#     1) bob-laptop  (192.168.1.55, new device)
#   pick [1-1]:
```

`encrypt` sends, `decrypt` receives. Whoever runs first hosts and waits; the other discovers it, you pick the peer from the list, and the transfer runs. The decrypted file lands in the receiver's current directory (auto-suffixed on name collision, never overwriting).

### Trust on first use

There's no setup, but transfers are still authenticated and private — trust works like SSH's `known_hosts`. Each machine has a long-term identity key; the first time you transfer with a peer, nomnom **pins** that peer's identity key (in `~/.config/nomnom/known_peers.json`) and tags it `new device` in the list. On later transfers a matching key shows as `known`. If a peer's identity key ever **changes**, nomnom warns and asks before continuing:

```text
  WARNING: the identity key for 'bob-laptop' has CHANGED.
  Expected if it was reinstalled or its config was wiped, but this
  is also exactly what a man-in-the-middle attack looks like.
    pinned:  b685:2bf3:e978:49de
    offered: 9c01:7a4f:21bd:0e88
  trust the new key and continue? [y/N]:
```

Press `y` only if you know why it changed (the other machine was reinstalled, etc.). To clear a pin ahead of time so the next transfer re-pins silently, run `nomnom forget <name>`.

### How it's kept private

Each transfer derives a **fresh session key** with a triple Diffie-Hellman exchange (RFC 3526 2048-bit group): each side contributes a throwaway ephemeral key plus its pinned identity key. That gives **forward secrecy** (a leaked key can't decrypt past transfers) and authenticates the exchange against the pinned identities — a man-in-the-middle that lacks the pinned private key can't produce a key that decrypts.

Only ciphertext crosses the wire. Discovery uses a UDP limited broadcast (`255.255.255.255`), which routers don't forward — so it stays on your local link and never reaches the wider internet. Each transfer is one-shot. A peer whose identity you decline, or a tampered blob, is rejected before anything is written.

Crypto is stdlib-only: triple Diffie-Hellman over big-ints for the session key, scrypt for the per-message key schedule, HMAC-SHA256 in counter mode for the stream cipher, encrypt-then-HMAC for authentication.

Notes:
- **Same Wi-Fi only.** Not an internet transfer; both machines must be on the same network segment.
- **Trust on first contact** is implicit: a man-in-the-middle present during the very first transfer with a peer would be pinned silently (TOFU's known limitation). Every later transfer is checked against that pin.
- On **macOS**, the first run may pop a firewall "allow incoming connections" prompt — click Allow, or the other machine can't reach the host.
- Under a **VPN** (or with multiple interfaces) auto-detection may pick the wrong address; pass `--host <your-wifi-ip>` on the side that hosts.
- `--timeout <seconds>` bounds how long a host waits.

### Two-machine check

1. Put `nomnom.py` on both machines (`curl -O …`) and join them to the same Wi-Fi.
2. On the receiver: `nomnom decrypt`. On the sender: `nomnom encrypt <file>`, then pick the receiver from the list.
3. On macOS, click Allow at the firewall prompt on whichever machine hosts. Under a VPN, add `--host <wifi-ip>` to the side that hosts.

A scripted version (two processes on one machine over real broadcast, trusting on first use) lives in the test suite as `TestLanTofuE2E`; run it with `NOMNOM_E2E=1 pytest -k TofuE2E`.

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
