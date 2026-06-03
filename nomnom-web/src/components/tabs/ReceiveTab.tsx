import { useState } from "react";
import { useStore } from "../../state/store";
import { useTransfer } from "../../hooks/useTransfer";
import { fmtSize } from "../FileDrop";

export function ReceiveTab() {
  const feeds = useStore((s) => s.feeds);
  const defaultFeed = useStore((s) => s.defaultFeed);
  const received = useStore((s) => s.received);
  const { receive, cancel, busy } = useTransfer();
  const watching = useStore((s) => s.transfer.kind === "receive") && busy;
  const [feedName, setFeedName] = useState(defaultFeed ?? feeds[0]?.name ?? "");

  const feed = feeds.find((f) => f.name === feedName) ?? feeds[0];

  if (!feed) {
    return (
      <div className="tab-empty">
        <p>no feeds yet.</p>
        <p className="dim">open or join a feed first, then watch it for incoming files.</p>
      </div>
    );
  }

  return (
    <div className="tab">
      <label className="field">
        <span className="field-label">watch feed</span>
        <select value={feed.name} onChange={(e) => setFeedName(e.target.value)} disabled={watching}>
          {feeds.map((f) => (
            <option key={f.name} value={f.name}>
              {f.name}
            </option>
          ))}
        </select>
      </label>

      {watching ? (
        <button type="button" className="btn ghost big" onClick={cancel}>
          stop watching
        </button>
      ) : (
        <button type="button" className="btn primary big" onClick={() => receive(feed)} disabled={busy}>
          watch for files →
        </button>
      )}

      {received.length > 0 && (
        <>
          <hr className="dashed" />
          <p className="field-label">received this session</p>
          <ul className="peer-list">
            {received.map((r, i) => (
              <li key={i} className="peer">
                <div className="peer-main">
                  <strong>{r.name}</strong>
                  <span className="dim small">
                    {" "}
                    · {fmtSize(r.bytes)} · from {r.peerName}
                  </span>
                </div>
                <span className="dim small">saved to downloads</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
