import assert from "node:assert/strict";
import test from "node:test";
import { buildRealtimeChatRoomEnterInfo, buildRealtimeChatTextMessage, decodeRealtimeChatRoomMessage } from "../src/netease/realtimeTransport.js";

test("decodes ordinary ChatRoom text messages", () => {
  const message = decodeRealtimeChatRoomMessage({
    msg_type_: 0,
    from_id_: "user-123456",
    from_nick_: "listener",
    msg_attach_: "hello from room",
    client_msg_id_: "message-1",
    timetag_: 123456789,
  }, 999);

  assert.deepEqual(message, {
    type: "chatroom_message",
    category: "text",
    msgType: 0,
    senderId: "user-123456",
    senderNick: "listener",
    text: "hello from room",
    messageId: "message-1",
    timetagMs: 123456789,
    receivedAtMs: 999,
  });
});

test("decodes custom ChatRoom payload text without treating it as robot", () => {
  const message = decodeRealtimeChatRoomMessage({
    msg_type_: 100,
    msg_attach_: JSON.stringify({ content: "custom hello" }),
  }, 1000);

  assert.equal(message?.category, "custom");
  assert.equal(message?.text, "custom hello");
});

test("returns null for an empty ChatRoom message", () => {
  assert.equal(decodeRealtimeChatRoomMessage(null), null);
});


test("builds ordinary ChatRoom text messages for sending", () => {
  assert.deepEqual(buildRealtimeChatTextMessage("  hello Cove  ", "logical-room-1", "send-1"), {
    msg_type_: 0,
    msg_attach_: "hello Cove",
    msg_body_: "",
    client_msg_id_: "send-1",
    sub_type_: 0,
    msg_setting_: {
      ext_: JSON.stringify({
        appName: "music",
        clientExt: {
          bizType: "listenTogether",
          ltType: "FRIEND",
          roomId: "logical-room-1",
          clientMsgId: "send-1",
        },
      }),
      anti_spam_enable_: false,
      history_save_: true,
      anti_spam_using_yidun_: 1,
      route_enabled_: true,
    },
  });
});

test("rejects empty and oversized outgoing ChatRoom text", () => {
  assert.throws(() => buildRealtimeChatTextMessage("   ", "room", "empty"), /cannot be empty/);
  assert.throws(() => buildRealtimeChatTextMessage("x".repeat(501), "room", "long"), /exceeds 500/);
});

test("builds ChatRoom enter profile from NetEase account display data", () => {
  assert.deepEqual(
    buildRealtimeChatRoomEnterInfo({
      nick: "  Cove  ",
      avatar: "  https://example.invalid/avatar.jpg  ",
    }),
    {
      values_: {
        nick: "Cove",
        avatar: "https://example.invalid/avatar.jpg",
      },
    },
  );
  assert.deepEqual(buildRealtimeChatRoomEnterInfo(), { values_: {} });
});

test("includes minimal Listen Together sender identity in serverExt", () => {
  const message = buildRealtimeChatTextMessage(
    "hello",
    "room-1",
    "12345678-1234-1234-1234-123456789abc",
    { userId: "12345678901", nick: "Cove", avatar: "https://example.invalid/a.jpg", gender: 1 },
  );
  const setting = message.msg_setting_ as Record<string, unknown>;
  const ext = JSON.parse(String(setting.ext_)) as Record<string, unknown>;
  assert.deepEqual(ext.serverExt && typeof ext.serverExt === "object" ? {
    userId: (ext.serverExt as Record<string, unknown>).userId,
    nickname: (ext.serverExt as Record<string, unknown>).nickname,
    avatarUrl: (ext.serverExt as Record<string, unknown>).avatarUrl,
    gender: (ext.serverExt as Record<string, unknown>).gender,
    msgIdType: typeof (ext.serverExt as Record<string, unknown>).msgId,
  } : null, {
    userId: 12345678901,
    nickname: "Cove",
    avatarUrl: "https://example.invalid/a.jpg",
    gender: 1,
    msgIdType: "number",
  });
});
