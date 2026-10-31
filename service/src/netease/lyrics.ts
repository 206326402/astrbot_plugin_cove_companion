export type LyricLine = {
  timeMs: number;
  text: string;
};

export type LyricTimeline = {
  raw: string;
  lines: LyricLine[];
};

export type LyricWindow = {
  previous?: string;
  current?: string;
  next?: string;
};

const TIMESTAMP = /\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]/g;

function fractionToMilliseconds(value: string | undefined): number {
  if (!value) return 0;
  if (value.length === 1) return Number(value) * 100;
  if (value.length === 2) return Number(value) * 10;
  return Number(value);
}

export function parseLrc(raw: string): LyricTimeline {
  const lines: LyricLine[] = [];

  for (const sourceLine of raw.split(/\r?\n/)) {
    const matches = [...sourceLine.matchAll(TIMESTAMP)];
    if (matches.length === 0) continue;

    const text = sourceLine.replace(TIMESTAMP, "").trim();
    if (!text) continue;

    for (const match of matches) {
      const minutes = Number(match[1]);
      const seconds = Number(match[2]);
      if (!Number.isFinite(minutes) || !Number.isFinite(seconds) || seconds >= 60) continue;
      lines.push({
        timeMs: minutes * 60_000 + seconds * 1000 + fractionToMilliseconds(match[3]),
        text,
      });
    }
  }

  lines.sort((a, b) => a.timeMs - b.timeMs);
  return { raw, lines };
}

export function findLyricWindow(lines: readonly LyricLine[], progressMs: number): LyricWindow {
  if (lines.length === 0) return {};

  let currentIndex = -1;
  for (let index = 0; index < lines.length; index += 1) {
    if (lines[index].timeMs > progressMs) break;
    currentIndex = index;
  }

  if (currentIndex < 0) {
    return { next: lines[0].text };
  }

  return {
    ...(currentIndex > 0 ? { previous: lines[currentIndex - 1].text } : {}),
    current: lines[currentIndex].text,
    ...(currentIndex + 1 < lines.length ? { next: lines[currentIndex + 1].text } : {}),
  };
}
