#!/usr/bin/env python3
"""Generate cross-language feeds-v2 crypto fixtures from nomnom.py.

Run via `npm run gen:fixtures` (or directly with uv). Emits
test/fixtures/feeds-vectors.json — the byte-for-byte expectations the vitest
suite asserts the TypeScript port reproduces. Re-run whenever nomnom.py's feed
crypto changes; the test then re-validates the port against the new bytes.

Every value here is deterministic: fixed seeds, nonce, and posted_at, so the
output is stable across runs and meaningful in a diff.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parents[1] / "test" / "fixtures" / "feeds-vectors.json"


def load_nomnom():
    spec = importlib.util.spec_from_file_location("nomnom", REPO_ROOT / "nomnom.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nomnom"] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    m = load_nomnom()

    # --- fixed inputs ---
    token = "testfeedtoken00"  # [A-Za-z0-9_-]{8,32}
    feed_id = token
    member_id = "0123456789abcdef0123456789abcdef"
    seed = bytes(range(32))  # sender Ed25519 seed
    sig_priv_hex = seed.hex()
    sig_pub_hex = m.ed25519_pub_from_seed(seed).hex()
    nonce = bytes(range(0x64, 0x70))  # 12 bytes
    posted_at = 1_717_459_200
    feed_key = m._feed_key_from_token(token)
    enc_key, mac_key = m._feed_subkeys(feed_key)

    # --- HKDF (incl. empty-salt expand path) ---
    hkdf_vectors = []
    for salt, ikm, info, length in [
        (b"nomnom-feed-v1", bytes(range(8)), b"testfeedtoken00", 32),
        (b"", feed_key, b"nomnom-feed-enc", 32),
        (b"", feed_key, b"nomnom-feed-mac", 48),  # >1 block
    ]:
        hkdf_vectors.append({
            "saltHex": salt.hex(),
            "ikmHex": ikm.hex(),
            "infoUtf8": info.decode("utf-8"),
            "length": length,
            "outHex": m._hkdf(salt=salt, ikm=ikm, info=info, length=length).hex(),
        })

    # --- Ed25519 ---
    ed_vectors = []
    for msg in [b"", b"hi", b"the quick brown fox \x00\x01\x02"]:
        sig = m.ed25519_sign(msg, seed)
        assert m.ed25519_verify(msg, sig, m.ed25519_pub_from_seed(seed))
        ed_vectors.append({
            "seedHex": seed.hex(),
            "pubHex": sig_pub_hex,
            "msgHex": msg.hex(),
            "sigHex": sig.hex(),
        })

    # --- Ed25519 adversarial verify vectors ---
    # Each records what the (unaudited) pure-Python verify returns for a crafted
    # input; the TS test asserts @noble/curves returns the SAME verdict, so any
    # malleability/canonicality divergence between the two fails CI.
    verify_vectors = []

    def add_verify(label: str, msg: bytes, sig: bytes, pub: bytes) -> None:
        verify_vectors.append({
            "label": label,
            "msgHex": msg.hex(),
            "sigHex": sig.hex(),
            "pubHex": pub.hex(),
            "valid": m.ed25519_verify(msg, sig, pub),
        })

    adv_msg = b"adversarial vectors"
    pub = bytes.fromhex(sig_pub_hex)
    base_sig = m.ed25519_sign(adv_msg, seed)
    other_pub = m.ed25519_pub_from_seed(bytes(range(32, 64)))
    flip_r = bytearray(base_sig); flip_r[0] ^= 0x01    # corrupt R
    flip_s = bytearray(base_sig); flip_s[32] ^= 0x01   # corrupt S (stays < L)
    s_val = int.from_bytes(base_sig[32:], "little")
    non_canon_s = base_sig[:32] + (s_val + m._ED_L).to_bytes(32, "little")  # S >= L
    add_verify("valid", adv_msg, base_sig, pub)
    add_verify("flipped-r", adv_msg, bytes(flip_r), pub)
    add_verify("flipped-s", adv_msg, bytes(flip_s), pub)
    add_verify("wrong-msg", b"a different message", base_sig, pub)
    add_verify("wrong-pub", adv_msg, base_sig, other_pub)
    add_verify("non-canonical-s", adv_msg, non_canon_s, pub)
    add_verify("zero-sig", adv_msg, bytes(64), pub)
    # The baseline must verify; every tampered case must be rejected by the CLI.
    assert verify_vectors[0]["valid"] is True
    assert all(not vv["valid"] for vv in verify_vectors[1:])

    # --- feed_seal / feed_open ---
    seal_vectors = []
    bodies = {
        "bundle.txt": b"hello from the CLI \x00\x01\x02 end",
        "empty.bin": b"",
        # crosses several 32-byte keystream blocks, non-aligned tail
        "big.bin": bytes((i * 7 + 3) & 0xFF for i in range(200)),
        "résumé.txt": "naïve — café".encode("utf-8"),
    }
    for fn, body in bodies.items():
        blob = m.feed_seal(
            feed_key=feed_key,
            feed_id=feed_id,
            sender_member_id=member_id,
            sender_sig_priv_hex=sig_priv_hex,
            sender_sig_pub_hex=sig_pub_hex,
            filename=fn,
            body=body,
            posted_at=posted_at,
            _nonce=nonce,
        )
        header, reopened = m.feed_open(feed_key=feed_key, feed_id=feed_id, blob=blob)
        assert reopened == body
        seal_vectors.append({
            "filename": fn,
            "bodyHex": body.hex(),
            "blobHex": blob.hex(),
            "header": header,
        })

    # --- request MAC / auth transcript ---
    ts = 1_717_459_200
    method = "GET"
    path = f"/feeds/{feed_id}/slots/abc123def456"
    request_mac = m._feed_request_mac(feed_key, method, path, ts)

    fixtures = {
        "_comment": "Generated by tools/gen_feeds_fixtures.py from nomnom.py. Do not edit by hand.",
        "token": token,
        "feedId": feed_id,
        "feedKeyHex": feed_key.hex(),
        "subkeys": {"encKeyHex": enc_key.hex(), "macKeyHex": mac_key.hex()},
        "senderMemberId": member_id,
        "senderSigPrivHex": sig_priv_hex,
        "senderSigPubHex": sig_pub_hex,
        "nonceHex": nonce.hex(),
        "postedAt": posted_at,
        "hkdf": hkdf_vectors,
        "ed25519": ed_vectors,
        "verify": verify_vectors,
        "seal": seal_vectors,
        "requestMac": {
            "method": method,
            "path": path,
            "ts": ts,
            "mac": request_mac,
            "authorization": f"NMNM-FEEDKEY-SHA256 {ts}:{request_mac}",
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixtures, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
