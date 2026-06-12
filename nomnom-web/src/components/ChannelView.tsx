import { useState } from "react";
import { useSending } from "../state/store";
import { send } from "../state/actions";
import { FileDrop, type StagedPayload } from "./FileDrop";
import { Timeline } from "./Timeline";
import { MembersFooter } from "./MembersFooter";
import type { Feed } from "../types";

/** Right pane: header, scrolling timeline, drop zone + send, devices footer. */
export function ChannelView({ channel }: { channel: Feed }) {
  const globalSending = useSending();
  const [staged, setStaged] = useState<StagedPayload[]>([]);
  // Local `sending` spans the whole batch: globalSending flickers false between
  // payloads, and both guard the button so a double-click can't fire two batches.
  const [sending, setSending] = useState(false);
  // Bumped after a batch to remount FileDrop, clearing its staged files + text.
  const [composerKey, setComposerKey] = useState(0);

  async function onSend() {
    if (staged.length === 0 || sending) return;
    setSending(true);
    try {
      // Sequential on purpose: the transfer slice and progress UI assume one
      // in-flight send, and each payload gets its own timeline row either way.
      // send() never throws — a bad payload fails its row, the rest still go.
      for (const payload of staged) {
        await send(payload);
      }
      setStaged([]);
      setComposerKey((k) => k + 1);
    } finally {
      setSending(false);
    }
  }

  const canSend = staged.length > 0 && !sending && !globalSending;
  const others = (channel.members_cache ?? []).filter(
    (m) => m.member_id !== channel.member_id,
  ).length;

  return (
    <section className="feed-view" aria-label="your channel">
      <header className="feed-head">
        <h2 className="feed-title">your channel</h2>
        <span className="dim small">
          {others} other device{others === 1 ? "" : "s"}
        </span>
      </header>

      <Timeline />

      <div className="feed-composer">
        <FileDrop
          key={composerKey}
          onChange={setStaged}
          disabled={sending || globalSending}
        />
        <button
          type="button"
          className="btn primary big"
          onClick={onSend}
          disabled={!canSend}
        >
          {sending
            ? "sending…"
            : staged.length > 1
            ? `send ${staged.length}`
            : "send"}
        </button>
      </div>

      <MembersFooter channel={channel} />
    </section>
  );
}
