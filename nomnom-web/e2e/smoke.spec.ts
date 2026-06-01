import { test, expect, type Page } from "@playwright/test";
import { mkdtempSync, truncateSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Offline, secret-free smoke test of the nomnom-web UI. We never hit the relay:
// onboarding's GET /health is mocked, and the tab shell is reached by seeding
// localStorage directly (shapes mirror src/state/persistence.ts) instead of
// pairing. See playwright.config.ts for why interop lives elsewhere.

const MAX_PAYLOAD_BYTES = 100 * 1024 * 1024; // keep in sync with src/config.ts
const PEER_IK = "ab".repeat(256); // 512 hex chars = one 256-byte DH public key
const RELAY = JSON.stringify({
  url: "https://relay.spencerjireh.com",
  secret: "alpha bravo charlie delta echo foxtrot", // NOT a real passphrase
});
const PEER = JSON.stringify({
  "00112233aabbccdd": { name: "test-peer", ik_pub: PEER_IK, first_seen: 1700000000 },
});

/** Seed a fully-onboarded browser (relay set + optional pinned peer) so the app
 * lands in the tab shell on load, no relay calls required. */
async function seedOnboarded(page: Page, { withPeer = true } = {}): Promise<void> {
  await page.addInitScript(
    ([relay, peer]) => {
      localStorage.setItem("nomnom:relay", relay);
      localStorage.setItem("nomnom:schema", "1");
      localStorage.setItem("nomnom:peers", peer);
    },
    [RELAY, withPeer ? PEER : "{}"] as const,
  );
}

test.describe("nomnom-web UI smoke (offline, secret-free)", () => {
  test("onboarding saves the passphrase and opens the tab shell", async ({ page }) => {
    // The lone network call onboarding makes is the relay health probe; stub it.
    await page.route("**/health", (route) => route.fulfill({ status: 200, body: "ok" }));
    await page.goto("/");

    await expect(page.getByText(/paste your relay passphrase/)).toBeVisible();
    await page
      .getByRole("textbox", { name: "passphrase" })
      .fill("alpha bravo charlie delta echo foxtrot");
    await page.getByRole("button", { name: /open tab/ }).click();

    await expect(page.getByRole("tab", { name: "send" })).toBeVisible();
    await expect(page.getByText(/guest: web-/)).toBeVisible();
  });

  test("send tab shows the empty state with no paired devices", async ({ page }) => {
    await seedOnboarded(page, { withPeer: false });
    await page.goto("/");

    await page.getByRole("tab", { name: "send" }).click();
    await expect(page.getByText("no devices on the menu yet.")).toBeVisible();
  });

  test("pair and peers tabs render with a seeded pin", async ({ page }) => {
    await seedOnboarded(page);
    await page.goto("/");

    await page.getByRole("tab", { name: "pair" }).click();
    await expect(page.getByText(/your fp/)).toBeVisible();

    await page.getByRole("tab", { name: "peers" }).click();
    await expect(page.getByText("test-peer")).toBeVisible();
  });

  test("FileDrop rejects a file over the 100 MB cap before any crypto", async ({ page }) => {
    await seedOnboarded(page);
    await page.goto("/");
    await page.getByRole("tab", { name: "send" }).click();

    // A sparse file just over the cap: the app reads only `file.size` on the
    // reject path (never the bytes), so this costs no real I/O.
    const dir = mkdtempSync(join(tmpdir(), "nomnom-e2e-"));
    const tooBig = join(dir, "too-big.bin");
    writeFileSync(tooBig, "");
    truncateSync(tooBig, MAX_PAYLOAD_BYTES + 1);

    await page.locator('input[type="file"]').setInputFiles(tooBig);

    await expect(page.getByText(/too big/)).toBeVisible();
    // Guard never armed the send button.
    await expect(page.getByRole("button", { name: /serve it up/ })).toBeDisabled();
  });

  test("settings reset wipes the device back to onboarding", async ({ page }) => {
    await seedOnboarded(page);
    await page.goto("/");

    await page.getByRole("button", { name: "settings" }).click();
    await page.getByRole("button", { name: "reset this device" }).click();
    await page.getByRole("button", { name: "wipe everything" }).click();

    await expect(page.getByText(/paste your relay passphrase/)).toBeVisible();
  });

  test("onboarded state survives a reload", async ({ page }) => {
    await seedOnboarded(page);
    await page.goto("/");
    await expect(page.getByRole("tab", { name: "send" })).toBeVisible();

    await page.reload();
    await expect(page.getByRole("tab", { name: "send" })).toBeVisible();
    await page.getByRole("tab", { name: "peers" }).click();
    await expect(page.getByText("test-peer")).toBeVisible();
  });
});
