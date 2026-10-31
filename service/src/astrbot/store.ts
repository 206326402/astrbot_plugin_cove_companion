import { createHash, randomUUID } from "node:crypto";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync, type StatementSync } from "node:sqlite";
import {
  ASTRBOT_COVE_PROTOCOL,
  nowMs,
  operationRequestHash,
  type BridgeEventRecord,
  type OperationAction,
  type OperationOrigin,
  type OperationPreconditions,
  type OperationRequest,
  type OperationState,
  type OperationView,
  type ProtocolSnapshot,
} from "./protocol.js";

type Row = Record<string, unknown>;

const SCHEMA_VERSION = 1;

function json(value: unknown): string {
  return JSON.stringify(value ?? null);
}

function parseJson(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "string" || !value) return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function text(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : Number(value) || 0;
}

export type LeaseView = {
  account_id: string;
  lease_id: string;
  fencing_token: number;
  holder: string;
  acquired_at: number;
  expires_at: number;
};

export type OperationCreateResult = {
  created: boolean;
  conflict: boolean;
  operation: OperationView;
};

export class AstrbotCoveStore {
  readonly db: DatabaseSync;
  private readonly statements = new Map<string, StatementSync>();
  private readonly eventWaiters = new Set<() => void>();

  constructor(path: string) {
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true });
    this.db = new DatabaseSync(path);
    this.db.exec("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000; PRAGMA synchronous=NORMAL;");
    this.migrate();
  }

  close(): void {
    for (const waiter of this.eventWaiters) waiter();
    this.eventWaiters.clear();
    this.db.close();
  }

  private migrate(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS bridge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS bridge_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
        room_id TEXT, room_epoch TEXT, occurred_at INTEGER NOT NULL, recorded_at INTEGER NOT NULL,
        source TEXT NOT NULL, source_event_key TEXT, actor_role TEXT NOT NULL, actor_uid TEXT,
        operation_id TEXT, payload_json TEXT NOT NULL
      );
      CREATE UNIQUE INDEX IF NOT EXISTS bridge_events_source_key
        ON bridge_events(source, source_event_key) WHERE source_event_key IS NOT NULL;
      CREATE INDEX IF NOT EXISTS bridge_events_room_time ON bridge_events(room_id, occurred_at DESC);
      CREATE TABLE IF NOT EXISTS controller_leases (
        account_id TEXT PRIMARY KEY, lease_id TEXT NOT NULL, fencing_token INTEGER NOT NULL,
        holder TEXT NOT NULL, acquired_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS operations (
        operation_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, session_id TEXT NOT NULL,
        origin_json TEXT NOT NULL, action TEXT NOT NULL, args_json TEXT NOT NULL,
        preconditions_json TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT,
        error_code TEXT, error_message TEXT, retryable INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS operations_state_time ON operations(state, updated_at DESC);
      CREATE TABLE IF NOT EXISTS operation_steps (
        operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
        step_name TEXT NOT NULL, state TEXT NOT NULL, evidence_json TEXT, updated_at INTEGER NOT NULL,
        PRIMARY KEY(operation_id, step_name)
      );
      CREATE TABLE IF NOT EXISTS message_outbox (
        message_id TEXT PRIMARY KEY, operation_id TEXT, room_id TEXT, text TEXT NOT NULL,
        state TEXT NOT NULL, provider_message_id TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS message_outbox_state_time ON message_outbox(state, updated_at);
      CREATE TABLE IF NOT EXISTS snapshots (
        account_id TEXT PRIMARY KEY, bridge_instance_id TEXT NOT NULL, event_cursor INTEGER NOT NULL,
        room_id TEXT, room_epoch TEXT, room_revision INTEGER NOT NULL,
        payload_json TEXT NOT NULL, observed_at INTEGER NOT NULL
      );
    `);
    this.db.prepare("INSERT INTO schema_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
      .run("schema_version", String(SCHEMA_VERSION));
    this.db.prepare("INSERT INTO schema_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
      .run("protocol", ASTRBOT_COVE_PROTOCOL);
    this.setMeta("bridge_instance_id", randomUUID());
  }

  private stmt(sql: string): StatementSync {
    let statement = this.statements.get(sql);
    if (!statement) {
      statement = this.db.prepare(sql);
      this.statements.set(sql, statement);
    }
    return statement;
  }

  setMeta(key: string, value: string): void {
    this.stmt("INSERT INTO bridge_meta(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at")
      .run(key, value, nowMs());
  }

  getMeta(key: string): string | null {
    const row = this.stmt("SELECT value FROM bridge_meta WHERE key=?").get(key) as Row | undefined;
    return row ? String(row.value) : null;
  }

  bridgeInstanceId(): string {
    return this.getMeta("bridge_instance_id") ?? "unknown";
  }

  recordEvent(input: {
    event_id?: string;
    event_type: string;
    room_id?: string | null;
    room_epoch?: string | null;
    occurred_at?: number;
    source: string;
    source_event_key?: string | null;
    actor_role?: BridgeEventRecord["actor_role"];
    actor_uid?: string | null;
    operation_id?: string | null;
    payload?: Record<string, unknown>;
  }): { created: boolean; event: BridgeEventRecord } {
    const eventId = input.event_id ?? randomUUID();
    const now = nowMs();
    const occurred = input.occurred_at ?? now;
    try {
      const result = this.stmt(`INSERT INTO bridge_events(
        event_id,event_type,room_id,room_epoch,occurred_at,recorded_at,source,source_event_key,actor_role,actor_uid,operation_id,payload_json
      ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)`).run(
        eventId, input.event_type, input.room_id ?? null, input.room_epoch ?? null, occurred, now,
        input.source, input.source_event_key ?? null, input.actor_role ?? "unknown",
        input.actor_uid ?? null, input.operation_id ?? null, json(input.payload ?? {}),
      );
      const seq = Number(result.lastInsertRowid);
      const event = this.getEventBySeq(seq);
      if (!event) throw new Error("event_insert_failed");
      this.wakeWaiters();
      return { created: true, event };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (!/UNIQUE constraint failed/i.test(message)) throw error;
      const existing = input.source_event_key
        ? this.stmt("SELECT * FROM bridge_events WHERE source=? AND source_event_key=?").get(input.source, input.source_event_key) as Row | undefined
        : this.stmt("SELECT * FROM bridge_events WHERE event_id=?").get(eventId) as Row | undefined;
      if (!existing) throw error;
      return { created: false, event: this.mapEvent(existing) };
    }
  }

  private mapEvent(row: Row): BridgeEventRecord {
    return {
      seq: number(row.seq), event_id: String(row.event_id), event_type: String(row.event_type),
      room_id: text(row.room_id), room_epoch: text(row.room_epoch),
      occurred_at: number(row.occurred_at), recorded_at: number(row.recorded_at),
      source: String(row.source), source_event_key: text(row.source_event_key),
      actor_role: String(row.actor_role) as BridgeEventRecord["actor_role"],
      actor_uid: text(row.actor_uid), operation_id: text(row.operation_id),
      payload: parseJson(row.payload_json) ?? {},
    };
  }

  private getEventBySeq(seq: number): BridgeEventRecord | null {
    const row = this.stmt("SELECT * FROM bridge_events WHERE seq=?").get(seq) as Row | undefined;
    return row ? this.mapEvent(row) : null;
  }

  listEvents(after: number, limit = 100): { events: BridgeEventRecord[]; next: number; earliest: number; cursorExpired: boolean } {
    const earliestRow = this.stmt("SELECT MIN(seq) AS seq FROM bridge_events").get() as Row | undefined;
    const earliest = earliestRow?.seq == null ? 0 : number(earliestRow.seq);
    const cursorExpired = after > 0 && earliest > 0 && after < earliest - 1;
    const rows = this.stmt("SELECT * FROM bridge_events WHERE seq>? ORDER BY seq ASC LIMIT ?")
      .all(after, Math.max(1, Math.min(200, limit))) as Row[];
    const events = rows.map((row) => this.mapEvent(row));
    return {
      events, next: events.length ? events[events.length - 1].seq : after,
      earliest, cursorExpired,
    };
  }

  latestEventSeq(): number {
    const row = this.stmt("SELECT COALESCE(MAX(seq),0) AS seq FROM bridge_events").get() as Row | undefined;
    return row ? number(row.seq) : 0;
  }

  async waitForEvents(after: number, waitMs: number): Promise<void> {
    if (waitMs <= 0 || this.latestEventSeq() > after) return;
    await new Promise<void>((resolve) => {
      let timer: ReturnType<typeof setTimeout> | null = null;
      const wake = () => {
        if (timer) clearTimeout(timer);
        this.eventWaiters.delete(wake);
        resolve();
      };
      this.eventWaiters.add(wake);
      timer = setTimeout(wake, Math.min(waitMs, 25_000));
      timer.unref?.();
    });
  }

  private wakeWaiters(): void {
    for (const waiter of [...this.eventWaiters]) waiter();
  }

  purgeEvents(olderThanMs: number, maxRows = 100_000): number {
    const cutoff = nowMs() - olderThanMs;
    const over = Math.max(0, this.latestEventSeq() - maxRows);
    const result = this.stmt("DELETE FROM bridge_events WHERE occurred_at < ? OR seq <= ?").run(cutoff, over);
    return Number(result.changes);
  }

  claimLease(accountId: string, holder: string, ttlMs = 30_000): LeaseView {
    const now = nowMs();
    const current = this.getLease(accountId);
    if (current && current.expires_at > now && current.holder !== holder) throw new Error("lease_held");
    const next = {
      account_id: accountId, lease_id: randomUUID(),
      fencing_token: (current?.fencing_token ?? 0) + 1, holder,
      acquired_at: now, expires_at: now + ttlMs,
    };
    this.stmt(`INSERT INTO controller_leases(account_id,lease_id,fencing_token,holder,acquired_at,expires_at)
      VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET lease_id=excluded.lease_id,
      fencing_token=excluded.fencing_token,holder=excluded.holder,acquired_at=excluded.acquired_at,expires_at=excluded.expires_at`)
      .run(next.account_id, next.lease_id, next.fencing_token, next.holder, next.acquired_at, next.expires_at);
    return next;
  }

  renewLease(accountId: string, leaseId: string, fencingToken: number, ttlMs = 30_000): LeaseView {
    const now = nowMs();
    const current = this.getLease(accountId);
    if (!current || current.lease_id !== leaseId || current.fencing_token !== fencingToken) throw new Error("lease_fenced");
    if (current.expires_at <= now) throw new Error("lease_expired");
    const expires = now + ttlMs;
    this.stmt("UPDATE controller_leases SET expires_at=? WHERE account_id=?").run(expires, accountId);
    return { ...current, expires_at: expires };
  }

  releaseLease(accountId: string, leaseId: string, fencingToken: number): boolean {
    // Keep the row so the fencing token remains monotonic across releases and restarts.
    const result = this.stmt("UPDATE controller_leases SET expires_at=0,holder='released' WHERE account_id=? AND lease_id=? AND fencing_token=?")
      .run(accountId, leaseId, fencingToken);
    return Number(result.changes) > 0;
  }

  getLease(accountId: string): LeaseView | null {
    const row = this.stmt("SELECT * FROM controller_leases WHERE account_id=?").get(accountId) as Row | undefined;
    return row ? {
      account_id: String(row.account_id), lease_id: String(row.lease_id),
      fencing_token: number(row.fencing_token), holder: String(row.holder),
      acquired_at: number(row.acquired_at), expires_at: number(row.expires_at),
    } : null;
  }

  assertLease(accountId: string, leaseId: string, fencingToken: number): void {
    const lease = this.getLease(accountId);
    if (!lease || lease.lease_id !== leaseId || lease.fencing_token !== fencingToken) throw new Error("lease_fenced");
    if (lease.expires_at <= nowMs()) throw new Error("lease_expired");
  }

  createOperation(request: OperationRequest): OperationCreateResult {
    const hash = createHash("sha256").update(operationRequestHash(request)).digest("hex");
    const existing = this.getOperation(request.operation_id);
    if (existing) {
      const row = this.stmt("SELECT request_hash FROM operations WHERE operation_id=?").get(request.operation_id) as Row;
      return { created: false, conflict: String(row.request_hash) !== hash, operation: existing };
    }
    const now = nowMs();
    this.stmt(`INSERT INTO operations(
      operation_id,request_hash,session_id,origin_json,action,args_json,preconditions_json,state,created_at,updated_at
    ) VALUES(?,?,?,?,?,?,?,?,?,?)`).run(
      request.operation_id, hash, request.session_id, json(request.origin), request.action,
      json(request.args ?? {}), json(request.preconditions ?? {}), "accepted", now, now,
    );
    return { created: true, conflict: false, operation: this.getOperation(request.operation_id)! };
  }

  updateOperation(
    operationId: string,
    state: OperationState,
    options: { result?: Record<string, unknown> | null; error?: { code: string; message: string; retryable: boolean } | null } = {},
  ): void {
    this.stmt(`UPDATE operations SET state=?,result_json=?,error_code=?,error_message=?,retryable=?,updated_at=? WHERE operation_id=?`)
      .run(state, options.result === undefined ? null : json(options.result), options.error?.code ?? null,
        options.error?.message ?? null, options.error?.retryable ? 1 : 0, nowMs(), operationId);
  }

  updateStep(operationId: string, name: string, state: string, evidence?: Record<string, unknown> | null): void {
    this.stmt(`INSERT INTO operation_steps(operation_id,step_name,state,evidence_json,updated_at) VALUES(?,?,?,?,?)
      ON CONFLICT(operation_id,step_name) DO UPDATE SET state=excluded.state,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at`)
      .run(operationId, name, state, evidence ? json(evidence) : null, nowMs());
  }

  getOperation(operationId: string): OperationView | null {
    const row = this.stmt("SELECT * FROM operations WHERE operation_id=?").get(operationId) as Row | undefined;
    if (!row) return null;
    const steps = this.stmt("SELECT * FROM operation_steps WHERE operation_id=? ORDER BY updated_at ASC").all(operationId) as Row[];
    return {
      operation_id: String(row.operation_id), state: String(row.state) as OperationState,
      action: String(row.action) as OperationAction, session_id: String(row.session_id),
      origin: (parseJson(row.origin_json) ?? { kind: "dashboard", request_id: "unknown" }) as OperationOrigin,
      preconditions: (parseJson(row.preconditions_json) ?? {}) as OperationPreconditions,
      result: parseJson(row.result_json),
      steps: steps.map((step) => ({
        name: String(step.step_name), state: String(step.state),
        evidence: parseJson(step.evidence_json), updated_at: number(step.updated_at),
      })),
      error: row.error_code ? {
        code: String(row.error_code), message: String(row.error_message ?? ""), retryable: Boolean(row.retryable),
      } : null,
      created_at: number(row.created_at), updated_at: number(row.updated_at),
    };
  }

  listOperations(limit = 50): OperationView[] {
    const rows = this.stmt("SELECT operation_id FROM operations ORDER BY updated_at DESC LIMIT ?")
      .all(Math.max(1, Math.min(200, limit))) as Row[];
    return rows.flatMap((row) => {
      const operation = this.getOperation(String(row.operation_id));
      return operation ? [operation] : [];
    });
  }

  countUnresolvedOperations(): number {
    const row = this.stmt("SELECT COUNT(*) AS count FROM operations WHERE state IN ('accepted','running','unknown')").get() as Row;
    return number(row.count);
  }

  saveSnapshot(accountId: string, snapshot: ProtocolSnapshot): void {
    this.stmt(`INSERT INTO snapshots(account_id,bridge_instance_id,event_cursor,room_id,room_epoch,room_revision,payload_json,observed_at)
      VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET bridge_instance_id=excluded.bridge_instance_id,
      event_cursor=excluded.event_cursor,room_id=excluded.room_id,room_epoch=excluded.room_epoch,
      room_revision=excluded.room_revision,payload_json=excluded.payload_json,observed_at=excluded.observed_at`)
      .run(accountId, snapshot.bridge_instance_id, snapshot.event_cursor, snapshot.room_id,
        snapshot.room_epoch, snapshot.room_revision, json(snapshot), snapshot.captured_at);
  }

  getSnapshot(accountId: string): ProtocolSnapshot | null {
    const row = this.stmt("SELECT payload_json FROM snapshots WHERE account_id=?").get(accountId) as Row | undefined;
    return (row ? parseJson(row.payload_json) : null) as ProtocolSnapshot | null;
  }

  health(): Record<string, unknown> {
    const pageCount = this.db.prepare("PRAGMA page_count").get() as Row;
    const pageSize = this.db.prepare("PRAGMA page_size").get() as Row;
    return {
      schema_version: SCHEMA_VERSION, protocol: ASTRBOT_COVE_PROTOCOL,
      bridge_instance_id: this.bridgeInstanceId(), event_cursor: this.latestEventSeq(),
      unresolved_operations: this.countUnresolvedOperations(),
      db_bytes: number(pageCount.page_count) * number(pageSize.page_size),
    };
  }
}
