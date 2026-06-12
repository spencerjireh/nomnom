import { useEffect, useState } from "react";
import { useStore } from "../state/store";
import { persistence } from "../state/persistence";
import { receive } from "../state/actions";
import { ChannelRail } from "./ChannelRail";
import { ChannelView } from "./ChannelView";
import { EmptyPane } from "./EmptyPane";
import { Settings } from "./Settings";
import { TofuModal } from "./TofuModal";

/** Two-pane shell. Left: brand + settings. Right: the channel timeline, or
 * (when there's no channel yet) the paste-a-secret / create-a-channel pane. */
export function Shell() {
  const identity = useStore((s) => s.identity);
  const channel = useStore((s) => s.channel);
  // Key the watch on feed_id only: roster/last_post_ts mutations on the
  // channel object would otherwise restart the loop on every poll.
  const channelFeedId = channel?.feed_id;

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [nicknameBannerDismissed, setNicknameBannerDismissed] = useState(false);
  // Desktop-only nicety; the mobile drill-down manages the rail itself. The
  // rail stays mounted either way — CSS hides it — so the <680px rules keep
  // working regardless of this flag.
  const [railCollapsed, setRailCollapsed] = useState(persistence.loadRailCollapsed);

  function toggleRail() {
    setRailCollapsed((c) => {
      persistence.saveRailCollapsed(!c);
      return !c;
    });
  }

  // Ambient watch: keep a receive loop running on the channel. Receiving needs
  // only the channel secret (feed key + host come from its URL) — NOT the relay
  // mint credential, which gates create() alone. So a join-only device (no relay
  // configured) still receives. Re-pairing aborts the old loop.
  useEffect(() => {
    if (!channelFeedId) return;
    const ch = useStore.getState().channel;
    if (!ch) return;
    const abort = new AbortController();
    receive(ch, abort.signal);
    return () => abort.abort();
  }, [channelFeedId]);

  if (!identity) return null;

  const showNicknameBanner =
    !!channel && identity.name === "web-guest" && !nicknameBannerDismissed;

  const shellClass = [
    "shell",
    channel ? "has-selection" : "",
    railCollapsed ? "rail-collapsed" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <main className={shellClass}>
      <ChannelRail onOpenSettings={() => setSettingsOpen(true)} onCollapse={toggleRail} />

      <button
        type="button"
        className="rail-expand chip"
        aria-label="show sidebar"
        aria-expanded={!railCollapsed}
        onClick={toggleRail}
      >
        »
      </button>

      <div className="shell-pane">
        {channel ? (
          <>
            {showNicknameBanner && (
              <div className="nick-banner small">
                <span>
                  you joined as <strong>{identity.name}</strong>.
                </span>
                <button
                  type="button"
                  className="linklike"
                  onClick={() => {
                    setSettingsOpen(true);
                    setNicknameBannerDismissed(true);
                  }}
                >
                  set a nickname →
                </button>
                <button
                  type="button"
                  className="chip"
                  onClick={() => setNicknameBannerDismissed(true)}
                >
                  dismiss
                </button>
              </div>
            )}
            <ChannelView channel={channel} />
          </>
        ) : (
          <EmptyPane onNeedRelay={() => setSettingsOpen(true)} />
        )}
      </div>

      {settingsOpen && (
        <Settings
          onClose={() => {
            setSettingsOpen(false);
            if (useStore.getState().identity?.name !== "web-guest") {
              setNicknameBannerDismissed(true);
            }
          }}
        />
      )}
      <TofuModal />
    </main>
  );
}
