// Cross-language interop tests. Every vector in crypto-vectors.json is produced by
// nomnom.py (tools/gen_crypto_fixtures.py); these assert the TS port reproduces the
// exact bytes and can decrypt a Python-sealed blob. If nomnom.py's crypto changes,
// `npm run gen:fixtures` regenerates this file and these tests re-validate.

import { describe, it, expect } from "vitest";
import vectors from "./fixtures/crypto-vectors.json";

import {
  hexToBytes,
  bytesToHexDigest,
  dhSharedBytes,
  sessionKeyInitiator,
  sessionKeyResponder,
  sealBytes,
  openBytes,
  slotRecurring,
  recurringBinding,
  firstContactBinding,
  firstContactInitSlot,
  firstContactRespBase,
  pairRespSlot,
  relayAuthHeader,
  ikFingerprint,
  parseHandshake,
  parsePairBlob,
  buildHandshakeBlob,
  buildPairBlob,
  DH_PRIME_HEX,
  DH_G,
} from "../src/crypto";

const hex = (b: Uint8Array) => bytesToHexDigest(b);

describe("hex parity (odd-length tolerance)", () => {
  it("treats odd-length hex as zero-padded", () => {
    expect(hex(hexToBytes("abc"))).toBe(hex(hexToBytes("0abc")));
  });
});

describe("DH group constants", () => {
  it("matches the RFC 3526 prime and generator", () => {
    expect(DH_PRIME_HEX.toLowerCase()).toBe(vectors.dh.primeHex.toLowerCase());
    expect(Number(DH_G)).toBe(vectors.dh.g);
  });
  it("reproduces DH shared secrets", () => {
    for (const v of vectors.dh.vectors) {
      expect(hex(dhSharedBytes(v.privHex, v.peerPubHex))).toBe(v.sharedHex);
    }
  });
});

describe("session key (triple-DH)", () => {
  for (const [i, v] of vectors.sessionKey.entries()) {
    it(`vector ${i}: initiator == responder == python`, () => {
      const pubs = {
        ikInitPub: v.ikInitPub,
        ekInitPub: v.ekInitPub,
        ikRespPub: v.ikRespPub,
        ekRespPub: v.ekRespPub,
      };
      const binding = hexToBytes(v.bindingHex);
      const init = sessionKeyInitiator(v.ikInitPriv, v.ekInitPriv, pubs, binding);
      const resp = sessionKeyResponder(v.ikRespPriv, v.ekRespPriv, pubs, binding);
      expect(hex(init)).toBe(v.sessionKeyHex);
      expect(hex(resp)).toBe(v.sessionKeyHex);
    });
  }
});

describe("AEAD (seal/open)", () => {
  for (const [i, v] of vectors.aead.entries()) {
    it(`vector ${i} (${v.name}): decrypts the Python blob`, async () => {
      const blob = hexToBytes(v.blobHex);
      const { name, body } = await openBytes(blob, v.kdfInput);
      expect(name).toBe(v.name);
      expect(hex(body)).toBe(v.plaintextHex);
    });
    it(`vector ${i} (${v.name}): reseals to identical bytes`, async () => {
      const out = await sealBytes(hexToBytes(v.plaintextHex), v.name, v.kdfInput, {
        salt: hexToBytes(v.saltHex),
        nonce: hexToBytes(v.nonceHex),
      });
      expect(hex(out)).toBe(v.blobHex);
    });
  }

  it("rejects a tampered blob", async () => {
    const v = vectors.aead[0];
    const blob = hexToBytes(v.blobHex);
    blob[blob.length - 1] ^= 0xff;
    await expect(openBytes(blob, v.kdfInput)).rejects.toThrow("authentication failed");
  });
});

describe("slots + bindings", () => {
  it("reproduces recurring slot ids", () => {
    for (const r of vectors.slots.recurring) {
      expect(slotRecurring(r.myIkPrivHex, r.theirIkPubHex)).toBe(r.slot);
    }
  });
  it("reproduces the recurring binding", () => {
    const rb = vectors.slots.recurringBinding;
    expect(hex(recurringBinding(rb.myIkPubHex, rb.theirIkPubHex))).toBe(rb.bindingHex);
  });
  it("reproduces first-contact slots", async () => {
    const fc = vectors.slots.firstContactInit;
    const binding = await firstContactBinding(fc.relaySecret);
    expect(hex(binding)).toBe(vectors.slots.firstContactBindingHex);
    expect(firstContactInitSlot(binding)).toBe(fc.slot);

    const rb = vectors.slots.firstContactRespBase;
    const binding2 = await firstContactBinding(rb.relaySecret);
    expect(firstContactRespBase(binding2, rb.senderIkHex)).toBe(rb.base);

    const pr = vectors.slots.pairRespSlot;
    const binding3 = await firstContactBinding(pr.relaySecret);
    expect(pairRespSlot(binding3, pr.initiatorIkHex)).toBe(pr.slot);
  });
});

describe("relay auth header", () => {
  it("reproduces the Authorization value at a fixed timestamp", () => {
    const ra = vectors.relayAuth;
    expect(relayAuthHeader(ra.secret, ra.method, ra.path, ra.ts)).toBe(ra.authorization);
  });
  it("strips the query string from the signed path", () => {
    const ra = vectors.relayAuth;
    expect(relayAuthHeader(ra.secret, ra.method, ra.path + "?wait=30000", ra.ts)).toBe(
      ra.authorization,
    );
  });
});

describe("fingerprint", () => {
  it("matches the CLI fingerprint", () => {
    for (const v of vectors.fingerprint) {
      expect(ikFingerprint(v.ikHex)).toBe(v.fingerprint);
    }
  });
});

describe("blobs", () => {
  it("parses Python handshake bytes", () => {
    const h = vectors.blobs.handshake;
    const parsed = parseHandshake(hexToBytes(h.bytesHex), h.magic);
    expect(parsed.ik).toBe(h.identity.ik_pub);
    expect(parsed.ek).toBe(h.ekPubHex);
    expect(parsed.device_id).toBe(h.identity.device_id);
    expect(parsed.name).toBe(h.identity.name);
  });
  it("round-trips a JS-built handshake blob", () => {
    const h = vectors.blobs.handshake;
    const built = buildHandshakeBlob(h.identity as any, h.ekPubHex, h.magic);
    const parsed = parseHandshake(built, h.magic);
    expect(parsed.ik).toBe(h.identity.ik_pub);
    expect(parsed.ek).toBe(h.ekPubHex);
  });
  it("parses Python pair bytes and round-trips a JS-built one", () => {
    const p = vectors.blobs.pair;
    const parsed = parsePairBlob(hexToBytes(p.bytesHex));
    expect(parsed.ik).toBe(p.identity.ik_pub);
    expect(parsed.device_id).toBe(p.identity.device_id);
    const built = buildPairBlob(p.identity as any);
    expect(parsePairBlob(built).name).toBe(p.identity.name);
  });
});
