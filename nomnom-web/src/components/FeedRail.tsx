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

/** Left rail: brand, feed list with selection + activity dot, inline open/join, settings. */
export function FeedRail({ onOpenSettings }: { onOpenSettings: () => void }) {
  const feeds = useStore((s) => s.feeds);
  const selectedFeed = useStore((s) => s.selectedFeed);
  const timelines = useStore((s) => s.timelines);
  const viewedAt = useStore((s) => s.viewedAt);
  const relay = useStore((s) => s.relay);
  const selectFeed = useStore((s) => s.selectFeed);
  const { open, join, sending } = useTransfer();

  const [adding, setAdding] = useState<null | "open" | "join">(null);
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
      setAdding(null);
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
      setAdding(null);
      selectFeed(feed.name);
    } catch (e) {
      setError(friendlyRelayMessage(e));
    } finally {
      setWorking(null);
    }
  }

  return (
    <aside className="rail" aria-label="feeds">
      <div className="rail-brand">
        <span className="logo">NOMNOM</span>
      </div>

      <ul className="feed-rail-list" role="list">
        {feeds.length === 0 && (
          <li className="feed-row-empty dim small">no feeds yet.</li>
        )}
        {feeds.map((feed) => {
          const rows = timelines[feed.name];
          const viewed = viewedAt[feed.name] ?? 0;
          const unread = rows ? rows.some((r) => r.at > viewed) : false;
          const others = (feed.members_cache ?? []).filter(
            (m) => m.member_id !== feed.member_id,
          ).length;
          const active = feed.name === selectedFeed;
          return (
            <li key={feed.name}>
              <button
                type="button"
                className={active ? "feed-row active" : "feed-row"}
                aria-current={active ? "true" : undefined}
                onClick={() => selectFeed(feed.name)}
              >
                <span className={unread && !active ? "dot on" : "dot"} aria-hidden="true" />
                <span className="feed-row-name">{feed.name}</span>
                <span className="feed-row-count dim small">{others + 1}</span>
              </button>
            </li>
          );
        })}
      </ul>

      <div className="rail-add">
        {adding === null && (
          <div className="rail-add-buttons">
            <button
              type="button"
              className="chip"
              onClick={() => {
                setAdding("open");
                setError(null);
              }}
              disabled={blocked || !relay}
              title={relay ? "open a new feed" : "configure a relay in settings first"}
            >
              + open
            </button>
            <button
              type="button"
              className="chip"
              onClick={() => {
                setAdding("join");
                setError(null);
              }}
              disabled={blocked}
            >
              + join
            </button>
          </div>
        )}
        {adding === "open" && (
          <div className="rail-form">
            <input
              autoFocus
              placeholder="nickname (optional)"
              value={openName}
              onChange={(e) => setOpenName(e.target.value)}
              disabled={blocked}
            />
            <div className="inline">
              <button type="button" className="chip" onClick={() => setAdding(null)}>
                cancel
              </button>
              <button type="button" className="btn primary" onClick={doOpen} disabled={blocked}>
                {working === "open" ? "opening…" : "open"}
              </button>
            </div>
          </div>
        )}
        {adding === "join" && (
          <div className="rail-form">
            <input
              autoFocus
              placeholder="https://relay…/f/<token>"
              value={joinUrl}
              onChange={(e) => setJoinUrl(e.target.value)}
              disabled={blocked}
            />
            <input
              placeholder="nickname (optional)"
              value={joinName}
              onChange={(e) => setJoinName(e.target.value)}
              disabled={blocked}
            />
            <div className="inline">
              <button type="button" className="chip" onClick={() => setAdding(null)}>
                cancel
              </button>
              <button
                type="button"
                className="btn primary"
                onClick={doJoin}
                disabled={blocked || !joinUrl.trim()}
              >
                {working === "join" ? "joining…" : "join"}
              </button>
            </div>
          </div>
        )}
        {error && <p className="err small">{error}</p>}
      </div>

      <button type="button" className="rail-settings chip" onClick={onOpenSettings}>
        settings
      </button>
    </aside>
  );
}

