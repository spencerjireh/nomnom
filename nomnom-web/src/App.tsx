import { useEffect } from "react";
import { useStore } from "./state/store";
import { Onboarding } from "./components/Onboarding";
import { TabShell } from "./components/TabShell";

export function App() {
  const relay = useStore((s) => s.relay);
  const identity = useStore((s) => s.identity);

  useEffect(() => {
    useStore.getState().hydrate();
  }, []);

  // Wait for hydrate() to seed identity before deciding which screen to show.
  if (!identity) return null;
  return relay ? <TabShell /> : <Onboarding />;
}
