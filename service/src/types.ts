export type ReplyRoute = "netease.chatroom";
export type ReplyPolicy = "required" | "optional";
export type BridgeStream = "conversation" | "state";

export type BridgeEvent = {
  id: string;
  correlationId: string;
  kind: "message";
  source: string;
  stream: BridgeStream;
  stateKey?: string;
  replyRoute?: ReplyRoute;
  replyPolicy?: ReplyPolicy;
  createdAt: string;
  visibleText: string;
  modelContext: string;
};

export type EventStatus = "pending" | "reserved" | "delivered";

export type ReplyDeliveryState = {
  fingerprint: string;
  sentCount: number;
  completed: boolean;
  inFlight: boolean;
  completedAt?: string;
};

export type StoredBridgeEvent = {
  event: BridgeEvent;
  status: EventStatus;
  reply?: ReplyDeliveryState;
};
