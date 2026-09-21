// Id grammars and member-card shape checks. Pure, no I/O; used by the Worker
// for URL captures and request bodies.

import { urlsafeBase64Encode } from "./crypto-util";
import { MAX_MEMBER_NAME_LEN, type MemberCard } from "./feed-types";

const FEED_ID_RE = /^[A-Za-z0-9_-]{8,32}$/;
const MEMBER_ID_RE = /^[A-Za-z0-9_-]{8,64}$/;
const SLOT_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;

export function validateFeedId(id: string): boolean {
  return FEED_ID_RE.test(id);
}

export function validateMemberId(id: string): boolean {
  return MEMBER_ID_RE.test(id);
}

export function validateSlotId(id: string): boolean {
  return SLOT_ID_RE.test(id);
}

export function byteLength(s: string): number {
  return new TextEncoder().encode(s).length;
}

// Shape + member-id grammar check shared by mint and putMember.
// `expectMemberId` additionally pins the body's member_id to the URL capture.
export function isValidCard(
  card: unknown,
  expectMemberId?: string,
): card is MemberCard {
  if (typeof card !== "object" || card === null) return false;
  const c = card as Partial<MemberCard>;
  return (
    typeof c.member_id === "string" &&
    validateMemberId(c.member_id) &&
    (expectMemberId === undefined || c.member_id === expectMemberId) &&
    typeof c.identity_pubkey === "string" &&
    typeof c.name === "string" &&
    c.name.length <= MAX_MEMBER_NAME_LEN
  );
}

export function generateFeedId(): string {
  // 9 random bytes = 12 base64url chars, ~72 bits. Matches Python
  // secrets.token_urlsafe(9).
  const bytes = new Uint8Array(9);
  crypto.getRandomValues(bytes);
  return urlsafeBase64Encode(bytes);
}
