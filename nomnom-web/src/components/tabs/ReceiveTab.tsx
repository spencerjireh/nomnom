import { useStore } from "../../state/store";
import { useTransfer } from "../../hooks/useTransfer";

export function ReceiveTab() {
  const peerCount = useStore((s) => Object.keys(s.peers).length);
  const { receive, cancel, busy } = useTransfer();
  const receiving = useStore((s) => s.transfer.kind === "receive" && busy);

  if (peerCount === 0) {
    return (
      <div className="tab-empty">
        <p>no devices on the menu yet.</p>
        <p className="dim">pair a device first, then come back to receive.</p>
      </div>
    );
  }

  return (
    <div className="tab center">
      <p className="dim">
        listens for one order from any of your {peerCount} paired device
        {peerCount === 1 ? "" : "s"} for 30 seconds.
      </p>
      {receiving ? (
        <button type="button" className="btn ghost big" onClick={cancel}>
          stop listening
        </button>
      ) : (
        <button type="button" className="btn primary big" onClick={receive} disabled={busy}>
          receive (30s)
        </button>
      )}
    </div>
  );
}
