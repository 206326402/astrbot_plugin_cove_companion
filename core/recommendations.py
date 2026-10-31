from __future__ import annotations
import random
import math
import time
import asyncio
from .text import clean


def terms(song):
    from .catalog_search import catalogue_text
    return catalogue_text(song)+' '+' '.join(str(x) for x in song.get('tags') or []).casefold()


def category(song):
    return str(song.get('category') or (song.get('tags') or [''])[0] or '').casefold()


def rank_songs(songs, *, preferences, intent, state, memory_terms, history, feedback, explore=False, same_limit=3, profile=None, persona_terms=()):
    """Missing features contribute no invented score. Estimates retain provenance."""
    ranked=[]
    recent_categories=[str((h.get('evidence') or {}).get('category') or '') for h in history if h.get('state')=='played'][:same_limit]
    repeated=recent_categories[0] if len(recent_categories)==same_limit and recent_categories[0] and len(set(recent_categories))==1 else None
    latest={}
    for f in feedback:latest.setdefault(str(f['song_id']),f['kind'])
    for raw in songs:
        song=dict(raw); text=terms(song); cat=category(song)
        if repeated and cat==repeated:continue
        if latest.get(str(song['id']))=='less':continue
        profile=profile or {}; known=profile.get('known',[]); inferred=profile.get('inferred',[])
        negative=[p['term'] for p in known if p['kind']=='less' and not str(p.get('source_key','')).startswith('feedback:')]
        if any(str(t).casefold() in text for t in negative):continue
        positive=list(preferences)+[p['term'] for p in known if p['kind']=='like']
        matched=[str(t) for t in positive if str(t).casefold() in text]
        weak=[p['term'] for p in inferred if str(p['term']).casefold() in text]
        taste=min(1.,len(matched)*.5+len(weak)*.08)
        if latest.get(str(song['id']))=='like':taste=1.
        from .catalog_search import describe_query,normalize
        intent_matches=[str(x) for x in intent if normalize(x) in text or (describe_query(str(x))['kind']=='work' and any(normalize(a) in text for a in describe_query(str(x))['aliases']))]
        state_matches=[]
        if state.get('tags'):state_matches.append(sum(str(x).casefold() in text for x in state['tags'])/len(state['tags']))
        audio=song.get('audio_features') or {}
        if audio.get('source') in {'measured','platform','tag_estimate'}:
            for key,attribute in [('energy','energy'),('emotion','valence')]:
                desired,actual=state.get(key),audio.get(attribute)
                if isinstance(desired,(int,float)) and isinstance(actual,(int,float)) and math.isfinite(actual) and 0<=actual<=1:
                    state_matches.append(1-abs(float(desired)-actual))
        demand=max([len(intent_matches)/max(1,len(intent))]+state_matches)
        recent_artists={h.get('artist') for h in history[:10]}
        diversity=float(song.get('artist') not in recent_artists)
        persona=[str(t) for t in persona_terms if str(t).casefold() in text]
        score=.70*taste+.15*demand+.10*diversity+.05*min(1.,len(persona)*.5)
        song['rank_score']=round(score,4)
        song['explanation']={'weights':{'taste':.7,'intent':.15,'diversity':.1,'persona':.05},'components':{'taste':taste,'intent':demand,'diversity':diversity,'persona':min(1.,len(persona)*.5)},'matched_preferences':matched,'weak_observations':weak,'intent_matches':intent_matches,'persona_matches':persona,'source':song.get('candidate_source') or song.get('source') or 'platform_candidate','limits':'未匹配的风格不加分；查询分类不是已验证曲风'}
        song['category']=cat
        song['audio_features']=song.get('audio_features') or {'energy':None,'valence':None,'source':'unknown'}
        ranked.append(song)
    ranked.sort(key=lambda s:(-s['rank_score'],str(s['id'])))
    shortlist=ranked[:12]
    heard={str(h.get('song_id')) for h in history}
    unknown=[s for s in ranked[1:] if str(s['id']) not in heard and latest.get(str(s['id']))!='like' and any(s['explanation'][k] for k in ('matched_preferences','weak_observations','intent_matches','persona_matches'))]
    if explore and unknown:
        alternate=dict(random.choice(unknown))
        alternate['exploration']=True
        alternate['explanation']={**alternate['explanation'],'exploration_basis':'本账号近期播放记录未见此曲；仍通过可用性、排除及去重筛选，不代表从未听过'}
        shortlist=[s for s in shortlist if s['id']!=alternate['id']][:11]+[alternate]
    return shortlist


class ShortlistUnavailable(RuntimeError):
    """A truthful terminal reason when no reusable shortlist can be produced."""


class Recommendations:
    @staticmethod
    def _recommendation_room_blocker(session,snapshot):
        """The active listening room is authoritative; member lists may be unavailable."""
        from .lifecycle import fresh
        if not fresh(snapshot,'room') or not fresh(snapshot,'playback'):
            return 'playback_evidence_stale'
        if not snapshot.get('room_id') or snapshot.get('connected') is not True:
            return 'room_not_ready'
        if session and (snapshot.get('room_id')!=session.get('room_id') or (session.get('room_epoch') and snapshot.get('room_epoch')!=session.get('room_epoch'))):
            return 'room_not_ready'
        return None

    def recommendation_assessment(self,session,snapshot):
        """Read-only gates shared by the player preview and the scheduler."""
        budget=self.store.token_summary(self.settings.token_daily_limit);reasons=[]
        if not self.settings.enabled:reasons.append('disabled')
        if not session or session.get('phase')!='active':reasons.append('no_active_session')
        if session:
            if not session.get('autodj_enabled'):reasons.append('autodj_disabled')
            if self.store.pending_recommendation(session['id']):reasons.append('pending_recommendation')
            if self.store.has_unresolved_music_operations(session['id']):reasons.append('unresolved_music_operation')
        room_blocker=self._recommendation_room_blocker(session,snapshot)
        if room_blocker:reasons.append(room_blocker)
        if (snapshot.get('playback') or {}).get('status')!='PLAY':reasons.append('playback_not_playing')
        if (snapshot.get('queue') or {}).get('play_mode')!='ORDER_LOOP':reasons.append('unsupported_play_mode')
        if budget['reached']:reasons.append('token_budget_reached')
        if not budget['meter_available']:reasons.append('token_meter_unavailable')
        cycle=self.cycle_assessment(session,snapshot)
        if session and not cycle['eligible']:reasons.append(cycle['reason'])
        if session and time.time()<float(session.get('blocked_until') or 0):reasons.append(session.get('blocked_reason') or 'retry_backoff')
        return {'eligible':not reasons,'blockers':list(dict.fromkeys(reasons)),'next_at':cycle.get('until'),'cycle':cycle,'budget':budget}

    async def recommendation_dry_run(self,kind='recommendation',signals=None):
        session=self.store.active_session()
        # Read Cove directly: engine.snapshot also reconciles/persists state.
        try:snapshot=await self.client.snapshot(True)
        except Exception:snapshot={}
        pseudo=session or {'id':None,'owner_umo':self.owner_umo,'decision_revision':0}
        assessment=self.invitation_assessment(signals or {}) if kind=='invitation' else self.recommendation_assessment(session,snapshot)
        candidate_status='ready'
        try:candidates=await asyncio.wait_for(self._collect_ranked_candidates(pseudo,dry_run=True),timeout=12)
        except asyncio.TimeoutError:candidates=[];candidate_status='candidate_read_timeout'
        return {'state':'preview','kind':kind,'side_effects':False,'model_called':False,**assessment,'candidates':candidates[:6],'candidate_status':candidate_status,'exploration_rate':self.settings.exploration_rate,'exploration_note':'真实推荐按此概率开放一首探索位；预览只展示确定性排序，不抽签、不生成介绍','profile':self.store.taste_profile(pseudo)}
    def _selection_state(self,session):
        state=dict(self.store.music_state(self.store.scope_key(session)))
        evidence=state.get('parameter_evidence') or {}
        for key in ('session_intent','energy','emotion','closeness','tags','weather','context_score','accumulation_score','relationship_score'):
            updated=(evidence.get(key) or {}).get('updated_at') or state.get('updated_at') or 0
            if time.time()-float(updated)>86400:state.pop(key,None)
        return state

    async def _collect_ranked_candidates(self,session,dry_run=False):
        started=time.monotonic();state=self._selection_state(session)
        found=[]
        try:found.extend([{**s,'candidate_source':'platform_candidates'} for s in await self.client.candidates(24)])
        except Exception:pass
        if hasattr(self.client,'playlist'):
            for pid in self.settings.reference_playlist_ids[:3]:
                try:found.extend([{**s,'candidate_source':'reference_playlist','playlist_id':pid} for s in await self.client.playlist(pid,8)])
                except Exception:pass
        intent=[str(state.get('session_intent') or '')][:1] if state.get('session_intent') else []
        profile=self.store.taste_profile(session)
        queries=list(dict.fromkeys(intent+list(self.settings.music_preferences[:2])+[p['term'] for p in profile['known'] if p['kind']=='like'][:2]+[p['term'] for p in profile['inferred']][:1]+list(state.get('tags') or [])[:1]))[:3]
        for query in queries:
            try:
                result=await self.search_catalog(query,allow_model=False,dry_run=dry_run)
                found.extend([{**v,'candidate_source':'keyword_search','query':str(query)} for v in result['songs'][:30]])
            except Exception:pass
        blocked=self.store.recent_song_ids(self.settings.dedup_minutes)
        try:
            snap=await self.client.snapshot(False)
            blocked.update(str(v) for v in (snap.get('queue') or {}).get('song_ids',[]))
            blocked.add(str((snap.get('current_song') or {}).get('id') or ''))
        except Exception:pass
        from .catalog_search import describe_query,normalize
        work=describe_query(intent[0]) if intent else None
        if work and work['kind']=='work':
            found=[v for v in found if v.get('work_evidence') or any(normalize(a) in terms(v) for a in work['aliases'])]
        seen=set(); eligible=[]
        for song in found:
            sid=str(song.get('id') or '')
            if not sid.isdigit() or sid in seen or sid in blocked or song.get('available') is False:continue
            if any(str(x).casefold() in terms(song) for x in self.settings.excluded_terms):continue
            seen.add(sid);eligible.append({**song,'id':sid})
            if len(eligible)>=60:break
        if not dry_run:self.store.decision(session.get('id'),'filter',retrieved=len(found),eligible=len(eligible),constraint=work if work and work['kind']=='work' else None,excluded_recent_or_queued=len(blocked),elapsed_ms=round((time.monotonic()-started)*1000,2))
        if not dry_run:self.store.remember_songs(session,eligible)
        profile=self.store.taste_profile(session)
        history=self.store.scoped_history(session,100)
        persona_terms=list(self.settings.persona_music_terms)
        if hasattr(self.agent,'music_style_terms'):
            persona_terms=list(dict.fromkeys(persona_terms+await self.agent.music_style_terms()))[:10]
        return rank_songs(eligible,preferences=self.settings.music_preferences,intent=intent,state=state,memory_terms=state.get('memory_terms') or [],history=history,feedback=self.store.feedback_items(session),explore=False if dry_run else self.settings.exploration_rate>0,same_limit=self.settings.same_category_limit,profile=profile,persona_terms=persona_terms)

    async def _hydrate_shortlist(self,songs):
        # Search payloads often omit album art; fetch at most three real details.
        songs=[dict(song) for song in songs[:3]]
        missing=[str(song['id']) for song in songs if not (song.get('cover_url') or song.get('coverUrl'))]
        details={}
        if missing:
            try:details={str(song['id']):song for song in await self.client.tracks(missing)}
            except Exception:pass
        for song in songs:
            detail=details.get(str(song['id']),{})
            art=detail.get('cover_url') or detail.get('coverUrl') or song.get('cover_url') or song.get('coverUrl')
            if art:song['cover_url']=art
            if detail.get('durationMs') or detail.get('duration_ms'):song['duration_ms']=detail.get('duration_ms') or detail.get('durationMs')
        return songs

    async def _ensure_shortlist(self,session):
        started=time.monotonic()
        cached=self.store.shortlist(session)
        if cached and cached['songs']:
            from .music_model import introduction
            for song in cached['songs']:song['intro']=introduction(song.get('intro'),song,song.get('reason',''))
            return cached
        binding_revision=self.store.binding(self.owner_umo)["revision"]
        ranked=await self._collect_ranked_candidates(session)
        self.store.decision(session.get('id'),'retrieve',count=len(ranked),sources=list(dict.fromkeys(s.get('candidate_source','unknown') for s in ranked)),elapsed_ms=round((time.monotonic()-started)*1000,2))
        if not ranked:return None
        state=self._selection_state(session)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        state={**state,'local_time':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'time_source':'server_clock/Asia/Shanghai'}
        result=await self.agent.rank_candidates(session=session,candidates=ranked,state=state)
        if result.get('state')=='budget_exhausted':raise ShortlistUnavailable(result.get('reason') or 'model_budget_exhausted')
        if result.get('state')=='failed':raise ShortlistUnavailable('candidate_ranking_failed:'+str(result.get('reason') or 'unknown'))
        valid={str(s['id']):s for s in ranked};selected=[];used=set()
        for item in result.get('songs',[])[:3]:
            sid=str(item.get('id') or '')
            if sid not in valid or sid in used:continue
            if valid[sid].get('exploration') and any(v.get('exploration') for v in selected):continue
            from .music_model import introduction
            intro=introduction(item.get('intro'),valid[sid],clean(item.get('reason'),80))
            used.add(sid);selected.append({**valid[sid],'reason':clean(item.get('reason'),300),'intro':intro,'token_request_id':result.get('token_request_id')})
        if not selected or len(selected)<min(2,len(ranked)):raise ShortlistUnavailable('invalid_candidate_ranking')
        exploration=next((s for s in ranked if s.get('exploration')),None)
        if exploration and not any(s.get('exploration') for s in selected):
            # A model cannot silently defeat the configured exploration draw.
            # This fallback states only catalog facts and the real eligibility rule.
            text=f"这次想一起探索《{exploration.get('name')}》—{exploration.get('artist')}。近期记录里没见这首，先听听再决定喜不喜欢。"
            selected[-1]={**exploration,'reason':exploration['explanation']['exploration_basis'],'intro':text[:80],'token_request_id':result.get('token_request_id')}
        self.store.decision(session.get('id'),'select',songs=[{'id':s['id'],'exploration':s.get('exploration',False),'evidence':s.get('explanation'),'model_request':s.get('token_request_id')} for s in selected],elapsed_ms=round((time.monotonic()-started)*1000,2))
        selected=await self._hydrate_shortlist(selected)
        current=self.store.session(session['id']) if session.get('id') else session
        if not current or current.get('phase')=='ended' or int(current.get('decision_revision') or 0)!=int(session.get('decision_revision') or 0):return None
        if self.store.binding(self.owner_umo)["revision"]!=binding_revision:return None
        return self.store.save_shortlist(current,selected)

    async def _recommend_cached(self,session,manual=False):
        explore=random.random()<self.settings.exploration_rate
        previous=self.store.shortlist(session)
        if explore and previous and not any(v.get('exploration') for v in previous['songs']):
            with self.store.tx() as db:db.execute('DELETE FROM music_shortlists WHERE id=?',(previous['id'],))
        try:cached=await self._ensure_shortlist(session)
        except ShortlistUnavailable as exc:
            reason=str(exc)
            return {'state':'budget_exhausted' if reason=='model_budget_exhausted' else 'skipped','reason':reason,'error':reason}
        if not cached:return {'state':'skipped','reason':'no_eligible_candidates'}
        chosen=next((v for v in cached['songs'] if bool(v.get('exploration'))==explore),None)
        if not chosen:chosen=next((v for v in cached['songs'] if not v.get('exploration')),None)
        if not chosen:return {'state':'skipped','reason':'no_eligible_candidates'}
        return await self.select_shortlist(session,cached['id'],str(chosen['id']),manual=manual)

    async def select_shortlist(self,session,batch_id,song_id,manual=True,allow_repeat=False,immediate=False,origin="qq_command"):
        if not manual and self.store.token_summary(self.settings.token_daily_limit)['reached']:return {'state':'blocked','reason':'token_budget_reached'}
        cached=self.store.search_batch(session,batch_id)
        purpose='search' if cached else 'recommendation'
        if not cached:cached=self.store.shortlist(session)
        if not cached or cached['id']!=batch_id:return {'state':'rejected','reason':'candidate_batch_expired'}
        song=next((s for s in cached['songs'] if str(s['id'])==str(song_id)),None)
        if not song:return {'state':'rejected','reason':'song_outside_batch'}
        if not allow_repeat and song_id in self.store.recent_song_ids(self.settings.dedup_minutes):
            self.store.consume_shortlist(batch_id,song_id)
            return {'state':'skipped','reason':'recently_played'}
        # Re-fetch the real song immediately before any side effect.
        if hasattr(self.client,'availability') and not await self.client.availability(song_id):
            self.store.consume_shortlist(batch_id,song_id)
            return {'state':'skipped','reason':'song_unavailable'}
        details=await self.client.tracks([song_id])
        if not any(str(s.get('id'))==song_id and s.get('available') is not False for s in details):
            self.store.consume_shortlist(batch_id,song_id)
            return {'state':'skipped','reason':'song_unavailable'}
        current=self.store.session(session['id'])
        if not current or current['phase']!='active' or int(current.get('decision_revision') or 0)!=int(cached['decision_revision']):return {'state':'cancelled','reason':'stale_decision'}
        if manual and purpose=='search':
            # A human selection is not an automatic recommendation slot. Insert
            # it ahead of the queued bot song, retaining that song and its audit.
            result=await self.execute_verified('PLAY_NOW' if immediate else 'ENQUEUE_NEXT',{'song_id':str(song_id)},session=current,origin_kind=origin,request_id='selection:'+batch_id+':'+str(song_id)+':'+('play' if immediate else 'next'),timeout=60 if immediate else 30,expected_decision_revision=int(cached['decision_revision']))
            self.store.decision(session['id'],'manual_selection',song_id=song_id,state=result.get('state','unknown'),operation_id=result.get('operation_id'),batch_id=batch_id)
        else:
            result=(await self.play_now_result(song_id,origin=origin) if immediate else await self.recommend_from_agent(song,song.get('intro') or '',manual=manual,expected_decision_revision=int(cached['decision_revision']),selection={**song,'purpose':purpose}))
        if result.get('state')=='confirmed':
            self.store.consume_shortlist(batch_id,song_id)
            rec=self.store.recommendation(result.get('recommendation_id')) if result.get('recommendation_id') else None
            if rec and not immediate:self.store.set_recommendation(rec['id'],rec['state'],evidence={'category':category(song),'exploration':bool(song.get('exploration')),'selection':{**song,'purpose':purpose},'token_request_id':song.get('token_request_id')})
            if manual:self.store.protect_queue(session['id'],song_id,seconds=0)
            # The manual enqueue changes decision revision; retain only this same
            # validated batch at the new revision, never other pending model work.
            with self.store.tx() as db:
                table='music_search_batches' if purpose=='search' else 'music_shortlists'
                db.execute('UPDATE '+table+' SET decision_revision=? WHERE id=?',(int(self.store.session(session['id']).get('decision_revision') or 0),batch_id))
        return result
