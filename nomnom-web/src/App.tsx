import { useEffect } from "react";
import { useStore } from "./state/store";
import { TabShell } from "./components/TabShell";

export function App() {
  const identity = useStore((s) => s.identity);

  useEffect(() => {
    useStore.getState().hydrate();
  }, []);

  // The app is usable immediately — an identity is generated on first run and you
  // can join a feed by URL with no relay. Relay setup (needed only to OPEN a feed)
  // lives in Settings. Wait for hydrate() to seed the identity before rendering.
  if (!identity) return null;
  return <TabShell />;
}
