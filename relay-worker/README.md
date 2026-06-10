# nomnom-relay

A Cloudflare Worker that brokers encrypted file transfers for [nomnom](../README.md).

The Worker is intentionally dumb: it stores opaque payloads at HMAC-authenticated slot ids and holds GETs open for long-polling. For feeds, receivers can instead subscribe to a Server-Sent Events stream (`GET /feeds/:id/stream`) backed by a per-feed **Durable Object**, which pushes new-slot notifications in real time rather than long-polling R2 (the long-poll stays as a fallback). All cryptography (identity keys, feed/session keys, AEAD ciphertext) happens client-side; the Worker only sees ciphertext + opaque slot ids.

## Prerequisites

- A Cloudflare account (free tier is enough for personal use — see [limits](#cost-and-limits)).
- Node.js 20+ and `npx` (no global `wrangler` install needed).
- `python3` (already required by nomnom).

## Deploy

The fastest path is to let `nomnom relay init` generate the HMAC secret for you and print the exact `wrangler` commands. From the repository root:

```sh
cd relay-worker
npm install
npx wrangler login
npx wrangler r2 bucket create nomnom-relay
```

Then on the machine that will be the first nomnom client, run:

```sh
nomnom relay init
# paste the Worker URL when prompted (e.g. https://nomnom-relay.your-subdomain.workers.dev)
```

`relay init` generates a random ~72-bit secret, saves it locally, and prints the exact commands to push it to the Worker and deploy:

```sh
echo 'GENERATED_SECRET' | npx wrangler secret put NOMNOM_HMAC_SECRET
npx wrangler deploy
```

Run those in the `relay-worker/` directory. The Worker treats the secret as opaque bytes — if you'd rather generate it yourself, set `SECRET=...` and skip `relay init`.

Verify the deploy:

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

## Onboarding other devices

In feeds v2 a join URL is per-feed, not per-relay. The first device mints a feed and shares the URL; other devices paste it. The relay HMAC stays on the device(s) that mint feeds.

```sh
# device 1 (with relay configured)
nomnom open --name home --default
# opened feed 'home' (TTL 86400s).
# share this URL with the other device:
# https://relay.your-subdomain.workers.dev/f/k4n2pX9qLm3T

# device 2 (no relay setup needed)
nomnom join 'https://relay.your-subdomain.workers.dev/f/k4n2pX9qLm3T'
```

If you want to give another device permission to mint feeds on the same relay (i.e. fully share the deployment), copy `relay.json` over or hand them the relay-HMAC token from `nomnom relay show --token`. Most users won't need this — one minting device + many joiners is the usual shape.

Verify end-to-end at any time:

```sh
nomnom relay test
# relay ok (RTT 142ms)
```

## Endpoints

Two auth schemes:

- **Relay HMAC** (per-deployment secret) gates `POST /feeds` and the legacy `/slots/*` paths:

  ```
  Authorization: NMNM-HMAC-SHA256 <unix_ts>:<hex_mac>
     mac = HMAC-SHA256(NOMNOM_HMAC_SECRET, method + "\n" + path + "\n" + unix_ts)
  ```

- **Feed-key signature** (per-feed key derived from the URL token) gates `/feeds/:id/*`:

  ```
  Authorization: NMNM-FEEDKEY-SHA256 <unix_ts>:<hex_mac>
     feed_key = HKDF-SHA256(salt="nomnom-feed-v1", ikm=urlsafeB64(feed_id), info=feed_id, length=32)
     mac      = HMAC-SHA256(feed_key, method + "\n" + path + "\n" + unix_ts)
  ```

Either way, `unix_ts` must be within ±300 seconds of the Worker's clock. The SSE
`/feeds/:id/stream` endpoint also accepts the feed-key signature as an
`?auth=<ts>:<mac>` query parameter (since `EventSource` can't set headers); the
signed message is unchanged — the MAC still covers the bare pathname.

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET` | `/health` | none | `200 "ok"` |
| `POST` | `/feeds` | relay HMAC | Body: `{ttl_seconds, member_card}`. Returns `{feed_id, created_at, expires_at}`. |
| `GET` | `/feeds/:id/meta` | feed-key | Returns `{created_at, expires_at}` or 404/410. |
| `POST` | `/feeds/:id/extend` | feed-key | Body: `{new_ttl_seconds}`. Anyone with the URL can extend. |
| `DELETE` | `/feeds/:id` | feed-key | Closes the feed and purges its members + slots. |
| `PUT` | `/feeds/:id/members/:mid` | feed-key | Publish a member card (identity_pubkey, name). |
| `DELETE` | `/feeds/:id/members/:mid` | feed-key | Leave (deletes the card). |
| `GET` | `/feeds/:id/members?wait=&since=` | feed-key | List roster, long-poll on `wait` until a new joiner appears since `since`. |
| `PUT` | `/feeds/:id/slots/:slot_id` | feed-key | Write a post. Slots live until feed TTL; broadcast (no delete-on-read). |
| `GET` | `/feeds/:id/slots/:slot_id?wait=` | feed-key | Long-poll fetch. |
| `GET` | `/feeds/:id/slots?wait=&since=` | feed-key | List new slot ids since `since`, long-poll on `wait`. |
| `GET` | `/feeds/:id/stream?since=&auth=` | feed-key | Subscribe (SSE) to new-slot notifications since `since`, pushed live by a per-feed Durable Object. Replays the backlog on connect; self-closes at ~4 min so clients reconnect with a fresh signature. Auth may ride the `?auth=` query. |
| `PUT` | `/slots/:slot_id` | relay HMAC | Legacy: single-shot slot, delete-on-read. |
| `GET` | `/slots/:slot_id?wait=` | relay HMAC | Legacy: long-poll + delete-on-read. |
| `DELETE` | `/slots/:slot_id` | relay HMAC | Legacy: idempotent cancel. |

`feed_id` matches `[A-Za-z0-9_-]{8,32}`; `member_id` matches `[A-Za-z0-9_-]{8,64}`; `slot_id` matches `[A-Za-z0-9_-]{1,128}`.

The HMAC and feed-key signatures authenticate clients to the Worker; they do not vouch for posted bodies. Body integrity + sender authenticity come from nomnom's AEAD wrapper + Ed25519 signature inside the slot payload — the receiver's decrypt fails on tampering and the signature catches impersonation by URL holders.

## Rotating the relay HMAC

Wipe the local config on one device and re-run `nomnom relay init` to generate a fresh secret, then push + deploy it. Other devices keep working without changes — feed-key signatures don't depend on the HMAC, so existing feeds stay reachable. Only minting (`nomnom open`) needs the new secret.

```sh
nomnom relay clear
nomnom relay init
# follow the printed wrangler commands to push the new secret and deploy
```

There is no `nomnom relay rotate-secret` command. Rotation is intentionally manual because it requires coordination with any other device that mints feeds on this deployment.

## Cost and limits

Cloudflare free tier (as of 2025):

- **Workers Free:** 100,000 requests/day, 30s wall-clock per request, **100 MB request body cap**. Long-polling does not count against CPU.
- **R2 Free:** 10 GB storage, 1M class-A operations/month (writes + lists), 10M class-B operations/month (reads).
- **Durable Objects:** one SQLite-backed instance per active feed, provisioned automatically by `wrangler deploy` (the `FeedNotifier` migration in `wrangler.toml`). An open SSE `/stream` connection keeps its feed's instance active while connected; for a handful of personal devices this is negligible.

For personal use across two Macs, transferring under 100 MB per file, you will not approach any of these limits.

To transfer files **between 100 MB and 256 MB**, you need the Workers Paid plan ($5/month) — the 100 MB body cap is enforced at Cloudflare's edge before the request reaches your Worker.

The Worker itself enforces a 256 MB hard cap regardless of plan.

## What the relay sees

- Ciphertext (opaque). The Worker cannot decrypt your files.
- Feed ids in URLs and request logs. Feed members publish (encrypted) cards under `/feeds/<id>/members/...`, so an adversary with read access to your R2 bucket sees who participates in which feed and when — they cannot tell what was sent.
- Source IPs in Cloudflare's standard request logs.

The relay has two credentials:

- The **HMAC secret** is a *deployment* credential. Anyone with `relay.json` can mint new feeds and use the legacy `/slots/*` endpoints on your Worker. Treat it like an SSH key for the relay.
- A **feed URL** is a *per-feed* credential. Anyone with it can read and post in that feed for as long as the feed lives, but can't touch any other feed or mint new ones. Sharing a feed URL is intentionally a much smaller surface than sharing relay creds.

Sender authentication inside feeds is handled by nomnom's Ed25519 signatures + TOFU identity pins (`~/.config/nomnom/known_peers.json`), independently of the relay.

## Local development

```sh
npx wrangler dev
# serves at http://localhost:8787
# you can hit /health without auth; everything else needs HMAC
```

`wrangler dev` does not connect to your production R2 bucket by default. See Cloudflare docs for `--remote` mode if you want to test against real R2.
