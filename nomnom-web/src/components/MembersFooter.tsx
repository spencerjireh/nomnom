import { useState } from "react";
import { useStore } from "../state/store";
import { useSending } from "../state/store";
import { leaveChannel } from "../state/actions";
import { Fingerprint } from "./Fingerprint";
import { expiry } from "../util/format";
import type { Feed } from "../types";

/** Collapsible footer under the timeline: device list, auto-save toggle, copy
 * the channel secret, leave. Pure channel metadata — kept out of the timeline
 * so transfers stay the visual focus. */
export function MembersFooter({ channel }: { channel: Feed }) {
  const setAutoSave = useStore((s) => s.setAutoSave);
  const sending = useSending();
  const [copied, setCopied] = useState(false);
  const [confirmLeave, setConfirmLeave] = useState(false);

  const members = channel.members_cache ?? [];

  async function copyUrl() {
    try {
      await navigator.clipboard.writeText(channel.url);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard blocked — leave the URL visible in the details
    }
  }

  return (
    <details className="members-footer">
      <summary>
        devices ({members.length}){" "}
        <span className="dim small">· {expiry(channel.expires_at)}</span>
      </summary>

      <div className="members-grid">
        <ul className="member-list">
          {members.map((m) => (
            <li key={m.member_id} className="member small">
              <span>{m.name || "(no name)"}</span>
              {m.member_id === channel.member_id && <span className="dim"> · you</span>}{" "}
              <Fingerprint hex={m.identity_pubkey} />
            </li>
          ))}
        </ul>

        <label className="member-toggle">
          <input
            type="checkbox"
            checked={channel.auto_save}
            onChange={(e) => setAutoSave(e.target.checked)}
          />
          <span>auto-save files from this channel</span>
          <span className="dim small">
            off: every incoming file holds for [save] / [discard]. on: files write
            straight to Downloads.
          </span>
        </label>

        <div className="member-url">
          <code className="feed-url">{channel.url}</code>
          <button type="button" className="chip" onClick={copyUrl}>
            {copied ? "copied!" : "copy secret"}
          </button>
        </div>

        {confirmLeave ? (
          <div className="member-leave">
            <span className="err small">leave the channel on this device?</span>
            <button type="button" className="chip" onClick={() => setConfirmLeave(false)}>
              keep it
            </button>
            <button
              type="button"
              className="chip danger"
              onClick={() => leaveChannel()}
              disabled={sending}
            >
              leave
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="chip danger member-leave-trigger"
            onClick={() => setConfirmLeave(true)}
            disabled={sending}
          >
            leave channel
          </button>
        )}
      </div>
    </details>
  );
}
