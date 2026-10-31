import { createHash } from "node:crypto";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { listenerWakeHub } from "../listenerWake.js";
import type { TogetherWorker } from "../netease/togetherWorker.js";
import type { InMemoryEventQueue } from "../queue.js";
import { normalizeReplyBubbles } from "../replyBubbles.js";
import type { BridgeEvent } from "../types.js";

export function registerBridgeTools(
  server: McpServer,
  queue: InMemoryEventQueue,
  eventFilter: (event: BridgeEvent) => boolean,
  togetherWorker: TogetherWorker,
): void {
  server.registerTool(
    "cove_bridge_reply",
    {
      title: "Reply through Cove Bridge",
      description: "Deliver the user-facing reply for the active Cove Bridge event back through its recorded reply route. For replyPolicy=required, call this exactly once before completing the turn. eventId is optional because the Bridge binds to the active required event and corrects stale ids. For normal conversation, messages[] should contain 2-5 short natural bubbles; a single bubble is only for a genuinely brief reply. Do not pack multiple sentences into one long bubble and do not add filler just to increase the count. The Bridge will also split an obviously long single bubble as a fallback. Do not call netease_together_send_message directly for routed replies.",
      inputSchema: {
        eventId: z.string().trim().min(1).optional(),
        messages: z.array(z.string().trim().min(1).max(500)).min(1).max(5),
      },
      outputSchema: {
        ok: z.literal(true),
        eventId: z.string(),
        route: z.literal("netease.chatroom"),
        sentCount: z.number(),
        completed: z.boolean(),
        deduplicated: z.boolean(),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        openWorldHint: true,
        idempotentHint: true,
      },
    },
    async ({ eventId, messages }) => {
      const outstanding = queue.getOutstandingRequiredReplyEvent(eventFilter);
      const rawRequested = eventId ? queue.getEvent(eventId) : null;
      const requested = rawRequested && eventFilter(rawRequested) ? rawRequested : null;
      const event = outstanding ?? requested;
      if (!event) {
        throw new Error(eventId
          ? `Unknown Cove Bridge event: ${eventId}`
          : "No active Cove Bridge event is awaiting a routed reply.");
      }

      const resolvedEventId = event.id;
      if (eventId && eventId !== resolvedEventId) {
        console.warn(
          `Cove Bridge reply eventId corrected: requested=${eventId} resolved=${resolvedEventId}`,
        );
      }
      if (!event.replyRoute) throw new Error(`Event has no reply route: ${resolvedEventId}`);
      if (event.replyRoute !== "netease.chatroom") {
        throw new Error(`Unsupported reply route: ${event.replyRoute}`);
      }

      const normalized = normalizeReplyBubbles(messages);
      const fingerprint = createHash("sha256")
        .update(JSON.stringify({ eventId: resolvedEventId, route: event.replyRoute, messages: normalized }))
        .digest("hex");
      const claim = queue.claimReply(resolvedEventId, fingerprint);

      if (claim.state === "already_completed" || claim.state === "in_progress") {
        return {
          structuredContent: {
            ok: true as const,
            eventId: resolvedEventId,
            route: "netease.chatroom" as const,
            sentCount: claim.sentCount,
            completed: claim.state === "already_completed",
            deduplicated: true,
          },
          content: [{
            type: "text",
            text: claim.state === "already_completed"
              ? "Reply was already delivered; duplicate send suppressed."
              : "The same reply is already being delivered; duplicate send suppressed.",
          }],
        };
      }

      try {
        let sentCount = claim.sentCount;
        for (let index = sentCount; index < normalized.length; index += 1) {
          await togetherWorker.sendChatRoomText(normalized[index]);
          sentCount = queue.markReplyMessageSent(resolvedEventId, fingerprint);
          if (index < normalized.length - 1) {
            await new Promise<void>((resolve) => setTimeout(resolve, 350));
          }
        }
        queue.markReplyCompleted(resolvedEventId, fingerprint);
        listenerWakeHub.wake("reply-completed");
        console.log(
          `Cove Bridge reply delivered: eventId=${resolvedEventId} route=${event.replyRoute} messages=${normalized.length}`,
        );
        return {
          structuredContent: {
            ok: true as const,
            eventId: resolvedEventId,
            route: "netease.chatroom" as const,
            sentCount,
            completed: true,
            deduplicated: false,
          },
          content: [{
            type: "text",
            text: `Delivered ${sentCount} chat bubble(s) through ${event.replyRoute}.`,
          }],
        };
      } catch (error) {
        queue.releaseReply(resolvedEventId, fingerprint);
        throw error;
      }
    },
  );

  server.registerTool(
    "cove_bridge_sync",
    {
      title: "Cove Bridge sync",
      description: "Reserve one pending test event for the Cove Bridge component.",
      inputSchema: {},
      outputSchema: {
        hasEvent: z.boolean(),
        eventId: z.string().optional(),
        awaitingReply: z.boolean().optional(),
      },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        openWorldHint: false,
        idempotentHint: false,
      },
      _meta: { ui: { visibility: ["app"] } },
    },
    async () => {
      const outstanding = queue.getOutstandingRequiredReplyEvent(eventFilter);
      if (outstanding) {
        return {
          structuredContent: {
            hasEvent: false,
            eventId: outstanding.id,
            awaitingReply: true,
          },
          content: [],
        };
      }

      const event = queue.reserveNext(eventFilter);
      if (!event) return { structuredContent: { hasEvent: false }, content: [] };
      console.log(
        `Cove Bridge event reserved: eventId=${event.id} stream=${event.stream} source=${event.source} replyPolicy=${event.replyPolicy ?? "none"}`,
      );
      return {
        structuredContent: { hasEvent: true, eventId: event.id, awaitingReply: false },
        content: [],
        _meta: { event },
      };
    },
  );

  server.registerTool(
    "cove_bridge_delivered",
    {
      title: "Cove Bridge delivered",
      description: "Mark a reserved event as accepted by the chat host.",
      inputSchema: { eventId: z.string().min(1) },
      outputSchema: { delivered: z.boolean() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        openWorldHint: false,
        idempotentHint: true,
      },
      _meta: { ui: { visibility: ["app"] } },
    },
    async ({ eventId }) => {
      queue.markDelivered(eventId);
      return { structuredContent: { delivered: true }, content: [] };
    },
  );

  server.registerTool(
    "cove_bridge_dismissed",
    {
      title: "Cove Bridge dismissed",
      description: "Mark a reserved event as terminal after the user or host dismisses the app-initiated message prompt. Required-reply backpressure is completed without sending a routed reply.",
      inputSchema: { eventId: z.string().min(1) },
      outputSchema: { dismissed: z.boolean() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        openWorldHint: false,
        idempotentHint: true,
      },
      _meta: { ui: { visibility: ["app"] } },
    },
    async ({ eventId }) => {
      queue.dismiss(eventId);
      listenerWakeHub.wake("dismissed");
      return { structuredContent: { dismissed: true }, content: [] };
    },
  );

  server.registerTool(
    "cove_bridge_release",
    {
      title: "Cove Bridge release",
      description: "Release a reserved event only when host dispatch failed before ui/message was handed off.",
      inputSchema: { eventId: z.string().min(1) },
      outputSchema: { released: z.boolean() },
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        openWorldHint: false,
        idempotentHint: true,
      },
      _meta: { ui: { visibility: ["app"] } },
    },
    async ({ eventId }) => {
      queue.release(eventId);
      listenerWakeHub.wake("released");
      return { structuredContent: { released: true }, content: [] };
    },
  );
}
