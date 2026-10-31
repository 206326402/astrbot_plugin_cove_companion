import type { RawLyrics, SongDetails } from "./types.js";

const LYRIC_FIELDS: Array<[keyof Omit<RawLyrics, "raw">, string]> = [
  ["lrc", "Original timed lyrics"],
  ["tlyric", "Translated timed lyrics"],
  ["romalrc", "Romanized timed lyrics"],
  ["klyric", "Karaoke lyrics"],
  ["yrc", "Word-level lyrics"],
  ["ytlrc", "Word-level translated lyrics"],
  ["yromalrc", "Word-level romanized lyrics"],
];

export function buildFullLyricsModelContext(
  song: SongDetails,
  lyrics: RawLyrics,
): string | null {
  const sections = LYRIC_FIELDS
    .map(([field, label]) => {
      const value = lyrics[field];
      return value?.trim()
        ? `[${label} / ${field}]\n${value.trim()}`
        : null;
    })
    .filter((value): value is string => Boolean(value));

  if (sections.length === 0) return null;

  return [
    "NETEASE FULL-LYRICS CONTEXT (song-change snapshot)",
    `songId=${song.id}`,
    `songName=${song.name}`,
    `artist=${song.artist}`,
    `durationMs=${song.durationMs}`,
    "Use these full-song lyrics for global understanding of themes, callbacks, and later lines.",
    "Do not treat this snapshot as the current playback position. Realtime/NIM playback state is authoritative for the current position.",
    "Available lyric payloads follow:",
    ...sections,
  ].join("\n\n");
}

export function countAvailableLyricPayloads(lyrics: RawLyrics): number {
  return LYRIC_FIELDS.reduce((count, [field]) => (
    lyrics[field]?.trim() ? count + 1 : count
  ), 0);
}
