# nomnom-relay

A Cloudflare Worker that brokers encrypted file transfers for [nomnom](../README.md).

The Worker is a blind board. Each feed is one **SQLite-backed Durable Object** holding an append-only index of posts (a monotonic `seq` cursor), the member roster, the live connections, and a daily alarm that enforces the 30-day retention window. Ciphertext bodies live in **R2** under `feeds/<id>/slots/<slot_id>`; nothing ever lists the bucket. The CLI long-polls the index; the browser client holds a hibernatable **WebSocket** that the object pushes `post` / `member` / `deleted` frames over. All cryptography (identity keys, feed keys, AEAD ciphertext) happens client-side; the Worker only sees ciphertext and opaque ids.

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
nomnom relay test
# relay ok (RTT 142ms)
```

`relay test` mints a throwaway feed, posts 1 KB through it, reads it back, and deletes the feed, so it exercises both auth schemes and storage end to end.

### Upgrading from the R2-indexed relay

Earlier versions kept the post index in R2 object listings and relied on a bucket lifecycle rule for expiry. This version is a clean cut: old feeds are not migrated, and the lifecycle rule must go (the object's alarm now owns expiry, and a bucket rule would delete bodies out from under live index rows).

```sh
npx wrangler r2 bucket lifecycle remove nomnom-relay --name nomnom-cleanup
npx wrangler r2 bucket lifecycle list nomnom-relay        # should be empty
# wipe leftover objects (or delete + recreate the bucket)
npx wrangler r2 object delete nomnom-relay --prefix feeds/ --force
npx wrangler deploy
```

Then re-run `nomnom init` on one device and `nomnom join` (or paste the secret into the web app) on the others. Old clients and old `feeds.json` / browser state are ignored.

## Onboarding other devices

A join URL is per-feed, not per-relay. The first device mints a feed and shares the URL; other devices paste it. The relay HMAC stays on the device(s) that mint feeds.

```sh
# device 1 (with relay configured)
nomnom init
# channel created. paste this secret on your other devices:
# https://relay.your-subdomain.workers.dev/f/k4n2pX9qLm3T

# device 2 (no relay setup needed)
nomnom join 'https://relay.your-subdomain.workers.dev/f/k4n2pX9qLm3T'
```

If you want to give another device permission to mint feeds on the same relay (i.e. fully share the deployment), copy `relay.json` over or hand them the relay-HMAC token from `nomnom relay show --token`. Most users won't need this — one minting device + many joiners is the usual shape.

## Endpoints

Two auth schemes:

- **Relay HMAC** (per-deployment secret) gates `GET /auth` and `POST /feeds`:

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

Either way, `unix_ts` must be within ±300 seconds of the Worker's clock. The `/feeds/:id/ws` endpoint also accepts the feed-key signature as an `?auth=<ts>:<mac>` query parameter (a browser WebSocket can't set headers); the signed message is unchanged — the MAC still covers the bare pathname.

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET` | `/health` | none | `200 "ok"` |
| `GET` | `/auth` | relay HMAC | `204` if the signature verifies. Lets a client check a pasted secret without minting. |
| `POST` | `/feeds` | relay HMAC | Body: `{member_card}`. Returns `{feed_id, created_at}`. |
| `GET` | `/feeds/:id/meta` | feed-key | `{created_at, last_used_at}` or 404. |
| `DELETE` | `/feeds/:id` | feed-key | Closes the feed: rows, bodies, sockets, alarm. |
| `PUT` | `/feeds/:id/members/:mid` | feed-key | Publish a member card (`identity_pubkey`, `name`). Re-PUT to rename. 409 `feed-full` past 64 members. |
| `DELETE` | `/feeds/:id/members/:mid` | feed-key | Leave (deletes the card). |
| `GET` | `/feeds/:id/members?wait=&since=` | feed-key | `{members, fresh}`; long-polls up to `wait` ms until a card newer than `since` (a `joined_at` timestamp) appears. |
| `PUT` | `/feeds/:id/slots/:slot_id` | feed-key | Post a body. Streams through the object into R2, then indexes it. 409 on a duplicate id. Broadcast: no delete-on-read. |
| `GET` | `/feeds/:id/slots/:slot_id` | feed-key | Read a body straight from R2. 404 if never posted, deleted, or aged out. |
| `DELETE` | `/feeds/:id/slots/:slot_id` | feed-key | Hard-delete a post for everyone (row + body). 404 if absent. |
| `GET` | `/feeds/:id/slots?wait=&since=` | feed-key | `{slots: [{seq, slot_id, created_at}]}` with `seq > since`, ascending. Long-polls up to `wait` ms (max 30 s) when empty. |
| `GET` | `/feeds/:id/ws?since=&auth=` | feed-key | WebSocket. On connect, replays `post` frames with `seq > since`, then pushes live. Send `"ping"`; the edge answers `"pong"` without waking the object. |

Frames on the socket are JSON text:

```
{"type":"post","seq":12,"slot_id":"…","created_at":1758400000}
{"type":"member","action":"join"|"leave","member":{"member_id":"…","identity_pubkey":"…","name":"…","joined_at":…}}
{"type":"deleted","slot_id":"…"}
```

A feed that is purged (closed, or idle for 30 days) closes its sockets with code `1000 "feed-deleted"`; a reconnect then gets 404.

`feed_id` matches `[A-Za-z0-9_-]{8,32}`; `member_id` matches `[A-Za-z0-9_-]{8,64}`; `slot_id` matches `[A-Za-z0-9_-]{1,128}`. Bodies are capped at 256 MB (`411` without a `Content-Length`).

The HMAC and feed-key signatures authenticate clients to the Worker; they do not vouch for posted bodies. Body integrity + sender authenticity come from nomnom's AEAD wrapper + Ed25519 signature inside the post — the receiver's decrypt fails on tampering and the signature catches impersonation by URL holders.

## Retention

Each feed's object schedules a daily alarm at mint. Every run deletes posts older than 30 days (rows first, then their R2 bodies). If no posts remain and the feed has had no authenticated request for 30 days, the object deletes its own storage and the feed is gone; the next request to that id gets 404. An active channel never ages out. There is no bucket lifecycle rule and none should be added.

## Rotating the relay HMAC

Wipe the local config on one device and re-run `nomnom relay init` to generate a fresh secret, then push + deploy it. Other devices keep working without changes — feed-key signatures don't depend on the HMAC, so existing feeds stay reachable. Only minting (`nomnom init`) needs the new secret.

```sh
nomnom relay clear
nomnom relay init
# follow the printed wrangler commands to push the new secret and deploy
```

There is no `nomnom relay rotate-secret` command. Rotation is intentionally manual because it requires coordination with any other device that mints feeds on this deployment.

## Cost and limits

Cloudflare free tier (as of 2026):

- **Workers Free:** 100,000 requests/day, **100 MB request body cap**. Long-poll and WebSocket idle time do not count against CPU.
- **R2 Free:** 10 GB storage, 1M class-A operations/month (writes), 10M class-B operations/month (reads). The relay never lists the bucket, so cost scales with posts, not history.
- **Durable Objects (SQLite):** 5M row reads/day, 100k row writes/day, 5 GB total. A post is one row write; `last_used_at` is written at most once per hour per feed; the alarm is one write per day per feed. Idle WebSockets hibernate: an open tab that receives nothing costs nothing.

For personal use across a few devices, transferring under 100 MB per file, you will not approach any of these limits.

To transfer files **between 100 MB and 256 MB**, you need the Workers Paid plan ($5/month) — the 100 MB body cap is enforced at Cloudflare's edge before the request reaches your Worker.

## What the relay sees

- Ciphertext (opaque). The Worker cannot decrypt your files.
- Feed ids in URLs and request logs, and the roster (member ids, names, identity public keys) in each feed's SQLite. An adversary with read access to the object's storage sees who participates in which feed and when — they cannot tell what was sent.
- Source IPs in Cloudflare's standard request logs.

The relay has two credentials:

- The **HMAC secret** is a *deployment* credential. Anyone with `relay.json` can mint new feeds on your Worker. Treat it like an SSH key for the relay.
- A **feed URL** is a *per-feed* credential. Anyone with it can read, post, and delete posts in that feed for as long as the feed lives, but can't touch any other feed or mint new ones. Sharing a feed URL is intentionally a much smaller surface than sharing relay creds.

Sender authentication inside feeds is handled by nomnom's Ed25519 signatures + TOFU identity pins (`~/.config/nomnom/known_peers.json`), independently of the relay.

## Local development

```sh
echo 'NOMNOM_HMAC_SECRET=dev-secret' > .dev.vars
npx wrangler dev
# serves at http://localhost:8787
# you can hit /health without auth; everything else needs a signature
```

`wrangler dev` runs the Durable Object and an R2 stand-in locally. It does not connect to your production bucket by default; see Cloudflare docs for `--remote` mode if you want to test against real R2.

Tests run in the Workers runtime via `@cloudflare/vitest-pool-workers`, reading bindings from `wrangler.toml`:

```sh
npm test
```
