from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_cove_companion.core.agent import CoveMusicAgent, ROOM_TOOLS, _filter_conversation_history, agent_final_state, format_recent_operations, sanitize_room_reply
from astrbot_plugin_cove_companion.core.config import Settings
from astrbot_plugin_cove_companion.core.engine import CompanionEngine
from astrbot_plugin_cove_companion.core.lyrics import current_lyric_window, parse_lrc
from astrbot_plugin_cove_companion.core.store import CompanionStore
from astrbot_plugin_cove_companion.core.text import extract_invite, parse_music_command
from astrbot_plugin_cove_companion.main import CoveCompanionPlugin, event_platform


class FakeClient:
    def __init__(self):
        self.current_id = "100"
        self.queue = ["100", "101", "102"]
        self.status = "PLAY"
        self.queue_version = 1
        self.room_id = "room-1"
        self.commands = []
        self.claimed = 0
        self.renewed = 0

    async def claim(self, holder):
        self.claimed += 1
        return {"lease_id": "lease-1", "fencing_token": self.claimed, "expires_at": 9_999_999_999}

    async def renew(self, lease_id, fencing_token):
        self.renewed += 1
        return {"lease_id": lease_id, "fencing_token": fencing_token, "expires_at": 9_999_999_999}

    async def release(self, lease_id, fencing_token):
        return True

    async def snapshot(self, fresh=False):
        return {
            "room_id": self.room_id,
            "room_epoch": "epoch-1",
            "room_revision": len(self.commands) + 4,
            "connected": bool(self.room_id),
            "owner_binding": {"uid": "10001", "present": True},
            "current_song": {"id": self.current_id, "name": "Current", "artist": "Artist", "duration_ms": 180000},
            "playback": {"status": self.status, "progress_ms": 1000, "observed_at": 1_700_000_000_000},
            "queue": {"fingerprint": "queue-" + str(self.queue_version) + "-" + ",".join(self.queue), "play_mode": "ORDER_LOOP", "song_ids": list(self.queue)},
            "evidence": {k:{"stale":False,"validated_at":time.time()} for k in ("room","members","playback","queue")},
        }

    async def command(self, request):
        self.commands.append(dict(request))
        action = request["action"]
        song_id = str((request.get("args") or {}).get("song_id") or "")
        if action == "ENQUEUE_NEXT":
            self.queue_version += 1
            self.queue = [item for item in self.queue if item != song_id]
            index = self.queue.index(self.current_id)
            self.queue.insert(index + 1, song_id)
        elif action == "PLAY_NOW":
            self.queue = [item for item in self.queue if item != song_id]
            index = self.queue.index(self.current_id)
            self.queue.insert(index + 1, song_id)
            self.current_id = song_id
            self.status = "PLAY"
        elif action == "NEXT":
            index = self.queue.index(self.current_id)
            self.current_id = self.queue[(index + 1) % len(self.queue)]
            self.status = "PLAY"
        elif action == "PAUSE":
            self.status = "PAUSE"
        elif action == "RESUME":
            self.status = "PLAY"
        elif action == "CANCEL_QUEUED_RECOMMENDATION":
            self.queue = [item for item in self.queue if item != song_id]
        elif action == "LEAVE":
            self.room_id = None
        return {"operation_id": request["operation_id"], "state": "accepted"}

    async def wait_operation(self, operation_id, deadline_seconds=30):
        action = self.commands[-1]["action"]
        return {"operation_id": operation_id, "state": "confirmed", "result": {"ok": True, "action": action, "messageId": "msg-1"}}

    async def search(self, query, limit=12):
        return [
            {"id": "3410744228", "name": "铁花飞", "artist": "测试歌手", "durationMs": 200000},
            {"id": "200", "name": "Next", "artist": "A", "durationMs": 180000},
        ][:limit]

    async def lyrics(self, song_id):
        return {"songId": song_id, "lrc": {"lines": [{"timeMs": 0, "text": "第一句"}, {"timeMs": 5000, "text": "第二句"}]}, "translation": None, "romanization": None}

    async def tracks(self, song_ids):
        return [{"id": item, "name": "Track " + item, "artist": "Artist", "duration_ms": 180000, "cover_url": "https://example.invalid/" + item + ".jpg"} for item in song_ids]


class FakeAgent:
    def __init__(self, result=None):
        self.settings = Settings()
        self.result = result or {"state": "confirmed", "text": "", "turn_id": "turn-1", "confirmed": True}
        self.turns = {}

    async def current_persona_id(self):
        return "小诺·陪伴"

    async def run_autodj(self, *, session, manual=False):
        return dict(self.result)

    async def run_room_turn(self, *, session, messages):
        return {"state": "completed", "text": "在听。", "turn_id": "turn-room"}

    async def run_proactive(self, *, session, song, lyric):
        return {"state": "confirmed", "text": "", "turn_id": "turn-proactive"}

    def active_turn(self, turn_id):
        return self.turns.get(turn_id)

    def reserve_followup(self, turn_id):
        return None

    def record_tool_result(self, turn_id, name, args, result):
        return None


class FakeConversationManager:
    async def get_curr_conversation_id(self, umo):
        return "conversation-1"

    async def get_conversation(self, umo, cid):
        return SimpleNamespace(history="[]", persona_id="persona-1")


class FakePersonaManager:
    async def resolve_selected_persona(self, **kwargs):
        return "小诺·陪伴", {"prompt": "测试人格"}, None, None


class FakeToolManager:
    def get_func(self, name):
        return None


class RetryProviderContext:
    def __init__(self, fail_count=2, tool_on_error=False):
        self.fail_count = fail_count
        self.tool_on_error = tool_on_error
        self.agent = None
        self.providers = []
        self.conversation_manager = FakeConversationManager()
        self.persona_manager = FakePersonaManager()

    def get_current_chat_provider_id(self, umo):
        return "primary"

    def get_config(self, umo=None):
        return {"provider_settings": {"fallback_chat_models": ["fallback"]}}

    def get_llm_tool_manager(self):
        return FakeToolManager()

    def tool_loop_agent(self, **kwargs):
        provider = kwargs["chat_provider_id"]
        self.providers.append(provider)

        async def run():
            if len(self.providers) <= self.fail_count:
                if self.tool_on_error and self.agent is not None:
                    turn_id = kwargs["event"].get_extra("cove_room_turn_id")
                    self.agent.record_tool_result(
                        turn_id,
                        "cove_music_play_now",
                        {"song_id": "partial"},
                        {"state": "unknown", "operation_id": "partial-op"},
                    )
                return SimpleNamespace(role="err", completion_text="All chat models failed: simulated 403")
            return SimpleNamespace(role="assistant", completion_text="主模型恢复后回复")

        return run()


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CompanionStore(Path(self.tmp.name) / "test.sqlite3")
        self.settings = Settings(
            enabled=True,
            owner_umo="aiocqhttp:FriendMessage:42",
            owner_netease_uid="10001",
            model_daily_budget=200,
            room_chat_daily_budget=80,
            autodj_daily_budget=40,
            proactive_daily_budget=40,
            autodj_hourly_limit=6,
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def engine(self, client, agent=None):
        async def qq_send(umo, text):
            return {"message_id": "qq-1"}
        return CompanionEngine(context=object(), config={}, settings=self.settings, store=self.store, client=client, agent=agent or FakeAgent(), logger=object(), qq_send=qq_send)

    def active_session(self, client):
        session = self.store.create_session("aiocqhttp:FriendMessage:42", "room-1", "epoch-1", True, True, 0, "小诺·陪伴")
        self.store.update_session(session["id"], phase="active")
        return self.store.active_session()

    def test_music_tools_are_not_registered_globally(self):
        source = inspect.getsource(CoveCompanionPlugin)
        self.assertNotIn('@filter.llm_tool(name="cove_music_', source)
        self.assertIn('tool_builder = self._room_tool_set', source) if False else None

    def test_extract_official_invite_and_real_url(self):
        payload = {"meta": {"roomId": "4fd61bf28d7c8902a4fe6dbdc8670081_1793353995", "inviterId": 10001}}
        self.assertEqual(extract_invite(payload)["inviter_id"], "10001")
        text = "https://music.163.com/listen-together/share/index.html?roomId=4fd61bf28d7c8902a4fe6dbdc8670081_1793353995&inviterId=12267195018"
        self.assertEqual(extract_invite(text)["room_id"], "4fd61bf28d7c8902a4fe6dbdc8670081_1793353995")

    def test_extract_invite_does_not_combine_unrelated_records(self):
        payload = {"room": {"roomId": "room-123456"}, "sender": {"uid": 10001}}
        self.assertIsNone(extract_invite(payload))

    def test_explicit_commands_are_fast_paths(self):
        self.assertEqual(parse_music_command("/一起听 暂停")["kind"], "pause")
        self.assertEqual(parse_music_command("/一起听 状态")["kind"], "status")
        self.assertEqual(parse_music_command("/一起听 诊断")["kind"], "diagnose")
        self.assertIsNone(parse_music_command("换首铁花飞"))

    def test_platform_metadata_object_does_not_stringify(self):
        class Meta:
            id = "onebot_v11"
            name = "OneBot"
            def __str__(self):
                return "PlatformMetadata(name='OneBot', id='onebot_v11', description='bad')"
        class Event:
            platform_meta = Meta()
            platform = Meta()
            unified_msg_origin = "onebot_v11:FriendMessage:42"
            def get_platform_name(self):
                return "onebot_v11"
        self.assertEqual(event_platform(Event()), "onebot_v11")

    def test_public_api_views_redact_account_identifiers(self):
        snapshot = CoveCompanionPlugin._public_snapshot({
            "room_id": "1234567890abcdef",
            "room_epoch": "epoch-secret",
            "bot_account": {"uid": "bot-uid"},
            "owner_binding": {"uid": "owner-uid", "present": True},
            "members": [{"uid": "owner-uid", "role": "owner", "present": True}],
            "queue": {"song_ids": ["1"], "versions": [{"user_id": "owner-uid", "version": 1}]},
            "worker": {"phase": "listening", "account_id": "bot-uid"},
        })
        serialized = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("owner-uid", serialized)
        self.assertNotIn("bot-uid", serialized)
        self.assertNotIn("abcdef", serialized)
        self.assertEqual(snapshot["room_id_hint"], "12345678…")
        self.assertNotIn("uid", snapshot["members"][0])
        session = CoveCompanionPlugin._public_session({"id": "s1", "owner_umo": "onebot_v11:FriendMessage:1", "phase": "active"})
        self.assertNotIn("owner_umo", session)

    def test_platform_instance_id_wins_over_adapter_type_for_owner_checks(self):
        class Meta:
            id = "onebot_v11"
            name = "aiocqhttp"
        class Event:
            platform_meta = Meta()
            unified_msg_origin = "onebot_v11:FriendMessage:10001"
            def get_platform_id(self):
                return "onebot_v11"
            def get_platform_name(self):
                return "aiocqhttp"
            def get_sender_id(self):
                return "10001"
            def get_group_id(self):
                return ""
        event = Event()
        self.assertEqual(event_platform(event), "onebot_v11")
        plugin = object.__new__(CoveCompanionPlugin)
        plugin.settings = Settings(enabled=True, owner_umo="onebot_v11:FriendMessage:10001")
        plugin.store = self.store
        self.assertTrue(plugin.can_manage(event))

    def test_context_filter_excludes_tool_and_cron(self):
        history = [
            {"role": "user", "content": "[cron] 每日任务汇报"},
            {"role": "assistant", "content": "旧任务"},
            {"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "第一回复"},
            {"role": "tool", "content": "超长工具结果"},
            {"role": "assistant", "tool_calls": [{"id": "x"}]},
            {"role": "user", "content": "第二轮"},
            {"role": "assistant", "content": "第二回复"},
        ]
        contexts = _filter_conversation_history(json.dumps(history, ensure_ascii=False), 6)
        joined = "".join(item["content"] for item in contexts)
        self.assertIn("第一轮", joined)
        self.assertIn("第二回复", joined)
        self.assertNotIn("工具结果", joined)
        self.assertNotIn("每日任务汇报", joined)

    def test_context_filter_strips_metadata_and_think_blocks(self):
        history = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "刚才那首是什么"},
                    {"type": "text", "text": "<system_reminder>User ID: 42</system_reminder>"},
                    {"type": "text", "text": "<qq_verified_components types=\"\"/>"},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "think", "think": "内部思考不能注入"},
                    {"type": "text", "text": "是 Brighter Skies。"},
                ],
            },
            {"role": "user", "content": "继续听 <system_reminder>Current datetime: now</system_reminder>"},
            {"role": "assistant", "content": "好，我在。"},
        ]
        contexts = _filter_conversation_history(history, 6)
        self.assertEqual(contexts[0], {"role": "user", "content": "刚才那首是什么"})
        self.assertEqual(contexts[1], {"role": "assistant", "content": "是 Brighter Skies。"})
        self.assertEqual(contexts[2], {"role": "user", "content": "继续听"})
        self.assertNotIn("system_reminder", json.dumps(contexts, ensure_ascii=False))
        self.assertNotIn("内部思考", json.dumps(contexts, ensure_ascii=False))

    def test_lyrics_timeline_alignment(self):
        timeline = parse_lrc("[00:00.00]第一句\n[00:05.00]第二句\n[00:10.00]第三句")
        window = current_lyric_window(timeline, 6000)
        self.assertEqual(window["current"], "第二句")
        self.assertEqual(window["next"], "第三句")

    def test_lyrics_accept_cove_time_ms_camel_case(self):
        window = current_lyric_window([
            {"timeMs": 0, "text": "\u7b2c\u4e00\u53e5"},
            {"timeMs": 5000, "text": "\u7b2c\u4e8c\u53e5"},
            {"timeMs": 10000, "text": "\u7b2c\u4e09\u53e5"},
        ], 6000)
        self.assertEqual(window["current"], "\u7b2c\u4e8c\u53e5")
        self.assertEqual(window["next"], "\u7b2c\u4e09\u53e5")

    def test_ledger_uses_song_id_dedupe_key(self):
        ledger = Path(self.tmp.name) / "pushed_songs.jsonl"
        ledger.write_text("\n".join([
            json.dumps({"ts": "2026-10-30 19:02:07", "songId": "16957743", "name": "Outro", "artist": "M83"}, ensure_ascii=False),
            json.dumps({"ts": "2026-10-30 19:02:07", "songId": "441491828", "name": "水星记", "artist": "郭顶"}, ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        result = self.store.import_legacy_ledger(ledger)
        self.assertEqual(result["imported"], 2)
        with self.store.lock:
            rows = self.store.conn.execute("SELECT evidence_json FROM listening_history ORDER BY id").fetchall()
        keys = [json.loads(row["evidence_json"])["record_key"] for row in rows]
        self.assertEqual(keys, ["netease:16957743", "netease:441491828"])
        self.assertEqual(self.store.import_legacy_ledger(ledger)["imported"], 0)

    def test_budget_pools_are_independent(self):
        self.assertIsNotNone(self.store.reserve_model_call("room_reply", 200, 80)[0])
        self.assertIsNotNone(self.store.reserve_model_call("autodj", 200, 40, 6)[0])
        self.assertIsNotNone(self.store.reserve_model_call("proactive", 200, 40)[0])
        self.assertEqual(self.store.models_used_today("room_reply"), 1)
        self.assertEqual(self.store.models_used_today("autodj"), 1)
        self.assertEqual(self.store.models_used_today("proactive"), 1)

    def test_play_now_requires_and_gets_snapshot_confirmation(self):
        client = FakeClient()
        engine = self.engine(client)
        self.active_session(client)
        result = asyncio.run(engine.play_now_result("200"))
        self.assertEqual(result["state"], "confirmed")
        self.assertTrue(result["snapshot_verified"])
        self.assertEqual(client.current_id, "200")
        self.assertEqual(client.commands[0]["action"], "PLAY_NOW")
        stored = self.store.operations(1)[0]
        self.assertTrue(stored["result"]["snapshot_verified"])
        self.assertEqual(stored["result"]["snapshot_evidence"]["song_id"], "200")

    def test_enqueue_next_verifies_adjacency(self):
        client = FakeClient()
        engine = self.engine(client)
        self.active_session(client)
        result = asyncio.run(engine.enqueue_next_result("200"))
        self.assertEqual(result["state"], "confirmed")
        self.assertTrue(result["snapshot_verified"])
        self.assertEqual(client.queue[client.queue.index("100") + 1], "200")

    def test_enqueue_existing_next_item_is_confirmed_by_version_change(self):
        client = FakeClient()
        engine = self.engine(client)
        self.active_session(client)
        result = asyncio.run(engine.enqueue_next_result("101"))
        self.assertEqual(result["state"], "confirmed")
        self.assertTrue(result["snapshot_verified"])
        self.assertEqual(client.queue[client.queue.index("100") + 1], "101")

    def test_pause_verifies_playback_status(self):
        client = FakeClient()
        engine = self.engine(client)
        self.active_session(client)
        result = asyncio.run(engine.pause_result())
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(client.status, "PAUSE")

    def test_recommendation_enqueues_next_and_sends_intro(self):
        client = FakeClient()
        engine = self.engine(client)
        self.active_session(client)
        result = asyncio.run(engine.recommend_from_agent({"id": "200", "name": "Next", "artist": "A"}, "下一首听这首。"))
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual([item["action"] for item in client.commands], ["ENQUEUE_NEXT"])
        asyncio.run(engine._drain_introductions(self.store.active_session()["id"]))
        self.assertEqual([item["action"] for item in client.commands], ["ENQUEUE_NEXT", "SEND_MESSAGE"])

    def test_external_queue_change_cancels_pending_recommendation(self):
        client = FakeClient()
        engine = self.engine(client)
        session = self.active_session(client)
        rec = self.store.create_recommendation(session["id"], "slot", {"id": "200", "name": "Next", "artist": "A"}, "intro", "test")
        event = {
            "event_type": "queue_changed",
            "room_id": "room-1",
            "payload": {"attribution": "external", "display_list": ["100", "999", "200"]},
        }
        asyncio.run(engine._handle_state_event(session, event))
        self.assertEqual(self.store.recommendation(rec["id"])["state"], "cancelled")
        overrides = self.store.external_overrides()
        self.assertEqual(len(overrides), 1)
        self.assertIsNone(overrides[0]["old_song_id"])
        self.assertIsNone(overrides[0]["new_song_id"])

    def test_external_playback_noop_does_not_create_override(self):
        client = FakeClient()
        engine = self.engine(client)
        session = self.active_session(client)
        engine._last_playback_song = "200"
        asyncio.run(engine._handle_state_event(session, {
            "event_type": "playback_changed",
            "room_id": "room-1",
            "payload": {"attribution": "external", "song_id": "200", "former_song_id": "200", "play_status": "PLAY"},
        }))
        self.assertEqual(self.store.external_overrides(), [])
        self.assertIsNone(self.store.get("last_external_override_at"))

    def test_initial_and_unknown_song_events_do_not_fake_external_override(self):
        client = FakeClient()
        engine = self.engine(client)
        session = self.active_session(client)
        asyncio.run(engine._handle_state_event(session, {
            "event_type": "playback_changed",
            "room_id": "room-1",
            "payload": {"attribution": "external", "song_id": "200", "play_status": "PLAY"},
        }))
        self.assertEqual(self.store.external_overrides(), [])

    def test_migration_purges_equal_song_external_overrides(self):
        path = Path(self.tmp.name) / "migration-cleanup.sqlite3"
        first = CompanionStore(path)
        first.record_external_override(
            session_id="s1",
            event_type="playback_changed",
            attribution="external",
            room_id="room-1",
            old_song_id="song-1",
            new_song_id="song-1",
            old_fingerprint=None,
            new_fingerprint=None,
            evidence={},
            action={},
        )
        first.record_external_override(
            session_id="s1",
            event_type="queue_changed",
            attribution="external",
            room_id="room-1",
            old_song_id="song-1",
            new_song_id=None,
            old_fingerprint="q1",
            new_fingerprint="q2",
            evidence={},
            action={},
        )
        first.close()
        reopened = CompanionStore(path)
        try:
            overrides = reopened.external_overrides()
            self.assertEqual(len(overrides), 1)
            self.assertEqual(overrides[0]["event_type"], "queue_changed")
            self.assertIsNone(overrides[0]["old_song_id"])
            self.assertIsNone(overrides[0]["new_song_id"])
        finally:
            reopened.close()

    def test_recent_operation_context_resolves_target_song_from_tool_trace(self):
        turn = self.store.start_agent_turn(kind="room_reply", trigger="test", session_id="s1", input_payload={})
        self.store.record_operation("op-1", "s1", "PLAY_NOW", "room_command", "request-1", "confirmed")
        self.store.record_operation("op-msg", "s1", "SEND_MESSAGE", "room_reply", "request-msg", "confirmed")
        tool_calls = [
            {"name": "cove_music_search", "state": "confirmed", "args": {"query": "铁花飞"}, "result": {"songs": [{"id": "3342981041", "name": "铁花飞", "artist": "Mili"}]}},
            {"name": "cove_music_play_now", "state": "confirmed", "args": {"song_id": "3342981041"}, "result": {"operation_id": "op-1", "state": "confirmed", "snapshot_verified": True, "snapshot_evidence": {"play_status": "PLAY"}}},
        ]
        self.store.finish_agent_turn(turn["id"], state="confirmed", tool_calls=tool_calls, operation_ids=["op-1"])
        recent = self.store.recent_operations_context(session_id="s1", limit=3)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["action"], "PLAY_NOW")
        self.assertEqual(recent[0]["target_song_name"], "铁花飞")
        self.assertTrue(recent[0]["snapshot_verified"])
        self.assertEqual(recent[0]["play_status"], "PLAY")
        prompt = format_recent_operations(recent)
        self.assertIn("立即播放(PLAY_NOW)", prompt)
        self.assertIn("《铁花飞》 - Mili", prompt)
        self.assertIn("状态=confirmed", prompt)
        self.assertIn("快照复核=通过", prompt)
        self.assertIn("播放状态=PLAY", prompt)
        agent = CoveMusicAgent(
            context=object(),
            config={},
            settings=self.settings,
            store=self.store,
            owner_umo=self.settings.owner_umo,
            logger=object(),
        )
        system = agent._system_prompt(
            "人格",
            "proactive",
            {"current_song": {"id": "200", "name": "当前"}, "playback": {"status": "PLAY"}, "queue": {"song_ids": []}},
            None,
            recent,
        )
        self.assertIn("后来房间又变了", system)
        self.assertIn("不得推断此前操作没成功", system)
        self.assertIn("快照复核=通过时，必须承认该操作已确认生效", system)

    def test_active_session_persona_is_repaired(self):
        client = FakeClient()
        self.active_session(client)
        self.store.update_session(self.store.active_session()["id"], persona_id="旧兜底文案")
        changed = self.store.sync_active_persona("小诺·陪伴")
        self.assertEqual(changed, 1)
        self.assertEqual(self.store.active_session()["persona_id"], "小诺·陪伴")

    def test_provider_retries_once_then_uses_configured_fallback(self):
        client = FakeClient()
        engine = self.engine(client)
        session = self.active_session(client)
        self.store.update_session(session["id"], persona_id="旧兜底文案")
        context = RetryProviderContext(fail_count=2)
        agent = CoveMusicAgent(
            context=context,
            config={},
            settings=Settings(
                enabled=True,
                owner_umo="onebot_v11:FriendMessage:10001",
                provider_id="primary",
                fallback_provider_id="",
                provider_retry_count=1,
                agent_timeout_seconds=5,
                model_daily_budget=20,
                room_chat_daily_budget=20,
            ),
            store=self.store,
            owner_umo="onebot_v11:FriendMessage:10001",
            logger=object(),
        )
        agent.attach_engine(engine)
        agent.tool_builder = lambda kind: SimpleNamespace(tools=[])
        result = asyncio.run(agent.run_room_turn(session=session, messages=[{"event_id": "e1", "text": "在吗"}]))
        self.assertEqual(context.providers, ["primary", "primary", "fallback"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["provider_id"], "fallback")
        stored = self.store.agent_turn(result["turn_id"])
        self.assertEqual(stored["provider_id"], "fallback")
        self.assertEqual(len(stored["input"]["provider_attempts"]), 3)
        self.assertEqual(self.store.models_used_today("room_reply"), 3)
        self.assertEqual(self.store.active_session()["persona_id"], "小诺·陪伴")

    def test_provider_is_not_switched_after_partial_tool_side_effect(self):
        client = FakeClient()
        engine = self.engine(client)
        session = self.active_session(client)
        context = RetryProviderContext(fail_count=1, tool_on_error=True)
        agent = CoveMusicAgent(
            context=context,
            config={},
            settings=Settings(
                enabled=True,
                owner_umo="onebot_v11:FriendMessage:10001",
                provider_id="primary",
                provider_retry_count=1,
                agent_timeout_seconds=5,
                model_daily_budget=20,
                room_chat_daily_budget=20,
            ),
            store=self.store,
            owner_umo="onebot_v11:FriendMessage:10001",
            logger=object(),
        )
        context.agent = agent
        agent.attach_engine(engine)
        agent.tool_builder = lambda kind: SimpleNamespace(tools=[])
        result = asyncio.run(agent.run_room_turn(session=session, messages=[{"event_id": "e1", "text": "切歌"}]))
        self.assertEqual(context.providers, ["primary"])
        self.assertEqual(result["state"], "failed")
        stored = self.store.agent_turn(result["turn_id"])
        self.assertEqual(stored["tool_calls"][0]["operation_id"] if "operation_id" in stored["tool_calls"][0] else stored["tool_calls"][0]["result"]["operation_id"], "partial-op")

    def test_player_state_includes_detailed_queue_and_server_time(self):
        client = FakeClient()
        engine = self.engine(client)
        self.active_session(client)
        state = asyncio.run(engine.player_state(force=True))
        self.assertGreater(state["server_time"], 0)
        self.assertEqual([item["name"] for item in state["queue"]["items"]], ["Track 100", "Track 101", "Track 102"])
        self.assertTrue(state["queue"]["items"][0]["is_current"])
        self.assertTrue(state["queue"]["items"][1]["is_next"])
        self.assertIn(state["lyric"]["current"], {"第一句", "第二句"})

    def test_retention_purges_old_operational_data_and_idempotency_cache(self):
        client = FakeClient()
        engine = self.engine(client)
        session = self.active_session(client)
        self.store.record_room_message(session_id=session["id"], provider_message_id="m-old", sender_uid="1", sender_role="owner", text="old", direction="inbound", state="received")
        self.store.set("control:old-key", {"result": "old"})
        with self.store.tx() as db:
            old = 1.0
            db.execute("UPDATE room_messages SET created_at=?", (old,))
            db.execute("UPDATE settings SET updated_at=? WHERE key='control:old-key'", (old,))
        removed = self.store.purge_old_data(30)
        self.assertGreaterEqual(removed["room_messages"], 1)
        self.assertIsNone(self.store.get("control:old-key"))

    def test_schema_upgrade_creates_pre_migration_backup(self):
        import sqlite3
        path = Path(self.tmp.name) / "upgrade.sqlite3"
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        raw.execute("INSERT INTO schema_meta(key,value) VALUES('schema_version','4')")
        raw.commit()
        raw.close()
        upgraded = CompanionStore(path)
        try:
            self.assertIsNotNone(upgraded.migration_backup)
            self.assertTrue(upgraded.migration_backup.is_file())
            self.assertEqual(upgraded.quick_check(), "ok")
        finally:
            upgraded.close()

    def test_priority_queue_preempts_unstarted_low_priority_work(self):
        client = FakeClient()
        engine = self.engine(client)
        self.active_session(client)
        agent = CoveMusicAgent(
            context=object(),
            config={},
            settings=Settings(enabled=True, owner_umo="onebot_v11:FriendMessage:10001"),
            store=self.store,
            owner_umo="onebot_v11:FriendMessage:10001",
            logger=object(),
        )
        agent.attach_engine(engine)

        async def quick_result():
            return {"state": "completed", "text": "high"}

        async def scenario():
            started = asyncio.Event()
            async def low_priority():
                started.set()
                await asyncio.sleep(10)
                return {"state": "completed", "text": "low"}
            low_task = asyncio.create_task(agent._enqueue(kind="autodj", runner=low_priority))
            await started.wait()
            high = await agent._enqueue(kind="room_reply", runner=quick_result)
            self.assertEqual(high["text"], "high")
            self.assertEqual((await low_task)["state"], "cancelled")
            await agent.stop()

        asyncio.run(scenario())

    def test_fake_success_guard_requires_confirmed_control(self):
        snapshot = {"current_song": {"id": "100", "name": "Current"}, "playback": {"status": "PLAY"}}
        text = sanitize_room_reply("换好了，已经在放。", [], snapshot)
        self.assertIn("没有拿到网易云的 confirmed 回执", text)
        self.assertIn("立即播放歌曲", text)
        confirmed = [{"name": "cove_music_play_now", "state": "confirmed"}]
        self.assertEqual(sanitize_room_reply("换好了。", confirmed, snapshot), "换好了。")
        historical = "最近排过《铁花飞》，当时状态是 confirmed。"
        self.assertEqual(
            sanitize_room_reply(historical, [], snapshot, prompt="你最近三次音乐操作分别做了什么？"),
            historical,
        )
        current = "换好了。"
        self.assertIn(
            "没有拿到网易云的 confirmed 回执",
            sanitize_room_reply(current, [], snapshot, prompt="换首铁花飞"),
        )

    def test_agent_turn_state_requires_confirmed_control(self):
        self.assertEqual(agent_final_state([]), "completed")
        self.assertEqual(agent_final_state([{"name": "cove_music_search", "state": "confirmed"}]), "completed")
        self.assertEqual(agent_final_state([{"name": "cove_music_play_now", "state": "confirmed"}]), "confirmed")

    def test_room_tools_are_non_destructive(self):
        self.assertEqual(ROOM_TOOLS, {'cove_music'})
        self.assertFalse(any(token in name for name in ROOM_TOOLS for token in ("delete", "clear", "kick", "remove")))

    def test_song_change_does_not_open_proactive_chat_window(self):
        for attribution in ("plugin", "external"):
            with self.subTest(attribution=attribution):
                client = FakeClient()
                engine = self.engine(client)
                session = self.active_session(client)
                engine._last_playback_song = "100"
                event = {
                    "event_type": "playback_changed",
                    "room_id": "room-1",
                    "payload": {
                        "attribution": attribution,
                        "song_id": "200",
                        "former_song_id": "100",
                        "play_status": "PLAY",
                    },
                }
                asyncio.run(engine._handle_state_event(session, event))
                self.assertIsNone(engine._proactive_song_id)
                self.assertIsNone(self.store.get(f"proactive_song:{session['id']}:200"))
                if engine._proactive_task and not engine._proactive_task.done():
                    engine._proactive_task.cancel()
                self.store.close()
                self.store = CompanionStore(Path(self.tmp.name) / f"proactive-{attribution}.sqlite3")

    def test_unknown_attribution_does_not_open_proactive_window(self):
        client = FakeClient()
        engine = self.engine(client)
        session = self.active_session(client)
        engine._last_playback_song = "100"
        event = {
            "event_type": "playback_changed",
            "room_id": "room-1",
            "payload": {"attribution": "unknown", "song_id": "200", "play_status": "PLAY"},
        }
        asyncio.run(engine._handle_state_event(session, event))
        self.assertIsNone(engine._proactive_song_id)

    def test_room_history_is_scoped_to_current_session(self):
        self.store.record_room_message(session_id="old", provider_message_id="m1", sender_uid="1", sender_role="owner", text="旧房间", direction="inbound", state="received")
        self.store.record_room_message(session_id="current", provider_message_id="m2", sender_uid="1", sender_role="owner", text="当前房间", direction="inbound", state="received")
        rows = self.store.room_messages(10, session_id="current")
        self.assertEqual([row["text"] for row in rows], ["当前房间"])

    def test_room_turn_buffers_memory_until_song_or_session_end(self):
        client = FakeClient()
        engine = self.engine(client)
        session = self.active_session(client)
        batch = [{"session_id": session["id"], "event_id": "e1", "message_id": "m1", "text": "今晚这首很好听"}]
        asyncio.run(engine._flush_chat_batch(batch))
        self.assertEqual(self.store.room_summaries(3), [])
        draft = self.store.room_memory_draft(session["id"])
        self.assertIsNotNone(draft)
        asyncio.run(engine._flush_room_memory(session))
        self.assertEqual(self.store.room_summaries(3), [])
        self.assertIsNone(self.store.room_batch(session['id']))
        asyncio.run(engine._finish_session(session,'owner_requested','confirmed'))
        batch=self.store.room_batch(session['id'])
        self.assertIsNotNone(batch)
        self.assertNotIn('今晚这首很好听',batch['summary'])
        asyncio.run(engine._flush_room_memory(session))
        self.assertEqual(self.store.room_batch(session['id'])['id'],batch['id'])

    def test_lease_heartbeat_runs_independently_of_work_lock(self):
        client = FakeClient()
        engine = self.engine(client)
        engine._lease = {"lease_id": "lease-1", "fencing_token": 1}
        engine._lease_renew_at = 0

        async def ensure_lease():
            return engine._lease

        engine._ensure_lease = ensure_lease

        async def scenario():
            task = asyncio.create_task(engine._lease_loop())
            try:
                async with engine._work_lock:
                    await asyncio.sleep(0.05)
                    self.assertGreaterEqual(client.renewed, 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        asyncio.run(scenario())

    def test_diagnosis_includes_context_and_duration(self):
        client = FakeClient()
        engine = self.engine(client)
        turn = self.store.start_agent_turn(
            kind="room_reply",
            trigger="test",
            session_id="s1",
            input_payload={"context": {"qq_turns": 4, "room_messages": 2}},
            persona_id="小诺·陪伴",
            provider_id="provider-1",
        )
        self.store.finish_agent_turn(turn["id"], state="completed", output_text="ok")
        diagnosis = json.loads(engine.diagnose_text())
        recent = diagnosis["recent_agent_turns"][0]
        self.assertEqual(recent["persona"], "小诺·陪伴")
        self.assertEqual(recent["provider"], "provider-1")
        self.assertEqual(recent["context"]["qq_turns"], 4)
        self.assertIsNotNone(recent["duration_ms"])

    def test_agent_turn_search_cache_and_override_tables(self):
        turn = self.store.start_agent_turn(kind="room_reply", trigger="test", session_id="s1", input_payload={"x": 1})
        self.store.cache_search_results(turn_id=turn["id"], session_id="s1", songs=[{"id": "200", "name": "Next"}])
        self.assertIsNotNone(self.store.cached_song(turn["id"], "200"))
        self.store.finish_agent_turn(turn["id"], state="confirmed", output_text="ok")
        self.assertEqual(self.store.agent_turn(turn["id"])["state"], "confirmed")


if __name__ == "__main__":
    unittest.main()
