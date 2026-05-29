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

Bare `nomnom` on a TTY opens a launcher with tiles for Bundle, Send, Receive, Pair, Rebuild, Pins, and Extensions. Inside the picker, `v` cycles bundle / `commit` / `pr` / `review` when run from inside a git repo.

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

## Send between machines (via your own relay)

`pair`, `encrypt`, and `decrypt` move files between machines through a Cloudflare Worker you deploy to your own account. Nothing hits disk on the sender. The relay sees only ciphertext + per-pair rendezvous ids.

### One-time setup

Deploy the Worker (see [`relay-worker/README.md`](relay-worker/README.md)). The HMAC secret is just a string — `scripts/generate-secret.sh` produces a memorable 6-word passphrase like `fend-sage-trash-cod-visa-data` so you can speak or password-manager it across machines. On each machine:

```sh
nomnom relay setup           # paste URL + the passphrase, runs a self-test
```

### Day-to-day

```sh
# First contact (no pin yet) — both sides run `pair` at the same time:
nomnom pair                          # identity-only handshake, TOFU on both ends

# Recurring (after pairing, no extra ceremony):
nomnom decrypt                       # long-polls every pinned peer
nomnom encrypt report.txt            # auto-targets the single pinned peer
nomnom encrypt report.txt --to spencer-mac   # disambiguates if many
```

`pair` is symmetric: whichever side wins the race at the rendezvous slot becomes the initiator, the other side falls back to responder. Pinning is local, so a one-sided TOFU decline just leaves that side unpinned; the other side times out after 30s with no harm.

`--trust-new` auto-accepts the TOFU prompt (scriptable but loses verification; an audit line is still written to stderr). Max transfer size: 256 MB (capped at 100 MB on Cloudflare's free tier — see relay-worker/README.md).

### Pinned peers

```sh
nomnom peers list                    # show pinned peers + fingerprints
nomnom peers fingerprint spencer-mac # for out-of-band verification
nomnom peers nickname dev-abc spencer   # short alias for --to
nomnom peers forget spencer-mac      # drop a pin (then `nomnom pair` to re-pair)
```

<details>
<summary><b>Trust on first use</b></summary>

Each machine has a long-term identity key in `~/.config/nomnom/identity.json`. The first transfer pins the peer's key in `~/.config/nomnom/known_peers.json`. If a pinned key changes, nomnom blocks:

```text
  WARNING: the identity key for 'bob-laptop' has CHANGED.
    pinned:  b685:2bf3:e978:49de
    offered: 9c01:7a4f:21bd:0e88
  trust the new key and continue? [y/N]:
```

First contact prompts before any pin commits:

```text
  first contact with 'bob-laptop' (device 7a31f9d2c8b04e15).
    fingerprint: 9c01:7a4f:21bd:0e88
  verify this fingerprint out-of-band with the sender if it matters.
  trust and pin this device? [y/N]:
```

Anyone who can talk to your relay can land at the rendezvous, so verify the fingerprint out-of-band if it matters. `--trust-new` skips the prompt for scripted callers and still writes an audit line to stderr.

</details>

<details>
<summary><b>Crypto</b></summary>

**Recurring transfers** (encrypt/decrypt to an already-pinned peer): Triple-DH over RFC 3526 group 14 derives a fresh session key per transfer (forward secrecy, identity-pinned auth). The canonical pair of identity pubkeys is mixed into the key derivation, so a relay-level adversary cannot land on the same key without the long-term pin. Three-message handshake over the Worker: sender PUTs init blob → receiver fetches + PUTs response → sender encrypts + PUTs ciphertext → receiver fetches + decrypts.

**First contact (`pair`)**: identity-only, two-message exchange. Each side PUTs its identity pubkey + device id + name into the per-relay rendezvous slot derived from `scrypt(relay_secret, "nomnom-first-contact-v2", N=2^15)`; the scrypt cost slows offline brute-force on a human-memorable passphrase. No DH, no session key, no payload — the trust claim narrows to "anyone with the relay secret can land at the rendezvous; TOFU confirms device identity." Verify the fingerprint out-of-band if it matters.

Each slot expires in 5 min on the Worker; bucket lifecycle deletes orphans after 1 day. The Worker's HMAC authenticates clients to your relay; body integrity comes from the AEAD wrapper (encrypt-then-HMAC-SHA256 in counter mode, keys via scrypt).

</details>

<details>
<summary><b>Relay config</b></summary>

| Command | Effect |
| --- | --- |
| `nomnom relay setup` | Interactive prompts for URL + secret; runs a round-trip test. |
| `nomnom relay set URL --secret S` | Non-interactive equivalent. |
| `nomnom relay test` | Round-trip: hits `/health` then PUT + GET a random slot. |
| `nomnom relay show` | Prints URL; redacts secret. |
| `nomnom relay clear` | Deletes `~/.config/nomnom/relay.json`. |

</details>

<details>
<summary><b>Reset all state</b></summary>

`nomnom reset` wipes `~/.config/nomnom/` after a y/N prompt — identity, pinned peers, and relay config in one shot. Refuses on a non-tty stdin so a piped invocation can't accidentally blow it away.

```sh
nomnom reset
# about to delete /Users/you/.config/nomnom and 3 entries
# (identity.json, known_peers.json, relay.json).
# identity rotation will invalidate every pin on every paired device.
# proceed? [y/N]: y
```

After reset the next `nomnom relay setup` regenerates a fresh identity. Other devices that previously paired with you will see a TOFU mismatch on the next transfer; re-pair to re-establish trust.

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
