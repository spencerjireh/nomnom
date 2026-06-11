import { useState } from "react";
import { useStore } from "../state/store";
import { RelayClient, type AuthCheck } from "../relay/client";
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
  const [check, setCheck] = useState<"idle" | "checking" | AuthCheck>("idle");
  const [savedRelay, setSavedRelay] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);

  // Actually exercise the passphrase against the relay, rather than just pinging
  // /health (which takes no auth and so can't tell a good secret from a bad one).
  async function verify(): Promise<void> {
    setCheck("checking");
    const trimmed = secret.trim();
    const result = await new RelayClient({
      url: url.trim().replace(/\/+$/, ""),
      secret: trimmed,
    }).verifyAuth();
    setCheck(result);
  }

  function saveRelay() {
    if (!secret.trim()) return;
    setRelay({ url: url.trim().replace(/\/+$/, ""), secret: secret.trim() });
    setSavedRelay(true);
    setTimeout(() => setSavedRelay(false), 1500);
    // Auto-validate on save so a silently-wrong secret can't slip through.
    void verify();
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

        <p className="field-label">relay (only needed to create a channel)</p>
        <label className="field">
          <span className="field-label dim">url</span>
          <input value={url} onChange={(e) => setUrl(e.target.value)} />
        </label>
        <label className="field">
          <span className="field-label dim">
            passphrase
            {secret.length > 0 && <span className="char-count"> · {secret.length} chars</span>}
          </span>
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
          <button
            type="button"
            className="chip"
            onClick={verify}
            disabled={!secret.trim() || check === "checking"}
          >
            {check === "checking" ? "checking…" : "test"}
          </button>
          <button type="button" className="chip" onClick={saveRelay} disabled={!secret.trim()}>
            {savedRelay ? "saved!" : "save relay"}
          </button>
          {check === "ok" && <span className="ok-inline">passphrase accepted.</span>}
          {check === "rejected" && (
            <span className="err">relay rejected this passphrase — check it's the full secret.</span>
          )}
          {check === "skew" && (
            <span className="err">passphrase ok, but this device's clock is off — fix the system time.</span>
          )}
          {check === "unreachable" && <span className="err">relay unreachable.</span>}
        </div>

        <hr className="dashed" />

        {confirmReset ? (
          <div className="reset-confirm">
            <p className="err">
              this wipes your identity, the channel and pins, and the relay passphrase from this
              browser. you&apos;ll need to re-create or re-join your channel.
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
