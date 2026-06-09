import { useEffect, useState } from "react";
import { useStore } from "../state/store";
import { useTransfer } from "../hooks/useTransfer";
import { FileDrop, type StagedPayload } from "./FileDrop";
import { Timeline } from "./Timeline";
import { MembersFooter } from "./MembersFooter";
import type { Feed } from "../types";

/** Right pane: header, scrolling timeline, drop zone + send, members footer. */
export function FeedView({ feed, onBack }: { feed: Feed; onBack: () => void }) {
  const markFeedViewed = useStore((s) => s.markFeedViewed);
  const { send, sending: globalSending } = useTransfer();
  const [staged, setStaged] = useState<StagedPayload | null>(null);
  const [sending, setSending] = useState(false);

  // Touch viewedAt on mount and on every new entry: the rail's activity dot
  // never lights up for the feed you're already looking at.
  const rowCount = useStore((s) => s.timelines[feed.name]?.length ?? 0);
  useEffect(() => {
    markFeedViewed(feed.name);
  }, [feed.name, rowCount, markFeedViewed]);

  async function onSend() {
    if (!staged || sending) return;
    setSending(true);
    try {
      const data = await staged.read();
      await send(feed, { name: staged.name, data });
      setStaged(null);
    } finally {
      setSending(false);
    }
  }

  const canSend = !!staged && !sending && !globalSending;
  const others = (feed.members_cache ?? []).filter(
    (m) => m.member_id !== feed.member_id,
  ).length;

  return (
    <section className="feed-view" aria-label={feed.name}>
      <header className="feed-head">
        <button type="button" className="feed-back chip" onClick={onBack}>
          ← feeds
        </button>
        <h2 className="feed-title">{feed.name}</h2>
        <span className="dim small">
          {others} other{others === 1 ? "" : "s"}
        </span>
      </header>

      <Timeline feedName={feed.name} />

      <div className="feed-composer">
        <FileDrop onChange={setStaged} disabled={sending || globalSending} />
        <button
          type="button"
          className="btn primary big"
          onClick={onSend}
          disabled={!canSend}
        >
          {sending ? "serving…" : "serve it up →"}
        </button>
      </div>

      <MembersFooter feed={feed} />
    </section>
  );
}
