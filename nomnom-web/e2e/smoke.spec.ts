import { test, expect, type Page } from "@playwright/test";
import { mkdtempSync, truncateSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Offline, secret-free smoke test of the nomnom-web single-channel UI. We never
// hit the relay: the app is usable with no relay (join/receive only), and channel
// state is seeded into localStorage directly (shapes mirror src/state/persistence.ts)
// instead of creating/joining over the network. See playwright.config.ts for why
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
// The single channel = one stored Feed object under nomnom:channel.
const CHANNEL = JSON.stringify({
  name: "channel",
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
});

interface SeedOptions {
  withChannel?: boolean;
  withRelay?: boolean;
}

/** Seed a device. `withChannel` adds the channel; `withRelay` adds a relay. */
async function seed(
  page: Page,
  { withChannel = true, withRelay = false }: SeedOptions = {},
): Promise<void> {
  await page.addInitScript(
    ([identity, channel, relay, hasChannel, hasRelay]) => {
      localStorage.setItem("nomnom:schema", "2");
      localStorage.setItem("nomnom:identity", identity as string);
      if (hasChannel) localStorage.setItem("nomnom:channel", channel as string);
      if (hasRelay) localStorage.setItem("nomnom:relay", relay as string);
    },
    [IDENTITY, CHANNEL, RELAY, withChannel, withRelay] as const,
  );
}

test.describe("nomnom-web single-channel UI smoke (offline, secret-free)", () => {
  test("with no channel and no relay, the bootstrap pane offers paste-to-join", async ({ page }) => {
    await seed(page, { withChannel: false });
    await page.goto("/");

    await expect(
      page.getByRole("heading", { name: "add this device to your channel" }),
    ).toBeVisible();
    // The channel-secret input + join button are the primary path.
    await expect(page.getByPlaceholder(/\/f\/<token>/)).toBeVisible();
    await expect(page.getByRole("button", { name: "join" })).toBeDisabled(); // empty input
    // Without a relay, creating a channel is gated.
    await expect(page.getByText(/creating a channel needs a relay/)).toBeVisible();
    // The rail shows there's no channel yet.
    await expect(page.getByText("no channel yet.")).toBeVisible();
  });

  test("a seeded channel auto-opens into the timeline pane", async ({ page }) => {
    await seed(page);
    await page.goto("/");

    // No selection step — the one channel shows immediately.
    await expect(page.getByRole("heading", { name: "your channel" })).toBeVisible();
    await expect(page.getByText("nothing here yet.")).toBeVisible();
    // Other-device count rendered.
    await expect(page.getByText("1 other device")).toBeVisible();

    // Devices footer collapses by default; expanding lists devices.
    await page.getByText(/devices \(2\)/).click();
    await expect(page.getByText("test-peer")).toBeVisible();
    await expect(page.getByLabel(/auto-save files from this channel/)).not.toBeChecked();
  });

  test("the composer rejects a file over the 100 MB cap", async ({ page }) => {
    await seed(page);
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "your channel" })).toBeVisible();
    const sendBtn = page.getByRole("button", { name: "send", exact: true });
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

  test("the rail collapses, persists across reload, and expands again", async ({ page }) => {
    await seed(page);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "your channel" })).toBeVisible();

    const rail = page.getByRole("complementary", { name: "channel" });
    await expect(rail).toBeVisible();

    await page.getByRole("button", { name: "hide sidebar" }).click();
    await expect(rail).toBeHidden();
    await expect(page.getByRole("button", { name: "show sidebar" })).toBeVisible();

    // The preference survives a reload.
    await page.reload();
    await expect(page.getByRole("heading", { name: "your channel" })).toBeVisible();
    await expect(rail).toBeHidden();

    await page.getByRole("button", { name: "show sidebar" }).click();
    await expect(rail).toBeVisible();
  });

  test("factory reset wipes the seeded channel back to the bootstrap pane", async ({ page }) => {
    await seed(page);
    await page.goto("/");

    await page.getByRole("button", { name: "settings" }).click();
    await page.getByRole("button", { name: "reset this device" }).click();
    await page.getByRole("button", { name: "wipe everything" }).click();

    await expect(
      page.getByRole("heading", { name: "add this device to your channel" }),
    ).toBeVisible();
    await expect(page.getByText("no channel yet.")).toBeVisible();
  });

  test("the channel restores the timeline view after a reload", async ({ page }) => {
    await seed(page);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "your channel" })).toBeVisible();

    await page.reload();
    await expect(page.getByRole("heading", { name: "your channel" })).toBeVisible();
  });
});
