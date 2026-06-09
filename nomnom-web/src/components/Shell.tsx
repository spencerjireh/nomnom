import { useEffect, useState } from "react";
import { useStore } from "../state/store";
import { useTransfer } from "../hooks/useTransfer";
import { FeedRail } from "./FeedRail";
import { FeedView } from "./FeedView";
import { EmptyPane } from "./EmptyPane";
import { Settings } from "./Settings";
import { TofuModal } from "./TofuModal";

/** Two-pane shell. Left: feed list. Right: selected feed timeline or
 * (when nothing's selected) the warm open/join empty state. */
export function Shell() {
  const identity = useStore((s) => s.identity);
  const feeds = useStore((s) => s.feeds);
  const selectedFeed = useStore((s) => s.selectedFeed);
  const selectFeed = useStore((s) => s.selectFeed);
  const relay = useStore((s) => s.relay);
  const { receive } = useTransfer();

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [nicknameBannerDismissed, setNicknameBannerDismissed] = useState(false);

  const feed = feeds.find((f) => f.name === selectedFeed) ?? null;

  // Ambient watch: keep a receive loop running on the selected feed whenever
  // a relay is configured. Switching cancels the previous loop via abort.
  useEffect(() => {
    if (!feed || !relay) return;
    const abort = new AbortController();
    receive(feed, abort.signal);
    return () => abort.abort();
    // We deliberately key only on feed name + relay url+secret: feed mutations
    // like roster updates re-trigger receive otherwise.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [feed?.name, relay?.url, relay?.secret]);

  if (!identity) return null;

  const showNicknameBanner =
    !!feed &&
    identity.name === "web-guest" &&
    !nicknameBannerDismissed;

  return (
    <main className={selectedFeed ? "shell has-selection" : "shell"}>
      <FeedRail onOpenSettings={() => setSettingsOpen(true)} />

      <div className="shell-pane">
        {feed ? (
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
            <FeedView feed={feed} onBack={() => selectFeed(null)} />
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
