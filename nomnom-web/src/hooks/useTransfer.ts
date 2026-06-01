// Bridges the framework-free orchestration state machines to the store. Each
// action builds an AbortController, wires onProgress/onTofu/onPin to store
// actions, and translates results/errors into the transfer slice.

import { useStore } from "../state/store";
import { runSend } from "../orchestration/send";
import { runReceivePinned } from "../orchestration/receive";
import { runPair } from "../orchestration/pair";
import { cryptoClient } from "../worker/cryptoClient";
import { friendlyRelayMessage } from "../relay/errors";

function wired(abort: AbortController) {
  const s = useStore.getState();
  return {
    onProgress: (phase: Parameters<typeof s.updateProgress>[0], label: string, frac: number) =>
      useStore.getState().updateProgress(phase, label, frac),
    onTofu: (req: Parameters<typeof s.requestTofu>[0]) => useStore.getState().requestTofu(req),
    onPin: (u: Parameters<typeof s.applyPin>[0]) => useStore.getState().applyPin(u),
    signal: abort.signal,
  };
}

function downloadBlob(name: string, body: ArrayBuffer): void {
  const url = URL.createObjectURL(new Blob([body], { type: "application/octet-stream" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke after the click has a chance to start the download.
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

export function useTransfer() {
  const phase = useStore((s) => s.transfer.phase);
  const busy = phase !== "idle" && phase !== "done" && phase !== "error";

  async function send(
    target: { peerId: string; peerIk: string; peerName: string },
    payload: { name: string; data: ArrayBuffer },
  ): Promise<void> {
    const s = useStore.getState();
    if (!s.identity || !s.relay) return;
    const abort = new AbortController();
    s.beginTransfer("send", abort);
    try {
      const result = await runSend({
        identity: s.identity,
        relay: s.relay,
        pins: s.peers,
        target,
        payload,
        ...wired(abort),
      });
      useStore.getState().finishTransfer(result);
    } catch (e) {
      // A user cancel aborts the in-flight promise; don't flip the idle state
      // back to an error.
      if (abort.signal.aborted) return;
      useStore.getState().failTransfer(friendlyRelayMessage(e));
    }
  }

  async function receive(): Promise<void> {
    const s = useStore.getState();
    if (!s.identity || !s.relay) return;
    const abort = new AbortController();
    s.beginTransfer("receive", abort);
    try {
      const outcome = await runReceivePinned({
        identity: s.identity,
        relay: s.relay,
        pins: s.peers,
        ...wired(abort),
      });
      downloadBlob(outcome.name, outcome.body);
      useStore.getState().finishTransfer({
        outName: outcome.name,
        bytes: outcome.bytes,
        peerName: outcome.peerName,
      });
    } catch (e) {
      // A user cancel aborts the in-flight promise; don't flip the idle state
      // back to an error.
      if (abort.signal.aborted) return;
      useStore.getState().failTransfer(friendlyRelayMessage(e));
    }
  }

  async function pair(): Promise<void> {
    const s = useStore.getState();
    if (!s.identity || !s.relay) return;
    const abort = new AbortController();
    s.beginTransfer("pair", abort);
    try {
      const result = await runPair({
        identity: s.identity,
        relay: s.relay,
        pins: s.peers,
        ...wired(abort),
      });
      useStore.getState().finishTransfer({ peerName: result.peerName });
    } catch (e) {
      // A user cancel aborts the in-flight promise; don't flip the idle state
      // back to an error.
      if (abort.signal.aborted) return;
      useStore.getState().failTransfer(friendlyRelayMessage(e));
    }
  }

  function cancel(): void {
    const t = useStore.getState().transfer;
    t.abort?.abort();
    cryptoClient.cancel();
    useStore.getState().resolveTofu(false);
    useStore.getState().resetTransfer();
  }

  return { send, receive, pair, cancel, busy };
}
