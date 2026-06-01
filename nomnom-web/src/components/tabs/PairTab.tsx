import { useStore } from "../../state/store";
import { useTransfer } from "../../hooks/useTransfer";
import { Fingerprint } from "../Fingerprint";

export function PairTab() {
  const identity = useStore((s) => s.identity);
  const { pair, cancel, busy } = useTransfer();
  const pairing = useStore((s) => s.transfer.kind === "pair" && busy);

  return (
    <div className="tab center">
      <p className="dim">
        run <code>nomnom pair</code> (or open this on another device) and tap pair on both — within
        30 seconds of each other.
      </p>
      {identity && (
        <p className="kv small">
          <span className="dim">your fp</span> <Fingerprint ikHex={identity.ik_pub} />
        </p>
      )}
      <p className="dim small">compare fingerprints out-of-band before you trust.</p>
      {pairing ? (
        <button type="button" className="btn ghost big" onClick={cancel}>
          cancel
        </button>
      ) : (
        <button type="button" className="btn primary big" onClick={pair} disabled={busy}>
          pair a device
        </button>
      )}
    </div>
  );
}
