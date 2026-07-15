/** Map `items` through `fn` with at most `limit` concurrent calls, preserving
 * input order in the result. A fixed pool of workers pulls the next index off a
 * shared cursor until the list drains, so the slowest item never blocks the
 * others (unlike a plain serial loop) and no more than `limit` run at once
 * (unlike Promise.all over the whole list). `fn` should handle its own errors —
 * a throw rejects the whole map. */
export async function mapLimit<T, R>(
  items: readonly T[],
  limit: number,
  fn: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(items.length);
  let next = 0;
  const worker = async (): Promise<void> => {
    for (let i = next++; i < items.length; i = next++) {
      results[i] = await fn(items[i], i);
    }
  };
  const workers = Math.max(1, Math.min(limit, items.length));
  await Promise.all(Array.from({ length: workers }, () => worker()));
  return results;
}
