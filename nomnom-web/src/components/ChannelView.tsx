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
  const [staged, setStaged] = useState<StagedPayload | null>(null);
  // Local `sending` covers the staged.read() window before the transfer slice
  // flips globalSending; both guard the button so a double-click can't fire two
  // sends. (The store doesn't know about the pre-send file read.)
  const [sending, setSending] = useState(false);

  async function onSend() {
    if (!staged || sending) return;
    setSending(true);
    try {
      const data = await staged.read();
      await send({ name: staged.name, data });
      setStaged(null);
    } finally {
      setSending(false);
    }
  }

  const canSend = !!staged && !sending && !globalSending;
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
        <FileDrop onChange={setStaged} disabled={sending || globalSending} />
        <button
          type="button"
          className="btn primary big"
          onClick={onSend}
          disabled={!canSend}
        >
          {sending ? "sending…" : "send"}
        </button>
      </div>

      <MembersFooter channel={channel} />
    </section>
  );
}
