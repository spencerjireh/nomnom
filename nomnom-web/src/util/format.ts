// Presentational formatters shared across components, so e.g. the timeline
// doesn't have to import a util from the file-drop component.

/** Human byte size: "512 B", "1.2 KB", "34 MB" (decimal units). */
export function fmtSize(n: number): string {
  if (n < 1000) return `${n} B`;
  const u = ["KB", "MB", "GB"];
  let v = n / 1000;
  let i = 0;
  while (v >= 1000 && i < u.length - 1) {
    v /= 1000;
    i++;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
}

/** Wall-clock HH:MM for a millisecond timestamp. */
export function clock(at: number): string {
  const d = new Date(at);
  const h = d.getHours().toString().padStart(2, "0");
  const m = d.getMinutes().toString().padStart(2, "0");
  return `${h}:${m}`;
}

/** Relative time-left for a unix-seconds expiry: "5m left", "3h left", "2d left". */
export function expiry(unix: number): string {
  if (!unix) return "unknown";
  const secs = unix - Math.floor(Date.now() / 1000);
  if (secs <= 0) return "expired";
  if (secs < 3600) return `${Math.floor(secs / 60)}m left`;
  if (secs < 86_400) return `${Math.floor(secs / 3600)}h left`;
  return `${Math.floor(secs / 86_400)}d left`;
}
