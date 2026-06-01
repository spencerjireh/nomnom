import { useState } from "react";
import { useStore } from "../../state/store";
import { Fingerprint } from "../Fingerprint";

function ago(unixSeconds?: number): string {
  if (!unixSeconds) return "never";
  const secs = Math.floor(Date.now() / 1000) - unixSeconds;
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

export function PeersTab() {
  const peers = useStore((s) => s.peers);
  const forgetPin = useStore((s) => s.forgetPin);
  const renamePin = useStore((s) => s.renamePin);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const entries = Object.entries(peers);
  if (entries.length === 0) {
    return (
      <div className="tab-empty">
        <p>the menu is empty.</p>
        <p className="dim">pair a device to add it here.</p>
      </div>
    );
  }

  return (
    <div className="tab">
      <ul className="peer-list">
        {entries.map(([id, pin]) => (
          <li key={id} className="peer">
            <div className="peer-main">
              {editing === id ? (
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    renamePin(id, draft.trim() || pin.name);
                    setEditing(null);
                  }}
                >
                  <input
                    autoFocus
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onBlur={() => setEditing(null)}
                  />
                </form>
              ) : (
                <strong>{pin.nickname || pin.name}</strong>
              )}
              <span className="dim small"> · {pin.transfer_count ?? 0} transfers · {ago(pin.last_transfer)}</span>
            </div>
            <div className="peer-fp">
              <Fingerprint ikHex={pin.ik_pub} />
            </div>
            <div className="peer-actions">
              <button
                type="button"
                className="chip"
                onClick={() => {
                  setEditing(id);
                  setDraft(pin.nickname || pin.name);
                }}
              >
                rename
              </button>
              <button type="button" className="chip danger" onClick={() => forgetPin(id)}>
                forget
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
