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

Bare `nomnom` on a TTY opens a launcher with tiles for Bundle, Send, Receive, Feeds, Commit, PR, Item, Rebuild, and Extensions. Inside the picker, `v` cycles bundle / `commit` / `pr` / `item` when run from inside a git repo.

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

`commit`, `pr`, and `item` bundle git/gh state into the same `<section name="…">` shape.

```sh
nomnom commit                  # status, diffs, recent commits
nomnom pr [--base develop]     # commits since base, full diff, existing PR body
nomnom item 123 [--diff]       # auto-detect: PR / issue / workflow run by number
nomnom item v1.2.3             # release notes + assets
nomnom item abc1234            # commit meta + diff + comments
nomnom item pr [123]           # explicit pr (auto-resolves current branch if id omitted)
nomnom item issue 45           # issue meta + body + comments + timeline + linked PRs
nomnom item discussion 7       # discussion meta + threaded comments + answer
nomnom item run 123456 [--all-logs]  # workflow run jobs + failing-step logs
nomnom item job 67890 [--all-logs]   # single job + steps + logs
```

`commit` errors on a clean tree. `pr` and `item` require [`gh`](https://cli.github.com); `pr` auto-detects the base. `nomnom item <id>` infers the kind: hex strings → commit, non-numeric tag-like strings → release, pure numbers run a parallel probe of pr / issue / workflow run and either auto-route on a unique hit or refuse with a disambiguation hint. `discussion` and `job` ids live in their own namespaces so they need explicit `nomnom item discussion <n>` / `nomnom item job <n>`. `pr` and `commit` honor `--diff` (off by default); `run` and `job` default to failing-step logs, `--all-logs` includes everything. Each verb accepts `--clipboard` or `--stdout`.

## Rebuild

Inverse of bundling — reconstructs the tree under cwd.

```sh
nomnom rebuild foo-20260503-101415.txt   # → ./foo/ (auto-suffixes on collision)
pbpaste | nomnom rebuild                 # from stdin
nomnom rebuild bundle.txt --name scratch # override folder name
```

Never overwrites. Path-escape attempts (absolute paths, `..` segments) are refused. Git-context bundles aren't reconstructable and error out.

## Send between machines (via your own relay)

`open`, `join`, `send`, and `receive` move files between machines through a
**feed** on a Cloudflare Worker you deploy to your own account. A feed is a
durable broadcast channel: one device mints it and shares a short URL; every
other device pastes the URL to join. Sends to a feed reach every member; the
URL is the credential. The relay sees only ciphertext + opaque slot ids.

### One-time setup

Deploy the Worker (see [`relay-worker/README.md`](relay-worker/README.md)). On the device that will own this relay:

```sh
nomnom relay init            # prompts for Worker URL; generates the HMAC secret
                             # used to mint feeds; prints the `wrangler` commands
                             # to push the secret and deploy.
```

### Open a feed and invite other devices

```sh
# device 1 (with relay configured)
nomnom open --name home --default
# opened feed 'home' (TTL 86400s, expires at 1717459200).
# share this URL with the other device:
# https://relay.your-subdomain.workers.dev/f/k4n2pX9qLm3T

# device 2 (no relay setup needed on this side — the URL is the credential)
nomnom join 'https://relay.your-subdomain.workers.dev/f/k4n2pX9qLm3T'
# joined feed 'feed-1' with 2 member(s).
```

You can also open multiple feeds (`home`, `work`, etc.); `nomnom feeds default <name>` switches which feed `send`/`receive` use by default.

### Day-to-day

```sh
nomnom receive                       # watch the default feed; one line per received file
nomnom send report.txt               # broadcast to every other member of the default feed
nomnom send report.txt --feed work   # broadcast to a different joined feed
```

`receive` keeps the long-poll alive after each delivery, so you can leave a laptop listening and fire off `send` from another machine all afternoon. Pass `--once` to exit after the first received file (for scripting).

Each post is encrypted with the feed key derived from the URL token, and signed by the sender's Ed25519 identity key. Receivers verify the signature against the sender's pinned identity — a hostile relay (or a URL leak) can't impersonate a member without their identity private key.

`--trust-new` auto-pins TOFU prompts (scriptable but loses verification; an audit line is still written to stderr). Max transfer size: 256 MB (capped at 100 MB on Cloudflare's free tier — see relay-worker/README.md).

### Managing feeds

```sh
nomnom feeds list                                  # joined feeds + default marker + TTL
nomnom feeds members home                          # fetch and display the live roster
nomnom feeds url home                              # print the shareable URL
nomnom feeds default work                          # switch the default
nomnom feeds rename home house                     # rename a feed locally
nomnom feeds extend home --ttl 604800              # bump expiry to a week from now
nomnom feeds leave home                            # delete this device's member card, drop locally
```

### Pinned identities

Sender authenticity is the load-bearing TOFU surface in v2: the first time
any feed introduces an identity that this device has never seen, you get a
prompt. After accepting, that identity is trusted globally — joining another
feed where the same person already participates is silent.

```sh
nomnom peers list                    # show pinned identities + fingerprints
nomnom peers fingerprint alice-mac   # for out-of-band verification
nomnom peers forget alice-mac        # drop a pin (TOFU fires fresh on next sighting)
```

### From a browser

[`nomnom-web/`](nomnom-web/README.md) is a browser client (Vite + React) that
participates in the same feeds as the CLI. Its crypto is a byte-for-byte
TypeScript port of `nomnom.py`. Deployed to Cloudflare Pages; the relay
Worker allowlists its origin for CORS.

<details>
<summary><b>Trust on first use</b></summary>

Each machine mints a long-term Ed25519 identity in `~/.config/nomnom/identity.json` on first run. The first time you see a member's identity across all feeds, nomnom prompts:

```text
  first contact with 'bob-laptop'.
    fingerprint: 9c01:7a4f:21bd:0e88
  verify out-of-band if it matters.
  trust and pin this device? [y/N]:
```

Once pinned, that identity is trusted in every feed it appears in. Possessing a feed URL grants access to that one feed, but never impersonates anyone — the signature is the trust anchor, not the URL.

`--trust-new` skips the prompt for scripted callers and writes an audit line to stderr.

</details>

<details>
<summary><b>Crypto</b></summary>

**Feed transport.** Each feed has a 32-byte symmetric key derived via HKDF-SHA256 from the URL token. Every member with the URL derives the same key; the Worker derives it on the fly per request to verify signatures. Posts are encrypted with ChaCha20-style keystream (HMAC-SHA256 as PRF) and authenticated with HMAC-SHA256 over (magic ‖ nonce ‖ ciphertext). The plaintext is `len(header) ‖ JSON header ‖ raw body`; the header carries the sender's member id, identity pubkey, filename, file size, content hash, posted timestamp, and an Ed25519 signature over the transcript (including the AEAD nonce, so a captured signature can't be replayed against a different ciphertext).

**Sender authentication.** Each device has an Ed25519 keypair (`sig_priv` / `sig_pub`) generated on first run. Posts are signed by `sig_priv`; receivers verify against the sender's pinned `sig_pub`. Global TOFU (`~/.config/nomnom/known_peers.json`) gates first sightings; subsequent feeds where the same identity appears are silent.

**Worker auth.** Two layers:
- `POST /feeds` (mint) is gated by the per-deployment HMAC secret. Only people with your relay HMAC can create feeds on your Worker.
- `/feeds/:id/*` (member roster, slots, extend, close) is gated by a per-request signature derived from the feed key via HKDF. Anyone with the URL can talk to the Worker about that feed; nothing else on the relay is reachable.

This split means a feed URL can be safely shared cross-account: the recipient gets access to that one feed without seeing or holding your relay credential. Each slot lives until feed TTL (default 1 day); the R2 bucket lifecycle collects orphans after 1 day.

</details>

<details>
<summary><b>Config commands</b></summary>

| Command | Effect |
| --- | --- |
| `nomnom relay init` | Prompt URL, generate HMAC secret, save to `relay.json`, print the `wrangler secret put` + `wrangler deploy` commands. |
| `nomnom relay show [--token]` | Print URL (secret redacted). `--token` prints the `host#secret` token used to give another device permission to mint feeds on your relay. |
| `nomnom relay test` | Round-trip: HMAC self-check via `/health` + `/slots`. |
| `nomnom relay clear` | Delete `~/.config/nomnom/relay.json`. |
| `nomnom open [--name N] [--ttl S] [--default]` | Mint a feed on the configured relay. |
| `nomnom join <url> [--name N] [--default]` | Join a feed; auto-publishes a member card. |
| `nomnom feeds list / members / url / default / rename / leave / extend` | Local + remote feed management. |
| `nomnom send <path> [--feed N]` | Broadcast a file to a feed. |
| `nomnom receive [--feed N] [--once]` | Watch a feed for incoming posts. |
| `nomnom peers list / fingerprint / forget` | Global identity pin management. |

</details>

<details>
<summary><b>Reset all state</b></summary>

`nomnom reset` wipes `~/.config/nomnom/` after a y/N prompt — identity, pinned identities, joined feeds, and relay config in one shot. Refuses on a non-tty stdin so a piped invocation can't accidentally blow it away.

```sh
nomnom reset
# about to delete /Users/you/.config/nomnom and N entries
# (identity.json, known_peers.json, feeds.json, relay.json).
# this wipes identity, pinned identities, joined feeds, and relay config.
# proceed? [y/N]: y
```

After reset the next `nomnom relay init` (or `nomnom open` / `nomnom join`) regenerates a fresh identity. Other devices that previously had this identity pinned will see a TOFU prompt on the next feed-shared post.

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
