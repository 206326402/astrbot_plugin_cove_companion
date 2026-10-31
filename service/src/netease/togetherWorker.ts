import { createHash, randomUUID } from "node:crypto";
import { NeteaseApiError, NeteaseClient, type AccountProfile, type PlayHistoryRecord } from "./client.js";
import { parseLrc, type LyricTimeline } from "./lyrics.js";
import { collectContactUserIds, parseLatestInvite } from "./inviteParser.js";
import { buildFullLyricsModelContext, countAvailableLyricPayloads } from "./lyricsContext.js";
import type { PlaybackStateSink } from "./playbackState.js";
import {
  NeteaseRealtimeTransport,
  type RealtimeChatRoomMessage,
  type RealtimeChatSendResult,
  type RealtimePlaybackEvent,
  type RealtimeTransportStatus,
} from "./realtimeTransport.js";
import type {
  PlayingState,
  SongDetails,
  TogetherInvite,
  TogetherWorkerStatus,
} from "./types.js";

type EventSink = (source: string, text: string, modelContext?: string) => unknown;

export type WorkerProtocolEvent = {
  type: "chat_message" | "playback_changed" | "queue_changed" | "room_joined" | "room_left" | "connection_changed" | "auth_invalid";
  occurred_at: number;
  source_event_key?: string | null;
  actor_role?: "owner" | "bot" | "other" | "unknown" | "system";
  actor_uid?: string | null;
  payload: Record<string, unknown>;
};

type TogetherWorkerOptions = {
  cookie: string;
  enabled: boolean;
  inviterUid?: string;
  pollIntervalMs?: number;
  heartbeatIntervalMs?: number;
  playbackReconcileIntervalMs?: number;
  playbackControlTimeoutMs?: number;
  onEvent: EventSink;
  stateSink?: PlaybackStateSink;
  onProtocolEvent?: (event: WorkerProtocolEvent) => void;
  autoAcceptInvites?: boolean;
};

export type PlaybackControlResult = {
  ok: true;
  confirmed: true;
  commandType: "PLAY" | "PAUSE" | "GOTO";
  roomId: string;
  songId: string;
  playStatus: "PLAY" | "PAUSE";
  progressMs: number;
  clientSeq: number;
  serverSeq: number;
  confirmedAt: string;
};

export type QueueMutationResult = {
  ok: true;
  confirmed: true;
  action: "ENQUEUE_NEXT";
  roomId: string;
  songId: string;
  afterSongId: string;
  version: number;
  confirmedAt: string;
};

type PendingPlaybackControl = {
  commandType: "PLAY" | "PAUSE" | "GOTO";
  roomId: string;
  songId: string;
  clientSeq: number;
  senderId: string;
  expectedPlayStatus: "PLAY" | "PAUSE";
  baselineServerSeq: number | null;
  timeout: ReturnType<typeof setTimeout>;
  resolve: (result: PlaybackControlResult) => void;
  reject: (error: Error) => void;
};

function collectMemberIds(value: unknown, depth = 0, output = new Set<string>()): string[] {
  if (depth > 8) return [...output];
  if (Array.isArray(value)) {
    for (const item of value) collectMemberIds(item, depth + 1, output);
    return [...output];
  }
  if (!value || typeof value !== "object") return [...output];
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (["userId", "user_id", "uid", "memberId", "member_id", "visitorId"].includes(key)) {
      const id = typeof item === "number" ? String(item) : typeof item === "string" ? item.trim() : "";
      if (/^\d{3,24}$/.test(id)) output.add(id);
    } else if (item && typeof item === "object") {
      collectMemberIds(item, depth + 1, output);
    }
  }
  return [...output];
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export class TogetherWorker {
  private readonly client: NeteaseClient;
  private readonly realtime: NeteaseRealtimeTransport;
  private readonly handledInvites = new Set<string>();
  private readonly handledChatMessageKeys = new Set<string>();
  private running = false;
  private runTask: Promise<void> | null = null;
  private roomId: string | null = null;
  private chatRoomId: string | null = null;
  private previousSongId: string | null = null;
  private previousPlayStatus: PlayingState["playStatus"] = "UNKNOWN";
  private joinedPending = false;
  private lastHeartbeatAt = 0;
  private lastRealtimeAttemptAt = 0;
  private lastPlaybackServerSeq: number | null = null;
  private lastPlaybackReconcileAt = 0;
  private latestPlaying: PlayingState | null = null;
  private latestPlaybackObservedAtMs = 0;
  private nextPlaybackClientSeq = 1;
  private pendingPlaybackControl: PendingPlaybackControl | null = null;
  private queueMutationPending = false;
  private playbackHandlingTail: Promise<void> = Promise.resolve();
  private errorStreak = 0;
  private accountProfile: AccountProfile | null = null;
  private currentLyricsModelContext: string | null = null;
  private readonly songDetailsCache = new Map<string, { observedAt: number; song: SongDetails }>();
  private protocolRoomEpoch: string | null = null;
  private protocolRoomRevision = 0;
  private protocolQueueFingerprint: string | null = null;
  private protocolPlaybackSignature = "UNKNOWN::0";
  private status: TogetherWorkerStatus;

  constructor(private readonly options: TogetherWorkerOptions) {
    this.client = new NeteaseClient(options.cookie);
    this.realtime = new NeteaseRealtimeTransport(
      options.enabled,
      (message) => {
        this.handleRealtimeChatMessage(message);
      },
      (event) => {
        this.handleRealtimePlaybackEvent(event);
      },
    );
    this.status = {
      enabled: options.enabled,
      phase: options.enabled ? "starting" : "disabled",
      accountId: null,
      roomId: null,
      currentSong: null,
      playStatus: "UNKNOWN",
      lastPollAt: null,
      lastHeartbeatAt: null,
      lastError: null,
    };
  }

  getStatus(): TogetherWorkerStatus {
    return { ...this.status };
  }

  getRealtimeStatus(): RealtimeTransportStatus {
    return this.realtime.getStatus();
  }

  async sendChatRoomText(text: string): Promise<RealtimeChatSendResult> {
    return await this.realtime.sendChatRoomText(text);
  }

  async leaveTogether(): Promise<{
    ok: true;
    ended: boolean;
    alreadyOut: boolean;
    roomId: string | null;
    confirmedAt: string;
  }> {
    const remote = await this.client.getRoomStatus();
    if (!remote.inRoom || !remote.roomId) {
      this.leaveRoom("一起听已经结束了。我会继续等你的下一次邀请。");
      return {
        ok: true,
        ended: false,
        alreadyOut: true,
        roomId: null,
        confirmedAt: new Date().toISOString(),
      };
    }

    const roomId = remote.roomId;
    await this.client.endRoom(roomId);
    const confirmed = await this.client.getRoomStatus();
    if (confirmed.inRoom) {
      throw new Error("NetEase Together leave was not confirmed by authoritative room status.");
    }

    this.leaveRoom("一起听已退出。我会继续等你的下一次邀请。");
    return {
      ok: true,
      ended: false,
      alreadyOut: false,
      roomId,
      confirmedAt: new Date().toISOString(),
    };
  }

  async pausePlayback(): Promise<PlaybackControlResult> {
    return await this.controlPlayback("PAUSE");
  }

  async resumePlayback(): Promise<PlaybackControlResult> {
    return await this.controlPlayback("PLAY");
  }

  async gotoPlayback(songId: string): Promise<PlaybackControlResult> {
    const targetSongId = songId.trim();
    if (!/^\d+$/.test(targetSongId)) {
      throw new Error("NetEase target songId must be numeric.");
    }
    if (!this.roomId) {
      throw new Error("NetEase Listen Together is not currently in a room.");
    }

    const playlist = await this.client.getTogetherPlaylist(this.roomId);
    if (!playlist.displayList.includes(targetSongId)) {
      throw new Error(
        `NetEase GOTO target is not in the current Together displayList: ${targetSongId}. Enqueue it before GOTO.`,
      );
    }

    return await this.controlPlayback("GOTO", targetSongId);
  }

  async nextPlayback(): Promise<PlaybackControlResult> {
    if (!this.roomId) throw new Error("NetEase Listen Together is not currently in a room.");
    const currentSongId = this.latestPlaying?.songId ?? this.status.currentSong?.id ?? null;
    if (!currentSongId) throw new Error("NetEase current playback state is unavailable.");

    const playlist = await this.client.getTogetherPlaylist(this.roomId);
    if (playlist.playMode !== "ORDER_LOOP") {
      throw new Error(`Unsupported NetEase Together playMode for NEXT: ${playlist.playMode ?? "unknown"}`);
    }
    if (!playlist.displayList.length) {
      throw new Error("NetEase Together displayList is empty.");
    }

    const currentIndex = playlist.displayList.indexOf(currentSongId);
    if (currentIndex < 0) {
      throw new Error(`Current NetEase song is missing from displayList: ${currentSongId}`);
    }

    const stillCurrentSongId = this.latestPlaying?.songId ?? this.status.currentSong?.id ?? null;
    if (stillCurrentSongId !== currentSongId) {
      throw new Error("NetEase playback changed while resolving NEXT; retry the command.");
    }

    const targetSongId = playlist.displayList[(currentIndex + 1) % playlist.displayList.length];
    return await this.gotoPlayback(targetSongId);
  }

  async enqueueNext(songId: string): Promise<QueueMutationResult> {
    const targetSongId = songId.trim();
    if (!/^\d+$/.test(targetSongId)) {
      throw new Error("NetEase target songId must be numeric.");
    }
    if (this.queueMutationPending) {
      throw new Error("NetEase queue mutation already pending.");
    }
    if (!this.roomId) throw new Error("NetEase Listen Together is not currently in a room.");

    const currentSongId = this.latestPlaying?.songId ?? this.status.currentSong?.id ?? null;
    if (!currentSongId) throw new Error("NetEase current playback state is unavailable.");
    if (targetSongId === currentSongId) {
      throw new Error("NetEase ENQUEUE_NEXT target cannot be the currently playing song.");
    }

    const accountId = this.status.accountId;
    if (!accountId) throw new Error("NetEase account identity is unavailable.");

    this.queueMutationPending = true;
    try {
      const roomId = this.roomId;
      const playlist = await this.client.getTogetherPlaylist(roomId);
      if (playlist.playMode !== "ORDER_LOOP") {
        throw new Error(`Unsupported NetEase Together playMode for ENQUEUE_NEXT: ${playlist.playMode ?? "unknown"}`);
      }
      if (!playlist.displayList.length) {
        throw new Error("NetEase Together displayList is empty.");
      }

      const currentIndex = playlist.displayList.indexOf(currentSongId);
      if (currentIndex < 0) {
        throw new Error(`Current NetEase song is missing from displayList: ${currentSongId}`);
      }

      const ownVersion = playlist.versions.find((entry) => entry.userId === accountId)?.version ?? 0;
      const nextVersion = ownVersion + 1;
      const nextDisplayList = playlist.displayList.filter((id) => id !== targetSongId);
      const currentIndexAfterRemoval = nextDisplayList.indexOf(currentSongId);
      if (currentIndexAfterRemoval < 0) {
        throw new Error(`Current NetEase song disappeared while building queue: ${currentSongId}`);
      }
      nextDisplayList.splice(currentIndexAfterRemoval + 1, 0, targetSongId);

      const stillCurrentSongId = this.latestPlaying?.songId ?? this.status.currentSong?.id ?? null;
      if (stillCurrentSongId !== currentSongId) {
        throw new Error("NetEase playback changed while resolving ENQUEUE_NEXT; retry the command.");
      }

      await this.client.replaceTogetherPlaylist({
        roomId,
        userId: accountId,
        version: nextVersion,
        displayList: nextDisplayList,
        randomList: playlist.randomList,
      });
      console.log(
        `NetEase queue mutation reported: action=ENQUEUE_NEXT afterSongId=${currentSongId} songId=${targetSongId} version=${nextVersion}`,
      );

      const timeoutMs = this.options.playbackControlTimeoutMs ?? 8_000;
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        await sleep(250);
        const confirmed = await this.client.getTogetherPlaylist(roomId);
        const confirmedCurrentIndex = confirmed.displayList.indexOf(currentSongId);
        const confirmedNext = confirmedCurrentIndex >= 0
          ? confirmed.displayList[(confirmedCurrentIndex + 1) % confirmed.displayList.length]
          : null;
        const confirmedVersion = confirmed.versions.find(
          (entry) => entry.userId === accountId,
        )?.version ?? 0;
        if (confirmedNext === targetSongId && confirmedVersion >= nextVersion) {
          console.log(
            `NetEase queue mutation confirmed: action=ENQUEUE_NEXT afterSongId=${currentSongId} songId=${targetSongId} version=${confirmedVersion}`,
          );
          return {
            ok: true,
            confirmed: true,
            action: "ENQUEUE_NEXT",
            roomId,
            songId: targetSongId,
            afterSongId: currentSongId,
            version: confirmedVersion,
            confirmedAt: new Date().toISOString(),
          };
        }
      }

      throw new Error(
        `NetEase ENQUEUE_NEXT timed out waiting for playlist confirmation: target=${targetSongId} version=${nextVersion}`,
      );
    } finally {
      this.queueMutationPending = false;
    }
  }


  private emitProtocol(event: WorkerProtocolEvent): void {
    try {
      this.options.onProtocolEvent?.(event);
    } catch (error) {
      const detail = error instanceof Error ? error.message : "unknown error";
      console.error("NetEase protocol event sink failed: " + detail);
    }
  }

  async joinTogether(roomId: string, inviterId: string): Promise<{
    ok: true;
    roomId: string;
    chatRoomId: string | null;
    confirmedAt: string;
  }> {
    const normalizedRoom = roomId.trim();
    const normalizedInviter = inviterId.trim();
    if (!normalizedRoom || !/^[A-Za-z0-9_-]{6,200}$/.test(normalizedRoom)) {
      throw new Error("NetEase roomId is invalid.");
    }
    if (!/^\d+$/.test(normalizedInviter)) throw new Error("NetEase inviterId must be numeric.");
    const accepted = await this.client.acceptInvite(normalizedRoom, normalizedInviter);
    if (!accepted.inRoom) throw new Error("NetEase invite acceptance was not confirmed.");
    const confirmed = await this.client.getRoomStatus();
    if (!confirmed.inRoom || !confirmed.roomId) throw new Error("NetEase room membership is not authoritative.");
    this.enterRoom(confirmed.roomId, confirmed.chatRoomId);
    if (confirmed.chatRoomId) await this.ensureRealtime(confirmed.roomId, confirmed.chatRoomId);
    return {
      ok: true,
      roomId: confirmed.roomId,
      chatRoomId: confirmed.chatRoomId,
      confirmedAt: new Date().toISOString(),
    };
  }

  async getProtocolState(): Promise<{
    roomId: string | null;
    roomEpoch: string | null;
    roomRevision: number;
    chatRoomId: string | null;
    queueFingerprint: string | null;
    currentSong: SongDetails | null;
    playStatus: "PLAY" | "PAUSE" | "UNKNOWN";
    progressMs: number;
    serverSeq: number | null;
    displayList: string[];
    randomList: string[];
    playMode: string | null;
    versions: Array<{ userId: string; version: number }>;
    memberIds: string[];
    membersKnown?: boolean;
    observedAt: number;
    authoritativeRoom: boolean;
  }> {
    const observedAt = Date.now();
    const remote = await this.client.getRoomStatus();
    if (!remote.inRoom || !remote.roomId) {
      if (this.roomId) this.leaveRoom("一起听已经结束了。");
      return {
        roomId: null, roomEpoch: null, roomRevision: this.protocolRoomRevision,
        chatRoomId: null, queueFingerprint: null, currentSong: null,
        playStatus: "UNKNOWN", progressMs: 0, serverSeq: null,
        displayList: [], randomList: [], playMode: null, versions: [], memberIds: [], membersKnown:true,
        observedAt, authoritativeRoom: true,
      };
    }
    if (this.roomId !== remote.roomId) this.enterRoom(remote.roomId, remote.chatRoomId);
    else if (remote.chatRoomId && remote.chatRoomId !== this.chatRoomId) this.chatRoomId = remote.chatRoomId;

    const [playing, playlist] = await Promise.all([
      this.client.getPlaying(remote.roomId),
      this.client.getTogetherPlaylist(remote.roomId),
    ]);
    let memberIds: string[] = [];
    let membersKnown=false;
    try {
      memberIds = collectMemberIds(await this.client.getTogetherRoomCheck(remote.roomId));
      membersKnown=memberIds.length>0;
    } catch (error) {
      console.error("NetEase room member check failed: " + (error instanceof Error ? error.message : "unknown"));
    }
    const currentSongId = playing.songId ?? this.status.currentSong?.id ?? null;
    const currentSong = currentSongId
      ? (this.status.currentSong?.id === currentSongId ? this.status.currentSong : await this.client.getSongDetails(currentSongId))
      : null;
    const queueFingerprint = createHash("sha256")
      .update(JSON.stringify({ list: playlist.displayList, random: playlist.randomList, versions: playlist.versions, mode: playlist.playMode }))
      .digest("hex");
    const playbackSignature = [currentSongId ?? "", playing.playStatus, playing.serverSeq ?? ""].join(":");
    if (playbackSignature !== this.protocolPlaybackSignature) {
      this.protocolPlaybackSignature = playbackSignature;
      this.protocolRoomRevision += 1;
      this.emitProtocol({
        type: "playback_changed", occurred_at: observedAt,
        source_event_key: "playback:" + playbackSignature,
        actor_role: "system",
        payload: { song_id: currentSongId, play_status: playing.playStatus, progress_ms: playing.progress, server_seq: playing.serverSeq ?? null },
      });
    }
    if (queueFingerprint !== this.protocolQueueFingerprint) {
      this.protocolQueueFingerprint = queueFingerprint;
      this.protocolRoomRevision += 1;
      this.emitProtocol({
        type: "queue_changed", occurred_at: observedAt,
        source_event_key: "queue:" + queueFingerprint,
        actor_role: "system",
        payload: { display_list: playlist.displayList, play_mode: playlist.playMode, versions: playlist.versions, fingerprint: queueFingerprint },
      });
    }
    this.status.currentSong = currentSong;
    this.status.playStatus = playing.playStatus;
    this.status.roomId = remote.roomId;
    return {
      roomId: remote.roomId, roomEpoch: this.protocolRoomEpoch,
      roomRevision: this.protocolRoomRevision, chatRoomId: remote.chatRoomId,
      queueFingerprint, currentSong, playStatus: playing.playStatus,
      progressMs: playing.progress, serverSeq: playing.serverSeq ?? null,
      displayList: playlist.displayList, randomList: playlist.randomList,
      playMode: playlist.playMode, versions: playlist.versions, memberIds, membersKnown,
      observedAt, authoritativeRoom: true,
    };
  }

  async searchSongs(query: string, limit = 12): Promise<SongDetails[]> {
    const normalized = query.trim();
    if (!normalized) throw new Error("NetEase search query is empty.");
    return await this.client.searchSongs(normalized, Math.max(1, Math.min(30, limit)));
  }

  async catalog(kind:string,query:string,id:string,limit:number) { return this.client.catalog(kind,query,id,limit); }

  async checkAvailability(id:string):Promise<boolean>{return this.client.checkAvailability(id);}

  async getSongDetailsMany(songIds: string[]): Promise<SongDetails[]> {
    const ids = [...new Set(songIds.map((value) => value.trim()).filter((value) => /^\d+$/.test(value)))].slice(0, 50);
    if (!ids.length) return [];
    const now = Date.now();
    const missing = ids.filter((id) => {
      const cached = this.songDetailsCache.get(id);
      return !cached || now - cached.observedAt > 30 * 60_000;
    });
    const output: SongDetails[] = [];
    for (let offset = 0; offset < missing.length; offset += 5) {
      const batch = missing.slice(offset, offset + 5);
      const resolved = await Promise.all(batch.map(async (id) => {
        try {
          const song = await this.client.getSongDetails(id);
          this.songDetailsCache.set(id, { observedAt: now, song });
          return song;
        } catch {
          return null;
        }
      }));
      for (const song of resolved) if (song) output.push(song);
    }
    if (this.songDetailsCache.size > 256) {
      const oldest = [...this.songDetailsCache.entries()].sort((a, b) => a[1].observedAt - b[1].observedAt);
      for (const [id] of oldest.slice(0, this.songDetailsCache.size - 256)) this.songDetailsCache.delete(id);
    }
    const byId = new Map(output.map((song) => [song.id, song]));
    return ids.map((id) => byId.get(id) ?? this.songDetailsCache.get(id)?.song).filter((song): song is SongDetails => Boolean(song));
  }

  async getPlaylistForProtocol(id:string,limit=30):Promise<Array<{id:string;name:string;artist:string;duration_ms:number;cover_url?:string}>> {
    if(!/^\d+$/.test(id)) throw new Error("playlist_id_invalid");
    return (await this.client.getPlaylistTracks(id,Math.min(60,limit))).map(v=>({id:v.id,name:v.name,artist:v.artist,duration_ms:v.durationMs,...(v.coverUrl?{cover_url:v.coverUrl}:{})}));
  }

  async getCandidates(limit = 40): Promise<Array<SongDetails & { source: string; reason?: string | null }>> {
    const output: Array<SongDetails & { source: string; reason?: string | null }> = [];
    const push = (song: SongDetails, source: string, reason?: string | null) => {
      if (output.length >= Math.max(1, Math.min(40, limit))) return;
      if (output.some((item) => item.id === song.id)) return;
      output.push({ ...song, source, ...(reason ? { reason } : {}) });
    };
    try {
      const recommended = await this.client.getDailyRecommendations(20);
      for (const song of recommended) push(song, "netease_daily", song.reason);
    } catch (error) {
      console.error("NetEase candidate daily recommendation failed: " + (error instanceof Error ? error.message : "unknown"));
    }
    try {
      const history: PlayHistoryRecord[] = await this.client.getPlayHistory(20);
      for (const record of history) push(record.song, "recent_play");
    } catch (error) {
      console.error("NetEase candidate history failed: " + (error instanceof Error ? error.message : "unknown"));
    }
    const playlistIds = (process.env.ASTROBOT_REFERENCE_PLAYLIST_IDS ?? "")
      .split(",").map((value) => value.trim()).filter((value) => /^\d+$/.test(value));
    for (const playlistId of playlistIds.slice(0, 3)) {
      try {
        const songs = await this.client.getPlaylistTracks(playlistId, 20);
        for (const song of songs) push(song, "reference_playlist");
      } catch (error) {
        console.error("NetEase candidate playlist failed: " + (error instanceof Error ? error.message : "unknown"));
      }
    }
    return output;
  }

  async getLyricsForSong(songId: string): Promise<{
    songId: string;
    lrc: LyricTimeline | null;
    translation: string | null;
    romanization: string | null;
  }> {
    if (!/^\d+$/.test(songId.trim())) throw new Error("NetEase songId must be numeric.");
    const raw = await this.client.getLyrics(songId.trim());
    return {
      songId,
      lrc: raw.lrc ? parseLrc(raw.lrc) : null,
      translation: raw.tlyric,
      romanization: raw.romalrc,
    };
  }

  async removeQueuedSong(songId: string): Promise<{
    ok: true;
    confirmed: true;
    action: "CANCEL_QUEUED_RECOMMENDATION";
    roomId: string;
    songId: string;
    version: number;
    confirmedAt: string;
  }> {
    const targetSongId = songId.trim();
    if (!/^\d+$/.test(targetSongId)) throw new Error("NetEase target songId must be numeric.");
    if (!this.roomId) throw new Error("NetEase Listen Together is not currently in a room.");
    const accountId = this.status.accountId;
    if (!accountId) throw new Error("NetEase account identity is unavailable.");
    const playlist = await this.client.getTogetherPlaylist(this.roomId);
    const currentSongId = this.latestPlaying?.songId ?? this.status.currentSong?.id ?? null;
    if (targetSongId === currentSongId) throw new Error("The currently playing song cannot be cancelled as queued.");
    if (!playlist.displayList.includes(targetSongId)) {
      return {
        ok: true, confirmed: true, action: "CANCEL_QUEUED_RECOMMENDATION",
        roomId: this.roomId, songId: targetSongId,
        version: playlist.versions.find((entry) => entry.userId === accountId)?.version ?? 0,
        confirmedAt: new Date().toISOString(),
      };
    }
    const ownVersion = playlist.versions.find((entry) => entry.userId === accountId)?.version ?? 0;
    const nextVersion = ownVersion + 1;
    const displayList = playlist.displayList.filter((id) => id !== targetSongId);
    await this.client.replaceTogetherPlaylist({
      roomId: this.roomId, userId: accountId, version: nextVersion,
      displayList, randomList: playlist.randomList,
    });
    const deadline = Date.now() + (this.options.playbackControlTimeoutMs ?? 8_000);
    while (Date.now() < deadline) {
      await sleep(250);
      const confirmed = await this.client.getTogetherPlaylist(this.roomId);
      const confirmedVersion = confirmed.versions.find((entry) => entry.userId === accountId)?.version ?? 0;
      if (!confirmed.displayList.includes(targetSongId) && confirmedVersion >= nextVersion) {
        return {
          ok: true, confirmed: true, action: "CANCEL_QUEUED_RECOMMENDATION",
          roomId: this.roomId, songId: targetSongId, version: confirmedVersion,
          confirmedAt: new Date().toISOString(),
        };
      }
    }
    throw new Error("NetEase queued recommendation cancellation was not confirmed.");
  }

  start(): void {
    if (this.running || !this.options.enabled) return;
    this.running = true;
    this.runTask = this.run();
  }

  async stop(): Promise<void> {
    this.running=false;
    await this.realtime.disconnect();
    if(this.runTask) await this.runTask;
    this.status.enabled=false;
  }

  async createTogether(): Promise<Record<string,unknown>> {
    const before=await this.client.getRoomStatus();
    if(before.inRoom) throw new Error("already_in_room");
    const created=await this.client.createRoom();
    const confirmed=await this.client.getRoomStatus();
    if(!confirmed.inRoom || confirmed.roomId!==created.roomId) throw new Error("room_create_confirmation_unknown");
    this.enterRoom(confirmed.roomId!,confirmed.chatRoomId);
    if(confirmed.chatRoomId) await this.ensureRealtime(confirmed.roomId!,confirmed.chatRoomId);
    const url=new URL("https://st.music.163.com/listen-together/share/index.html");
    url.searchParams.set("roomId",confirmed.roomId!);url.searchParams.set("inviterId",this.status.accountId!);
    return {room_id:confirmed.roomId,invite_url:url.toString(),confirmed_at:Date.now(),room_ended:false};
  }

  private async run(): Promise<void> {
    try {
      this.accountProfile = await this.client.getAccountProfile();
      this.status.accountId = this.accountProfile.id;
      this.status.phase = "waiting_invite";
      console.log(`NetEase Together worker ready for account ${this.status.accountId}`);
    } catch (error) {
      this.recordError(error, "Cookie validation failed");
    }

    while (this.running) {
      const startedAt = Date.now();
      try {
        if (!this.status.accountId) {
          this.accountProfile = await this.client.getAccountProfile();
          this.status.accountId = this.accountProfile.id;
        }
        await this.pollOnce();
        this.errorStreak = 0;
        this.status.lastError = null;
      } catch (error) {
        if (error instanceof NeteaseApiError && error.code === 488) {
          this.leaveRoom("房间已经结束了。一起听已退出，继续等待下一次邀请。");
        } else {
          this.recordError(error, "Together poll failed");
        }
      }

      const base = this.options.pollIntervalMs ?? 4000;
      const backoff = this.errorStreak > 0
        ? Math.min(60_000, base * 2 ** Math.min(this.errorStreak, 4))
        : base;
      const until=Date.now()+Math.max(250,backoff-(Date.now()-startedAt));
      while(this.running && Date.now()<until) await sleep(Math.min(250,until-Date.now()));
    }
  }

  private async pollOnce(): Promise<void> {
    this.status.lastPollAt = new Date().toISOString();
    const remote = await this.client.getRoomStatus();

    if (remote.inRoom && remote.roomId) {
      if (this.roomId !== remote.roomId) {
        this.enterRoom(remote.roomId, remote.chatRoomId);
      } else if (remote.chatRoomId && remote.chatRoomId !== this.chatRoomId) {
        this.chatRoomId = remote.chatRoomId;
      }
      if (remote.chatRoomId) await this.ensureRealtime(remote.roomId, remote.chatRoomId);
    } else if (this.roomId) {
      this.leaveRoom("一起听已经结束了。我会继续等你的下一次邀请。");
    }

    if (!this.roomId) {
      this.status.phase = "waiting_invite";
      if (this.options.autoAcceptInvites === false) return;
      const invite = await this.findInvite();
      if (!invite) return;
      await this.acceptInvite(invite);
    }

    if (!this.roomId) return;
    this.status.phase = "listening";

    const now = Date.now();
    const realtimeConnected = this.realtime.getStatus().connected;
    let playbackHandling = Promise.resolve();
    if (this.shouldReconcilePlayback(now, realtimeConnected)) {
      const playing = await this.client.getPlaying(this.roomId);
      const observedAtMs = Date.now();
      this.lastPlaybackReconcileAt = observedAtMs;
      playbackHandling = this.applyPlayback(playing, observedAtMs, "http");
    }

    await this.maybeHeartbeat();
    await playbackHandling;
  }

  private async ensureRealtime(roomId: string, chatRoomId: string): Promise<void> {
    const realtimeStatus = this.realtime.getStatus();
    if (
      realtimeStatus.connected
      && realtimeStatus.roomId === roomId
      && realtimeStatus.chatRoomId === chatRoomId
    ) return;

    const now = Date.now();
    if (now - this.lastRealtimeAttemptAt < 10_000) return;
    this.lastRealtimeAttemptAt = now;

    try {
      const credentials = await this.client.getRealtimeCredentials();
      console.log(
        `NetEase NIM realtime credentials acquired for account ${this.status.accountId ?? "unknown"}; addresses=${credentials.addresses.length}`,
      );
      if (!this.accountProfile) {
        this.accountProfile = await this.client.getAccountProfile();
        this.status.accountId = this.accountProfile.id;
      }
      await this.realtime.connect({
        roomId,
        chatRoomId,
        credentials,
        memberProfile: {
          userId: this.accountProfile.id,
          ...(this.accountProfile.nickname ? { nick: this.accountProfile.nickname } : {}),
          ...(this.accountProfile.avatarUrl ? { avatar: this.accountProfile.avatarUrl } : {}),
          ...(this.accountProfile.gender !== null ? { gender: this.accountProfile.gender } : {}),
        },
      });
      console.log(`NetEase NIM realtime connected: roomId=${roomId} chatRoomId=${chatRoomId}`);
    } catch (error) {
      this.realtime.recordConnectionError(error);
      const detail = error instanceof Error ? error.message : "unknown error";
      console.error(`NetEase NIM realtime connect failed: ${detail}`);
    }
  }

  private async findInvite(): Promise<TogetherInvite | null> {
    const ownId = this.status.accountId;
    if (!ownId) return null;
    let contactIds: string[];

    if (this.options.inviterUid) {
      contactIds = [this.options.inviterUid];
    } else {
      const contacts = await this.client.getRecentContacts();
      contactIds = collectContactUserIds(contacts, ownId);
    }

    for (const uid of contactIds) {
      const history = await this.client.getPrivateHistory(uid);
      const invite = parseLatestInvite(history, uid);
      if (invite && !this.handledInvites.has(invite.roomId)) return invite;
    }
    return null;
  }

  private async acceptInvite(invite: TogetherInvite): Promise<void> {
    this.status.phase = "joining";
    this.handledInvites.add(invite.roomId);
    if (this.handledInvites.size > 50) {
      const oldest = this.handledInvites.values().next().value as string | undefined;
      if (oldest) this.handledInvites.delete(oldest);
    }
    try {
      const accepted = await this.client.acceptInvite(invite.roomId, invite.inviterId);
      this.enterRoom(accepted.roomId ?? invite.roomId, accepted.chatRoomId);
      if (accepted.roomId && accepted.chatRoomId) {
        await this.ensureRealtime(accepted.roomId, accepted.chatRoomId);
      }
    } catch (error) {
      this.handledInvites.delete(invite.roomId);
      throw error;
    }
  }

  private enterRoom(roomId: string, chatRoomId: string | null = null): void {
    const changedRoom = this.roomId !== roomId;
    if (changedRoom) {
      this.rejectPendingPlaybackControl("NetEase Listen Together room changed before playback control confirmation.");
      void this.realtime.disconnect();
      this.nextPlaybackClientSeq = 1;
    }
    this.roomId = roomId;
    this.chatRoomId = chatRoomId;
    this.protocolRoomEpoch = randomUUID();
    this.protocolRoomRevision += 1;
    this.protocolQueueFingerprint = null;
    this.emitProtocol({
      type: "room_joined", occurred_at: Date.now(),
      source_event_key: "room_joined:" + roomId + ":" + this.protocolRoomEpoch,
      actor_role: "system", payload: { room_id: roomId, chat_room_id: chatRoomId },
    });
    this.previousSongId = null;
    this.previousPlayStatus = "UNKNOWN";
    this.currentLyricsModelContext = null;
    this.joinedPending = true;
    this.lastHeartbeatAt = 0;
    this.lastRealtimeAttemptAt = 0;
    this.lastPlaybackServerSeq = null;
    this.lastPlaybackReconcileAt = 0;
    this.latestPlaying = null;
    this.latestPlaybackObservedAtMs = 0;
    this.status.roomId = roomId;
    this.status.currentSong = null;
    this.status.playStatus = "UNKNOWN";
    this.status.phase = "listening";
    this.updateState((sink) => sink.enterRoom(roomId));
  }

  private leaveRoom(message: string): void {
    const wasInRoom = Boolean(this.roomId);
    const endedRoomId = this.roomId;
    this.rejectPendingPlaybackControl("NetEase Listen Together ended before playback control confirmation.");
    this.roomId = null;
    this.chatRoomId = null;
    if (endedRoomId) {
      this.protocolRoomRevision += 1;
      this.emitProtocol({
        type: "room_left", occurred_at: Date.now(),
        source_event_key: "room_left:" + endedRoomId + ":" + Date.now(),
        actor_role: "system", payload: { room_id: endedRoomId, reason: message },
      });
    }
    this.protocolRoomEpoch = null;
    this.protocolQueueFingerprint = null;
    this.previousSongId = null;
    this.previousPlayStatus = "UNKNOWN";
    this.currentLyricsModelContext = null;
    this.joinedPending = false;
    this.lastHeartbeatAt = 0;
    this.lastRealtimeAttemptAt = 0;
    this.lastPlaybackServerSeq = null;
    this.lastPlaybackReconcileAt = 0;
    this.latestPlaying = null;
    this.latestPlaybackObservedAtMs = 0;
    this.status.roomId = null;
    this.status.currentSong = null;
    this.status.playStatus = "UNKNOWN";
    this.status.phase = "waiting_invite";
    void this.realtime.disconnect();
    this.updateState((sink) => sink.leaveRoom());
    if (wasInRoom) this.options.onEvent("netease.together", message);
  }

  private currentPlaybackProgressMs(nowMs = Date.now()): number {
    const playing = this.latestPlaying;
    if (!playing) return 0;

    const elapsedMs = playing.playStatus === "PLAY"
      ? Math.max(0, nowMs - this.latestPlaybackObservedAtMs)
      : 0;
    let progressMs = Math.max(0, playing.progress + elapsedMs);
    const currentSong = this.status.currentSong;
    if (currentSong && currentSong.id === playing.songId) {
      progressMs = Math.min(progressMs, currentSong.durationMs);
    }
    return progressMs;
  }

  private async controlPlayback(
    commandType: "PLAY" | "PAUSE" | "GOTO",
    requestedTargetSongId?: string,
  ): Promise<PlaybackControlResult> {
    if (this.pendingPlaybackControl) {
      throw new Error(
        `NetEase playback control already pending: ${this.pendingPlaybackControl.commandType}`,
      );
    }
    if (!this.roomId) throw new Error("NetEase Listen Together is not currently in a room.");

    const realtimeStatus = this.realtime.getStatus();
    if (!realtimeStatus.connected) {
      throw new Error("NetEase realtime is not connected; refusing unconfirmed playback control.");
    }

    const senderId = this.status.accountId;
    if (!senderId) throw new Error("NetEase account identity is unavailable.");

    const playing = this.latestPlaying;
    const currentSongId = playing?.songId ?? this.status.currentSong?.id ?? null;
    if (!playing || !currentSongId) throw new Error("NetEase current playback state is unavailable.");

    const targetSongId = commandType === "GOTO"
      ? requestedTargetSongId ?? null
      : currentSongId;
    if (!targetSongId) throw new Error("NetEase target songId is unavailable.");

    const expectedPlayStatus: "PLAY" | "PAUSE" = commandType === "PAUSE" ? "PAUSE" : "PLAY";
    const progressMs = commandType === "GOTO" ? 0 : this.currentPlaybackProgressMs();
    const roomId = this.roomId;
    const clientSeq = this.nextPlaybackClientSeq++;
    const baselineServerSeq = this.lastPlaybackServerSeq;
    const timeoutMs = this.options.playbackControlTimeoutMs ?? 8_000;

    let resolveConfirmation!: (result: PlaybackControlResult) => void;
    let rejectConfirmation!: (error: Error) => void;
    const confirmation = new Promise<PlaybackControlResult>((resolve, reject) => {
      resolveConfirmation = resolve;
      rejectConfirmation = reject;
    });

    const timeout = setTimeout(() => {
      if (this.pendingPlaybackControl?.clientSeq !== clientSeq) return;
      this.pendingPlaybackControl = null;
      rejectConfirmation(new Error(
        `NetEase playback control timed out waiting for realtime confirmation: ${commandType} clientSeq=${clientSeq}`,
      ));
    }, timeoutMs);

    this.pendingPlaybackControl = {
      commandType,
      roomId,
      songId: targetSongId,
      clientSeq,
      senderId,
      expectedPlayStatus,
      baselineServerSeq,
      timeout,
      resolve: resolveConfirmation,
      reject: rejectConfirmation,
    };

    try {
      await this.client.reportPlaybackCommand({
        roomId,
        commandType,
        progress: progressMs,
        playStatus: expectedPlayStatus,
        formerSongId: currentSongId,
        targetSongId,
        clientSeq,
      });
      console.log(
        `NetEase playback control reported: command=${commandType} formerSongId=${currentSongId} targetSongId=${targetSongId} progressMs=${Math.floor(progressMs)} clientSeq=${clientSeq}`,
      );
    } catch (error) {
      if (this.pendingPlaybackControl?.clientSeq === clientSeq) {
        clearTimeout(this.pendingPlaybackControl.timeout);
        this.pendingPlaybackControl = null;
      }
      throw error;
    }

    return await confirmation;
  }

  private confirmPendingPlaybackControl(event: RealtimePlaybackEvent): void {
    const pending = this.pendingPlaybackControl;
    if (!pending) return;
    if (event.clientSeq !== pending.clientSeq) return;
    if (event.senderId !== pending.senderId) return;
    if (event.commandType !== pending.commandType) return;
    if (event.songId !== pending.songId) return;
    if (event.playStatus !== pending.expectedPlayStatus) return;
    if (event.serverSeq === null) return;
    if (
      pending.baselineServerSeq !== null
      && event.serverSeq <= pending.baselineServerSeq
    ) return;

    clearTimeout(pending.timeout);
    this.pendingPlaybackControl = null;
    const result: PlaybackControlResult = {
      ok: true,
      confirmed: true,
      commandType: pending.commandType,
      roomId: pending.roomId,
      songId: pending.songId,
      playStatus: event.playStatus,
      progressMs: event.progressMs,
      clientSeq: pending.clientSeq,
      serverSeq: event.serverSeq,
      confirmedAt: new Date(event.receivedAtMs).toISOString(),
    };
    console.log(
      `NetEase playback control confirmed: command=${result.commandType} songId=${result.songId} clientSeq=${result.clientSeq} serverSeq=${result.serverSeq}`,
    );
    pending.resolve(result);
  }

  private rejectPendingPlaybackControl(message: string): void {
    const pending = this.pendingPlaybackControl;
    if (!pending) return;
    clearTimeout(pending.timeout);
    this.pendingPlaybackControl = null;
    pending.reject(new Error(message));
  }

  private shouldReconcilePlayback(nowMs: number, realtimeConnected: boolean): boolean {
    if (!this.latestPlaying) return true;
    if (!realtimeConnected) return true;
    const interval = this.options.playbackReconcileIntervalMs ?? 30_000;
    return nowMs - this.lastPlaybackReconcileAt >= interval;
  }

  private async maybeHeartbeat(): Promise<void> {
    const playing = this.latestPlaying;
    if (!this.roomId || !playing) return;

    const interval = this.options.heartbeatIntervalMs ?? 10_000;
    const nowMs = Date.now();
    if (nowMs - this.lastHeartbeatAt < interval) return;

    const progressMs = this.currentPlaybackProgressMs(nowMs);
    await this.client.sendHeartbeat(
      this.roomId,
      playing.songId,
      playing.playStatus === "PLAY",
      progressMs,
    );
    this.lastHeartbeatAt = Date.now();
    this.status.lastHeartbeatAt = new Date(this.lastHeartbeatAt).toISOString();
  }

  private applyPlayback(
    playing: PlayingState,
    observedAtMs: number,
    origin: "http" | "realtime",
  ): Promise<void> {
    const serverSeq = playing.serverSeq;
    if (
      serverSeq !== undefined
      && this.lastPlaybackServerSeq !== null
      && serverSeq < this.lastPlaybackServerSeq
    ) {
      console.log(
        `Together stale playback ignored: origin=${origin} serverSeq=${serverSeq} latest=${this.lastPlaybackServerSeq}`,
      );
      return Promise.resolve();
    }
    if (serverSeq !== undefined) this.lastPlaybackServerSeq = serverSeq;

    this.latestPlaying = { ...playing };
    this.latestPlaybackObservedAtMs = observedAtMs;

    this.updateState((sink) => sink.updatePlayback({
      songId: playing.songId,
      playStatus: playing.playStatus,
      progressMs: playing.progress,
      observedAtMs,
      ...(serverSeq === undefined ? {} : { serverSeq }),
    }));

    const handling = this.playbackHandlingTail.then(() => this.handlePlaying(playing));
    this.playbackHandlingTail = handling.catch(() => {});
    return handling;
  }

  private handleRealtimePlaybackEvent(event: RealtimePlaybackEvent): void {
    if (!this.roomId) return;

    const knownStatus = this.status.playStatus;
    const playStatus = event.playStatus === "UNKNOWN" && knownStatus !== "UNKNOWN"
      ? knownStatus
      : event.playStatus;
    const songId = event.songId ?? this.status.currentSong?.id ?? this.previousSongId;
    const playing: PlayingState = {
      songId,
      playStatus,
      progress: event.progressMs,
      ...(event.serverSeq === null ? {} : { serverSeq: event.serverSeq }),
    };

    void this.applyPlayback(playing, event.receivedAtMs, "realtime")
      .then(() => {
        this.confirmPendingPlaybackControl(event);
      })
      .catch((error) => {
        const detail = error instanceof NeteaseApiError
          ? `${error.operation}${error.code === null ? "" : ` code=${error.code}`}`
          : error instanceof Error ? error.message : "unknown error";
        console.error(`Together realtime playback handling failed: ${detail}`);
      });
  }

  private async handlePlaying(playing: PlayingState): Promise<void> {
    this.status.playStatus = playing.playStatus;
    const statusChanged = this.previousPlayStatus !== playing.playStatus;

    if (playing.songId && playing.songId !== this.previousSongId) {
      const song = await this.client.getSongDetails(playing.songId);
      this.previousSongId = playing.songId;
      this.currentLyricsModelContext = null;
      this.status.currentSong = song;
      this.updateState((sink) => sink.updateSong(song));
      const lyricsContext = await this.loadLyrics(song);

      if (playing.playStatus === "PLAY") {
        const prefix = this.joinedPending
          ? "你把我拉进一起听啦。现在播放"
          : "你换到了";
        this.options.onEvent(
          "netease.music_changed",
          `${prefix}《${song.name}》— ${song.artist}。请结合我们的对话自然回应；如果没必要点评，也可以只安静陪听。`,
          lyricsContext,
        );
        this.joinedPending = false;
      }
    } else if (statusChanged && this.previousPlayStatus !== "UNKNOWN") {
      if (playing.playStatus === "PAUSE") {
        this.options.onEvent(
          "netease.playback",
          "一起听暂停了。",
          this.currentLyricsModelContext ?? undefined,
        );
      } else if (playing.playStatus === "PLAY") {
        const song = this.status.currentSong;
        this.options.onEvent(
          "netease.playback",
          song
            ? `一起听继续播放：《${song.name}》— ${song.artist}。`
            : "一起听继续播放了。",
          this.currentLyricsModelContext ?? undefined,
        );
        this.joinedPending = false;
      }
    }

    this.previousPlayStatus = playing.playStatus;
  }

  private handleRealtimeChatMessage(message: RealtimeChatRoomMessage): void {
    if (message.category !== "text" || !message.text) return;
    const ownAccountId = this.status.accountId;
    if (ownAccountId && message.senderId === ownAccountId) return;

    const roomKey = this.roomId ?? "no-room";
    const messageKey = message.messageId
      ? roomKey + ":" + message.messageId
      : roomKey + ":" + (message.senderId ?? "unknown") + ":" + String(message.timetagMs ?? message.receivedAtMs) + ":" + message.text;
    if (this.handledChatMessageKeys.has(messageKey)) {
      const suffix = message.messageId ? "***" + message.messageId.slice(-6) : "unknown";
      console.warn("NetEase duplicate ChatRoom message suppressed: messageId=" + suffix);
      return;
    }
    this.handledChatMessageKeys.add(messageKey);
    while (this.handledChatMessageKeys.size > 512) {
      const oldest = this.handledChatMessageKeys.values().next().value;
      if (!oldest) break;
      this.handledChatMessageKeys.delete(oldest);
    }

    const ownerUid = this.options.inviterUid ?? process.env.NETEASE_INVITER_UID?.trim() ?? null;
    const actorRole = ownAccountId && message.senderId === ownAccountId
      ? "bot"
      : ownerUid && message.senderId === ownerUid
        ? "owner"
        : message.senderId
          ? "other"
          : "unknown";
    this.emitProtocol({
      type: "chat_message",
      occurred_at: message.timetagMs ?? message.receivedAtMs,
      source_event_key: message.messageId ?? messageKey,
      actor_role: actorRole,
      actor_uid: message.senderId,
      payload: {
        text: message.text,
        sender_nick: message.senderNick,
        message_id: message.messageId,
        category: message.category,
      },
    });
    this.options.onEvent(
      "netease.chatroom",
      message.text,
      this.currentLyricsModelContext ?? undefined,
    );
  }

  private async loadLyrics(song: SongDetails): Promise<string | undefined> {
    try {
      const lyrics = await this.client.getLyrics(song.id);
      this.updateState((sink) => sink.updateLyrics(song.id, lyrics));
      const modelContext = buildFullLyricsModelContext(song, lyrics);
      this.currentLyricsModelContext = modelContext;
      if (modelContext) {
        console.log(
          `Together full lyrics context ready: songId=${song.id} payloads=${countAvailableLyricPayloads(lyrics)} contextChars=${modelContext.length}`,
        );
      }
      return modelContext ?? undefined;
    } catch (error) {
      this.currentLyricsModelContext = null;
      const detail = error instanceof NeteaseApiError
        ? `${error.operation}${error.code === null ? "" : ` code=${error.code}`}`
        : error instanceof Error ? error.message : "unknown error";
      console.error(`Together lyrics load failed: ${detail}`);
      return undefined;
    }
  }

  private updateState(update: (sink: PlaybackStateSink) => void): void {
    if (!this.options.stateSink) return;
    try {
      update(this.options.stateSink);
    } catch (error) {
      const detail = error instanceof Error ? error.message : "unknown error";
      console.error(`Together state update failed: ${detail}`);
    }
  }

  private recordError(error: unknown, prefix: string): void {
    if(error instanceof NeteaseApiError && [301,302,401,403].includes(error.code || 0)) {
      this.running=false;this.status.accountId=null;this.status.phase="error";
      this.options.onProtocolEvent?.({type:"auth_invalid",occurred_at:Date.now(),payload:{reason:"credentials_rejected"}});
      void this.realtime.disconnect();this.status.lastError="credentials_rejected";return;
    }
    this.errorStreak += 1;
    this.status.phase = this.errorStreak >= 3 ? "error" : "backoff";
    const detail = error instanceof NeteaseApiError
      ? `${error.operation}${error.code === null ? "" : ` code=${error.code}`}`
      : error instanceof Error ? error.message : "unknown error";
    this.status.lastError = `${prefix}: ${detail}`;
    console.error(this.status.lastError);
  }
}

export function createTogetherWorker(
  onEvent: EventSink,
  stateSink?: PlaybackStateSink,
  onProtocolEvent?: (event: WorkerProtocolEvent) => void,
  credentialOverride?: string,
): TogetherWorker {
  const cookie = credentialOverride ?? process.env.NETEASE_COOKIE?.trim() ?? "";
  const explicitlyDisabled = /^(0|false|off|no)$/i.test(
    process.env.TOGETHER_ENABLED?.trim() ?? "",
  );
  const enabled = Boolean(cookie) && !explicitlyDisabled;
  const parseInterval = (name: string, fallback: number) => {
    const value = Number(process.env[name]);
    return Number.isFinite(value) && value >= 1000 ? value : fallback;
  };

  if (cookie && !cookie.includes("MUSIC_U=")) {
    console.warn("NETEASE_COOKIE is set but MUSIC_U is missing; Together worker may not authenticate.");
  }

  return new TogetherWorker({
    cookie,
    enabled,
    inviterUid: process.env.NETEASE_INVITER_UID?.trim() || undefined,
    autoAcceptInvites: !/^(astrbot|1|true|yes)$/i.test(process.env.HOST_MODE?.trim() ?? "") && process.env.ASTROBOT_BRIDGE_AUTO_ACCEPT !== "true",
    onProtocolEvent,
    pollIntervalMs: parseInterval("TOGETHER_POLL_INTERVAL_MS", 4000),
    heartbeatIntervalMs: parseInterval("TOGETHER_HEARTBEAT_INTERVAL_MS", 10_000),
    playbackReconcileIntervalMs: parseInterval("TOGETHER_PLAYBACK_RECONCILE_INTERVAL_MS", 30_000),
    playbackControlTimeoutMs: parseInterval("TOGETHER_PLAYBACK_CONTROL_TIMEOUT_MS", 8_000),
    onEvent,
    stateSink,
  });
}
