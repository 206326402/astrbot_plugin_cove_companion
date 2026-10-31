import assert from "node:assert/strict";
import test from "node:test";
import { ListenerWakeHub } from "../src/listenerWake.js";

test("listener wake sessions are short-lived and single-use", () => {
  const hub = new ListenerWakeHub();
  const session = hub.createSession();

  assert.ok(session.token.length > 20);
  assert.ok(Date.parse(session.expiresAt) > Date.now());

  const consumed = hub.consumeSession(session.token);
  assert.ok(consumed);
  assert.ok(consumed.expiresAtMs > Date.now());
  assert.equal(hub.consumeSession(session.token), null);
});

test("wake sequence advances even without connected listeners", () => {
  const hub = new ListenerWakeHub();
  assert.deepEqual(hub.status(), { clients: 0, sequence: 0, sessions: 0 });
  assert.equal(hub.wake("test"), 1);
  assert.equal(hub.wake("test"), 2);
  assert.equal(hub.status().sequence, 2);
});
