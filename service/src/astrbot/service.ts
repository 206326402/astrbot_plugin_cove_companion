import type { MusicAuthentication } from "./credentials.js";
import { randomUUID } from "node:crypto";
import type { TogetherWorker, WorkerProtocolEvent } from "../netease/togetherWorker.js";
import type { SongDetails } from "../netease/types.js";
import {
  ASTRBOT_COVE_PROTOCOL,
  nowMs,
  type OperationRequest,
  type OperationView,
  type ProtocolSnapshot,
  type ProtocolSong,
} from "./protocol.js";
import { AstrbotCoveStore, type LeaseView } from "./store.js";

type WorkerState = Awaited<ReturnType<TogetherWorker["getProtocolState"]>>;
type Candidate = Awaited<ReturnType<TogetherWorker["getCandidates"]>>[number];
type Lyrics = Awaited<ReturnType<TogetherWorker["getLyricsForSong"]>>;

function song(value: SongDetails | null): ProtocolSong | null {
  return value ? {
    id: value.id,
    name: value.name,
    artist: value.artist,
    duration_ms: value.durationMs,
    ...(value.coverUrl ? { cover_url: value.coverUrl } : {}),
  } : null;
}

function errorMessage(error: unknown): string {
  const text=error instanceof Error ? error.message : String(error);
  return text.replace(/(MUSIC_U|MUSIC_A|__csrf|csrf_token|NMTID|NETEASE_COOKIE|authorization)\s*[:=]\s*[^;\s,]+/gi,'$1=[redacted]').slice(0,500);
}

function unknownRemote(error: unknown): boolean {
  return /timeout|timed out|ECONN|fetch failed|socket|network|aborted|unknown|confirmation|不可用/i.test(errorMessage(error));
}

export class AstrbotCoveService {
  private worker: TogetherWorker | null = null;
  authentication: MusicAuthentication | null = null;
  replaceWorker: ((cookie:string)=>Promise<void>) | null = null;
  private switching=false;
  async availability(id:string):Promise<{available:boolean}> {
    if(!this.worker)throw new Error('worker_unavailable');
    return {available:await this.worker.checkAvailability(id)};
  }
  async activateAccount(id:string):Promise<Record<string,unknown>> {
    if(!this.authentication || !this.replaceWorker) throw new Error("auth_unavailable");
    if(this.switching) throw new Error("account_switch_in_progress");
    this.switching=true;
    try {
      const previousAccount=this.accountId();
      const current=previousAccount?await this.snapshot(true):null;
      if(current && (current.evidence.room.stale || current.room_id)) throw new Error("end_confirmed_room_before_switch");
      if(Number((this.store.db.prepare("SELECT COUNT(*) AS n FROM operations WHERE state IN ('accepted','running') OR (state='unknown' AND updated_at>?)").get(nowMs()-120_000) as {n:number}).n)) throw new Error("unresolved_operations_before_switch");
      const previous=this.accountId();
      if(previous) this.store.db.prepare("UPDATE controller_leases SET expires_at=0,holder='account_switch' WHERE account_id=?").run(previous);
      const result=await this.authentication.activate(id,this.replaceWorker);
      this.lastSnapshot=null;this.recentOperations.length=0;
      this.store.recordEvent({event_type:"auth_changed",source:"cove.auth",payload:{account_id:result.account_id}});
      return result;
    } finally {this.switching=false;}
  }
  private assertCurrent(request:OperationRequest,account:string):void {
    if(this.switching || account!==this.accountId()) throw new Error("account_changed");
    if(request.deadline_at && nowMs()>request.deadline_at) throw new Error("command_deadline_expired");
    this.store.assertLease(account,request.lease_id,request.fencing_token);
    const generation=Number(this.store.getMeta("generation:"+request.session_id)||0);
    const binding=Number(this.store.getMeta("binding:"+account)||0);
    if((request.control_generation||0)<generation || (request.binding_revision||0)<binding) throw new Error("obsolete_command");
  }
  private timer: ReturnType<typeof setInterval> | null = null;
  private purgeAt = 0;
  private refreshInFlight: Promise<ProtocolSnapshot> | null = null;
  private operationTail = new Map<string, Promise<void>>();
  private lastSnapshot: ProtocolSnapshot | null = null;
  private readonly recentOperations: Array<{
    id: string;
    action: string;
    startedAt: number;
    finishedAt: number | null;
    targetSongId: string | null;
  }> = [];
  private readonly ownerUid = process.env.NETEASE_INVITER_UID?.trim() || null;

  constructor(readonly store: AstrbotCoveStore) {}

  attachWorker(worker: TogetherWorker): void {
    this.worker = worker;
  }

  ingestWorkerEvent(event: WorkerProtocolEvent): void {
    if(event.type === "auth_invalid") this.authentication?.invalidate();
    if (event.type === "playback_changed") {
      const songId = typeof event.payload.song_id === "string" ? event.payload.song_id : null;
      const formerSongId = typeof event.payload.former_song_id === "string"
        ? event.payload.former_song_id
        : typeof event.payload.formerSongId === "string"
          ? event.payload.formerSongId
          : null;
      if (songId && formerSongId && songId === formerSongId) return;
    }
    const source = event.type === "chat_message"
      ? "netease.chatroom"
      : event.type.startsWith("room_")
        ? "netease.room"
        : event.type.startsWith("playback_")
          ? "netease.playback"
          : event.type.startsWith("queue_")
            ? "netease.queue"
            : "netease.connection";
    const matchedOperation = this.matchRecentOperation(event);
    const attribution = matchedOperation
      ? "plugin"
      : this.recentOperations.some((operation) => this.operationAttributionWindowOpen(operation))
        ? "unknown"
        : "external";
    const payload = {
      ...event.payload,
      attribution,
      ...(matchedOperation ? { operation_id: matchedOperation.id } : {}),
    };
    this.store.recordEvent({
      event_type: event.type,
      room_id: this.lastSnapshot?.room_id ?? null,
      room_epoch: this.lastSnapshot?.room_epoch ?? null,
      occurred_at: event.occurred_at,
      source,
      source_event_key: event.source_event_key ?? null,
      actor_role: event.actor_role ?? "unknown",
      actor_uid: event.actor_uid ?? null,
      operation_id: matchedOperation?.id ?? null,
      payload,
    });
  }

  private operationAttributionWindowOpen(operation: { startedAt: number; finishedAt: number | null }): boolean {
    if (operation.finishedAt !== null) return nowMs() - operation.finishedAt < 5_000;
    return nowMs() - operation.startedAt < 30_000;
  }

  private matchRecentOperation(event: WorkerProtocolEvent): { id: string; action: string } | null {
    const playlistActions = new Set(["ENQUEUE_NEXT", "PLAY_NOW", "CANCEL_QUEUED_RECOMMENDATION"]);
    const playbackActions = new Set(["PLAY_NOW", "NEXT", "PAUSE", "RESUME"]);
    const eventSong = typeof event.payload.song_id === "string" ? event.payload.song_id : null;
    const displayList = Array.isArray(event.payload.display_list)
      ? event.payload.display_list.map((item) => String(item))
      : null;
    for (let index = this.recentOperations.length - 1; index >= 0; index -= 1) {
      const operation = this.recentOperations[index];
      if (!this.operationAttributionWindowOpen(operation)) continue;
      if (event.type === "queue_changed" && playlistActions.has(operation.action)) {
        if (operation.action === "CANCEL_QUEUED_RECOMMENDATION") {
          if (!displayList || !operation.targetSongId || !displayList.includes(operation.targetSongId)) return operation;
        } else if (!displayList || !operation.targetSongId || displayList.includes(operation.targetSongId)) {
          return operation;
        }
      }
      if (event.type === "playback_changed" && playbackActions.has(operation.action)) {
        if (operation.action === "PAUSE" || operation.action === "RESUME") return operation;
        if (operation.action === "NEXT" || !operation.targetSongId || !eventSong || operation.targetSongId === eventSong) {
          return operation;
        }
      }
      if (event.type === "room_joined" && operation.action === "JOIN") return operation;
      if (event.type === "room_left" && operation.action === "LEAVE") return operation;
    }
    return null;
  }

  start(): void {
    if (this.timer) return;
    void this.refresh(true).catch((error) => {
      console.error("AstrBot protocol initial refresh failed: " + errorMessage(error));
    });
    this.timer = setInterval(() => {
      void this.refresh(true).catch((error) => {
        console.error("AstrBot protocol refresh failed: " + errorMessage(error));
      });
      if (Date.now() >= this.purgeAt) {
        this.purgeAt = Date.now() + 60 * 60_000;
        try {
          this.store.purgeEvents(7 * 24 * 60 * 60_000);
        } catch (error) {
          console.error("AstrBot protocol event retention failed: " + errorMessage(error));
        }
      }
    }, 10_000);
    this.timer.unref?.();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.store.close();
  }

  private requireWorker(): TogetherWorker {
    if (!this.worker) throw new Error("worker_not_attached");
    return this.worker;
  }

  accountId(): string | null {
    return this.worker?.getStatus().accountId ?? null;
  }

  async refresh(fresh = true): Promise<ProtocolSnapshot> {
    if (this.refreshInFlight) return await this.refreshInFlight;
    this.refreshInFlight = this.doRefresh(fresh).finally(() => {
      this.refreshInFlight = null;
    });
    return await this.refreshInFlight;
  }

  private async doRefresh(fresh: boolean): Promise<ProtocolSnapshot> {
    const worker = this.requireWorker();
    const status = worker.getStatus();
    const realtime = worker.getRealtimeStatus();
    const cached = this.store.getSnapshot(status.accountId ?? "unbound");
    if (!fresh && cached) {
      this.lastSnapshot = cached;
      return cached;
    }
    let state: WorkerState | null = null;
    try {
      state = await worker.getProtocolState();
    } catch (error) {
      if (cached) {
        const stale = structuredClone(cached);
        stale.captured_at = nowMs();
        stale.evidence.room.stale = true;
        stale.evidence.members.stale = true;
        stale.evidence.queue.stale = true;
        stale.evidence.playback.stale = true;
        stale.worker.last_error = errorMessage(error);
        this.lastSnapshot = stale;
        return stale;
      }
      throw error;
    }
    const observed = state.observedAt;
    const membersKnown=state.membersKnown === true;
    const ownerPresent = this.ownerUid && membersKnown ? state.memberIds.includes(this.ownerUid) : null;
    const members: ProtocolSnapshot["members"] = [];
    if (status.accountId) members.push({ uid: status.accountId, role: "bot", present: true });
    if (this.ownerUid) members.push({ uid: this.ownerUid, role: "owner", present: ownerPresent });
    for (const uid of state.memberIds) {
      if (uid === status.accountId || uid === this.ownerUid) continue;
      members.push({ uid, role: "other", present: true });
    }
    for (const version of state.versions) {
      if (version.userId === status.accountId || version.userId === this.ownerUid) continue;
      if (members.some((member) => member.uid === version.userId)) continue;
      members.push({ uid: version.userId, role: "unknown", present: null });
    }
    const snapshot: ProtocolSnapshot = {
      protocol: ASTRBOT_COVE_PROTOCOL,
      bridge_instance_id: this.store.bridgeInstanceId(),
      event_cursor: this.store.latestEventSeq(),
      captured_at: nowMs(),
      room_id: state.roomId,
      room_epoch: state.roomEpoch,
      room_revision: state.roomRevision,
      connected: Boolean(state.roomId) && status.phase === "listening",
      nim_connected: realtime.connected,
      bot_account: { uid: status.accountId },
      owner_binding: { uid: this.ownerUid, present: ownerPresent },
      members,
      current_song: song(state.currentSong),
      playback: {
        status: state.playStatus,
        progress_ms: state.progressMs,
        observed_at: observed,
        server_seq: state.serverSeq,
      },
      queue: {
        song_ids: state.displayList,
        play_mode: state.playMode,
        versions: state.versions.map((entry) => ({ user_id: entry.userId, version: entry.version })),
        fingerprint: state.queueFingerprint,
      },
      evidence: {
        room: { source: "netease_api", observed_at: observed, validated_at: observed, stale: false },
        members: { source: "netease_room_check", observed_at: observed, validated_at: observed, stale: ownerPresent === null },
        queue: { source: "netease_api", observed_at: observed, validated_at: observed, stale: false },
        playback: { source: realtime.connected ? "nim+netease_api" : "netease_api", observed_at: observed, validated_at: observed, stale: !realtime.connected },
      },
      worker: {
        phase: status.phase,
        account_id: status.accountId,
        last_poll_at: status.lastPollAt,
        last_heartbeat_at: status.lastHeartbeatAt,
        last_error: status.lastError,
      },
    };
    this.lastSnapshot = snapshot;
    const accountKey = status.accountId ?? "unbound";
    this.store.saveSnapshot(accountKey, snapshot);
    return snapshot;
  }

  private statusOwnerHint(): boolean | null {
    return null;
  }

  async bootstrap(): Promise<Record<string, unknown>> {
    const snapshot = await this.refresh(true);
    const lease = snapshot.worker.account_id ? this.store.getLease(snapshot.worker.account_id) : null;
    return {
      protocol: ASTRBOT_COVE_PROTOCOL,
      capabilities: [
        "snapshot", "events", "search", "candidates", "lyrics", "controller_lease", "commands",
        "operation_status", "message_outbox", "cancel_queued_recommendation",
      ],
      service_instance_id: snapshot.bridge_instance_id,
      account_ready: Boolean(snapshot.worker.account_id),
      control: lease ? {
        lease_id: lease.lease_id,
        fencing_token: lease.fencing_token,
        holder: lease.holder,
        expires_at: lease.expires_at,
      } : null,
    };
  }

  async snapshot(fresh = false): Promise<ProtocolSnapshot> {
    return await this.refresh(fresh);
  }

  async events(after: number, limit: number, waitMs: number): Promise<Record<string, unknown>> {
    await this.store.waitForEvents(after, waitMs);
    const result = this.store.listEvents(after, limit);
    const snapshot = this.lastSnapshot ?? this.store.getSnapshot(this.accountId() ?? "unbound");
    if (result.cursorExpired) {
      return {
        code: "cursor_expired",
        cursor: after,
        earliest: result.earliest,
        next: result.next,
        event_cursor: this.store.latestEventSeq(),
        snapshot: snapshot ?? (await this.snapshot(true)),
      };
    }
    return {
      events: result.events,
      next: result.next,
      event_cursor: this.store.latestEventSeq(),
      wait_ms: waitMs,
    };
  }

  async search(query: string, limit: number): Promise<SongDetails[]> {
    return await this.requireWorker().searchSongs(query, limit);
  }

  async catalog(kind:string,query:string,id:string,limit:number) { return this.requireWorker().catalog(kind,query,id,limit); }

  async candidates(limit = 40): Promise<Candidate[]> {
    return await this.requireWorker().getCandidates(limit);
  }

  async tracks(ids: string[]): Promise<SongDetails[]> {
    return await this.requireWorker().getSongDetailsMany(ids);
  }

  async lyrics(songId: string): Promise<Lyrics> {
    return await this.requireWorker().getLyricsForSong(songId);
  }

  claimLease(holder: string, accountId?: string): LeaseView {
    const account = accountId || this.accountId();
    if (!account) throw new Error("account_not_ready");
    return this.store.claimLease(account, holder);
  }

  renewLease(leaseId: string, fencingToken: number): LeaseView {
    const account = this.accountId();
    if (!account) throw new Error("account_not_ready");
    return this.store.renewLease(account, leaseId, fencingToken);
  }

  releaseLease(leaseId: string, fencingToken: number): boolean {
    const account = this.accountId();
    if (!account) throw new Error("account_not_ready");
    return this.store.releaseLease(account, leaseId, fencingToken);
  }

  async submitOperation(request: OperationRequest): Promise<{ status: number; operation: OperationView; conflict?: boolean }> {
    const account = this.accountId();
    if (!account) throw new Error("account_not_ready");
    this.store.assertLease(account, request.lease_id, request.fencing_token);
    const created = this.store.createOperation(request);
    if (created.conflict) return { status: 409, operation: created.operation, conflict: true };
    if (created.created) {
      this.store.updateStep(request.operation_id, "accepted", "confirmed", { account_id: account });
      this.store.recordEvent({
        event_type: "command_result",
        source: "astrbot.operation",
        source_event_key: "accepted:" + request.operation_id,
        actor_role: "system",
        operation_id: request.operation_id,
        payload: { state: "accepted", action: request.action, session_id: request.session_id },
      });
      this.enqueueOperation(request, account);
    }
    return { status: 202, operation: created.operation };
  }

  private enqueueOperation(request: OperationRequest, account: string): void {
    const previous = this.operationTail.get(account) ?? Promise.resolve();
    const next = previous.then(async () => {
      await this.runOperation(request, account);
    }).catch((error) => {
      console.error("AstrBot operation runner failed: " + errorMessage(error));
    }).finally(() => {
      if (this.operationTail.get(account) === next) this.operationTail.delete(account);
    });
    this.operationTail.set(account, next);
  }

  private async runOperation(request: OperationRequest, account: string): Promise<void> {
    const operation = request.operation_id;
    let remoteStarted = false;
    let result: Record<string, unknown> | null = null;
    try {
      this.store.updateOperation(operation, "running");
      this.store.updateStep(operation, "precheck", "running", null);
      this.assertCurrent(request,account);
      const current = await this.snapshot(true);
      const precondition = this.checkPreconditions(request, current);
      if (precondition) {
        this.store.updateStep(operation, "precheck", "rejected", { reason: precondition });
        this.store.updateOperation(operation, "rejected", {
          error: { code: "precondition_failed", message: precondition, retryable: true },
        });
        return;
      }
      this.store.updateStep(operation, "precheck", "confirmed", {
        room_id: current.room_id,
        room_revision: current.room_revision,
        queue_fingerprint: current.queue.fingerprint,
      });
      this.store.updateStep(operation, "remote_request", "running", { action: request.action });
      this.assertCurrent(request,account);
      remoteStarted = true;
      const recent = {
        id: operation,
        action: request.action,
        startedAt: nowMs(),
        finishedAt: null as number | null,
        targetSongId: typeof request.args?.song_id === "string" ? request.args.song_id : null,
      };
      this.recentOperations.push(recent);
      while (this.recentOperations.length > 50) this.recentOperations.shift();
      try {
        result = await this.executeRemote(request);
      } finally {
        recent.finishedAt = nowMs();
      }
      this.store.updateStep(operation, "remote_request", "confirmed", result);
      this.store.updateStep(operation, "remote_confirm", "confirmed", result);
      this.store.updateOperation(operation, "confirmed", { result });
      this.store.updateStep(operation, "recorded", "confirmed", { result });
      this.store.recordEvent({
        event_type: "command_result",
        source: "astrbot.operation",
        source_event_key: "confirmed:" + operation,
        actor_role: "system",
        operation_id: operation,
        payload: { state: "confirmed", action: request.action, result },
      });
      await this.refresh(true).catch(() => undefined);
    } catch (error) {
      const message = errorMessage(error);
      const ambiguous = remoteStarted && unknownRemote(error);
      const state = ambiguous ? "unknown" : "rejected";
      this.store.updateStep(operation, "remote_request", state, { error: message });
      this.store.updateOperation(operation, state, {
        error: {
          code: ambiguous ? "remote_result_unknown" : "operation_failed",
          message,
          retryable: !ambiguous,
        },
      });
      this.store.recordEvent({
        event_type: "command_result",
        source: "astrbot.operation",
        source_event_key: state + ":" + operation,
        actor_role: "system",
        operation_id: operation,
        payload: { state, action: request.action, error: message },
      });
    }
  }

  private checkPreconditions(request: OperationRequest, current: ProtocolSnapshot): string | null {
    const pre = request.preconditions;
    if (!pre) return null;
    if (pre.room_id && pre.room_id !== current.room_id) return "room_id changed";
    if (pre.room_epoch && pre.room_epoch !== current.room_epoch) return "room_epoch changed";
    if (typeof pre.room_revision === "number" && pre.room_revision !== current.room_revision) return "room_revision changed";
    if (pre.queue_fingerprint && pre.queue_fingerprint !== current.queue.fingerprint) return "queue_fingerprint changed";
    return null;
  }

  private async executeRemote(request: OperationRequest): Promise<Record<string, unknown>> {
    const worker = this.requireWorker();
    const args = request.args ?? {};
    const songId = typeof args.song_id === "string" ? args.song_id : "";
    const text = typeof args.text === "string" ? args.text.trim() : "";
    switch (request.action) {
      case "CREATE": return await worker.createTogether();
      case "JOIN": {
        const roomId = typeof args.room_id === "string" ? args.room_id.trim() : "";
        const inviterId = typeof args.inviter_id === "string" ? args.inviter_id.trim() : "";
        return { ...(await worker.joinTogether(roomId, inviterId)) };
      }
      case "ENQUEUE_NEXT":
        if (!songId) throw new Error("song_id is required");
        return { ...(await worker.enqueueNext(songId)) };
      case "PLAY_NOW": {
        if (!songId) throw new Error("song_id is required");
        const enqueued = await worker.enqueueNext(songId);
        this.store.updateStep(request.operation_id, "enqueue_confirmed", "confirmed", { ...enqueued });
        this.assertCurrent(request,this.accountId()!);
        const played = await worker.gotoPlayback(songId);
        return { enqueue: enqueued, playback: played };
      }
      case "PAUSE":
        return { ...(await worker.pausePlayback()) };
      case "RESUME":
        return { ...(await worker.resumePlayback()) };
      case "NEXT":
        return { ...(await worker.nextPlayback()) };
      case "LEAVE":
        return { ...(await worker.leaveTogether()) };
      case "SEND_MESSAGE": {
        if (!text) throw new Error("text is required");
        if (text.length > 500) throw new Error("text exceeds 500 characters");
        return { ...(await worker.sendChatRoomText(text)) };
      }
      case "CANCEL_QUEUED_RECOMMENDATION":
        if (!songId) throw new Error("song_id is required");
        return { ...(await worker.removeQueuedSong(songId)) };
      default:
        throw new Error("unsupported_action");
    }
  }

  async playlist(id:string,limit:number):Promise<ProtocolSong[]> {
    return this.requireWorker().getPlaylistForProtocol(id,limit);
  }

  getOperation(operationId: string): OperationView | null {
    return this.store.getOperation(operationId);
  }

  health(): Record<string, unknown> {
    const status = this.worker?.getStatus();
    const realtime = this.worker?.getRealtimeStatus();
    return {
      live: Boolean(this.worker),
      ready: Boolean(status?.accountId),
      protocol: ASTRBOT_COVE_PROTOCOL,
      worker: status ?? null,
      realtime: realtime ?? null,
      store: this.store.health(),
      last_snapshot_at: this.lastSnapshot?.captured_at ?? null,
      event_lag_ms: this.lastSnapshot ? Math.max(0, nowMs() - this.lastSnapshot.captured_at) : null,
    };
  }
}
