import { useState } from "react";
import { useStore } from "../state/store";
import { RelayClient } from "../relay/client";
import { Fingerprint } from "./Fingerprint";

export function Settings({ onClose }: { onClose: () => void }) {
  const identity = useStore((s) => s.identity);
  const relay = useStore((s) => s.relay);
  const setName = useStore((s) => s.setName);
  const factoryReset = useStore((s) => s.factoryReset);

  const [name, setNameDraft] = useState(identity?.name ?? "");
  const [showSecret, setShowSecret] = useState(false);
  const [health, setHealth] = useState<"idle" | "checking" | "ok" | "down">("idle");
  const [confirmReset, setConfirmReset] = useState(false);

  async function testRelay() {
    if (!relay) return;
    setHealth("checking");
    const ok = await new RelayClient(relay).health();
    setHealth(ok ? "ok" : "down");
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <section className="ticket modal settings">
        <div className="ticket-head">
          <span>** SETTINGS **</span>
          <button type="button" className="chip" onClick={onClose}>
            close
          </button>
        </div>

        <label className="field">
          <span className="field-label">your name</span>
          <div className="inline">
            <input value={name} onChange={(e) => setNameDraft(e.target.value)} />
            <button
              type="button"
              className="chip"
              onClick={() => setName(name.trim() || identity!.name)}
              disabled={!name.trim() || name === identity?.name}
            >
              save
            </button>
          </div>
        </label>
        {identity && (
          <p className="kv small">
            <span className="dim">device {identity.device_id} · fp</span>{" "}
            <Fingerprint ikHex={identity.ik_pub} />
          </p>
        )}

        <hr className="dashed" />

        <p className="kv small">
          <span className="dim">relay</span> {relay?.url}
        </p>
        <p className="kv small">
          <span className="dim">passphrase</span>{" "}
          <code>{showSecret ? relay?.secret : "••••••••••••"}</code>{" "}
          <button type="button" className="chip" onClick={() => setShowSecret((v) => !v)}>
            {showSecret ? "hide" : "show"}
          </button>
        </p>
        <button type="button" className="chip" onClick={testRelay}>
          {health === "checking" ? "checking…" : "test connection"}
        </button>
        {health === "ok" && <span className="ok-inline"> relay is up.</span>}
        {health === "down" && <span className="err"> relay unreachable.</span>}

        <hr className="dashed" />

        {confirmReset ? (
          <div className="reset-confirm">
            <p className="err">
              this wipes your identity, all paired devices, and the relay passphrase from this
              browser. you&apos;ll need to re-pair.
            </p>
            <div className="modal-actions">
              <button type="button" className="btn ghost" onClick={() => setConfirmReset(false)}>
                keep it
              </button>
              <button
                type="button"
                className="btn danger"
                onClick={() => {
                  factoryReset();
                  onClose();
                }}
              >
                wipe everything
              </button>
            </div>
          </div>
        ) : (
          <button type="button" className="btn danger" onClick={() => setConfirmReset(true)}>
            reset this device
          </button>
        )}
      </section>
    </div>
  );
}
