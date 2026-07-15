// The pinned @cloudflare/workers-types cut omits `include` from R2ListOptions,
// but the R2 runtime supports it: passing include:["customMetadata"] returns
// customMetadata on each listed object, so we can read a slot's created_at
// straight from the list instead of an O(slot_count) per-object head scan.
// Augment the global interface (this file is a global script — no import/export
// — so the declaration merges into the workers-types one).
interface R2ListOptions {
  include?: ("httpMetadata" | "customMetadata")[];
}
