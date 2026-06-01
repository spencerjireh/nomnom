// Tracks slots this client PUT so they can be best-effort deleted on completion,
// cancel, or error — mirrors the `authored`/`cleanup_authored` pattern in
// nomnom.py's relay orchestrators. Deletes never throw.

import type { RelayClient } from "../relay/client";

export class AuthoredSlots {
  private readonly slots: string[] = [];
  constructor(private readonly client: RelayClient) {}

  add(slotId: string): void {
    this.slots.push(slotId);
  }

  async cleanup(): Promise<void> {
    await Promise.all(this.slots.map((s) => this.client.deleteSlot(s)));
    this.slots.length = 0;
  }
}
