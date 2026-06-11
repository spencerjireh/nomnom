// DOM side-effecting helpers used by the UI layer. Isolated here so the hook
// that uses them stays pure glue.

/** Trigger a browser download of `body` under `name`. */
export function downloadBlob(name: string, body: ArrayBuffer): void {
  const url = URL.createObjectURL(new Blob([body], { type: "application/octet-stream" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
