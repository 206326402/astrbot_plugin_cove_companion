import { randomBytes } from "node:crypto";
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { OperationAction, OperationOrigin, OperationRequest } from "./protocol.js";
import { ASTRBOT_COVE_PROTOCOL } from "./protocol.js";
import type { AstrbotCoveService } from "./service.js";

const OPERATION_ACTIONS = new Set<OperationAction>([
  "CREATE", "JOIN", "ENQUEUE_NEXT", "PLAY_NOW", "PAUSE", "RESUME", "NEXT", "LEAVE", "SEND_MESSAGE",
  "CANCEL_QUEUED_RECOMMENDATION",
]);
const ORIGIN_KINDS = new Set<OperationOrigin["kind"]>([
  "qq_command", "room_command", "autodj", "room_reply", "dashboard",
]);
const MAX_BODY = 256 * 1024;

function tokenFromEnvironment(): string {
  const configured = process.env.ASTROBOT_BRIDGE_TOKEN?.trim();
  if (configured) return configured;
  const file = process.env.ASTROBOT_BRIDGE_TOKEN_FILE?.trim() || "data/astrbot-bridge-token";
  if (existsSync(file)) return readFileSync(file, "utf8").trim();
  mkdirSync(dirname(file), { recursive: true });
  const token = randomBytes(32).toString("base64url");
  writeFileSync(file, token + "\n", { encoding: "utf8", mode: 0o600 });
  chmodSync(file, 0o600);
  return token;
}

export class AstrbotHttpRouter {
  private readonly token = tokenFromEnvironment();

  constructor(private readonly service: AstrbotCoveService) {}

  private authorized(req: IncomingMessage): boolean {
    return req.headers.authorization === "Bearer " + this.token;
  }

  private send(res: ServerResponse, status: number, body: unknown): void {
    res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
    res.end(JSON.stringify(body));
  }

  private error(res: ServerResponse, status: number, code: string, message: string, retryable = false): void {
    this.send(res, status, { error: { code, message, retryable } });
  }

  private async body(req: IncomingMessage): Promise<Record<string, unknown>> {
    const chunks: Buffer[] = [];
    let size = 0;
    for await (const chunk of req) {
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      size += buffer.length;
      if (size > MAX_BODY) throw new Error("request_too_large");
      chunks.push(buffer);
    }
    const raw = Buffer.concat(chunks).toString("utf8");
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("json_object_required");
    return parsed as Record<string, unknown>;
  }

  private parseOperation(body: Record<string, unknown>): OperationRequest {
    const operationId = typeof body.operation_id === "string" ? body.operation_id.trim() : "";
    const leaseId = typeof body.lease_id === "string" ? body.lease_id.trim() : "";
    const fencingToken = Number(body.fencing_token);
    const sessionId = typeof body.session_id === "string" ? body.session_id.trim() : "";
    const action = body.action as OperationAction;
    const origin = body.origin as OperationOrigin | undefined;
    if (!/^[A-Za-z0-9_.:-]{8,160}$/.test(operationId)) throw new Error("operation_id_invalid");
    if (!leaseId) throw new Error("lease_id_required");
    if (!Number.isInteger(fencingToken) || fencingToken < 1) throw new Error("fencing_token_invalid");
    if (!sessionId || sessionId.length > 200) throw new Error("session_id_invalid");
    if (!action || !OPERATION_ACTIONS.has(action)) throw new Error("action_invalid");
    if (!origin || typeof origin !== "object" || !ORIGIN_KINDS.has(origin.kind)) throw new Error("origin_invalid");
    if (typeof origin.request_id !== "string" || !origin.request_id.trim()) throw new Error("origin_request_id_required");
    const preconditions = body.preconditions && typeof body.preconditions === "object" && !Array.isArray(body.preconditions)
      ? body.preconditions as Record<string, unknown>
      : {};
    const args = body.args && typeof body.args === "object" && !Array.isArray(body.args)
      ? body.args as Record<string, unknown>
      : {};
    return {
      operation_id: operationId,
      lease_id: leaseId,
      fencing_token: fencingToken,
      session_id: sessionId,
      deadline_at: typeof body.deadline_at === "number" ? body.deadline_at : undefined,
      control_generation: Number.isSafeInteger(body.control_generation) ? Number(body.control_generation) : undefined,
      binding_revision: Number.isSafeInteger(body.binding_revision) ? Number(body.binding_revision) : undefined,
      session_version: Number.isSafeInteger(body.session_version) ? Number(body.session_version) : undefined,
      origin,
      preconditions: {
        room_id: typeof preconditions.room_id === "string" ? preconditions.room_id : null,
        room_epoch: typeof preconditions.room_epoch === "string" ? preconditions.room_epoch : null,
        room_revision: typeof preconditions.room_revision === "number" ? preconditions.room_revision : null,
        queue_fingerprint: typeof preconditions.queue_fingerprint === "string" ? preconditions.queue_fingerprint : null,
      },
      action,
      args,
    };
  }

  async handle(req: IncomingMessage, res: ServerResponse, url: URL): Promise<boolean> {
    if (!url.pathname.startsWith("/astrbot/v1")) return false;
    if (req.method === "GET" && url.pathname === "/astrbot/v1/health/live") {
      this.send(res, 200, { ok: true, protocol: ASTRBOT_COVE_PROTOCOL, pid: process.pid });
      return true;
    }
    if (!this.authorized(req)) {
      this.error(res, 401, "unauthorized", "Bearer token is missing or invalid.", false);
      return true;
    }
    try {
      if (req.method === "GET" && url.pathname === "/astrbot/v1/health/ready") {
        const health = this.service.health();
        this.send(res, health.ready ? 200 : 503, { ok: Boolean(health.ready), health });
        return true;
      }
      if (req.method === "GET" && url.pathname === "/astrbot/v1/auth/status") {
        this.send(res,200,await this.service.authentication?.status(url.searchParams.get("fresh")==="1") || {state:"unavailable"}); return true;
      }
      if (req.method === "POST" && url.pathname === "/astrbot/v1/auth/qr/start") {
        if(!this.service.authentication) throw new Error("auth_unavailable");
        this.send(res,200,await this.service.authentication.start()); return true;
      }
      if (req.method === "POST" && url.pathname === "/astrbot/v1/auth/qr/check") {
        const b=await this.body(req);if(!this.service.authentication) throw new Error("auth_unavailable");
        this.send(res,200,await this.service.authentication.check(String(b.id||"")));return true;
      }
      if (req.method === "POST" && url.pathname === "/astrbot/v1/auth/activate") {
        const b=await this.body(req);this.send(res,200,await this.service.activateAccount(String(b.id||"")));return true;
      }
      if (req.method === "GET" && url.pathname === "/astrbot/v1/availability") {
        const id=String(url.searchParams.get('id')||'');if(!/^\d+$/.test(id))throw new Error('invalid_song_id');
        this.send(res,200,await this.service.availability(id));return true;
      }
      if (req.method === "GET" && url.pathname === "/astrbot/v1/playlist") {
        this.send(res,200,{songs:await this.service.playlist(String(url.searchParams.get("id")||""),30)});return true;
      }
      if (req.method === "GET" && url.pathname === "/astrbot/v1/bootstrap") {
        this.send(res, 200, await this.service.bootstrap());
        return true;
      }
      if (req.method === "GET" && url.pathname === "/astrbot/v1/snapshot") {
        const fresh = url.searchParams.get("fresh") === "1";
        this.send(res, 200, await this.service.snapshot(fresh));
        return true;
      }
      if (req.method === "GET" && url.pathname === "/astrbot/v1/events") {
        const after = Number(url.searchParams.get("after") || 0);
        const limit = Number(url.searchParams.get("limit") || 100);
        const wait = Number(url.searchParams.get("wait") || 0);
        if (!Number.isFinite(after) || after < 0) throw new Error("after_invalid");
        const result = await this.service.events(after, Math.max(1, Math.min(200, limit)), Math.max(0, Math.min(25_000, wait)));
        this.send(res, result.code === "cursor_expired" ? 410 : 200, result);
        return true;
      }
      if (req.method === 'GET' && url.pathname === '/astrbot/v1/catalog') {
        const kind=url.searchParams.get('kind') || 'songs';
        const query=(url.searchParams.get('query') || '').trim();
        const id=url.searchParams.get('id') || '';
        const limit=Number(url.searchParams.get('limit') || 20);
        if (!Number.isFinite(limit) || (!query && !id)) throw new Error('catalog_query_invalid');
        this.send(res,200,await this.service.catalog(kind,query,id,limit));return true;
      }
      if (req.method === "GET" && url.pathname === "/astrbot/v1/search") {
        const query = (url.searchParams.get("query") || "").trim();
        const limit = Number(url.searchParams.get("limit") || 12);
        if (!query) throw new Error("query_required");
        this.send(res, 200, { songs: await this.service.search(query, Math.max(1, Math.min(30, limit))) });
        return true;
      }
      if (req.method === "GET" && url.pathname === "/astrbot/v1/tracks") {
        const ids = (url.searchParams.get("ids") ?? "").split(",").map((value) => value.trim()).filter(Boolean);
        if (!ids.length) {
          this.error(res, 400, "ids_required", "At least one song id is required.", false);
          return true;
        }
        this.send(res, 200, { songs: await this.service.tracks(ids) });
        return true;
      }
      if (req.method === "GET" && url.pathname === "/astrbot/v1/candidates") {
        const limit = Number(url.searchParams.get("limit") || 40);
        this.send(res, 200, { songs: await this.service.candidates(Math.max(1, Math.min(60, limit))) });
        return true;
      }
      if (req.method === "GET" && url.pathname === "/astrbot/v1/lyrics") {
        const songId = (url.searchParams.get("song_id") || "").trim();
        if (!songId) throw new Error("song_id_required");
        this.send(res, 200, await this.service.lyrics(songId));
        return true;
      }
      if (req.method === "POST" && url.pathname === "/astrbot/v1/controller/claim") {
        const body = await this.body(req);
        const holder = typeof body.holder === "string" ? body.holder.trim() : "";
        if (!holder) throw new Error("holder_required");
        const lease = this.service.claimLease(holder, typeof body.account_id === "string" ? body.account_id : undefined);
        this.send(res, 200, { lease });
        return true;
      }
      if (req.method === "POST" && url.pathname === "/astrbot/v1/controller/renew") {
        const body = await this.body(req);
        const lease = this.service.renewLease(String(body.lease_id || ""), Number(body.fencing_token));
        this.send(res, 200, { lease });
        return true;
      }
      if (req.method === "POST" && url.pathname === "/astrbot/v1/controller/release") {
        const body = await this.body(req);
        const released = this.service.releaseLease(String(body.lease_id || ""), Number(body.fencing_token));
        this.send(res, 200, { released });
        return true;
      }
      if (req.method === "POST" && url.pathname === "/astrbot/v1/commands") {
        const request = this.parseOperation(await this.body(req));
        const result = await this.service.submitOperation(request);
        if (result.conflict) {
          this.error(res, 409, "idempotency_conflict", "The operation_id was reused with different parameters.", false);
          return true;
        }
        this.send(res, result.status, { operation: result.operation });
        return true;
      }
      const operationMatch = /^\/astrbot\/v1\/operations\/([A-Za-z0-9_.:-]+)$/.exec(url.pathname);
      if (req.method === "GET" && operationMatch) {
        const operation = this.service.getOperation(operationMatch[1]);
        if (!operation) {
          this.error(res, 404, "operation_not_found", "Operation does not exist.", false);
          return true;
        }
        this.send(res, 200, { operation });
        return true;
      }
      this.error(res, 404, "not_found", "AstrBot Cove endpoint does not exist.", false);
      return true;
    } catch (error) {
      const message = error instanceof Error ? error.message : "internal_error";
      if (message === "request_too_large") {
        this.error(res, 413, "request_too_large", "Request body exceeds 256 KiB.", false);
      } else if (/lease_/.test(message)) {
        this.error(res, 409, message, "Controller lease is not valid.", true);
      } else if (/account_not_ready/.test(message)) {
        this.error(res, 503, "account_not_ready", "NetEase account is not ready.", true);
      } else if (/required|invalid|json_object/.test(message)) {
        this.error(res, 400, message, message.replaceAll("_", " "), false);
      } else {
        this.error(res, 500, "internal_error", "Cove request failed; inspect protected service diagnostics.", true);
      }
      return true;
    }
  }
}
