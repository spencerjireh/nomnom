// Barrel for the crypto module — the single source of truth for CLI interop.
// Imported by the Web Worker (key material + big buffers stay off the main
// thread) and directly by the cross-language vitest suite. Feeds v2 only; the
// legacy DH/pair primitives were excised when the client moved to feeds.

export * from "./constants";
export * from "./hex";
export * from "./primitives";
export * from "./fingerprint";
export * from "./stream";
export * from "./hkdf";
export * from "./ed25519";
export * from "./identity";
export * from "./feeds";
export * from "./feed-auth";
export * from "./relay-auth";
