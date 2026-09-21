// Types and constants shared by the Worker (worker.ts) and the per-feed
// Durable Object (feed.ts). Kept separate so neither imports the other's
// implementation.

export const MAX_MEMBER_CARD_BYTES = 4096;
export const MAX_MEMBER_NAME_LEN = 128;
export const MAX_MEMBER_COUNT = 64; // bound roster size per feed

// Retention. Posts are deleted this long after creation; a feed with no posts
// left and no activity for this long is purged along with its DO storage.
export const POST_TTL_SEC = 30 * 86_400;
export const FEED_IDLE_SEC = 30 * 86_400;
// `last_used_at` is rewritten at most this often so an active feed costs one
// row write per hour, not one per request.
export const TOUCH_THROTTLE_SEC = 3600;
export const ALARM_PERIOD_MS = 24 * 3600 * 1000;

// Body of a member card as posted by clients. `card` in the DB is the raw JSON
// the client sent (re-served verbatim on list), the other columns are
// denormalised copies for querying.
export interface MemberCard {
  member_id: string;
  identity_pubkey: string;
  name: string;
}

// Row types are declared as type aliases (not interfaces) so they satisfy the
// `Record<string, SqlStorageValue>` bound on `sql.exec<T>()`.
export type MemberRow = {
  member_id: string;
  identity_pubkey: string;
  name: string;
  joined_at: number;
};

export type PostRow = {
  seq: number;
  slot_id: string;
  created_at: number;
};

// Frames pushed over the /ws socket. All JSON text frames.
export type WsFrame =
  | { type: "post"; seq: number; slot_id: string; created_at: number }
  | { type: "member"; action: "join" | "leave"; member: MemberRow }
  | { type: "deleted"; slot_id: string };

// RPC results cross the DO boundary as plain data. Errors thrown across RPC
// lose their class, so expected failures travel as `{ok:false}` and throws are
// reserved for genuine faults (the Worker maps those to 502).
export type DoResult<T> =
  | { ok: true; value: T }
  | { ok: false; status: number; error: string };

export function ok<T>(value: T): DoResult<T> {
  return { ok: true, value };
}

export function fail<T = never>(status: number, error: string): DoResult<T> {
  return { ok: false, status, error };
}
