import { useEffect, useState } from "react";
import { useStore } from "../state/store";
import { useTransfer } from "../hooks/useTransfer";
import { FeedRail } from "./FeedRail";
import { FeedView } from "./FeedView";
import { EmptyPane } from "./EmptyPane";
import { Settings } from "./Settings";
import { TofuModal } from "./TofuModal";

/** Two-pane shell. Left: brand + settings. Right: the channel timeline, or
 * (when there's no channel yet) the paste-a-secret / create-a-channel pane. */
export function Shell() {
  const identity = useStore((s) => s.identity);
  const channel = useStore((s) => s.channel);
  const { receive } = useTransfer();

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [nicknameBannerDismissed, setNicknameBannerDismissed] = useState(false);

  // Ambient watch: keep a receive loop running on the channel. Receiving needs
  // only the channel secret (feed key + host come from its URL) — NOT the relay
  // mint credential, which gates create() alone. So a join-only device (no relay
  // configured) still receives. Re-pairing aborts the old loop.
  useEffect(() => {
    if (!channel) return;
    const abort = new AbortController();
    receive(channel, abort.signal);
    return () => abort.abort();
    // Key only on feed_id: roster/last_post_ts mutations would otherwise restart
    // the loop on every poll.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channel?.feed_id]);

  if (!identity) return null;

  const showNicknameBanner =
    !!channel && identity.name === "web-guest" && !nicknameBannerDismissed;

  return (
    <main className={channel ? "shell has-selection" : "shell"}>
      <FeedRail onOpenSettings={() => setSettingsOpen(true)} />

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
            <FeedView feed={channel} />
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
