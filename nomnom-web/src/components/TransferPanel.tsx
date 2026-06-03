import { useStore } from "../state/store";
import { useTransfer } from "../hooks/useTransfer";
import { fmtSize } from "./FileDrop";

/** Always mounted by the shell so progress survives tab switches. Renders the
 * current transfer's phase, an order-ticket progress bar, cancel, and the final
 * success/error line in diner voice. */
export function TransferPanel() {
  const transfer = useStore((s) => s.transfer);
  const { cancel } = useTransfer();

  if (transfer.phase === "idle") return null;

  const pct = Math.round(transfer.progress * 100);
  const active = transfer.phase !== "done" && transfer.phase !== "error";
  const r = transfer.result;

  return (
    <section className="ticket panel">
      <div className="ticket-head">
        <span>** ORDER TICKET **</span>
        <span className="dim">{transfer.kind}</span>
      </div>

      {active && (
        <>
          <p className="phase-label">{transfer.label || transfer.phase}…</p>
          {transfer.kind === "send" && (
            <>
              <div className="progress" aria-label={`${pct}%`}>
                <div className="progress-fill" style={{ width: `${pct}%` }} />
                <span className="progress-pct">{pct}%</span>
              </div>
            </>
          )}
          <button type="button" className="btn ghost" onClick={cancel}>
            {transfer.kind === "receive" ? "stop watching" : "cancel"}
          </button>
        </>
      )}

      {transfer.phase === "done" && (
        <div className="result ok">
          {transfer.kind === "send" && r?.name && (
            <p>
              served <strong>{r.name}</strong>
              {r.bytes != null && <span className="dim"> · {fmtSize(r.bytes)}</span>}
              {r.recipients != null && (
                <span> → {r.recipients} member{r.recipients === 1 ? "" : "s"}</span>
              )}
            </p>
          )}
          <p className="thanks">— thank you, come again —</p>
          <button
            type="button"
            className="btn ghost"
            onClick={() => useStore.getState().resetTransfer()}
          >
            clear
          </button>
        </div>
      )}

      {transfer.phase === "error" && (
        <div className="result err-result">
          <p className="err">86&apos;d: {transfer.error}</p>
          <button
            type="button"
            className="btn ghost"
            onClick={() => useStore.getState().resetTransfer()}
          >
            dismiss
          </button>
        </div>
      )}
    </section>
  );
}
