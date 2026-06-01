import { useState } from "react";
import { useStore } from "../state/store";
import { RelayClient } from "../relay/client";
import { DEFAULT_RELAY_URL } from "../config";

/** First-run gate (shown when no relay secret is stored). Passphrase-only: the
 * relay URL is prefilled and tucked under "advanced". */
export function Onboarding() {
  const setRelay = useStore((s) => s.setRelay);
  const [passphrase, setPassphrase] = useState("");
  const [url, setUrl] = useState(DEFAULT_RELAY_URL);
  const [advanced, setAdvanced] = useState(false);
  const [probing, setProbing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [force, setForce] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const secret = passphrase.trim();
    if (!secret) return;
    const cfg = { url: url.trim().replace(/\/+$/, ""), secret };
    if (force) {
      setRelay(cfg);
      return;
    }
    setError(null);
    setProbing(true);
    const reachable = await new RelayClient(cfg).health();
    setProbing(false);
    if (reachable) {
      setRelay(cfg);
    } else {
      setError("couldn't reach that relay. check the URL — or press again to save anyway.");
      setForce(true);
    }
  }

  return (
    <main className="onboarding">
      <form className="ticket onboard-ticket" onSubmit={submit}>
        <div className="ticket-head big-head">
          <span>NOMNOM</span>
          <span className="dim">guest check</span>
        </div>
        <p className="dim">paste your relay passphrase to open a tab.</p>

        <label className="field">
          <span className="field-label">passphrase</span>
          <textarea
            className="textbox"
            rows={2}
            placeholder="six words from `nomnom relay`…"
            value={passphrase}
            onChange={(e) => setPassphrase(e.target.value)}
            autoFocus
          />
        </label>

        <button type="button" className="chip" onClick={() => setAdvanced((v) => !v)}>
          {advanced ? "− advanced" : "+ advanced"}
        </button>
        {advanced && (
          <label className="field">
            <span className="field-label">relay url</span>
            <input value={url} onChange={(e) => setUrl(e.target.value)} />
          </label>
        )}

        {error && <p className="err">{error}</p>}

        <button type="submit" className="btn primary big" disabled={!passphrase.trim() || probing}>
          {probing ? "checking…" : force ? "save anyway →" : "open tab →"}
        </button>
        <p className="dim small">
          stored in this browser only. anyone with access to this device can read it.
        </p>
      </form>
    </main>
  );
}
