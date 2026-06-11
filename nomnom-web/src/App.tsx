import { useEffect } from "react";
import { useStore } from "./state/store";
import { Shell } from "./components/Shell";

export function App() {
  const identity = useStore((s) => s.identity);

  useEffect(() => {
    useStore.getState().hydrate();
  }, []);

  // The app is usable immediately — an identity is generated on first run and you
  // can join your channel by pasting its secret with no relay. Relay setup (needed
  // only to CREATE a channel) lives in Settings. Wait for hydrate() to seed the
  // identity before rendering.
  if (!identity) return null;
  return <Shell />;
}
