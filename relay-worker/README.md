# nomnom-relay

A Cloudflare Worker that brokers encrypted file transfers for [nomnom](../README.md).

The Worker is intentionally dumb: it stores opaque payloads at HMAC-authenticated slot ids, holds GETs open for long-polling, and deletes slots on read. All cryptography (identity keys, triple-DH session keys, AEAD ciphertext) happens client-side; the Worker only sees ciphertext + a per-pair rendezvous id.

## Prerequisites

- A Cloudflare account (free tier is enough for personal use — see [limits](#cost-and-limits)).
- Node.js 20+ and `npx` (no global `wrangler` install needed).
- `python3` (the bundled `scripts/generate-secret.sh` invokes it; already required by nomnom).

## Deploy

From the repository root:

```sh
cd relay-worker
npm install
npx wrangler login
npx wrangler r2 bucket create nomnom-relay
```

Generate and push the HMAC secret. The script emits a 6-word diceware passphrase (~62 bits of entropy) like `fend-sage-trash-cod-visa-data` — memorable enough to speak across the room or paste from a password manager when you set up another machine.

```sh
SECRET=$(../scripts/generate-secret.sh)
echo "save this somewhere: $SECRET"
printf '%s' "$SECRET" | npx wrangler secret put NOMNOM_HMAC_SECRET
```

The secret is treated as opaque bytes by both the Worker and the client — if you want to pick your own phrase instead, just `SECRET="…"` and skip the script.

Deploy:

```sh
npx wrangler deploy
```

Wrangler prints the worker URL (e.g. `https://nomnom-relay.your-subdomain.workers.dev`). Verify it's up:

```sh
curl https://nomnom-relay.your-subdomain.workers.dev/health
# ok
```

### Required: R2 lifecycle rule

The Worker writes a `customMetadata.expires_at` timestamp on every slot and refuses to serve expired ones, but objects orphaned by an abandoned sender accumulate until something deletes them. Add a one-day lifecycle rule so the bucket cleans itself:

```sh
npx wrangler r2 bucket lifecycle add nomnom-relay nomnom-cleanup --expire-days 1 --force
```

Verify:

```sh
npx wrangler r2 bucket lifecycle list nomnom-relay
```

This is belt-and-suspenders for the 5-minute protocol TTL. Without it, abandoned slots sit indefinitely.

## Configure nomnom

On each Mac that will use the relay:

```sh
nomnom relay setup
# paste the worker URL
# paste the passphrase from above
```

Confirm end-to-end:

```sh
nomnom relay test
# relay ok (RTT 142ms)
```

## Endpoints

All requests except `/health` require:

```
Authorization: NMNM-HMAC-SHA256 <unix_ts>:<hex_mac>
   mac = HMAC-SHA256(NOMNOM_HMAC_SECRET, method + "\n" + path + "\n" + unix_ts)
```

`unix_ts` must be within ±300 seconds of the Worker's clock.

| Method | Path | Body | Notes |
|---|---|---|---|
| `PUT` | `/slots/:slot_id` | raw bytes | 204 on success; 409 if slot occupied; 413 if Content-Length > 256 MB |
| `GET` | `/slots/:slot_id?wait=<ms>` | — | 200 + body (deletes slot); 404 if empty after wait; 410 if expired. `wait` caps at 30000. |
| `DELETE` | `/slots/:slot_id` | — | 204 (idempotent) — clients call this on cancel |
| `GET` | `/health` | — | 200 "ok"; no HMAC |

`slot_id` matches `[A-Za-z0-9_-]{1,128}`.

The HMAC authenticates clients to the Worker; it does not vouch for the body. Body integrity comes from nomnom's AEAD wrapper inside `slot_data` — the receiver's decrypt fails on tampering.

## Rotating the secret

Generate a new secret, push it, redeploy, then update each client.

```sh
SECRET=$(../scripts/generate-secret.sh)
printf '%s' "$SECRET" | npx wrangler secret put NOMNOM_HMAC_SECRET
npx wrangler deploy
# On every Mac that uses the relay:
nomnom relay set <worker-url> --secret "$SECRET"
```

There is no `nomnom relay rotate-secret` command in this version. Rotation is intentionally manual because it requires coordination across every device using the relay.

## Cost and limits

Cloudflare free tier (as of 2025):

- **Workers Free:** 100,000 requests/day, 30s wall-clock per request, **100 MB request body cap**. Long-polling does not count against CPU.
- **R2 Free:** 10 GB storage, 1M class-A operations/month (writes + lists), 10M class-B operations/month (reads).

For personal use across two Macs, transferring under 100 MB per file, you will not approach any of these limits.

To transfer files **between 100 MB and 256 MB**, you need the Workers Paid plan ($5/month) — the 100 MB body cap is enforced at Cloudflare's edge before the request reaches your Worker.

The Worker itself enforces a 256 MB hard cap regardless of plan.

## What the relay sees

- Ciphertext (opaque). The Worker cannot decrypt your files.
- Slot ids, which deterministically correlate the same peer-pair across transfers. An adversary with read access to your R2 bucket sees "device A and device B exchanged something at time T" — they cannot tell what.
- Source IPs in Cloudflare's standard request logs.

The HMAC secret is a **deployment** credential, not a per-peer credential. Anyone with `relay.json` can publish to slots and consume from slots on your Worker. Peer-to-peer authentication is handled by nomnom's TOFU identity pins, independently of the relay.

## Local development

```sh
npx wrangler dev
# serves at http://localhost:8787
# you can hit /health without auth; everything else needs HMAC
```

`wrangler dev` does not connect to your production R2 bucket by default. See Cloudflare docs for `--remote` mode if you want to test against real R2.
