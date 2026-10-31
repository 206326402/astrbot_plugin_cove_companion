import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { AstrbotCoveStore } from "../src/astrbot/store.js";
import { AstrbotCoveService } from "../src/astrbot/service.js";

function withStore(fn: (store: AstrbotCoveStore) => void): void {
  const dir = mkdtempSync(join(tmpdir(), "cove-astrbot-"));
  const store = new AstrbotCoveStore(join(dir, "bridge.sqlite3"));
  try {
    fn(store);
  } finally {
    store.close();
    rmSync(dir, { recursive: true, force: true });
  }
}

test("event ingestion deduplicates provider keys while preserving sequence", () => withStore((store) => {
  const first = store.recordEvent({
    event_type: "chat_message",
    source: "netease.chatroom",
    source_event_key: "message-1",
    payload: { text: "hello" },
  });
  const duplicate = store.recordEvent({
    event_type: "chat_message",
    source: "netease.chatroom",
    source_event_key: "message-1",
    payload: { text: "hello" },
  });
  assert.equal(first.created, true);
  assert.equal(duplicate.created, false);
  assert.equal(duplicate.event.seq, first.event.seq);
  assert.equal(store.listEvents(0).events.length, 1);
}));

test("lease fencing advances and rejects stale holders", () => withStore((store) => {
  const first = store.claimLease("account-1", "plugin-a");
  assert.equal(first.fencing_token, 1);
  assert.throws(() => store.claimLease("account-1", "plugin-b"), /lease_held/);
  store.releaseLease("account-1", first.lease_id, first.fencing_token);
  const second = store.claimLease("account-1", "plugin-b");
  assert.equal(second.fencing_token, 2);
  assert.throws(() => store.renewLease("account-1", first.lease_id, first.fencing_token), /lease_fenced/);
}));

test("operation idempotency keeps same request and rejects changed parameters", () => withStore((store) => {
  const request = {
    operation_id: "operation-1",
    lease_id: "lease-1",
    fencing_token: 1,
    session_id: "session-1",
    origin: { kind: "autodj" as const, request_id: "request-1" },
    preconditions: { room_id: "room-1" },
    action: "ENQUEUE_NEXT" as const,
    args: { song_id: "123456" },
  };
  const first = store.createOperation(request);
  assert.equal(first.created, true);
  const duplicate = store.createOperation(request);
  assert.equal(duplicate.created, false);
  assert.equal(duplicate.conflict, false);
  const conflict = store.createOperation({ ...request, args: { song_id: "654321" } });
  assert.equal(conflict.conflict, true);
}));

test("cursor expiry is explicit and snapshot payload round-trips", () => withStore((store) => {
  for (let index = 0; index < 3; index += 1) {
    store.recordEvent({ event_type: "playback_changed", source: "netease.playback", source_event_key: String(index) });
  }
  store.db.exec("DELETE FROM bridge_events WHERE seq IN (1,2)");
  const events = store.listEvents(1);
  assert.equal(events.cursorExpired, true);
  const snapshot = {
    protocol: "astrbot-cove/1" as const,
    bridge_instance_id: store.bridgeInstanceId(),
    event_cursor: store.latestEventSeq(),
    captured_at: Date.now(),
    room_id: null,
    room_epoch: null,
    room_revision: 0,
    connected: false,
    nim_connected: false,
    bot_account: { uid: null },
    owner_binding: { uid: null, present: null },
    members: [],
    current_song: null,
    playback: { status: "UNKNOWN" as const, progress_ms: 0, observed_at: Date.now(), server_seq: null },
    queue: { song_ids: [], play_mode: null, versions: [], fingerprint: null },
    evidence: {
      room: { source: "test", observed_at: Date.now(), validated_at: null, stale: true },
      members: { source: "test", observed_at: Date.now(), validated_at: null, stale: true },
      queue: { source: "test", observed_at: Date.now(), validated_at: null, stale: true },
      playback: { source: "test", observed_at: Date.now(), validated_at: null, stale: true },
    },
    worker: { phase: "disabled", account_id: null, last_poll_at: null, last_heartbeat_at: null, last_error: null },
  };
  store.saveSnapshot("unbound", snapshot);
  assert.equal(store.getSnapshot("unbound")?.protocol, "astrbot-cove/1");
}));
test("playback events from plugin operations retain operation attribution", () => withStore((store) => {
  const service = new AstrbotCoveService(store);
  (service as unknown as { recentOperations: Array<Record<string, unknown>> }).recentOperations.push({
    id: "operation-200",
    action: "PLAY_NOW",
    startedAt: Date.now(),
    finishedAt: null,
    targetSongId: "200",
  });
  service.ingestWorkerEvent({
    type: "playback_changed",
    occurred_at: Date.now(),
    actor_role: "system",
    payload: { song_id: "200", play_status: "PLAY", command_type: "GOTO" },
  });
  const event = store.listEvents(0).events[0];
  assert.equal(event.operation_id, "operation-200");
  assert.equal(event.payload.attribution, "plugin");
}));

test("completed operations leave a short unknown grace then external attribution", () => withStore((store) => {
  const service = new AstrbotCoveService(store);
  const operations = (service as unknown as { recentOperations: Array<Record<string, unknown>> }).recentOperations;
  operations.push({
    id: "operation-queue",
    action: "ENQUEUE_NEXT",
    startedAt: Date.now() - 20_000,
    finishedAt: Date.now() - 4_000,
    targetSongId: "200",
  });
  service.ingestWorkerEvent({
    type: "queue_changed",
    occurred_at: Date.now(),
    actor_role: "system",
    payload: { display_list: ["100", "999"], fingerprint: "queue-external" },
  });
  assert.equal(store.listEvents(0).events[0].payload.attribution, "unknown");
  operations[0].finishedAt = Date.now() - 10_000;
  service.ingestWorkerEvent({
    type: "queue_changed",
    occurred_at: Date.now(),
    actor_role: "system",
    payload: { display_list: ["100", "998"], fingerprint: "queue-external-2" },
  });
  const external = store.listEvents(0).events.find((event) => event.payload.fingerprint === "queue-external-2");
  assert.equal(external?.payload.attribution, "external");
}));

test("track detail lookup delegates to the worker song cache", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cove-astrbot-"));
  const store = new AstrbotCoveStore(join(dir, "bridge.sqlite3"));
  try {
    const service = new AstrbotCoveService(store);
    service.attachWorker({
      getSongDetailsMany: async (ids: string[]) => ids.map((id) => ({ id, name: "Track " + id, artist: "Artist", durationMs: 1000 })),
    } as unknown as import("../src/netease/togetherWorker.js").TogetherWorker);
    const songs = await service.tracks(["1", "2"]);
    assert.deepEqual(songs.map((song) => song.name), ["Track 1", "Track 2"]);
  } finally {
    store.close();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("same-song playback notifications are ignored", () => withStore((store) => {
  const service = new AstrbotCoveService(store);
  service.ingestWorkerEvent({
    type: "playback_changed",
    occurred_at: Date.now(),
    actor_role: "system",
    payload: { song_id: "song-1", former_song_id: "song-1", play_status: "PAUSE" },
  });
  assert.equal(store.listEvents(0).events.length, 0);
}));

test("unmatched playback events are marked external", () => withStore((store) => {
  const service = new AstrbotCoveService(store);
  service.ingestWorkerEvent({
    type: "playback_changed",
    occurred_at: Date.now(),
    actor_role: "system",
    payload: { song_id: "999", play_status: "PLAY", command_type: "GOTO" },
  });
  const event = store.listEvents(0).events[0];
  assert.equal(event.payload.attribution, "external");
  assert.equal(event.operation_id, null);
}));
