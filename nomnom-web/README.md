# nomnom-web

A browser sender / receiver / pair client for [nomnom](../README.md), speaking the
same relay protocol as the Python CLI with **full crypto interop** — a transfer
composed in the browser decrypts on a CLI Mac and vice-versa. The browser is just
another nomnom device with its own identity.

Stack: Vite + React + TypeScript. All crypto runs in a Web Worker. Deployed to
Cloudflare Pages at `nomnom.spencerjireh.com`.

## How it works

- **Crypto** (`src/crypto/`) is a byte-for-byte TypeScript port of `nomnom.py`:
  RFC 3526 group-14 triple-DH, the `seal_bytes`/`open_bytes` AEAD (scrypt +
  HMAC-SHA256 keystream + encrypt-then-MAC), slot derivation, first-contact scrypt
  binding, and the relay HMAC auth header. `@noble/hashes` provides sha256/hmac/
  scrypt; BigInt modexp is hand-rolled.
- **Web Worker** (`src/worker/`) runs scrypt, the stream cipher over big files
  (up to 100 MB), and modexp off the main thread, reporting progress.
- **Orchestration** (`src/orchestration/`) implements send / receive / pair as
  framework-free state machines over a thin relay HTTP client (`src/relay/`).
- **UI** (`src/components/`, zustand store in `src/state/`) is a tabbed
  "diner-receipt" interface: Send / Receive / Pair / Peers + Settings.

Identity, TOFU pins, and the relay passphrase live in `localStorage`, shapes
matching the CLI's `identity.json` / `known_peers.json`. **Accepted tradeoff:** the
identity private key and relay secret are readable by any script on this origin —
the same trust model as the CLI's `~/.config/nomnom` files. The site ships a strict
CSP (`public/_headers`) and no third-party scripts to keep the origin clean.

## Develop

```sh
npm install
npm run dev          # http://localhost:5173 — hits the live relay (allowlisted in the Worker CORS)
npm run typecheck
npm test             # crypto interop vectors (vitest)
npm run test:e2e     # Playwright UI smoke (offline; builds + previews on :4173)
npm run build        # tsc --noEmit && vite build -> dist/
```

`npm run dev` talks to the production relay; the Worker's CORS allowlist includes
`http://localhost:5173`.

## Tests

Three layers, each proving a different thing:

- **`npm test`** — cross-language crypto vectors (vitest). Proves the TS port
  reproduces `nomnom.py` byte-for-byte and decrypts a Python-sealed blob.
- **`npm run test:e2e`** — Playwright UI smoke (`e2e/`). Drives the built app
  through `vite preview`, fully **offline and secret-free**: onboarding's relay
  health probe is mocked and every screen is reached from `localStorage`, so it
  guards the UI + state wiring without touching the relay. First run needs
  `npx playwright install chromium`.
- **Manual CLI↔browser round-trip** — the real interop proof, run by hand against
  the live relay: pair the browser with `nomnom pair`, send a file each way, and
  checksum-compare. Not in CI (it needs the relay passphrase + a running CLI).

## Crypto interop fixtures

`test/fixtures/crypto-vectors.json` is generated from `nomnom.py` (the source of
truth) and asserts the TS port reproduces the exact bytes + can decrypt a
Python-sealed blob. Regenerate after any change to the CLI crypto:

```sh
npm run gen:fixtures      # uv run python ../tools/gen_crypto_fixtures.py ...
# or, from the repo root:
make fixtures
```

CI runs `fixtures-no-drift`: it regenerates and fails on any diff, so the Python
crypto and the TS port can't silently diverge.

## Deploy (Cloudflare Worker — static assets)

Hosted as a static-assets Worker bound to `nomnom.spencerjireh.com` (see
`wrangler.toml`). We use a Worker rather than Pages because `wrangler deploy`
auto-provisions the custom domain's DNS + TLS through the Workers API — the same
path the relay Worker uses for `relay.spencerjireh.com` — so the whole deploy is
one CLI command with no dashboard step. (A Pages custom domain needs a separate
DNS record that a `pages:write`/`zone:read` token can't create.) Workers Static
Assets honors `dist/_headers`, so the strict CSP ships unchanged.

```sh
npm run build
npx wrangler deploy
```

No build-time env is required — the relay URL is hardcoded in `src/config.ts` and
the passphrase is entered at runtime. The `spencerjireh.com` zone is already on
Cloudflare, so the custom domain + cert are provisioned automatically on first
deploy.
