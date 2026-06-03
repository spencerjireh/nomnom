import { useState } from "react";
import { useStore } from "../state/store";
import { RelayClient } from "../relay/client";
import { Fingerprint } from "./Fingerprint";
import { DEFAULT_RELAY_URL } from "../config";

export function Settings({ onClose }: { onClose: () => void }) {
  const identity = useStore((s) => s.identity);
  const relay = useStore((s) => s.relay);
  const setName = useStore((s) => s.setName);
  const setRelay = useStore((s) => s.setRelay);
  const factoryReset = useStore((s) => s.factoryReset);

  const [name, setNameDraft] = useState(identity?.name ?? "");
  const [url, setUrl] = useState(relay?.url ?? DEFAULT_RELAY_URL);
  const [secret, setSecret] = useState(relay?.secret ?? "");
  const [showSecret, setShowSecret] = useState(false);
  const [health, setHealth] = useState<"idle" | "checking" | "ok" | "down">("idle");
  const [savedRelay, setSavedRelay] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);

  async function testRelay() {
    setHealth("checking");
    const ok = await new RelayClient({ url: url.trim().replace(/\/+$/, ""), secret }).health();
    setHealth(ok ? "ok" : "down");
  }

  function saveRelay() {
    if (!secret.trim()) return;
    setRelay({ url: url.trim().replace(/\/+$/, ""), secret: secret.trim() });
    setSavedRelay(true);
    setTimeout(() => setSavedRelay(false), 1500);
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
            <Fingerprint hex={identity.sig_pub} />
          </p>
        )}

        <hr className="dashed" />

        <p className="field-label">relay (only needed to open feeds)</p>
        <label className="field">
          <span className="field-label dim">url</span>
          <input value={url} onChange={(e) => setUrl(e.target.value)} />
        </label>
        <label className="field">
          <span className="field-label dim">passphrase</span>
          <div className="inline">
            <input
              type={showSecret ? "text" : "password"}
              placeholder="relay HMAC secret"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
            />
            <button type="button" className="chip" onClick={() => setShowSecret((v) => !v)}>
              {showSecret ? "hide" : "show"}
            </button>
          </div>
        </label>
        <div className="inline">
          <button type="button" className="chip" onClick={testRelay}>
            {health === "checking" ? "checking…" : "test"}
          </button>
          <button type="button" className="chip" onClick={saveRelay} disabled={!secret.trim()}>
            {savedRelay ? "saved!" : "save relay"}
          </button>
          {health === "ok" && <span className="ok-inline">relay is up.</span>}
          {health === "down" && <span className="err">relay unreachable.</span>}
        </div>

        <hr className="dashed" />

        {confirmReset ? (
          <div className="reset-confirm">
            <p className="err">
              this wipes your identity, all feeds and pins, and the relay passphrase from this
              browser. you&apos;ll need to re-open or re-join feeds.
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
