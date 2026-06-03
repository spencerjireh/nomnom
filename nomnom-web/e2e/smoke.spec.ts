import { test, expect, type Page } from "@playwright/test";
import { mkdtempSync, truncateSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Offline, secret-free smoke test of the nomnom-web UI. We never hit the relay:
// the app is usable with no relay (join-only), and feed state is seeded into
// localStorage directly (shapes mirror src/state/persistence.ts) instead of
// opening/joining over the network. See playwright.config.ts for why interop
// lives elsewhere.

const MAX_PAYLOAD_BYTES = 100 * 1024 * 1024; // keep in sync with src/config.ts

const SELF_SIG_PUB = "ab".repeat(32); // 64 hex = one Ed25519 pubkey
const PEER_SIG_PUB = "ef".repeat(32);
const SELF_MEMBER = "5e1f5e1f5e1f5e1f5e1f5e1f5e1f5e1f";
const PEER_MEMBER = "9eef9eef9eef9eef9eef9eef9eef9eef";

const IDENTITY = JSON.stringify({
  device_id: "00112233aabbccdd",
  name: "web-guest",
  sig_priv: "cd".repeat(32),
  sig_pub: SELF_SIG_PUB,
});
const RELAY = JSON.stringify({
  url: "https://relay.spencerjireh.com",
  secret: "not-a-real-secret",
});
const FEEDS = JSON.stringify({
  default: "home",
  feeds: [
    {
      name: "home",
      feed_id: "testfeedtoken00",
      feed_token: "testfeedtoken00",
      url: "https://relay.spencerjireh.com/f/testfeedtoken00",
      expires_at: 4102444800, // year 2100
      joined_at: 1700000000,
      member_id: SELF_MEMBER,
      members_cache: [
        { member_id: SELF_MEMBER, identity_pubkey: SELF_SIG_PUB, name: "web-guest" },
        { member_id: PEER_MEMBER, identity_pubkey: PEER_SIG_PUB, name: "test-peer" },
      ],
      last_post_ts: 0,
    },
  ],
});

/** Seed a device. `withFeed` adds the `home` feed; `withRelay` adds a relay. */
async function seed(
  page: Page,
  { withFeed = true, withRelay = false } = {},
): Promise<void> {
  await page.addInitScript(
    ([identity, feeds, relay, hasFeed, hasRelay]) => {
      localStorage.setItem("nomnom:schema", "2");
      localStorage.setItem("nomnom:identity", identity as string);
      if (hasFeed) localStorage.setItem("nomnom:feeds", feeds as string);
      if (hasRelay) localStorage.setItem("nomnom:relay", relay as string);
    },
    [IDENTITY, FEEDS, RELAY, withFeed, withRelay] as const,
  );
}

test.describe("nomnom-web UI smoke (offline, secret-free)", () => {
  test("loads straight into the tab shell — no relay needed", async ({ page }) => {
    await seed(page, { withFeed: false });
    await page.goto("/");

    await expect(page.getByRole("tab", { name: "feeds" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "send" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "receive" })).toBeVisible();
    // Rail shows we can join but not open without a relay.
    await expect(page.getByText("none (join-only)")).toBeVisible();
    await expect(page.getByText("no feeds yet.")).toBeVisible();
    await expect(page.getByText(/opening a feed needs a relay/)).toBeVisible();
  });

  test("a seeded feed lists in the feeds tab with members", async ({ page }) => {
    await seed(page);
    await page.goto("/");

    await expect(page.getByText("home", { exact: true })).toBeVisible();
    await expect(page.getByText("default", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: /members \(2\)/ }).click();
    await expect(page.getByText("test-peer")).toBeVisible();
  });

  test("send tab targets the feed and rejects a file over the 100 MB cap", async ({ page }) => {
    await seed(page);
    await page.goto("/");
    await page.getByRole("tab", { name: "send" }).click();

    // 1 other member should receive.
    await expect(page.getByText(/1 other member will receive this/)).toBeVisible();

    // A sparse file just over the cap: the app reads only `file.size` on the
    // reject path (never the bytes), so this costs no real I/O.
    const dir = mkdtempSync(join(tmpdir(), "nomnom-e2e-"));
    const tooBig = join(dir, "too-big.bin");
    writeFileSync(tooBig, "");
    truncateSync(tooBig, MAX_PAYLOAD_BYTES + 1);
    await page.locator('input[type="file"]').setInputFiles(tooBig);

    await expect(page.getByText(/too big/)).toBeVisible();
    await expect(page.getByRole("button", { name: /serve it up/ })).toBeDisabled();
  });

  test("send/receive tabs show the empty state with no feeds", async ({ page }) => {
    await seed(page, { withFeed: false });
    await page.goto("/");
    await page.getByRole("tab", { name: "send" }).click();
    await expect(page.getByText(/open or join one in the Feeds tab/)).toBeVisible();
    await page.getByRole("tab", { name: "receive" }).click();
    await expect(page.getByText(/open or join a feed first/)).toBeVisible();
  });

  test("settings reset wipes feeds back to empty", async ({ page }) => {
    await seed(page);
    await page.goto("/");

    await page.getByRole("button", { name: "settings" }).click();
    await page.getByRole("button", { name: "reset this device" }).click();
    await page.getByRole("button", { name: "wipe everything" }).click();

    // Still in the tab shell (no onboarding gate), but the feed is gone.
    await expect(page.getByRole("tab", { name: "feeds" })).toBeVisible();
    await expect(page.getByText("no feeds yet.")).toBeVisible();
  });

  test("feed state survives a reload", async ({ page }) => {
    await seed(page);
    await page.goto("/");
    await expect(page.getByText("home", { exact: true })).toBeVisible();

    await page.reload();
    await expect(page.getByText("home", { exact: true })).toBeVisible();
  });
});
