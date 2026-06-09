import { test, expect, type Page } from "@playwright/test";
import { mkdtempSync, truncateSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Offline, secret-free smoke test of the nomnom-web feed-timeline UI. We never
// hit the relay: the app is usable with no relay (join-only), and feed state is
// seeded into localStorage directly (shapes mirror src/state/persistence.ts)
// instead of opening/joining over the network. See playwright.config.ts for why
// interop lives elsewhere.

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
      auto_save: false,
    },
  ],
});

interface SeedOptions {
  withFeed?: boolean;
  withRelay?: boolean;
  selectFeed?: string | null;
}

/** Seed a device. `withFeed` adds the `home` feed; `withRelay` adds a relay;
 * `selectFeed` writes the last-selected-feed pointer (drives initial pane). */
async function seed(
  page: Page,
  { withFeed = true, withRelay = false, selectFeed = null }: SeedOptions = {},
): Promise<void> {
  await page.addInitScript(
    ([identity, feeds, relay, hasFeed, hasRelay, sel]) => {
      localStorage.setItem("nomnom:schema", "2");
      localStorage.setItem("nomnom:identity", identity as string);
      if (hasFeed) localStorage.setItem("nomnom:feeds", feeds as string);
      if (hasRelay) localStorage.setItem("nomnom:relay", relay as string);
      if (sel) localStorage.setItem("nomnom:lastSelectedFeed", JSON.stringify(sel));
    },
    [IDENTITY, FEEDS, RELAY, withFeed, withRelay, selectFeed] as const,
  );
}

test.describe("nomnom-web feed-timeline UI smoke (offline, secret-free)", () => {
  test("with no feeds and no relay, the warm empty pane is the right pane", async ({ page }) => {
    await seed(page, { withFeed: false });
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "a warm place to drop a file" })).toBeVisible();
    await expect(page.getByText(/open a feed \(you host\)/)).toBeVisible();
    // Without a relay, opening is gated.
    await expect(page.getByText(/opening a feed needs a relay/)).toBeVisible();
    // Rail still offers join even without a relay.
    await expect(page.getByRole("button", { name: "+ join" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "+ open" })).toBeDisabled();
  });

  test("a seeded feed appears in the rail and opens into the timeline pane", async ({ page }) => {
    await seed(page);
    await page.goto("/");

    // Rail shows the feed; nothing selected yet → empty pane on the right.
    const feedRow = page.getByRole("button", { name: /home/ });
    await expect(feedRow).toBeVisible();
    await expect(page.getByRole("heading", { name: "a warm place to drop a file" })).toBeVisible();

    await feedRow.click();

    // Feed view header + empty timeline state.
    await expect(page.getByRole("heading", { name: "home" })).toBeVisible();
    await expect(page.getByText("nothing here yet.")).toBeVisible();
    // Other-members count rendered.
    await expect(page.getByText("1 other")).toBeVisible();

    // Members footer collapses by default; expanding lists members.
    await page.getByText(/members \(2\)/).click();
    await expect(page.getByText("test-peer")).toBeVisible();
    await expect(page.getByLabel(/auto-save files from this feed/)).not.toBeChecked();
  });

  test("the composer rejects a file over the 100 MB cap", async ({ page }) => {
    await seed(page, { selectFeed: "home" });
    await page.goto("/");

    // Feed pre-selected via lastSelectedFeed.
    await expect(page.getByRole("heading", { name: "home" })).toBeVisible();
    const sendBtn = page.getByRole("button", { name: /serve it up/ });
    await expect(sendBtn).toBeDisabled(); // nothing staged yet

    // A sparse file just over the cap — the dropzone reads only `file.size` on
    // the reject path (no bytes touched), so this costs no real I/O.
    const dir = mkdtempSync(join(tmpdir(), "nomnom-e2e-"));
    const tooBig = join(dir, "too-big.bin");
    writeFileSync(tooBig, "");
    truncateSync(tooBig, MAX_PAYLOAD_BYTES + 1);
    await page.locator('input[type="file"]').setInputFiles(tooBig);

    await expect(page.getByText(/too big/)).toBeVisible();
    await expect(sendBtn).toBeDisabled();
  });

  test("factory reset wipes the seeded feed back to the empty pane", async ({ page }) => {
    await seed(page);
    await page.goto("/");

    await page.getByRole("button", { name: "settings" }).click();
    await page.getByRole("button", { name: "reset this device" }).click();
    await page.getByRole("button", { name: "wipe everything" }).click();

    await expect(page.getByRole("heading", { name: "a warm place to drop a file" })).toBeVisible();
    await expect(page.getByText("no feeds yet.")).toBeVisible();
  });

  test("lastSelectedFeed restores the feed view after a reload", async ({ page }) => {
    await seed(page, { selectFeed: "home" });
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "home" })).toBeVisible();

    await page.reload();
    await expect(page.getByRole("heading", { name: "home" })).toBeVisible();
  });
});
