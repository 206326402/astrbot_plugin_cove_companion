from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from starlette.responses import StreamingResponse
except Exception:
    StreamingResponse = None

try:
    from astrbot.api import AstrBotConfig, logger, llm_tool
except Exception:
    def llm_tool(*args, **kwargs):
        return lambda fn: fn
    AstrBotConfig = dict
    class _Logger:
        def __getattr__(self, name):
            return print
    logger = _Logger()

try:
    from astrbot.api.event import AstrMessageEvent, filter
except Exception:
    class AstrMessageEvent:
        pass
    class _Filter:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: (lambda fn: fn)
    filter = _Filter()

try:
    from astrbot.api.star import Context, Star, StarTools, register
except Exception:
    class Context:
        pass
    class Star:
        def __init__(self, context=None):
            self.context = context
    class StarTools:
        @staticmethod
        def get_data_dir(name):
            return str(Path("data") / "plugin_data" / name)
    def register(*args, **kwargs):
        return lambda cls: cls

try:
    from astrbot.api.web import error_response, json_response, request
except Exception:
    def json_response(data):
        return data
    def error_response(message, status_code=400):
        return {"status": "error", "message": message, "status_code": status_code}
    request = None

from .core.web_v2 import CompanionV2
from .core.config import Settings, as_bool, day_key, split_terms
from .core.cove_client import CoveAPIError, CoveClient
from .core.engine import CompanionEngine
from .core.agent import AUTODJ_TOOLS, PROACTIVE_TOOLS, ROOM_TOOLS, CoveMusicAgent, CoveRoomEvent

try:
    from astrbot.core.agent.tool import FunctionTool, ToolSet
except Exception:
    class ToolSet:
        def __init__(self, tools=None):
            self.tools = list(tools or [])
        def add_tool(self, tool):
            self.tools.append(tool)
        def merge(self, other):
            self.tools.extend(getattr(other, "tools", []))
    class FunctionTool:
        def __init__(self, *, name, description, parameters, handler, **kwargs):
            self.name = name
            self.description = description
            self.parameters = parameters
            self.handler = handler
            self.active = True
from .core.store import CompanionStore
from .core.text import age_seconds, clean, compact, extract_invite, parse_music_command

NAME = "astrbot_plugin_cove_companion"
VERSION = "1.4.1"
DESCRIPTION = "单人陪听与深色唱片室：可靠状态、动态选歌和事件邀听"


def hook(name: str, *args: Any, **kwargs: Any):
    decorator = getattr(filter, name, None)
    if decorator is None:
        return lambda fn: fn
    try:
        return decorator(*args, **kwargs)
    except TypeError:
        try:
            return decorator()
        except Exception:
            return lambda fn: fn


def cfg(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    try:
        value = config.get(key, default)
    except Exception:
        value = default
    return default if value is None else value


def event_obj(event: Any) -> Any:
    return getattr(event, "message_obj", None) or event


def event_text(event: Any) -> str:
    candidates: list[Any] = []
    for obj in (event, event_obj(event)):
        for attr in ("message_str", "message_text", "text", "raw_message", "content"):
            value = getattr(obj, attr, None)
            if value:
                candidates.append(value)
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            candidates.append(getter())
        except Exception:
            pass
    for value in candidates:
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value, ensure_ascii=False)
            except Exception:
                continue
        text = clean(value, None)
        if text:
            return text
    return ""


def event_values(event: Any) -> list[Any]:
    values: list[Any] = [event_text(event)]
    for obj in (event, event_obj(event)):
        for attr in ("raw_message", "raw", "content", "message", "message_obj"):
            value = getattr(obj, attr, None)
            if value is not None:
                values.append(value)
    getter = getattr(event, "get_messages", None)
    if callable(getter):
        try:
            messages = getter()
            values.extend(messages)
            for item in messages:
                for attr in ("data", "raw_message", "message"):
                    value = getattr(item, attr, None)
                    if value is not None:
                        values.append(value)
                dump = getattr(item, "model_dump", None)
                if callable(dump):
                    try:
                        values.append(dump())
                    except Exception:
                        pass
        except Exception:
            pass
    return values


def event_platform(event: Any) -> str:
    getter = getattr(event, "get_platform_id", None)
    if callable(getter):
        try:
            value = getter()
            if value and isinstance(value, str):
                return clean(value, 60)
        except Exception:
            pass
    origin = str(getattr(event, "unified_msg_origin", "") or "")
    if origin.count(":") >= 2:
        return clean(origin.split(":", 1)[0], 60)
    for obj in (event, event_obj(event)):
        meta = getattr(obj, "platform_meta", None) or getattr(obj, "platform", None)
        value = getattr(meta, "id", None) if meta is not None else None
        if isinstance(value, str) and value:
            return clean(value, 60)
    getter = getattr(event, "get_platform_name", None)
    if callable(getter):
        try:
            value = getter()
            if value and isinstance(value, str):
                return clean(value, 60)
        except Exception:
            pass
    return "unknown"


def event_sender(event: Any) -> str:
    getter = getattr(event, "get_sender_id", None)
    if callable(getter):
        try:
            value = getter()
            if value:
                return clean(value, 80)
        except Exception:
            pass
    for obj in (event, event_obj(event)):
        for attr in ("sender_id", "user_id", "senderId", "uid"):
            value = getattr(obj, attr, None)
            if value:
                return clean(value, 80)
    origin = str(getattr(event, "unified_msg_origin", "") or "")
    return origin.rsplit(":", 1)[-1]


def event_group(event: Any) -> str:
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            return clean(getter(), 80)
        except Exception:
            pass
    for obj in (event, event_obj(event)):
        value = getattr(obj, "group_id", None)
        if value:
            return clean(value, 80)
    return ""


def event_origin(event: Any) -> str:
    value = getattr(event, "unified_msg_origin", None)
    return clean(value, 300) if value else ""


def event_message_id(event: Any) -> str:
    for obj in (event, event_obj(event)):
        for attr in ("message_id", "id"):
            value = getattr(obj, attr, None)
            if value:
                return clean(value, 200)
    return ""


def event_timestamp(event: Any) -> float | None:
    for obj in (event, event_obj(event)):
        for attr in ("timestamp", "time", "message_time", "created_at"):
            value = getattr(obj, attr, None)
            if value is None:
                continue
            try:
                if isinstance(value, datetime):
                    return value.timestamp()
                parsed = float(value)
                return parsed / 1000 if parsed > 10_000_000_000 else parsed
            except (TypeError, ValueError):
                pass
    return None


@register(NAME, "QQbot维护", DESCRIPTION, VERSION)
class CoveCompanionPlugin(CompanionV2, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.settings = Settings.from_config(self.config)
        self.data_dir = self._data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        db_path = self.settings.db_path or str(self.data_dir / "cove_companion.sqlite3")
        if not Path(db_path).is_absolute():
            db_path = str(self.data_dir / db_path)
        self.store = CompanionStore(db_path)
        token_file = self.settings.cove_token_file or str(self.data_dir / "cove-bridge-token")
        if not Path(token_file).is_absolute():
            token_file = str(self.data_dir / token_file)
        self.client = CoveClient(self.settings.cove_base_url, token_file)
        self.engine = CompanionEngine(
            context=self.context,
            config=self.config,
            settings=self.settings,
            store=self.store,
            client=self.client,
            agent=None,
            logger=logger,
            qq_send=self._send_qq,
            room_summary=self._anchor_room_memory,
        )
        self.music_agent = CoveMusicAgent(
            context=self.context,
            config=self.config,
            settings=self.settings,
            store=self.store,
            owner_umo=self.settings.owner_umo,
            logger=logger,
        )
        self.music_agent.attach_engine(self.engine)
        self.music_agent.tool_builder = self._room_tool_set
        self.engine.agent = self.music_agent
        self._sse_clients = 0
        self._stream_seq = 0
        self._dashboard_control_lock = asyncio.Lock()
        self._invite_lock = asyncio.Lock()
        self._register_web_apis()
        self._register_v2()

    def _data_dir(self) -> Path:
        try:
            return Path(StarTools.get_data_dir(NAME))
        except Exception:
            return Path("data") / "plugin_data" / NAME

    async def initialize(self) -> None:
        await self.client.start()
        try:
            persona_id = await self.music_agent.current_persona_id()
            self.store.sync_active_persona(persona_id)
        except Exception as exc:
            logger.debug("[cove-companion] initial persona sync unavailable: %s", type(exc).__name__)
        self.engine.start()

    async def terminate(self) -> None:
        await self.engine.stop()
        await self.music_agent.stop()
        self.store.close()

    def _reload_settings(self) -> Settings:
        self.settings = Settings.from_config(self.config)
        self.music_agent.settings = self.settings
        self.engine.update_settings(self.settings)
        return self.settings

    def event_management_context(self, event: AstrMessageEvent) -> dict[str, Any]:
        platform_getter = getattr(event, "get_platform_name", None)
        return {
            "origin": event_origin(event),
            "platform": event_platform(event),
            "platform_name": platform_getter() if callable(platform_getter) else "",
            "sender": event_sender(event),
            "group": event_group(event),
            "enabled": self.settings.enabled,
        }

    def can_manage(self, event: AstrMessageEvent) -> bool:
        context=self.event_management_context(event)
        owner=self.settings.owner_umo.split(':')
        origins=[self.settings.owner_umo]+list(self.settings.trusted_private_origins)
        origin=context['origin'];parts=origin.split(':')
        checks={'owner_configured':len(owner)>=3,'origin':origin in origins,'platform':len(parts)>=3 and context['platform']==parts[0],'sender':bool(owner) and context['sender']==owner[-1] and bool(parts) and parts[-1]==owner[-1],'private':not context['group'] and len(parts)>=3 and parts[-2]=='FriendMessage'}
        allowed=all(checks.values())
        if not allowed:logger.debug('[cove-companion] permission_denied %s',json.dumps({'origin':origin,'platform':context['platform'],'sender':context['sender'],'invite':'not_inspected','enabled':self.settings.enabled,'failed':[k for k,v in checks.items() if not v],'reason':'untrusted_private_identity'},ensure_ascii=False))
        if allowed:
            getter=getattr(event,'get_self_id',None)
            bot=str(getter() or '') if callable(getter) else str(getattr(event_obj(event),'self_id','') or '')
            identities={'owner':context['sender']}
            if bot.isdigit():identities['bot']=bot
            self.store.set('verified_qq_identities',{**(self.store.get('verified_qq_identities',{}) or {}),**identities})
        return allowed

    async def _anchor_room_memory(self, summary: str, session: Mapping[str, Any]) -> None:
        batch=session.get('summary_batch')
        if batch:await self._sync_room_batch(batch,session)

    async def _send_qq(self, umo: str, text: str) -> Any:
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Plain
        return await self.context.send_message(umo, MessageChain(chain=[Plain(clean(text, 1800))]))

    def _room_tool_set(self, kind: str) -> ToolSet:
        return ToolSet([FunctionTool(name='cove_music',description='音乐唯一入口：status/search/select/play_now/cancel_recommendation/pause/resume/next/leave/autodj/feedback/state_update/candidates。所有歌曲须来自真实候选批次。',parameters={'type':'object','properties':{'action':{'type':'string'},'payload':{'type':'string','description':'JSON 对象字符串'}},'required':['action','payload']},handler=self.cove_music)])

    @llm_tool(name='cove_music')
    async def cove_music(self,event: AstrMessageEvent,action: str,payload: str='{}') -> str:
        """单人陪听工具。先搜索或取 candidates，再用批次 select；不能指定主人身份。凭据管理使用后台页面。

        Args:
            action(string): status/search/candidates/select/play_now/cancel_recommendation/recommend/pause/resume/next/leave/autodj/feedback/state_update/invite_evaluate/invite_reply/auth_status。
            payload(string): JSON 对象。select 排下一首，play_now 立即播放，两者需 batch_id/song_id；feedback 需 song_id/kind=like|less/lifetime=session|longterm；state_update 记录有本轮原文依据的 session_intent/tags/energy/emotion/closeness；invite_evaluate 需 signals={context,accumulation,relationship}；invite_reply 需 invitation_id/accept，且原文必须指向当前邀请。
        """
        return await self.primary_music_tool(event,action,payload)

    def _room_tool_turn(self, event: AstrMessageEvent) -> dict[str, Any]:
        if not getattr(event, "get_extra", lambda *_: None)("cove_room_agent", False):
            raise ValueError("此工具只能由网易云房间 Agent 调用。")
        turn_id = str(event.get_extra("cove_room_turn_id", ""))
        turn = self.music_agent.active_turn(turn_id)
        if not turn:
            raise ValueError("房间 Agent 回合不存在或已结束。")
        self.music_agent.reserve_followup(turn_id)
        return turn

    def _record_tool(self, event: AstrMessageEvent, name: str, args: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        turn_id = str(event.get_extra("cove_room_turn_id", ""))
        self.music_agent.record_tool_result(turn_id, name, args, result)

    def _candidate_allowed(self, turn: Mapping[str, Any], song_id: str) -> bool:
        return bool(self.store.cached_song(str(turn.get("id") or ""), song_id))

    async def cove_music_search(self, event: AstrMessageEvent, query: str, limit: int = 5) -> str:
        """搜索网易云真实歌曲，结果中的 song_id 才能用于点歌或排队。

        Args:
            query(string): 歌名、歌手或搜索关键词。
            limit(number): 最多返回结果数，1 到 5。
        """
        try:
            turn = self._room_tool_turn(event)
            search_limit = 5 if turn.get("kind") == "room_reply" else 3
            if int(turn.get("search_count") or 0) >= search_limit:
                result = {"state": "rejected", "error": {"code": "search_limit", "message": "本轮搜索次数已用完，请如实告诉主人没有找到准确歌曲。"}}
                self._record_tool(event, "cove_music_search", {"query": query, "limit": limit}, result)
                return json.dumps(result, ensure_ascii=False)
            turn["search_count"] = int(turn.get("search_count") or 0) + 1
            rows = await self.engine.search(query)
            rows = rows[:max(1, min(5, int(limit)))]
            song_ids = self.store.cache_search_results(turn_id=str(turn["id"]), session_id=str(turn.get("session", {}).get("id") or ""), songs=rows)
            self.store.create_candidate_batch(turn_id=str(turn["id"]), session_id=str(turn.get("session", {}).get("id") or ""), song_ids=song_ids)
            result = {"state": "confirmed", "songs": rows, "song_ids": song_ids}
            self._record_tool(event, "cove_music_search", {"query": query, "limit": limit}, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            result = {"state": "rejected", "error": {"code": "search_failed", "message": clean(exc, 300)}}
            try:
                self._record_tool(event, "cove_music_search", {"query": query, "limit": limit}, result)
            except Exception:
                pass
            return json.dumps(result, ensure_ascii=False)

    async def cove_music_status(self, event: AstrMessageEvent) -> str:
        """读取当前网易云房间、歌曲、播放、歌词和队列状态。"""
        try:
            self._room_tool_turn(event)
            result = await self.engine.status_payload()
            self._record_tool(event, "cove_music_status", {}, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"state": "rejected", "error": {"code": "status_failed", "message": clean(exc, 300)}}, ensure_ascii=False)

    async def cove_music_enqueue_next(self, event: AstrMessageEvent, song_id: str) -> str:
        """把本轮搜索结果中的歌曲排到当前歌曲之后，不切换当前歌曲。

        Args:
            song_id(string): 本轮 cove_music_search 返回的真实网易云歌曲 ID。
        """
        turn = self._room_tool_turn(event)
        if not self._candidate_allowed(turn, song_id):
            result = {"state": "rejected", "error": {"code": "song_not_in_turn_candidates", "message": "必须先搜索，且只能使用本轮真实结果。"}}
        else:
            result = await self.engine.enqueue_next_result(song_id, origin="room_command")
        self._record_tool(event, "cove_music_enqueue_next", {"song_id": song_id}, result)
        return json.dumps(result, ensure_ascii=False)

    async def cove_music_play_now(self, event: AstrMessageEvent, song_id: str) -> str:
        """立即播放本轮搜索结果中的歌曲，工具内部完成入队、切歌和双重确认。

        Args:
            song_id(string): 本轮 cove_music_search 返回的真实网易云歌曲 ID。
        """
        turn = self._room_tool_turn(event)
        if not self._candidate_allowed(turn, song_id):
            result = {"state": "rejected", "error": {"code": "song_not_in_turn_candidates", "message": "必须先搜索，且只能使用本轮真实结果。"}}
        else:
            result = await self.engine.play_now_result(song_id, origin="room_command")
        self._record_tool(event, "cove_music_play_now", {"song_id": song_id}, result)
        return json.dumps(result, ensure_ascii=False)

    async def cove_music_skip_current(self, event: AstrMessageEvent) -> str:
        """明确跳过当前歌曲并等待实时播放回执。"""
        self._room_tool_turn(event)
        result = await self.engine.skip_result()
        self._record_tool(event, "cove_music_skip_current", {}, result)
        return json.dumps(result, ensure_ascii=False)

    async def cove_music_control(self, event: AstrMessageEvent, action: str) -> str:
        """暂停或继续当前网易云播放。

        Args:
            action(string): pause 或 resume。
        """
        self._room_tool_turn(event)
        result = await self.engine.pause_result() if action == "pause" else await self.engine.resume_result() if action == "resume" else {"state": "rejected", "error": {"code": "invalid_action", "message": "只支持 pause 或 resume。"}}
        self._record_tool(event, "cove_music_control", {"action": action}, result)
        return json.dumps(result, ensure_ascii=False)

    async def cove_music_recommend_next(self, event: AstrMessageEvent, song_id: str, intro: str) -> str:
        """自动推荐专用：只把本轮搜索结果排到下一首，并发送一句介绍。

        Args:
            song_id(string): 本轮搜索返回的真实网易云歌曲 ID。
            intro(string): 不超过 80 个中文字符的介绍。
        """
        turn = self._room_tool_turn(event)
        if turn.get("kind") != "autodj":
            result = {"state": "rejected", "error": {"code": "autodj_only", "message": "推荐工具只用于自动推荐回合。"}}
        elif not self._candidate_allowed(turn, song_id):
            result = {"state": "rejected", "error": {"code": "song_not_in_turn_candidates", "message": "必须先搜索本轮真实结果。"}}
        else:
            selected = self.store.cached_song(str(turn.get("id") or ""), song_id) or {"id": song_id}
            result = await self.engine.recommend_from_agent(
                selected,
                intro,
                manual=str(turn.get("trigger") or "").startswith("manual"),
                expected_session_id=str((turn.get("session") or {}).get("id") or ""),
                expected_decision_revision=int(turn.get("decision_revision") or 0),
            )
        self._record_tool(event, "cove_music_recommend_next", {"song_id": song_id, "intro": intro}, result)
        return json.dumps(result, ensure_ascii=False)

    async def cove_music_cancel_recommendation(self, event: AstrMessageEvent) -> str:
        """取消本插件尚未播放的自动推荐。"""
        self._room_tool_turn(event)
        result = await self.engine.cancel_recommendation_result()
        self._record_tool(event, "cove_music_cancel_recommendation", {}, result)
        return json.dumps(result, ensure_ascii=False)

    async def cove_music_set_mode(self, event: AstrMessageEvent, autodj: str = "unchanged", room_chat: str = "unchanged") -> str:
        """调整自动推荐或房间聊天开关。

        Args:
            autodj(string): on、off 或 unchanged。
            room_chat(string): on、off 或 unchanged。
        """
        self._room_tool_turn(event)
        changed = {}
        if autodj in {"on", "off"}:
            changed["autodj"] = await self.engine.set_autodj(autodj == "on")
        if room_chat in {"on", "off"}:
            changed["room_chat"] = await self.engine.set_room_chat(room_chat == "on")
        result = {"state": "confirmed" if changed else "rejected", "changed": changed}
        self._record_tool(event, "cove_music_set_mode", {"autodj": autodj, "room_chat": room_chat}, result)
        return json.dumps(result, ensure_ascii=False)

    async def cove_music_leave(self, event: AstrMessageEvent) -> str:
        """停止陪听并核对网易云远端退出状态。"""
        self._room_tool_turn(event)
        result = await self.engine.leave_result(reason="room_agent")
        self._record_tool(event, "cove_music_leave", {}, result)
        return json.dumps(result, ensure_ascii=False)

    @filter.command("一起听")
    async def together_command(self, event: AstrMessageEvent):
        if not self.can_manage(event):
            return
        text = event_text(event).strip().removeprefix("/").strip()
        command = parse_music_command(text)
        if command:
            if not self.settings.enabled and command.get("kind") not in {"status","diagnose","leave"}:
                yield event.plain_result("陪听已禁用，可查询状态、退出或在后台处理账号。")
                return
            yield event.plain_result(await self._dispatch_command(command))
            return
        pending=self.engine.pending_invitation()
        if pending and text.strip()=="一起听 "+pending["id"][:8]:
            if not self.settings.enabled:
                yield event.plain_result("陪听已禁用，未创建房间。");return
            result=await self.engine.respond_invitation(pending["id"],True)
            yield event.plain_result(str(result.get("message") or result.get("reason") or result.get("state")))
            return
        if pending and text.strip() in {"不听 "+pending["id"][:8],"拒绝 "+pending["id"][:8]}:
            await self.engine.respond_invitation(pending["id"],False)
            yield event.plain_result("好，这次先不听。我会按退避设置减少邀请。")
            return
        if not self.settings.enabled:return
        invite = extract_invite(*event_values(event))
        if invite:
            yield event.plain_result(await self._join_invite(event, invite))
            return
        yield event.plain_result("用法：/一起听 状态、自动 开/关、聊天 开/关、推荐一首、搜索 关键词、下首 歌曲ID、立刻播放 歌曲ID、暂停、继续、下一首、取消待播推荐、退出。")

    @filter.command("网易云陪听")
    async def together_legacy_command(self, event: AstrMessageEvent):
        async for result in self.together_command(event):
            yield result

    @hook("event_message_type", getattr(getattr(filter, "EventMessageType", object), "ALL", "all"))
    async def on_message(self, event: AstrMessageEvent):
        context = self.event_management_context(event)
        if not self.can_manage(event):
            logger.debug("[cove-companion] on_message skip reason=not_owner context=%s", context)
            return
        text = event_text(event)
        if text.lstrip().startswith(("/一起听", "/网易云陪听")):
            logger.debug("[cove-companion] on_message skip reason=slash_command context=%s", context)
            return
        pending=self.engine.pending_invitation()
        reply=text.strip().removeprefix('/').strip()
        if pending and reply=='一起听 '+pending['id'][:8]:
            if not self.settings.enabled:
                yield event.plain_result('陪听已禁用，未创建房间。');return
            result=await self.engine.respond_invitation(pending['id'],True)
            yield event.plain_result(str(result.get('message') or result.get('reason') or result.get('state')));return
        if pending and reply in {'不听 '+pending['id'][:8],'拒绝 '+pending['id'][:8]}:
            await self.engine.respond_invitation(pending['id'],False)
            yield event.plain_result('好，这次先不听。我会按退避设置减少邀请。');return
        command = parse_music_command(text)
        if command:
            if not self.settings.enabled and command.get('kind') not in {'status','diagnose','leave'}:
                yield event.plain_result('陪听已禁用，可查询状态、退出或在后台处理账号。');return
            yield event.plain_result(await self._dispatch_command(command))
            return
        if not self.settings.enabled:return
        invite = extract_invite(*event_values(event))
        if not invite:
            ident=event_message_id(event) or str(uuid.uuid4())
            self.engine._spawn(self.engine.music_invitation_event('qq:'+ident,'private_conversation',text),'music-private-event')
            return
        timestamp = event_timestamp(event)
        age = age_seconds(timestamp) if timestamp else 0
        if age is not None and age > 600:
            yield event.plain_result("这张邀请已经超过十分钟，为避免重放没有加入。请重新邀请。")
            return
        yield event.plain_result(await self._join_invite(event, invite))

    async def _join_invite(self, event: AstrMessageEvent, invite: Mapping[str, str]) -> str:
        room_id = str(invite.get("room_id") or "").strip()
        inviter_id = str(invite.get("inviter_id") or "").strip()
        if inviter_id != self.settings.owner_netease_uid:
            logger.debug("[cove-companion] permission_denied %s",json.dumps({**self.event_management_context(event),"invite":"untrusted_inviter","failed":["inviter"],"reason":"inviter_mismatch"},ensure_ascii=False))
            return "邀请者与已绑定主人不符，未执行控制。"
        message_id = event_message_id(event)
        marker = "invite:" + message_id if message_id else ""
        lock = getattr(self, "_invite_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._invite_lock = lock
        async with lock:
            if marker and self.store.get(marker):
                return "这张一起听邀请已经处理过了。"
            logger.info("[cove-companion] invite accepted room=%s inviter=%s", room_id[:16] + "…", inviter_id[-4:])
            current = self.store.active_session()
            if current and current.get("room_id") == room_id and current.get("phase") == "active":
                from .core.lifecycle import fresh
                snapshot=await self.engine.snapshot(True)
                if not (fresh(snapshot,'room') and fresh(snapshot,'members') and snapshot.get('room_id')==room_id and snapshot.get('connected') is True and (snapshot.get('owner_binding') or {}).get('present') is True):
                    return '之前的房间状态已过期，正在重新核验；未重复下发加入。'
                result = {"ok": True, "already_joined": True, "message": "已经在这个一起听房间里了。", "session_id": current.get("id")}
            else:
                if current and current.get("room_id") and current.get("room_id") != room_id:
                    old = await self.engine.leave_result(session_id=current["id"], reason="room_changed")
                    self.store.audit("qq_owner", "room_change", {"old_room": current.get("room_id"), "new_room": room_id, "leave": old})
                    if old.get("state") != "confirmed":
                        return "原一起听房间退出未确认，暂不切换新房间。请先确认网易云当前房间状态。"
                result = await self.engine.join(room_id, inviter_id, origin="qq_command")
            if result.get("ok") and marker:
                self.store.set(marker, {"room_id": room_id, "inviter_id": inviter_id, "processed_at": time.time()})
            self.store.audit("qq_owner", "invite", {"room_id": room_id, "inviter_id": inviter_id, "ok": result.get("ok"), "message": result.get("message"), "message_id": message_id})
            return str(result.get("message") or "处理完成。")

    async def _dispatch_command(self, command: Mapping[str, Any]) -> str:
        kind = str(command.get("kind") or "")
        if kind == "diagnose":
            await self.engine.snapshot(True)
            return self.engine.diagnose_text()
        if kind == "status":
            await self.engine.snapshot(True)
            return self.engine.status_text()
        if kind == "autodj":
            return await self.engine.set_autodj(bool(command.get("enabled")))
        if kind == "room_chat":
            return await self.engine.set_room_chat(bool(command.get("enabled")))
        if kind == "recommend_once":
            return await self.engine.recommend_once()
        if kind == "cancel_recommendation":
            return await self.engine.cancel_pending_recommendation()
        if kind == "pause":
            return await self.engine.pause()
        if kind == "resume":
            return await self.engine.resume()
        if kind == "next":
            return await self.engine.next_track()
        if kind == "enqueue":
            return await self.engine.enqueue(str(command.get("song_id") or ""))
        if kind == "play_now":
            return await self.engine.enqueue(str(command.get("song_id") or ""), play_now=True)
        if kind == "leave":
            return await self.engine.leave()
        if kind == "duration":
            session = self.store.active_session()
            if not session:
                return "当前没有陪听会话；发送双人邀请后再设定时长。"
            minutes = int(command.get("minutes") or 0)
            self.store.update_session(session["id"], expires_at=time.time() + minutes * 60)
            return f"好，这次陪你听 {minutes} 分钟。"
        if kind == "search":
            query = str(command.get("query") or "")
            try:
                rows = await self.engine.search(query)
            except Exception as exc:
                return "搜索失败：" + clean(exc, 300)
            if not rows:
                return "没有找到真实结果。"
            return "\n".join(f"{item.get('name')} - {item.get('artist')}（{item.get('id')}）" for item in rows)
        return "没有识别这条音乐指令。"

    def _register_web_apis(self) -> None:
        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            return
        routes = [
            (f"/{NAME}/v1/bootstrap", self._api_bootstrap, ["GET"]),
            (f"/{NAME}/v1/player", self._api_player, ["GET"]),
            (f"/{NAME}/v1/stream", self._api_stream, ["GET"]),
            (f"/{NAME}/v1/onboarding/check", self._api_onboarding_check, ["POST"]),
            (f"/{NAME}/v1/onboarding/enable", self._api_onboarding_enable, ["POST"]),
            (f"/{NAME}/v1/status", self._api_status, ["GET"]),
            (f"/{NAME}/v1/queue", self._api_queue, ["GET"]),
            (f"/{NAME}/v1/search", self._api_search, ["GET"]),
            (f"/{NAME}/v1/lyrics", self._api_lyrics, ["GET"]),
            (f"/{NAME}/v1/history", self._api_history, ["GET"]),
            (f"/{NAME}/v1/operations", self._api_operations, ["GET"]),
            (f"/{NAME}/v1/health", self._api_health, ["GET"]),
            (f"/{NAME}/v1/control", self._api_control, ["POST"]),
            (f"/{NAME}/v1/settings", self._api_settings, ["POST"]),
            (f"/{NAME}/v1/maintenance", self._api_maintenance, ["POST"]),
        ]
        for route, handler, methods in routes:
            try:
                register_api(route, handler, methods=methods, desc="cove companion")
            except TypeError:
                try:
                    register_api(route, handler, methods)
                except Exception:
                    pass
            except Exception:
                pass

    @staticmethod
    def _query(request_obj: Any = None) -> Mapping[str, Any]:
        if request_obj is None:
            request_obj = request
        if request_obj is None:
            return {}
        for attr in ("query_params", "query", "args"):
            value = getattr(request_obj, attr, None)
            if isinstance(value, Mapping) or hasattr(value, "get"):
                return value
        return {}

    async def _body(self, request_obj: Any = None) -> dict[str, Any]:
        if request_obj is None:
            request_obj = request
        if request_obj is None:
            return {}
        value = getattr(request_obj, "json", None)
        try:
            if callable(value):
                try:
                    value = value(default={})
                except TypeError:
                    value = value()
                value = await value if asyncio.iscoroutine(value) else value
        except Exception:
            value = None
        if isinstance(value, Mapping):
            return dict(value)
        body = getattr(request_obj, "body", None)
        if isinstance(body, (bytes, bytearray)):
            try:
                parsed = json.loads(body.decode("utf-8"))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                pass
        return {}

    @staticmethod
    def _public_session(session: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not session:
            return None
        return {
            key: session.get(key)
            for key in ("id", "phase", "autodj_enabled", "room_chat_enabled", "playback_state", "session_version", "expires_at", "next_recommend_at", "next_eligibility_check_at", "blocked_reason", "blocked_until", "decision_revision", "end_reason", "started_at", "ended_at")
        }

    @staticmethod
    def _public_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
        value = dict(snapshot or {})
        room_id = str(value.pop("room_id", "") or "")
        value.pop("room_epoch", None)
        value.pop("bot_account", None)
        value.pop("owner_binding", None)
        if room_id:
            value["room_id_hint"] = room_id[:8] + "…"
        members = []
        for member in value.get("members") or []:
            if isinstance(member, Mapping):
                members.append({"role": member.get("role"), "present": member.get("present")})
        value["members"] = members
        queue = dict(value.get("queue") or {})
        queue.pop("versions", None)
        value["queue"] = queue
        worker = dict(value.get("worker") or {})
        worker.pop("account_id", None)
        value["worker"] = worker
        return value

    @staticmethod
    def _public_turn(turn: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": turn.get("id"),
            "kind": turn.get("kind"),
            "trigger": turn.get("trigger"),
            "state": turn.get("state"),
            "provider_id": turn.get("provider_id"),
            "context_bytes": turn.get("context_bytes"),
            "error": turn.get("error"),
            "started_at": turn.get("started_at"),
            "finished_at": turn.get("finished_at"),
            "input": {"context": (turn.get("input") or {}).get("context") or {}},
            "tool_calls": [{"name": tool.get("name"), "state": tool.get("state")} for tool in turn.get("tool_calls") or []],
        }

    @classmethod
    def _public_player(cls, state: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(state)
        value["session"] = cls._public_session(state.get("session"))
        value["snapshot"] = cls._public_snapshot(state.get("snapshot"))
        value["recent_agent_turns"] = [cls._public_turn(turn) for turn in state.get("recent_agent_turns") or []]
        return value

    @staticmethod
    def _public_health(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not value:
            return None
        return {
            "ready": bool(value.get("ok") or (value.get("health") or {}).get("ready")),
            "protocol": value.get("protocol") or (value.get("health") or {}).get("protocol"),
            "worker_phase": (value.get("health") or {}).get("worker", {}).get("phase"),
            "nim_connected": bool((value.get("health") or {}).get("realtime", {}).get("connected")),
        }

    @staticmethod
    def _ok(data: Any) -> Any:
        return json_response({"status": "ok", "data": data})

    async def _api_bootstrap(self, request_obj: Any = None) -> Any:
        self._reload_settings()
        data = await self.engine.bootstrap_status()
        data["session"] = self._public_session(data.get("session"))
        data["snapshot"] = self._public_snapshot(data.get("snapshot"))
        data["cove"] = self._public_health(data.get("cove"))
        data.update({"version": VERSION, "protocol": "astrbot-cove/1"})
        return self._ok(data)

    async def _api_player(self, request_obj: Any = None) -> Any:
        force = str(self._query(request_obj).get("fresh") or "") in {"1", "true"}
        return self._ok(self._public_player(await self.engine.player_state(force=force)))

    async def _api_stream(self, request_obj: Any = None) -> Any:
        if StreamingResponse is None:
            return error_response("streaming unavailable", status_code=503)
        if not self.settings.player_stream_enabled:
            return error_response("streaming disabled", status_code=503)
        if self._sse_clients >= 3:
            return error_response("too many stream subscribers", status_code=429)
        raw_request = getattr(request_obj, "request", None) or request_obj
        self._sse_clients += 1

        async def disconnected() -> bool:
            checker = getattr(raw_request, "is_disconnected", None)
            if not callable(checker):
                return False
            try:
                result = checker()
                return bool(await result) if asyncio.iscoroutine(result) else bool(result)
            except Exception:
                return False

        async def events():
            last_signature = ""
            last_heartbeat = 0.0
            try:
                while not await disconnected():
                    try:
                        state = self._public_player(await self.engine.player_state())
                        operations = state.get("recent_operations") or []
                        turns = state.get("recent_agent_turns") or []
                        summaries = state.get("recent_summaries") or []
                        signature = json.dumps({
                            "snapshot": state.get("snapshot", {}).get("room_revision"),
                            "song": (state.get("song") or {}).get("id"),
                            "status": (state.get("playback") or {}).get("status"),
                            "server_seq": (state.get("playback") or {}).get("server_seq"),
                            "progress_anchor": (state.get("playback") or {}).get("progress_ms"),
                            "observed_at": (state.get("playback") or {}).get("observed_at"),
                            "lyric": (state.get("lyric") or {}).get("current"),
                            "queue": (state.get("queue") or {}).get("fingerprint"),
                            "pending": (state.get("pending_recommendation") or {}).get("id"),
                            "operation": (operations[0] or {}).get("operation_id") if operations else None,
                            "turn": (turns[0] or {}).get("id") if turns else None,
                            "summary": (summaries[0] or {}).get("id") if summaries else None,
                        }, ensure_ascii=False, sort_keys=True)
                        now_mono = time.monotonic()
                        if signature != last_signature:
                            event_type = "snapshot" if not last_signature else "operation" if operations and operations[0].get("operation_id") and str(operations[0].get("operation_id")) not in last_signature else "agent_turn" if turns and turns[0].get("id") and str(turns[0].get("id")) not in last_signature else "snapshot"
                            self._stream_seq += 1
                            yield f"id: {self._stream_seq}\nevent: {event_type}\ndata: {json.dumps(state, ensure_ascii=False, separators=(',', ':'))}\n\n"
                            last_signature = signature
                            last_heartbeat = now_mono
                        elif now_mono - last_heartbeat >= 10:
                            self._stream_seq += 1
                            payload = {"server_time": int(time.time() * 1000)}
                            yield f"id: {self._stream_seq}\nevent: heartbeat\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
                            last_heartbeat = now_mono
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        payload = {"error": type(exc).__name__}
                        yield "event: error\ndata: " + json.dumps(payload) + "\n\n"
                    await asyncio.sleep(1)
            finally:
                self._sse_clients = max(0, self._sse_clients - 1)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _api_onboarding_check(self, request_obj: Any = None) -> Any:
        body = await self._body(request_obj)
        checks: list[dict[str, Any]] = []

        def add(check_id: str, label: str, state: str, message: str) -> None:
            checks.append({"id": check_id, "label": label, "state": state, "message": message})

        add("owner", "主人绑定", "pass" if self.settings.owner_umo else "fail", "已配置主人 QQ 会话" if self.settings.owner_umo else "缺少 owner_umo")
        add("netease", "网易云账号", "pass" if self.settings.owner_netease_uid else "fail", "已配置网易云 UID" if self.settings.owner_netease_uid else "缺少 owner_netease_uid")
        try:
            token_ok = self.client.token_file.is_file() and self.client.token_file.stat().st_size > 0
        except OSError:
            token_ok = False
        add("token", "Cove 令牌文件", "pass" if token_ok else "fail", "令牌文件可读取" if token_ok else "缺少令牌文件；请完成 Cove 认证")
        try:
            health = await self.client.health()
            ready = bool(health.get("ok") or health.get("health", {}).get("ready"))
            add("cove", "Cove 服务", "pass" if ready else "fail", "Cove ready" if ready else "Cove 尚未 ready")
        except Exception as exc:
            add("cove", "Cove 服务", "fail", "Cove 检查失败：" + type(exc).__name__)
        try:
            provider = await self.music_agent.provider_id()
            add("provider", "主 Provider", "pass" if provider else "fail", "已解析 Provider" if provider else "缺少可用 Provider")
        except Exception as exc:
            add("provider", "主 Provider", "fail", "Provider 检查失败：" + type(exc).__name__)
        try:
            persona = await self.music_agent.current_persona_id()
            add("persona", "人格", "pass" if persona else "fail", "已解析最终人格" if persona else "未解析到人格")
        except Exception as exc:
            add("persona", "人格", "fail", "人格检查失败：" + type(exc).__name__)
        try:
            db_status = self.store.quick_check()
            add("database", "数据库", "pass" if db_status == "ok" else "fail", "SQLite quick_check=" + db_status)
        except Exception as exc:
            add("database", "数据库", "fail", "数据库检查失败：" + type(exc).__name__)
        snapshot = await self.engine.snapshot(False)
        add("room", "网易云房间", "pass" if snapshot.get("room_id") and snapshot.get("nim_connected") else "warn", "房间与 NIM 在线" if snapshot.get("room_id") and snapshot.get("nim_connected") else "尚未进入房间或 NIM 未连接")
        if body.get("run_agent_selftest"):
            session = self.store.active_session()
            if session:
                result = await self.music_agent.run_selftest(session=session)
                add("agent_selftest", "Agent 自检", "pass" if result.get("state") in {"completed", "confirmed"} else "fail", "模型与工具链可用" if result.get("state") in {"completed", "confirmed"} else str(result.get("error") or "自检失败"))
            else:
                add("agent_selftest", "Agent 自检", "warn", "需要先进入房间会话")
        return self._ok({"ready": all(item["state"] != "fail" for item in checks), "checks": checks, "generated_at": int(time.time() * 1000)})

    async def _api_onboarding_enable(self, request_obj: Any = None) -> Any:
        """Enable automation only after the non-secret first-run checks pass."""
        if not self.settings.owner_umo or not self.settings.owner_netease_uid:
            return error_response("请先配置主人会话和网易云 UID。", status_code=409)
        try:
            if not self.client.token_file.is_file() or self.client.token_file.stat().st_size <= 0:
                return error_response("Cove 令牌文件尚未准备好。", status_code=409)
            health = await self.client.health()
            if not bool(health.get("ok") or health.get("health", {}).get("ready")):
                return error_response("Cove 尚未 ready。", status_code=409)
            provider = await self.music_agent.provider_id()
            persona = await self.music_agent.current_persona_id()
            if not provider or not persona:
                return error_response("主 Provider 或人格尚未解析。", status_code=409)
            if self.store.quick_check() != "ok":
                return error_response("SQLite 检查未通过。", status_code=409)
        except Exception as exc:
            return error_response("首次检查失败：" + type(exc).__name__, status_code=409)
        self.config.update({"enabled": True, "autodj_enabled": True, "invitation_enabled": True})
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            result = saver()
            if asyncio.iscoroutine(result):
                await result
        settings = self._reload_settings()
        self.store.audit("dashboard", "onboarding_enable", {"automatic_actions": True})
        return self._ok({"enabled": True, "settings": settings.public()})

    async def _api_status(self, request_obj: Any = None) -> Any:
        await self.engine.snapshot(True)
        return self._ok({"text": self.engine.status_text(), "session": self._public_session(self.store.active_session()), "snapshot": self._public_snapshot(await self.engine.snapshot(False))})

    async def _api_queue(self, request_obj: Any = None) -> Any:
        raw_offset = self._query(request_obj).get("offset")
        try:
            offset = int(raw_offset) if raw_offset is not None else None
            if offset is not None and offset < 0:
                raise ValueError()
        except (ValueError, TypeError):
            return error_response("队列分页位置无效。", status_code=400)
        snapshot = self._public_snapshot(await self.engine.snapshot(False))
        page = await self.engine.queue_page(offset, snapshot=snapshot)
        return self._ok({**page, "current_song": snapshot.get("current_song"), "queue": page, "playback": snapshot.get("playback"), "evidence": snapshot.get("evidence")})

    async def _api_search(self, request_obj: Any = None) -> Any:
        query = str(self._query(request_obj).get("query") or "").strip()
        if not query:
            return error_response("missing query", status_code=400)
        try:
            return self._ok({"songs": await self.engine.search(query)})
        except Exception as exc:
            return error_response(clean(exc, 300), status_code=502)

    async def _api_lyrics(self, request_obj: Any = None) -> Any:
        song_id = str(self._query(request_obj).get("song_id") or "").strip()
        if not song_id.isdigit():
            return error_response("invalid song_id", status_code=400)
        try:
            return self._ok(await self.engine.lyrics(song_id))
        except Exception as exc:
            return error_response(clean(exc, 300), status_code=502)

    async def _api_history(self, request_obj: Any = None) -> Any:
        query = self._query(request_obj)
        try:
            limit = int(query.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        messages = self.store.room_messages(limit)
        return self._ok({"items": self.store.history(limit=limit), "message_count": len(messages), "reply_batch_count": len(self.store.reply_batches(limit))})

    async def _api_operations(self, request_obj: Any = None) -> Any:
        try:
            limit = int(self._query(request_obj).get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        return self._ok({
            "items": self.store.operations(limit),
            "jobs": [{"id": job.get("id"), "kind": job.get("kind"), "status": job.get("status"), "attempts": job.get("attempts"), "error": job.get("error"), "created_at": job.get("created_at"), "updated_at": job.get("updated_at")} for job in self.store.jobs(limit)],
            "agent_turns": [self._public_turn(turn) for turn in self.store.agent_turns(limit)],
            "external_overrides": [{"id": item.get("id"), "event_type": item.get("event_type"), "attribution": item.get("attribution"), "action": item.get("action"), "created_at": item.get("created_at")} for item in self.store.external_overrides(limit)],
        })

    async def _api_health(self, request_obj: Any = None) -> Any:
        cove = None
        cove_error = None
        try:
            cove = await self.client.health()
        except Exception as exc:
            cove_error = type(exc).__name__
        return self._ok({
            "plugin": {"version": VERSION, "enabled": self.settings.enabled, "store": self.store.health(), "usage": self.store.usage_summary()},
            "cove": self._public_health(cove),
            "cove_error": cove_error,
            "snapshot": self._public_snapshot(await self.engine.snapshot(False)),
        })

    async def _api_control(self, request_obj: Any = None) -> Any:
        body = await self._body(request_obj)
        async with self._dashboard_control_lock:
            return await self._dashboard_control(body)

    async def _dashboard_control(self, body: Mapping[str, Any]) -> Any:
        action = str(body.get("action") or "").strip()
        logger.info("[cove-companion] dashboard control action=%s", action)
        idempotency_key = clean(body.get("idempotency_key"), 160)
        if not idempotency_key:
            return error_response("idempotency_key required", status_code=400)
        cache_key = "control:" + idempotency_key
        request_hash = hashlib.sha256(json.dumps({key: value for key, value in body.items() if key != "idempotency_key"}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        previous = self.store.get(cache_key)
        if previous is not None:
            if "request_hash" not in previous:
                return self._ok(previous)
            if previous["request_hash"] != request_hash:
                return error_response("同一个请求标识不能用于不同操作。请刷新页面。", status_code=409)
            if previous.get("state") != "done":
                return error_response("上次请求可能仍在执行，请先查看活动与回执；不会重复发送。", status_code=409)
            if previous.get("error"):
                return error_response(previous["error"], status_code=409)
            return self._ok(previous["response"])
        session = self.store.active_session()
        if body.get("session_id") and (not session or body["session_id"] != session["id"]):
            return error_response("陪听会话已变化，请刷新后操作。", status_code=409)
        expected_version = body.get("expected_version")
        if expected_version is not None and session and int(expected_version) != int(session.get("session_version") or 0):
            logger.debug("[cove-companion] dashboard control version mismatch expected=%s current=%s", expected_version, session.get("session_version"))
            return error_response("房间状态已变化，请刷新后操作。", status_code=409)
        self.store.set(cache_key, {"request_hash": request_hash, "state": "running"})
        try:
            if action == "autodj":
                result = await self.engine.set_autodj(as_bool(body.get("enabled")))
            elif action == "room_chat":
                result = await self.engine.set_room_chat(as_bool(body.get("enabled")))
            elif action == "recommend_once":
                result = await self.engine.recommend_once()
            elif action == "cancel_recommendation":
                result = await self.engine.cancel_pending_recommendation()
            elif action == "pause":
                result = await self.engine.pause(origin="dashboard")
            elif action == "resume":
                result = await self.engine.resume(origin="dashboard")
            elif action == "next":
                result = await self.engine.next_track(origin="dashboard")
            elif action == "enqueue":
                result = await self.engine.enqueue(str(body.get("song_id") or ""), origin="dashboard")
            elif action == "play_now":
                result = await self.engine.enqueue(str(body.get("song_id") or ""), play_now=True, origin="dashboard")
            elif action == "leave":
                result = await self.engine.leave()
            elif action == "join":
                joined = await self.engine.join(str(body.get("room_id") or ""), str(body.get("inviter_id") or ""), origin="dashboard")
                result = str(joined.get("message") or joined)
            elif action == "import_ledger":
                result = self.store.import_legacy_ledger(str(body.get("path") or str(self.data_dir / "pushed_songs.jsonl")))
            else:
                self.store.set(cache_key, {"request_hash": request_hash, "state": "done", "error": "不支持的操作。"})
                return error_response("unsupported action", status_code=400)
            payload = {"action": action, "result": result, "at": time.time()}
            self.store.set(cache_key, {"request_hash": request_hash, "state": "done", "response": payload})
            self.store.audit("dashboard", "control", payload)
            return self._ok(payload)
        except Exception as exc:
            self.store.set(cache_key, {"request_hash": request_hash, "state": "done", "error": clean(exc, 500)})
            return error_response(clean(exc, 500), status_code=409)

    async def _api_settings(self, request_obj: Any = None) -> Any:
        body = await self._body(request_obj)
        allowed = {
            'token_daily_limit','persona_music_terms',
            "enabled", "owner_netease_uid", "provider_id", "fallback_provider_id", "provider_retry_count",
            "pause_end_seconds", "owner_absence_seconds", "disconnect_timeout_seconds", "meeting_timeout_seconds", "invitation_enabled", "invitation_threshold", "exploration_rate", "same_category_limit", "diagnostic_progress_seconds",
            "autodj_enabled", "room_chat_enabled", "recommendation_interval_seconds", "first_recommendation_delay_seconds",
            "dedup_minutes", "session_max_minutes", "model_daily_budget", "room_chat_daily_budget", "autodj_daily_budget", "proactive_daily_budget", "reserve_daily_budget", "autodj_hourly_limit",
            "model_timeout_seconds", "agent_timeout_seconds", "agent_max_steps", "agent_context_bytes", "qq_history_turns", "room_history_messages", "chat_batch_wait_seconds", "chat_batch_max_wait_seconds", "room_chat_backlog_ttl_seconds", "proactive_cooldown_seconds", "proactive_window_delay_seconds",
            "reference_playlist_ids", "music_preferences", "excluded_terms", "reduced_motion", "default_panel_page", "retention_days", "backup_keep", "player_stream_enabled",
        }
        updated: dict[str, Any] = {}
        for key, value in body.items():
            if key not in allowed:
                continue
            try:
                if key=='token_daily_limit':
                    value=int(value)
                    if not 1000<=value<=100_000_000:raise ValueError()
                elif key=='persona_music_terms':value=split_terms(value,10)
                elif key in {"enabled", "autodj_enabled", "room_chat_enabled", "reduced_motion", "player_stream_enabled", "invitation_enabled"}:
                    value = as_bool(value)
                elif key in {"pause_end_seconds", "owner_absence_seconds", "disconnect_timeout_seconds", "meeting_timeout_seconds", "same_category_limit", "diagnostic_progress_seconds", "recommendation_interval_seconds", "first_recommendation_delay_seconds", "dedup_minutes", "session_max_minutes", "model_daily_budget", "room_chat_daily_budget", "autodj_daily_budget", "proactive_daily_budget", "reserve_daily_budget", "autodj_hourly_limit", "model_timeout_seconds", "agent_timeout_seconds", "agent_max_steps", "agent_context_bytes", "qq_history_turns", "room_history_messages", "chat_batch_wait_seconds", "chat_batch_max_wait_seconds", "room_chat_backlog_ttl_seconds", "proactive_cooldown_seconds", "proactive_window_delay_seconds", "provider_retry_count", "retention_days", "backup_keep"}:
                    value = int(value)
                elif key in {"invitation_threshold", "exploration_rate"}:
                    value = float(value)
                    if not 0 <= value <= 1:raise ValueError()
                    if key=='exploration_rate' and value>.4:raise ValueError()
                elif key in {"reference_playlist_ids", "music_preferences", "excluded_terms"}:
                    value = split_terms(value, 3 if key == "reference_playlist_ids" else 30)
                    if key == "reference_playlist_ids" and any(not item.isdigit() for item in value):
                        return error_response("参考歌单 ID 必须是数字，并用逗号分隔。", status_code=400)
                else:
                    value = clean(value, 2000)
                updated[key] = value
            except (TypeError, ValueError):
                return error_response(f"invalid setting: {key}", status_code=400)
        self.config.update(updated)
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            try:
                result = saver()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                return error_response("配置已更新到内存，但保存失败：" + clean(exc, 300), status_code=500)
        settings = self._reload_settings()
        self.store.audit("dashboard", "settings", {"keys": sorted(updated)})
        return self._ok({"updated": updated, "settings": settings.public()})

    @staticmethod
    def _redact_diagnostic(value: Any) -> Any:
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                lowered = str(key).lower()
                if any(marker in lowered for marker in ("owner_umo", "owner_netease_uid", "token", "cookie", "authorization")):
                    redacted[key] = "[redacted]"
                elif lowered in {"prompt", "input", "output_text", "text"}:
                    redacted[key] = "[omitted]"
                else:
                    redacted[key] = CoveCompanionPlugin._redact_diagnostic(item)
            return redacted
        if isinstance(value, list):
            return [CoveCompanionPlugin._redact_diagnostic(item) for item in value[:50]]
        return value

    async def _api_maintenance(self, request_obj: Any = None) -> Any:
        body = await self._body(request_obj)
        kind = str(body.get("kind") or "").strip()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if kind == "backup":
            destination = self.store.backup(self.data_dir / "backups" / f"cove-companion-{stamp}.sqlite3")
            return self._ok({"backup": str(destination), "bytes": destination.stat().st_size})
        if kind == "diagnostic_bundle":
            destination = self.data_dir / "diagnostics" / f"diagnostic-{stamp}.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            turns = []
            for turn in self.store.agent_turns(10):
                turns.append({key: turn.get(key) for key in ("id", "kind", "trigger", "state", "persona_id", "provider_id", "context_bytes", "error", "started_at", "finished_at")})
            payload = self._redact_diagnostic({
                "version": VERSION,
                "protocol": "astrbot-cove/1",
                "generated_at": int(time.time() * 1000),
                "health": self.store.health(),
                "usage": self.store.usage_summary(),
                "snapshot": await self.engine.snapshot(False),
                "recent_agent_turns": turns,
                "recent_external_overrides": self.store.external_overrides(10),
            })
            destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            try:
                destination.chmod(0o600)
            except OSError:
                pass
            return self._ok({"bundle": str(destination), "bytes": destination.stat().st_size})
        if kind == "import_ledger":
            path = str(body.get("path") or str(self.data_dir / "pushed_songs.jsonl"))
            return self._ok(self.store.import_legacy_ledger(path))
        if kind == "pause_autodj":
            return self._ok({"message": await self.engine.set_autodj(False)})
        if kind == "agent_selftest":
            session = self.store.active_session()
            if not session:
                return error_response("no active session", status_code=409)
            return self._ok(await self.music_agent.run_selftest(session=session))
        if kind == "retry_failed_jobs":
            count = 0
            for job in self.store.jobs(200):
                if job.get("status") == "failed":
                    self.store.retry_job(job["id"], "manual_retry", 0)
                    count += 1
            return self._ok({"retried": count})
        return error_response("unsupported maintenance action", status_code=400)

    @hook('on_llm_request')
    async def on_music_llm_request(self,event: AstrMessageEvent,req: Any=None):
        if req is None or not self.can_manage(event):return
        events=self.store.music_events(0,pending=True)
        state=self.store.music_state(self.store.scope_key())
        # Keep complete JSON and acknowledge only events actually delivered. A
        # large state/event cannot truncate another event and silently consume it.
        def bounded(value,depth=0):
            if depth>3:return str(value)[:120]
            if isinstance(value,dict):return {str(k)[:60]:bounded(v,depth+1) for k,v in list(value.items())[:12]}
            if isinstance(value,list):return [bounded(v,depth+1) for v in value[:5]]
            return value[:200] if isinstance(value,str) else value
        packet={'music_events':[],'selection_state':{},'rules':'只有新音乐相关依据才考虑 cove_music invite_evaluate；正常聊天可用 state_update 保存有来源的选歌参数。别固定唤醒。'}
        def encoded():return json.dumps(packet,ensure_ascii=False,default=str,separators=(',',':'))
        for item in events[:5]:
            entry={'seq':item['seq'],'type':item['event_type'],'payload':bounded(item['payload'])}
            if isinstance(entry['payload'],dict):
                while len(json.dumps(entry,ensure_ascii=False,default=str))>900 and entry['payload']:
                    entry['payload'].pop(next(reversed(entry['payload'])))
                    entry['payload_omitted']=True
            packet['music_events'].append(entry)
            if len(encoded())>1800:
                packet['music_events'].pop();break
        invitation=self.engine.pending_invitation()
        if invitation:
            packet['invitation']=bounded({k:invitation.get(k) for k in ('id','song','reason','expires_at')})
            if len(encoded())>1800:packet.pop('invitation')
        for key in ('session_intent','energy','emotion','closeness','tags','memory_terms','weather','updated_at','parameter_evidence'):
            if key not in state:continue
            packet['selection_state'][key]=bounded(state[key])
            if len(encoded())>1800:packet['selection_state'].pop(key)
        message=encoded()
        parts=getattr(req,'extra_user_content_parts',None)
        if not isinstance(parts,list):return
        if any(getattr(p,'text','').startswith('[本轮音乐状态]') for p in parts):return
        from astrbot.core.agent.message import TextPart
        parts.append(TextPart(text='[本轮音乐状态]'+message).mark_as_temp())
        event.set_extra('cove_music_event_seqs',[e['seq'] for e in packet['music_events']])

    @hook('on_agent_begin')
    async def on_music_agent_begin(self,event: AstrMessageEvent,run_context: Any=None):
        if not self.can_manage(event) or event.get_extra('cove_room_agent',False):return
        try:
            # AstrBot currently exposes its per-UMO runner through this registry.
            # Guard the adapter: no patch is applied to shared provider instances.
            from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS
            from .core.metering import SharedTurnMeter
            runner=_ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
            if runner is None:raise RuntimeError('runner_unavailable')
            if isinstance(runner.provider,SharedTurnMeter):return
            meter=SharedTurnMeter(runner.provider,self.store,event,self.store.scope_key(),self.settings.token_daily_limit)
            runner.provider=meter
            runner.fallback_providers=[SharedTurnMeter(p,self.store,event,meter.scope,meter.limit,meter.ledger) for p in getattr(runner,'fallback_providers',[])]
            event.set_extra('cove_shared_meter',meter)
            self.store.set('shared_meter_state','request_level')
        except Exception:
            self.store.set('shared_meter_state','unavailable')

    @hook('on_llm_response')
    async def on_music_llm_response(self,event: AstrMessageEvent,response: Any=None):
        meter=event.get_extra('cove_shared_meter',None)
        if meter:meter.flush()
        elif event.get_extra('cove_shared_music_used',False):
            # Do not silently report only the final continuation as a whole turn.
            self.store.set('token_meter_unavailable',day_key())

    @hook('after_message_sent')
    async def after_music_message_sent(self,event: AstrMessageEvent,*args,**kwargs):
        seqs=getattr(event,'get_extra',lambda *_:[])('cove_music_event_seqs',[]) or []
        if seqs and self.can_manage(event):self.store.acknowledge_music_events(seqs)

