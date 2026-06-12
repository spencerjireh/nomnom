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

// 413 if Content-Length exceeds maxBytes, 400 if there is no body, else null.
export function rejectBody(req: Request, maxBytes: number): Response | null {
  const len = Number.parseInt(req.headers.get("Content-Length") ?? "", 10);
  if (Number.isFinite(len) && len > maxBytes) {
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
