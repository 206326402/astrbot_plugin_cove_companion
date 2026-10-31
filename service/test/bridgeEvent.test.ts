import assert from "node:assert/strict";
import test from "node:test";

test("NetEase ChatRoom event carries explicit source and required route semantics", async () => {
  const previous = process.env.NODE_ENV;
  process.env.NODE_ENV = "test";
  const { buildBridgeEvent } = await import("../src/server.js");
  if (previous === undefined) delete process.env.NODE_ENV;
  else process.env.NODE_ENV = previous;

  const event = buildBridgeEvent(
    "evt-chat",
    "netease.chatroom",
    "hello from netease",
    new Date(0).toISOString(),
  );

  assert.equal(event.stream, "conversation");
  assert.equal(event.replyRoute, "netease.chatroom");
  assert.equal(event.replyPolicy, "required");
  assert.match(event.visibleText, /^【网易云聊天室】\nhello from netease$/);
  assert.match(event.modelContext, /MESSAGE ORIGIN: NetEase ChatRoom/);
  assert.match(event.modelContext, /NOT in the ChatGPT conversation window/);
  assert.match(event.modelContext, /source=netease\.chatroom/);
  assert.match(event.modelContext, /replyRoute=netease\.chatroom/);
  assert.match(event.modelContext, /replyPolicy=required/);
  assert.match(event.modelContext, /must be routed back to NetEase ChatRoom through cove_bridge_reply/);
});

test("NetEase playback state is labeled as state and not direct user chat", async () => {
  const previous = process.env.NODE_ENV;
  process.env.NODE_ENV = "test";
  const { buildBridgeEvent } = await import("../src/server.js");
  if (previous === undefined) delete process.env.NODE_ENV;
  else process.env.NODE_ENV = previous;

  const event = buildBridgeEvent(
    "evt-state",
    "netease.playback",
    "一起听暂停了。",
    new Date(0).toISOString(),
  );

  assert.equal(event.stream, "state");
  assert.equal(event.replyRoute, "netease.chatroom");
  assert.equal(event.replyPolicy, "optional");
  assert.match(event.visibleText, /^【网易云一起听状态】\n一起听暂停了。$/);
  assert.match(event.modelContext, /MESSAGE ORIGIN: NetEase Listen Together state\/event stream/);
  assert.match(event.modelContext, /NOT a direct user chat message/);
});
