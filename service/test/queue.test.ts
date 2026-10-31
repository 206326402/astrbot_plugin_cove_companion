import assert from "node:assert/strict";
import test from "node:test";
import { buildListenerHtml } from "../src/listener-html.js";
import { InMemoryEventQueue } from "../src/queue.js";
import type { BridgeEvent } from "../src/types.js";

function event(id: string): BridgeEvent {
  return {
    id,
    correlationId: id,
    kind: "message",
    source: "test",
    stream: "conversation",
    createdAt: new Date(0).toISOString(),
    visibleText: "hello",
    modelContext: `eventId=${id}`,
  };
}

test("queue reserves once and delivers idempotently", () => {
  const queue = new InMemoryEventQueue();
  assert.equal(queue.enqueue(event("evt-1")), true);
  assert.equal(queue.enqueue(event("evt-1")), false);
  assert.equal(queue.reserveNext()?.id, "evt-1");
  assert.equal(queue.reserveNext(), null);
  queue.markDelivered("evt-1");
  queue.markDelivered("evt-1");
  assert.deepEqual(queue.status(), {
    pending: 0,
    reserved: 0,
    delivered: 1,
    total: 1,
    conversation: { pending: 0, reserved: 0, delivered: 1, total: 1 },
    state: { pending: 0, reserved: 0, delivered: 0, total: 0 },
  });
});

test("failed delivery can be released and retried", () => {
  const queue = new InMemoryEventQueue();
  queue.enqueue(event("evt-2"));
  assert.equal(queue.reserveNext()?.id, "evt-2");
  queue.release("evt-2");
  assert.equal(queue.reserveNext()?.id, "evt-2");
});

test("dismissed required event becomes terminal and releases backpressure", () => {
  const queue = new InMemoryEventQueue();
  queue.enqueue({
    ...event("evt-dismiss"),
    source: "netease.chatroom",
    replyRoute: "netease.chatroom",
    replyPolicy: "required",
  });
  queue.enqueue({
    ...event("evt-after-dismiss"),
    source: "netease.chatroom",
    replyRoute: "netease.chatroom",
    replyPolicy: "required",
  });

  assert.equal(queue.reserveNext()?.id, "evt-dismiss");
  queue.dismiss("evt-dismiss");
  assert.equal(queue.getOutstandingRequiredReplyEvent(), null);
  assert.deepEqual(
    queue.claimReply("evt-dismiss", "late-reply"),
    { state: "already_completed", sentCount: 0 },
  );
  assert.equal(queue.reserveNext()?.id, "evt-after-dismiss");
});

test("widget is idle by default and initializes MCP Apps bridge", () => {
  const html = buildListenerHtml();
  assert.match(html, /尚未监听/);
  assert.match(html, /ui\/initialize/);
  assert.match(html, /ui\/notifications\/initialized/);
  assert.match(html, /ui\/update-model-context/);
  assert.match(html, /ui\/message/);
  assert.match(html, /cove_bridge_listener_session/);
  assert.match(html, /new EventSource\(streamUrl\.toString\(\)\)/);
  assert.match(html, /FALLBACK_POLL_MS = 60000/);
  assert.match(html, /cove-bridge-dispatched-v1/);
  assert.match(html, /cove-bridge-pending-dismissals-v1/);
  assert.match(html, /requestHumanDecision\('ui\/message'/);
  assert.match(html, /cove_bridge_dismissed/);
  assert.doesNotMatch(html, /await request\('ui\/message'/);
  assert.match(html, /不会重复显示/);
  const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
  assert.ok(script);
  assert.doesNotThrow(() => new Function(script));
  assert.ok(html.indexOf("ui/update-model-context") < html.indexOf("ui/message"));
});

test("reply delivery resumes without resending completed bubbles", () => {
  const queue = new InMemoryEventQueue();
  queue.enqueue({
    ...event("evt-reply"),
    source: "netease.chatroom",
    replyRoute: "netease.chatroom",
    replyPolicy: "required",
  });

  assert.deepEqual(queue.claimReply("evt-reply", "fp-1"), { state: "send", sentCount: 0 });
  assert.equal(queue.markReplyMessageSent("evt-reply", "fp-1"), 1);
  queue.releaseReply("evt-reply", "fp-1");
  assert.deepEqual(queue.claimReply("evt-reply", "fp-1"), { state: "send", sentCount: 1 });
  assert.equal(queue.markReplyMessageSent("evt-reply", "fp-1"), 2);
  queue.markReplyCompleted("evt-reply", "fp-1");
  assert.deepEqual(
    queue.claimReply("evt-reply", "fp-1"),
    { state: "already_completed", sentCount: 2 },
  );
});

test("reply delivery suppresses concurrent duplicate claims and rejects different replies", () => {
  const queue = new InMemoryEventQueue();
  queue.enqueue({
    ...event("evt-reply-2"),
    source: "netease.chatroom",
    replyRoute: "netease.chatroom",
    replyPolicy: "required",
  });

  assert.deepEqual(queue.claimReply("evt-reply-2", "fp-a"), { state: "send", sentCount: 0 });
  assert.deepEqual(
    queue.claimReply("evt-reply-2", "fp-a"),
    { state: "in_progress", sentCount: 0 },
  );
  assert.throws(
    () => queue.claimReply("evt-reply-2", "fp-b"),
    /different reply/,
  );
});

test("required routed reply applies backpressure until the reply completes", () => {
  const queue = new InMemoryEventQueue();
  queue.enqueue({
    ...event("evt-a"),
    source: "netease.chatroom",
    replyRoute: "netease.chatroom",
    replyPolicy: "required",
  });
  queue.enqueue({
    ...event("evt-b"),
    source: "netease.chatroom",
    replyRoute: "netease.chatroom",
    replyPolicy: "required",
  });

  assert.equal(queue.reserveNext()?.id, "evt-a");
  assert.equal(queue.reserveNext(), null);
  queue.markDelivered("evt-a");
  assert.equal(queue.getOutstandingRequiredReplyEvent()?.id, "evt-a");
  assert.equal(queue.reserveNext(), null);
  const fingerprint = "fp-a";
  assert.deepEqual(queue.claimReply("evt-a", fingerprint), { state: "send", sentCount: 0 });
  queue.markReplyMessageSent("evt-a", fingerprint);
  queue.markReplyCompleted("evt-a", fingerprint);

  assert.equal(queue.getOutstandingRequiredReplyEvent(), null);
  assert.equal(queue.reserveNext()?.id, "evt-b");
});

test("completed routed replies suppress later duplicate content", () => {
  const queue = new InMemoryEventQueue();
  queue.enqueue({
    ...event("evt-complete"),
    source: "netease.chatroom",
    replyRoute: "netease.chatroom",
    replyPolicy: "required",
  });

  assert.deepEqual(queue.claimReply("evt-complete", "fp-original"), { state: "send", sentCount: 0 });
  queue.markReplyMessageSent("evt-complete", "fp-original");
  queue.markReplyCompleted("evt-complete", "fp-original");
  assert.deepEqual(
    queue.claimReply("evt-complete", "fp-different"),
    { state: "already_completed", sentCount: 1 },
  );
});

test("conversation stream keeps every message in order", () => {
  const queue = new InMemoryEventQueue();
  queue.enqueue(event("conv-1"));
  queue.enqueue(event("conv-2"));

  assert.equal(queue.reserveNext()?.id, "conv-1");
  queue.markDelivered("conv-1");
  assert.equal(queue.reserveNext()?.id, "conv-2");
});

test("state stream coalesces stale pending presence events", () => {
  const queue = new InMemoryEventQueue();
  queue.enqueue({
    ...event("state-1"),
    source: "netease.music_changed",
    stream: "state",
    stateKey: "netease.together.presence",
    replyPolicy: "optional",
  });
  queue.enqueue({
    ...event("state-2"),
    source: "netease.playback",
    stream: "state",
    stateKey: "netease.together.presence",
    replyPolicy: "optional",
  });

  const status = queue.status();
  assert.equal(status.state.pending, 1);
  assert.equal(status.state.total, 1);
  assert.equal(queue.reserveNext()?.id, "state-2");
});

test("conversation stream has priority over pending state", () => {
  const queue = new InMemoryEventQueue();
  queue.enqueue({
    ...event("state-first"),
    source: "netease.music_changed",
    stream: "state",
    stateKey: "netease.together.presence",
    replyPolicy: "optional",
  });
  queue.enqueue(event("conv-after"));

  assert.equal(queue.reserveNext()?.id, "conv-after");
});

test("releasing an old reserved state event does not replay it over a newer state", () => {
  const queue = new InMemoryEventQueue();
  queue.enqueue({
    ...event("state-old"),
    source: "netease.music_changed",
    stream: "state",
    stateKey: "netease.together.presence",
    replyPolicy: "optional",
  });
  assert.equal(queue.reserveNext()?.id, "state-old");

  queue.enqueue({
    ...event("state-new"),
    source: "netease.playback",
    stream: "state",
    stateKey: "netease.together.presence",
    replyPolicy: "optional",
  });
  queue.release("state-old");

  assert.equal(queue.getEvent("state-old"), null);
  assert.equal(queue.reserveNext()?.id, "state-new");
});


test("profile filters isolate pending events and required-reply backpressure", () => {
  const queue = new InMemoryEventQueue();
  const musicFilter = (item: BridgeEvent) => item.source.startsWith("netease.");
  const spicyFilter = (item: BridgeEvent) => item.source === "spicy-monopoly-web";
  const coreFilter = (item: BridgeEvent) => !musicFilter(item) && !spicyFilter(item);

  queue.enqueue({
    ...event("music-required"),
    source: "netease.chatroom",
    replyRoute: "netease.chatroom",
    replyPolicy: "required",
  });
  queue.enqueue({ ...event("spicy-turn"), source: "spicy-monopoly-web" });
  queue.enqueue({ ...event("core-event"), source: "telegram.test" });

  assert.equal(queue.reserveNext(musicFilter)?.id, "music-required");
  assert.equal(queue.getOutstandingRequiredReplyEvent(musicFilter)?.id, "music-required");

  // A required music reply must not block the independent game view.
  assert.equal(queue.reserveNext(spicyFilter)?.id, "spicy-turn");
  assert.equal(queue.reserveNext(coreFilter)?.id, "core-event");

  assert.equal(queue.reserveNext(musicFilter), null);
  assert.equal(queue.reserveNext(spicyFilter), null);
  assert.equal(queue.reserveNext(coreFilter), null);
});
