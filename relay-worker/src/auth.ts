// HMAC-SHA256 verification for relay requests.
//
// Authorization: NMNM-HMAC-SHA256 <unix_ts>:<hex_mac>
//   mac = HMAC-SHA256(secret, method + "\n" + path + "\n" + unix_ts)
//
// The MAC does NOT cover the request body. Body integrity is provided by the
// AEAD wrapper inside slot_data (nomnom's existing encrypt_bytes adds an HMAC
// over the ciphertext). The relay's HMAC authenticates the client to the
// Worker; it does not vouch for what the client is sending.

const TIMESTAMP_WINDOW_SEC = 300;
const AUTH_PREFIX = "NMNM-HMAC-SHA256 ";

export type AuthResult =
  | { ok: true }
  | { ok: false; status: number; reason: string };

export async function verifyHmac(
  req: Request,
  secret: string,
): Promise<AuthResult> {
  const header = req.headers.get("Authorization") ?? "";
  if (!header.startsWith(AUTH_PREFIX)) {
    return { ok: false, status: 401, reason: "missing-mac" };
  }
  const rest = header.slice(AUTH_PREFIX.length);
  const colon = rest.indexOf(":");
  if (colon < 0) {
    return { ok: false, status: 401, reason: "bad-mac" };
  }
  const tsStr = rest.slice(0, colon);
  const macHex = rest.slice(colon + 1).toLowerCase();
  const ts = Number.parseInt(tsStr, 10);
  if (!Number.isFinite(ts) || tsStr !== String(ts)) {
    return { ok: false, status: 401, reason: "bad-mac" };
  }
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - ts) > TIMESTAMP_WINDOW_SEC) {
    return { ok: false, status: 401, reason: "clock-skew" };
  }
  const url = new URL(req.url);
  const msg = `${req.method}\n${url.pathname}\n${tsStr}`;
  const expected = await hmacSha256Hex(secret, msg);
  if (!constantTimeEqualHex(expected, macHex)) {
    return { ok: false, status: 401, reason: "bad-mac" };
  }
  return { ok: true };
}

async function hmacSha256Hex(secret: string, msg: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(msg));
  return bytesToHex(new Uint8Array(sig));
}

function bytesToHex(bytes: Uint8Array): string {
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    out += bytes[i].toString(16).padStart(2, "0");
  }
  return out;
}

function constantTimeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}
