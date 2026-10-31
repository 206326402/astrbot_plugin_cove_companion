import assert from "node:assert/strict";
import test from "node:test";
import { buildPlaybackDiagnostic, decodeRealtimePlaybackEvent, NeteaseRealtimeTransport } from "../src/netease/realtimeTransport.js";

test("decodes event_type=20000 config string playback event", () => {
  const event = decodeRealtimePlaybackEvent({
    event_type: 20_000,
    config: JSON.stringify({
      serverSeq: 9,
      commandType: "PAUSE",
      formerSongId: 123,
      targetSongId: 456,
      progress: 2500,
      playStatus: "PAUSE",
    }),
  }, 123456);

  assert.deepEqual(event, {
    type: "playback",
    serverSeq: 9,
    commandType: "PAUSE",
    songId: "456",
    formerSongId: "123",
    progressMs: 2500,
    playStatus: "PAUSE",
    receivedAtMs: 123456,
  });
});

test("decodes nested content type=20000 playback event", () => {
  const event = decodeRealtimePlaybackEvent({
    msg_attach_: JSON.stringify({
      content: {
        type: 20_000,
        content: {
          serverSeq: 17,
          commandType: "GOTO",
          formerSongId: "11",
          targetSongId: "22",
          progress: 9001,
          playStatus: "PLAY",
        },
      },
    }),
  }, 500);

  assert.equal(event?.commandType, "GOTO");
  assert.equal(event?.serverSeq, 17);
  assert.equal(event?.songId, "22");
  assert.equal(event?.progressMs, 9001);
  assert.equal(event?.playStatus, "PLAY");
});

test("decodes PROGRESS event and clamps negative progress", () => {
  const event = decodeRealtimePlaybackEvent({
    type: 20_000,
    data: {
      commandType: "PROGRESS",
      targetSongId: 88,
      progress: -100,
      playStatus: "PLAY",
    },
  }, 42);

  assert.equal(event?.commandType, "PROGRESS");
  assert.equal(event?.progressMs, 0);
  assert.equal(event?.receivedAtMs, 42);
});

test("ignores malformed and unrelated events", () => {
  assert.equal(decodeRealtimePlaybackEvent("not-json"), null);
  assert.equal(decodeRealtimePlaybackEvent({ event_type: 19999, config: "{}" }), null);
  assert.equal(decodeRealtimePlaybackEvent({ event_type: 20_000, config: "{}" }), null);
});

test("realtime status never contains credentials", () => {
  const transport = new NeteaseRealtimeTransport(false);
  const status = transport.getStatus();
  assert.equal(status.enabled, false);
  assert.equal(status.connected, false);
  assert.equal("token" in status, false);
  assert.equal("cookie" in status, false);
  assert.equal("addresses" in status, false);
});

test("playback diagnostic only exposes whitelisted protocol fields", () => {
  const diagnostic = buildPlaybackDiagnostic({
    msg_type_: 100,
    sub_type_: 7,
    from_id_: "sensitive-account-1234",
    client_msg_id_: "secret-message-abcdef",
    msg_attach_: JSON.stringify({
      event_type: 20_000,
      config: JSON.stringify({
        commandType: "PAUSE",
        targetSongId: "42",
        progress: 1234,
        playStatus: "PAUSE",
        serverSeq: 9,
        token: "must-not-leak",
      }),
      cookie: "must-not-leak",
    }),
    msg_setting_: {
      ext_: JSON.stringify({
        appName: "music",
        clientExt: {
          bizType: "listenTogether",
          ltType: "FRIEND",
          roomId: "room",
          auth: "must-not-leak",
        },
      }),
    },
  });

  assert.ok(diagnostic);
  const serialized = JSON.stringify(diagnostic);
  assert.match(serialized, /PAUSE/);
  assert.match(serialized, /listenTogether/);
  assert.doesNotMatch(serialized, /must-not-leak/);
  assert.doesNotMatch(serialized, /sensitive-account/);
  assert.doesNotMatch(serialized, /secret-message/);
});

test("decodes playback clientSeq and senderId for outbound confirmation", () => {
  const event = decodeRealtimePlaybackEvent({
    from_id_: "cove-account",
    msg_attach_: JSON.stringify({
      content: {
        type: 20_000,
        bizType: 3,
        content: {
          serverSeq: 123,
          roomId: "room",
          commandType: "PAUSE",
          formerSongId: "42",
          targetSongId: "42",
          progress: 7654,
          playStatus: "PAUSE",
          clientSeq: 8,
        },
      },
    }),
  }, 999);

  assert.equal(event?.clientSeq, 8);
  assert.equal(event?.senderId, "cove-account");
  assert.equal(event?.serverSeq, 123);
  assert.equal(event?.commandType, "PAUSE");
});
