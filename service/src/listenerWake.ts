import { randomBytes } from "node:crypto";
import type { ServerResponse } from "node:http";

const SESSION_TTL_MS = 10 * 60_000;
const HEARTBEAT_MS = 20_000;

type SessionRecord = {
  expiresAtMs: number;
};

type ClientRecord = {
  response: ServerResponse;
  heartbeat: NodeJS.Timeout;
  expires: NodeJS.Timeout;
};

export class ListenerWakeHub {
  private readonly sessions = new Map<string, SessionRecord>();
  private readonly clients = new Set<ClientRecord>();
  private sequence = 0;

  createSession(): { token: string; expiresAt: string } {
    this.pruneSessions();
    const token = randomBytes(32).toString("base64url");
    const expiresAtMs = Date.now() + SESSION_TTL_MS;
    this.sessions.set(token, { expiresAtMs });
    return { token, expiresAt: new Date(expiresAtMs).toISOString() };
  }

  consumeSession(token: string): { expiresAtMs: number } | null {
    this.pruneSessions();
    const record = this.sessions.get(token);
    if (!record) return null;
    this.sessions.delete(token);
    if (record.expiresAtMs <= Date.now()) return null;
    return { expiresAtMs: record.expiresAtMs };
  }

  subscribe(response: ServerResponse, expiresAtMs: number): () => void {
    response.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      "Connection": "keep-alive",
      "Access-Control-Allow-Origin": "*",
      "X-Accel-Buffering": "no",
    });
    response.flushHeaders?.();

    const heartbeat = setInterval(() => {
      if (!response.destroyed) response.write(`: heartbeat ${Date.now()}\n\n`);
    }, HEARTBEAT_MS);
    heartbeat.unref?.();

    const expires = setTimeout(() => {
      if (!response.destroyed) {
        response.write("event: session-expired\ndata: {}\n\n");
        response.end();
      }
    }, Math.max(1000, expiresAtMs - Date.now()));
    expires.unref?.();

    const client: ClientRecord = { response, heartbeat, expires };
    this.clients.add(client);
    this.writeWake(response, "connected");

    const cleanup = () => {
      clearInterval(heartbeat);
      clearTimeout(expires);
      this.clients.delete(client);
    };
    response.once("close", cleanup);
    return cleanup;
  }

  wake(reason = "event"): number {
    this.sequence += 1;
    for (const client of [...this.clients]) {
      if (client.response.destroyed) {
        clearInterval(client.heartbeat);
        clearTimeout(client.expires);
        this.clients.delete(client);
        continue;
      }
      this.writeWake(client.response, reason);
    }
    return this.sequence;
  }

  status(): { clients: number; sequence: number; sessions: number } {
    this.pruneSessions();
    return {
      clients: this.clients.size,
      sequence: this.sequence,
      sessions: this.sessions.size,
    };
  }

  private writeWake(response: ServerResponse, reason: string): void {
    response.write(
      `event: wake\ndata: ${JSON.stringify({ seq: this.sequence, reason })}\n\n`,
    );
  }

  private pruneSessions(): void {
    const now = Date.now();
    for (const [token, record] of this.sessions) {
      if (record.expiresAtMs <= now) this.sessions.delete(token);
    }
  }
}

export const listenerWakeHub = new ListenerWakeHub();
