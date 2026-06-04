// Shared HTTP response shapes + sleep. Both feeds.ts and slots.ts emit the same
// JSON `{error: reason}` body on failure, and several handlers long-poll with a
// fixed-interval sleep.

export function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function errorResponse(reason: string, status: number): Response {
  return jsonResponse({ error: reason }, status);
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
