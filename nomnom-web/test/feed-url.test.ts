// parseFeedUrl / formatFeedUrl — pure string logic with documented strictness.

import { describe, expect, it } from "vitest";
import { formatFeedUrl, parseFeedUrl } from "../src/util/feed-url";

describe("formatFeedUrl", () => {
  it("joins host and token, stripping trailing slashes", () => {
    expect(formatFeedUrl("https://relay.test/", "abcDEF12")).toBe(
      "https://relay.test/f/abcDEF12",
    );
  });
});

describe("parseFeedUrl", () => {
  it("accepts a full https url", () => {
    expect(parseFeedUrl("https://relay.example.com/f/abcDEF12-_")).toEqual({
      host: "https://relay.example.com",
      feedId: "abcDEF12-_",
    });
  });

  it("prepends https:// when the scheme is absent", () => {
    expect(parseFeedUrl("relay.example.com/f/abcd1234")).toEqual({
      host: "https://relay.example.com",
      feedId: "abcd1234",
    });
  });

  it("trims surrounding whitespace", () => {
    expect(parseFeedUrl("  https://relay.test/f/abcd1234  ").feedId).toBe(
      "abcd1234",
    );
  });

  it.each([
    ["", "empty"],
    ["https://relay.test/abcd1234", "missing /f/ segment"],
    ["https://relay.test/f/abcd1234/extra", "trailing path"],
    ["https://relay.test/f/abc", "token too short"],
    ["https://relay.test/f/abcd$123", "invalid token chars"],
    ["https://relay.test/f/abcd1234?x=1", "query suffix"],
  ])("rejects %s (%s)", (raw) => {
    expect(() => parseFeedUrl(raw)).toThrow();
  });
});
