import { useState } from "react";
import { useStore } from "../state/store";
import { SendTab } from "./tabs/SendTab";
import { ReceiveTab } from "./tabs/ReceiveTab";
import { PairTab } from "./tabs/PairTab";
import { PeersTab } from "./tabs/PeersTab";
import { Settings } from "./Settings";
import { TransferPanel } from "./TransferPanel";
import { TofuModal } from "./TofuModal";

type Tab = "send" | "receive" | "pair" | "peers";
const TABS: Tab[] = ["send", "receive", "pair", "peers"];

export function TabShell() {
  const [tab, setTab] = useState<Tab>("send");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const identity = useStore((s) => s.identity);

  return (
    <main className="shell">
      <div className="ticket app-ticket">
        <header className="app-head">
          <div className="brand">
            <span className="logo">NOMNOM</span>
            <span className="dim small">guest check</span>
          </div>
          <button type="button" className="chip" onClick={() => setSettingsOpen(true)}>
            settings
          </button>
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
          {tab === "pair" && <PairTab />}
          {tab === "peers" && <PeersTab />}
        </div>

        <TransferPanel />

        <footer className="app-foot dim small">
          guest: {identity?.name} · keep this passphrase secret
        </footer>
      </div>

      {settingsOpen && <Settings onClose={() => setSettingsOpen(false)} />}
      <TofuModal />
    </main>
  );
}
