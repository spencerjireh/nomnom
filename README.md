# nomnom

[![tests](https://github.com/spencerjireh/nomnom/actions/workflows/test.yml/badge.svg)](https://github.com/spencerjireh/nomnom/actions/workflows/test.yml)

Single-file Python CLI that bundles a repo into one `.txt` for an LLM. Stdlib only — no install, no venv, no deps.

It does three things:

- **Bundle** a repo (or git/PR/issue context) into one text file shaped for an LLM.
- **Send** that file between your own machines over an end-to-end encrypted channel.
- **Rebuild** the original tree on the other side.

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

Run bare `nomnom` on a TTY to open a launcher with tiles for Bundle, Send, Receive, Channel, Commit, PR, Item, Rebuild, and Extensions. Inside the picker, `v` cycles bundle / `commit` / `pr` / `item` when run from inside a git repo.

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

`commit` errors on a clean tree. `pr` and `item` require [`gh`](https://cli.github.com); `pr` auto-detects the base.

`nomnom item <id>` figures out what you mean from the id: hex → commit, tag-like → release, plain number → a PR, issue, or workflow run (it auto-routes on a unique hit and asks you to disambiguate otherwise). `discussion` and `job` ids overlap with those, so name them explicitly: `nomnom item discussion <n>` / `nomnom item job <n>`. `pr` and `commit` honor `--diff` (off by default); `run` and `job` show failing-step logs by default, `--all-logs` includes everything. Every verb accepts `--clipboard` or `--stdout`.

## Rebuild

Inverse of bundling — reconstructs the tree under cwd.

```sh
nomnom rebuild foo-20260503-101415.txt   # → ./foo/ (auto-suffixes on collision)
pbpaste | nomnom rebuild                 # from stdin
nomnom rebuild bundle.txt --name scratch # override folder name
```

Never overwrites. Path-escape attempts (absolute paths, `..` segments) are refused. Git-context bundles aren't reconstructable and error out.

## Send between machines

Move a bundle (or any file) between your own devices over an end-to-end encrypted **channel**. No accounts and no shared third-party server — traffic flows through a small Cloudflare Worker you run yourself, and it only ever sees ciphertext.

Your **channel** is one permanent, shared space across all of your own devices. You set it up once on the device that owns the relay; every other device joins it once by pasting a short secret and stays in forever. Anything you `send` reaches every other device. The secret is the password — but even a leaked secret can't impersonate a sender (see [Trust on first use](#trust) below).

Five verbs: `init` · `join` · `channel` · `send` · `receive`.

### Create your channel and add a device

```sh
# device 1 (the one that owns the relay) — first-time setup, run once
nomnom init
# (prompts for the Worker URL + HMAC secret if the relay isn't set up yet,
#  then creates your channel and prints the secret to paste elsewhere:)
# channel created. paste this secret on your other devices (`nomnom join <secret>`):
# https://relay.your-subdomain.workers.dev/f/k4n2pX9qLm3T

# device 2 (nothing to set up on this side — the secret is the credential)
nomnom join 'https://relay.your-subdomain.workers.dev/f/k4n2pX9qLm3T'
# joined the channel (2 device(s)).
```

Re-display the secret (to add a third device) anytime with `nomnom channel`.

### Send and receive

```sh
nomnom receive          # watch your channel; one line per received file
nomnom send report.txt  # send to every other device on your channel
```

`receive` stays open after each delivery, so you can leave a laptop listening and fire off `send` from another machine all afternoon — new posts arrive in real time (the relay pushes them over Server-Sent Events, falling back to long-polling). Pass `--once` to exit after the first file (handy for scripting).

Max transfer size is 256 MB (100 MB on Cloudflare's free tier — see [`relay-worker/README.md`](relay-worker/README.md)).

### From a browser

[`nomnom-web/`](nomnom-web/README.md) is a browser client (Vite + React) that joins the same channel as the CLI — its crypto is a byte-for-byte TypeScript port of `nomnom.py`. It's deployed to Cloudflare Pages, and the relay Worker allowlists its origin for CORS.

<details>
<summary><b>First-time setup: deploy your relay</b></summary>

Creating a channel needs a relay — a small Cloudflare Worker you deploy once to your own account. It only ever sees ciphertext and opaque slot ids.

1. Deploy the Worker — see [`relay-worker/README.md`](relay-worker/README.md).
2. On the device that will own the relay, run `nomnom init`. If the relay isn't configured yet it prompts for the Worker URL, generates the HMAC secret, and prints the `wrangler` commands to push the secret and deploy — then it creates your channel.

Other devices don't need any of this — they just `join` the channel secret. To let another *owner* device create channels on the same relay, copy the token from `nomnom relay show --token` over to it.

</details>

<details>
<summary><b>Your channel</b></summary>

There's one channel, shared across your devices. Inspect it anytime:

```sh
nomnom channel   # print the channel secret (to add another device) + the device roster
```

To leave on a device, `nomnom reset` wipes that device's local state; re-join later with the secret. Re-pair a device by running `nomnom join <secret>` again (it replaces whatever channel that device had).

</details>

<details>
<summary><b><a name="trust"></a>Trust on first use</b></summary>

Every post is encrypted with the channel key derived from the secret's URL token and signed by the sender's Ed25519 identity. Receivers verify the signature against the sender's pinned identity, so a hostile relay (or a leaked secret) can't impersonate a device without its private key.

Each machine mints a long-term Ed25519 identity in `~/.config/nomnom/identity.json` on first run. The first time you see a device's identity on your channel, nomnom prompts:

```text
  first contact with 'bob-laptop'.
    fingerprint: 9c01:7a4f:21bd:0e88
  verify out-of-band if it matters.
  trust and pin this device? [y/N]:
```

Once pinned, that identity is trusted wherever it appears — re-pairing the same device is silent. Possessing the channel secret grants access to the channel, but never impersonates anyone: the signature is the trust anchor, not the secret.

```sh
nomnom peers list                    # show pinned identities + fingerprints
nomnom peers fingerprint alice-mac   # for out-of-band verification
nomnom peers forget alice-mac        # drop a pin (TOFU fires fresh on next sighting)
```

`--trust-new` auto-pins TOFU prompts for scripted callers (loses interactive verification; an audit line is still written to stderr).

</details>

<details>
<summary><b>Crypto</b></summary>

**Feed transport.** Each feed has a 32-byte symmetric key derived via HKDF-SHA256 from the URL token. Every member with the URL derives the same key; the Worker derives it on the fly per request to verify signatures. Posts are encrypted with ChaCha20-style keystream (HMAC-SHA256 as PRF) and authenticated with HMAC-SHA256 over (magic ‖ nonce ‖ ciphertext). The plaintext is `len(header) ‖ JSON header ‖ raw body`; the header carries the sender's member id, identity pubkey, filename, file size, content hash, posted timestamp, and an Ed25519 signature over the transcript (including the AEAD nonce, so a captured signature can't be replayed against a different ciphertext).

**Sender authentication.** Each device has an Ed25519 keypair (`sig_priv` / `sig_pub`) generated on first run. Posts are signed by `sig_priv`; receivers verify against the sender's pinned `sig_pub`. Global TOFU (`~/.config/nomnom/known_peers.json`) gates first sightings; subsequent feeds where the same identity appears are silent.

**Worker auth.** Two layers:
- `POST /feeds` (mint) is gated by the per-deployment HMAC secret. Only people with your relay HMAC can create feeds on your Worker.
- `/feeds/:id/*` (member roster, slots, extend, close) is gated by a per-request signature derived from the feed key via HKDF. Anyone with the URL can talk to the Worker about that feed; nothing else on the relay is reachable.

This split means the channel secret can be safely shared cross-account: the recipient gets access to that one channel without seeing or holding your relay credential. Slots live until the channel's TTL (a channel is minted with a multi-year TTL, so it's effectively permanent); the R2 bucket lifecycle collects orphans after 1 day. (The wire protocol still calls a channel a "feed" — `/feeds/:id/*` — for back-compat with the original feeds-v2 transport.)

</details>

<details>
<summary><b>All send/receive commands</b></summary>

| Command | Effect |
| --- | --- |
| `nomnom init` | First-device setup: configure the relay if needed (prompts URL, generates the HMAC secret, prints the `wrangler` commands), then create your one permanent channel and print its secret. |
| `nomnom join <secret>` | Add this device to your channel by pasting its secret; replaces any channel already on this device. |
| `nomnom channel` | Print the channel secret (to add a device) and the device roster. |
| `nomnom send <path>` | Send a file to every other device on your channel. |
| `nomnom receive [--once]` | Watch your channel for incoming files. |
| `nomnom relay show [--token]` | Print URL (secret redacted). `--token` prints the `host#secret` token, which lets another *owner* device run `nomnom init` against the same relay. |
| `nomnom relay test` | Round-trip: HMAC self-check via `/health` + `/slots`. |
| `nomnom relay clear` | Delete `~/.config/nomnom/relay.json`. |
| `nomnom peers list / fingerprint / forget` | Global identity pin management. |

</details>

<details>
<summary><b>Reset all state</b></summary>

`nomnom reset` wipes `~/.config/nomnom/` after a y/N prompt — identity, pinned identities, the channel, and relay config in one shot. Refuses on a non-tty stdin so a piped invocation can't accidentally blow it away.

```sh
nomnom reset
# about to delete /Users/you/.config/nomnom and N entries
# (identity.json, known_peers.json, feeds.json, relay.json).
# this wipes identity, pinned identities, joined feeds, and relay config.
# proceed? [y/N]: y
```

After reset the next `nomnom init` (or `nomnom join <secret>`) regenerates a fresh identity. Other devices that previously had this identity pinned will see a TOFU prompt on its next post to the channel.

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
