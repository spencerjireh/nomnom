import { useMemo, useState } from "react";
import { useStore } from "../state/store";
import { saveHeld, discardHeld } from "../state/actions";
import { fmtSize, clock } from "../util/format";
import { looksLikeText, decodePreview, decodeText, PREVIEW_CAP_BYTES } from "../textPreview";
import type { TimelineEntry } from "../types";

/** The channel's session timeline. In-flight sends show an inline progress bar;
 * held receives show [save] / [discard]; everything else is a static row. */
export function Timeline() {
  const rows = useStore((s) => s.timeline);

  if (rows.length === 0) {
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
          <Row row={row} onSave={saveHeld} onDiscard={discardHeld} />
        </li>
      ))}
    </ol>
  );
}

function Row({
  row,
  onSave,
  onDiscard,
}: {
  row: TimelineEntry;
  onSave: (id: string) => void;
  onDiscard: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState<"idle" | "ok" | "err">("idle");

  // Decode the held body once, only if it sniffs as text. Re-runs when the body
  // is cleared by save/discard, dropping the preview and collapsing the slip.
  const preview = useMemo(() => {
    if (row.status !== "held" || !row.body || !looksLikeText(row.body)) return null;
    return decodePreview(row.body);
  }, [row.status, row.body]);

  async function copyText() {
    if (!row.body) return;
    try {
      await navigator.clipboard.writeText(decodeText(row.body));
      setCopied("ok");
    } catch {
      setCopied("err");
    }
    window.setTimeout(() => setCopied("idle"), 1400);
  }

  const arrow = row.kind === "receive" ? "←" : "→";
  const peer =
    row.kind === "receive"
      ? row.peerName ?? "(unknown)"
      : row.recipients != null
      ? `${row.recipients} device${row.recipients === 1 ? "" : "s"}`
      : "channel";

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
        {preview && (
          <button
            type="button"
            className="row-view-toggle"
            aria-expanded={expanded}
            onClick={() => setExpanded((e) => !e)}
          >
            {expanded ? "hide ▾" : "view ▸"}
          </button>
        )}
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
      {expanded && preview && (
        <div className="row-slip">
          <pre className="row-slip-text">{preview.text}</pre>
          {preview.truncated && (
            <p className="row-slip-note dim small">
              showing the first {Math.round(PREVIEW_CAP_BYTES / 1024)} KB · copy takes the whole
              file
            </p>
          )}
          {copied !== "idle" && (
            <span className={`row-copied-stamp ${copied}`} role="status">
              {copied === "ok" ? "copied ✓" : "copy failed"}
            </span>
          )}
        </div>
      )}
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
          {preview && (
            <button
              type="button"
              className={`chip ${copied === "ok" ? "active" : ""}`}
              onClick={copyText}
            >
              {copied === "ok" ? "copied ✓" : copied === "err" ? "copy failed" : "copy text"}
            </button>
          )}
          <button type="button" className="chip" onClick={() => onSave(row.id)}>
            save to downloads
          </button>
          <button
            type="button"
            className="chip danger"
            onClick={() => onDiscard(row.id)}
          >
            discard
          </button>
        </div>
      )}
    </>
  );
}
