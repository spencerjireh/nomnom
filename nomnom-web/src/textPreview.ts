// Heuristics for previewing a received file's bytes as text. nomnom beams
// arbitrary files; only UTF-8-decodable, non-binary payloads earn the inline
// view + copy affordance, so a .png or .zip body never renders as garbage and
// `clipboard.writeText` never gets a blob's worth of control bytes.

const SNIFF_BYTES = 4096;

/** Cap the *rendered* preview so a huge text file can't lock the main thread
 * while painting. Copy still takes the whole file — see `decodeText`. */
export const PREVIEW_CAP_BYTES = 256 * 1024;

/** The full-screen viewer affords a roomier cap; still bounded for the same
 * don't-lock-the-main-thread reason. */
export const FULL_VIEW_CAP_BYTES = 2 * 1024 * 1024;

/** True if `body` looks like UTF-8 text that's safe to show and copy. */
export function looksLikeText(body: ArrayBuffer): boolean {
  const n = Math.min(body.byteLength, SNIFF_BYTES);
  if (n === 0) return true; // empty file: trivially text, copies to ""
  const bytes = new Uint8Array(body, 0, n);
  let control = 0;
  for (let i = 0; i < n; i++) {
    const b = bytes[i];
    if (b === 0) return false; // a NUL byte is the classic binary tell
    // control chars other than tab / newline / carriage-return / form-feed
    if (b < 0x20 && b !== 0x09 && b !== 0x0a && b !== 0x0d && b !== 0x0c) control++;
  }
  if (control / n > 0.1) return false; // dense control bytes → binary
  // Valid UTF-8 across the sniffed prefix. `stream: true` tolerates a multibyte
  // character clipped at the SNIFF_BYTES boundary rather than falsely rejecting.
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(bytes, { stream: true });
  } catch {
    return false;
  }
  return true;
}

/** Decode the capped preview slice. Replacement chars are fine here — this is
 * for display only, and `looksLikeText` has already vouched for the prefix. */
export function decodePreview(
  body: ArrayBuffer,
  cap: number = PREVIEW_CAP_BYTES,
): { text: string; truncated: boolean } {
  const truncated = body.byteLength > cap;
  const slice = truncated ? body.slice(0, cap) : body;
  return { text: new TextDecoder("utf-8").decode(slice), truncated };
}

/** Decode the full body for a clipboard copy. */
export function decodeText(body: ArrayBuffer): string {
  return new TextDecoder("utf-8").decode(body);
}

/** Markdown tells, each matched at most once. Anchored to line starts where
 * the construct is line-shaped so prose mentioning "# 1 fan" doesn't count. */
const MD_SIGNALS: RegExp[] = [
  /^#{1,6} \S/m,            // ATX heading
  /^```/m,                  // fenced code block
  /^ {0,3}[-*+] \S/m,       // unordered list item
  /^ {0,3}\d+\. \S/m,       // ordered list item
  /^ {0,3}> \S/m,           // blockquote
  /\[[^\]\n]+\]\([^)\s]+\)/, // inline link
  /\*\*[^*\n]+\*\*/,        // bold
];

/** True if a text payload smells like markdown: a .md filename is decisive;
 * otherwise the preview must show at least two distinct markdown constructs
 * (one alone — a stray `**bold**`, a lone URL in brackets — is just prose). */
export function looksLikeMarkdown(name: string, text: string): boolean {
  if (/\.(md|markdown)$/i.test(name)) return true;
  let hits = 0;
  for (const re of MD_SIGNALS) {
    if (re.test(text) && ++hits >= 2) return true;
  }
  return false;
}
