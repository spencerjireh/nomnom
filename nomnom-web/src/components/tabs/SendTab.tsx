import { useMemo, useState } from "react";
import { useStore } from "../../state/store";
import { useTransfer } from "../../hooks/useTransfer";
import { FileDrop, type StagedPayload } from "../FileDrop";
import { Fingerprint } from "../Fingerprint";

export function SendTab() {
  const peers = useStore((s) => s.peers);
  const { send, busy } = useTransfer();
  const peerList = useMemo(() => Object.entries(peers), [peers]);
  const [peerId, setPeerId] = useState<string>(peerList[0]?.[0] ?? "");
  const [staged, setStaged] = useState<StagedPayload | null>(null);

  const target = peers[peerId];
  const canSend = !busy && !!target && !!staged;

  async function onSend() {
    if (!target || !staged) return;
    const data = await staged.read();
    await send(
      { peerId, peerIk: target.ik_pub, peerName: target.nickname || target.name },
      { name: staged.name, data },
    );
  }

  if (peerList.length === 0) {
    return (
      <div className="tab-empty">
        <p>no devices on the menu yet.</p>
        <p className="dim">head to the Pair tab to add one.</p>
      </div>
    );
  }

  return (
    <div className="tab">
      <label className="field">
        <span className="field-label">to</span>
        <select value={peerId} onChange={(e) => setPeerId(e.target.value)} disabled={busy}>
          {peerList.map(([id, pin]) => (
            <option key={id} value={id}>
              {pin.nickname || pin.name}
            </option>
          ))}
        </select>
      </label>
      {target && (
        <p className="kv small">
          <span className="dim">fp</span> <Fingerprint ikHex={target.ik_pub} />
        </p>
      )}

      <FileDrop onChange={setStaged} disabled={busy} />

      <button type="button" className="btn primary big" onClick={onSend} disabled={!canSend}>
        serve it up →
      </button>
    </div>
  );
}
