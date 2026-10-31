from __future__ import annotations

import bisect
import re
import time
from typing import Any, Mapping

_TIMESTAMP = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def _fraction_ms(value: str | None) -> int:
    if not value:
        return 0
    if len(value) == 1:
        return int(value) * 100
    if len(value) == 2:
        return int(value) * 10
    return int(value[:3])


def parse_lrc(raw: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for source in str(raw or "").splitlines():
        matches = list(_TIMESTAMP.finditer(source))
        if not matches:
            continue
        text = _TIMESTAMP.sub("", source).strip()
        if not text:
            continue
        for match in matches:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            if seconds >= 60:
                continue
            lines.append({"time_ms": minutes * 60_000 + seconds * 1000 + _fraction_ms(match.group(3)), "text": text})
    lines.sort(key=lambda item: int(item["time_ms"]))
    return lines


def current_lyric_window(lines: list[Mapping[str, Any]], progress_ms: int) -> dict[str, str] | None:
    if not lines:
        return None
    times = [int(item.get("time_ms") or item.get("timeMs") or 0) for item in lines]
    index = bisect.bisect_right(times, max(0, int(progress_ms))) - 1
    if index < 0:
        return {"next": str(lines[0].get("text") or "")} if lines[0].get("text") else None
    result: dict[str, str] = {}
    if index > 0 and lines[index - 1].get("text"):
        result["previous"] = str(lines[index - 1]["text"])
    if lines[index].get("text"):
        result["current"] = str(lines[index]["text"])
    if index + 1 < len(lines) and lines[index + 1].get("text"):
        result["next"] = str(lines[index + 1]["text"])
    return result or None


def estimated_progress_ms(snapshot: Mapping[str, Any], at: float | None = None) -> int:
    playback = snapshot.get("playback") or {}
    base = int(playback.get("progress_ms") or 0)
    if str(playback.get("status") or "") != "PLAY":
        return max(0, base)
    observed = float(playback.get("observed_at") or 0)
    if observed > 10_000_000_000:
        observed /= 1000
    elapsed = max(0.0, (at or time.time()) - observed) * 1000 if observed else 0
    duration = int((snapshot.get("current_song") or {}).get("duration_ms") or 0)
    value = int(base + elapsed)
    return min(value, duration) if duration > 0 else value
