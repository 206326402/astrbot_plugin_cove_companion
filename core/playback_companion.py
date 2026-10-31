"""Event-driven recommendations and delayed, auditable room introductions."""
from __future__ import annotations
import asyncio
import json
import time
from .lifecycle import fresh


class PlaybackCompanion:
    def introduction_assessment(self, session, snapshot):
        budget=self.store.token_summary(self.settings.token_daily_limit);blockers=[]
        tasks=self.store.introductions(session['id'],pending=True) if session else []
        if not session or session.get('phase')!='active':blockers.append('no_active_session')
        if not tasks:blockers.append('no_pending_introduction')
        if not (fresh(snapshot,'room') and fresh(snapshot,'playback') and snapshot.get('connected') is True):blockers.append('playback_evidence_stale')
        if session and snapshot.get('room_id')!=session.get('room_id'):blockers.append('room_not_ready')
        if (snapshot.get('playback') or {}).get('status')!='PLAY':blockers.append('playback_not_playing')
        if budget['reached']:blockers.append('token_budget_reached')
        if not budget['meter_available']:blockers.append('token_meter_unavailable')
        messages=self.store.room_messages(10,session_id=session['id']) if session else []
        if self._chat_buffer or any(m.get('direction') in {'inbound','in'} and time.time()-m['created_at']<12 for m in messages):blockers.append('waiting_quiet_window')
        return {'eligible':not blockers,'blockers':blockers,'tasks':tasks,'quiet_window_seconds':12,'stable_seconds':15,'budget':budget}

    def cycle_assessment(self,session,snapshot):
        if not session:return {'eligible':False,'reason':'no_active_session'}
        c=self.store.cycle(session['id']);at=time.time()
        current=str((snapshot.get('current_song') or {}).get('id') or '')
        if c['protect_song_id'] and current!=c['protect_song_id']:
            return {'eligible':False,'reason':'manual_next_reserved','cycle':c}
        if c['protect_until']>at:return {'eligible':False,'reason':'queue_protected','until':c['protect_until'],'cycle':c}
        if c['last_recommend_at'] and at-c['last_recommend_at']<180:
            return {'eligible':False,'reason':'recommendation_cooldown','until':c['last_recommend_at']+180,'cycle':c}
        needed=1 if c['runs']==0 else c['target']
        counted=c['qualified']-c['baseline']
        if c['runs']==0 and c['played_seconds']<60:
            return {'eligible':False,'reason':'waiting_first_playback','cycle':c,'counted':counted,'needed':needed,'remaining_seconds':max(0,60-c['played_seconds'])}
        return {'eligible':counted>=needed,'reason':None if counted>=needed else 'waiting_tracks','cycle':c,'counted':counted,'needed':needed,'remaining':max(0,needed-counted)}

    async def _companion_tick(self):
        session=self.store.active_session()
        if not session or session['phase']!='active':return
        snapshot=await self.snapshot(False);song=snapshot.get('current_song') or {}
        reliable=fresh(snapshot,'room') and fresh(snapshot,'playback') and snapshot.get('connected') is True and snapshot.get('room_id')==session['room_id']
        rec=None
        with self.store.lock:
            rec=self.store.conn.execute('SELECT * FROM recommendations WHERE session_id=? AND song_id=? ORDER BY created_at DESC LIMIT 1',(session['id'],str(song.get('id') or ''))).fetchone()
        selection=json.loads(rec['evidence_json'] or '{}').get('selection',{}) if rec else {}
        # A historical recommendation for the same song does not own future user replays.
        owned=rec and (rec['state'] in {'queued','enqueueing','unknown'} or (rec['state']=='played' and time.time()-rec['updated_at']<35))
        origin=('user' if selection.get('purpose')=='search' else 'bot') if owned else 'unknown'
        manual=next((d for d in self.store.decision_trace(session['id']) if d['stage']=='manual_selection' and d['detail'].get('state')=='confirmed' and str(d['detail'].get('song_id'))==str(song.get('id')) and time.time()-d['created_at']<35),None)
        if manual and (not owned or manual['created_at']>=rec['updated_at']):origin='user'
        instance=self.store.observe_listening(session,song,snapshot.get('playback') or {},origin,reliable=reliable)
        cycle=self.store.cycle(session['id'])
        if cycle['protect_song_id']:
            target=cycle['protect_song_id'];queue={str(x) for x in (snapshot.get('queue') or {}).get('song_ids',[])}
            if (instance and instance['song_id']==target and instance['listened']>=60) or (fresh(snapshot,'queue') and target not in queue and str(song.get('id'))!=target):
                with self.store.tx() as db:db.execute('UPDATE music_cycles SET protect_until=0,protect_song_id=NULL WHERE session_id=?',(session['id'],))
        if instance and instance['user_counted'] and instance['origin']!='bot' and cycle['user_count']-cycle['user_baseline']>=cycle['user_target'] and session.get('room_chat_enabled'):
            ident=self.store.queue_introduction(session,song['id'],instance_id=instance['id'],kind='user_selection')
            with self.store.tx() as db:
                db.execute('UPDATE music_cycles SET user_baseline=user_count,user_target=CASE WHEN user_target=2 THEN 3 ELSE 2 END WHERE session_id=?',(session['id'],))
            self.store.decision(session['id'],'introduction_prepared',id=ident,kind='user_selection',song_id=song['id'])
        if not getattr(self,'_introduction_task',None) or self._introduction_task.done():
            self._introduction_task=self._spawn(self._drain_introductions(session['id']),'cove-introduction-outbox')

    async def _drain_introductions(self,session_id):
        for item in reversed(self.store.introductions(session_id,pending=True)):
            try:await self._deliver_introduction(item)
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.store.finish_introduction(item['id'],'failed',{'reason':getattr(exc,'code',type(exc).__name__)})
                self.store.decision(session_id,'introduction',state='failed',reason=type(exc).__name__)

    async def _deliver_introduction(self,item):
        started=time.monotonic();at=time.time();session=self.store.session(item['session_id'])
        if item['deadline']<at or not session or session['phase']!='active':
            self.store.finish_introduction(item['id'],'cancelled',{'reason':'expired_or_session_ended'});return
        if at<item['not_before']:return
        if item['kind']!='recommendation' and item['state'] in {'pending','ready'}:
            # Queue observations can briefly lose their source at a transition.
            # Never turn that uncertainty into a second introduction or model call.
            with self.store.lock:
                introduced=self.store.conn.execute("SELECT id FROM music_introductions WHERE session_id=? AND song_id=? AND kind='recommendation' AND state IN ('confirmed','sending','unknown') AND updated_at>? LIMIT 1",(session['id'],item['song_id'],at-180)).fetchone()
            if introduced:
                self.store.finish_introduction(item['id'],'cancelled',{'reason':'already_introduced','introduction_id':introduced['id']});return
        if item['state']=='unknown':
            operation=item['receipt'].get('operation_id')
            if operation:
                result=await self.client.operation(operation)
                if result.get('state') in {'confirmed','rejected','cancelled'}:
                    self.store.finish_introduction(item['id'],result['state'],result)
                    if item['recommendation_id']:self.store.set_recommendation(item['recommendation_id'],(self.store.recommendation(item['recommendation_id']) or {}).get('state','queued'),evidence={'intro_state':result['state'],'intro':result})
            with self.store.tx() as db:db.execute('UPDATE music_introductions SET not_before=? WHERE id=?',(at+10,item['id']))
            return
        snapshot=await self.snapshot(True)
        if item['recommendation_id']:
            rec=self.store.recommendation(item['recommendation_id'])
            if not rec or rec['state'] in {'cancelled','rejected','failed'}:
                self.store.finish_introduction(item['id'],'cancelled',{'reason':'recommendation_cancelled'});return
        if not (fresh(snapshot,'room') and fresh(snapshot,'playback') and snapshot.get('room_id')==session['room_id'] and snapshot.get('connected') is True):return
        current=str((snapshot.get('current_song') or {}).get('id') or '')
        queue=[str(v) for v in (snapshot.get('queue') or {}).get('song_ids',[])]
        expected_next=queue[queue.index(current)+1] if current in queue and queue.index(current)+1<len(queue) else None
        valid=current==item['song_id'] or (item['kind']=='recommendation' and expected_next==item['song_id'])
        if not valid or (item['kind']!='recommendation' and not session.get('room_chat_enabled')):
            self.store.finish_introduction(item['id'],'cancelled',{'reason':'song_or_intent_changed'});return
        if snapshot.get('playback',{}).get('status')!='PLAY':return
        budget=self.store.token_summary(self.settings.token_daily_limit)
        if budget['reached'] or not budget['meter_available']:
            self.store.finish_introduction(item['id'],'blocked',{'reason':'token_budget_reached' if budget['reached'] else 'token_meter_unavailable'});return
        messages=self.store.room_messages(10,session_id=session['id'])
        if self._chat_buffer or any(m.get('direction') in {'inbound','in'} and at-m['created_at']<12 for m in messages):return
        if not item['text']:
            detail=await self.client.tracks([item['song_id']]);song=next((s for s in detail if str(s['id'])==item['song_id']),None)
            if not song:
                self.store.finish_introduction(item['id'],'cancelled',{'reason':'catalog_unavailable'});return
            with self.store.lock:
                source=self.store.conn.execute('SELECT origin FROM music_playback_instances WHERE id=? AND session_id=?',(item['instance_id'],session['id'])).fetchone()
            song={**song,'selection_verified':bool(source and source['origin']=='user')}
            if hasattr(self.agent,'introduce_user_song'):text=await self.agent.introduce_user_song(session,song)
            else:text='现在播放《'+str(song.get('name') or '')+'》，这一首也想陪你一起听。'
            if not text.strip():
                self.store.finish_introduction(item['id'],'failed',{'reason':'empty_model_introduction'});return
            self.store.finish_introduction(item['id'],'ready',text=text);return
        # Revalidate after generation and immediately before a single durable send claim.
        if int(session.get('decision_revision') or 0)!=int(item['decision_revision']) and item['kind']!='recommendation':
            self.store.finish_introduction(item['id'],'cancelled',{'reason':'stale_decision'});return
        with self.store.tx() as db:
            claimed=db.execute("UPDATE music_introductions SET state='sending',updated_at=? WHERE id=? AND state='ready'",(time.time(),item['id'])).rowcount
        if not claimed:return
        text=item['text']
        if item['kind']=='recommendation':
            from .music_model import introduction
            rec=self.store.recommendation(item['recommendation_id']) or {}
            text=introduction(text,{'id':item['song_id'],'name':rec.get('song_name') or item['song_id']})
        if item['kind']=='recommendation' and current!=item['song_id']:text='下一首，'+text[:76]
        result=await self._send_room(session,text,origin='autodj' if item['kind']=='recommendation' else 'room_reply',request_id=item['id'])
        state=result.get('state','unknown')
        self.store.finish_introduction(item['id'],state,result,text=text)
        if item['recommendation_id']:
            rec=self.store.recommendation(item['recommendation_id']) or {}
            self.store.set_recommendation(item['recommendation_id'],rec.get('state','queued'),evidence={'intro_state':state,'intro':result})
        self.store.decision(session['id'],'introduction',song_id=item['song_id'],state=state,request_id=item['id'],elapsed_ms=round((time.monotonic()-started)*1000,2))

    async def music_invitation_event(self,event_key,kind,evidence):
        if not self.settings.enabled or not self.settings.invitation_enabled or self.store.active_session():return
        text=str(evidence or '')[:500]
        import re
        if not re.search(r'(音乐|听歌|原声|歌手|歌曲|专辑|想听|喜欢听|鸣潮|music|song)',text,re.I):return
        profile=self.store.taste_profile({'owner_umo':self.owner_umo})
        # These are explicit scheduler scores, not claims about emotional closeness.
        signals={'context':1.0,'accumulation':min(1.,len(profile['known'])/3),'relationship':0.0}
        return await self.evaluate_invitation(event_key,kind,text,signals)
