import type { BridgeEvent, StoredBridgeEvent } from "./types.js";

export type ReplyClaim =
  | { state: "send"; sentCount: number }
  | { state: "in_progress"; sentCount: number }
  | { state: "already_completed"; sentCount: number };

type StreamCounts = {
  pending: number;
  reserved: number;
  delivered: number;
  total: number;
};

export type QueueStatus = StreamCounts & {
  conversation: StreamCounts;
  state: StreamCounts;
};

const STATE_HISTORY_PER_KEY = 12;

function emptyCounts(): StreamCounts {
  return { pending: 0, reserved: 0, delivered: 0, total: 0 };
}

export class InMemoryEventQueue {
  private readonly conversationRows = new Map<string, StoredBridgeEvent>();
  private readonly stateRows = new Map<string, StoredBridgeEvent>();
  private readonly statePendingByKey = new Map<string, string>();

  enqueue(event: BridgeEvent): boolean {
    if (this.getRow(event.id)) return false;

    if (event.stream === "state") {
      const key = event.stateKey ?? event.source;
      const previousPendingId = this.statePendingByKey.get(key);
      if (previousPendingId) {
        const previous = this.stateRows.get(previousPendingId);
        if (previous?.status === "pending") this.stateRows.delete(previousPendingId);
      }

      this.stateRows.set(event.id, { event, status: "pending" });
      this.statePendingByKey.delete(key);
      this.statePendingByKey.set(key, event.id);
      this.pruneStateHistory(key);
      return true;
    }

    this.conversationRows.set(event.id, { event, status: "pending" });
    return true;
  }

  getEvent(eventId: string): BridgeEvent | null {
    return this.getRow(eventId)?.event ?? null;
  }

  getOutstandingRequiredReplyEvent(filter?: (event: BridgeEvent) => boolean): BridgeEvent | null {
    for (const row of this.conversationRows.values()) {
      if (filter && !filter(row.event)) continue;
      if (row.event.replyPolicy !== "required") continue;
      if (row.reply?.completed) continue;
      if (row.status === "reserved" || row.status === "delivered") return row.event;
    }
    return null;
  }

  reserveNext(filter?: (event: BridgeEvent) => boolean): BridgeEvent | null {
    if (this.getOutstandingRequiredReplyEvent(filter)) return null;

    for (const row of this.conversationRows.values()) {
      if (row.status !== "pending") continue;
      if (filter && !filter(row.event)) continue;
      row.status = "reserved";
      return row.event;
    }

    for (const [key, eventId] of this.statePendingByKey) {
      const row = this.stateRows.get(eventId);
      if (!row || row.status !== "pending") {
        this.statePendingByKey.delete(key);
        continue;
      }
      if (filter && !filter(row.event)) continue;
      row.status = "reserved";
      this.statePendingByKey.delete(key);
      return row.event;
    }

    return null;
  }

  markDelivered(eventId: string): void {
    const row = this.getRow(eventId);
    if (!row) throw new Error(`Unknown event: ${eventId}`);
    if (row.status === "delivered") return;
    if (row.status !== "reserved") {
      throw new Error(`Event is not reserved: ${eventId}`);
    }
    row.status = "delivered";
  }

  release(eventId: string): void {
    const row = this.getRow(eventId);
    if (!row || row.status === "delivered") return;

    if (row.event.stream === "state") {
      const key = row.event.stateKey ?? row.event.source;
      const newerPendingId = this.statePendingByKey.get(key);
      if (newerPendingId && newerPendingId !== eventId) {
        this.stateRows.delete(eventId);
        return;
      }
      row.status = "pending";
      this.statePendingByKey.delete(key);
      this.statePendingByKey.set(key, eventId);
      return;
    }

    row.status = "pending";
  }

  dismiss(eventId: string): void {
    const row = this.getRow(eventId);
    if (!row) throw new Error(`Unknown event: ${eventId}`);
    if (row.status === "pending") {
      throw new Error(`Event is not reserved: ${eventId}`);
    }

    row.status = "delivered";

    if (row.event.replyPolicy !== "required") return;

    if (row.reply) {
      row.reply.completed = true;
      row.reply.inFlight = false;
      row.reply.completedAt = new Date().toISOString();
      return;
    }

    row.reply = {
      fingerprint: "__dismissed__",
      sentCount: 0,
      completed: true,
      inFlight: false,
      completedAt: new Date().toISOString(),
    };
  }

  claimReply(eventId: string, fingerprint: string): ReplyClaim {
    const row = this.getRow(eventId);
    if (!row) throw new Error(`Unknown event: ${eventId}`);
    const reply = row.reply;

    if (!reply) {
      row.reply = {
        fingerprint,
        sentCount: 0,
        completed: false,
        inFlight: true,
      };
      return { state: "send", sentCount: 0 };
    }

    if (reply.completed) {
      return { state: "already_completed", sentCount: reply.sentCount };
    }
    if (reply.fingerprint !== fingerprint) {
      throw new Error(`Event already has a different reply: ${eventId}`);
    }
    if (reply.inFlight) {
      return { state: "in_progress", sentCount: reply.sentCount };
    }

    reply.inFlight = true;
    return { state: "send", sentCount: reply.sentCount };
  }

  markReplyMessageSent(eventId: string, fingerprint: string): number {
    const row = this.getRow(eventId);
    if (!row?.reply || row.reply.fingerprint !== fingerprint) {
      throw new Error(`Reply is not claimed: ${eventId}`);
    }
    row.reply.sentCount += 1;
    return row.reply.sentCount;
  }

  markReplyCompleted(eventId: string, fingerprint: string): void {
    const row = this.getRow(eventId);
    if (!row?.reply || row.reply.fingerprint !== fingerprint) {
      throw new Error(`Reply is not claimed: ${eventId}`);
    }
    row.reply.completed = true;
    row.reply.inFlight = false;
    row.reply.completedAt = new Date().toISOString();
  }

  releaseReply(eventId: string, fingerprint: string): void {
    const row = this.getRow(eventId);
    if (!row?.reply || row.reply.fingerprint !== fingerprint || row.reply.completed) return;
    row.reply.inFlight = false;
  }

  status(): QueueStatus {
    const conversation = this.countRows(this.conversationRows);
    const state = this.countRows(this.stateRows);
    return {
      pending: conversation.pending + state.pending,
      reserved: conversation.reserved + state.reserved,
      delivered: conversation.delivered + state.delivered,
      total: conversation.total + state.total,
      conversation,
      state,
    };
  }

  private getRow(eventId: string): StoredBridgeEvent | undefined {
    return this.conversationRows.get(eventId) ?? this.stateRows.get(eventId);
  }

  private countRows(rows: Map<string, StoredBridgeEvent>): StreamCounts {
    const counts = emptyCounts();
    counts.total = rows.size;
    for (const row of rows.values()) counts[row.status] += 1;
    return counts;
  }

  private pruneStateHistory(key: string): void {
    const deliveredIds: string[] = [];
    for (const [eventId, row] of this.stateRows) {
      if ((row.event.stateKey ?? row.event.source) !== key) continue;
      if (row.status === "delivered") deliveredIds.push(eventId);
    }
    const overflow = deliveredIds.length - STATE_HISTORY_PER_KEY;
    for (let index = 0; index < overflow; index += 1) {
      this.stateRows.delete(deliveredIds[index]);
    }
  }
}
