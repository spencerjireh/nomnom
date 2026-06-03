import { useState } from "react";
import { useStore } from "../../state/store";
import { useTransfer } from "../../hooks/useTransfer";
import { FileDrop, type StagedPayload } from "../FileDrop";

export function SendTab() {
  const feeds = useStore((s) => s.feeds);
  const defaultFeed = useStore((s) => s.defaultFeed);
  const { send, busy } = useTransfer();
  const [feedName, setFeedName] = useState(defaultFeed ?? feeds[0]?.name ?? "");
  const [staged, setStaged] = useState<StagedPayload | null>(null);

  const feed = feeds.find((f) => f.name === feedName) ?? feeds[0];

  if (!feed) {
    return (
      <div className="tab-empty">
        <p>no feeds yet.</p>
        <p className="dim">open or join one in the Feeds tab, then broadcast into it.</p>
      </div>
    );
  }

  const recipients = (feed.members_cache ?? []).filter((m) => m.member_id !== feed.member_id).length;
  const canSend = !busy && !!staged;

  async function onSend() {
    if (!feed || !staged) return;
    const data = await staged.read();
    await send(feed, { name: staged.name, data });
  }

  return (
    <div className="tab">
      <label className="field">
        <span className="field-label">broadcast into</span>
        <select value={feed.name} onChange={(e) => setFeedName(e.target.value)} disabled={busy}>
          {feeds.map((f) => (
            <option key={f.name} value={f.name}>
              {f.name}
            </option>
          ))}
        </select>
      </label>
      <p className="kv small dim">
        {recipients} other member{recipients === 1 ? "" : "s"} will receive this.
      </p>

      <FileDrop onChange={setStaged} disabled={busy} />

      <button type="button" className="btn primary big" onClick={onSend} disabled={!canSend}>
        serve it up →
      </button>
    </div>
  );
}
