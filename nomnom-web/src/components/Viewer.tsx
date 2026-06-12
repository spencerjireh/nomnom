import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fmtSize } from "../util/format";
import { decodePreview, decodeText, FULL_VIEW_CAP_BYTES } from "../textPreview";

/** Rendered markdown. Raw HTML in the source is escaped by react-markdown's
 * defaults; images become links so opting into a render never auto-fetches a
 * remote URL (that's a read receipt the sender's content shouldn't get). */
export function MarkdownBody({ text }: { text: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node: _node, ...props }) => (
            <a {...props} target="_blank" rel="noopener noreferrer" />
          ),
          img: ({ node: _node, src, alt }) => (
            <a href={typeof src === "string" ? src : undefined} target="_blank" rel="noopener noreferrer">
              [image{alt ? `: ${alt}` : ""}]
            </a>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

/** Full-screen overlay for a received text payload. Esc or a backdrop click
 * closes; the rendered/raw choice is owned by the timeline row so it survives
 * closing and reopening. */
export function ViewerOverlay({
  name,
  bytes,
  body,
  isMarkdown,
  rendered,
  onToggleRendered,
  onClose,
}: {
  name: string;
  bytes: number;
  body: ArrayBuffer;
  isMarkdown: boolean;
  rendered: boolean;
  onToggleRendered: () => void;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState<"idle" | "ok" | "err">("idle");
  const view = useMemo(() => decodePreview(body, FULL_VIEW_CAP_BYTES), [body]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function copyText() {
    try {
      await navigator.clipboard.writeText(decodeText(body));
      setCopied("ok");
    } catch {
      setCopied("err");
    }
    window.setTimeout(() => setCopied("idle"), 1400);
  }

  // Portal to <body>: the timeline pane's entry animation leaves an identity
  // transform behind (fill-mode both), which would otherwise make it the
  // containing block for this fixed-position backdrop.
  return createPortal(
    <div
      className="modal-backdrop viewer-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label={`viewing ${name}`}
      onClick={onClose}
    >
      <section className="viewer" onClick={(e) => e.stopPropagation()}>
        <header className="viewer-head">
          <span className="row-name">{name}</span>
          <span className="dim small">{fmtSize(bytes)}</span>
          <span className="viewer-tools">
            {isMarkdown && (
              <button
                type="button"
                className={rendered ? "chip active" : "chip"}
                onClick={onToggleRendered}
              >
                {rendered ? "raw" : "render md"}
              </button>
            )}
            <button type="button" className="chip" onClick={copyText}>
              {copied === "ok" ? "copied ✓" : copied === "err" ? "copy failed" : "copy text"}
            </button>
            <button type="button" className="chip" onClick={onClose}>
              close ✕
            </button>
          </span>
        </header>
        <div className="viewer-body">
          {rendered && isMarkdown ? (
            <MarkdownBody text={view.text} />
          ) : (
            <pre className="row-slip-text viewer-text">{view.text}</pre>
          )}
          {view.truncated && (
            <p className="row-slip-note dim small">
              showing the first {Math.round(FULL_VIEW_CAP_BYTES / (1024 * 1024))} MB · copy takes
              the whole file
            </p>
          )}
        </div>
      </section>
    </div>,
    document.body,
  );
}
