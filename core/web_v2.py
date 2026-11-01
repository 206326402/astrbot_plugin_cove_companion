"""Protected AstrBot page APIs and the primary agent's one music tool."""
from __future__ import annotations
import asyncio
import json
import hashlib
import time
import uuid
from typing import Any

class CompanionV2:
    async def _refresh_qq_identities(self):
        if time.time()-getattr(self,'_qq_identity_checked',0)<60:return
        self._qq_identity_checked=time.time()
        try:
            parts=self.settings.owner_umo.split(':')
            platform=self.context.get_platform_inst(parts[0])
            if not platform or platform.meta().name not in {'aiocqhttp','onebot','onebot_v11'}:return
            client=platform.get_client()
            login=await asyncio.wait_for(client.get_login_info(),timeout=3)
            owner=await asyncio.wait_for(client.get_stranger_info(user_id=int(parts[-1])),timeout=3)
            uid=str(login.get('user_id') or '')
            if uid.isdigit() and str(owner.get('user_id'))==parts[-1]:
                self.store.set('verified_qq_identities',{'owner':parts[-1],'bot':uid,'platform':parts[0],'verified_at':time.time()})
        except Exception:pass

    def _register_v2(self):
        register=getattr(self.context,'register_web_api',None)
        if not callable(register):return
        for route,handler,method in [('scene',self._v2_scene,'GET'),('journal',self._v2_journal,'GET'),('jobs',self._v2_jobs,'GET'),('action',self._v2_action,'POST'),('events',self._v2_events,'GET'),('dry-run',self._v2_dry_run,'GET'),('recommendations',self._v2_recommendations,'GET')]:
            register('/astrbot_plugin_cove_companion/v2/'+route,handler,methods=[method],desc='single-owner record room')

    async def _v2_scene(self,request_obj=None):
        try:
            snap=await self.engine.snapshot(False)
            try:auth=await self.client.auth_status()
            except Exception:auth={'state':'unavailable','requires_qr':False}
            session=self.store.active_session()
            if not session:
                binding=self.store.binding(self.settings.owner_umo,str((snap.get('bot_account') or {}).get('uid') or ''))
                if auth.get('account_id') and str(auth['account_id'])!=binding['account_id']:
                    auth={**auth,'state':'binding_mismatch','message':'音乐账号与当前绑定不一致，请通过换号入口重新绑定。'}
            else:binding=self.store.binding(self.settings.owner_umo)
            pseudo=session or {'id':None,'owner_umo':self.settings.owner_umo,'decision_revision':0}
            await self._refresh_qq_identities()
            identities=self.store.get('verified_qq_identities',{}) or {}
            avatars={k:('https://q1.qlogo.cn/g?b=qq&nk='+str(v)+'&s=640') for k,v in identities.items() if k in {'owner','bot'} and str(v).isdigit()}
            public_session=self._public_session(session)
            if public_session:
                public_session={**public_session,'pause_since':session.get('pause_since'),'absent_since':session.get('absent_since'),'remote_result':session.get('remote_result'),'binding_revision':session.get('binding_revision')}
            state=self.store.music_state(self.store.scope_key())
            signals={k:state.get(k+'_score',0) for k in ('context','accumulation','relationship')}
            with self.store.lock:
                last=self.store.conn.execute('SELECT state,reason,updated_at FROM listening_invitations WHERE scope_key=? ORDER BY created_at DESC LIMIT 1',(self.store.scope_key(),)).fetchone()
            automation={'recommendation':self.engine.recommendation_assessment(session,snap),'invitation':{**self.engine.invitation_assessment(signals),'last_result':dict(last) if last else None},'song_chatter':{'enabled':bool(session and session.get('room_chat_enabled')),'status':'event_driven','reason':'自选歌曲每2至3首回应一次；播放稳定且安静后发送'}}
            return self._ok({'version':'1.4.1','at':time.time(),'session':public_session,'snapshot':snap,'binding':binding,'auth':auth,'avatars':avatars,'shortlist':self.store.shortlist(pseudo),'music_state':state,'invitation':self.engine.pending_invitation(),'pending_recommendation':self.store.pending_recommendation(session['id']) if session else None,'settings':self.settings.public(),'budget':self.store.token_summary(self.settings.token_daily_limit),'legacy_call_counts':self.store.usage_summary(),'automation':automation,'profile':self.store.taste_profile(pseudo),'recommendations':self.store.recommendation_details(pseudo),'health':self.store.health(),'introductions':self.store.introductions(session['id']) if session else [],'decision_trace':self.store.decision_trace(session['id']) if session else [],'events':self.store.music_events(0,pending=True)})
        except Exception as exc:return self._v2_error(exc)

    async def _v2_dry_run(self,request_obj=None):
        query=self._query(request_obj);kind=str(query.get('kind') or 'recommendation')
        if kind not in {'recommendation','invitation','search','introduction'}:return self._v2_error(ValueError('invalid_preview_kind'))
        # Signals are a preview scenario, never saved as observed facts.
        try:
            if kind=='search':return self._ok({'state':'preview','side_effects':False,'model_called':False,**await self.engine.search_catalog(str(query.get('query') or ''),str(query.get('type') or 'auto'),allow_model=False,dry_run=True)})
            if kind=='introduction':
                session=self.store.active_session()
                snapshot=await self.client.snapshot(True)
                return self._ok({'state':'preview','side_effects':False,'model_called':False,**self.engine.introduction_assessment(session,snapshot)})
            signals={k:float(query.get(k,0)) for k in ('context','accumulation','relationship')}
            return self._ok(await self.engine.recommendation_dry_run(kind,signals))
        except Exception as exc:return self._v2_error(exc)

    async def _v2_recommendations(self,request_obj=None):
        return self._ok({'items':self.store.recommendation_details(self.store.active_session() or {'owner_umo':self.settings.owner_umo})})

    async def _v2_journal(self,request_obj=None):
        items=[{k:v for k,v in item.items() if k!='persona_id'} for item in self.store.journal()]
        ids=list(dict.fromkeys(str(h['song_id']) for item in items for h in item['tracks'] if h['state']=='played'))[:50]
        try:details={str(s['id']):s for s in await self.client.tracks(ids)} if ids else {}
        except Exception:details={}
        for item in items:
            item['tracks']=[{**h,'cover_url':(details.get(str(h['song_id']),{}).get('cover_url') or details.get(str(h['song_id']),{}).get('coverUrl') or '')} for h in item['tracks'] if h['state']=='played']
        return self._ok({'sessions':items})

    async def _v2_jobs(self,request_obj=None):
        key=str(self._query(request_obj).get('id') or '')
        job=self.store.music_job(key)
        if not job or job['scope_key']!=self.store.scope_key():return self._v2_error(ValueError('job_not_found'))
        return self._ok({k:job[k] for k in ('id','state','result','created_at','updated_at')})

    async def _v2_events(self,request_obj=None):
        return self._ok({'events':self.store.music_events(int(self._query(request_obj).get('after') or 0))})

    def _v2_error(self,exc):
        from ..main import error_response
        return error_response(str(exc)[:240],status_code=409 if 'version' in str(exc) or 'conflict' in str(exc) else 400)

    async def _v2_action(self,request_obj=None):
        try:
            body=await self._body(request_obj)
            key=str(body.get('idempotency_key') or '')
            if not 8<=len(key)<=160:raise ValueError('idempotency_key_required')
            action=str(body.get('action') or '')
            payload=body.get('payload') or {}
            if not isinstance(payload,dict):raise ValueError('payload_object_required')
            if any(k in payload for k in ('cookie','binding','owner_umo','sender','account_id')):raise ValueError('client_identity_or_credentials_forbidden')
            expected=int(body.get('expected_version',0))
            job,created=self.store.request_job(key,self.store.scope_key(),{'action':action,'payload':payload},expected)
            if created:
                session=self.store.active_session()
                if session and int(session['session_version'])!=expected:
                    self.store.finish_music_job(key,{'state':'rejected','reason':'stale_version'})
                else:self.engine._spawn(self._perform_v2_job(key,action,payload,expected),'music-page-'+key[:24])
            return self._ok({'task_id':key,'local_state':self.store.music_job(key)['state'],'remote_result':'unknown','message':'请求已记录，通过任务状态读取结果。'})
        except Exception as exc:return self._v2_error(exc)

    async def _perform_v2_job(self,key,action,payload,expected):
        with self.store.tx() as db:db.execute("UPDATE music_requests SET state='running',updated_at=? WHERE id=?",(time.time(),key))
        try:
            result=await self._music_action(action,payload,source='dashboard',evidence='主人在受保护管理页明确操作',request_key=key,expected=expected)
        except asyncio.CancelledError:
            self.store.finish_music_job(key,{'state':'abandoned','remote_result':'unknown','reason':'plugin_stopped'});raise
        except Exception as exc:result={'state':'failed','remote_result':'unknown','reason':str(exc)[:240]}
        result=self._music_receipt(key,result)
        self.store.finish_music_job(key,result)

    @staticmethod
    def _music_receipt(key,result):
        result=dict(result or {'state':'unknown'})
        result.setdefault('task_id',key)
        result.setdefault('local_state',result.get('state','unknown'))
        result.setdefault('remote_result',result['state'] if result.get('state') in {'confirmed','rejected'} else 'unknown')
        result.setdefault('reason','music_'+str(result.get('state','unknown')))
        result.setdefault('message',str(result['reason']))
        return result

    async def _music_action(self,action,payload,*,source,evidence,request_key,expected=None):
        session=self.store.active_session()
        if expected is not None and int((session or {}).get('session_version') or 0)!=expected:
            return {'state':'rejected','reason':'stale_version'}
        if action=='status':return {'state':'confirmed','session':self._public_session(session),'snapshot':await self.engine.snapshot(True)}
        if not self.settings.enabled and action not in {'leave','auth_status','qr_start','qr_check','account_activate'}:return {'state':'rejected','reason':'disabled'}
        if action=='search':
            pseudo=session or {'id':None,'owner_umo':self.settings.owner_umo,'decision_revision':0}
            batch=await self.engine.search_result(pseudo,str(payload.get('query') or ''),str(payload.get('kind') or 'auto'),payload.get('batch_id'),payload.get('offset',0))
            return {'state':'confirmed','batch':batch}
        if action=='candidates':
            pseudo=session or {'id':None,'owner_umo':self.settings.owner_umo,'decision_revision':0}
            from .recommendations import ShortlistUnavailable
            try:batch=await self.engine._ensure_shortlist(pseudo)
            except ShortlistUnavailable as exc:return {'state':'skipped','reason':str(exc)}
            if not batch:return {'state':'skipped','reason':'no_eligible_candidates'}
            self.engine._spawn(self.engine.music_invitation_event(request_key,'candidates_formed','音乐候选已形成：'+','.join(s.get('name','') for s in batch['songs'])),'music-candidates-event')
            return {'state':'confirmed','batch':batch}
        if action in {'select','play_now'}:
            if not session:return {'state':'rejected','reason':'no_session'}
            allow_repeat=bool(payload.get('allow_repeat')) and (source=='dashboard' or any(x in evidence for x in ('再听','循环','重复','再放')))
            return await self.engine.select_shortlist(session,str(payload.get('batch_id') or ''),str(payload.get('song_id') or ''),manual=True,allow_repeat=allow_repeat,immediate=action=='play_now',origin=source if source in {'dashboard','room_command'} else 'qq_command')
        if action=='cancel_recommendation':return await self.engine.cancel_recommendation_result()
        if action=='recommend':
            if not session:return {'state':'rejected','reason':'no_session'}
            return await self.engine._run_recommendation(session,manual=True)
        if action=='leave':return await self.engine.leave_result(reason='owner_requested')
        if action in {'pause','resume','next'}:
            if not session:return {'state':'rejected','reason':'no_session'}
            return await self.engine.execute_verified({'pause':'PAUSE','resume':'RESUME','next':'NEXT'}[action],{},session=session,origin_kind=source if source in {'dashboard','room_command'} else 'qq_command',timeout=45)
        if action=='autodj':
            return {'state':'confirmed','message':await self.engine.set_autodj(bool(payload.get('enabled')))}
        if action=='room_chat':
            if not session:return {'state':'rejected','reason':'no_session'}
            self.store.update_session(session['id'],room_chat_enabled=int(bool(payload.get('enabled'))))
            return {'state':'confirmed','reason':'room_chat_updated'}
        if action=='taste':
            if source!='dashboard':return {'state':'rejected','reason':'use_protected_page'}
            pseudo=session or {'owner_umo':self.settings.owner_umo}
            self.store.correct_taste(pseudo,payload.get('term'),payload.get('kind'),'用户在口味面板明确修正')
            self.engine._spawn(self.engine.music_invitation_event(request_key,'preference_updated','音乐偏好更新：'+str(payload.get('term'))),'music-preference-event')
            if session:self.store.bump_decision_revision(session['id'])
            return {'state':'confirmed','reason':'taste_updated'}
        if action=='journal_rating':
            if source!='dashboard':return {'state':'rejected','reason':'use_protected_page'}
            previous=self.store.session(str(payload.get('session_id') or ''));sid=str(payload.get('song_id') or '')
            if not previous or self.store.scope_key(previous)!=self.store.scope_key():raise ValueError('journal_scope_mismatch')
            batch=self.store.room_batch(previous['id'])
            if batch and batch.get('retracted'):raise ValueError('journal_retracted')
            song=next((h for h in self.store.history(previous['id'],100) if str(h['song_id'])==sid and h['state']=='played'),None)
            if not song:raise ValueError('song_not_in_journal')
            self.store.remember_songs(previous,[{'id':sid,'artist':song['artist'],'name':song['song_name']}])
            self.store.feedback(previous,sid,str(payload.get('kind')),'longterm','用户在手帐明确评价这首歌','journal:'+previous['id'])
            self.engine._spawn(self.engine.music_invitation_event(request_key,'preference_updated','音乐手帐明确评价：'+song['song_name']),'music-journal-event')
            if session:self.store.bump_decision_revision(session['id'])
            return {'state':'confirmed','reason':'journal_rating_saved'}
        if action=='feedback':
            sid=str(payload.get('song_id') or '')
            known=await self.client.tracks([sid])
            if not known or str(known[0].get('id'))!=sid:raise ValueError('unknown_song')
            pseudo=session or {'id':None,'owner_umo':self.settings.owner_umo}
            self.store.remember_songs(pseudo,known)
            self.store.feedback(pseudo,sid,str(payload.get('kind')),str(payload.get('lifetime','session')),evidence)
            if payload.get('lifetime')=='longterm':self.engine._spawn(self.engine.music_invitation_event(request_key,'preference_updated','音乐偏好更新：'+known[0].get('name','')),'music-feedback-event')
            if session:self.store.bump_decision_revision(session['id'])
            return {'state':'confirmed','reason':'explicit_feedback_saved'}
        if action=='state_update':
            patch={**payload,'evidence':evidence}
            state=self.store.update_music_state(self.store.scope_key(),patch,payload.get('expected_version'))
            if session and any(k in payload for k in ('session_intent','tags','memory_terms','energy','emotion','closeness')):self.store.bump_decision_revision(session['id'])
            return {'state':'confirmed','music_state':state}
        if action=='invite_evaluate':
            return await self.engine.evaluate_invitation(request_key,str(payload.get('event_type') or 'private_conversation'),evidence,payload.get('signals') or {})
        if action=='invite_reply':return await self.engine.respond_invitation(str(payload.get('invitation_id') or ''),bool(payload.get('accept')))
        if action=='auth_status':return {'state':'confirmed','auth':await self.client.auth_status(True)}
        if action in {'qr_start','qr_check'}:
            if source!='dashboard':return {'state':'rejected','reason':'use_protected_account_page'}
            return {'state':'confirmed','auth':await self.client.auth_action('qr/start' if action=='qr_start' else 'qr/check',payload.get('id'))}
        if action=='account_activate':
            if source!='dashboard':return {'state':'rejected','reason':'use_protected_account_page'}
            if session:
                result=await self.engine.leave_result(reason='account_switch')
                if result.get('state')!='confirmed':return {'state':'blocked','reason':'old_room_exit_not_confirmed'}
            auth=await self.client.auth_action('activate',payload.get('id'))
            self.store.switch_binding(self.settings.owner_umo,str(auth.get('account_id') or ''))
            self.engine._lease=None;self.engine._lease_renew_at=0
            return {'state':'confirmed','auth':auth}
        if action in {'journal_edit','journal_retract'}:
            sid=str(payload.get('session_id') or '')
            batch=self.store.edit_room_batch(sid,str(payload.get('summary') or ''),int(payload.get('version') or 0),action=='journal_retract')
            try:await self._sync_room_batch(batch,self.store.session(sid))
            except Exception:
                self.store.enqueue_job('room_summary','sync:'+batch['id']+':'+str(batch['version']),time.time()+10,sid,{})
                raise
            return {'state':'confirmed','batch':batch}
        if action=='events_ack':
            self.store.acknowledge_music_events([int(v) for v in payload.get('seqs',[])][:50]);return {'state':'confirmed'}
        raise ValueError('unknown_music_action')

    async def _sync_room_batch(self,batch,session):
        meta=self.context.get_registered_star('astrbot_plugin_memory_constellation')
        plugin=(getattr(meta,'star_cls',None) or getattr(meta,'star',None)) if meta else None
        handler=getattr(plugin,'sync_cove_room_batch',None)
        if callable(handler):
            await handler(batch,session)
            self.store.set('room_memory_sync',{'state':'ok','at':time.time(),'batch':batch['id']})
        else:self.store.set('room_memory_sync',{'state':'not_installed','at':time.time()})

    async def primary_music_tool(self,event,action,payload):
        from ..main import event_text,event_message_id,event_origin
        room=bool(getattr(event,'get_extra',lambda *_:False)('cove_room_agent',False))
        if room:
            turn=self._room_tool_turn(event)
            if action not in {'status','search','select','play_now','cancel_recommendation','pause','resume','next','leave','autodj','feedback','state_update','candidates'}:return json.dumps({'state':'rejected','reason':'action_not_available_in_room'},ensure_ascii=False)
            evidence='房间主人明确消息：'+str(turn.get('user_text') or event_text(event))[:500]
        elif not self.can_manage(event):return json.dumps({'state':'rejected','reason':'untrusted_private_identity'})
        else:evidence=event_text(event)[:500]
        if not room:
            getattr(event,'set_extra',lambda *_:None)('cove_shared_music_used',True)
            meter=event.get_extra('cove_shared_meter',None)
            if meter:meter.flush()
            else:
                from .config import day_key
                self.store.set('token_meter_unavailable',day_key())
        try:
            args=json.loads(payload) if isinstance(payload,str) else dict(payload or {})
            if not isinstance(args,dict):raise ValueError()
        except (ValueError,TypeError):return json.dumps({'state':'rejected','reason':'payload_object_required'})
        if any(k in args for k in ('cookie','binding','owner_umo','sender','account_id')):return json.dumps({'state':'rejected','reason':'client_identity_or_credentials_forbidden'})
        if action=='invite_reply':
            invite=self.engine.pending_invitation()
            reply=event_text(event).strip()
            agreeing=invite and reply=='一起听 '+invite['id'][:8]
            declining=invite and reply in {'不听 '+invite['id'][:8],'拒绝 '+invite['id'][:8]}
            if not invite or args.get('invitation_id')!=invite['id'] or not isinstance(args.get('accept'),bool) or (args['accept'] and not agreeing) or (not args['accept'] and not declining):return json.dumps({'state':'rejected','reason':'explicit_current_invitation_reply_required'})
        if action=='feedback' and not any(k in evidence for k in ('喜欢','少推荐','不喜欢','别推荐','收藏')):return json.dumps({'state':'rejected','reason':'explicit_feedback_required'})
        key='tool:'+event_origin(event)+':'+str(event_message_id(event) or id(event))+':'+action
        key+=':'+hashlib.sha256(json.dumps(args,sort_keys=True).encode()).hexdigest()[:16]
        try:
            row,created=self.store.request_job(key,self.store.scope_key(),{'action':action,'payload':args},0)
            if not created:
                result=self.store.music_job(key)['result'] or {'state':row['state'],'task_id':key}
            else:
                result=await self._music_action(action,args,source='room_command' if room else 'qq_command',evidence=evidence,request_key=key)
                self.store.finish_music_job(key,result)
        except Exception as exc:
            result={'state':'rejected','reason':str(exc)[:200]}
            if self.store.music_job(key):self.store.finish_music_job(key,result)
        result=self._music_receipt(key,result)
        if room:self._record_tool(event,'cove_music',{'action':action,**args},result)
        return json.dumps(result,ensure_ascii=False,default=str)

