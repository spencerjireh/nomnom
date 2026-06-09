import { useState } from "react";
import { useStore } from "../state/store";
import { useTransfer } from "../hooks/useTransfer";
import { friendlyRelayMessage } from "../relay/errors";

function autoFeedName(taken: Set<string>): string {
  for (let i = 1; i < 10_000; i++) {
    const candidate = `feed-${i}`;
    if (!taken.has(candidate)) return candidate;
  }
  return `feed-${Date.now()}`;
}

/** Warm right-pane state shown when no feed is selected: brand prompt + open/join forms. */
export function EmptyPane({ onNeedRelay }: { onNeedRelay: () => void }) {
  const feeds = useStore((s) => s.feeds);
  const relay = useStore((s) => s.relay);
  const selectFeed = useStore((s) => s.selectFeed);
  const { open, join, sending } = useTransfer();

  const [openName, setOpenName] = useState("");
  const [joinUrl, setJoinUrl] = useState("");
  const [joinName, setJoinName] = useState("");
  const [working, setWorking] = useState<null | "open" | "join">(null);
  const [error, setError] = useState<string | null>(null);

  const taken = new Set(feeds.map((f) => f.name));
  const blocked = sending || working !== null;

  async function doOpen() {
    setError(null);
    setWorking("open");
    try {
      const feed = await open(openName.trim() || autoFeedName(taken));
      setOpenName("");
      selectFeed(feed.name);
    } catch (e) {
      setError(friendlyRelayMessage(e));
    } finally {
      setWorking(null);
    }
  }

  async function doJoin() {
    setError(null);
    setWorking("join");
    try {
      const feed = await join(joinUrl.trim(), joinName.trim() || autoFeedName(taken));
      setJoinUrl("");
      setJoinName("");
      selectFeed(feed.name);
    } catch (e) {
      setError(friendlyRelayMessage(e));
    } finally {
      setWorking(null);
    }
  }

  return (
    <div className="empty-pane">
      <header className="empty-head">
        <h1 className="empty-title">a warm place to drop a file</h1>
        <p className="empty-sub dim">
          {feeds.length === 0
            ? "open a feed (you host) or join one from a shared URL."
            : "pick a feed on the left, or start a new one here."}
        </p>
      </header>

      <section className="empty-form">
        <span className="field-label">open a new feed</span>
        {relay ? (
          <div className="inline">
            <input
              placeholder="nickname (optional)"
              value={openName}
              onChange={(e) => setOpenName(e.target.value)}
              disabled={blocked}
            />
            <button type="button" className="btn primary" onClick={doOpen} disabled={blocked}>
              {working === "open" ? "opening…" : "open"}
            </button>
          </div>
        ) : (
          <p className="dim small">
            opening a feed needs a relay.{" "}
            <button type="button" className="linklike" onClick={onNeedRelay}>
              configure one →
            </button>
          </p>
        )}
      </section>

      <section className="empty-form">
        <span className="field-label">join by url</span>
        <input
          placeholder="https://relay…/f/<token>"
          value={joinUrl}
          onChange={(e) => setJoinUrl(e.target.value)}
          disabled={blocked}
        />
        <div className="inline">
          <input
            placeholder="nickname (optional)"
            value={joinName}
            onChange={(e) => setJoinName(e.target.value)}
            disabled={blocked}
          />
          <button
            type="button"
            className="btn primary"
            onClick={doJoin}
            disabled={blocked || !joinUrl.trim()}
          >
            {working === "join" ? "joining…" : "join"}
          </button>
        </div>
      </section>

      {error && <p className="err">{error}</p>}
    </div>
  );
}
