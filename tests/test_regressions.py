from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_core import FakeAgent, FakeClient
from astrbot_plugin_cove_companion.core.config import Settings
from astrbot_plugin_cove_companion.core.engine import CompanionEngine
from astrbot_plugin_cove_companion.core.cove_client import CoveAmbiguousError
from astrbot_plugin_cove_companion.core.store import CompanionStore
from astrbot_plugin_cove_companion.core.text import parse_music_command
from astrbot_plugin_cove_companion.main import CoveCompanionPlugin


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CompanionStore(Path(self.tmp.name) / "regressions.sqlite3")
        self.settings = Settings(enabled=True, owner_umo="onebot:FriendMessage:42", owner_netease_uid="10001")
        self.client = FakeClient()
        self.engine = CompanionEngine(context=object(), config={}, settings=self.settings, store=self.store, client=self.client, agent=FakeAgent(), logger=object(), qq_send=AsyncMock())
        self.session = self.store.create_session(self.settings.owner_umo, "room-1", "epoch-1", True, True)
        self.store.update_session(self.session["id"], phase="active")
        self.session = self.store.active_session()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def plugin(self):
        plugin = CoveCompanionPlugin.__new__(CoveCompanionPlugin)
        plugin.config = {}
        plugin.settings = self.settings
        plugin.store = self.store
        plugin.engine = self.engine
        plugin.music_agent = self.engine.agent
        plugin._dashboard_control_lock = asyncio.Lock()
        return plugin

    @staticmethod
    def request(body):
        return SimpleNamespace(json=lambda **kwargs: body)

    @staticmethod
    def payload(response):
        return json.loads(response.body) if hasattr(response, "body") else response

    @staticmethod
    def status(response):
        return response.status_code if hasattr(response, "status_code") else response.get("status_code", 200)

    def test_casual_conversation_and_negations_do_not_control_music(self):
        for text in ["今天状态不好", "我不想退出", "不要暂停", "这个问题怎么排查", "我想听你说下一首", "聊天关闭后还能开吗", "陪我听了30分钟真开心"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_music_command(text))
        self.assertEqual(parse_music_command("/一起听 暂停！")["kind"], "pause")
        self.assertEqual(parse_music_command("先别推歌了")["enabled"], False)
        self.assertEqual(parse_music_command("请聊天 开")["enabled"], True)
        self.assertEqual(parse_music_command("/网易云陪听 状态")["kind"], "status")

    def test_introduction_does_not_release_pending_slot_or_overwrite_receipt(self):
        result = asyncio.run(self.engine.recommend_from_agent({"id": "200", "name": "Next", "artist": "A"}, "下一首听这首。"))
        pending = self.store.pending_recommendation(self.session["id"])
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(pending["state"], "queued")
        evidence = json.loads(pending["evidence_json"])
        self.assertIn("operation", evidence)
        self.assertIn("intro", evidence)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_recommendation(self.session["id"], "another-slot", {"id": "300"}, "", "test")

    def test_legacy_intro_state_blocks_duplicate_recommendation(self):
        rec = self.store.create_recommendation(self.session["id"], "old", {"id": "200"}, "", "test")
        self.store.set_recommendation(rec["id"], "queued_intro_sent")
        self.assertEqual(self.store.pending_recommendation(self.session["id"])["id"], rec["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_recommendation(self.session["id"], "new", {"id": "300"}, "", "test")

    def test_recommendation_releases_slot_only_after_reliable_playback(self):
        rec = self.store.create_recommendation(self.session["id"], "old", {"id": "200"}, "", "test")
        self.store.set_recommendation(rec["id"], "queued")
        snapshot = asyncio.run(self.client.snapshot())
        snapshot["current_song"]["id"] = "200"
        snapshot["evidence"] = {"playback": {"stale": True}}
        self.engine._reconcile_pending_recommendation(self.session, snapshot)
        self.assertIsNotNone(self.store.pending_recommendation(self.session["id"]))
        snapshot["evidence"]["playback"]["stale"] = False
        self.engine._reconcile_pending_recommendation(self.session, snapshot)
        self.assertIsNone(self.store.pending_recommendation(self.session["id"]))
        self.assertEqual(self.store.recommendation(rec["id"])["state"], "played")

    def test_failed_fresh_read_cannot_verify_or_submit_control(self):
        async def scenario():
            await self.engine.refresh_snapshot()
            self.engine._snapshot_at = 0
            self.client.snapshot = AsyncMock(side_effect=ConnectionError("offline"))
            cached = await self.engine.snapshot(False)
            self.assertFalse(cached["connected"])
            self.assertTrue(cached["evidence"]["playback"]["stale"])
            with self.assertRaises(ConnectionError):
                await self.engine.execute_verified("PAUSE", {}, session=self.session)
            self.assertEqual(self.client.commands, [])
        asyncio.run(scenario())

    def test_player_force_requests_real_fresh_snapshot(self):
        async def scenario():
            self.client.snapshot = AsyncMock(wraps=self.client.snapshot)
            await self.engine.player_state()
            await self.engine.player_state(force=True)
            self.assertEqual(self.client.snapshot.await_args.args, (True,))
        asyncio.run(scenario())

    def test_foreign_room_and_epoch_do_not_enter_chat_or_change_queue_state(self):
        async def scenario():
            for room_id, epoch in [("another-room", "epoch-1"), ("room-1", "old-epoch")]:
                event = {"event_id": room_id + epoch, "room_id": room_id, "room_epoch": epoch, "event_type": "chat_message", "actor_role": "owner", "actor_uid": "10001", "payload": {"text": "你好"}}
                await self.engine._handle_room_chat_event(self.session, event)
                event.update(event_type="queue_changed", payload={"attribution": "external", "display_list": ["900"]})
                await self.engine._handle_state_event(self.session, event)
            self.assertEqual(self.store.room_messages(10), [])
            self.assertEqual(self.store.external_overrides(), [])
            self.assertEqual(self.engine._chat_buffer, [])
        asyncio.run(scenario())

    def test_disabled_chat_can_be_reenabled_from_room_without_llm(self):
        self.store.update_session(self.session["id"], room_chat_enabled=0)
        session = self.store.active_session()
        asyncio.run(self.engine._handle_room_chat_event(session, {"event_id": "enable", "room_id": "room-1", "room_epoch": "epoch-1", "actor_role": "owner", "actor_uid": "10001", "payload": {"text": "聊天 开"}}))
        self.assertEqual(self.store.active_session()["room_chat_enabled"], 1)
        self.assertEqual([command["action"] for command in self.client.commands], ["SEND_MESSAGE"])

    def test_debounce_does_not_wait_for_next_long_poll(self):
        async def scenario():
            self.settings.chat_batch_wait_seconds = .01
            self.settings.chat_batch_max_wait_seconds = .03
            flushed = asyncio.Event()
            async def capture(batch):
                self.assertEqual(batch[0]["text"], "这首很好听")
                flushed.set()
            self.engine._flush_chat_batch = capture
            await self.engine._handle_room_chat_event(self.session, {"event_id": "chat", "room_id": "room-1", "actor_role": "owner", "actor_uid": "10001", "payload": {"text": "这首很好听"}})
            await asyncio.wait_for(flushed.wait(), timeout=.5)
        asyncio.run(scenario())

    def test_old_buffer_does_not_reply_into_new_session(self):
        batch = [{"session_id": self.session["id"], "text": "旧房间的话"}]
        self.store.create_session(self.settings.owner_umo, "room-2", "epoch-2", True, True)
        self.engine.agent.run_room_turn = AsyncMock()
        asyncio.run(self.engine._flush_chat_batch(batch))
        self.engine.agent.run_room_turn.assert_not_called()

    def test_dashboard_concurrent_retry_executes_once_and_survives_reload(self):
        async def scenario():
            self.engine.pause = AsyncMock(return_value="已暂停。")
            plugin = self.plugin()
            body = {"action": "pause", "idempotency_key": "same-click", "session_id": self.session["id"]}
            first, second = await asyncio.gather(plugin._api_control(self.request(body)), plugin._api_control(self.request(body)))
            self.assertEqual(self.payload(first), self.payload(second))
            self.engine.pause.assert_awaited_once()
            reloaded = self.plugin()
            self.assertEqual(self.payload(await reloaded._api_control(self.request(body))), self.payload(first))
            self.engine.pause.assert_awaited_once()
            conflict = await reloaded._api_control(self.request({**body, "action": "next"}))
            self.assertEqual(self.status(conflict), 409)
        asyncio.run(scenario())

    def test_dashboard_abandoned_request_is_not_replayed(self):
        plugin = self.plugin()
        body = {"action": "pause", "idempotency_key": "crashed"}
        import hashlib
        digest = hashlib.sha256(json.dumps({"action": "pause"}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.store.set("control:crashed", {"state": "running", "request_hash": digest})
        self.engine.pause = AsyncMock()
        result = asyncio.run(plugin._api_control(self.request(body)))
        self.assertEqual(self.status(result), 409)
        self.engine.pause.assert_not_called()

    def test_settings_preserve_term_lists_and_allow_clearing(self):
        async def scenario():
            plugin = self.plugin()
            await plugin._api_settings(self.request({"music_preferences": ["器乐", "Tycho"], "excluded_terms": ["现场"], "reference_playlist_ids": ["123", "456"]}))
            self.assertEqual(plugin.settings.music_preferences, ["器乐", "Tycho"])
            self.assertEqual(plugin.settings.reference_playlist_ids, ["123", "456"])
            await plugin._api_settings(self.request({"excluded_terms": "", "reference_playlist_ids": []}))
            self.assertEqual(plugin.settings.excluded_terms, [])
            self.assertEqual(plugin.settings.reference_playlist_ids, [])
        asyncio.run(scenario())

    def test_setting_validation_is_atomic(self):
        plugin = self.plugin()
        result = asyncio.run(plugin._api_settings(self.request({"music_preferences": ["new"], "recommendation_interval_seconds": "invalid"})))
        self.assertEqual(self.status(result), 400)
        self.assertEqual(plugin.config, {})

    def test_due_jobs_are_claimed_once_and_reclaimed_after_lease_expiry(self):
        job_id = self.store.enqueue_job("recommendation", "slot", time.time() - 1, self.session["id"])
        first = self.store.claim_due_job(["recommendation"])
        self.assertEqual(first["id"], job_id)
        self.assertEqual(first["attempts"], 1)
        self.assertIsNone(self.store.claim_due_job(["recommendation"]))
        with self.store.tx() as db:
            db.execute("UPDATE jobs SET locked_until=0 WHERE id=?", (job_id,))
        recovered = self.store.claim_due_job(["recommendation"])
        self.assertEqual(recovered["id"], job_id)
        self.assertEqual(recovered["attempts"], 2)

    def test_recommendation_job_retries_are_bounded(self):
        async def scenario():
            self.engine._run_recommendation = AsyncMock(side_effect=ConnectionError("offline"))
            self.store.enqueue_job("recommendation", "slot", 0, self.session["id"])
            for attempt in range(3):
                with self.store.tx() as db:
                    db.execute("UPDATE jobs SET run_at=0")
                await self.engine._run_recommendation_job(self.store.claim_due_job(["recommendation"]))
            self.assertEqual(self.store.jobs()[0]["status"], "failed")
        asyncio.run(scenario())

    def test_stopped_or_replaced_session_cannot_apply_old_autodj_result(self):
        async def scenario():
            self.store.update_session(self.session["id"], autodj_enabled=0)
            stopped = await self.engine.execute("ENQUEUE_NEXT", {"song_id": "200"}, session=self.session, origin_kind="autodj")
            self.assertEqual(stopped["state"], "cancelled")
            self.store.create_session(self.settings.owner_umo, "room-2", "epoch-2", True, True)
            replaced = await self.engine.execute("PAUSE", {}, session=self.session)
            self.assertEqual(replaced["state"], "cancelled")
            self.assertEqual(self.client.commands, [])
        asyncio.run(scenario())

    def test_chat_closed_while_model_runs_does_not_send_late_reply(self):
        async def late_model(**kwargs):
            self.store.update_session(self.session["id"], room_chat_enabled=0)
            return {"state": "completed", "text": "旧回复"}
        self.engine.agent.run_room_turn = late_model
        asyncio.run(self.engine._flush_chat_batch([{"session_id": self.session["id"], "text": "你好"}]))
        self.assertEqual(self.client.commands, [])

    def test_intro_completion_does_not_resurrect_played_recommendation(self):
        async def fake_intro(*args, **kwargs):
            rec = self.store.pending_recommendation(self.session["id"])
            self.store.set_recommendation(rec["id"], "played")
            return {"state": "confirmed"}
        self.engine._send_room = fake_intro
        result = asyncio.run(self.engine.recommend_from_agent({"id": "200"}, "介绍"))
        asyncio.run(self.engine._drain_introductions(self.session["id"]))
        self.assertEqual(self.store.recommendation(result["recommendation_id"])["state"], "played")

    def test_receipt_disconnect_marks_unknown_and_freezes_autodj(self):
        self.client.wait_operation = AsyncMock(side_effect=CoveAmbiguousError("offline", "lost receipt"))
        result = asyncio.run(self.engine.execute("PAUSE", {}, session=self.session))
        self.assertEqual(result["state"], "unknown")
        self.assertTrue(self.store.has_unresolved_operations())
        self.assertEqual(self.store.active_session()["autodj_enabled"], 0)
        self.assertEqual(len(self.client.commands), 1)

    def test_join_duplicate_invite_is_idempotent_and_does_not_create_a_session(self):
        plugin = self.plugin()
        self.engine.refresh_snapshot = AsyncMock(return_value=asyncio.run(self.client.snapshot(True)))
        self.engine.join = AsyncMock()
        message = asyncio.run(plugin._join_invite(SimpleNamespace(message_id="invite-1"), {"room_id": "room-1", "inviter_id": "10001"}))
        self.assertIn("已经在", message)
        self.engine.join.assert_not_called()
        self.assertEqual(self.store.active_session()["id"], self.session["id"])

    def test_failed_leave_blocks_room_switch(self):
        plugin = self.plugin()
        self.engine.leave_result = AsyncMock(return_value={"state": "unknown", "error": {"message": "receipt unavailable"}})
        self.engine.join = AsyncMock()
        message = asyncio.run(plugin._join_invite(SimpleNamespace(message_id="invite-switch"), {"room_id": "room-2", "inviter_id": "10001"}))
        self.assertIn("退出未确认", message)
        self.engine.join.assert_not_called()
        self.assertEqual(self.store.active_session()["id"], self.session["id"])

    def test_duplicate_invite_message_is_not_submitted_twice(self):
        plugin = self.plugin()
        self.store.set("invite:invite-replayed", {"room_id": "room-2", "processed_at": time.time()})
        self.engine.join = AsyncMock()
        message = asyncio.run(plugin._join_invite(SimpleNamespace(message_id="invite-replayed"), {"room_id": "room-2", "inviter_id": "10001"}))
        self.assertIn("已经处理过", message)
        self.engine.join.assert_not_called()

    def test_join_schedules_first_recommendation_after_verified_room(self):
        async def scenario():
            self.store.update_session(self.session["id"], phase="ended", ended_at=time.time())
            result = await self.engine.join("room-1", "10001")
            self.assertTrue(result["ok"])
            session = self.store.active_session()
            self.assertEqual(session["phase"], "active")
            self.assertGreater(float(session["next_recommend_at"]), time.time())
            self.assertEqual(session["next_eligibility_check_at"], session["next_recommend_at"])
        asyncio.run(scenario())

    def test_manual_recommendation_run_does_not_consume_or_hit_auto_quota(self):
        self.settings.autodj_daily_budget = 0
        self.settings.autodj_hourly_limit = 0
        run_id, reason = self.store.reserve_recommendation_run(self.session["id"], "manual", hourly_limit=None, daily_limit=None)
        self.assertIsNotNone(run_id)
        self.assertIsNone(reason)
        self.store.finish_recommendation_run(run_id, "confirmed")
        self.assertEqual(self.store.recommendation_usage()["auto_today"], 0)

    def test_large_queue_follows_current_song_and_exposes_every_page(self):
        async def scenario():
            self.client.queue = [str(1000 + index) for index in range(96)]
            self.client.current_id = "1075"
            self.client.tracks = AsyncMock(wraps=self.client.tracks)
            state = await self.engine.player_state(force=True)
            self.assertEqual(state["queue"]["total"], 96)
            self.assertEqual(state["queue"]["offset"], 50)
            self.assertEqual(state["queue"]["current_position"], 76)
            self.assertTrue(state["queue"]["items"][25]["is_current"])
            self.assertEqual(state["queue"]["items"][-1]["position"], 96)
            first = await self.engine.queue_page(0)
            self.assertEqual(len(first["items"]), 50)
            self.assertEqual(first["items"][0]["position"], 1)
            self.assertEqual(set(first["song_ids"]) | set(state["queue"]["song_ids"]), set(self.client.queue))
            for call in self.client.tracks.await_args_list:
                self.assertLessEqual(len(call.args[0]), 50)
            last = await self.engine.queue_page(9999)
            self.assertEqual(last["offset"], 50)
        asyncio.run(scenario())

    def test_queue_api_rejects_invalid_cursor_without_fetching(self):
        plugin = self.plugin()
        self.engine.queue_page = AsyncMock()
        result = asyncio.run(plugin._api_queue(SimpleNamespace(query_params={"offset": "-1"})))
        self.assertEqual(self.status(result), 400)
        self.engine.queue_page.assert_not_called()
