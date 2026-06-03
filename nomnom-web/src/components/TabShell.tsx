import { useState } from "react";
import { useStore } from "../state/store";
import { SendTab } from "./tabs/SendTab";
import { ReceiveTab } from "./tabs/ReceiveTab";
import { FeedsTab } from "./tabs/FeedsTab";
import { Settings } from "./Settings";
import { TransferPanel } from "./TransferPanel";
import { TofuModal } from "./TofuModal";
import { Fingerprint } from "./Fingerprint";

type Tab = "send" | "receive" | "feeds";
const TABS: Tab[] = ["send", "receive", "feeds"];

export function TabShell() {
  const [tab, setTab] = useState<Tab>("feeds");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const identity = useStore((s) => s.identity);
  const relay = useStore((s) => s.relay);
  const feedCount = useStore((s) => s.feeds.length);
  const defaultFeed = useStore((s) => s.defaultFeed);

  return (
    <main className="scene">
      <div className="scene-inner">
        {/* Left: the menu board — brand + this device's standing. */}
        <aside className="rail">
          <div className="brand">
            <span className="logo">NOMNOM</span>
          </div>
          <p className="rail-tag">a warm place to drop a file</p>

          <hr className="dashed" />

          <dl className="rail-stats">
            <dt>guest</dt>
            <dd>{identity?.name}</dd>
            <dt>fingerprint</dt>
            <dd>{identity && <Fingerprint hex={identity.sig_pub} />}</dd>
            <dt>relay</dt>
            <dd className={relay ? "" : "dim"}>{relay ? "configured" : "none (join-only)"}</dd>
            <dt>feeds</dt>
            <dd>
              {feedCount}
              {defaultFeed && <span className="dim"> · default {defaultFeed}</span>}
            </dd>
          </dl>

          <button type="button" className="btn ghost rail-settings" onClick={() => setSettingsOpen(true)}>
            settings
          </button>

          <footer className="rail-foot dim small">keep your relay passphrase secret.</footer>
        </aside>

        {/* Right: the receipt — the working surface. */}
        <div className="ticket app-ticket">
          <header className="app-head">
            <div className="brand">
              <span className="logo small-logo">GUEST CHECK</span>
            </div>
            <span className="dim small">no. {identity?.device_id?.slice(0, 6)}</span>
          </header>

          <nav className="tabbar" role="tablist">
            {TABS.map((t) => (
              <button
                key={t}
                role="tab"
                aria-selected={tab === t}
                className={tab === t ? "tab-btn active" : "tab-btn"}
                onClick={() => setTab(t)}
              >
                {t}
              </button>
            ))}
          </nav>

          <div className="tab-body">
            {tab === "send" && <SendTab />}
            {tab === "receive" && <ReceiveTab />}
            {tab === "feeds" && <FeedsTab onNeedRelay={() => setSettingsOpen(true)} />}
          </div>

          <TransferPanel />
        </div>
      </div>

      {settingsOpen && <Settings onClose={() => setSettingsOpen(false)} />}
      <TofuModal />
    </main>
  );
}
