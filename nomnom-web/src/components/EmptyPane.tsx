import { useState } from "react";
import { useStore, useSending } from "../state/store";
import { openChannel, joinChannel } from "../state/actions";
import { friendlyRelayMessage } from "../relay/errors";

/** Bootstrap pane shown when this device has no channel yet: paste a secret to
 * add this device (the common path), or — if this device owns a relay — create
 * the channel. */
export function EmptyPane({ onNeedRelay }: { onNeedRelay: () => void }) {
  const relay = useStore((s) => s.relay);
  const sending = useSending();

  const [secret, setSecret] = useState("");
  const [working, setWorking] = useState<null | "open" | "join">(null);
  const [error, setError] = useState<string | null>(null);

  const blocked = sending || working !== null;

  async function doJoin() {
    setError(null);
    setWorking("join");
    try {
      await joinChannel(secret.trim());
      setSecret("");
    } catch (e) {
      setError(friendlyRelayMessage(e));
    } finally {
      setWorking(null);
    }
  }

  async function doOpen() {
    setError(null);
    setWorking("open");
    try {
      await openChannel();
    } catch (e) {
      setError(friendlyRelayMessage(e));
    } finally {
      setWorking(null);
    }
  }

  return (
    <div className="empty-pane">
      <header className="empty-head">
        <h1 className="empty-title">add this device to your channel</h1>
        <p className="empty-sub dim">
          paste the channel secret from another device (run <code>nomnom channel</code>,
          or copy it from the channel here).
        </p>
      </header>

      <section className="empty-form">
        <span className="field-label">channel secret</span>
        <input
          placeholder="https://relay…/f/<token>"
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
          disabled={blocked}
        />
        <button
          type="button"
          className="btn primary"
          onClick={doJoin}
          disabled={blocked || !secret.trim()}
        >
          {working === "join" ? "joining…" : "join"}
        </button>
      </section>

      <section className="empty-form">
        <span className="field-label">no channel yet?</span>
        {relay ? (
          <button type="button" className="btn" onClick={doOpen} disabled={blocked}>
            {working === "open" ? "creating…" : "create a channel (you host)"}
          </button>
        ) : (
          <p className="dim small">
            creating a channel needs a relay.{" "}
            <button type="button" className="linklike" onClick={onNeedRelay}>
              set one up →
            </button>
          </p>
        )}
      </section>

      {error && <p className="err">{error}</p>}
    </div>
  );
}
