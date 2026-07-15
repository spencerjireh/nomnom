// Shared HTTP plumbing. Both feeds.ts and slots.ts emit the same JSON
// `{error: reason}` body on failure, guard PUT bodies identically, and the
// long-poll handlers share one wait budget.

export const MAX_BUDGET_MS = 30_000;

// 256 MB cap matches R2's single-PUT limit; the free tier rejects bodies
// over 100 MB at the edge before this check runs.
export const MAX_BODY_BYTES = 256 * 1024 * 1024;

export function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function errorResponse(reason: string, status: number): Response {
  return jsonResponse({ error: reason }, status);
}

// 411 if there is no usable Content-Length, 413 if it exceeds maxBytes, 400 if
// there is no body, else null.
//
// The body streams straight into R2, so Content-Length is our only pre-write
// size gate. A missing/unparseable/negative header must be REJECTED, not waved
// through: parsing "" or a chunked request yields NaN, and treating "unknown
// length" as "within limit" lets an over-cap (or negative-declared) body defeat
// the cap entirely. All nomnom clients buffer before PUT, so they always send a
// valid Content-Length.
export function rejectBody(req: Request, maxBytes: number): Response | null {
  const raw = req.headers.get("Content-Length");
  const len = Number.parseInt(raw ?? "", 10);
  if (!Number.isFinite(len) || len < 0) {
    return errorResponse("length-required", 411);
  }
  if (len > maxBytes) {
    return errorResponse("payload-too-large", 413);
  }
  if (req.body === null) {
    return errorResponse("empty-body", 400);
  }
  return null;
}

// Clamp a client-requested wait into [0, MAX_BUDGET_MS], anchored at now.
export function pollDeadline(waitMs: number): number {
  return Date.now() + Math.min(Math.max(waitMs, 0), MAX_BUDGET_MS);
}

// `?since=` / `?since_ts=` parser: non-negative integer, else 0.
export function parseSinceTs(s: string | null): number {
  const n = Number.parseInt(s ?? "0", 10);
  return Number.isFinite(n) && n >= 0 ? n : 0;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
