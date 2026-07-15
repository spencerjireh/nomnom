import { describe, expect, it } from "vitest";
import { listSlotCreatedAts } from "../src/slot-index";

// Minimal in-memory R2Bucket.list stub that honours the cursor + include the
// production code relies on. Keys are returned in lexicographic order (like R2)
// and paged at `pageSize` so we can prove the cursor loop crosses page limits.
function fakeBucket(
  objects: { key: string; created_at: number }[],
  pageSize: number,
): R2Bucket {
  const sorted = [...objects].sort((a, b) => (a.key < b.key ? -1 : 1));
  return {
    async list(opts: R2ListOptions = {}) {
      const prefix = opts.prefix ?? "";
      const all = sorted.filter((o) => o.key.startsWith(prefix));
      const start = opts.cursor ? Number(opts.cursor) : 0;
      const page = all.slice(start, start + pageSize);
      const nextStart = start + pageSize;
      const truncated = nextStart < all.length;
      const withMeta = opts.include?.includes("customMetadata");
      return {
        objects: page.map((o) => ({
          key: o.key,
          customMetadata: withMeta
            ? { created_at: String(o.created_at) }
            : undefined,
        })),
        delimitedPrefixes: [],
        truncated,
        ...(truncated ? { cursor: String(nextStart) } : {}),
      };
    },
  } as unknown as R2Bucket;
}

describe("listSlotCreatedAts", () => {
  it("follows the cursor across pages instead of capping at one list", async () => {
    // 2500 slots at 1000/page: a single un-paginated list would hide 1500.
    const objects = Array.from({ length: 2500 }, (_, i) => ({
      key: `feeds/f1/slots/slot-${String(i).padStart(5, "0")}`,
      created_at: 1000 + i,
    }));
    const map = await listSlotCreatedAts(fakeBucket(objects, 1000), "f1");
    expect(map.size).toBe(2500);
    // A slot that only exists on the 3rd page is present with its created_at.
    expect(map.get("feeds/f1/slots/slot-02499")).toBe(3499);
  });

  it("reads created_at from customMetadata (no head scan)", async () => {
    const map = await listSlotCreatedAts(
      fakeBucket([{ key: "feeds/f1/slots/x", created_at: 42 }], 1000),
      "f1",
    );
    expect(map.get("feeds/f1/slots/x")).toBe(42);
  });

  it("scopes to the feed prefix", async () => {
    const objects = [
      { key: "feeds/f1/slots/a", created_at: 1 },
      { key: "feeds/f2/slots/b", created_at: 2 },
    ];
    const map = await listSlotCreatedAts(fakeBucket(objects, 1000), "f1");
    expect([...map.keys()]).toEqual(["feeds/f1/slots/a"]);
  });
});
