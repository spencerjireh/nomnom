import { useMemo, useState } from "react";
import { useStore } from "../state/store";
import { saveHeld, discardHeld } from "../state/actions";
import { fmtSize, clock } from "../util/format";
import {
  looksLikeText,
  looksLikeMarkdown,
  decodePreview,
  decodeText,
  PREVIEW_CAP_BYTES,
} from "../textPreview";
import { ViewerOverlay, MarkdownBody } from "./Viewer";
import type { TimelineEntry } from "../types";

/** The channel's session timeline. In-flight sends show an inline progress bar;
 * received rows keep their bytes (and the view / copy / save actions) until
 * discarded — discard removes the row entirely. */
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
  const [viewerOpen, setViewerOpen] = useState(false);
  const [renderMd, setRenderMd] = useState(false);
  const [copied, setCopied] = useState<"idle" | "ok" | "err">("idle");

  // Decode the body once, only if it sniffs as text. The body survives saving,
  // so the preview (and the actions it gates) stays available until discard.
  const preview = useMemo(() => {
    if (!row.body || !looksLikeText(row.body)) return null;
    return decodePreview(row.body);
  }, [row.body]);

  const isMarkdown = useMemo(
    () => (preview ? looksLikeMarkdown(row.name, preview.text) : false),
    [row.name, preview],
  );

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

  // A received file the user can act on: either its bytes are in memory (live
  // receipt / already-saved) or it was rebuilt from history and carries a
  // slot_id we can re-fetch on save.
  const hasFile = row.kind === "receive" && (!!row.body || !!row.slot_id);

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
        {row.status === "failed" && (
          <span className="row-stamp err">
            {" "}
            failed{row.error ? `: ${row.error}` : ""}
          </span>
        )}
        {row.kind === "receive" && row.error && (
          <span className="row-stamp err"> · {row.error}</span>
        )}
      </div>
      {expanded && preview && (
        <div className="row-slip">
          <div className="row-slip-tools">
            {isMarkdown && (
              <button
                type="button"
                className={renderMd ? "chip active" : "chip"}
                onClick={() => setRenderMd((r) => !r)}
              >
                {renderMd ? "raw" : "render md"}
              </button>
            )}
            <button type="button" className="chip" onClick={() => setViewerOpen(true)}>
              full screen ⤢
            </button>
          </div>
          {renderMd && isMarkdown ? (
            <MarkdownBody text={preview.text} />
          ) : (
            <pre className="row-slip-text">{preview.text}</pre>
          )}
          {preview.truncated && (
            <p className="row-slip-note dim small">
              showing the first {Math.round(PREVIEW_CAP_BYTES / 1024)} KB · full screen shows
              more, copy takes the whole file
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
      {hasFile && (
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
            {row.status === "saved" ? "save again" : "save to downloads"}
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
      {viewerOpen && row.body && (
        <ViewerOverlay
          name={row.name}
          bytes={row.bytes}
          body={row.body}
          isMarkdown={isMarkdown}
          rendered={renderMd}
          onToggleRendered={() => setRenderMd((r) => !r)}
          onClose={() => setViewerOpen(false)}
        />
      )}
    </>
  );
}
