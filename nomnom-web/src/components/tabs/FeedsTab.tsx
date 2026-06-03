import { useState } from "react";
import { useStore } from "../../state/store";
import { useTransfer } from "../../hooks/useTransfer";
import { Fingerprint } from "../Fingerprint";
import { friendlyRelayMessage } from "../../relay/errors";
import type { Feed } from "../../types";

function autoFeedName(feeds: Feed[]): string {
  const taken = new Set(feeds.map((f) => f.name));
  for (let i = 1; i < 10_000; i++) {
    const c = `feed-${i}`;
    if (!taken.has(c)) return c;
  }
  return `feed-${Date.now()}`;
}

function expiry(unix: number): string {
  if (!unix) return "unknown";
  const secs = unix - Math.floor(Date.now() / 1000);
  if (secs <= 0) return "expired";
  if (secs < 3600) return `${Math.floor(secs / 60)}m left`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h left`;
  return `${Math.floor(secs / 86400)}d left`;
}

export function FeedsTab({ onNeedRelay }: { onNeedRelay: () => void }) {
  const feeds = useStore((s) => s.feeds);
  const defaultFeed = useStore((s) => s.defaultFeed);
  const relay = useStore((s) => s.relay);
  const setDefaultFeed = useStore((s) => s.setDefaultFeed);
  const { open, join, leave, busy } = useTransfer();

  const [openName, setOpenName] = useState("");
  const [joinUrl, setJoinUrl] = useState("");
  const [joinName, setJoinName] = useState("");
  const [working, setWorking] = useState<null | "open" | "join">(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  async function doOpen() {
    setError(null);
    setWorking("open");
    try {
      await open(openName.trim() || autoFeedName(feeds));
      setOpenName("");
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
      await join(joinUrl.trim(), joinName.trim() || autoFeedName(feeds));
      setJoinUrl("");
      setJoinName("");
    } catch (e) {
      setError(friendlyRelayMessage(e));
    } finally {
      setWorking(null);
    }
  }

  async function copyUrl(feed: Feed) {
    try {
      await navigator.clipboard.writeText(feed.url);
      setCopied(feed.name);
      setTimeout(() => setCopied((c) => (c === feed.name ? null : c)), 1500);
    } catch {
      // clipboard blocked — leave the URL visible for manual copy
    }
  }

  const blocked = busy || working !== null;

  return (
    <div className="tab">
      {/* Open / Join actions */}
      <div className="feed-forms">
        <div className="feed-form">
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
        </div>

        <div className="feed-form">
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
        </div>
        {error && <p className="err">{error}</p>}
      </div>

      <hr className="dashed" />

      {feeds.length === 0 ? (
        <div className="tab-empty">
          <p>no feeds yet.</p>
          <p className="dim">open one (you host) or join one from a shared URL.</p>
        </div>
      ) : (
        <ul className="peer-list">
          {feeds.map((feed) => {
            const others = (feed.members_cache ?? []).filter((m) => m.member_id !== feed.member_id);
            const isDefault = feed.name === defaultFeed;
            return (
              <li key={feed.name} className="peer">
                <div className="peer-main">
                  <strong>{feed.name}</strong>
                  {isDefault && <span className="badge"> default</span>}
                  <span className="dim small">
                    {" "}
                    · {others.length} other member{others.length === 1 ? "" : "s"} · {expiry(feed.expires_at)}
                  </span>
                </div>
                <div className="peer-fp small">
                  <code className="feed-url">{feed.url}</code>
                </div>
                <div className="peer-actions">
                  <button type="button" className="chip" onClick={() => copyUrl(feed)}>
                    {copied === feed.name ? "copied!" : "copy url"}
                  </button>
                  {!isDefault && (
                    <button type="button" className="chip" onClick={() => setDefaultFeed(feed.name)}>
                      set default
                    </button>
                  )}
                  <button
                    type="button"
                    className="chip"
                    onClick={() => setExpanded((e) => (e === feed.name ? null : feed.name))}
                  >
                    {expanded === feed.name ? "hide members" : `members (${(feed.members_cache ?? []).length})`}
                  </button>
                  <button
                    type="button"
                    className="chip danger"
                    onClick={() => leave(feed)}
                    disabled={blocked}
                  >
                    leave
                  </button>
                </div>
                {expanded === feed.name && (
                  <ul className="member-list">
                    {(feed.members_cache ?? []).map((m) => (
                      <li key={m.member_id} className="member small">
                        <span>{m.name || "(no name)"}</span>
                        {m.member_id === feed.member_id && <span className="dim"> · you</span>}{" "}
                        <Fingerprint hex={m.identity_pubkey} />
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
