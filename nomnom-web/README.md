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
npm run build        # tsc --noEmit && vite build -> dist/
```

`npm run dev` talks to the production relay; the Worker's CORS allowlist includes
`http://localhost:5173`.

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

## Deploy (Cloudflare Pages)

The `spencerjireh.com` zone is already on Cloudflare (the relay Worker uses
`relay.spencerjireh.com`).

**Git-connected (recommended):** create a Pages project, set
- Root directory: `nomnom-web`
- Build command: `npm run build`
- Build output directory: `dist`

No build-time env is required — the relay URL is hardcoded in `src/config.ts` and
the passphrase is entered at runtime. Then add `nomnom.spencerjireh.com` under the
project's **Custom domains** tab; Cloudflare provisions the CNAME + cert.

**Direct upload:**

```sh
npm run build
npx wrangler pages deploy dist --project-name nomnom-web
```

`public/_headers` ships the CSP and security headers with the build.
