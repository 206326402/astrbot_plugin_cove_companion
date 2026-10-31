"""Bounded reconciliation and single-owner room lifecycle.
Only authoritative fresh evidence advances the pause timer.
"""
from __future__ import annotations
import asyncio
import time
from .text import clean


def fresh(snapshot, key, at=None):
    evidence=(snapshot.get('evidence') or {}).get(key) or {}
    if evidence.get('stale') is not False:
        return False
    timestamp=evidence.get('validated_at') or evidence.get('observed_at') or 0
    try:
        timestamp=float(timestamp)
        if timestamp>10_000_000_000:timestamp/=1000
        return 0 <= (time.time() if at is None else at)-timestamp <= 35
    except (ValueError,TypeError):return False


class Lifecycle:
    async def _finish_session(self,session,reason,remote_result):
        current=self.store.session(session['id'])
        if not current or current.get('phase')=='ended':return
        with self.store.tx():
            self.store.update_session(session['id'],phase='ended',ended_at=time.time(),end_reason=reason,remote_result=remote_result,autodj_enabled=0,control_generation=int(current.get('control_generation') or 1)+1,pause_since=None,absent_since=None)
            self._chat_buffer=[]
            self.store.emit_music_event('ended:'+session['id'],'music.session.ended',{'session_id':session['id'],'reason':reason,'duration_seconds':max(0,time.time()-session['started_at']),'remote_result':remote_result,'binding_revision':session.get('binding_revision')})
            summary_job=self.store.enqueue_job('room_summary','summary:'+session['id'],time.time()+15,session['id'],{})
        try:
            await self._commit_session_summary(session)
            if summary_job:self.store.finish_job(summary_job,{'state':'confirmed'})
        except Exception as exc:
            self._log('warning','room summary deferred: %s',type(exc).__name__)
            self.store.enqueue_job('room_summary','summary:'+session['id'],time.time()+15,session['id'],{})

    def _session_started(self,session):
        self.store.emit_music_event('started:'+session['id'],'music.session.started',{'session_id':session['id'],'reason':'room_joined','duration_seconds':0,'remote_result':'confirmed','binding_revision':session.get('binding_revision')})

    async def _lifecycle_tick(self):
        await self._reconcile_operations()
        session=self.store.active_session()
        if not session:return
        at=time.time()
        if session['phase']=='leaving':
            if at-float(session.get('leaving_since') or at)>=120:
                await self._finish_session(session,session.get('end_reason') or 'exit_timeout','unknown')
            return
        if at-getattr(self,'_lifecycle_checked_at',0)<5:return
        self._lifecycle_checked_at=at
        try:snapshot=await self.snapshot(True)
        except Exception:return
        # Snapshot validation happens during the awaited read; assess it after completion.
        at=time.time()
        connected=snapshot.get('connected') is True and snapshot.get('room_id')==session.get('room_id') and fresh(snapshot,'room',at)
        room_known=fresh(snapshot,'room',at)
        if connected and not session.get('owner_binding_id'):
            account=(snapshot.get('bot_account') or {}).get('uid')
            if account:
                self.store.bind_session(session['id'],self.owner_umo,str(account))
                session=self.store.session(session['id'])
        if session['phase']=='joining':
            if connected:
                with self.store.tx():
                    self.store.update_session(session['id'],phase='active',room_epoch=snapshot.get('room_epoch'))
                    self.store.set_recommendation_due(session['id'],at+self.settings.first_recommendation_delay_seconds)
                    self._session_started(self.store.session(session['id']))
            return
        if session['phase']!='active':return
        if room_known and not snapshot.get('room_id'):
            await self._finish_session(session,'room_ended','confirmed_not_in_room');return
        changes={}
        previous=float(session.get('last_evidence_at') or 0)
        # Scheduler downtime cannot count as continuous evidence.
        gap=previous>0 and at-previous>35
        changes['disconnected_since']=None
        pause_ok=connected and fresh(snapshot,'playback',at) and (snapshot.get('playback') or {}).get('status')=='PAUSE'
        changes['pause_since']=(session.get('pause_since') if not gap else None) or at if pause_ok else None
        changes['absent_since']=None
        changes['last_evidence_at']=at
        self.store.update_session(session['id'],**changes)
        reason=None
        if changes['pause_since'] and at-float(changes['pause_since'])>=self.settings.pause_end_seconds:reason='pause_timeout'
        if reason:
            # One last fresh read closes the PAUSE -> PLAY race before fencing LEAVE.
            try:check=await self.snapshot(True)
            except Exception:return
            if not (fresh(check,'playback') and fresh(check,'room') and check.get('room_id')==session['room_id'] and (check.get('playback') or {}).get('status')=='PAUSE'):
                self.store.update_session(session['id'],pause_since=None);return
            await self.leave_result(session_id=session['id'],reason=reason)

    async def _reconcile_operations(self):
        for op in self.store.reconciliation_due():
            now=time.time()
            session=self.store.session(op.get('session_id')) if op.get('session_id') else None
            obsolete=not session or session['phase']=='ended' or int(session.get('control_generation') or 1)!=op['generation'] or int(session.get('binding_revision') or 1)!=op['binding_revision']
            terminal=now>=op['deadline'] or op['checks']>=3 or obsolete
            result=None
            if not terminal:
                try:
                    result=await asyncio.wait_for(self.client.operation(op['operation_id']),timeout=min(12,max(.1,op['deadline']-time.time())))
                    if result.get('state') in {'confirmed','rejected','cancelled'}:
                        # No late receipt may activate or mutate an ended session.
                        self.store.reconcile_attempt(op['operation_id'],result['state'],result)
                        if op['action']=='CREATE' and result['state']=='confirmed' and session['phase']=='joining':
                            await self._complete_room_invite(session,result)
                        if op['action']=='LEAVE' and result['state']=='confirmed':
                            snap=await self.snapshot(True)
                            if fresh(snap,'room') and not snap.get('room_id'):
                                await self._finish_session(session,session.get('end_reason') or 'leave','confirmed')
                        if op['action']=='JOIN' and result['state']=='rejected' and session['phase']=='joining':
                            snap=await self.snapshot(True)
                            if fresh(snap,'room') and not snap.get('room_id'):await self._finish_session(session,'join_failed','confirmed_not_in_room')
                        continue
                    snap=await self.snapshot(True)
                    if op['action']=='LEAVE' and fresh(snap,'room') and not snap.get('room_id'):
                        self.store.reconcile_attempt(op['operation_id'],'confirmed',{'state':'confirmed','evidence':'fresh_not_in_room'})
                        await self._finish_session(session,session.get('end_reason') or 'leave','confirmed');continue
                except Exception:pass
                self.store.reconcile_attempt(op['operation_id'])
                terminal=op['checks']+1>=3 or time.time()>=op['deadline']
            if terminal:
                self.store.reconcile_attempt(op['operation_id'],'abandoned',{'state':'abandoned','remote_result':'unknown','reason':'reconcile_limit' if not obsolete else 'session_fenced'})
                if session and op['action']=='LEAVE' and session['phase']!='ended':
                    await self._finish_session(session,session.get('end_reason') or 'exit_timeout','unknown')
                if self.store.notify_operation_once(op['operation_id']):
                    self.store.emit_music_event('op-ended:'+op['operation_id'],'music.operation.ended',{'operation_id':op['operation_id'],'state':'abandoned','remote_result':'unknown'})
                    try:await self.qq_send(self.owner_umo,'一起听操作核对已到上限，远端结果仍未知；本地已停止该任务。下一次操作会先核验房间。')
                    except Exception:pass

    async def _commit_session_summary(self,session):
        existing=self.store.room_batch(session['id'])
        if existing:
            if self.room_summary:await self.room_summary(existing['summary'],{**session,'summary_batch':existing})
            return existing
        played=[h for h in self.store.history(session['id'],100) if h['state']=='played']
        unique={str(h['song_id']):h for h in reversed(played)}
        names=[str(h.get('song_name') or h['song_id']) for h in unique.values()]
        feedback=self.store.feedback_items(session)
        explicit=[{'song_id':f['song_id'],'kind':f['kind'],'evidence':f['evidence']} for f in feedback if f['session_id']==session['id']]
        text=('这次一起听了'+str(len(names))+'首：'+'、'.join(names[:12])+'。')[:300]
        batch=self.store.commit_room_batch(session,text,{'explicit_feedback':explicit[:1]},['一起听']+(['明确音乐反馈'] if explicit else []),{'source':'room','session_id':session['id'],'account':session.get('bot_music_account_id'),'song_ids':list(unique)[:100],'feedback_ids':[f['id'] for f in feedback if f['session_id']==session['id']]})
        if self.room_summary:await self.room_summary(batch['summary'],{**session,'summary_batch':batch})
        return batch
