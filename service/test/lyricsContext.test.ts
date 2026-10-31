import assert from "node:assert/strict";
import test from "node:test";
import { buildFullLyricsModelContext, countAvailableLyricPayloads } from "../src/netease/lyricsContext.js";
import type { RawLyrics, SongDetails } from "../src/netease/types.js";

const song: SongDetails = {
  id: "42",
  name: "Test Song",
  artist: "Test Artist",
  durationMs: 180000,
};

function lyrics(overrides: Partial<RawLyrics> = {}): RawLyrics {
  return {
    lrc: null,
    tlyric: null,
    romalrc: null,
    klyric: null,
    yrc: null,
    ytlrc: null,
    yromalrc: null,
    raw: { secret: "not-model-context" },
    ...overrides,
  };
}

test("builds hidden full-song context from every available lyric payload", () => {
  const input = lyrics({
    lrc: "[00:01]original",
    tlyric: "[00:01]translation",
    romalrc: "[00:01]romanized",
    klyric: "[1000,1000]karaoke",
    yrc: "[1000,1000](1000,500,0)word",
    ytlrc: "[00:01]word translation",
    yromalrc: "[00:01]word romanized",
  });
  const context = buildFullLyricsModelContext(song, input);

  assert.ok(context);
  assert.match(context, /NETEASE FULL-LYRICS CONTEXT/);
  assert.match(context, /original/);
  assert.match(context, /translation/);
  assert.match(context, /romanized/);
  assert.match(context, /karaoke/);
  assert.match(context, /word translation/);
  assert.match(context, /word romanized/);
  assert.doesNotMatch(context, /not-model-context/);
  assert.equal(countAvailableLyricPayloads(input), 7);
});

test("returns null when no lyric text exists", () => {
  const input = lyrics();
  assert.equal(buildFullLyricsModelContext(song, input), null);
  assert.equal(countAvailableLyricPayloads(input), 0);
});
