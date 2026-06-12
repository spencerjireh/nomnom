import { useStore } from "../state/store";

/** Left rail: brand, the single channel's status, and settings. Bootstrapping
 * (paste a secret / create a channel) lives in the main pane, not here. */
export function ChannelRail({ onOpenSettings }: { onOpenSettings: () => void }) {
  const channel = useStore((s) => s.channel);

  const others = channel
    ? (channel.members_cache ?? []).filter((m) => m.member_id !== channel.member_id).length
    : 0;

  return (
    <aside className="rail" aria-label="channel">
      <div className="rail-brand">
        <span className="logo">NOMNOM</span>
      </div>

      <div className="rail-channel">
        {channel ? (
          <p className="small">
            <span className="rail-channel-name">your channel</span>
            <span className="dim"> · {others + 1} device{others === 0 ? "" : "s"}</span>
          </p>
        ) : (
          <p className="dim small">no channel yet.</p>
        )}
      </div>

      <button type="button" className="rail-settings chip" onClick={onOpenSettings}>
        settings
      </button>
    </aside>
  );
}
