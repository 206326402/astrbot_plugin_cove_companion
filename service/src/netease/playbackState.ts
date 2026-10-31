import { findLyricWindow, parseLrc, type LyricLine, type LyricTimeline, type LyricWindow } from "./lyrics.js";
import type { PlaybackSnapshot, RawLyrics, SongDetails } from "./types.js";

export type CurrentPlaybackState = {
  inRoom: boolean;
  roomId?: string;
  playStatus: PlaybackSnapshot["playStatus"];
  song?: SongDetails;
  progressMs: number;
  durationMs: number;
  progressRatio: number;
  lyric?: LyricWindow;
  observedAt: string | null;
  stateUpdatedAt: string;
};

export type CurrentLyricsState = {
  songId: string | null;
  lines: LyricLine[];
};

export type PlaybackStateSink = {
  enterRoom(roomId: string): void;
  leaveRoom(): void;
  updatePlayback(snapshot: PlaybackSnapshot): void;
  updateSong(song: SongDetails): void;
  updateLyrics(songId: string, lyrics: RawLyrics): void;
};

export class PlaybackStateStore implements PlaybackStateSink {
  private roomId: string | null = null;
  private snapshot: PlaybackSnapshot | null = null;
  private song: SongDetails | null = null;
  private lyrics: LyricTimeline | null = null;
  private stateUpdatedAtMs = Date.now();

  enterRoom(roomId: string): void {
    if (this.roomId !== roomId) {
      this.snapshot = null;
      this.song = null;
      this.lyrics = null;
    }
    this.roomId = roomId;
    this.touch();
  }

  leaveRoom(): void {
    this.roomId = null;
    this.snapshot = null;
    this.song = null;
    this.lyrics = null;
    this.touch();
  }

  updatePlayback(snapshot: PlaybackSnapshot): void {
    if (this.song && snapshot.songId && snapshot.songId !== this.song.id) {
      this.song = null;
      this.lyrics = null;
    }
    if (this.shouldReplacePlaybackAnchor(snapshot)) {
      this.snapshot = { ...snapshot };
    }
    this.touch();
  }

  updateSong(song: SongDetails): void {
    if (this.song?.id !== song.id) this.lyrics = null;
    this.song = { ...song };
    this.touch();
  }

  updateLyrics(songId: string, lyrics: RawLyrics): void {
    if (this.song?.id !== songId) return;
    this.lyrics = parseLrc(lyrics.lrc ?? "");
    this.touch();
  }

  getCurrentState(now = Date.now()): CurrentPlaybackState {
    if (!this.roomId) {
      return {
        inRoom: false,
        playStatus: "UNKNOWN",
        progressMs: 0,
        durationMs: 0,
        progressRatio: 0,
        observedAt: null,
        stateUpdatedAt: new Date(this.stateUpdatedAtMs).toISOString(),
      };
    }

    const durationMs = Math.max(0, this.song?.durationMs ?? 0);
    const rawProgress = this.snapshot
      ? this.snapshot.progressMs + (
        this.snapshot.playStatus === "PLAY"
          ? Math.max(0, now - this.snapshot.observedAtMs)
          : 0
      )
      : 0;
    const progressMs = Math.min(durationMs, Math.max(0, rawProgress));
    const lyric = this.lyrics
      ? findLyricWindow(this.lyrics.lines, progressMs)
      : undefined;

    return {
      inRoom: true,
      roomId: this.roomId,
      playStatus: this.snapshot?.playStatus ?? "UNKNOWN",
      ...(this.song ? { song: { ...this.song } } : {}),
      progressMs,
      durationMs,
      progressRatio: durationMs > 0 ? progressMs / durationMs : 0,
      ...(lyric && Object.keys(lyric).length > 0 ? { lyric } : {}),
      observedAt: this.snapshot
        ? new Date(this.snapshot.observedAtMs).toISOString()
        : null,
      stateUpdatedAt: new Date(this.stateUpdatedAtMs).toISOString(),
    };
  }

  getLyricsState(): CurrentLyricsState {
    return {
      songId: this.song?.id ?? null,
      lines: this.lyrics
        ? this.lyrics.lines.map((line) => ({ timeMs: line.timeMs, text: line.text }))
        : [],
    };
  }

  private touch(): void {
    this.stateUpdatedAtMs = Date.now();
  }

  private shouldReplacePlaybackAnchor(incoming: PlaybackSnapshot): boolean {
    const current = this.snapshot;
    if (!current) return true;
    if (
      incoming.songId !== current.songId
      || incoming.playStatus !== current.playStatus
    ) {
      return true;
    }

    if (incoming.serverSeq !== undefined && current.serverSeq !== undefined) {
      return incoming.serverSeq !== current.serverSeq;
    }
    if (incoming.serverSeq !== undefined) return true;

    return incoming.progressMs !== current.progressMs;
  }
}
