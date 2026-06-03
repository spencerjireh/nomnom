// Cross-language feeds-v2 interop tests. Every vector in feeds-vectors.json is
// produced by nomnom.py (tools/gen_feeds_fixtures.py via `npm run gen:fixtures`);
// these assert the TS port reproduces the exact bytes, decrypts a Python-sealed
// post, and re-seals to identical bytes. If nomnom.py's feed crypto changes,
// regenerate the fixture and these tests re-validate the port.

import { describe, it, expect } from "vitest";
import v from "./fixtures/feeds-vectors.json";

import {
  hexToBytes,
  bytesToHexDigest,
  hkdf,
  ed25519PubFromSeed,
  ed25519Sign,
  ed25519Verify,
  feedKeyFromToken,
  feedSubkeys,
  feedRequestMac,
  feedSeal,
  feedOpen,
  feedAuthHeader,
} from "../src/crypto";

const hex = (b: Uint8Array) => bytesToHexDigest(b);
const enc = new TextEncoder();

describe("HKDF (RFC 5869)", () => {
  for (const [i, h] of v.hkdf.entries()) {
    it(`vector ${i}: reproduces ${h.length}-byte output`, () => {
      const out = hkdf(hexToBytes(h.saltHex), hexToBytes(h.ikmHex), enc.encode(h.infoUtf8), h.length);
      expect(hex(out)).toBe(h.outHex);
    });
  }
});

describe("Ed25519", () => {
  for (const [i, e] of v.ed25519.entries()) {
    it(`vector ${i}: pub + signature match the CLI`, () => {
      const seed = hexToBytes(e.seedHex);
      expect(hex(ed25519PubFromSeed(seed))).toBe(e.pubHex);
      expect(hex(ed25519Sign(hexToBytes(e.msgHex), seed))).toBe(e.sigHex);
      expect(ed25519Verify(hexToBytes(e.msgHex), hexToBytes(e.sigHex), hexToBytes(e.pubHex))).toBe(true);
    });
  }
  it("rejects a tampered signature", () => {
    const e = v.ed25519[1];
    const sig = hexToBytes(e.sigHex);
    sig[0] ^= 0xff;
    expect(ed25519Verify(hexToBytes(e.msgHex), sig, hexToBytes(e.pubHex))).toBe(false);
  });
});

describe("feed key derivation", () => {
  it("derives the feed key from the URL token", () => {
    expect(hex(feedKeyFromToken(v.token))).toBe(v.feedKeyHex);
  });
  it("derives enc/mac subkeys", () => {
    const { encKey, macKey } = feedSubkeys(hexToBytes(v.feedKeyHex));
    expect(hex(encKey)).toBe(v.subkeys.encKeyHex);
    expect(hex(macKey)).toBe(v.subkeys.macKeyHex);
  });
});

describe("feed post (seal/open)", () => {
  for (const [i, s] of v.seal.entries()) {
    it(`vector ${i} (${s.filename}): opens the Python blob`, async () => {
      const { header, body } = await feedOpen({
        feedKey: hexToBytes(v.feedKeyHex),
        feedId: v.feedId,
        blob: hexToBytes(s.blobHex),
      });
      expect(hex(body)).toBe(s.bodyHex);
      expect(header.fn).toBe(s.filename);
      expect(header.smid).toBe(v.senderMemberId);
      expect(header.sik).toBe(v.senderSigPubHex);
    });

    it(`vector ${i} (${s.filename}): reseals to identical bytes`, async () => {
      const out = await feedSeal({
        feedKey: hexToBytes(v.feedKeyHex),
        feedId: v.feedId,
        senderMemberId: v.senderMemberId,
        senderSigPrivHex: v.senderSigPrivHex,
        senderSigPubHex: v.senderSigPubHex,
        filename: s.filename,
        body: hexToBytes(s.bodyHex),
        postedAt: v.postedAt,
        nonce: hexToBytes(v.nonceHex),
      });
      expect(hex(out)).toBe(s.blobHex);
    });
  }

  it("rejects a tampered post", async () => {
    const s = v.seal[0];
    const blob = hexToBytes(s.blobHex);
    blob[blob.length - 1] ^= 0xff;
    await expect(
      feedOpen({ feedKey: hexToBytes(v.feedKeyHex), feedId: v.feedId, blob }),
    ).rejects.toThrow("feed authentication failed");
  });

  it("enforces expected member id / sig pub", async () => {
    const s = v.seal[0];
    await expect(
      feedOpen({
        feedKey: hexToBytes(v.feedKeyHex),
        feedId: v.feedId,
        blob: hexToBytes(s.blobHex),
        expectMemberId: "deadbeefdeadbeefdeadbeefdeadbeef",
      }),
    ).rejects.toThrow("sender_member_id mismatch");
  });
});

describe("feed request auth", () => {
  it("reproduces the request MAC", () => {
    const rm = v.requestMac;
    expect(feedRequestMac(hexToBytes(v.feedKeyHex), rm.method, rm.path, rm.ts)).toBe(rm.mac);
  });
  it("builds the Authorization header and strips the query string", () => {
    const rm = v.requestMac;
    expect(feedAuthHeader(hexToBytes(v.feedKeyHex), rm.method, rm.path, rm.ts)).toBe(rm.authorization);
    expect(
      feedAuthHeader(hexToBytes(v.feedKeyHex), rm.method, rm.path + "?wait=30000", rm.ts),
    ).toBe(rm.authorization);
  });
});
