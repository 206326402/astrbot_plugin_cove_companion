export function calculateProgress(
  playback: {status?: string; progress_ms?: number; observed_at?: number; stale?: boolean},
  durationMs: number,
  clientNowMs: number,
  serverOffsetMs = 0,
) {
  const base = Math.max(0, Number(playback.progress_ms || 0));
  if (playback.status !== 'PLAY' || playback.stale) return Math.min(base, durationMs || Number.MAX_SAFE_INTEGER);
  const observed = Number(playback.observed_at || clientNowMs + serverOffsetMs);
  const value = base + Math.min(30_000, Math.max(0, clientNowMs + serverOffsetMs - observed));
  return Math.min(value, durationMs || Number.MAX_SAFE_INTEGER);
}

export type TimedLyric = {time_ms?: number; timeMs?: number; text?: string};
export type LyricWindow = {previous?: string; current?: string; next?: string};

export function lyricWindowAtProgress(
  timeline: TimedLyric[] | undefined,
  progressMs: number,
  fallback?: LyricWindow | null,
): LyricWindow | null {
  if (!timeline?.length) return fallback || null;
  const lines = timeline
    .map((line) => ({time_ms: Number(line.time_ms ?? line.timeMs ?? 0), text: String(line.text || '')}))
    .filter((line) => line.text);
  if (!lines.length) return fallback || null;
  let low = 0;
  let high = lines.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (lines[middle].time_ms <= progressMs) low = middle + 1;
    else high = middle;
  }
  const index = low - 1;
  if (index < 0) return {next: lines[0].text};
  return {
    previous: index > 0 ? lines[index - 1].text : undefined,
    current: lines[index].text,
    next: index + 1 < lines.length ? lines[index + 1].text : undefined,
  };
}

export function streamModeLabel(mode: 'connecting' | 'live' | 'polling') {
  return mode === 'live' ? 'SSE 实时同步' : mode === 'polling' ? '降级轮询' : '正在连接';
}
