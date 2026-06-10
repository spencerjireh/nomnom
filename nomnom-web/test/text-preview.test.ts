// Coverage for the text-vs-binary sniff and the capped/full decode that gate
// the held-row "view + copy" affordance. The component wiring (Timeline.tsx)
// needs a DOM and lives outside this node-env suite; these are the pure parts.

import { describe, expect, it } from "vitest";
import { looksLikeText, decodePreview, decodeText, PREVIEW_CAP_BYTES } from "../src/textPreview";

function buf(bytes: number[]): ArrayBuffer {
  return new Uint8Array(bytes).buffer;
}

function utf8(s: string): ArrayBuffer {
  return new TextEncoder().encode(s).buffer;
}

describe("looksLikeText", () => {
  it("accepts plain ascii and an empty body", () => {
    expect(looksLikeText(utf8("ssh-keygen -t ed25519"))).toBe(true);
    expect(looksLikeText(utf8(""))).toBe(true);
  });

  it("accepts valid multibyte utf-8 (emoji, accents)", () => {
    expect(looksLikeText(utf8("café — naïve — 日本語 — 🍔"))).toBe(true);
  });

  it("rejects a NUL byte as binary", () => {
    expect(looksLikeText(buf([0x68, 0x69, 0x00, 0x68, 0x69]))).toBe(false);
  });

  it("rejects a PNG magic header", () => {
    expect(looksLikeText(buf([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))).toBe(false);
  });

  it("rejects dense control bytes", () => {
    const noise = new Uint8Array(64);
    for (let i = 0; i < noise.length; i++) noise[i] = 0x01; // SOH, a control char
    expect(looksLikeText(noise.buffer)).toBe(false);
  });

  it("tolerates a multibyte char clipped at the sniff boundary", () => {
    // 4096 ascii bytes then a 3-byte char split across the sniff cap: the
    // streaming decode must not falsely reject a genuinely-text large file.
    const head = "a".repeat(4095);
    const body = utf8(head + "日" + "b".repeat(4096));
    expect(looksLikeText(body)).toBe(true);
  });
});

describe("decodePreview", () => {
  it("returns the whole text untruncated when under the cap", () => {
    const { text, truncated } = decodePreview(utf8("hello\nworld"));
    expect(text).toBe("hello\nworld");
    expect(truncated).toBe(false);
  });

  it("truncates to the cap and flags it", () => {
    const big = "x".repeat(PREVIEW_CAP_BYTES + 1000);
    const { text, truncated } = decodePreview(utf8(big));
    expect(truncated).toBe(true);
    expect(text.length).toBe(PREVIEW_CAP_BYTES);
  });
});

describe("decodeText", () => {
  it("decodes the full body regardless of size", () => {
    const big = "y".repeat(PREVIEW_CAP_BYTES + 1000);
    expect(decodeText(utf8(big)).length).toBe(PREVIEW_CAP_BYTES + 1000);
  });
});
