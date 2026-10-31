import type { BridgeEvent, BridgeStream, ReplyPolicy, ReplyRoute } from "../types.js";

function replyRoutingFor(source: string): { replyRoute?: ReplyRoute; replyPolicy?: ReplyPolicy } {
  if (source === "netease.chatroom") {
    return { replyRoute: "netease.chatroom", replyPolicy: "required" };
  }
  if (source.startsWith("netease.")) {
    return { replyRoute: "netease.chatroom", replyPolicy: "optional" };
  }
  return {};
}

function streamRoutingFor(source: string): { stream: BridgeStream; stateKey?: string } {
  if (source === "netease.chatroom") return { stream: "conversation" };
  if (source.startsWith("netease.")) {
    return { stream: "state", stateKey: "netease.together.presence" };
  }
  return { stream: "conversation" };
}

export function buildBridgeEvent(
  id: string,
  source: string,
  text: string,
  createdAt = new Date().toISOString(),
  additionalModelContext?: string,
): BridgeEvent {
  const routing = replyRoutingFor(source);
  const streamRouting = streamRoutingFor(source);
  const routeLines = routing.replyRoute
    ? [
        "replyRoute=" + routing.replyRoute,
        "replyPolicy=" + (routing.replyPolicy ?? "optional"),
        routing.replyPolicy === "required"
          ? "ROUTING CONTRACT (mandatory): before completing this turn, call cove_bridge_reply exactly once and put the complete user-facing reply in messages[]. A reply shown only in ChatGPT is incomplete because the user is waiting in NetEase ChatRoom. For normal conversation, default to 2-5 short natural bubbles; use one bubble only for a genuinely brief reply. Do not pack a multi-sentence reply into one bubble, and do not invent filler merely to increase the count. eventId may be omitted because the Bridge binds to the active required event. Do not use netease_together_send_message directly."
          : "ROUTING CONTRACT (optional): if you choose to send a user-facing reaction for this event, deliver it through cove_bridge_reply using this exact eventId. If no reaction is useful, do not call the reply tool.",
      ]
    : [];

  const originContext = source === "netease.chatroom"
    ? [
        "MESSAGE ORIGIN: NetEase ChatRoom.",
        "The human typed this message inside NetEase Cloud Music, NOT in the ChatGPT conversation window.",
        "Treat the bridged text as the current foreground user message from NetEase.",
        "Any user-facing answer to this turn must be routed back to NetEase ChatRoom through cove_bridge_reply before the turn is complete.",
      ]
    : source.startsWith("netease.")
      ? [
          "MESSAGE ORIGIN: NetEase Listen Together state/event stream.",
          "This is NOT a direct user chat message. Do not confuse it with something the human typed in ChatGPT or NetEase ChatRoom.",
        ]
      : [
          "This message entered through Cove Bridge.",
          "Treat the visible text as the current foreground user message.",
        ];

  const visibleText = source === "netease.chatroom"
    ? "【网易云聊天室】\n" + text
    : source.startsWith("netease.")
      ? "【网易云一起听状态】\n" + text
      : text;

  return {
    id,
    correlationId: id,
    kind: "message",
    source,
    ...streamRouting,
    ...routing,
    createdAt,
    visibleText,
    modelContext: [
      ...originContext,
      "eventId=" + id,
      "correlationId=" + id,
      "source=" + source,
      "stream=" + streamRouting.stream,
      ...(streamRouting.stateKey ? ["stateKey=" + streamRouting.stateKey] : []),
      ...(streamRouting.stream === "state"
        ? ["STATE STREAM: this event is ephemeral/latest-state oriented. Do not assume older undelivered state events will be replayed."]
        : ["CONVERSATION STREAM: preserve ordering and complete any required reply before the next conversation turn is released."]),
      ...routeLines,
      ...(additionalModelContext ? [additionalModelContext] : []),
      "createdAt=" + createdAt,
    ].join("\n"),
  };
}
