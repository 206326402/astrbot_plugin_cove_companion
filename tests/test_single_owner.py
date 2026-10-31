"""Failure and isolation cases for the single-owner upgrade."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from astrbot_plugin_cove_companion.core.config import Settings
from astrbot_plugin_cove_companion.core.store import CompanionStore
from astrbot_plugin_cove_companion.core.engine import CompanionEngine
from astrbot_plugin_cove_companion.core.recommendations import rank_songs
from astrbot_plugin_cove_companion.main import CoveCompanionPlugin
from test_core import FakeClient,FakeAgent

class SingleOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.file=Path(self.temp.name)/'music.sqlite3';self.store=CompanionStore(self.file)
        self.settings=Settings(enabled=True,owner_umo='onebot_v11:FriendMessage:10001',owner_netease_uid='10001')
        self.client=FakeClient();self.agent=FakeAgent();self.send=AsyncMock(return_value=True)
        self.engine=CompanionEngine(context=object(),config={},settings=self.settings,store=self.store,client=self.client,agent=self.agent,logger=object(),qq_send=self.send)
        self.session=self.store.create_session(self.settings.owner_umo,'room-1','epoch-1',True,False,0,'internal')
        self.store.update_session(self.session['id'],phase='active');self.session=self.store.session(self.session['id'])
    def tearDown(self):self.store.close();self.temp.cleanup()
    def run_async(self,c):return asyncio.run(c)
    def arm_pause(self):
        self.store.update_session(self.session['id'],pause_since=time.time()-301,last_evidence_at=time.time()-5)
        self.client.status='PAUSE';self.engine._lifecycle_checked_at=0
    def test_repeated_pause_keeps_original_timer(self):
        self.client.status='PAUSE';old=time.time()-50;self.store.update_session(self.session['id'],pause_since=old,last_evidence_at=time.time()-5)
        self.run_async(self.engine._lifecycle_tick());self.assertEqual(self.store.session(self.session['id'])['pause_since'],old)
    def test_snapshot_validated_during_read_is_fresh_after_read(self):
        self.client.status='PAUSE';old=time.time()-50
        self.store.update_session(self.session['id'],pause_since=old,last_evidence_at=time.time()-5)
        with patch('astrbot_plugin_cove_companion.core.lifecycle.time.time',return_value=time.time()) as clock:
            async def read_snapshot(_fresh):
                clock.return_value+=1
                return await self.client.snapshot(True)
            self.engine.snapshot=read_snapshot
            self.run_async(self.engine._lifecycle_tick())
        self.assertEqual(self.store.session(self.session['id'])['pause_since'],old)
    def test_resumed_before_exit_cancels_timer(self):
        self.arm_pause();paused=self.run_async(self.client.snapshot(True));playing={**paused,'playback':{'status':'PLAY'}}
        self.engine.snapshot=AsyncMock(side_effect=[paused,playing]);self.engine.leave_result=AsyncMock()
        self.run_async(self.engine._lifecycle_tick());self.engine.leave_result.assert_not_awaited();self.assertIsNone(self.store.session(self.session['id'])['pause_since'])
    def test_expired_playback_interrupts_continuity(self):
        self.arm_pause();snap=self.run_async(self.client.snapshot(True));snap['evidence']['playback']['stale']=True
        self.engine.snapshot=AsyncMock(return_value=snap);self.engine.leave_result=AsyncMock();self.run_async(self.engine._lifecycle_tick())
        self.assertIsNone(self.store.session(self.session['id'])['pause_since']);self.engine.leave_result.assert_not_awaited()
    def test_scheduler_downtime_does_not_count_as_pause(self):
        self.arm_pause();self.store.update_session(self.session['id'],last_evidence_at=time.time()-100)
        self.engine.leave_result=AsyncMock();self.run_async(self.engine._lifecycle_tick());self.engine.leave_result.assert_not_awaited()
        self.assertGreater(self.store.session(self.session['id'])['pause_since'],time.time()-5)
    def test_continuous_pause_ends_only_after_fresh_verification(self):
        self.arm_pause();self.engine.leave_result=AsyncMock();self.run_async(self.engine._lifecycle_tick());self.engine.leave_result.assert_awaited_once_with(session_id=self.session['id'],reason='pause_timeout')
    def test_exit_unknown_has_terminal_local_state_and_one_summary(self):
        self.store.update_session(self.session['id'],phase='leaving',leaving_since=time.time()-121)
        self.run_async(self.engine._lifecycle_tick());self.run_async(self.engine._lifecycle_tick())
        s=self.store.session(self.session['id']);self.assertEqual(s['phase'],'ended');self.assertEqual(s['remote_result'],'unknown');self.assertFalse(s['autodj_enabled'])
        self.assertEqual(len([e for e in self.store.music_events() if e['event_type']=='music.session.ended']),1)
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM music_room_batches').fetchone()[0],1)
    def test_expired_original_operation_is_abandoned_without_reissuing(self):
        self.store.record_operation('op1',self.session['id'],'LEAVE','dashboard','req1','unknown');self.store.track_operation('op1',self.session)
        with self.store.tx() as db:db.execute('UPDATE operation_deadlines SET deadline=?,next_check=0',(time.time()-1,))
        self.run_async(self.engine._reconcile_operations());self.run_async(self.engine._reconcile_operations())
        self.assertEqual(self.store.operations(20,session_id=self.session['id'])[0]['state'],'abandoned');self.assertEqual(self.client.commands,[]);self.assertEqual(self.send.await_count,1)
        self.assertEqual(self.store.operations(20,session_id=self.session['id'])[0]['result']['remote_result'],'unknown')
    def test_late_receipt_cannot_resurrect_an_abandoned_operation(self):
        self.store.record_operation('op1',self.session['id'],'JOIN','dashboard','req1','unknown');self.store.track_operation('op1',self.session);self.store.reconcile_attempt('op1','abandoned',{'remote_result':'unknown'})
        self.store.record_operation('op1',self.session['id'],'JOIN','dashboard','req1','confirmed');self.assertEqual(self.store.operations(20,session_id=self.session['id'])[0]['state'],'abandoned')
    def test_cached_shortlist_reused_without_new_model_call(self):
        self.agent.rank_candidates=AsyncMock(return_value={'songs':[{'id':'200','reason':'依据A'},{'id':'201','reason':'依据B'}]})
        self.engine._collect_ranked_candidates=AsyncMock(return_value=[{'id':'200'},{'id':'201'}]);a=self.run_async(self.engine._ensure_shortlist(self.session));b=self.run_async(self.engine._ensure_shortlist(self.session));self.assertEqual(a['id'],b['id']);self.agent.rank_candidates.assert_awaited_once()
    def test_exhausted_budget_returns_skip_without_claiming_prepared_candidates(self):
        self.agent.rank_candidates=AsyncMock(return_value={'state':'budget_exhausted','songs':[]})
        self.engine._collect_ranked_candidates=AsyncMock(return_value=[{'id':'200'},{'id':'201'}])
        p=object.__new__(CoveCompanionPlugin);p.store=self.store;p.engine=self.engine;p.settings=self.settings
        r=self.run_async(p._music_action('candidates',{},source='dashboard',evidence='明确操作',request_key='budget-test'))
        self.assertEqual(r['state'],'skipped');self.assertEqual(r['reason'],'model_budget_exhausted');self.assertNotIn('batch',r)
        r=self.run_async(self.engine._recommend_cached(self.session))
        self.assertEqual(r['state'],'budget_exhausted');self.assertEqual(r['reason'],'model_budget_exhausted');self.assertFalse(self.client.commands)

    def test_shortlist_hydrates_real_cover_with_three_track_bound(self):
        self.client.tracks=AsyncMock(return_value=[{'id':'200','coverUrl':'https://music.example/real.jpg','durationMs':180000}])
        rows=self.run_async(self.engine._hydrate_shortlist([{'id':str(i),'name':'真实歌曲','reason':'来源'} for i in range(200,205)]))
        self.assertEqual(len(rows),3);self.assertEqual(rows[0]['cover_url'],'https://music.example/real.jpg');self.assertEqual(rows[0]['duration_ms'],180000)
        self.client.tracks.assert_awaited_once_with(['200','201','202']);self.assertFalse(self.client.commands)

    def test_invalid_model_choices_do_not_claim_candidate_success(self):
        self.agent.rank_candidates=AsyncMock(return_value={'songs':[{'id':'forged','reason':'伪造'}]})
        self.engine._collect_ranked_candidates=AsyncMock(return_value=[{'id':'200'},{'id':'201'}])
        p=object.__new__(CoveCompanionPlugin);p.store=self.store;p.engine=self.engine;p.settings=self.settings
        r=self.run_async(p._music_action('candidates',{},source='dashboard',evidence='明确操作',request_key='ranking-test'))
        self.assertEqual(r['state'],'skipped');self.assertEqual(r['reason'],'invalid_candidate_ranking');self.assertFalse(self.client.commands)

    def test_fake_song_id_never_reaches_queue(self):
        batch=self.store.save_shortlist(self.session,[{'id':'200','reason':'来源'}]);r=self.run_async(self.engine.select_shortlist(self.session,batch['id'],'999999'))
        self.assertEqual(r['reason'],'song_outside_batch');self.assertFalse(self.client.commands)
    def test_switch_account_invalidates_old_batch_and_scopes_feedback(self):
        self.store.save_shortlist(self.session,[{'id':'200'}]);oldscope=self.store.scope_key(self.session);self.store.feedback(self.session,'200','like','longterm','用户明确说喜欢')
        self.store.update_session(self.session['id'],phase='ended');b=self.store.switch_binding(self.settings.owner_umo,'new-account');self.assertEqual(b['revision'],2)
        self.assertNotEqual(oldscope,self.store.scope_key());self.assertEqual(self.store.feedback_items({'owner_umo':self.settings.owner_umo}),[]);self.assertIsNone(self.store.shortlist(self.session))
    def test_job_idempotency_and_restart_preserve_unknown(self):
        p={'action':'pause','payload':{}};a,created=self.store.request_job('idempotency1',self.store.scope_key(),p,2);self.assertTrue(created)
        self.assertFalse(self.store.request_job('idempotency1',self.store.scope_key(),p,2)[1])
        with self.assertRaisesRegex(ValueError,'idempotency_conflict'):self.store.request_job('idempotency1',self.store.scope_key(),p,3)
        self.store.close();self.store=CompanionStore(self.file);r=self.store.music_job('idempotency1');self.assertEqual(r['state'],'abandoned');self.assertEqual(r['result']['remote_result'],'unknown')
    def test_nested_failed_transaction_rolls_back_complete_batch(self):
        with self.assertRaises(RuntimeError):
            with self.store.tx():
                self.store.commit_room_batch(self.session,'不应留下',{},[],{});raise RuntimeError()
        self.assertIsNone(self.store.room_batch(self.session['id']))
    def test_state_requires_evidence_and_finite_values(self):
        with self.assertRaisesRegex(ValueError,'state_evidence'):self.store.update_music_state(self.store.scope_key(),{'emotion':.5})
        with self.assertRaisesRegex(ValueError,'finite'):self.store.update_music_state(self.store.scope_key(),{'energy':float('nan'),'evidence':'来源'})
        with self.assertRaisesRegex(ValueError,'weather_source'):self.store.update_music_state(self.store.scope_key(),{'weather':{'text':'晴'},'evidence':'来源'})
    def test_no_new_event_never_calls_model_for_invite(self):
        self.engine._ensure_shortlist=AsyncMock();r=self.run_async(self.engine.evaluate_invitation('','private_conversation','',{}));self.assertEqual(r['state'],'skipped');self.engine._ensure_shortlist.assert_not_awaited()
    def test_declining_current_invitation_cannot_be_reinterpreted_as_acceptance(self):
        p=object.__new__(CoveCompanionPlugin);p.engine=self.engine;p.store=self.store;p.settings=self.settings;p.can_manage=lambda e:True
        ident='abcdefgh-123';self.engine.pending_invitation=lambda:{'id':ident};self.engine.respond_invitation=AsyncMock()
        e=SimpleNamespace(get_extra=lambda *a:False,get_message_str=lambda:'拒绝 abcdefgh',unified_msg_origin=self.settings.owner_umo)
        r=json.loads(self.run_async(p.primary_music_tool(e,'invite_reply',{'invitation_id':ident,'accept':True})))
        self.assertEqual(r['state'],'rejected');self.engine.respond_invitation.assert_not_awaited()
    def test_diversity_and_latest_explicit_feedback_are_applied(self):
        songs=[{'id':str(i),'category':'a' if i<3 else 'b','name':'歌','artist':'歌手'} for i in range(5)]
        history=[{'state':'played','evidence':{'category':'a'}}]*3
        result=rank_songs(songs,preferences=[],intent=[],state={},memory_terms=[],history=history,feedback=[{'song_id':'3','kind':'less'},{'song_id':'3','kind':'like'}])
        self.assertEqual([s['id'] for s in result],['4']);self.assertEqual(result[0]['audio_features']['source'],'unknown')

    def test_changed_intent_invalidates_idle_candidates_and_preserves_parameter_sources(self):
        pseudo={'id':None,'owner_umo':self.settings.owner_umo,'decision_revision':0}
        self.store.save_shortlist(pseudo,[{'id':'200'}])
        state=self.store.update_music_state(self.store.scope_key(),{'session_intent':'安静一点','energy':.2,'evidence':'用户说今天想安静听歌'})
        self.assertIsNone(self.store.shortlist(pseudo));self.assertEqual(state['parameter_evidence']['energy']['evidence'],'用户说今天想安静听歌')
        newer=self.store.update_music_state(self.store.scope_key(),{'emotion':.5,'evidence':'后续消息'})
        self.assertEqual(newer['parameter_evidence']['energy'],state['parameter_evidence']['energy'])

    def test_slash_accept_is_handled_once_and_disabled_accept_does_not_create(self):
        p=object.__new__(CoveCompanionPlugin);p.engine=self.engine;p.store=self.store;p.settings=self.settings;p.can_manage=lambda e:True
        self.engine.pending_invitation=lambda:{'id':'abcdefgh-123'};self.engine.respond_invitation=AsyncMock(return_value={'state':'confirmed'})
        e=SimpleNamespace(get_message_str=lambda:'/一起听 abcdefgh',plain_result=lambda text:text)
        async def collect():return [r async for r in p.together_command(e)]
        self.assertTrue(self.run_async(collect()));self.engine.respond_invitation.assert_awaited_once_with('abcdefgh-123',True)
        self.settings.enabled=False;self.run_async(collect());self.assertEqual(self.engine.respond_invitation.await_count,1)

    def test_music_context_uses_real_temporary_parts_and_never_persists(self):
        from astrbot.core.agent.message import Message,TextPart
        from astrbot.core.provider.entities import ProviderRequest,LLMResponse
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage
        p=object.__new__(CoveCompanionPlugin);p.engine=self.engine;p.store=self.store;p.settings=self.settings;p.can_manage=lambda e:True
        self.store.emit_music_event('start-test','music.session.started',{'session_id':self.session['id']})
        values={};e=SimpleNamespace(unified_msg_origin=self.settings.owner_umo,set_extra=lambda k,v:values.update({k:v}),get_extra=lambda k,d=None:values.get(k,d))
        stage=object.__new__(InternalAgentSubStage);saved=[]
        async def persist(umo,cid,history,**kwargs):saved.append(history)
        stage.conv_manager=SimpleNamespace(update_conversation=persist)
        async def rounds():
            history=[]
            for i in range(20):
                req=ProviderRequest(prompt='一起听了吗',contexts=history,conversation=SimpleNamespace(cid='music-test',token_usage=0))
                await p.on_music_llm_request(e,req);await p.on_music_llm_request(e,req)
                self.assertEqual(len(req.extra_user_content_parts),1);part=req.extra_user_content_parts[0];self.assertTrue(part._no_save)
                messages=[Message.model_validate(h) for h in history]+[Message(role='user',content=[TextPart(text=req.prompt),part]),Message(role='assistant',content='收到')]
                await stage._save_to_history(e,req,LLMResponse(role='assistant',completion_text='收到'),messages,None)
                self.assertNotIn('[本轮音乐状态]',json.dumps(saved[-1],ensure_ascii=False));history=saved[-1]
            await p.after_music_message_sent(e)
        self.run_async(rounds());self.assertEqual(len(saved[-1]),40);self.assertFalse(self.store.music_events(pending=True))

    def test_large_event_packet_is_complete_and_only_delivered_events_acknowledged(self):
        from astrbot.core.provider.entities import ProviderRequest
        p=object.__new__(CoveCompanionPlugin);p.engine=self.engine;p.store=self.store;p.settings=self.settings;p.can_manage=lambda e:True
        for i in range(7):self.store.emit_music_event('large-'+str(i),'music.session.ended',{'session_id':str(i),'reason':'暂停'*2000,**{str(n):'长内容'*1000 for n in range(10)}})
        values={};e=SimpleNamespace(set_extra=lambda k,v:values.update({k:v}),get_extra=lambda k,d=None:values.get(k,d))
        req=ProviderRequest(prompt='状态')
        self.run_async(p.on_music_llm_request(e,req))
        text=req.extra_user_content_parts[0].text.removeprefix('[本轮音乐状态]');packet=json.loads(text)
        self.assertLessEqual(len(text),1800);seqs=[x['seq'] for x in packet['music_events']];self.assertTrue(seqs)
        self.run_async(p.after_music_message_sent(e));remaining={x['seq'] for x in self.store.music_events(pending=True)}
        self.assertEqual(len(remaining),7-len(seqs));self.assertFalse(remaining.intersection(seqs))

    def test_legacy_expired_operations_migrate_without_notification_flood(self):
        self.store.record_operation('old-task',self.session['id'],'PAUSE','qq_command','old-task','unknown',result={})
        with self.store.tx() as db:db.execute('UPDATE operations SET created_at=? WHERE operation_id=?',(time.time()-86400,'old-task'))
        self.store.close();self.store=CompanionStore(self.file)
        old=next(o for o in self.store.reconciliation_due() if o['operation_id']=='old-task')
        self.assertEqual(old['notified'],1);self.assertFalse(self.store.notify_operation_once('old-task'))

    def test_old_energy_is_not_refreshed_by_unrelated_state_update(self):
        scope=self.store.scope_key(self.session)
        self.store.update_music_state(scope,{'energy':.1,'session_intent':'安静','evidence':'昨天的要求'})
        state=self.store.music_state(scope)
        for value in state['parameter_evidence'].values():value['updated_at']=time.time()-90000
        with self.store.tx() as db:db.execute('UPDATE music_state SET state_json=? WHERE scope_key=?',(json.dumps(state),scope))
        self.store.update_music_state(scope,{'closeness':.5,'evidence':'今天的聊天'})
        filtered=self.engine._selection_state(self.session)
        self.assertNotIn('energy',filtered);self.assertNotIn('session_intent',filtered);self.assertEqual(filtered['closeness'],.5)

    def test_immediate_play_uses_validated_batch_and_not_recommendation_slot(self):
        batch=self.store.save_shortlist(self.session,[{'id':'200','name':'真实歌'}])
        self.client.tracks=AsyncMock(return_value=[{'id':'200','available':True}]);self.client.availability=AsyncMock(return_value=True)
        self.engine.play_now_result=AsyncMock(return_value={'state':'confirmed'});self.engine.recommend_from_agent=AsyncMock()
        result=self.run_async(self.engine.select_shortlist(self.session,batch['id'],'999',immediate=True))
        self.assertEqual(result['reason'],'song_outside_batch');self.engine.play_now_result.assert_not_awaited()
        result=self.run_async(self.engine.select_shortlist(self.session,batch['id'],'200',immediate=True))
        self.assertEqual(result['state'],'confirmed');self.engine.play_now_result.assert_awaited_once_with('200',origin='qq_command');self.engine.recommend_from_agent.assert_not_awaited()
