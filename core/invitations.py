"""Event driven invitations. Quiet polling never calls a language model."""
from __future__ import annotations
import asyncio
import json
import time
import uuid
import math
from datetime import datetime
from zoneinfo import ZoneInfo

class Invitations:
    def invitation_assessment(self,signals):
        scope=self.store.scope_key();at=time.time();local=datetime.fromtimestamp(at,ZoneInfo('Asia/Shanghai'))
        try:
            values={k:float(signals.get(k,0)) for k in ('context','accumulation','relationship')}
            if not all(math.isfinite(v) for v in values.values()):raise ValueError()
        except (ValueError,TypeError,AttributeError):return {'eligible':False,'blockers':['invalid_invitation_signals'],'score':None}
        score=sum(max(0,min(1,values[k]))*w for k,w in [('context',.5),('accumulation',.3),('relationship',.2)])
        blockers=[]
        if not self.settings.enabled or not self.settings.invitation_enabled:blockers.append('invitation_disabled')
        if self.store.active_session():blockers.append('session_exists')
        with self.store.lock:
            pending=self.store.conn.execute("SELECT 1 FROM listening_invitations WHERE scope_key=? AND state='offered' AND expires_at>?",(scope,at)).fetchone()
            previous=self.store.conn.execute("SELECT created_at FROM listening_invitations WHERE scope_key=? AND state NOT IN ('skipped','failed') ORDER BY created_at DESC LIMIT 1",(scope,)).fetchone()
        if pending:blockers.append('invitation_pending')
        if local.hour>=23 or local.hour<8:blockers.append('quiet_hours')
        if score<self.settings.invitation_threshold:blockers.append('below_threshold')
        if float(self.store.get('invite_backoff_until',0))>at:blockers.append('refusal_backoff')
        if previous and (at-previous['created_at']<43200 or datetime.fromtimestamp(previous['created_at'],ZoneInfo('Asia/Shanghai')).date()==local.date()):blockers.append('invitation_frequency_limit')
        budget=self.store.token_summary(self.settings.token_daily_limit)
        if budget['reached']:blockers.append('token_budget_reached')
        if not budget['meter_available']:blockers.append('token_meter_unavailable')
        return {'eligible':not blockers,'blockers':blockers,'score':round(score,4),'threshold':self.settings.invitation_threshold,'signals':values,'trigger':'有证据的新音乐事件；非定时唤醒模型'}
    def pending_invitation(self):
        scope=self.store.scope_key()
        with self.store.tx() as db:
            db.execute("UPDATE listening_invitations SET state='expired',updated_at=? WHERE state IN ('offered','sharing','asking') AND expires_at<=?",(time.time(),time.time()))
            row=db.execute("SELECT * FROM listening_invitations WHERE scope_key=? AND state='offered' ORDER BY created_at DESC LIMIT 1",(scope,)).fetchone()
        return {**dict(row),'song':json.loads(row['song_json']),'evidence':json.loads(row['evidence_json'])} if row else None

    async def evaluate_invitation(self,event_key,kind,evidence,signals):
        if not hasattr(self,'_invitation_lock'):self._invitation_lock=asyncio.Lock()
        async with self._invitation_lock:
            return await self._evaluate_invitation(event_key,kind,evidence,signals)

    async def _evaluate_invitation(self,event_key,kind,evidence,signals):
        allowed={'private_conversation','preference_updated','memory_organized','candidates_formed'}
        if kind not in allowed or not evidence or not event_key:return {'state':'skipped','reason':'missing_event_evidence'}
        with self.store.lock:
            old=self.store.conn.execute('SELECT id,state FROM listening_invitations WHERE event_key=?',(event_key,)).fetchone()
        if old:return dict(old)
        scope=self.store.scope_key();now=time.time();local=datetime.fromtimestamp(now,ZoneInfo('Asia/Shanghai'))
        assessment=self.invitation_assessment(signals)
        score=assessment.get('score');reason=next(iter(assessment['blockers']),None)
        ident=str(uuid.uuid4());b=self.store.binding(self.owner_umo)
        with self.store.tx() as db:
            db.execute('INSERT INTO listening_invitations VALUES(?,?,?,?,?,?,?,?,?,?,?)',(ident,event_key,scope,b['revision'],'skipped' if reason else 'preparing','{}',str(reason or kind),json.dumps({'kind':kind,'evidence':str(evidence)[:500],'signals':signals,'score':score},ensure_ascii=False),now+1800,now,now))
        if reason:return {'id':ident,'state':'skipped','reason':reason}
        try:
            auth=await self.client.auth_status()
            snap=await self.snapshot(True)
            from .lifecycle import fresh
            if auth.get('state')!='authenticated' or not fresh(snap,'room') or snap.get('room_id'):raise ValueError('account_or_room_not_ready')
            pseudo={'id':None,'owner_umo':self.owner_umo,'decision_revision':0}
            batch=await self._ensure_shortlist(pseudo)
            if not batch or not batch['songs']:raise ValueError('no_eligible_song')
            selected=batch['songs'][0];why=str(selected.get('reason') or '').strip()
            if not why:raise ValueError('no_share_reason')
            self._invitation_update(ident,'sharing',selected)
            introduction=str(selected.get('intro') or f"想分享《{selected.get('name')}》— {selected.get('artist')}：{why}")
            sent=await self.qq_send(self.owner_umo,introduction+f"\nhttps://music.163.com/song?id={selected['id']}")
            if sent is not True:raise ValueError('song_share_delivery_unknown')
            self._invitation_update(ident,'asking',selected)
            sent=await self.qq_send(self.owner_umo,f"这轮想到你，是因为：{str(evidence)[:120]}。想一起听这首吗？\n同意请回复「一起听 {ident[:8]}」，不听请回复「不听 {ident[:8]}」。30 分钟内有效。")
            if sent is not True:raise ValueError('invitation_delivery_unknown')
            self._invitation_update(ident,'offered',selected)
            return {'id':ident,'state':'offered','expires_at':now+1800}
        except Exception as exc:
            self._invitation_update(ident,'failed')
            self.store.audit('invitation','failed',{'id':ident,'reason':str(exc)[:100]})
            return {'id':ident,'state':'skipped','reason':str(exc)[:100]}

    def _invitation_update(self,ident,state,song=None):
        with self.store.tx() as db:
            if song is None:db.execute('UPDATE listening_invitations SET state=?,updated_at=? WHERE id=?',(state,time.time(),ident))
            else:db.execute('UPDATE listening_invitations SET state=?,song_json=?,updated_at=? WHERE id=?',(state,json.dumps(song,ensure_ascii=False),time.time(),ident))

    async def respond_invitation(self,ident,accept):
        if accept and not self.settings.enabled:return {'state':'rejected','reason':'disabled'}
        invite=self.pending_invitation()
        if not invite or invite['id']!=ident:return {'state':'rejected','reason':'invitation_expired_or_changed'}
        with self.store.tx() as db:
            changed=db.execute("UPDATE listening_invitations SET state=?,updated_at=? WHERE id=? AND state='offered'",('accepted' if accept else 'declined',time.time(),ident)).rowcount
        if not changed:return {'state':'rejected','reason':'invitation_already_handled'}
        if not accept:
            n=int(self.store.get('invite_refusals',0))+1
            self.store.set('invite_refusals',n);self.store.set('invite_backoff_until',time.time()+min(7*86400,86400*2**min(n-1,4)))
            return {'state':'confirmed','reason':'invitation_declined'}
        self.store.set('invite_refusals',0)
        if self.store.active_session():return {'state':'rejected','reason':'session_exists'}
        snap=await self.snapshot(True)
        from .lifecycle import fresh
        if snap.get('room_id') or not fresh(snap,'room'):return {'state':'rejected','reason':'remote_room_not_confirmed_empty'}
        account=str((snap.get('bot_account') or {}).get('uid') or '')
        binding=self.store.binding(self.owner_umo,account)
        if binding['revision']!=invite['revision']:return {'state':'rejected','reason':'binding_changed'}
        session=self.store.create_session(owner_umo=self.owner_umo,room_id='',room_epoch=None,persona_id=await self.agent.current_persona_id(),autodj=self.settings.autodj_enabled,room_chat=self.settings.room_chat_enabled,max_minutes=self.settings.session_max_minutes)
        self.store.bind_session(session['id'],self.owner_umo,account)
        self.store.set('invite_session:'+session['id'],{'invitation_id':ident,'song':invite['song'] if 'song' in invite else json.loads(invite['song_json'])})
        result=await self.execute('CREATE',{},session=session,origin_kind='qq_command',timeout=45)
        if result.get('state')!='confirmed':
            if result.get('state') in {'rejected','cancelled'}:await self._finish_session(session,'create_failed','rejected')
            return result
        return await self._complete_room_invite(self.store.session(session['id']),result)

    async def _complete_room_invite(self,session,result):
        current=self.store.session(session['id'])
        if not current or current['phase']!='joining' or current.get('binding_revision')!=self.store.binding(self.owner_umo)['revision']:return {'state':'cancelled','reason':'session_fenced'}
        context=self.store.get('invite_session:'+session['id'],{})
        ident=context.get('invitation_id')
        if not ident:return {'state':'rejected','reason':'missing_invitation_context'}
        remote=result.get('result') or {};url=remote.get('invite_url');room=remote.get('room_id')
        if not url or not room:
            self.store.update_session(session['id'],room_id=(await self.snapshot(True)).get('room_id'))
            return {'state':'unknown','reason':'invite_details_missing','session_id':session['id']}
        self.store.update_session(session['id'],room_id=room,phase='joining')
        self.store.save_shortlist(self.store.session(session['id']),[context['song']])
        with self.store.tx() as db:
            claimed=db.execute("UPDATE listening_invitations SET state='sending_room_invite',updated_at=? WHERE id=? AND state='accepted'",(time.time(),ident)).rowcount
        if not claimed:return {'state':'unknown','reason':'room_invite_already_attempted'}
        sent=await self.qq_send(self.owner_umo,'房间已准备好，点这里一起听：'+url)
        if sent is not True:return {'state':'unknown','reason':'room_invite_delivery_unknown','session_id':session['id']}
        self._invitation_update(ident,'room_invited')
        return {'state':'confirmed','reason':'awaiting_owner','session_id':session['id'],'message':'已发送真实房间邀请，等待你加入。'}
