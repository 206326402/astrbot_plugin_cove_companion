from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

ROOM_ID = re.compile(r"(?:roomId|room_id|roomid)[\"'\s:=]+([A-Za-z0-9_-]{6,200})", re.I)
INVITER_ID = re.compile(r"(?:inviterId|inviter_id|inviterUid|inviter_uid|uid)[\"'\s:=]+(\d{4,24})", re.I)
BARE_INVITE = re.compile(r"/?(?:一起听|listen.?together).*?(?:roomId|room_id)[\"'\s:=]+([A-Za-z0-9_-]{6,200})", re.I)


def clean(value: Any, limit: int = 8000) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def compact(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", clean(value, limit)).strip()


def walk_json(value: Any, depth: int = 0):
    if depth > 8:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk_json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from walk_json(item, depth + 1)


def parse_json_text(value: str) -> Any:
    text = value.strip()
    if not text.startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def extract_invite(*values: Any) -> dict[str, str] | None:
    for value in values:
        if value is None:
            continue
        parsed = parse_json_text(value) if isinstance(value, str) else value
        for record in walk_json(parsed):
            room_id = next((str(record.get(key)).strip() for key in ("roomId", "room_id", "roomid", "roomID") if isinstance(record.get(key), str) and re.fullmatch(r"[A-Za-z0-9_-]{6,200}", record.get(key).strip())), "")
            inviter_id = next((str(record.get(key)).strip() for key in ("inviterId", "inviter_id", "inviterUid", "inviter_uid", "uid") if isinstance(record.get(key), (str, int)) and re.fullmatch(r"\d{4,24}", str(record.get(key)).strip())), "")
            if room_id and inviter_id:
                return {"room_id": room_id, "inviter_id": inviter_id}
        text = value if isinstance(value, str) else ""
        room_match = ROOM_ID.search(text) or BARE_INVITE.search(text)
        inviter_match = INVITER_ID.search(text)
        if room_match and inviter_match:
            return {"room_id": room_match.group(1), "inviter_id": inviter_match.group(1)}
    return None


def parse_duration_minutes(text: str) -> int | None:
    match = re.search(r"(\d{1,3})\s*(?:分钟|分|min)", text, re.I)
    if match:
        return max(1, min(720, int(match.group(1))))
    match = re.search(r"(\d{1,2})\s*(?:小时|h)", text, re.I)
    if match:
        return max(1, min(720, int(match.group(1)) * 60))
    return None


def normalize_command(text: str) -> str:
    value = compact(text, 500)
    value = re.sub(r"^[/／]\s*", "", value)
    value = re.sub(r"^(?:网易云陪听|一起听|陪听|网易云)\s*", "", value, count=1)
    return value.strip()


def parse_music_command(text: str, *, room: bool = False) -> dict[str, Any] | None:
    raw = compact(text, 500)
    command = normalize_command(raw).rstrip("。！!？?~～ ")
    command = re.sub(r"^(?:请|帮我)\s*", "", command)
    if not command:
        return None
    lower = command.lower()

    if command in {"诊断", "排查"}:
        return {"kind": "diagnose"}
    if command in {"状态", "现在听什么", "在听什么"}:
        return {"kind": "status"}
    if command in {"停止推荐", "别推歌", "先别推歌", "先别推歌了", "不要推歌", "停止自动"}:
        return {"kind": "autodj", "enabled": False}
    if command in {"恢复推荐", "继续推荐", "自动推荐开", "开启推荐"}:
        return {"kind": "autodj", "enabled": True}
    if re.fullmatch(r"(?:自动|推荐|自动推荐)\s*(?:开|关|开启|关闭)", command):
        return {"kind": "autodj", "enabled": bool(re.search(r"(?:开|启用|打开)", command))}
    if re.fullmatch(r"(?:聊天|回应)\s*(?:开|关|开启|关闭)", command):
        return {"kind": "room_chat", "enabled": bool(re.search(r"(?:开|启用|打开)", command))}
    if command in {"推荐一首", "推一首", "来一首"}:
        return {"kind": "recommend_once"}
    if command in {"取消待播推荐", "取消推荐", "别放推荐"}:
        return {"kind": "cancel_recommendation"}
    if command in {"退出", "结束陪听", "不听了", "离开一起听"}:
        return {"kind": "leave"}
    if lower in {"暂停", "先暂停", "暂停播放", "pause"}:
        return {"kind": "pause"}
    if lower in {"继续", "继续播放", "resume"}:
        return {"kind": "resume"}
    if lower in {"下一首", "下首", "切歌", "next"}:
        return {"kind": "next"}
    if command.startswith(("搜索", "搜一下", "找歌")):
        query = re.sub(r"^(?:搜索|搜一下|找歌)\s*", "", command).strip()
        if query:
            return {"kind": "search", "query": query}
    match = re.match(r"(?:下首|下一首|排队|排下一首)\s*(\d{5,24})$", command, re.I)
    if match:
        return {"kind": "enqueue", "song_id": match.group(1)}
    match = re.match(r"(?:立刻播放|马上播放|直接播放|立即播放)\s*(\d{5,24})$", command, re.I)
    if match:
        return {"kind": "play_now", "song_id": match.group(1)}
    duration = parse_duration_minutes(command)
    if duration and re.fullmatch(r"(?:陪我听|一起听|听)\s*\d{1,3}\s*(?:分钟|分|min|小时|h)", command, re.I):
        return {"kind": "duration", "minutes": duration}
    if room and lower in {"在吗", "你听吗", "好听吗"}:
        return None
    return None


def age_seconds(timestamp: Any) -> float | None:
    if timestamp is None:
        return None
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return None
    if value > 10_000_000_000:
        value /= 1000
    return max(0.0, datetime.now(timezone.utc).timestamp() - value)
