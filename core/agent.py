from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Mapping

from .config import Settings
from .lyrics import current_lyric_window, estimated_progress_ms, parse_lrc
from .store import CompanionStore
from .text import clean
from .music_model import MusicModel

try:
    from astrbot.core.agent.tool import ToolSet
except Exception:
    class ToolSet:
        def __init__(self, tools=None):
            self.tools = list(tools or [])
        def add_tool(self, tool):
            self.tools.append(tool)
        def merge(self, other):
            self.tools.extend(getattr(other, "tools", []))
        def names(self):
            return [getattr(tool, "name", "") for tool in self.tools]

try:
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.message.components import Plain
    from astrbot.core.message.message_event_result import MessageChain
except Exception:
    class AstrMessageEvent:
        def __init__(self, message_str, message_obj, platform_meta, session_id):
            self.message_str = message_str
            self.message_obj = message_obj
            self.platform_meta = platform_meta
            self.session_id = session_id
            self._extras = {}
            self.is_wake = False
            self.is_at_or_wake_command = False
            self.role = "member"
        def get_platform_name(self):
            return getattr(self.platform_meta, "id", "")
        def get_sender_id(self):
            return getattr(getattr(self.message_obj, "sender", None), "user_id", "")
        def set_extra(self, key, value):
            self._extras[key] = value
        def get_extra(self, key, default=None):
            return self._extras.get(key, default)
        async def send(self, message):
            raise RuntimeError("room agent cannot send QQ messages")

    class AstrBotMessage:
        def __init__(self):
            self.timestamp = int(time.time())

    class MessageMember:
        def __init__(self, user_id, nickname=None):
            self.user_id = user_id
            self.nickname = nickname

    class MessageType:
        FRIEND_MESSAGE = "FriendMessage"

    class PlatformMetadata:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Plain:
        def __init__(self, text):
            self.text = text

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])


CONTROL_TOOLS = {
    "cove_music_enqueue_next",
    "cove_music_play_now",
    "cove_music_skip_current",
    "cove_music_control",
    "cove_music_cancel_recommendation",
    "cove_music_set_mode",
    "cove_music_leave",
    "cove_music_recommend_next",
}
ROOM_TOOLS = {"cove_music"}
AUTODJ_TOOLS = {"cove_music"}
PROACTIVE_TOOLS = {"cove_music"}
MEMORY_TOOL = "recall_long_term_memory"
SUCCESS_CLAIM = re.compile(
    r"(已切|已经切|切好|换好|已换|已暂停|暂停了|已继续|继续播放|已加|加好|排好|已排队|已经在放|给你放|放上了|已退出|已离开)",
    re.I,
)
SYSTEM_REMINDER = re.compile(r"<system_reminder>.*?</system_reminder>", re.I | re.S)
QQ_METADATA = re.compile(r"<qq_verified_components\b[^>]*?/\s*>", re.I | re.S)


def has_confirmed_control(tool_calls: list[Mapping[str, Any]]) -> bool:
    return any(item.get("state") == "confirmed" and (item.get("name") in CONTROL_TOOLS or (item.get("name") == "cove_music" and (item.get("args") or {}).get("action") in {"select", "pause", "resume", "next", "leave", "autodj"})) for item in tool_calls)


def agent_final_state(tool_calls: list[Mapping[str, Any]]) -> str:
    """Only a verified control call makes a room turn operationally confirmed."""
    return "confirmed" if has_confirmed_control(tool_calls) else "completed"


OPERATION_LABELS = {
    "PLAY_NOW": "立即播放",
    "ENQUEUE_NEXT": "排下一首",
    "NEXT": "跳过当前",
    "PAUSE": "暂停",
    "RESUME": "继续",
    "CANCEL_QUEUED_RECOMMENDATION": "取消待播推荐",
    "LEAVE": "退出房间",
}


def format_recent_operations(operations: list[Mapping[str, Any]]) -> str:
    lines = []
    for item in operations:
        timestamp = float(item.get("updated_at") or item.get("created_at") or 0)
        time_text = time.strftime("%H:%M:%S", time.localtime(timestamp)) if timestamp else "未知时间"
        action = str(item.get("action") or "UNKNOWN")
        label = OPERATION_LABELS.get(action, action)
        song_name = str(item.get("target_song_name") or "")
        artist = str(item.get("artist") or "")
        target = "《" + song_name + "》" if song_name else "无歌曲目标"
        if artist:
            target += " - " + artist
        elif item.get("target_song_id"):
            target += "（ID " + str(item["target_song_id"]) + "）"
        evidence = ""
        if item.get("snapshot_verified") is not None:
            evidence += "；快照复核=" + ("通过" if item.get("snapshot_verified") else "未通过")
        if item.get("play_status"):
            evidence += "；播放状态=" + str(item["play_status"])
        lines.append(
            f"- {time_text} {label}({action}) {target}；状态={item.get('state') or 'unknown'}{evidence}；"
            f"操作={item.get('operation_id') or '无'}；来源={item.get('origin_kind') or 'unknown'}"
        )
    return "\n".join(lines)


def _claimed_intent(text: str, tool_calls: list[Mapping[str, Any]]) -> str:
    names = {str(item.get("name") or "") for item in tool_calls}
    if "cove_music_play_now" in names or any(word in text for word in ("切歌", "换歌", "换首", "换好", "已换", "切好", "立刻播放")):
        return "立即播放歌曲"
    if names & {"cove_music_enqueue_next", "cove_music_recommend_next"} or any(word in text for word in ("排队", "下一首", "加歌")):
        return "排队歌曲"
    if names == {"cove_music_control"}:
        return "暂停或继续播放"
    if "cove_music_skip_current" in names:
        return "跳过当前歌曲"
    if "cove_music_leave" in names:
        return "退出一起听"
    return "音乐控制"


def is_current_control_request(prompt: str | None) -> bool:
    if prompt is None:
        return True
    value = clean(prompt, 4000)
    if re.search(r"(最近|刚才|之前|上次|历史|切过|排过|操作回执|做过什么)", value):
        return False
    return bool(re.search(r"(换歌|换首|切歌|点歌|播放|暂停|继续|下一首|排队|退出|取消|想听|来一首)", value))


def sanitize_room_reply(
    text: str,
    tool_calls: list[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
    *,
    prompt: str | None = None,
) -> str:
    value = clean(text, 4000)
    if not value or has_confirmed_control(tool_calls):
        return value
    if not is_current_control_request(prompt):
        return value
    if not SUCCESS_CLAIM.search(value):
        return value
    song = snapshot.get("current_song") or {}
    intent = _claimed_intent(value, tool_calls)
    return (
        f"这次没有拿到网易云的 confirmed 回执，所以我没有执行或无法确认：{intent}。"
        f"当前状态：{song.get('name') or '未知歌曲'} / {(snapshot.get('playback') or {}).get('status') or 'UNKNOWN'}。"
    )


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                item_type = str(item.get("type") or "")
                if item_type in {"think", "tool", "tool_call", "tool_result", "image"}:
                    continue
                if item_type in {"", "text"} and isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return "\n".join(parts)
    if isinstance(value, Mapping):
        if str(value.get("type") or "") in {"think", "tool", "tool_call", "tool_result", "image"}:
            return ""
        return str(value.get("text") or value.get("content") or "")
    return str(value or "")


def _history_text(value: Any) -> str:
    text = _text_content(value)
    text = SYSTEM_REMINDER.sub("", text)
    text = QQ_METADATA.sub("", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(line.lower().startswith(("[cron]", "定时任务汇报", "工具调用结果")) for line in lines):
        return ""
    return clean("\n".join(lines), 4000)


def _filter_conversation_history(history: Any, turns: int) -> list[dict[str, str]]:
    if isinstance(history, str):
        try:
            records = json.loads(history)
        except ValueError:
            records = []
    elif isinstance(history, list):
        records = history
    else:
        records = []
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for record in records:
        if not isinstance(record, Mapping):
            continue
        role = str(record.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        if record.get("tool_calls"):
            continue
        text = _history_text(record.get("content"))
        if not text:
            continue
        if role == "user":
            current = {"user": text, "assistant": ""}
            groups.append(current)
        elif current is not None:
            current["assistant"] = text
    selected = groups[-max(0, turns):] if turns else []
    contexts: list[dict[str, str]] = []
    for group in selected:
        if group.get("user"):
            contexts.append({"role": "user", "content": group["user"]})
        if group.get("assistant"):
            contexts.append({"role": "assistant", "content": group["assistant"]})
    return contexts


def _room_history(store: CompanionStore, session_id: str | None, limit: int, exclude_ids: set[str] | None = None) -> list[dict[str, str]]:
    exclude = exclude_ids or set()
    result: list[dict[str, str]] = []
    for item in reversed(store.room_messages(limit * 2, session_id=session_id)):
        provider_id = str(item.get("provider_message_id") or "")
        if provider_id and provider_id in exclude:
            continue
        text = clean(item.get("text"), 1000)
        if not text:
            continue
        role = "assistant" if item.get("direction") == "outbound" else "user"
        result.append({"role": role, "content": text})
        if len(result) >= limit:
            break
    result.reverse()
    return result


def _byte_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _get_role(context: Mapping[str, Any]) -> str:
    return str(context.get("role") or "")


class CoveRoomEvent(AstrMessageEvent):
    def __init__(self, *, owner_umo: str, text: str, turn_id: str, kind: str):
        platform_id, message_type, session_id = owner_umo.split(":", 2)
        message = AstrBotMessage()
        message.type = getattr(MessageType, "FRIEND_MESSAGE", "FriendMessage")
        message.self_id = "cove-room"
        message.session_id = session_id
        message.message_id = "cove-" + turn_id
        message.group = None
        message.sender = MessageMember(session_id, "")
        message.message = [Plain(text)]
        message.message_str = text
        message.raw_message = text
        message.timestamp = int(time.time())
        platform_meta = PlatformMetadata(
            name=platform_id,
            id=platform_id,
            description="Cove room agent context",
            support_streaming_message=False,
            support_proactive_message=False,
        )
        super().__init__(text, message, platform_meta, session_id)
        self.is_wake = True
        self.is_at_or_wake_command = True
        self.role = "admin"
        self.set_extra("cove_room_agent", True)
        self.set_extra("cove_room_turn_id", turn_id)
        self.set_extra("cove_room_kind", kind)
        self.set_extra("cove_owner_umo", owner_umo)

    async def send(self, message: MessageChain) -> None:
        raise RuntimeError("room agent cannot send QQ messages; final text is routed by the plugin")


class CoveMusicAgent(MusicModel):
    def __init__(self, *, context: Any, config: Any, settings: Settings, store: CompanionStore, owner_umo: str, logger: Any):
        self.context = context
        self.config = config
        self.settings = settings
        self.store = store
        self.owner_umo = owner_umo
        self.logger = logger
        self.engine: Any = None
        self.tool_builder: Any = None
        self._lock = asyncio.Lock()
        self._turns: dict[str, dict[str, Any]] = {}
        self._jobs: asyncio.PriorityQueue[tuple[int, int, dict[str, Any]]] = asyncio.PriorityQueue()
        self._job_seq = 0
        self._worker_task: asyncio.Task[Any] | None = None
        self._worker_stop = asyncio.Event()
        self._active_job: dict[str, Any] | None = None
        self._active_task: asyncio.Task[Any] | None = None
        self._active_turn_id: str | None = None

    def attach_engine(self, engine: Any) -> None:
        self.engine = engine

    def _log(self, level: str, message: str, *args: Any) -> None:
        try:
            getattr(self.logger, level)("[cove-agent] " + message, *args)
        except Exception:
            pass

    def _priority(self, kind: str) -> int:
        return {"room_reply": 0, "proactive": 1, "autodj": 2, "selftest": 3}.get(kind, 3)

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_stop.clear()
            self._worker_task = asyncio.create_task(self._worker_loop(), name="cove-agent-priority-worker")

    async def interrupt_low_priority(self) -> bool:
        if not self._active_job or not self._active_task or self._active_task.done():
            return False
        if int(self._active_job.get("priority") or 0) <= 0:
            return False
        turn = self._turns.get(str(self._active_turn_id or ""))
        if turn and turn.get("tool_calls"):
            return False
        self._active_task.cancel()
        return True

    async def _enqueue(self, *, kind: str, runner: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        self._ensure_worker()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._job_seq += 1
        job = {
            "priority": self._priority(kind),
            "sequence": self._job_seq,
            "kind": kind,
            "runner": runner,
            "future": future,
        }
        await self._jobs.put((job["priority"], job["sequence"], job))
        if job["priority"] == 0:
            await self.interrupt_low_priority()
        return await future

    async def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            _, _, job = await self._jobs.get()
            self._active_job = job
            task = asyncio.create_task(job["runner"](), name=f"cove-agent-{job['kind']}")
            self._active_task = task
            try:
                result = await task
            except asyncio.CancelledError:
                if self._worker_stop.is_set():
                    raise
                result = {"state": "cancelled", "text": "", "error": "preempted_by_higher_priority"}
            except Exception as exc:
                result = {"state": "failed", "text": "", "error": type(exc).__name__ + ": " + clean(exc, 300)}
            finally:
                self._active_job = None
                self._active_task = None
            if not job["future"].done():
                job["future"].set_result(result)
            self._jobs.task_done()

    async def stop(self) -> None:
        self._worker_stop.set()
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
        tasks = [task for task in (self._active_task, self._worker_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def provider_id(self) -> str:
        if self.settings.provider_id:
            return self.settings.provider_id
        getter = getattr(self.context, "get_current_chat_provider_id", None)
        if not callable(getter):
            return ""
        value = getter(umo=self.owner_umo)
        if asyncio.iscoroutine(value):
            value = await value
        return str(value or "")

    def _provider_settings(self) -> dict[str, Any]:
        try:
            value = self.context.get_config(umo=self.owner_umo).get("provider_settings", {})
            return dict(value) if isinstance(value, Mapping) else {}
        except Exception:
            return {}

    async def provider_candidates(self, *, include_configured: bool = True) -> list[str]:
        primary = ""
        try:
            primary = await self.provider_id()
        except Exception:
            primary = ""
        provider_settings = self._provider_settings()
        fallback_ids: list[str] = []
        if self.settings.fallback_provider_id:
            fallback_ids.append(self.settings.fallback_provider_id)
        configured = provider_settings.get("fallback_chat_models") or []
        if include_configured and isinstance(configured, (list, tuple)):
            fallback_ids.extend(str(item).strip() for item in configured if str(item).strip())
        candidates = [primary] * (1 + max(0, self.settings.provider_retry_count)) if primary else []
        seen = {primary}
        for fallback_id in fallback_ids:
            if fallback_id in seen:
                continue
            seen.add(fallback_id)
            candidates.append(fallback_id)
        return candidates

    async def _conversation(self) -> Any:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return None
        try:
            cid = await manager.get_curr_conversation_id(self.owner_umo)
            if not cid:
                return None
            return await manager.get_conversation(self.owner_umo, cid)
        except Exception:
            return None

    async def _persona(self, conversation: Any) -> tuple[str | None, str, list[dict[str, Any]]]:
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return None, "", []
        platform_name = self.owner_umo.split(":", 1)[0]
        provider_settings = {}
        try:
            provider_settings = self.context.get_config(umo=self.owner_umo).get("provider_settings", {})
        except Exception:
            pass
        selected_id, persona, _, _ = await manager.resolve_selected_persona(
            umo=self.owner_umo,
            conversation_persona_id=getattr(conversation, "persona_id", None),
            platform_name=platform_name,
            provider_settings=provider_settings,
        )
        if isinstance(persona, Mapping):
            prompt = clean(persona.get("prompt"), 64_000)
            dialogs = persona.get("_begin_dialogs_processed") or []
            return selected_id, prompt, list(dialogs) if isinstance(dialogs, list) else []
        return selected_id, "", []

    async def current_persona_id(self) -> str | None:
        conversation = await self._conversation()
        persona_id, _, _ = await self._persona(conversation)
        return persona_id

    async def music_style_terms(self):
        """Use only named music interests in the selected persona, never stereotypes."""
        conversation=await self._conversation()
        _,prompt,_=await self._persona(conversation)
        vocabulary=('爵士','古典','摇滚','电子','器乐','钢琴','民谣','流行','嘻哈','游戏原声','动漫原声','R&B','jazz','ambient','lofi')
        terms=[]
        for sentence in re.split(r'[。\n；!！]',prompt):
            if any(k in sentence for k in ('喜欢','爱听','偏爱','音乐口味','音乐偏好')) and not any(k in sentence for k in ('不喜欢','讨厌','不爱','不要')):
                terms.extend(t for t in vocabulary if t.casefold() in sentence.casefold())
        return list(dict.fromkeys(terms))[:10]

    async def _lyric_window(self, snapshot: Mapping[str, Any]) -> dict[str, str] | None:
        song = snapshot.get("current_song") or {}
        song_id = str(song.get("id") or "")
        if not song_id.isdigit():
            return None
        cache = self._turns.setdefault("__lyrics__", {})
        item = cache.get(song_id)
        if not item or time.time() - item["at"] > 600:
            try:
                data = await self.engine.lyrics(song_id)
                lines = ((data.get("lrc") or {}).get("lines") or [])
                if not lines and (data.get("lrc") or {}).get("raw"):
                    lines = parse_lrc(data["lrc"]["raw"])
                item = {"at": time.time(), "lines": lines}
                cache[song_id] = item
            except Exception:
                return None
        return current_lyric_window(item.get("lines") or [], estimated_progress_ms(snapshot))

    def _select_tools(self, kind: str) -> ToolSet:
        tools = ToolSet()
        if callable(self.tool_builder):
            tools.merge(self.tool_builder(kind))
        manager = self.context.get_llm_tool_manager()
        memory_tool = manager.get_func(MEMORY_TOOL)
        if memory_tool is not None and getattr(memory_tool, "active", True):
            tools.add_tool(memory_tool)
        return tools

    def _reserve(self, kind: str, *, followup: bool = False, manual: bool = False) -> str | None:
        # Keep request auditing; only the token ledger enforces business quotas.
        call_id, _ = self.store.reserve_model_call(
            kind,
            None,
            None,
            None,
        )
        return call_id

    def reserve_followup(self, turn_id: str) -> None:
        turn = self._turns.get(turn_id)
        if not turn:
            raise RuntimeError("agent_turn_missing")
        # Tool execution is not a model request. The request proxy meters
        # actual provider continuations, including parallel tool results.
        return

    def finish_pending_calls(self, turn: Mapping[str, Any], state: str, error: str | None = None) -> None:
        elapsed = int((time.monotonic() - float(turn.get("started") or time.monotonic())) * 1000)
        pending = list(turn.get("pending_call_ids") or [])
        for call_id in pending:
            self.store.finish_model_call(str(call_id), state, elapsed, error)
        if isinstance(turn, dict):
            turn["pending_call_ids"] = []

    def record_tool_result(self, turn_id: str, name: str, args: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        turn = self._turns.get(turn_id)
        if not turn:
            return
        item = {
            "name": name,
            "args": dict(args),
            "state": str(result.get("state") or "unknown"),
            "result": dict(result),
            "at": time.time(),
        }
        turn["tool_calls"].append(item)
        operation_id = result.get("operation_id")
        if operation_id:
            turn["operation_ids"].append(str(operation_id))

    def active_turn(self, turn_id: str) -> dict[str, Any] | None:
        return self._turns.get(turn_id)

    def _system_prompt(self, persona: str, kind: str, snapshot: Mapping[str, Any], lyric: Mapping[str, str] | None, recent_operations: list[Mapping[str, Any]] | None = None) -> str:
        current = snapshot.get("current_song") or {}
        playback = snapshot.get("playback") or {}
        room_rules = [
            "# 网易云一起听房间",
            "你正在网易云一起听房间里陪已验证的私聊用户听歌；称呼和关系完全沿用当前人格，不得把权限角色当成称谓。",
            "这是房间场景，不是 QQ 私聊；不得发送 QQ 消息，不得写长期记忆、改备注、改人格或操作定时任务。",
            "只有专用工具返回 state=confirmed 才能说自己完成了切歌、暂停、继续、排队或退出。",
            "如果工具返回 rejected 或 unknown，必须如实说明未确认。",
            f"当前歌曲：{current.get('name') or '未知'} - {current.get('artist') or '未知'}",
            f"当前播放：{playback.get('status') or 'UNKNOWN'}，进度约 {playback.get('progress_ms') or 0} ms",
        ]
        queue_ids = list((snapshot.get("queue") or {}).get("song_ids") or [])
        if queue_ids:
            room_rules.append("当前队列：" + " -> ".join(str(item) for item in queue_ids[:12]))
        if recent_operations:
            room_rules.append("最近操作回执（这是历史事实，不代表当前仍在播放）：\n" + format_recent_operations(recent_operations))
            room_rules.append(
                "最近操作回执来自数据库，是最高优先级的操作事实；旧聊天、房间摘要或你以前的猜测都不能覆盖它。"
                "state=confirmed 且快照复核=通过时，必须承认该操作已确认生效；不得改口说没执行或没有回执。"
                "ENQUEUE_NEXT 只表示已排下一首；PLAY_NOW 才表示已切到目标歌曲并确认播放。"
                "如果最近存在 confirmed 的播放操作，而当前歌曲已被后续变化覆盖，"
                "必须表述为“我操作过某首，后来房间又变了”，不得推断此前操作没成功。"
                "只有 rejected/unknown 才能说未确认。"
            )
        if lyric:
            room_rules.append("当前歌词：" + " / ".join(filter(None, [lyric.get("previous"), lyric.get("current"), lyric.get("next")])))
        if kind == "autodj":
            room_rules.extend([
                "这是自动选歌回合。使用 cove_music(action=search) 主动搜索，最多三次。",
                "只能从搜索结果或本轮候选中选择，并调用 cove_music(action=select) 并携带候选 batch_id/song_id；不要自动 GOTO 或 NEXT。",
                "没有合适歌曲就不调用推荐工具，安静跳过。",
            ])
        elif kind == "proactive":
            room_rules.extend([
                "这是歌曲变更后的主动表达窗口。你可以说一句自然、简短的话，也可以只返回 [SKIP]。",
                "不要为每次切歌强行找话；没有真实感受或上下文时选择跳过。",
            ])
        else:
            room_rules.extend([
                "回应用户真实房间消息，称呼依照当前人格。明确控制优先调用工具，不要只口头答应。",
                "普通回复保持当前人格，简短自然，不超过 240 个中文字符。",
                "如果搜索不到准确歌曲，如实告诉用户没有找到，不编造 ID，不随意选一首不相关的歌。",
            ])
        return (persona + "\n\n" + "\n".join(room_rules)).strip()

    async def _run(
        self,
        *,
        kind: str,
        trigger: str,
        session: Mapping[str, Any],
        prompt: str,
        current_batch_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        return await self._enqueue(
            kind=kind,
            runner=lambda: self._run_serialized(
                kind=kind,
                trigger=trigger,
                session=session,
                prompt=prompt,
                current_batch_ids=current_batch_ids,
            ),
        )

    async def _run_serialized(
        self,
        *,
        kind: str,
        trigger: str,
        session: Mapping[str, Any],
        prompt: str,
        current_batch_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            providers = await self.provider_candidates(include_configured=kind != "autodj")
            if not providers:
                return {"state": "failed", "error": "provider_missing", "text": ""}
            provider = providers[0]
            conversation = await self._conversation()
            persona_id, persona, begin_dialogs = await self._persona(conversation)
            if not persona:
                return {"state": "failed", "error": "persona_missing", "text": ""}
            self.store.update_session(str(session.get("id") or ""), persona_id=persona_id)
            snapshot = await self.engine.snapshot(True)
            lyric = await self._lyric_window(snapshot)
            qq_contexts = _filter_conversation_history(getattr(conversation, "history", ""), self.settings.qq_history_turns)
            room_contexts = _room_history(self.store, session.get("id"), self.settings.room_history_messages, current_batch_ids)
            summary_contexts = [{"role": "assistant", "content": "[一起听摘要] " + str(item.get("summary") or "")} for item in reversed(self.store.room_summaries(3, session_id=str(session.get("id") or "")))]
            recent_operations = self.store.recent_operations_context(session_id=str(session.get("id") or ""), limit=3)
            operation_contexts = []
            if recent_operations:
                operation_contexts.append({
                    "role": "assistant",
                    "content": "[最近操作回执｜数据库事实，优先于旧聊天]\n" + format_recent_operations(recent_operations),
                })
            context_items: list[tuple[str, dict[str, str]]] = []
            context_items.extend(("persona_dialog", dict(item)) for item in begin_dialogs if isinstance(item, Mapping))
            context_items.extend(("qq", dict(item)) for item in qq_contexts)
            context_items.extend(("room_summary", dict(item)) for item in summary_contexts)
            context_items.extend(("operation_receipt", dict(item)) for item in operation_contexts)
            context_items.extend(("room", dict(item)) for item in room_contexts)
            system = self._system_prompt(persona, kind, snapshot, lyric, recent_operations)
            budget = self.settings.agent_context_bytes
            prompt_context = {"role": "user", "content": prompt}
            while context_items and len(system.encode("utf-8")) + _byte_size([item[1] for item in context_items] + [prompt_context]) > budget:
                removed = False
                for category in ("qq", "room", "room_summary", "persona_dialog", "operation_receipt"):
                    index = next((i for i, (item_category, _) in enumerate(context_items) if item_category == category), None)
                    if index is not None:
                        context_items.pop(index)
                        removed = True
                        break
                if not removed:
                    break
            contexts = [item[1] for item in context_items]
            context_meta = {
                "qq_turns": sum(1 for role, _ in context_items if role == "qq" and _get_role(_) == "user"),
                "qq_messages": sum(1 for role, _ in context_items if role == "qq"),
                "room_messages": sum(1 for role, _ in context_items if role == "room"),
                "room_summaries": sum(1 for role, _ in context_items if role == "room_summary"),
                "operation_receipts": sum(1 for role, _ in context_items if role == "operation_receipt"),
                "persona_dialogs": sum(1 for role, _ in context_items if role == "persona_dialog"),
                "recent_operations": len(recent_operations),
                "trimmed": len(begin_dialogs) + len(qq_contexts) + len(summary_contexts) + len(operation_contexts) + len(room_contexts) - len(context_items),
            }
            context_bytes = len(system.encode("utf-8")) + _byte_size(contexts + [prompt_context])
            if len(system.encode("utf-8")) >= budget:
                return {"state": "failed", "error": "persona_too_large", "text": ""}
            input_payload = {
                "prompt": prompt,
                "song": snapshot.get("current_song"),
                "lyric": lyric,
                "context": context_meta,
                "recent_operations": recent_operations,
            }
            turn = self.store.start_agent_turn(
                kind=kind,
                trigger=trigger,
                session_id=str(session.get("id") or ""),
                input_payload=input_payload,
                song_id=str((snapshot.get("current_song") or {}).get("id") or ""),
                persona_id=persona_id,
                provider_id=provider,
                context_bytes=context_bytes,
            )
            call_id = self._reserve(kind, manual=trigger == "manual_recommendation")
            if not call_id:
                self.store.finish_agent_turn(turn["id"], state="budget_exhausted", error="model_budget_exhausted")
                return {"state": "budget_exhausted", "error": "model_budget_exhausted", "text": "", "turn_id": turn["id"]}
            state = {
                "id": turn["id"],
                "kind": kind,
                "trigger": trigger,
                "session": dict(session),
                "snapshot": snapshot,
                "persona": persona,
                "provider": provider,
                "persona_id": persona_id,
                "call_ids": [call_id],
                "pending_call_ids": [call_id],
                "provider_attempts": [],
                "input_payload": input_payload,
                "tool_calls": [],
                "operation_ids": [],
                "search_count": 0,
                "started": time.monotonic(),
                "decision_revision": int(session.get("decision_revision") or 0),
            }
            self._turns[turn["id"]] = state
            self._active_turn_id = turn["id"]
            event = CoveRoomEvent(owner_umo=self.owner_umo, text=prompt, turn_id=turn["id"], kind=kind)
            event.set_extra("cove_room_id", session.get("room_id"))
            event.set_extra("cove_room_session_id", session.get("id"))
            try:
                tools = self._select_tools(kind)
                deadline = time.monotonic() + self.settings.agent_timeout_seconds
                successful_result = None
                last_detail = "model_provider_failed"
                for attempt_index, attempt_provider in enumerate(providers):
                    if attempt_index:
                        if state["tool_calls"] or time.monotonic() >= deadline - 0.25:
                            break
                        retry_call_id = self._reserve(kind, followup=True, manual=trigger == "manual_recommendation")
                        if not retry_call_id:
                            last_detail = "model_budget_exhausted"
                            break
                        state["call_ids"].append(retry_call_id)
                        state["pending_call_ids"].append(retry_call_id)
                    attempt = {
                        "attempt": attempt_index + 1,
                        "provider_id": attempt_provider,
                        "state": "running",
                    }
                    state["provider"] = attempt_provider
                    state["provider_attempts"].append(attempt)
                    try:
                        remaining = max(0.25, deadline - time.monotonic())
                        if hasattr(self.context, 'provider_manager'):
                            from .metering import tool_loop
                            runner = tool_loop(self,event,attempt_provider,prompt,contexts,tools,system,self.settings.agent_max_steps if kind=='room_reply' else 5)
                        else:
                            runner = self.context.tool_loop_agent(
                            event=event,
                            chat_provider_id=attempt_provider,
                            prompt=prompt,
                            contexts=contexts,
                            tools=tools,
                            system_prompt=system,
                            max_steps=self.settings.agent_max_steps if kind == "room_reply" else 5,
                            tool_call_timeout=15,
                            stream=False,
                            )
                        result = await asyncio.wait_for(runner, timeout=remaining)
                        text = clean(getattr(result, "completion_text", ""), 4000)
                        if getattr(result, "role", None) == "err" or text.startswith("All chat models failed"):
                            raise RuntimeError("model_provider_failed: " + text[:240])
                        provider = attempt_provider
                        state["provider"] = provider
                        attempt["state"] = "success"
                        successful_result = result
                        self.finish_pending_calls(state, "success")
                        break
                    except Exception as exc:
                        detail = type(exc).__name__ + ": " + clean(exc, 300)
                        last_detail = f"provider_attempts_exhausted: {detail}"
                        attempt["state"] = "failed"
                        attempt["error"] = detail
                        self.finish_pending_calls(state, "failed", detail)
                        self._log("warning", "provider attempt failed: %s/%s %s", attempt_index + 1, attempt_provider, type(exc).__name__)
                if successful_result is None:
                    state["input_payload"]["provider_attempts"] = state["provider_attempts"]
                    self.store.finish_agent_turn(
                        turn["id"],
                        state="failed",
                        error=last_detail,
                        tool_calls=state["tool_calls"],
                        operation_ids=state["operation_ids"],
                        input_payload=state["input_payload"],
                        provider_id=state["provider"],
                    )
                    return {"state": "failed", "error": last_detail, "text": "", "turn_id": turn["id"]}
                confirmed = has_confirmed_control(state["tool_calls"])
                if kind == "room_reply":
                    text = sanitize_room_reply(text, state["tool_calls"], snapshot, prompt=prompt)
                if kind == "proactive" and text.strip() in {"[SKIP]", "SKIP", "跳过"}:
                    text = ""
                final_state = agent_final_state(state["tool_calls"])
                state["input_payload"]["provider_attempts"] = state["provider_attempts"]
                self.store.finish_agent_turn(
                    turn["id"],
                    state=final_state,
                    output_text=text,
                    tool_calls=state["tool_calls"],
                    operation_ids=state["operation_ids"],
                    input_payload=state["input_payload"],
                    provider_id=provider,
                )
                return {
                    "state": final_state,
                    "text": text,
                    "turn_id": turn["id"],
                    "tool_calls": state["tool_calls"],
                    "operation_ids": state["operation_ids"],
                    "confirmed": confirmed,
                    "persona_id": persona_id,
                    "provider_id": provider,
                    "provider_attempts": state["provider_attempts"],
                }
            except asyncio.CancelledError:
                detail = "preempted_by_higher_priority"
                self.finish_pending_calls(state, "failed", detail)
                state["input_payload"]["provider_attempts"] = state["provider_attempts"]
                self.store.finish_agent_turn(
                    turn["id"],
                    state="cancelled",
                    error=detail,
                    tool_calls=state["tool_calls"],
                    operation_ids=state["operation_ids"],
                    input_payload=state["input_payload"],
                    provider_id=state["provider"],
                )
                raise
            except Exception as exc:
                detail = type(exc).__name__ + ": " + clean(exc, 300)
                self.finish_pending_calls(state, "failed", detail)
                state["input_payload"]["provider_attempts"] = state["provider_attempts"]
                self.store.finish_agent_turn(
                    turn["id"],
                    state="failed",
                    error=detail,
                    tool_calls=state["tool_calls"],
                    operation_ids=state["operation_ids"],
                    input_payload=state["input_payload"],
                    provider_id=state["provider"],
                )
                self._log("warning", "agent turn failed: %s", type(exc).__name__)
                return {"state": "failed", "error": detail, "text": "", "turn_id": turn["id"]}
            finally:
                self._turns.pop(turn["id"], None)
                if self._active_turn_id == turn["id"]:
                    self._active_turn_id = None

    async def run_room_turn(self, *, session: Mapping[str, Any], messages: list[Mapping[str, Any]]) -> dict[str, Any]:
        lines = []
        ids: set[str] = set()
        for item in messages:
            text = clean(item.get("text"), 1000)
            if not text:
                continue
            lines.append(text)
            for key in ("event_id", "message_id"):
                value = str(item.get(key) or "")
                if value:
                    ids.add(value)
        prompt = "已验证的用户刚刚在网易云房间里说（称呼依照人格）：\n" + "\n".join(lines)
        return await self._run(kind="room_reply", trigger="room_message", session=session, prompt=prompt, current_batch_ids=ids)

    async def run_autodj(self, *, session: Mapping[str, Any], manual: bool = False) -> dict[str, Any]:
        prompt = "推荐窗口到了。请根据偏好、近期聊天和播放历史，主动搜索并推荐一首适合现在听的歌；没有合适的就跳过。"
        result = await self._run(kind="autodj", trigger="manual_recommendation" if manual else "scheduled_recommendation", session=session, prompt=prompt)
        result["manual"] = manual
        return result

    async def run_selftest(self, *, session: Mapping[str, Any]) -> dict[str, Any]:
        return await self._run(
            kind="selftest",
            trigger="readonly_selftest",
            session=session,
            prompt="主 Agent 只读自检：请只回复 OK，不要调用任何工具或发送消息。",
        )

    async def run_proactive(self, *, session: Mapping[str, Any], song: Mapping[str, Any], lyric: Mapping[str, str] | None) -> dict[str, Any]:
        prompt = (
            f"歌曲刚切换到《{song.get('name') or '未知'}》- {song.get('artist') or '未知'}。"
            "这是主动表达窗口。如果你想自然地说一句就回复一句短话；否则只输出 [SKIP]。"
        )
        return await self._run(kind="proactive", trigger="song_changed", session=session, prompt=prompt)

