"""Recommendation evidence, no-effect previews and request-level billing."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

from astrbot_plugin_cove_companion.core.agent import CoveMusicAgent,CoveRoomEvent
from astrbot_plugin_cove_companion.core.config import Settings
from astrbot_plugin_cove_companion.core.store import CompanionStore
from astrbot_plugin_cove_companion.core.engine import CompanionEngine
from astrbot_plugin_cove_companion.core.metering import MeteredProvider,SharedTurnMeter,usage_of,tool_loop
from astrbot_plugin_cove_companion.core.recommendations import rank_songs
from test_core import FakeClient,FakeAgent

class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=CompanionStore(Path(self.temp.name)/'music.db')
        self.settings=Settings(enabled=True,owner_umo='onebot:FriendMessage:10001',exploration_rate=0)
        self.client=FakeClient();self.agent=FakeAgent()
        self.engine=CompanionEngine(context=object(),config={},settings=self.settings,store=self.store,client=self.client,agent=self.agent,logger=object(),qq_send=AsyncMock(return_value=True))
        s=self.store.create_session(self.settings.owner_umo,'room-1','epoch-1',True,True,0,'internal')
        self.store.bind_session(s['id'],self.settings.owner_umo,'music-1');self.store.update_session(s['id'],phase='active');self.session=self.store.session(s['id'])
    def tearDown(self):self.store.close();self.temp.cleanup()
    def run_async(self,fn):return asyncio.run(fn)
    def response(self,i=100,o=50):return SimpleNamespace(completion_text='hi',usage={'prompt_tokens':i,'completion_tokens':o})
    def test_provider_usage_cached_and_raw_formats(self):
        self.assertEqual(usage_of(SimpleNamespace(usage=SimpleNamespace(input_other=80,input_cached=20,output=30))),(100,30))
        self.assertEqual(usage_of({'raw_completion':{'usage':{'input_tokens':25,'output_tokens':10}}}),(25,10))
        self.assertIsNone(usage_of({'usage':{'input_tokens':-1,'output_tokens':2}}))
    def test_each_request_and_continuation_counted_once_not_each_tool(self):
        provider=SimpleNamespace(text_chat=AsyncMock(return_value=self.response()))
        proxy=MeteredProvider(provider,self.store,self.store.scope_key(),'room_reply',1000000,False)
        async def run():
            await proxy.text_chat(contexts=[{'role':'user','content':'hi'}])
            await proxy.text_chat(contexts=[{'role':'tool','content':'parallel results'}])
        self.run_async(run());summary=self.store.token_summary();self.assertEqual(summary['total_tokens'],300)
        self.assertEqual(sum(r['requests'] for r in summary['by_kind']),2)
    def test_reservation_is_atomic_cap_stops_auto_but_chat_continues(self):
        scope=self.store.scope_key();self.store.reserve_tokens('first',scope,'autodj',900,100,1000,True)
        with self.assertRaisesRegex(RuntimeError,'token_budget_reached'):self.store.reserve_tokens('second',scope,'autodj',1,1,1000,True)
        self.store.reserve_tokens('chat',scope,'room_reply',10,10,1000,False)
        self.store.reserve_tokens('chat',scope,'room_reply',10,10,1000,False)
        self.assertEqual(self.store.token_summary(1000)['total_tokens'],1020)
    def test_daily_reset_uses_shanghai_and_does_not_rewrite_yesterday(self):
        with patch('astrbot_plugin_cove_companion.core.config.day_key',return_value='2026-10-31'):
            self.store.reserve_tokens('old',self.store.scope_key(),'autodj',900,100,1000,True)
            self.assertTrue(self.store.token_summary(1000)['reached'])
        with patch('astrbot_plugin_cove_companion.core.config.day_key',return_value='2026-11-01'):
            self.assertEqual(self.store.token_summary(1000)['total_tokens'],0)
            self.store.reserve_tokens('new',self.store.scope_key(),'autodj',10,10,1000,True)
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM music_token_usage').fetchone()[0],2)
    def test_missing_usage_is_labeled_estimate_and_failure_retains_reservation(self):
        self.store.reserve_tokens('estimate',self.store.scope_key(),'room_reply',100,100,1000)
        self.store.settle_tokens('estimate',response=SimpleNamespace(completion_text='hello'))
        self.assertEqual(self.store.token_summary()['by_kind'][0]['source'],'utf8_estimate')
        self.store.reserve_tokens('fail',self.store.scope_key(),'autodj',20,40,1000)
        self.store.settle_tokens('fail',failed=True)
        row=self.store.conn.execute("SELECT * FROM music_token_usage WHERE request_id='fail'").fetchone()
        self.assertEqual(row['input_tokens']+row['output_tokens'],60);self.assertEqual(row['source'],'reserved_unknown')
    def test_shared_turn_includes_pre_tool_and_post_tool_responses(self):
        extra={};event=SimpleNamespace(get_extra=lambda k,d=None:extra.get(k,d))
        meter=SharedTurnMeter(SimpleNamespace(text_chat=AsyncMock(return_value=self.response())),self.store,event,self.store.scope_key(),1000000)
        async def run():
            await meter.text_chat(prompt='call tool')
            self.assertEqual(self.store.token_summary()['total_tokens'],0)
            extra['cove_shared_music_used']=True;meter.flush();meter.flush()
            await meter.text_chat(prompt='tool results')
            meter.flush()
        self.run_async(run());summary=self.store.token_summary();self.assertEqual(summary['total_tokens'],300);self.assertEqual(summary['by_kind'][0]['shared'],1)
    def test_shared_stream_chunks_are_one_request(self):
        async def stream(**_):
            yield SimpleNamespace(is_chunk=True,completion_text='a',usage={'prompt_tokens':100,'completion_tokens':2})
            yield SimpleNamespace(is_chunk=False,completion_text='all',usage={'prompt_tokens':100,'completion_tokens':10})
        e=SimpleNamespace(get_extra=lambda *args:True)
        meter=SharedTurnMeter(SimpleNamespace(text_chat_stream=stream),self.store,e,self.store.scope_key(),1000000)
        async def run():
            async for _ in meter.text_chat_stream(prompt='hi'):pass
        self.run_async(run());self.assertEqual(self.store.token_summary()['total_tokens'],110)
    def test_stream_settles_before_consumer_breaks_on_final_item(self):
        async def stream(**_):yield SimpleNamespace(is_chunk=False,usage={'prompt_tokens':60,'completion_tokens':20})
        meter=SharedTurnMeter(SimpleNamespace(text_chat_stream=stream),self.store,SimpleNamespace(get_extra=lambda *args:True),self.store.scope_key(),1000000)
        async def run():
            async for _ in meter.text_chat_stream(prompt='hi'):break
            self.assertEqual(self.store.token_summary()['total_tokens'],80)
        self.run_async(run())
    def test_real_astrbot_runner_uses_instance_meter(self):
        from astrbot.core.provider.entities import LLMResponse,TokenUsage
        from astrbot.core.agent.tool import ToolSet
        provider=SimpleNamespace(provider_config={'id':'fake'},text_chat=AsyncMock(return_value=LLMResponse(role='assistant',completion_text='哥哥，一起听。',usage=TokenUsage(input_other=100,output=12))))
        from astrbot.api.star import Context
        context=object.__new__(Context);context.provider_manager=SimpleNamespace(get_provider_by_id=AsyncMock(return_value=provider))
        agent=SimpleNamespace(context=context,store=self.store,settings=self.settings)
        event=CoveRoomEvent(owner_umo=self.settings.owner_umo,text='hi',turn_id='test',kind='room_reply')
        result=self.run_async(tool_loop(agent,event,'fake','hi',[],ToolSet(),'',2))
        self.assertIn('哥哥',result.completion_text);self.assertEqual(self.store.token_summary()['total_tokens'],112)
    def test_unmeterable_shared_turn_stops_automatic_requests(self):
        from astrbot_plugin_cove_companion.core.config import day_key
        self.store.set('token_meter_unavailable',day_key())
        with self.assertRaisesRegex(RuntimeError,'token_meter_unavailable'):self.store.reserve_tokens('auto',self.store.scope_key(),'autodj',10,10,1000,True)
    def test_strong_feedback_weak_playback_and_clear_have_different_effects(self):
        songs=[{'id':'200','artist':'Jazz Artist','tags':['爵士']},{'id':'201','artist':'Other'}]
        self.store.remember_songs(self.session,songs)
        self.store.record_history(self.session['id'],'200','Song','Jazz Artist','played','observation')
        profile=self.store.taste_profile(self.session);self.assertFalse(profile['known']);self.assertTrue(profile['inferred'])
        self.store.feedback(self.session,'200','like','longterm','明确喜欢')
        self.assertTrue(any(s['term']=='爵士' for s in self.store.taste_profile(self.session)['known']))
        self.store.correct_taste(self.session,'爵士','clear','明确操作')
        self.assertFalse(any(s['term']=='爵士' for s in self.store.taste_profile(self.session)['known']))
    def test_session_feedback_does_not_become_longterm_artist_preference(self):
        self.store.remember_songs(self.session,[{'id':'200','artist':'A'}]);self.store.feedback(self.session,'200','like','session','本场喜欢')
        self.assertFalse(self.store.taste_profile(self.session)['known'])
    def test_tastes_and_history_do_not_cross_binding_revision(self):
        self.store.correct_taste(self.session,'爵士','like','明确操作')
        self.store.update_session(self.session['id'],phase='ended')
        self.store.switch_binding(self.settings.owner_umo,'music-2')
        self.assertEqual(self.store.taste_profile({'owner_umo':self.settings.owner_umo}),{'known':[],'inferred':[]})
    def test_rank_weights_and_exploration_are_grounded(self):
        songs=[{'id':str(i),'artist':('爵士歌手' if i in (0,1) else '其他'),'name':str(i)} for i in range(20)]
        r=rank_songs(songs,preferences=['爵士'],intent=[],state={},memory_terms=[],history=[],feedback=[],explore=True)
        self.assertEqual(len([s for s in r if s.get('exploration')]),1)
        self.assertEqual(r[0]['id'],'0');self.assertEqual(r[0]['explanation']['weights'],{'taste':.7,'intent':.15,'diversity':.1,'persona':.05})
    def test_dry_run_writes_nothing_and_calls_no_model_or_commands(self):
        self.client.candidates=AsyncMock(return_value=[{'id':'200','name':'A','artist':'B'}]);self.agent.rank_candidates=AsyncMock()
        before=self.store.conn.total_changes
        r=self.run_async(self.engine.recommendation_dry_run())
        self.assertEqual(self.store.conn.total_changes,before);self.assertFalse(r['side_effects']);self.agent.rank_candidates.assert_not_awaited();self.assertFalse(self.client.commands);self.engine.qq_send.assert_not_awaited()
        r=self.run_async(self.engine.recommendation_dry_run('invitation',{'context':.8,'accumulation':.9,'relationship':1}))
        self.assertAlmostEqual(r['score'],.87);self.assertIn('session_exists',r['blockers']);self.assertEqual(self.store.conn.total_changes,before)
    def test_lifecycle_ignores_absence_third_party_disconnect_and_failed_snapshot(self):
        snapshot=self.run_async(self.client.snapshot(True));snapshot['owner_binding']['present']=False;snapshot['participants']=[{'uid':'999'}];snapshot['connected']=False
        self.engine.snapshot=AsyncMock(return_value=snapshot);self.engine.leave_result=AsyncMock()
        self.run_async(self.engine._lifecycle_tick());self.engine.leave_result.assert_not_awaited();self.assertEqual(self.store.session(self.session['id'])['phase'],'active')
        self.engine._lifecycle_checked_at=0;self.engine.snapshot=AsyncMock(side_effect=RuntimeError('offline'));self.run_async(self.engine._lifecycle_tick());self.engine.leave_result.assert_not_awaited()
    def test_recommendation_uses_room_evidence_when_members_unknown_or_absent(self):
        for presence in (None,False,True):
            with self.subTest(presence=presence):
                snapshot=self.run_async(self.client.snapshot(True));snapshot['owner_binding']['present']=presence
                snapshot['evidence']['members']['stale']=presence is None
                self.engine.snapshot=AsyncMock(return_value=snapshot)
                self.engine._reconcile_operations=AsyncMock()
                self.agent.run_autodj=AsyncMock(return_value={'state':'confirmed'})
                self.store.cycle(self.session["id"],True)
                with self.store.tx() as db:db.execute("UPDATE music_cycles SET qualified=1,baseline=0,runs=0,last_recommend_at=0,played_seconds=60")
                assessment=self.engine.recommendation_assessment(self.session,snapshot)
                self.assertNotIn('owner_presence_unconfirmed',assessment['blockers'])
                self.assertTrue(assessment['eligible'])
                result=self.run_async(self.engine._run_recommendation(self.session,manual=False))
                self.assertEqual(result['state'],'confirmed');self.agent.run_autodj.assert_awaited_once()
                self.assertFalse(self.client.commands)
    def test_recommendation_room_gate_rejects_wrong_room_epoch_stale_and_disconnected(self):
        for change,reason in (({'room_id':'other-room'},'room_not_ready'),({'room_epoch':'other-epoch'},'room_not_ready'),({'connected':False},'room_not_ready'),({'stale':True},'playback_evidence_stale')):
            with self.subTest(change=change):
                snapshot=self.run_async(self.client.snapshot(True))
                if 'stale' in change:snapshot['evidence']['playback']['stale']=True
                else:snapshot.update(change)
                self.engine.snapshot=AsyncMock(return_value=snapshot);self.agent.run_autodj=AsyncMock()
                self.assertIn(reason,self.engine.recommendation_assessment(self.session,snapshot)['blockers'])
                self.assertEqual(self.run_async(self.engine._run_recommendation(self.session,manual=False))['reason'],reason)
                self.agent.run_autodj.assert_not_awaited();self.assertFalse(self.client.commands)
    def test_room_ended_finishes_without_leave(self):
        self.client.room_id=None;self.engine.leave_result=AsyncMock();self.run_async(self.engine._lifecycle_tick())
        self.assertEqual(self.store.session(self.session['id'])['end_reason'],'room_ended');self.engine.leave_result.assert_not_awaited();self.assertFalse(self.client.commands)
    def test_shared_hook_wraps_only_this_runner_and_preserves_global_provider(self):
        from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS
        from astrbot_plugin_cove_companion.main import CoveCompanionPlugin
        extra={};event=SimpleNamespace(unified_msg_origin=self.settings.owner_umo,get_extra=lambda k,d=None:extra.get(k,d),set_extra=lambda k,v:extra.update({k:v}))
        provider=SimpleNamespace(text_chat=AsyncMock(return_value=self.response()))
        runner=SimpleNamespace(provider=provider,fallback_providers=[])
        plugin=object.__new__(CoveCompanionPlugin);plugin.can_manage=lambda _:True;plugin.store=self.store;plugin.settings=self.settings
        with patch.dict(_ACTIVE_AGENT_RUNNERS,{event.unified_msg_origin:runner}):
            self.run_async(plugin.on_music_agent_begin(event,None))
        self.assertIsInstance(runner.provider,SharedTurnMeter);self.assertIs(runner.provider.provider,provider)
        self.assertEqual(self.store.get('shared_meter_state'),'request_level');self.assertFalse(isinstance(provider,SharedTurnMeter))
    def test_full_budget_does_not_block_manual_playback_or_leave(self):
        self.store.reserve_tokens('full',self.store.scope_key(),'autodj',1000000,0,1000000)
        r=self.run_async(self.engine.execute_verified('PAUSE',{},session=self.session,origin_kind='dashboard'))
        self.assertEqual(r['state'],'confirmed')
        self.assertEqual(self.run_async(self.engine._run_recommendation(self.session,manual=False))['reason'],'token_budget_reached')
    def test_cleared_journal_removes_only_its_explicit_feedback(self):
        self.store.commit_room_batch(self.session,'摘要',{},[],{})
        self.store.feedback(self.session,'200','like','longterm','手帐评价','journal:'+self.session['id'])
        self.store.feedback(self.session,'201','like','longterm','面板评价')
        self.store.edit_room_batch(self.session['id'],'',1,True)
        self.assertEqual([f['song_id'] for f in self.store.feedback_items(self.session)],['201'])

