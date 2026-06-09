import { useStore } from "../state/store";
import { useTransfer } from "../hooks/useTransfer";
import { fmtSize } from "./FileDrop";
import type { TimelineEntry } from "../types";

function clock(at: number): string {
  const d = new Date(at);
  const h = d.getHours().toString().padStart(2, "0");
  const m = d.getMinutes().toString().padStart(2, "0");
  return `${h}:${m}`;
}

/** Per-feed session timeline. In-flight sends show an inline progress bar;
 * held receives show [save] / [discard]; everything else is a static row. */
export function Timeline({ feedName }: { feedName: string }) {
  const rows = useStore((s) => s.timelines[feedName]);
  const { saveHeld, discardHeld } = useTransfer();

  if (!rows || rows.length === 0) {
    return (
      <div className="timeline-empty dim">
        <p>nothing here yet.</p>
        <p className="small">drop a file below — or wait for one.</p>
      </div>
    );
  }

  return (
    <ol className="timeline" role="list">
      {rows.map((row) => (
        <li key={row.id} className={`timeline-row row-${row.status}`}>
          <Row row={row} feedName={feedName} onSave={saveHeld} onDiscard={discardHeld} />
        </li>
      ))}
    </ol>
  );
}

function Row({
  row,
  feedName,
  onSave,
  onDiscard,
}: {
  row: TimelineEntry;
  feedName: string;
  onSave: (feedName: string, id: string) => void;
  onDiscard: (feedName: string, id: string) => void;
}) {
  const arrow = row.kind === "receive" ? "←" : "→";
  const peer =
    row.kind === "receive"
      ? row.peerName ?? "(unknown)"
      : row.recipients != null
      ? `${row.recipients} other${row.recipients === 1 ? "" : "s"}`
      : "feed";

  return (
    <>
      <div className="row-head">
        <span className="row-time dim small">{clock(row.at)}</span>
        <span className="row-arrow">{arrow}</span>
        <span className="row-peer">{peer}</span>
        {row.status === "held" && <span className="row-tag dim small">held</span>}
      </div>
      <div className="row-body">
        <span className="row-name">{row.name}</span>
        <span className="dim small"> · {fmtSize(row.bytes)}</span>
        {row.status === "served" && <span className="row-stamp"> ✓ served</span>}
        {row.status === "saved" && <span className="row-stamp"> ✓ saved</span>}
        {row.status === "discarded" && <span className="row-stamp dim"> discarded</span>}
        {row.status === "failed" && (
          <span className="row-stamp err">
            {" "}
            failed{row.error ? `: ${row.error}` : ""}
          </span>
        )}
      </div>
      {row.status === "in_flight" && (
        <div
          className="row-progress"
          aria-label={`${Math.round((row.progress ?? 0) * 100)}%`}
        >
          <div
            className="row-progress-fill"
            style={{ width: `${Math.round((row.progress ?? 0) * 100)}%` }}
          />
        </div>
      )}
      {row.status === "held" && (
        <div className="row-held-actions">
          <button type="button" className="chip" onClick={() => onSave(feedName, row.id)}>
            save to downloads
          </button>
          <button
            type="button"
            className="chip danger"
            onClick={() => onDiscard(feedName, row.id)}
          >
            discard
          </button>
        </div>
      )}
    </>
  );
}
