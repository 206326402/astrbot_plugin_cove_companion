import assert from "node:assert/strict";
import test from "node:test";
import { PlaybackStateStore } from "../src/netease/playbackState.js";
import type { RawLyrics, SongDetails } from "../src/netease/types.js";

const song = (id: string, durationMs = 10_000): SongDetails => ({
  id,
  name: `Song ${id}`,
  artist: "Artist",
  durationMs,
});

const lyrics = (lrc: string): RawLyrics => ({
  lrc,
  tlyric: null,
  romalrc: null,
  klyric: null,
  yrc: null,
  ytlrc: null,
  yromalrc: null,
  raw: {},
});

test("returns an explicit not-in-room state without throwing", () => {
  const state = new PlaybackStateStore().getCurrentState(0);
  assert.equal(state.inRoom, false);
  assert.equal(state.playStatus, "UNKNOWN");
  assert.equal(state.progressMs, 0);
});

test("estimates live progress while playing", () => {
  const store = new PlaybackStateStore();
  store.enterRoom("room");
  store.updateSong(song("one"));
  store.updatePlayback({
    songId: "one",
    playStatus: "PLAY",
    progressMs: 2000,
    observedAtMs: 1000,
  });

  assert.equal(store.getCurrentState(2500).progressMs, 3500);
});

test("does not advance progress while paused", () => {
  const store = new PlaybackStateStore();
  store.enterRoom("room");
  store.updateSong(song("one"));
  store.updatePlayback({
    songId: "one",
    playStatus: "PAUSE",
    progressMs: 2000,
    observedAtMs: 1000,
  });

  assert.equal(store.getCurrentState(9000).progressMs, 2000);
});

test("clamps estimated progress to song duration", () => {
  const store = new PlaybackStateStore();
  store.enterRoom("room");
  store.updateSong(song("one", 5000));
  store.updatePlayback({
    songId: "one",
    playStatus: "PLAY",
    progressMs: 4500,
    observedAtMs: 1000,
  });

  const state = store.getCurrentState(3000);
  assert.equal(state.progressMs, 5000);
  assert.equal(state.progressRatio, 1);
});

test("clears and refreshes cached lyrics when the song changes", () => {
  const store = new PlaybackStateStore();
  store.enterRoom("room");
  store.updateSong(song("one"));
  store.updateLyrics("one", lyrics("[00:00]旧歌词"));
  assert.equal(store.getCurrentState(0).lyric?.current, "旧歌词");

  store.updateSong(song("two"));
  assert.equal(store.getCurrentState(0).lyric, undefined);

  store.updateLyrics("one", lyrics("[00:00]过期歌词"));
  assert.equal(store.getCurrentState(0).lyric, undefined);

  store.updateLyrics("two", lyrics("[00:00]新歌词"));
  assert.equal(store.getCurrentState(0).lyric?.current, "新歌词");
});

test("keeps the original timing anchor across polls with the same serverSeq", () => {
  const store = new PlaybackStateStore();
  store.enterRoom("room");
  store.updateSong(song("one", 60_000));
  store.updatePlayback({
    songId: "one",
    playStatus: "PLAY",
    progressMs: 500,
    observedAtMs: 1000,
    serverSeq: 7,
  });
  store.updatePlayback({
    songId: "one",
    playStatus: "PLAY",
    progressMs: 500,
    observedAtMs: 3500,
    serverSeq: 7,
  });

  const state = store.getCurrentState(4000);
  assert.equal(state.progressMs, 3500);
  assert.equal(state.observedAt, new Date(1000).toISOString());
});

test("continues advancing about twenty seconds despite repeated command polls", () => {
  const store = new PlaybackStateStore();
  store.enterRoom("room");
  store.updateSong(song("one", 60_000));
  store.updatePlayback({
    songId: "one",
    playStatus: "PLAY",
    progressMs: 500,
    observedAtMs: 1000,
    serverSeq: 7,
  });
  for (const observedAtMs of [3500, 6000, 8500]) {
    store.updatePlayback({
      songId: "one",
      playStatus: "PLAY",
      progressMs: 500,
      observedAtMs,
      serverSeq: 7,
    });
  }

  assert.equal(store.getCurrentState(21_000).progressMs, 20_500);
});

test("recalibrates when serverSeq changes", () => {
  const store = new PlaybackStateStore();
  store.enterRoom("room");
  store.updateSong(song("one", 60_000));
  store.updatePlayback({
    songId: "one",
    playStatus: "PLAY",
    progressMs: 1000,
    observedAtMs: 1000,
    serverSeq: 7,
  });
  store.updatePlayback({
    songId: "one",
    playStatus: "PLAY",
    progressMs: 7000,
    observedAtMs: 5000,
    serverSeq: 8,
  });

  const state = store.getCurrentState(6000);
  assert.equal(state.progressMs, 8000);
  assert.equal(state.observedAt, new Date(5000).toISOString());
});

test("recalibrates on PLAY to PAUSE and stops advancing", () => {
  const store = new PlaybackStateStore();
  store.enterRoom("room");
  store.updateSong(song("one", 60_000));
  store.updatePlayback({
    songId: "one",
    playStatus: "PLAY",
    progressMs: 1000,
    observedAtMs: 1000,
    serverSeq: 7,
  });
  store.updatePlayback({
    songId: "one",
    playStatus: "PAUSE",
    progressMs: 6000,
    observedAtMs: 5000,
    serverSeq: 8,
  });

  assert.equal(store.getCurrentState(20_000).progressMs, 6000);
});

test("restarts progress growth after PAUSE to PLAY", () => {
  const store = new PlaybackStateStore();
  store.enterRoom("room");
  store.updateSong(song("one", 60_000));
  store.updatePlayback({
    songId: "one",
    playStatus: "PAUSE",
    progressMs: 6000,
    observedAtMs: 5000,
    serverSeq: 8,
  });
  store.updatePlayback({
    songId: "one",
    playStatus: "PLAY",
    progressMs: 6000,
    observedAtMs: 10_000,
    serverSeq: 9,
  });

  assert.equal(store.getCurrentState(15_000).progressMs, 11_000);
});

test("establishes a new anchor when the song changes", () => {
  const store = new PlaybackStateStore();
  store.enterRoom("room");
  store.updateSong(song("one", 60_000));
  store.updatePlayback({
    songId: "one",
    playStatus: "PLAY",
    progressMs: 10_000,
    observedAtMs: 1000,
    serverSeq: 7,
  });
  store.updatePlayback({
    songId: "two",
    playStatus: "PLAY",
    progressMs: 500,
    observedAtMs: 20_000,
    serverSeq: 7,
  });
  store.updateSong(song("two", 60_000));

  assert.equal(store.getCurrentState(21_000).progressMs, 1500);
});

test("keeps the anchor for repeated command tuples without serverSeq", () => {
  const store = new PlaybackStateStore();
  store.enterRoom("room");
  store.updateSong(song("one", 60_000));
  store.updatePlayback({
    songId: "one",
    playStatus: "PLAY",
    progressMs: 1000,
    observedAtMs: 1000,
  });
  store.updatePlayback({
    songId: "one",
    playStatus: "PLAY",
    progressMs: 1000,
    observedAtMs: 3500,
  });

  const state = store.getCurrentState(11_000);
  assert.equal(state.progressMs, 11_000);
  assert.equal(state.observedAt, new Date(1000).toISOString());
});

test("exposes the cached lyric timeline and clears it with the song", () => {
  const store = new PlaybackStateStore();
  store.enterRoom("room");
  store.updateSong(song("one"));
  store.updateLyrics("one", lyrics("[00:01]第一句\n[00:03]第二句"));

  assert.deepEqual(store.getLyricsState(), {
    songId: "one",
    lines: [
      { timeMs: 1000, text: "第一句" },
      { timeMs: 3000, text: "第二句" },
    ],
  });

  store.updateSong(song("two"));
  assert.deepEqual(store.getLyricsState(), {
    songId: "two",
    lines: [],
  });
});
