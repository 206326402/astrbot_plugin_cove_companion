from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import timedelta
from typing import Any, Awaitable, Callable, Mapping

from .config import Settings, day_key, now
from .cove_client import CoveAmbiguousError, CoveAPIError, CoveClient
from .lyrics import current_lyric_window, estimated_progress_ms, parse_lrc
from .store import CompanionStore
from .text import clean, compact, parse_music_command


from .lifecycle import Lifecycle, fresh
from .recommendations import Recommendations
from .invitations import Invitations
from .catalog_search import CatalogueSearch
from .playback_companion import PlaybackCompanion


class CompanionEngine(Lifecycle, Recommendations, Invitations, CatalogueSearch, PlaybackCompanion):
    def __init__(
        self,
        *,
        context: Any,
        config: Any,
        settings: Settings,
        store: CompanionStore,
        client: CoveClient,
        agent: Any,
        logger: Any,
        qq_send: Callable[[str, str], Awaitable[Any]],
        room_summary: Callable[[str, Mapping[str, Any]], Awaitable[Any]] | None = None,
    ):
        self.context = context
        self.config = config
        self.settings = settings
        self.store = store
        self.store.binding(settings.owner_umo)
        self.client = client
        self.agent = agent
        self.logger = logger
        self.qq_send = qq_send
        self.room_summary = room_summary
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stop = asyncio.Event()
        self._work_lock = asyncio.Lock()
        self._lease_lock = asyncio.Lock()
        self._lease: dict[str, Any] | None = None
        self._lease_renew_at = 0.0
        self._lease_holder = "astrbot_plugin_cove_companion:" + str(uuid.uuid4())
        self._snapshot: dict[str, Any] = {}
        self._snapshot_at = 0.0
        self._snapshot_error: str | None = None
        self._chat_buffer: list[dict[str, Any]] = []
        self._chat_first_at = 0.0
        self._chat_last_at = 0.0
        self._chat_flush_task: asyncio.Task[Any] | None = None
        self._proactive_task: asyncio.Task[Any] | None = None
        self._proactive_song_id: str | None = None
        self._last_proactive_at = 0.0
        self._last_playback_song: str | None = None
        self._last_queue_fingerprint: str | None = None
        self._lyrics_cache: dict[str, dict[str, Any]] = {}
        self._queue_details: dict[str, dict[str, Any]] = {}
        self._queue_details_at = 0.0
        self._player_state_cache: dict[str, Any] | None = None
        self._player_state_cache_at = 0.0

    @property
    def owner_umo(self) -> str:
        return self.settings.owner_umo

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings
        if self.agent is not None:
            self.agent.settings = settings

    def start(self) -> None:
        self._spawn(self._event_loop(), "cove-event-loop")
        self._spawn(self._scheduler_loop(), "cove-scheduler-loop")
        self._spawn(self._lease_loop(), "cove-lease-loop")
        self._spawn(self._maintenance_loop(), "cove-maintenance-loop")
        self._spawn(self._job_loop(), "cove-job-loop")

    def _spawn(self, awaitable: Awaitable[Any], name: str) -> None:
        try:
            task = asyncio.create_task(awaitable, name=name)
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def stop(self) -> None:
        self._stop.set()
        if self._proactive_task and not self._proactive_task.done():
            self._proactive_task.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            if self._lease:
                await self.client.release(str(self._lease.get("lease_id")), int(self._lease.get("fencing_token") or 0))
        except Exception:
            pass
        await self.client.close()

    async def _event_loop(self) -> None:
        while not self._stop.is_set():
            if not self.settings.enabled:
                await asyncio.sleep(2)
                continue
            try:
                await self._ensure_lease()
                cursor = self.store.cursor("cove")
                result = await self.client.events(int(cursor.get("cursor") or 0), limit=100, wait=8000)
                if result.get("code") == "cursor_expired":
                    self.store.set_cursor("cove", int(result.get("event_cursor") or 0), int(cursor.get("cursor") or 0), int(result.get("event_cursor") or 0))
                    self.store.audit("cove", "event_gap", {"from": cursor.get("cursor"), "to": result.get("event_cursor")})
                    await self.refresh_snapshot(True)
                else:
                    for event in result.get("events") or []:
                        active = self.store.active_session()
                        if event.get("event_type") not in {"playback_progress","lyrics_scroll","progress"}:
                            self.store.ingest_event("cove", event, active.get("id") if active else None)
                    self.store.set_cursor("cove", int(result.get("next") or cursor.get("cursor") or 0))
                await self._process_pending_events()
                await self._maybe_flush_chat()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("warning", "event loop failed: %s", type(exc).__name__)
                await asyncio.sleep(2)

    async def _lease_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.settings.enabled:
                    await self._ensure_lease()
                    await self._renew_lease_if_needed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("warning", "lease heartbeat failed: %s", type(exc).__name__)
            await asyncio.sleep(5)

    async def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.settings.enabled:
                    today = day_key()
                    if str(self.store.get("last_maintenance_day", "")) != today:
                        backup_dir = self.store.path.parent / "backups"
                        self.store.daily_backup(backup_dir, keep=self.settings.backup_keep)
                        self.store.purge_old_data(self.settings.retention_days)
                        self.store.optimize_if_needed()
                        self.store.set("last_maintenance_day", today)
                        self.store.audit("system", "daily_maintenance", {"day": today, "retention_days": self.settings.retention_days})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("warning", "daily maintenance failed: %s", type(exc).__name__)
            await asyncio.sleep(3600)

    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._lifecycle_tick()
                if self.settings.enabled:
                    await self._ensure_lease()
                    await self._renew_lease_if_needed()
                    await self._expire_session_if_needed()
                    await self._companion_tick()
                    await self._schedule_recommendation()
                    await self._reconcile_playback_history()
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("warning", "scheduler loop failed: %s", type(exc).__name__)
                await asyncio.sleep(3)

    async def _job_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.store.claim_due_job(["recommendation","room_summary"]) if self.settings.enabled else None
                if job:
                    if job.get("kind")=="room_summary":
                        try:
                            await self._commit_session_summary(self.store.session(job["session_id"]))
                            self.store.finish_job(job["id"],{"state":"confirmed"})
                        except Exception as exc:
                            if int(job.get("attempts") or 0)>=3:self.store.finish_job(job["id"],error=type(exc).__name__)
                            else:self.store.retry_job(job["id"],type(exc).__name__,delay=30)
                    else:await self._run_recommendation_job(job)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("warning", "job worker failed: %s", type(exc).__name__)
            await asyncio.sleep(1)

    def _log(self, level: str, message: str, *args: Any) -> None:
        try:
            getattr(self.logger, level)("[cove-companion] " + message, *args)
        except Exception:
            pass

    async def _ensure_lease(self) -> dict[str, Any]:
        async with self._lease_lock:
            if self._lease and time.time() < self._lease_renew_at:
                return self._lease
            if self._lease:
                try:
                    self._lease = await self.client.renew(
                        str(self._lease.get("lease_id")),
                        int(self._lease.get("fencing_token") or 0),
                    )
                    self._lease_renew_at = time.time() + 10
                    return self._lease
                except Exception:
                    # Do not immediately claim again after a failed renewal:
                    # the old lease may still be live on the server.
                    self._lease = None
                    self._lease_renew_at = time.time() + 2
                    raise
            if time.time() < self._lease_renew_at:
                raise CoveAPIError("lease_retry_wait", "控制租约续期失败，等待后重试。", retryable=True)
            try:
                lease = await self.client.claim(self._lease_holder)
                self._lease = lease
                self._lease_renew_at = time.time() + 10
                return lease
            except CoveAPIError as exc:
                if exc.code == "lease_held":
                    self._lease = None
                    raise
                raise

    async def _renew_lease_if_needed(self) -> None:
        async with self._lease_lock:
            if not self._lease or time.time() < self._lease_renew_at:
                return
            try:
                self._lease = await self.client.renew(str(self._lease.get("lease_id")), int(self._lease.get("fencing_token") or 0))
                self._lease_renew_at = time.time() + 10
            except Exception as exc:
                self._log("warning", "lease renew failed: %s", type(exc).__name__)
                self._lease = None

    async def _expire_session_if_needed(self) -> None:
        # Legacy timer fields are retained for migration, not automatic exits.
        return

    async def refresh_snapshot(self, fresh: bool = False) -> dict[str, Any]:
        snapshot = await self.client.snapshot(fresh)
        self._snapshot = snapshot
        self._snapshot_at = time.time()
        self._snapshot_error = None
        session = self.store.active_session()
        if session and snapshot.get("room_id") == session.get("room_id"):
            self.store.update_session(session["id"], room_epoch=snapshot.get("room_epoch"), playback_state=(snapshot.get("playback") or {}).get("status") or "UNKNOWN")
            self._reconcile_pending_recommendation(session, snapshot)
        return snapshot

    def _reconcile_pending_recommendation(self, session: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
        pending = self.store.pending_recommendation(session["id"])
        if not pending or pending.get("state") not in {"queued", "queued_intro_sent", "queued_intro_unknown"}:
            return
        target = str(pending.get("song_id") or "")
        current = str((snapshot.get("current_song") or {}).get("id") or "")
        evidence = snapshot.get("evidence") or {}
        if current == target and (snapshot.get("playback") or {}).get("status") == "PLAY" and (evidence.get("playback") or {}).get("stale") is False:
            self.store.set_recommendation(pending["id"], "played", evidence={"playback_observation": {"song_id": current, "observed_at": (evidence.get("playback") or {}).get("observed_at")}})
        elif target not in [str(value) for value in (snapshot.get("queue") or {}).get("song_ids") or []] and (evidence.get("queue") or {}).get("stale") is False:
            self.store.set_recommendation(pending["id"], "cancelled", evidence={"reason": "removed_from_queue"})

    async def snapshot(self, fresh: bool = False) -> dict[str, Any]:
        if not fresh and not self._snapshot_error and self._snapshot and time.time() - self._snapshot_at < 5:
            return self._snapshot
        try:
            return await self.refresh_snapshot(fresh)
        except Exception as exc:
            self._snapshot_error = type(exc).__name__
            if fresh:
                raise
            return {**self._snapshot, "connected": False, "evidence": {
                name: {**(self._snapshot.get("evidence", {}).get(name) or {}), "stale": True}
                for name in {"room", "members", "queue", "playback", *self._snapshot.get("evidence", {})}
            }}

    async def bootstrap_status(self) -> dict[str, Any]:
        session = self.store.active_session()
        snapshot = await self.snapshot(False)
        cove: dict[str, Any] | None = None
        error = None
        try:
            cove = await self.client.bootstrap()
        except Exception as exc:
            error = type(exc).__name__
        return {
            "enabled": self.settings.enabled,
            "session": session,
            "snapshot": snapshot,
            "cove": cove,
            "cove_error": error,
            "lease": self._lease,
            "settings": self.settings.public(),
            "health": self.store.health(),
        }

    async def queue_page(self, offset: int | None = None, *, snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
        snapshot = snapshot if snapshot is not None else await self.snapshot(False)
        current = dict(snapshot.get("current_song") or {})
        all_ids = [str(item) for item in ((snapshot.get("queue") or {}).get("song_ids") or [])]
        current_id = str(current.get("id") or "")
        current_index = all_ids.index(current_id) if current_id in all_ids else -1
        page_start = max(0, current_index) // 50 * 50 if offset is None else max(0, int(offset))
        page_start = min(page_start, max(0, (len(all_ids) - 1) // 50 * 50))
        queue_ids = all_ids[page_start:page_start + 50]
        now_ts = time.time()
        if now_ts - self._queue_details_at > 1800 or len(self._queue_details) > 200:
            self._queue_details = {}
        missing = [song_id for song_id in queue_ids if song_id not in self._queue_details]
        if missing:
            try:
                songs = await self.client.tracks(missing)
                self._queue_details.update({str(song.get("id")): dict(song) for song in songs if song.get("id") is not None})
                self._queue_details_at = now_ts
            except Exception:
                pass
        queue_items = []
        for index, song_id in enumerate(queue_ids):
            song = self._queue_details.get(song_id) or (current if song_id == current_id else {})
            queue_items.append({
                "id": song_id,
                "name": song.get("name") or ("当前歌曲" if song_id == current_id else "歌曲信息暂不可用"),
                "artist": song.get("artist") or "",
                "duration_ms": song.get("duration_ms") or song.get("durationMs") or 0,
                "cover_url": song.get("cover_url") or song.get("coverUrl") or "",
                "position": page_start + index + 1,
                "is_current": song_id == current_id,
                "is_next": current_index >= 0 and page_start + index == current_index + 1,
            })
        return {"items": queue_items, "song_ids": queue_ids, "offset": page_start, "limit": 50, "total": len(all_ids), "current_position": current_index + 1 if current_index >= 0 else None, "play_mode": (snapshot.get("queue") or {}).get("play_mode"), "fingerprint": (snapshot.get("queue") or {}).get("fingerprint")}

    async def player_state(self, *, force: bool = False) -> dict[str, Any]:
        now_ts = time.time()
        if not force and self._player_state_cache and now_ts - self._player_state_cache_at < 0.8:
            return self._player_state_cache
        snapshot = await self.snapshot(force)
        session = self.store.active_session()
        current = dict(snapshot.get("current_song") or {})
        playback = dict(snapshot.get("playback") or {})
        queue = await self.queue_page(snapshot=snapshot)
        current_id = str(current.get("id") or "")
        lyric = await self.current_lyric(current_id) if current_id.isdigit() else None
        if lyric:
            timeline = await self.lyric_lines(current_id)
            lyric["timeline"] = [
                {"time_ms": int(line.get("time_ms") or line.get("timeMs") or 0), "text": str(line.get("text") or "")}
                for line in timeline[:500]
                if line.get("text")
            ]
        health = self.store.health()
        usage = self.store.usage_summary()
        state = {
            "server_time": int(time.time() * 1000),
            "protocol": "astrbot-cove/1",
            "session": session,
            "snapshot": snapshot,
            "song": current or None,
            "playback": playback,
            "queue": queue,
            "lyric": lyric,
            "pending_recommendation": self.store.pending_recommendation(session["id"]) if session else None,
            "recent_operations": self.store.recent_operations_context(session_id=session["id"] if session else None, limit=5),
            "recent_agent_turns": self.store.agent_turns(3),
            "recent_summaries": self.store.room_summaries(3, session_id=session["id"] if session else None),
            "health": {
                "enabled": self.settings.enabled,
                "store": health,
                "usage": usage,
                "cove_error": self._snapshot_error,
            },
        }
        self._player_state_cache = state
        self._player_state_cache_at = now_ts
        return state

    def status_text(self) -> str:
        session = self.store.active_session()
        snapshot = self._snapshot or {}
        current = snapshot.get("current_song") or {}
        playback = snapshot.get("playback") or {}
        if not session:
            return "网易云陪听：未在房间。发送双人邀请即可开始。\nCove：" + ("已连接" if snapshot else "等待状态")
        return "\n".join([
            f"网易云陪听：{session.get('phase')}",
            f"房间：{session.get('room_id') or '未知'}",
            f"当前：{current.get('name') or '等待播放'} - {current.get('artist') or ''}",
            f"播放：{playback.get('status') or 'UNKNOWN'}，推荐{'开' if session.get('autodj_enabled') else '关'}，聊天{'开' if session.get('room_chat_enabled') else '关'}",
            f"下次推荐：{self._time_text(session.get('next_recommend_at'))}",
            f"推荐状态：{session.get('blocked_reason') or '正常等待'}，重试：{self._time_text(session.get('blocked_until') or session.get('next_eligibility_check_at'))}",
        ])

    def diagnose_text(self) -> str:
        turns = self.store.agent_turns(3)
        overrides = self.store.external_overrides(3)
        snapshot = self._snapshot or {}
        compact_turns = []
        for turn in turns:
            compact_turns.append({
                "kind": turn.get("kind"),
                "trigger": turn.get("trigger"),
                "state": turn.get("state"),
                "persona": turn.get("persona_id"),
                "provider": turn.get("provider_id"),
                "provider_attempts": turn.get("input", {}).get("provider_attempts") or [],
                "context_bytes": turn.get("context_bytes"),
                "context": turn.get("input", {}).get("context") or {},
                "tools": [{"name": tool.get("name"), "state": tool.get("state")} for tool in turn.get("tool_calls") or []],
                "error": turn.get("error"),
                "started_at": turn.get("started_at"),
                "finished_at": turn.get("finished_at"),
                "duration_ms": int(max(0.0, float(turn.get("finished_at") or turn.get("started_at") or 0) - float(turn.get("started_at") or 0)) * 1000) if turn.get("finished_at") else None,
            })
        session = self.store.active_session()
        scheduler = {
            "autodj_enabled": bool(session.get("autodj_enabled")) if session else False,
            "next_recommend_at": session.get("next_recommend_at") if session else None,
            "now": time.time(),
            "overdue": bool(session and session.get("next_recommend_at") and float(session["next_recommend_at"]) <= time.time()) if session else False,
            "recent_jobs": self.store.jobs(5),
        }
        return json.dumps({
            "worker": {
                "phase": (snapshot.get("worker") or {}).get("phase"),
                "room_id": snapshot.get("room_id"),
                "nim_connected": snapshot.get("nim_connected"),
                "account_id": (snapshot.get("worker") or {}).get("account_id"),
            },
            "scheduler": scheduler,
            "recent_agent_turns": compact_turns,
            "recent_external_overrides": overrides,
            "budget": self.store.usage_summary(),
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def _time_text(timestamp: Any) -> str:
        if not timestamp:
            return "未安排"
        try:
            return time.strftime("%H:%M:%S", time.localtime(float(timestamp)))
        except (TypeError, ValueError):
            return "未安排"

    async def join(self, room_id: str, inviter_id: str, *, max_minutes: int | None = None, origin: str = "qq_command") -> dict[str, Any]:
        room_id = str(room_id or "").strip()
        inviter_id = str(inviter_id or "").strip()
        if not self.settings.enabled:
            return {"ok": False, "message": "陪听插件总开关尚未启用。"}
        if not self.settings.owner_netease_uid:
            return {"ok": False, "message": "尚未配置主人网易云 UID。"}
        if inviter_id != self.settings.owner_netease_uid:
            return {"ok": False, "message": "邀请者与已配置的主人网易云 UID 不一致，未加入。"}
        if not room_id:
            return {"ok": False, "message": "邀请里没有可靠的房间 ID，未加入。"}
        current = self.store.active_session()
        if current:
            current_room = str(current.get("room_id") or "")
            if current_room == room_id and current.get("phase") == "active":
                observed=await self.snapshot(True)
                if not (fresh(observed,'room') and fresh(observed,'members') and observed.get('room_id')==room_id and observed.get('connected') is True and (observed.get('owner_binding') or {}).get('present') is True):return {'ok':False,'state':'unknown','message':'房间状态需要重新核验，未重复加入。'}
                return {"ok": True, "already_joined": True, "message": "已经在这个一起听房间里了。", "session_id": current.get("id")}
            if current_room == room_id and current.get("phase") == "joining":
                return {"ok": False, "joining": True, "message": "正在加入这个一起听房间，请等候上一条回执。", "session_id": current.get("id")}
            if current_room and current_room != room_id:
                return {"ok": False, "message": "当前仍在另一个一起听房间，退出未确认，未切换。"}
        observed=await self.snapshot(True)
        if not fresh(observed,'room') or (observed.get('room_id') and observed.get('room_id')!=room_id):
            return {'ok':False,'state':'unknown','message':'未确认远端房间为空或为目标房间，未下发加入。'}
        session = self.store.create_session(
            self.settings.owner_umo,
            room_id,
            None,
            self.settings.autodj_enabled,
            self.settings.room_chat_enabled,
            max_minutes if max_minutes is not None else self.settings.session_max_minutes,
            await self.agent.current_persona_id(),
        )
        try:
            result = await self.execute_verified(
                "JOIN",
                {"room_id": room_id, "inviter_id": inviter_id},
                session=session,
                origin_kind=origin,
                request_id="join:" + session["id"],
                timeout=35,
            )
            if result.get("state") != "confirmed":
                if result.get("state") == "unknown":
                    return {"ok":False,"state":"joining","message":"加入回执未确认，正在限时核对。","session_id":session["id"]}
                raise RuntimeError(self._operation_error(result))
            snapshot = await self.refresh_snapshot(True)
            room_epoch = snapshot.get("room_epoch")
            first_at = time.time() + self.settings.first_recommendation_delay_seconds
            ready = fresh(snapshot,"room") and snapshot.get("connected") is True and snapshot.get("room_id")==room_id
            self.store.bind_session(session["id"], self.owner_umo, (snapshot.get("bot_account") or {}).get("uid"))
            self.store.update_session(session["id"], phase="active" if ready else "joining", room_epoch=room_epoch)
            if ready:self._session_started(self.store.session(session["id"]))
            self.store.set_recommendation_due(session["id"], first_at)
            return {"ok": True, "message": "已加入一起听。" if ready else "加入回执已确认，等待可靠房间快照。", "session_id": session["id"]}
        except Exception as exc:
            try:observed=await self.snapshot(True)
            except Exception:observed={}
            if fresh(observed,'room') and not observed.get('room_id'):await self._finish_session(session,'join_failed','confirmed_not_in_room')
            # Unknown join outcome is reconciled; it is not an automatic exit.
            return {"ok": False, "message": f"加入失败：{clean(exc, 300)}"}

    async def execute(
        self,
        action: str,
        args: Mapping[str, Any],
        *,
        session: Mapping[str, Any] | None = None,
        origin_kind: str = "qq_command",
        request_id: str | None = None,
        timeout: float = 30,
        expected_decision_revision: int | None = None,
    ) -> dict[str, Any]:
        session = dict(session or self.store.active_session() or {})
        if not session:
            raise RuntimeError("没有有效的陪听会话")
        async with self._work_lock:
            current = self.store.active_session()
            if not current or current["id"] != session["id"]:
                return {"state": "cancelled", "error": {"code": "session_changed", "message": "陪听会话已变化，旧操作已取消。"}}
            if expected_decision_revision is not None and int(current.get("decision_revision") or 0) != int(expected_decision_revision):
                return {"state": "cancelled", "error": {"code": "stale_decision", "message": "房间意图已变化，旧推荐结果已取消。"}}
            if (not self.settings.enabled and action != "LEAVE") or (origin_kind == "autodj" and not current.get("autodj_enabled")) or (origin_kind == "room_reply" and not current.get("room_chat_enabled")):
                return {"state": "cancelled", "error": {"code": "feature_disabled", "message": "相关功能已关闭，旧操作已取消。"}}
            if origin_kind not in {"autodj"} and action in {"ENQUEUE_NEXT", "PLAY_NOW", "NEXT", "PAUSE", "RESUME", "CANCEL_QUEUED_RECOMMENDATION", "LEAVE"}:
                self.store.bump_decision_revision(session["id"])
            await self._ensure_lease()
            assert self._lease is not None
            snapshot = await self.snapshot(True)
            if action not in {"JOIN","CREATE"} and (snapshot.get("room_id") != current.get("room_id") or (current.get("room_epoch") and snapshot.get("room_epoch") != current.get("room_epoch"))):
                return {"state": "cancelled", "error": {"code": "room_changed", "message": "房间状态已变化，未发送旧操作。"}}
            if current.get("phase") == "leaving" and action != "LEAVE":
                return {"state":"cancelled","reason":"session_leaving"}
            binding = self.store.binding(self.owner_umo)
            if int(current.get("binding_revision") or 1) != binding["revision"]:
                return {"state":"cancelled","reason":"binding_changed"}
            operation_id = str(uuid.uuid4())
            request = {
                "operation_id": operation_id,
                "lease_id": self._lease["lease_id"],
                "fencing_token": int(self._lease["fencing_token"]),
                "session_id": session["id"],
                "deadline_at": int((time.time()+min(45, timeout))*1000),
                "control_generation": int(current.get("control_generation") or 1),
                "binding_revision": int(current.get("binding_revision") or 1),
                "session_version": int(current.get("session_version") or 1),
                "origin": {"kind": origin_kind, "request_id": request_id or operation_id},
                "preconditions": {
                    "room_id": snapshot.get("room_id") if action not in {"JOIN","CREATE"} else None,
                    "room_epoch": snapshot.get("room_epoch") if action not in {"JOIN","CREATE"} else None,
                    "room_revision": snapshot.get("room_revision") if action not in {"JOIN","CREATE"} else None,
                    "queue_fingerprint": ((snapshot.get("queue") or {}).get("fingerprint")) if action in {"ENQUEUE_NEXT", "CANCEL_QUEUED_RECOMMENDATION"} else None,
                },
                "action": action,
                "args": dict(args),
            }
            self.store.track_operation(operation_id, current)
            submitted_at=time.monotonic()
            try:
                accepted = await asyncio.wait_for(self.client.command(request),timeout=min(45,timeout))
            except (CoveAmbiguousError,asyncio.TimeoutError):
                self.store.record_operation(operation_id, session["id"], action, origin_kind, request["origin"]["request_id"], "unknown")
                return {"operation_id": operation_id, "state": "unknown", "error": {"code": "remote_result_unknown", "message": "提交命令时连接中断，结果未知；不会自动重试。"}}
            self.store.record_operation(operation_id, session["id"], action, origin_kind, request["origin"]["request_id"], str(accepted.get("state") or "accepted"))
            try:
                final = await self.client.wait_operation(operation_id, deadline_seconds=max(.1,min(45,timeout)-(time.monotonic()-submitted_at)))
            except (CoveAmbiguousError,asyncio.TimeoutError):
                final = {"operation_id": operation_id, "state": "unknown", "error": {"code": "receipt_unavailable", "message": "操作已提交，但回执读取中断；请先核对，不会重复发送。"}}
            self.store.record_operation(
                operation_id,
                session["id"],
                action,
                origin_kind,
                request["origin"]["request_id"],
                str(final.get("state") or "unknown"),
                result=final,
            )
            if final.get("state") == "unknown":
                self.store.update_session(session["id"], autodj_enabled=0)
                self.store.audit("cove", "unknown_operation", {"operation_id": operation_id, "action": action})
            return final

    async def execute_verified(
        self,
        action: str,
        args: Mapping[str, Any],
        *,
        session: Mapping[str, Any] | None = None,
        origin_kind: str = "qq_command",
        request_id: str | None = None,
        timeout: float = 45,
        expected_decision_revision: int | None = None,
    ) -> dict[str, Any]:
        session = dict(session or self.store.active_session() or {})
        before = await self.snapshot(True)
        operation = await self.execute(
            action,
            args,
            session=session,
            origin_kind=origin_kind,
            request_id=request_id,
            timeout=timeout,
            expected_decision_revision=expected_decision_revision,
        )
        if operation.get("state") != "confirmed":
            return operation
        await asyncio.sleep(0.25)
        after = await self.snapshot(True)
        check = self._verify_snapshot(action, args, before, after)
        operation["snapshot_verified"] = check["ok"]
        operation["snapshot_evidence"] = check
        if check["ok"]:
            self.store.record_operation(
                str(operation.get("operation_id") or ""),
                session.get("id"),
                action,
                origin_kind,
                request_id or "",
                str(operation.get("state") or "confirmed"),
                result=operation,
            )
            return operation
        operation["state"] = "unknown"
        operation["error"] = {
            "code": "snapshot_mismatch",
            "message": check["reason"],
            "retryable": False,
        }
        self.store.record_operation(
            str(operation.get("operation_id") or ""),
            session.get("id"),
            action,
            origin_kind,
            request_id or "",
            "unknown",
            result=operation,
        )
        self.store.update_session(session["id"], autodj_enabled=0)
        return operation

    def _verify_snapshot(self, action: str, args: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
        target = str(args.get("song_id") or "")
        before_song = str((before.get("current_song") or {}).get("id") or "")
        after_song = str((after.get("current_song") or {}).get("id") or "")
        before_status = str((before.get("playback") or {}).get("status") or "UNKNOWN")
        after_status = str((after.get("playback") or {}).get("status") or "UNKNOWN")
        before_queue = list((before.get("queue") or {}).get("song_ids") or [])
        after_queue = list((after.get("queue") or {}).get("song_ids") or [])
        if action == "ENQUEUE_NEXT":
            current = after_song or before_song
            index = after_queue.index(current) if current in after_queue else -1
            next_song = after_queue[index + 1] if 0 <= index < len(after_queue) - 1 else None
            before_fingerprint = str((before.get("queue") or {}).get("fingerprint") or "")
            after_fingerprint = str((after.get("queue") or {}).get("fingerprint") or "")
            version_or_order_changed = (
                after_queue != before_queue
                or (before_fingerprint and after_fingerprint and before_fingerprint != after_fingerprint)
            )
            ok = next_song == target and bool(version_or_order_changed)
            return {"ok": ok, "reason": f"下一首不是目标歌曲：{next_song or '无'}", "next_song_id": next_song}
        if action == "PLAY_NOW":
            ok = after_song == target and after_status == "PLAY"
            return {"ok": ok, "reason": f"当前歌曲/状态未确认：{after_song}/{after_status}", "song_id": after_song, "play_status": after_status}
        if action == "PAUSE":
            ok = fresh(after,"playback") and after_status == "PAUSE"
            return {"ok": ok, "reason": f"暂停状态未确认：{after_status}", "play_status": after_status}
        if action == "RESUME":
            ok = fresh(after,"playback") and after_status == "PLAY"
            return {"ok": ok, "reason": f"播放状态未确认：{after_status}", "play_status": after_status}
        if action == "NEXT":
            ok = bool(after_song) and after_song != before_song and after_status == "PLAY"
            return {"ok": ok, "reason": f"下一首未确认：{before_song}->{after_song}", "song_id": after_song}
        if action == "CANCEL_QUEUED_RECOMMENDATION":
            ok = target not in after_queue
            return {"ok": ok, "reason": f"目标仍在队列中：{target}", "queue": after_queue}
        if action == "LEAVE":
            ok = fresh(after,"room") and not after.get("room_id")
            return {"ok": ok, "reason": f"仍在房间：{after.get('room_id')}", "room_id": after.get("room_id")}
        if action == "JOIN":
            target_room = str(args.get("room_id") or "")
            room_evidence = (after.get("evidence") or {}).get("room") or {}
            room_fresh = fresh(after,"room")
            ok = str(after.get("room_id") or "") == target_room and after.get("connected") is True and room_fresh
            reason = "房间和连接已确认" if ok else f"房间或连接未确认：{after.get('room_id')} / connected={after.get('connected')}"
            return {"ok": ok, "reason": reason, "room_id": after.get("room_id"), "connected": after.get("connected")}
        return {"ok": True, "reason": "无需快照核对"}

    def _operation_error(self, operation: Mapping[str, Any]) -> str:
        error = operation.get("error") or {}
        return str(error.get("message") or operation.get("state") or "操作未确认")

    async def _send_room(self, session: Mapping[str, Any], text: str, *, origin: str = "room_reply", request_id: str | None = None) -> dict[str, Any]:
        normalized = clean(text, 500)
        result = await self.execute(
            "SEND_MESSAGE",
            {"text": normalized},
            session=session,
            origin_kind=origin,
            request_id=request_id or str(uuid.uuid4()),
            timeout=20,
        )
        if result.get("state") == "confirmed":
            self.store.record_room_message(
                session_id=str(session.get("id") or ""),
                provider_message_id=str(((result.get("result") or {}).get("messageId")) or "") or None,
                sender_uid=None,
                sender_role="bot",
                text=normalized,
                direction="outbound",
                state="sent",
            )
        return result

    async def set_autodj(self, enabled: bool) -> str:
        session = self.store.active_session()
        if not session:
            return "当前没有陪听会话。"
        self.store.bump_decision_revision(session["id"])
        self.store.update_session(session["id"], autodj_enabled=int(enabled))
        if enabled:
            next_at = time.time() + self.settings.recommendation_interval_seconds
            self.store.set_recommendation_due(session["id"], next_at)
        else:
            self.store.clear_recommendation_block(session["id"])
        return "自动推荐已开启，仍只排下一首。" if enabled else "已停止后续自动推荐，当前歌曲和已排队歌曲保持不动。"

    async def set_room_chat(self, enabled: bool) -> str:
        session = self.store.active_session()
        if not session:
            return "当前没有陪听会话。"
        self.store.update_session(session["id"], room_chat_enabled=int(enabled))
        return "网易云房间聊天已开启。" if enabled else "网易云房间聊天已关闭。"

    async def recommend_once(self) -> str:
        session = self.store.active_session()
        if not session:
            return "当前没有陪听会话。"
        try:
            result = await asyncio.wait_for(
                self._run_recommendation(session, manual=True),
                timeout=max(20, float(self.settings.agent_timeout_seconds) + 15),
            )
        except asyncio.TimeoutError:
            return "推荐超时：没有拿到可靠回执，未确认排队。"
        except Exception as exc:
            return "推荐失败：" + clean(exc, 240)
        if result.get("state") == "confirmed":
            return "已确认排好下一首。"
        reason = str(result.get("reason") or result.get("error") or "暂时没有合适的推荐")
        return "这次没有排入下一首：" + clean(reason, 240)

    async def cancel_recommendation_result(self) -> dict[str, Any]:
        session = self.store.active_session()
        if not session:
            return {"state": "rejected", "error": {"code": "no_session", "message": "当前没有陪听会话。", "retryable": False}}
        pending = self.store.pending_recommendation(session["id"])
        if not pending:
            return {"state": "rejected", "error": {"code": "no_pending_recommendation", "message": "没有待播的自动推荐。", "retryable": False}}
        if pending.get("state") in {"selecting", "enqueueing"}:
            self.store.bump_decision_revision(session["id"])
            self.store.set_recommendation(pending["id"], "cancelled", evidence={"reason": "user_cancelled_before_enqueue"})
            return {"state": "confirmed", "recommendation_id": pending["id"], "result": {"cancelled_before_enqueue": True}}
        operation = await self.execute_verified(
            "CANCEL_QUEUED_RECOMMENDATION",
            {"song_id": pending["song_id"]},
            session=session,
            origin_kind="qq_command",
            request_id="cancel:" + pending["id"],
            timeout=30,
        )
        if operation.get("state") == "confirmed":
            self.store.set_recommendation(pending["id"], "cancelled", evidence=operation.get("result") or {})
        return operation

    async def cancel_pending_recommendation(self) -> str:
        result = await self.cancel_recommendation_result()
        if result.get("state") == "confirmed":
            return "已取消待播推荐。"
        return "取消未确认：" + self._operation_error(result)

    async def _control_result(self, action: str, *, origin: str = "qq_command", timeout: float = 30) -> dict[str, Any]:
        session = self.store.active_session()
        if not session:
            return {"state": "rejected", "error": {"code": "no_session", "message": "当前没有陪听会话。", "retryable": False}}
        try:
            return await self.execute_verified(
                action,
                {},
                session=session,
                origin_kind=origin,
                request_id=action.lower() + ":" + str(uuid.uuid4()),
                timeout=timeout,
            )
        except Exception as exc:
            return {"state": "unknown", "error": {"code": "operation_exception", "message": clean(exc, 300), "retryable": False}}

    async def pause_result(self, *, origin: str = "qq_command") -> dict[str, Any]:
        return await self._control_result("PAUSE", origin=origin)

    async def resume_result(self, *, origin: str = "qq_command") -> dict[str, Any]:
        return await self._control_result("RESUME", origin=origin)

    async def skip_result(self, *, origin: str = "qq_command") -> dict[str, Any]:
        return await self._control_result("NEXT", origin=origin)

    async def pause(self, *, origin: str = "qq_command") -> str:
        result = await self.pause_result(origin=origin)
        return "已暂停。" if result.get("state") == "confirmed" else "操作未确认：" + self._operation_error(result)

    async def resume(self, *, origin: str = "qq_command") -> str:
        result = await self.resume_result(origin=origin)
        return "已继续播放。" if result.get("state") == "confirmed" else "操作未确认：" + self._operation_error(result)

    async def next_track(self, *, origin: str = "qq_command") -> str:
        result = await self.skip_result(origin=origin)
        return "已切到下一首。" if result.get("state") == "confirmed" else "操作未确认：" + self._operation_error(result)

    async def enqueue_next_result(self, song_id: str, *, origin: str = "qq_command") -> dict[str, Any]:
        session = self.store.active_session()
        if not session:
            return {"state": "rejected", "error": {"code": "no_session", "message": "当前没有陪听会话。", "retryable": False}}
        target = str(song_id or "").strip()
        if not target.isdigit():
            return {"state": "rejected", "error": {"code": "invalid_song_id", "message": "歌曲 ID 必须是数字。", "retryable": False}}
        try:
            return await self.execute_verified(
                "ENQUEUE_NEXT",
                {"song_id": target},
                session=session,
                origin_kind=origin,
                request_id="enqueue:" + str(uuid.uuid4()),
                timeout=30,
            )
        except Exception as exc:
            return {"state": "unknown", "error": {"code": "operation_exception", "message": clean(exc, 300), "retryable": False}}

    async def play_now_result(self, song_id: str, *, origin: str = "qq_command") -> dict[str, Any]:
        session = self.store.active_session()
        if not session:
            return {"state": "rejected", "error": {"code": "no_session", "message": "当前没有陪听会话。", "retryable": False}}
        target = str(song_id or "").strip()
        if not target.isdigit():
            return {"state": "rejected", "error": {"code": "invalid_song_id", "message": "歌曲 ID 必须是数字。", "retryable": False}}
        try:
            return await self.execute_verified(
                "PLAY_NOW",
                {"song_id": target},
                session=session,
                origin_kind=origin,
                request_id="play_now:" + str(uuid.uuid4()),
                timeout=60,
            )
        except Exception as exc:
            return {"state": "unknown", "error": {"code": "operation_exception", "message": clean(exc, 300), "retryable": False}}

    async def enqueue(self, song_id: str, *, play_now: bool = False, origin: str = "qq_command") -> str:
        result = await (self.play_now_result(song_id, origin=origin) if play_now else self.enqueue_next_result(song_id, origin=origin))
        if result.get("state") != "confirmed":
            return "操作未确认：" + self._operation_error(result)
        return "已立即播放。" if play_now else "已排到下一首。"

    async def status_payload(self) -> dict[str, Any]:
        session = self.store.active_session()
        snapshot = await self.snapshot(True)
        lyric = None
        song = snapshot.get("current_song") or {}
        song_id = str(song.get("id") or "")
        if song_id.isdigit():
            lyric = await self.current_lyric(song_id)
        return {
            "state": "confirmed" if session and snapshot.get("room_id") else "rejected",
            "session": session,
            "snapshot": snapshot,
            "lyric": lyric,
            "pending_recommendation": self.store.pending_recommendation(session["id"]) if session else None,
        }

    async def lyric_lines(self, song_id: str) -> list[dict[str, Any]]:
        if not song_id.isdigit():
            return []
        item = self._lyrics_cache.get(song_id)
        if not item or time.time() - float(item.get("at") or 0) > 600:
            try:
                data = await self.client.lyrics(song_id)
                lrc = data.get("lrc") or {}
                lines = lrc.get("lines") or []
                if not lines and lrc.get("raw"):
                    lines = parse_lrc(lrc["raw"])
                item = {"at": time.time(), "lines": lines}
                self._lyrics_cache[song_id] = item
            except Exception:
                return []
        return [dict(line) for line in item.get("lines") or []]

    async def current_lyric(self, song_id: str) -> dict[str, str] | None:
        if not song_id.isdigit():
            return None
        snapshot = await self.snapshot(False)
        lines = await self.lyric_lines(song_id)
        return current_lyric_window(lines, estimated_progress_ms(snapshot))

    async def recommend_from_agent(
        self,
        song: Mapping[str, Any],
        intro: str,
        *,
        manual: bool = False,
        expected_session_id: str | None = None,
        expected_decision_revision: int | None = None,
        selection: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        song_id = str(song.get("id") or song.get("song_id") or "")
        if not manual and self.store.token_summary(self.settings.token_daily_limit)['reached']:
            return {'state':'blocked','reason':'token_budget_reached'}
        session = self.store.active_session()
        if not session:
            return {"state": "rejected", "error": {"code": "no_session", "message": "当前没有陪听会话。", "retryable": False}}
        if expected_session_id and str(session.get("id") or "") != str(expected_session_id):
            return {"state": "cancelled", "error": {"code": "session_changed", "message": "陪听房间已经变化，旧房间的推荐已取消。", "retryable": False}}
        if expected_decision_revision is not None and int(session.get("decision_revision") or 0) != int(expected_decision_revision):
            return {"state": "cancelled", "error": {"code": "stale_decision", "message": "主人刚刚改变了下一首意图，这次推荐已取消。", "retryable": False}}
        if self.store.pending_recommendation(session["id"]):
            return {"state": "rejected", "error": {"code": "pending_recommendation", "message": "已有待播推荐。", "retryable": False}}
        snapshot = await self.snapshot(True)
        selected = {"id": str(song_id), "name": str(song.get("name") or ""), "artist": str(song.get("artist") or ""), "source": "agent_search"}
        if not str(song_id).isdigit():
            return {"state": "rejected", "error": {"code": "invalid_song_id", "message": "歌曲 ID 无效。", "retryable": False}}
        if not manual:
            current_id = str((snapshot.get("current_song") or {}).get("id") or "")
            queue_ids = {str(item) for item in ((snapshot.get("queue") or {}).get("song_ids") or [])}
            if song_id == current_id:
                return {"state": "rejected", "error": {"code": "current_song", "message": "候选就是当前歌曲，等待下一轮。", "retryable": False}}
            if song_id in queue_ids:
                return {"state": "rejected", "error": {"code": "already_in_queue", "message": "候选已经在队列里，等待下一轮。", "retryable": False}}
            recent = self.store.recent_song_ids(self.settings.dedup_minutes)
            if song_id in recent:
                return {"state": "rejected", "error": {"code": "deduplicated", "message": "候选在去重窗口内，等待下一轮。", "retryable": False}}
            haystack = (selected["name"] + " " + selected["artist"]).casefold()
            excluded = [str(term).strip().casefold() for term in self.settings.excluded_terms if str(term).strip()]
            matched = next((term for term in excluded if term and term in haystack), None)
            if matched:
                return {"state": "rejected", "error": {"code": "excluded_term", "message": "候选命中排除词。", "term": matched, "retryable": False}}
        slot_key = ("manual:" if manual else "cycle:" + str(self.store.cycle(session['id'])['runs']) + ":") + uuid.uuid4().hex
        try:
            recommendation = self.store.create_recommendation(session["id"], slot_key, selected, clean(intro, 80), "agent_search")
        except Exception:
            return {"state": "rejected", "error": {"code": "pending_recommendation", "message": "已有待播推荐。", "retryable": False}}
        self.store.set_recommendation(recommendation["id"], "enqueueing", evidence={"snapshot": snapshot.get("queue"),"selection":dict(selection or song)})
        operation = await self.execute_verified(
            "ENQUEUE_NEXT",
            {"song_id": str(song_id)},
            session=session,
            origin_kind="autodj" if not manual else "qq_command",
            request_id="enqueue:" + recommendation["id"],
            timeout=30,
            expected_decision_revision=expected_decision_revision,
        )
        if operation.get("state") != "confirmed":
            rec_state = "unknown" if operation.get("state") == "unknown" else "cancelled"
            self.store.set_recommendation(recommendation["id"], rec_state, evidence=operation)
            return {"state": operation.get("state") or "unknown", "error": operation.get("error"), "recommendation_id": recommendation["id"], "operation": operation}
        self.store.set_recommendation(recommendation["id"], "queued", operation.get("operation_id"), {"operation": operation.get("result") or {}})
        intro_result = None
        if (selection or {}).get('purpose') != 'search':
            from .music_model import introduction
            text=introduction(intro,selected)
            self.store.queue_introduction(self.store.session(session['id']),song_id,text,recommendation_id=recommendation['id'])
            intro_result={'state':'ready','request_id':'intro:'+recommendation['id']}
            self.store.set_recommendation(recommendation['id'],'queued',evidence={'intro_state':'ready','intro':intro_result})
        self.store.decision(session['id'],'enqueue',song_id=song_id,state='confirmed',operation_id=operation.get('operation_id'))
        return {
            "state": "confirmed",
            "recommendation_id": recommendation["id"],
            "operation_id": operation.get("operation_id"),
            "intro": intro_result,
            "operation": operation,
        }

    async def search(self, query: str) -> list[dict[str, Any]]:
        return (await self.search_catalog(query))['songs'][:20]

    async def lyrics(self, song_id: str) -> dict[str, Any]:
        return await self.client.lyrics(song_id)

    async def leave_result(self, *, session_id: str | None = None, reason: str = "user_request") -> dict[str, Any]:
        session = self.store.session(session_id) if session_id else self.store.active_session()
        if not session:
            return {"state": "rejected", "error": {"code": "no_session", "message": "当前没有陪听会话。", "retryable": False}}
        if session.get("phase") == "ended":
            return {"state":"confirmed" if session.get("remote_result")=="confirmed" else "unknown","remote_result":session.get("remote_result")}
        if session.get("phase") == "leaving":
            return {"state":"pending","remote_result":"unknown","reason":"already_leaving"}
        self.store.update_session(session["id"],phase="leaving",leaving_since=time.time(),end_reason=reason,autodj_enabled=0,pause_since=None,control_generation=int(session.get("control_generation") or 1)+1)
        try:
            snap=await self.snapshot(True)
            if fresh(snap,"room") and not snap.get("room_id"):
                result={"state":"confirmed","remote_result":"confirmed","reason":"already_out"}
            else:
                result=await self.execute_verified("LEAVE",{},session=session,origin_kind="qq_command",request_id="leave:"+session["id"],timeout=45)
        except Exception as exc:
            result={"state":"unknown","remote_result":"unknown","reason":type(exc).__name__}
        if result.get("state")=="confirmed":
            await self._finish_session(session,reason,"confirmed")
        elif result.get("state") in {"rejected","cancelled"}:
            await self._finish_session(session,reason,"unknown")
        return result

    async def leave(self, *, session_id: str | None = None, reason: str = "user_request") -> str:
        result = await self.leave_result(session_id=session_id, reason=reason)
        if result.get("state") == "confirmed":
            return "已退出一起听。"
        return "退出未确认：" + self._operation_error(result)

    async def _process_pending_events(self) -> None:
        for row in self.store.pending_events(100):
            try:
                active = self.store.active_session()
                event = row["payload"]
                event_type = str(event.get("event_type") or "")
                if event_type in {"progress", "playback_progress", "lyrics_scroll"}:
                    self.store.mark_event(row["id"], "done")
                    continue
                if event_type == "chat_message":
                    await self._handle_room_chat_event(active, event)
                elif event_type in {"members_changed", "room_changed", "room_joined", "room_left", "connection_changed", "auth_invalid", "auth_changed"}:
                    if event_type == "auth_invalid" and active:
                        self.store.audit('lifecycle','auth_unavailable_waiting',{'session_id':active['id']})
                    else:
                        self._lifecycle_checked_at = 0
                        await self._lifecycle_tick()
                elif event_type in {"playback_changed", "queue_changed"}:
                    await self._handle_state_event(active, event)
                self.store.mark_event(row["id"], "done")
            except Exception as exc:
                self.store.mark_event(row["id"], "failed", clean(exc, 500))
                self._log("warning", "event handling failed: %s", type(exc).__name__)

    async def _handle_state_event(self, session: Mapping[str, Any] | None, event: Mapping[str, Any]) -> None:
        if not self._event_matches_session(session, event):
            return
        payload = event.get("payload") or {}
        attribution = str(payload.get("attribution") or event.get("attribution") or "unknown")
        operation_id = event.get("operation_id") or payload.get("operation_id")
        if operation_id:
            attribution = "plugin"
        event_type = str(event.get("event_type") or "")
        old_song = self._last_playback_song
        new_song = str(payload.get("song_id") or "") or None
        reported_old_song = payload.get("former_song_id") or payload.get("formerSongId")
        if reported_old_song is not None:
            old_song = str(reported_old_song or "") or None
        old_fingerprint = self._last_queue_fingerprint
        new_fingerprint = str(payload.get("fingerprint") or "") or None
        if event_type == "playback_changed":
            self._last_playback_song = new_song
        if event_type == "queue_changed":
            self._last_queue_fingerprint = new_fingerprint
        if event_type == "playback_changed":
            if not new_song or old_song == new_song:
                return
            if old_song is None and reported_old_song is None:
                return
            await self._flush_room_memory(session, old_song)
        if event_type == "queue_changed" and old_fingerprint and new_fingerprint and old_fingerprint == new_fingerprint:
            return
        state_changed = (
            event_type == "playback_changed" and old_song != new_song
        ) or (
            event_type == "queue_changed" and old_fingerprint and new_fingerprint and old_fingerprint != new_fingerprint
        )
        if state_changed and attribution != "plugin":
            # A user action or an actual room-state transition makes any
            # in-flight model choice stale, regardless of inferred actor.
            self.store.bump_decision_revision(session["id"])
        if attribution == "external":
            pending = self.store.pending_recommendation(session["id"])
            action: dict[str, Any] = {"decision_revision": self.store.session(session["id"]).get("decision_revision")}
            if pending:
                queue = [str(value) for value in payload.get("display_list") or []]
                target = str(pending.get("song_id") or "")
                if not new_song:
                    new_song = str((self._snapshot.get("current_song") or {}).get("id") or "")
                valid_next = False
                if target in queue and new_song in queue:
                    index = queue.index(new_song)
                    valid_next = index + 1 < len(queue) and queue[index + 1] == target
                if not valid_next:
                    self.store.set_recommendation(pending["id"], "cancelled", evidence={"reason": "external_override", "event": payload})
                    action["pending_recommendation"] = "cancelled"
            if event_type == "queue_changed" and session.get("autodj_enabled"):
                self.store.protect_queue(session['id'],seconds=120)
                retry_at = time.time() + 120
                self.store.defer_recommendation(session["id"], "queue_changed", retry_at, shorten_only=True)
                action["eligibility_check_at"] = retry_at
            self.store.record_external_override(
                session_id=session["id"],
                event_type=event_type,
                attribution=attribution,
                room_id=event.get("room_id"),
                old_song_id=old_song if event_type == "playback_changed" else None,
                new_song_id=new_song if event_type == "playback_changed" else None,
                old_fingerprint=old_fingerprint,
                new_fingerprint=new_fingerprint,
                evidence=payload,
                action=action,
            )
            self.store.set("last_external_override_at", time.time())
        elif attribution != "plugin":
            return
        if self._chat_buffer:
            return
        if event_type == "playback_changed" and new_song and new_song != old_song and str(payload.get("play_status") or "") == "PLAY":
            self._schedule_proactive_window(session, new_song)

    def _schedule_proactive_window(self, session: Mapping[str, Any], song_id: str) -> None:
        # Proactive room chatter is intentionally retired; invitations are event driven.
        return
        if session.get("phase") != "active" or not session.get("room_chat_enabled"):
            return
        if self.store.get(f"proactive_song:{session['id']}:{song_id}"):
            return
        last = float(self.store.get("last_proactive_at", 0) or 0)
        if time.time() - last < self.settings.proactive_cooldown_seconds:
            return
        if self.store.pending_recommendation(session["id"]):
            return
        if self.store.has_unresolved_operations():
            return
        self.store.set(f"proactive_song:{session['id']}:{song_id}", time.time())
        if self._proactive_task and not self._proactive_task.done():
            self._proactive_task.cancel()
        self._proactive_song_id = song_id
        self._proactive_task = self._spawn_task(self._open_proactive_window(session, song_id), "cove-proactive-window")
        self.store.audit("cove", "proactive_window_opened", {"song_id": song_id, "session_id": session.get("id")})

    def _spawn_task(self, awaitable: Awaitable[Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _open_proactive_window(self, session: Mapping[str, Any], song_id: str) -> None:
        try:
            await asyncio.sleep(self.settings.proactive_window_delay_seconds)
            if self._proactive_song_id != song_id or self._chat_buffer:
                return
            snapshot = await self.snapshot(True)
            current = snapshot.get("current_song") or {}
            if str(current.get("id") or "") != song_id or str((snapshot.get("playback") or {}).get("status") or "") != "PLAY":
                return
            result = await self.agent.run_proactive(session=session, song=current, lyric=await self.current_lyric(song_id))
            text = clean(result.get("text"), 500)
            if not text:
                self.store.audit("agent", "proactive_skip", {"song_id": song_id, "turn_id": result.get("turn_id")})
                return
            sent = await self._send_room(session, text, origin="room_reply", request_id="proactive:" + str(result.get("turn_id")))
            if sent.get("state") == "confirmed":
                self.store.set("last_proactive_at", time.time())
                self.store.audit("agent", "proactive_sent", {"song_id": song_id, "turn_id": result.get("turn_id")})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log("warning", "proactive window failed: %s", type(exc).__name__)

    async def _handle_room_chat_event(self, session: Mapping[str, Any] | None, event: Mapping[str, Any]) -> None:
        if not self._event_matches_session(session, event) or session.get("phase") != "active":
            return
        role = str(event.get("actor_role") or "unknown")
        payload = event.get("payload") or {}
        text = compact(payload.get("text"), 500)
        if role != "owner" or not text:
            return
        self.store.record_room_message(
            session_id=str(session.get("id") or ""),
            provider_message_id=str((event.get("payload") or {}).get("message_id") or event.get("event_id") or "") or None,
            sender_uid=str(event.get("actor_uid") or ""),
            sender_role=role,
            text=text,
            direction="inbound",
            state="received",
        )
        occurred_at = float(event.get("occurred_at") or time.time())
        if occurred_at > 10_000_000_000:
            occurred_at /= 1000
        if time.time() - occurred_at > self.settings.room_chat_backlog_ttl_seconds:
            return
        command = parse_music_command(text, room=True)
        if command:
            await self._handle_room_command(session, command, text)
            return
        if not session.get("room_chat_enabled"):
            return
        if self._proactive_task and not self._proactive_task.done():
            self._proactive_task.cancel()
            self._proactive_song_id = None
        interrupt = getattr(self.agent, "interrupt_low_priority", None)
        if callable(interrupt):
            try:
                await interrupt()
            except Exception:
                pass
        item = {
            "session_id": str(session["id"]),
            "event_id": str(event.get("event_id") or ""),
            "message_id": str(payload.get("message_id") or ""),
            "text": text,
            "actor_role": role,
            "occurred_at": occurred_at,
        }
        self.store.bump_decision_revision(str(session["id"]))
        if not self._chat_buffer:
            self._chat_first_at = time.time()
        self._chat_buffer.append(item)
        self._chat_last_at = time.time()
        if self._chat_flush_task is None or self._chat_flush_task.done():
            self._chat_flush_task = self._spawn_task(self._wait_to_flush_chat(), "cove-chat-debounce")

    async def _wait_to_flush_chat(self) -> None:
        while self._chat_buffer:
            due = min(self._chat_first_at + self.settings.chat_batch_max_wait_seconds, self._chat_last_at + self.settings.chat_batch_wait_seconds)
            await asyncio.sleep(max(0, due - time.time()))
            await self._maybe_flush_chat()

    @staticmethod
    def _event_matches_session(session: Mapping[str, Any] | None, event: Mapping[str, Any]) -> bool:
        if not session or not event.get("room_id") or event.get("room_id") != session.get("room_id"):
            return False
        expected_epoch = session.get("room_epoch")
        event_epoch = event.get("room_epoch")
        return not (expected_epoch and event_epoch and expected_epoch != event_epoch)

    async def _handle_room_command(self, session: Mapping[str, Any], command: Mapping[str, Any], text: str) -> None:
        kind = str(command.get("kind") or "")
        reply = ""
        if kind == "diagnose":
            await self.snapshot(True)
            reply = self.diagnose_text()
        elif kind == "status":
            reply = self.status_text()
        elif kind == "autodj":
            reply = await self.set_autodj(bool(command.get("enabled")))
        elif kind == "room_chat":
            reply = await self.set_room_chat(bool(command.get("enabled")))
        elif kind == "recommend_once":
            reply = await self.recommend_once()
        elif kind == "cancel_recommendation":
            reply = await self.cancel_pending_recommendation()
        elif kind == "pause":
            reply = await self.pause()
        elif kind == "resume":
            reply = await self.resume()
        elif kind == "next":
            reply = await self.next_track()
        elif kind == "enqueue":
            reply = await self.enqueue(str(command.get("song_id") or ""), origin="room_command")
        elif kind == "play_now":
            reply = await self.enqueue(str(command.get("song_id") or ""), play_now=True, origin="room_command")
        elif kind == "search":
            try:
                rows = await self.search(str(command.get("query") or ""))
                reply = "\n".join(f"{item.get('name')} - {item.get('artist')}（{item.get('id')}）" for item in rows) or "没有找到真实结果。"
            except Exception as exc:
                reply = "搜索失败：" + clean(exc, 200)
        elif kind == "duration":
            minutes = int(command.get("minutes") or 0)
            self.store.update_session(session["id"], expires_at=time.time() + minutes * 60)
            reply = f"好，这次陪你听 {minutes} 分钟。"
        elif kind == "leave":
            reply = await self.leave(session_id=session["id"], reason="room_command")
        if reply:
            try:
                await self._send_room(session, clean(reply, 500), origin="room_command", request_id="room-command:" + str(uuid.uuid4()))
            except Exception as exc:
                self._log("warning", "room command reply failed: %s", type(exc).__name__)

    async def _maybe_flush_chat(self) -> None:
        if not self._chat_buffer:
            return
        current = time.time()
        if current - self._chat_first_at < self.settings.chat_batch_max_wait_seconds and current - self._chat_last_at < self.settings.chat_batch_wait_seconds:
            return
        batch = self._chat_buffer
        self._chat_buffer = []
        self._chat_first_at = 0
        self._chat_last_at = 0
        self._spawn(self._flush_chat_batch(batch), "cove-chat-turn")

    async def _flush_chat_batch(self, batch: list[dict[str, Any]]) -> None:
        session = self.store.active_session()
        if not session or not session.get("room_chat_enabled"):
            return
        batch = [item for item in batch if item.get("session_id") == session["id"]]
        if not batch or not self.settings.enabled:
            return
        batch_id = str(uuid.uuid4())
        self.store.create_reply_batch(session["id"], batch_id, time.time(), batch)
        result = await self.agent.run_room_turn(session=session, messages=batch)
        current = self.store.active_session()
        if not self.settings.enabled or not current or current["id"] != session["id"] or current.get("phase") != "active" or not current.get("room_chat_enabled"):
            self.store.set_reply_batch(batch_id, "cancelled", error="session_or_chat_changed")
            return
        if result.get("state") not in {"confirmed", "completed"}:
            self.store.set_reply_batch(batch_id, "failed", error=result.get("error") or "agent_failed")
            return
        text = clean(result.get("text"), 500)
        if not text:
            self.store.set_reply_batch(batch_id, "failed", error="empty_agent_reply")
            return
        self.store.set_reply_batch(batch_id, "generated", output_text=text)
        sent = await self._send_room(session, text, origin="room_reply", request_id="reply:" + batch_id)
        if sent.get("state") == "confirmed":
            message_id = str(((sent.get("result") or {}).get("messageId")) or "")
            self.store.set_reply_batch(batch_id, "sent", output_text=text, message_id=message_id)
        else:
            self.store.set_reply_batch(batch_id, "unknown", output_text=text, error=self._operation_error(sent))
        snapshot = await self.snapshot(False)
        song = snapshot.get("current_song") or {}
        song_id = str(song.get("id") or "")
        if song_id:
            user_text = " / ".join(clean(item.get("text"), 120) for item in batch[-3:])
            self.store.append_room_memory_draft(
                session_id=str(session["id"]),
                song_id=song_id,
                song_name=str(song.get("name") or ""),
                artist=str(song.get("artist") or ""),
                user_text=user_text,
                assistant_text=text,
            )

    async def _flush_room_memory(self, session: Mapping[str, Any], song_id: str | None = None) -> None:
        # Raw drafts stay local. One atomic, retractable batch is committed at end.
        if song_id is None and self.store.session(session['id']).get('phase')=='ended':
            await self._commit_session_summary(session)

    async def _schedule_recommendation(self) -> None:
        session = self.store.active_session()
        if not session or session.get("phase") != "active" or not session.get("autodj_enabled"):
            return
        snapshot=await self.snapshot(False)
        if not self.recommendation_assessment(session,snapshot)['eligible']:return
        next_at = time.time()
        check_at = float(session.get("next_eligibility_check_at") or 0)
        if time.time() < check_at:
            return
        if self.store.pending_recommendation(session["id"]):
            return
        if self.store.has_pending_job("recommendation", str(session["id"])):
            return
        slot = f"{session['id']}:{int(next_at)}:{int(check_at)}"
        job_id = self.store.enqueue_job("recommendation", slot, time.time(), session["id"], {"manual": False})
        if job_id:
            self.store.update_session(session["id"], next_eligibility_check_at=time.time() + 30)
            self.store.audit("cove", "recommendation_scheduled", {"job_id": job_id, "slot": slot, "next_at": next_at})

    async def _run_recommendation_job(self, job: Mapping[str, Any]) -> None:
        job_id = str(job["id"])
        session = self.store.session(str(job.get("session_id") or ""))
        try:
            result = await self._run_recommendation(session, manual=False)
            self.store.finish_job(job_id, result)
        except Exception as exc:
            if int(job.get("attempts") or 0) >= 3:
                self.store.finish_job(job_id, error=type(exc).__name__)
                if session:
                    self.store.defer_recommendation(session["id"], "temporary_error", time.time() + 60)
            else:
                self.store.retry_job(job_id, type(exc).__name__, delay=60)

    async def _run_recommendation(self, session: Mapping[str, Any] | None, *, manual: bool) -> dict[str, Any]:
        if not session or session.get("phase") != "active":
            return {"state": "skipped", "reason": "no_active_session"}
        session_id = str(session.get("id") or "")
        def blocked(reason: str, delay: int = 30, resume_at: float | None = None) -> dict[str, Any]:
            until = float(resume_at if resume_at is not None else time.time() + delay)
            if not manual and session_id:
                self.store.defer_recommendation(session_id, reason, until)
            return {"state": "blocked", "reason": reason, "resume_at": until}
        if self.store.token_summary(self.settings.token_daily_limit)['reached']:
            return blocked('token_budget_reached',resume_at=self._quota_resume_at('daily_limit'))
        if not self.store.token_summary(self.settings.token_daily_limit)['meter_available']:
            return blocked('token_meter_unavailable',60)
        try:
            snapshot = await self.snapshot(True)
        except Exception as exc:
            return blocked("cove_unavailable", 60)
        playback = snapshot.get("playback") or {}
        queue = snapshot.get("queue") or {}
        room_blocker = self._recommendation_room_blocker(session,snapshot)
        if room_blocker:
            return blocked(room_blocker,30 if room_blocker=='playback_evidence_stale' else 60)
        if not manual and not session.get("autodj_enabled"):
            return {"state": "skipped", "reason": "autodj_disabled"}
        await self._reconcile_operations()
        if self.store.has_unresolved_music_operations(session_id):
            return blocked("unresolved_music_operation", 30)
        if playback.get("status") != "PLAY" and not (manual and playback.get("status") == "PAUSE"):
            return blocked("playback_not_playing", 30)
        if not manual and queue.get("play_mode") != "ORDER_LOOP":
            return blocked("unsupported_play_mode", 60)
        if self.store.pending_recommendation(session["id"]):
            return blocked("pending_recommendation", 30)
        if not manual:
            cycle=self.cycle_assessment(session,snapshot)
            if not cycle['eligible']:return blocked(cycle['reason'],resume_at=cycle.get('until') or time.time()+5)
        self.store.decision(session_id,'trigger',manual=manual,cycle=self.store.cycle(session_id))
        started=time.monotonic()
        run_id, limit_reason = self.store.reserve_recommendation_run(
            session_id,
            "manual" if manual else "auto",
            hourly_limit=None,
            daily_limit=None,
        )
        if not run_id:
            if limit_reason in {"hourly_limit", "daily_limit"}:
                resume_at = self._quota_resume_at(limit_reason)
                return blocked(limit_reason, resume_at=resume_at)
            return blocked(limit_reason or "run_in_progress", 30)
        try:
            if hasattr(self.agent, "rank_candidates"):
                result = await self._recommend_cached(session,manual=manual)
            else:
                result = await self.agent.run_autodj(session=session, manual=manual)
        except Exception as exc:
            result = {"state": "failed", "error": type(exc).__name__ + ": " + clean(exc, 240)}
        self.store.decision(session_id,'result',state=result.get('state'),reason=result.get('reason') or result.get('error'),elapsed_ms=round((time.monotonic()-started)*1000,2))
        if result.get("state") == "confirmed":
            if not manual:self.store.advance_cycle(session_id)
            self.store.finish_recommendation_run(run_id, "confirmed")
            self.store.clear_recommendation_block(session_id)
            self.store.audit("cove", "recommendation_confirmed", {"turn_id": result.get("turn_id"), "manual": manual})
            return result
        error = str(result.get("error") or "")
        reason=result.get('reason')
        if reason in {'token_budget_reached','token_meter_unavailable'}:
            self.store.finish_recommendation_run(run_id,'not_started',reason)
            return blocked(reason,resume_at=self._quota_resume_at('daily_limit') if reason=='token_budget_reached' else time.time()+60)
        if error == "provider_missing" or "ProviderNotFoundError" in error:
            self.store.finish_recommendation_run(run_id, "not_started", "provider_unavailable")
            return blocked("provider_unavailable", 60)
        if "model_budget_exhausted" in error or result.get("state") == "budget_exhausted":
            self.store.finish_recommendation_run(run_id, "not_started", "model_budget_exhausted")
            return blocked("model_budget_exhausted", resume_at=self._quota_resume_at("daily_limit"))
        if result.get("state") == "failed" or "provider_attempts_exhausted" in error:
            self.store.finish_recommendation_run(run_id, "failed", error or "model_failed")
            return blocked("model_unavailable", 60)
        self.store.finish_recommendation_run(run_id, "skipped", error or "agent_skipped")
        if not manual:
            self._advance_next_recommendation(session_id)
        self.store.clear_recommendation_block(session_id)
        skipped = {"state": "skipped", "reason": result.get("error") or "agent_skipped", "agent": result}
        self.store.audit("cove", "recommendation_skipped", {"reason": skipped["reason"], "manual": manual, "turn_id": result.get("turn_id")})
        return skipped

    def _advance_next_recommendation(self, session_id: str) -> None:
        self.store.update_session(session_id,next_eligibility_check_at=time.time()+30,next_recommend_at=None)

    @staticmethod
    def _quota_resume_at(reason: str) -> float:
        current = now()
        if reason == "hourly_limit":
            return (current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()
        tomorrow = current.date() + timedelta(days=1)
        return current.replace(year=tomorrow.year, month=tomorrow.month, day=tomorrow.day, hour=0, minute=0, second=0, microsecond=0).timestamp()

    async def _reconcile_playback_history(self) -> None:
        session = self.store.active_session()
        snapshot = await self.snapshot(False)
        current = snapshot.get("current_song") or {}
        song_id = str(current.get("id") or "")
        if not session or session.get("phase")!="active" or not song_id or snapshot.get("room_id")!=session["room_id"] or not fresh(snapshot,"playback") or (snapshot.get("playback") or {}).get("status")!="PLAY":
            return
        self.store.record_playback_observation(
            session["id"],
            song_id,
            str(current.get("name") or ""),
            str(current.get("artist") or ""),
            {"source": snapshot.get("evidence", {}).get("playback"), "category": self._played_category(session["id"],song_id)},
        )

    def _played_category(self,session_id,song_id):
        with self.store.lock:
            rows=self.store.conn.execute("SELECT song_id,evidence_json FROM recommendations WHERE session_id=? ORDER BY created_at DESC LIMIT 20",(session_id,)).fetchall()
        for rec in map(dict,rows):
            if str(rec.get("song_id"))==song_id:
                return json.loads(rec.get("evidence_json") or "{}").get("category","")
        return ""
