"""1.3 behavior contracts: bounded search, durable cadence and introduction delivery."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from test_recommendation_workbench import WorkbenchTests
from astrbot_plugin_cove_companion.core.catalog_search import describe_query
from astrbot_plugin_cove_companion.core.music_model import introduction
from astrbot_plugin_cove_companion.core.music_model import MusicModel
from astrbot_plugin_cove_companion.core.store import CompanionStore


class V13Tests(unittest.TestCase):
    setUp=WorkbenchTests.setUp
    tearDown=WorkbenchTests.tearDown
    run_async=WorkbenchTests.run_async
    def listen(self,song_id,at,duration=180,progress=0,origin='user_or_unknown',reliable=True):
        return self.store.observe_listening(self.session,{'id':song_id,'name':'曲目'+song_id,'duration_ms':duration*1000},
                                          {'status':'PLAY','progress_ms':progress*1000},origin,at=at,reliable=reliable)

    def test_first_60_then_alternating_two_three_tracks_deduplicates_seeks(self):
        self.listen('1',1000)
        self.listen('1',1030,progress=30)
        self.listen('1',1059,progress=59)
        self.assertEqual(self.store.cycle(self.session['id'])['qualified'],0)
        self.listen('1',1060,progress=60)
        self.listen('1',1060,progress=0) # duplicate or seek, not a new playback
        self.assertEqual(self.store.cycle(self.session['id'])['qualified'],1)
        self.store.advance_cycle(self.session['id'],1060)
        self.assertEqual(self.store.cycle(self.session['id'])['target'],2)
        for ident,start in [('2',1100),('3',1200)]:
            for dt in (0,30,60):self.listen(ident,start+dt,progress=dt)
        c=self.store.cycle(self.session['id']);self.assertEqual(c['qualified']-c['baseline'],2)
        self.store.advance_cycle(self.session['id'],1260)
        self.assertEqual(self.store.cycle(self.session['id'])['target'],3)
        self.store.advance_cycle(self.session['id'],1400)
        self.assertEqual(self.store.cycle(self.session['id'])['target'],2)

    def test_short_track_requires_80_percent_and_disconnect_adds_no_time(self):
        self.listen('1',1000,duration=30)
        self.listen('1',1023,duration=30,progress=23)
        self.assertEqual(self.store.cycle(self.session['id'])['qualified'],0)
        self.listen('1',1024,duration=30,progress=24)
        self.assertEqual(self.store.cycle(self.session['id'])['qualified'],1)
        self.assertEqual(self.engine.cycle_assessment(self.session,{'current_song':{'id':'1'}})['reason'],'waiting_first_playback')
        self.listen('2',2000)
        self.listen('2',2300,progress=150)
        self.assertEqual(self.store.cycle(self.session['id'])['qualified'],1)
        self.listen('2',2330,reliable=False)
        self.assertEqual(self.store.cycle(self.session['id'])['qualified'],1)

    def test_user_replies_count_at_15_seconds_independently_of_recommendation(self):
        for ident,start in [('1',1000),('2',1100)]:
            self.listen(ident,start);self.listen(ident,start+15,progress=15)
        c=self.store.cycle(self.session['id'])
        self.assertEqual(c['user_count'],2);self.assertEqual(c['qualified'],0)
        self.listen('3',1200,origin='bot');self.listen('3',1215,progress=15,origin='bot')
        self.assertEqual(self.store.cycle(self.session['id'])['user_count'],2)

    def test_restart_preserves_instance_and_turns_sending_into_unknown(self):
        self.listen('1',1000);self.listen('1',1030,progress=30)
        ident=self.store.queue_introduction(self.session,'1','测试介绍',instance_id='play1')
        self.store.finish_introduction(ident,'sending')
        db=self.store.path if hasattr(self.store,'path') else Path(self.temp.name)/'music.db'
        self.store.close();self.store=CompanionStore(db);self.engine.store=self.store
        self.listen('1',1060,progress=60)
        self.assertEqual(self.store.cycle(self.session['id'])['qualified'],1)
        self.assertEqual(self.store.introductions(self.session['id'])[0]['state'],'unknown')

    def test_batches_do_not_share_purpose_scope_or_decision_revision(self):
        songs=[{'id':str(i),'name':'鸣潮 '+str(i),'artist':'A'} for i in range(45)]
        search=self.store.save_search_batch(self.session,songs,{'original':'鸣潮','kind':'work'})
        rec=self.store.save_shortlist(self.session,[{'id':'999','name':'日推'}])
        self.assertNotEqual(search['id'],rec['id'])
        self.assertEqual(len(self.store.search_batch(self.session,search['id'])['songs']),45)
        forged={**self.session,'owner_umo':'other:FriendMessage:99'}
        self.assertIsNone(self.store.search_batch(forged,search['id']))
        stale={**self.session,'decision_revision':self.session['decision_revision']+1}
        self.assertIsNone(self.store.search_batch(stale,search['id']))
        page=self.run_async(self.engine.search_result(self.session,'鸣潮',batch_id=search['id'],offset=20))
        self.assertEqual(len(page['songs']),20);self.assertEqual(page['next_offset'],40)
        self.assertEqual(self.store.shortlist(self.session)['songs'][0]['id'],'999')

    def test_work_catalog_expansion_rejects_unrelated_partial_characters(self):
        calls=[];active=0;peak=0
        async def catalog(route,word='',ident='',limit=30):
            nonlocal active,peak
            calls.append((route,word,ident));active+=1;peak=max(peak,active)
            try:
                await asyncio.sleep(.001)
                if route=='albums':return {'items':[{'id':'400','name':'鸣潮 原声集'}]}
                if route=='album_tracks':return {'songs':[{'id':'900','name':'无冠者','artist':'A','album':{'id':'400','name':'原声'}}]}
                if route=='artists':raise ConnectionError()
                return {'songs':[{'id':'901','name':'共鸣','artist':'周深'},{'id':'902','name':'鸣潮主题曲','artist':'A'}]}
            finally:active-=1
        self.client.catalog=catalog
        result=self.run_async(self.engine.search_catalog('鳴潮',allow_model=False))
        self.assertLessEqual(len(calls),8);self.assertLessEqual(peak,3)
        self.assertTrue(result['partial'])
        ids={s['id'] for s in result['songs']};self.assertIn('900',ids);self.assertNotIn('901',ids)
        self.assertTrue(next(s for s in result['songs'] if s['id']=='900')['work_evidence'])

    def test_model_rewrite_must_be_verified_by_real_catalog(self):
        async def catalog(route,word='',ident='',limit=30):
            if word=='夜曲' and route=='songs':return {'songs':[{'id':'9','name':'夜曲','artist':'周杰伦'}]}
            return {}
        self.client.catalog=catalog;self.agent.rewrite_music_query=AsyncMock(return_value='夜曲')
        result=self.run_async(self.engine.search_catalog('夜取'))
        self.agent.rewrite_music_query.assert_awaited_once()
        self.assertEqual([s['id'] for s in result['songs']],['9'])
        self.assertEqual(result['songs'][0]['candidate_source'],'model_query_verified_by_catalog')
        self.agent.rewrite_music_query.reset_mock()
        self.run_async(self.engine.search_catalog('夜取',dry_run=True))
        self.agent.rewrite_music_query.assert_not_awaited()

    def test_search_cache_pages_without_touching_recommendation(self):
        self.engine.search_catalog=AsyncMock(return_value={'songs':[{'id':str(i),'name':'Q'} for i in range(50)],'query':{'original':'Q','kind':'song'},'sources':[],'partial':False})
        async def run():
            a=await self.engine.search_result(self.session,'Q')
            b=await self.engine.search_result(self.session,'Q')
            self.assertEqual(a['id'],b['id'])
            self.assertEqual(len(a['songs']),20)
        self.run_async(run());self.engine.search_catalog.assert_awaited_once()
        self.assertIsNone(self.store.shortlist(self.session))

    def test_unknown_receipt_only_queries_original_operation_never_resends(self):
        ident=self.store.queue_introduction(self.session,'100','介绍文本',instance_id='p')
        self.store.finish_introduction(ident,'unknown',{'operation_id':'original'})
        self.client.operation=AsyncMock(return_value={'state':'confirmed','operation_id':'original'})
        self.run_async(self.engine._drain_introductions(self.session['id']))
        self.client.operation.assert_awaited_once_with('original')
        self.assertEqual(self.client.commands,[])
        self.assertEqual(self.store.introductions(self.session['id'])[0]['state'],'confirmed')

    def test_stale_song_cancels_intro_and_budget_blocks_without_sending(self):
        ident=self.store.queue_introduction(self.session,'999','介绍',instance_id='old',kind='user_selection')
        self.run_async(self.engine._drain_introductions(self.session['id']))
        self.assertEqual(self.store.introductions(self.session['id'])[0]['state'],'cancelled')
        self.store.queue_introduction(self.session,'100','介绍',instance_id='new',kind='user_selection')
        self.store.reserve_tokens('all',self.store.scope_key(),'autodj',1000000,0,1000000)
        self.run_async(self.engine._drain_introductions(self.session['id']))
        self.assertEqual(self.store.introductions(self.session['id'])[0]['state'],'blocked')
        self.assertEqual(self.client.commands,[])

    def test_intro_length_name_and_no_fixed_master_address(self):
        for text in ('','喜欢','哥哥，一起听这一首。'):
            value=introduction(text,{'name':'夜曲'},'目录相关')
            self.assertGreaterEqual(len(value),35);self.assertLessEqual(len(value),80)
            self.assertIn('夜曲',value);self.assertNotIn('主人',value)
        self.assertEqual(describe_query('ＷＵＴＨＥＲＩＮＧ　ＷＡＶＥＳ')['canonical'],'鸣潮')

    def test_unknown_song_source_does_not_claim_user_selection(self):
        model=MusicModel();model.music_json=AsyncMock(return_value={'text':'一起听《夜曲》。'})
        value=self.run_async(model.introduce_user_song(self.session,{'name':'夜曲','selection_verified':False}))
        self.assertIn('来源尚未核实',model.music_json.call_args.args[0])
        self.assertNotIn('你选的',value)
        self.run_async(model.introduce_user_song(self.session,{'name':'夜曲','selection_verified':True}))
        self.assertIn('用户亲自点了',model.music_json.call_args.args[0])

    def test_technical_audit_reason_never_becomes_spoken_padding(self):
        value=introduction('一起听《夜曲》。',{'name':'夜曲'},'rank_score=0.9，候选排序最高，数据库证据')
        self.assertGreaterEqual(len(value),35)
        self.assertNotIn('rank_score',value);self.assertNotIn('候选',value)
        value=introduction('《夜曲》在候选排序中最高。',{'name':'夜曲'})
        self.assertNotIn('候选',value);self.assertIn('夜曲',value)

    def test_request_ledger_can_disable_legacy_count_limits(self):
        self.assertIsNotNone(self.store.reserve_model_call('autodj',None,None)[0])
        self.assertIsNone(self.store.reserve_model_call('autodj',1,1)[0])
        self.assertIsNotNone(self.store.reserve_model_call('autodj',None,None)[0])

    def test_human_search_queues_ahead_of_pending_bot_without_consuming_auto_slot(self):
        rec=self.store.create_recommendation(self.session['id'],'auto',{'id':'102','name':'Bot'},'介绍','autodj')
        self.store.set_recommendation(rec['id'],'queued')
        batch=self.store.save_search_batch(self.session,[{'id':'200','name':'User'}],{'original':'User'})
        result=self.run_async(self.engine.select_shortlist(self.session,batch['id'],'200',origin='dashboard'))
        self.assertEqual(result['state'],'confirmed')
        self.assertEqual(self.client.queue[:3],['100','200','101'])
        self.assertIn('102',self.client.queue)
        self.assertEqual(self.store.pending_recommendation(self.session['id'])['id'],rec['id'])
        self.assertEqual(self.store.cycle(self.session['id'])['protect_song_id'],'200')
        self.assertEqual(self.store.decision_trace(self.session['id'])[0]['stage'],'manual_selection')

    def test_already_introduced_bot_song_does_not_get_a_second_transition_response(self):
        bot=self.store.queue_introduction(self.session,'100','先前介绍',recommendation_id='bot')
        for state in ('confirmed','unknown'):
            self.store.finish_introduction(bot,state)
            user=self.store.queue_introduction(self.session,'100',instance_id='play-'+state,kind='user_selection')
            self.run_async(self.engine._deliver_introduction(next(i for i in self.store.introductions(self.session['id']) if i['id']==user)))
            item=next(i for i in self.store.introductions(self.session['id']) if i['id']==user)
            self.assertEqual(item['state'],'cancelled')
            self.assertEqual(item['receipt']['reason'],'already_introduced')
        self.assertEqual(self.client.commands,[])


del WorkbenchTests
