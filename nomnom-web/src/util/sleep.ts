// Abort-aware sleep: resolves (never rejects) after `ms` or as soon as the
// signal aborts. Shared by the relay client's reconnect backoff and the
// receive loops' retry ticks.
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    const t = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(t);
        resolve();
      },
      { once: true },
    );
  });
}
