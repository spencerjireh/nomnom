#!/usr/bin/env python3
"""Generate cross-language crypto fixtures from nomnom.py.

The browser client (nomnom-web) re-implements nomnom's crypto in TypeScript. This
script calls the canonical Python primitives and emits a JSON vector file that the
web client's vitest suite loads to assert byte-for-byte agreement (and to decrypt a
Python-sealed blob). nomnom.py is the source of truth; the committed fixture and the
TS port must move in lockstep — CI regenerates this and fails on any diff.

Run:  uv run python tools/gen_crypto_fixtures.py --out nomnom-web/test/fixtures/crypto-vectors.json
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import nomnom  # noqa: E402

P = nomnom._DH_P
G = nomnom._DH_G


def pubhex(priv: int) -> str:
    return format(pow(G, priv, P), "x")


def find_odd_length_pub() -> tuple[int, str]:
    """A priv whose public hex is odd-length, to exercise the padding path."""
    i = 2
    while True:
        h = pubhex(i)
        if len(h) % 2 == 1:
            return i, h
        i += 1


def build() -> dict:
    odd_priv, odd_pub = find_odd_length_pub()

    # --- DH shared-secret vectors ---
    dh_pairs = [(2, 3), (12345, 67890), (0xDEADBEEF, 0xCAFEBABE), (odd_priv, 7)]
    dh_vectors = []
    for a_priv, b_priv in dh_pairs:
        b_pub = pow(G, b_priv, P)
        shared = nomnom._dh_shared(a_priv, b_pub)
        dh_vectors.append({
            "privHex": format(a_priv, "x"),
            "peerPubHex": format(b_pub, "x"),
            "sharedHex": shared.hex(),
        })

    # --- session-key vectors (initiator == responder) ---
    session_vectors = []
    sk_cases = [
        # (ik_init_priv, ek_init_priv, ik_resp_priv, ek_resp_priv, binding)
        (111, 222, 333, 444, b""),
        (0x1111, 0x2222, 0x3333, 0x4444, b"recurring-v1demo-binding"),
        (odd_priv, 555, 666, 777, b""),
    ]
    for ikip, ekip, ikrp, ekrp, binding in sk_cases:
        ik_init_pub = pow(G, ikip, P)
        ek_init_pub = pow(G, ekip, P)
        ik_resp_pub = pow(G, ikrp, P)
        ek_resp_pub = pow(G, ekrp, P)
        init_key = nomnom._session_key_initiator(
            ikip, ekip, ik_init_pub, ek_init_pub, ik_resp_pub, ek_resp_pub,
            binding=binding,
        )
        resp_key = nomnom._session_key_responder(
            ikrp, ekrp, ik_resp_pub, ek_resp_pub, ik_init_pub, ek_init_pub,
            binding=binding,
        )
        assert init_key == resp_key, "initiator/responder key mismatch in fixture"
        session_vectors.append({
            "ikInitPriv": format(ikip, "x"),
            "ekInitPriv": format(ekip, "x"),
            "ikRespPriv": format(ikrp, "x"),
            "ekRespPriv": format(ekrp, "x"),
            "ikInitPub": format(ik_init_pub, "x"),
            "ekInitPub": format(ek_init_pub, "x"),
            "ikRespPub": format(ik_resp_pub, "x"),
            "ekRespPub": format(ek_resp_pub, "x"),
            "bindingHex": binding.hex(),
            "sessionKeyHex": init_key.hex(),
        })

    # --- AEAD vectors ---
    salt = bytes(range(16))
    nonce = bytes(range(100, 112))
    session_hex = session_vectors[0]["sessionKeyHex"]  # 64-char relay-path KDF input
    # `kdf` is the scrypt input (the relay flow passes session_key.hex(); the CLI
    # also accepts a raw string). The emitted JSON key is `kdfInput`, deliberately
    # NOT named "passphrase"/"password" so secret scanners don't false-positive on
    # this test-vector data.
    aead_cases = [
        {"name": "bundle.txt", "kdf": session_hex,
         "plaintext": b"hello from the CLI \x00\x01\x02 end"},
        {"name": "msg.txt", "kdf": "raw-string-kdf-input-vector-001",
         "plaintext": b"short raw kdf-input case"},
        # \U00020000 is a 4-byte-UTF-8 / surrogate-pair (astral) CJK char — it
        # exercises non-ASCII and astral handling without using an emoji.
        {"name": "résumé\U00020000.txt", "kdf": session_hex,
         "plaintext": "non-ascii name é \U00020001".encode("utf-8")},
        {"name": "empty.bin", "kdf": session_hex, "plaintext": b""},
        {"name": "block-edge.bin", "kdf": session_hex,
         "plaintext": bytes(range(256)) * 4},  # spans many keystream blocks
    ]
    aead_vectors = []
    for c in aead_cases:
        blob = nomnom.seal_bytes(
            c["plaintext"], c["name"], c["kdf"], _salt=salt, _nonce=nonce,
        )
        # sanity: round-trips in Python
        rname, rbody = nomnom.open_bytes(blob, c["kdf"])
        assert rname == c["name"] and rbody == c["plaintext"]
        aead_vectors.append({
            "name": c["name"],
            "kdfInput": c["kdf"],
            "saltHex": salt.hex(),
            "nonceHex": nonce.hex(),
            "plaintextHex": c["plaintext"].hex(),
            "blobHex": blob.hex(),
        })

    # --- slots + bindings ---
    my_priv = 0x1234567890
    their_pub = pubhex(0x9876543210)
    recurring = [
        {"myIkPrivHex": format(my_priv, "x"), "theirIkPubHex": their_pub,
         "slot": nomnom._slot_recurring(my_priv, their_pub)},
        # odd-length peer pub exercises the hex-padding path
        {"myIkPrivHex": format(my_priv, "x"), "theirIkPubHex": odd_pub,
         "slot": nomnom._slot_recurring(my_priv, odd_pub)},
    ]
    my_pub = pubhex(my_priv)
    relay_secret = "donut waffle pickle syrup gravy melon"
    fc_binding = nomnom._first_contact_binding(relay_secret)
    sender_ik = pubhex(0x5555)
    slots = {
        "recurring": recurring,
        "recurringBinding": {
            "myIkPubHex": my_pub,
            "theirIkPubHex": their_pub,
            "bindingHex": nomnom._recurring_binding(my_pub, their_pub).hex(),
        },
        "firstContactBindingHex": fc_binding.hex(),
        "firstContactInit": {
            "relaySecret": relay_secret,
            "slot": nomnom._slot_first_contact_init(relay_secret),
        },
        "firstContactRespBase": {
            "relaySecret": relay_secret,
            "senderIkHex": sender_ik,
            "base": nomnom._slot_first_contact_resp_base(relay_secret, sender_ik),
        },
        "pairRespSlot": {
            "relaySecret": relay_secret,
            "initiatorIkHex": sender_ik,
            "slot": nomnom._relay_pair_resp_slot(relay_secret, sender_ik),
        },
    }

    # --- relay auth header (fixed ts; reconstruct the formula _relay_hmac_headers uses) ---
    ra_secret = "donut waffle pickle syrup gravy melon"
    ra_method = "PUT"
    ra_path = "/slots/abcDEF-_123"
    ra_ts = 1700000000
    ra_msg = f"{ra_method}\n{ra_path}\n{ra_ts}".encode("utf-8")
    ra_mac = hmac.new(ra_secret.encode("utf-8"), ra_msg, hashlib.sha256).hexdigest()
    relay_auth = {
        "secret": ra_secret,
        "method": ra_method,
        "path": ra_path,
        "ts": ra_ts,
        "authorization": f"NMNM-HMAC-SHA256 {ra_ts}:{ra_mac}",
    }

    # --- fingerprint ---
    fp_vectors = [
        {"ikHex": their_pub, "fingerprint": nomnom._ik_fingerprint(their_pub)},
        {"ikHex": odd_pub, "fingerprint": nomnom._ik_fingerprint(odd_pub)},
    ]

    # --- handshake / pair blobs (parsed, not byte-compared) ---
    identity = {
        "device_id": "00112233aabbccdd",
        "name": "web-abcd",
        "ik_pub": my_pub,
        "ik_priv": format(my_priv, "x"),
    }
    ek_pub = pubhex(0xABCDEF)
    handshake_bytes = nomnom._relay_handshake_blob(
        identity, ek_pub, nomnom._RELAY_INIT_MAGIC,
    )
    pair_bytes = nomnom._relay_pair_blob(identity)
    blobs = {
        "handshake": {
            "identity": identity,
            "ekPubHex": ek_pub,
            "magic": nomnom._RELAY_INIT_MAGIC,
            "bytesHex": handshake_bytes.hex(),
        },
        "pair": {
            "identity": identity,
            "bytesHex": pair_bytes.hex(),
        },
    }

    return {
        "schema": 1,
        "generatedFrom": "nomnom.py",
        "dh": {"primeHex": format(P, "x"), "g": G, "vectors": dh_vectors},
        "sessionKey": session_vectors,
        "aead": aead_vectors,
        "slots": slots,
        "relayAuth": relay_auth,
        "fingerprint": fp_vectors,
        "blobs": blobs,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output JSON path")
    args = ap.parse_args()
    data = build()
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=True, sort_keys=True)
        f.write("\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
