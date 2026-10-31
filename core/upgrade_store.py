"""Versioned single-owner state. No credentials are stored in the plugin database."""
from __future__ import annotations
import hashlib
import math
import json
import time
import uuid
from typing import Any, Mapping
from .store_v13 import StoreV13


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load(value, default=None):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


class UpgradeStore(StoreV13):
    def reserve_tokens(self, key, scope, kind, incoming, outgoing, limit, automatic=False, shared=False):
        from .config import day_key
        with self.tx() as db:
            if db.execute('SELECT 1 FROM music_token_usage WHERE request_id=?',(key,)).fetchone():return
            if automatic and self.get('token_meter_unavailable')==day_key():raise RuntimeError('token_meter_unavailable')
            if automatic and (incoming is None or outgoing is None):raise RuntimeError('token_meter_unavailable')
            used=db.execute('SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM music_token_usage WHERE day=?',(day_key(),)).fetchone()[0]
            if automatic and used+int(incoming or 0)+int(outgoing or 0)>limit:raise RuntimeError('token_budget_reached')
            db.execute('INSERT INTO music_token_usage VALUES(?,?,?,?,?,?,?,?,?,?)',(key,scope,day_key(),kind,int(shared),int(incoming or 0),int(outgoing or 0),'reserved_estimate','reserved',time.time()))

    def settle_tokens(self,key,response=None,fallback_input=None,failed=False):
        from .metering import usage_of,estimate,field
        actual=usage_of(response) if response is not None else None
        with self.tx() as db:
            row=db.execute('SELECT * FROM music_token_usage WHERE request_id=?',(key,)).fetchone()
            if not row:return
            if actual:incoming,outgoing=actual;source='provider'
            elif failed:incoming,outgoing=row['input_tokens'],row['output_tokens'];source='reserved_unknown'
            else:
                incoming=fallback_input if fallback_input is not None else row['input_tokens']
                outgoing=estimate({'text':field(response,'completion_text',''),'tools':field(response,'tools_call_args',[])}) or 0
                source='utf8_estimate'
            db.execute('UPDATE music_token_usage SET input_tokens=?,output_tokens=?,source=?,status=? WHERE request_id=?',(incoming,outgoing,source,'failed' if failed else 'settled',key))

    def token_summary(self, limit=1_000_000):
        from .config import day_key,now
        from datetime import timedelta
        scope=self.scope_key()
        with self.lock:
            rows=[dict(r) for r in self.conn.execute('SELECT kind,shared,source,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,COUNT(*) requests FROM music_token_usage WHERE day=? GROUP BY kind,shared,source',(day_key(),))]
        incoming=sum(r['input_tokens'] for r in rows);outgoing=sum(r['output_tokens'] for r in rows)
        return {'day':day_key(),'timezone':'Asia/Shanghai','input_tokens':incoming,'output_tokens':outgoing,'total_tokens':incoming+outgoing,'limit':limit,'remaining':max(0,limit-incoming-outgoing),'reached':incoming+outgoing>=limit,'meter_available':self.get('token_meter_unavailable')!=day_key(),'shared_meter_state':self.get('shared_meter_state','waiting_for_qq_turn'),'resets_at':(now().replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).timestamp(),'by_kind':rows,'historical_usage_unavailable':True}

    def remember_songs(self,session,songs):
        scope=self.scope_key(session)
        with self.tx() as db:
            for song in songs[:60]:
                db.execute('INSERT INTO music_song_facts VALUES(?,?,?) ON CONFLICT(scope_key,song_id) DO UPDATE SET song_json=excluded.song_json',(scope,str(song['id']),dump(song)))

    def scoped_history(self,session,limit=100):
        with self.lock:
            rows=self.conn.execute("SELECT h.* FROM listening_history h JOIN listen_sessions s ON s.id=h.session_id WHERE s.owner_binding_id=? AND s.bot_music_account_id=? AND s.binding_revision=? AND h.state='played' ORDER BY h.occurred_at DESC LIMIT ?",(*self.scope_key(session).rsplit(':',2),min(500,limit))).fetchall()
            return [{**dict(r),'evidence':load(r['evidence_json'],{})} for r in rows]

    def taste_profile(self,session):
        scope=self.scope_key(session)
        with self.lock:
            signals=[dict(r) for r in self.conn.execute('SELECT * FROM music_taste_signals WHERE scope_key=? ORDER BY created_at DESC LIMIT 200',(scope,))]
            history=self.scoped_history(session,100)
            feedback=self.feedback_items(session)
            ids=list(dict.fromkeys([h['song_id'] for h in history]+[f['song_id'] for f in feedback]))[:300]
            facts={r['song_id']:load(r['song_json'],{}) for r in self.conn.execute('SELECT song_id,song_json FROM music_song_facts WHERE scope_key=? AND song_id IN ('+','.join('?' for _ in ids)+')',(scope,*ids))} if ids else {}
        explicit={}
        for signal in signals:explicit.setdefault(signal['term'].casefold(),signal)
        for f in feedback:
            if f['lifetime']!='longterm':continue
            song=facts.get(f['song_id'],{})
            # Query estimates do not establish a song's actual genre.
            for term in [song.get('artist')]+list(song.get('tags') or []):
                if term:explicit.setdefault(str(term).casefold(),{'term':str(term),'kind':f['kind'],'strength':'strong','source_key':'feedback:'+f['id'],'evidence':f['evidence']})
        weak={}
        for h in history:
            song=facts.get(h['song_id'],{})
            for term in [h.get('artist')]+list(song.get('tags') or []):
                if term and str(term).casefold() not in explicit:weak[str(term).casefold()]={'term':str(term),'kind':'observed','strength':'weak','source_key':'play:'+str(h['id']),'evidence':'实际播放记录；不代表喜欢'}
        excluded=set(self.get('taste_hidden:'+scope,[]))
        return {'known':[v for k,v in explicit.items() if k not in excluded][:30],'inferred':[v for k,v in weak.items() if k not in excluded][:30]}

    def correct_taste(self,session,term,kind,evidence):
        term=str(term).strip()[:80];scope=self.scope_key(session)
        if not term or kind not in {'like','less','clear'}:raise ValueError('invalid_taste')
        with self.tx() as db:
            hidden=set(self.get('taste_hidden:'+scope,[]))
            if kind=='clear':hidden.add(term.casefold())
            else:
                hidden.discard(term.casefold())
                db.execute('INSERT INTO music_taste_signals VALUES(?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,scope,term,kind,'strong','manual',str(evidence)[:500],time.time()))
            self.set('taste_hidden:'+scope,sorted(hidden))
            db.execute('DELETE FROM music_shortlists WHERE scope_key=?',(scope,))

    def recommendation_details(self, session, limit=10):
        with self.lock:
            rows=self.conn.execute('SELECT r.* FROM recommendations r JOIN listen_sessions s ON s.id=r.session_id WHERE s.owner_binding_id=? AND s.bot_music_account_id=? AND s.binding_revision=? ORDER BY r.created_at DESC LIMIT ?',(*self.scope_key(session).rsplit(':',2),min(30,limit))).fetchall()
            result=[]
            for row in rows:
                evidence=load(row['evidence_json'],{})
                usage=self.conn.execute('SELECT input_tokens,output_tokens,source,status FROM music_token_usage WHERE request_id=?',(evidence.get('token_request_id'),)).fetchone()
                result.append({**dict(row),'evidence':evidence,'token_usage':dict(usage) if usage else None})
            return result

    def _migrate_v2(self):
        with self.tx() as db:
            fields = {r['name'] for r in db.execute('PRAGMA table_info(listen_sessions)')}
            for name, typ in [('owner_binding_id','TEXT'),('bot_music_account_id','TEXT'),('binding_revision','INTEGER DEFAULT 1'),('control_generation','INTEGER DEFAULT 1'),('pause_since','REAL'),('absent_since','REAL'),('disconnected_since','REAL'),('leaving_since','REAL'),('remote_result','TEXT'),('last_evidence_at','REAL')]:
                if name not in fields:
                    db.execute(f'ALTER TABLE listen_sessions ADD COLUMN {name} {typ}')
            db.executescript("""
            CREATE TABLE IF NOT EXISTS music_token_usage(request_id TEXT PRIMARY KEY,scope_key TEXT NOT NULL,day TEXT NOT NULL,kind TEXT NOT NULL,shared INTEGER NOT NULL DEFAULT 0,input_tokens INTEGER NOT NULL DEFAULT 0,output_tokens INTEGER NOT NULL DEFAULT 0,source TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS music_token_usage_day ON music_token_usage(scope_key,day);
            CREATE INDEX IF NOT EXISTS music_token_day ON music_token_usage(day);
            CREATE TABLE IF NOT EXISTS music_song_facts(scope_key TEXT NOT NULL,song_id TEXT NOT NULL,song_json TEXT NOT NULL,PRIMARY KEY(scope_key,song_id));
            CREATE TABLE IF NOT EXISTS music_taste_signals(id TEXT PRIMARY KEY,scope_key TEXT NOT NULL,term TEXT NOT NULL,kind TEXT NOT NULL,strength TEXT NOT NULL,source_key TEXT NOT NULL,evidence TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS music_taste_scope ON music_taste_signals(scope_key,strength);
            CREATE INDEX IF NOT EXISTS listen_scope_time ON listen_sessions(owner_binding_id,bot_music_account_id,binding_revision,started_at DESC);
            CREATE INDEX IF NOT EXISTS listening_session_time ON listening_history(session_id,occurred_at DESC);
            CREATE TABLE IF NOT EXISTS music_bindings(id TEXT PRIMARY KEY,owner_umo TEXT NOT NULL,account_id TEXT NOT NULL,revision INTEGER NOT NULL,updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_deadlines(operation_id TEXT PRIMARY KEY,deadline REAL NOT NULL,next_check REAL NOT NULL,checks INTEGER NOT NULL DEFAULT 0,notified INTEGER NOT NULL DEFAULT 0,remote_result TEXT NOT NULL DEFAULT 'unknown',generation INTEGER NOT NULL DEFAULT 1,binding_revision INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS operation_deadlines_due ON operation_deadlines(next_check,deadline);
            CREATE TABLE IF NOT EXISTS music_shortlists(id TEXT PRIMARY KEY,session_id TEXT,scope_key TEXT NOT NULL,revision INTEGER NOT NULL,decision_revision INTEGER NOT NULL,expires_at REAL NOT NULL,songs_json TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS music_shortlists_scope ON music_shortlists(scope_key,expires_at DESC);
            CREATE TABLE IF NOT EXISTS music_feedback(id TEXT PRIMARY KEY,scope_key TEXT NOT NULL,session_id TEXT,song_id TEXT NOT NULL,kind TEXT NOT NULL,lifetime TEXT NOT NULL,evidence TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS music_state(scope_key TEXT PRIMARY KEY,version INTEGER NOT NULL DEFAULT 1,state_json TEXT NOT NULL,updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS listening_invitations(id TEXT PRIMARY KEY,event_key TEXT UNIQUE NOT NULL,scope_key TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,song_json TEXT NOT NULL,reason TEXT NOT NULL,evidence_json TEXT NOT NULL,expires_at REAL NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS listening_invitations_scope ON listening_invitations(scope_key,created_at DESC);
            CREATE TABLE IF NOT EXISTS music_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT UNIQUE NOT NULL,event_type TEXT NOT NULL,payload_json TEXT NOT NULL,created_at REAL NOT NULL,consumed_at REAL);
            CREATE TABLE IF NOT EXISTS music_room_batches(id TEXT PRIMARY KEY,session_id TEXT UNIQUE NOT NULL,scope_key TEXT NOT NULL,summary TEXT NOT NULL,profile_json TEXT NOT NULL,tags_json TEXT NOT NULL,evidence_json TEXT NOT NULL,retracted INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS music_requests(id TEXT PRIMARY KEY,scope_key TEXT NOT NULL,fingerprint TEXT NOT NULL,expected_version INTEGER NOT NULL,state TEXT NOT NULL,payload_json TEXT NOT NULL,result_json TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL);
            """)
            if 'source_key' not in {r['name'] for r in db.execute('PRAGMA table_info(music_feedback)')}:
                db.execute("ALTER TABLE music_feedback ADD COLUMN source_key TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE listening_invitations SET state='failed',updated_at=? WHERE state IN ('preparing','sharing','asking','sending_room_invite')",(time.time(),))
            # Recover old unknown operations as read-only reconciliation, never resend.
            # Already-expired legacy tasks remain auditable without replaying months
            # of timeout notifications on the first upgrade. Existing tracked tasks
            # keep their original deadline and once-only notification state.
            migrated_at = time.time()
            db.execute("INSERT OR IGNORE INTO operation_deadlines(operation_id,deadline,next_check,notified) SELECT operation_id,created_at+120,?,CASE WHEN created_at+120<=? THEN 1 ELSE 0 END FROM operations WHERE state IN ('accepted','running','unknown')", (migrated_at,migrated_at))
            db.execute("UPDATE music_requests SET state='abandoned',result_json=? WHERE state IN ('pending','running')", (dump({'state':'abandoned','remote_result':'unknown','reason':'restart_during_request'}),))

    def binding(self, owner_umo, account_id=None):
        with self.tx() as db:
            row = db.execute('SELECT * FROM music_bindings LIMIT 1').fetchone()
            if not row:
                binding_id = hashlib.sha256(str(owner_umo).encode()).hexdigest()[:24]
                db.execute('INSERT INTO music_bindings VALUES(?,?,?,?,?)',(binding_id,owner_umo,str(account_id or ''),1,time.time()))
            elif account_id and not row['account_id']:
                db.execute('UPDATE music_bindings SET account_id=?,updated_at=? WHERE id=?',(str(account_id),time.time(),row['id']))
            return dict(db.execute('SELECT * FROM music_bindings LIMIT 1').fetchone())

    def switch_binding(self, owner_umo, account_id):
        if self.active_session():
            raise ValueError('end_session_before_switch')
        binding = self.binding(owner_umo)
        with self.tx() as db:
            db.execute('UPDATE music_bindings SET owner_umo=?,account_id=?,revision=revision+1,updated_at=? WHERE id=?',(owner_umo,str(account_id),time.time(),binding['id']))
            db.execute('DELETE FROM music_shortlists')
            db.execute("UPDATE listening_invitations SET state='cancelled',updated_at=? WHERE state NOT IN ('ended','declined','expired','cancelled')",(time.time(),))
        return self.binding(owner_umo)

    def scope_key(self, session=None):
        if session and session.get('owner_binding_id'):
            return ':'.join([session['owner_binding_id'],str(session.get('bot_music_account_id') or ''),str(session.get('binding_revision') or 1)])
        b = self.binding((session or {}).get('owner_umo') or getattr(self,'owner_umo',''))
        return ':'.join([b['id'],b['account_id'],str(b['revision'])])

    def bind_session(self, session_id, owner_umo, account_id=None):
        b=self.binding(owner_umo, account_id)
        with self.tx() as db:
            db.execute('UPDATE listen_sessions SET owner_binding_id=?,bot_music_account_id=?,binding_revision=? WHERE id=?',(b['id'],b['account_id'],b['revision'],session_id))
        return b

    def track_operation(self, operation_id, session):
        with self.tx() as db:
            db.execute('INSERT OR IGNORE INTO operation_deadlines(operation_id,deadline,next_check,generation,binding_revision) VALUES(?,?,?,?,?)',(operation_id,time.time()+120,time.time()+45,int(session.get('control_generation') or 1),int(session.get('binding_revision') or 1)))

    def reconciliation_due(self, at=None):
        at=time.time() if at is None else at
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT o.*,d.deadline,d.checks,d.generation,d.binding_revision,d.notified FROM operations o JOIN operation_deadlines d USING(operation_id) WHERE o.state IN ('accepted','running','unknown') AND d.next_check<=? ORDER BY d.next_check LIMIT 20",(at,))]

    def reconcile_attempt(self, operation_id, state=None, result=None):
        with self.tx() as db:
            db.execute('UPDATE operation_deadlines SET checks=checks+1,next_check=? WHERE operation_id=?',(time.time()+25,operation_id))
            if state:
                db.execute('UPDATE operations SET state=?,result_json=?,updated_at=? WHERE operation_id=?',(state,dump(result or {}),time.time(),operation_id))
                db.execute('UPDATE operation_deadlines SET remote_result=? WHERE operation_id=?',('unknown' if state=='abandoned' else state,operation_id))

    def notify_operation_once(self, operation_id):
        with self.tx() as db:
            return bool(db.execute('UPDATE operation_deadlines SET notified=1 WHERE operation_id=? AND notified=0',(operation_id,)).rowcount)

    def emit_music_event(self, key, kind, payload):
        with self.tx() as db:
            db.execute('INSERT OR IGNORE INTO music_outbox(event_key,event_type,payload_json,created_at) VALUES(?,?,?,?)',(key,kind,dump(payload),time.time()))

    def music_events(self, after=0, pending=False):
        with self.lock:
            rows=self.conn.execute('SELECT * FROM music_outbox WHERE seq>? '+('AND consumed_at IS NULL ' if pending else '')+'ORDER BY seq LIMIT 50',(int(after),)).fetchall()
            return [{**dict(r),'payload':load(r['payload_json'],{})} for r in rows]

    def acknowledge_music_events(self, seqs):
        with self.tx() as db:
            db.executemany('UPDATE music_outbox SET consumed_at=? WHERE seq=?',[(time.time(),int(seq)) for seq in seqs[:50]])

    def music_state(self, scope):
        with self.lock:
            row=self.conn.execute('SELECT * FROM music_state WHERE scope_key=?',(scope,)).fetchone()
            return {**load(row['state_json'],{}),'version':row['version'],'updated_at':row['updated_at']} if row else {'version':0}

    def update_music_state(self, scope, delta, expected_version=None):
        allowed={'energy','emotion','closeness','session_intent','tags','context_score','accumulation_score','relationship_score','evidence','weather','memory_terms'}
        with self.tx() as db:
            old=self.music_state(scope)
            if expected_version is not None and int(expected_version)!=old['version']:
                raise ValueError('stale_version')
            clean={k:v for k,v in delta.items() if k in allowed}
            for k in ('energy','emotion','closeness','context_score','accumulation_score','relationship_score'):
                if k in clean:
                    clean[k]=float(clean[k])
                    if not math.isfinite(clean[k]):raise ValueError('finite_state_required')
                    clean[k]=max(0.,min(1.,clean[k]))
            for k in ('tags','memory_terms'):
                if k in clean:clean[k]=[str(x)[:40] for x in clean[k]][:10]
            if 'weather' in clean:
                weather=clean['weather']
                if not isinstance(weather,dict) or not weather.get('source') or not weather.get('observed_at'):raise ValueError('weather_source_required')
            if 'session_intent' in clean:clean['session_intent']=str(clean['session_intent'])[:200]
            clean['evidence']=str(clean.get('evidence') or '')[:500]
            if not clean['evidence']:raise ValueError('state_evidence_required')
            current={k:v for k,v in old.items() if k not in {'version','updated_at'}}
            provenance=dict(current.get('parameter_evidence') or {})
            for key in clean:
                if key!='evidence':provenance[key]={'source':'explicit_context','evidence':clean['evidence'],'updated_at':time.time()}
            current.update(clean);current['parameter_evidence']=provenance
            if any(k in clean and clean[k]!=old.get(k) for k in ('session_intent','tags','memory_terms','energy','emotion','closeness')):
                db.execute('DELETE FROM music_shortlists WHERE scope_key=?',(scope,))
            db.execute('INSERT INTO music_state VALUES(?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET version=excluded.version,state_json=excluded.state_json,updated_at=excluded.updated_at',(scope,old['version']+1,dump(current),time.time()))
        return self.music_state(scope)

    def save_shortlist(self, session, songs):
        sid=session.get('id'); scope=self.scope_key(session); b=self.binding(session.get('owner_umo',''))
        batch=str(uuid.uuid4())
        with self.tx() as db:
            db.execute('DELETE FROM music_shortlists WHERE scope_key=?',(scope,))
            db.execute('INSERT INTO music_shortlists VALUES(?,?,?,?,?,?,?,?)',(batch,sid,scope,b['revision'],int(session.get('decision_revision') or 0),time.time()+1800,dump(songs[:3]),time.time()))
        return self.shortlist(session)

    def shortlist(self, session):
        scope=self.scope_key(session)
        with self.lock:
            r=self.conn.execute('SELECT * FROM music_shortlists WHERE scope_key=? AND session_id IS ? AND expires_at>? ORDER BY created_at DESC LIMIT 1',(scope,session.get('id'),time.time())).fetchone()
            if not r or int(r['decision_revision'])!=int(session.get('decision_revision') or 0):return None
            return {**dict(r),'songs':load(r['songs_json'],[])}

    def consume_shortlist(self,batch_id,song_id):
        with self.tx() as db:
            row=db.execute('SELECT songs_json FROM music_shortlists WHERE id=?',(batch_id,)).fetchone()
            if row:db.execute('UPDATE music_shortlists SET songs_json=? WHERE id=?',(dump([s for s in load(row['songs_json'],[]) if str(s.get('id'))!=str(song_id)]),batch_id))

    def feedback(self,session,song_id,kind,lifetime,evidence,source_key=''):
        if kind not in {'like','less'} or lifetime not in {'session','longterm'} or not evidence:raise ValueError('invalid_feedback')
        with self.tx() as db:
            db.execute('INSERT INTO music_feedback(id,scope_key,session_id,song_id,kind,lifetime,evidence,created_at,source_key) VALUES(?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),self.scope_key(session),session.get('id'),str(song_id),kind,lifetime,str(evidence)[:500],time.time(),source_key))
            db.execute('DELETE FROM music_shortlists WHERE scope_key=?',(self.scope_key(session),))

    def feedback_items(self,session):
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM music_feedback WHERE scope_key=? AND (lifetime='longterm' OR session_id IS ?) ORDER BY created_at DESC LIMIT 200",(self.scope_key(session),session.get('id')))]

    def room_batch(self,session_id):
        with self.lock:
            row=self.conn.execute('SELECT * FROM music_room_batches WHERE session_id=?',(session_id,)).fetchone()
            return {**dict(row),'tags':load(row['tags_json'],[]),'profile':load(row['profile_json'],{})} if row else None

    def commit_room_batch(self,session,summary,profile,tags,evidence):
        batch='room:'+session['id']
        with self.tx() as db:
            db.execute('INSERT OR IGNORE INTO music_room_batches VALUES(?,?,?,?,?,?,?,?,?,?)',(batch,session['id'],self.scope_key(session),str(summary)[:300],dump(profile),dump(list(tags)[:5]),dump(evidence),0,1,time.time()))
        return self.room_batch(session['id'])

    def journal(self,limit=30):
        with self.lock:
            return [{**dict(r),'batch':self.room_batch(r['id']),'tracks':list(reversed(self.history(r['id'],100)))} for r in self.conn.execute('SELECT * FROM listen_sessions WHERE owner_binding_id=? AND bot_music_account_id=? AND binding_revision=? ORDER BY started_at DESC LIMIT ?',(*self.scope_key().rsplit(':',2),min(50,max(1,limit))))]

    def edit_room_batch(self,session_id,summary,expected_version,retract=False):
        session=self.session(session_id)
        if not session or self.scope_key(session)!=self.scope_key():raise ValueError('journal_scope_mismatch')
        with self.tx() as db:
            count=db.execute('UPDATE music_room_batches SET summary=?,retracted=?,version=version+1 WHERE session_id=? AND version=?',(str(summary)[:300],int(retract),session_id,int(expected_version))).rowcount
            if not count:raise ValueError('stale_version')
            db.execute('DELETE FROM room_memory_drafts WHERE session_id=?',(session_id,))
            db.execute('DELETE FROM room_summaries WHERE session_id=?',(session_id,))
            if retract:
                db.execute('DELETE FROM music_feedback WHERE scope_key=? AND source_key=?',(self.scope_key(session),'journal:'+session_id))
                db.execute('DELETE FROM music_taste_signals WHERE scope_key=? AND source_key=?',(self.scope_key(session),'journal:'+session_id))
                db.execute('DELETE FROM music_shortlists WHERE scope_key=?',(self.scope_key(session),))
        return self.room_batch(session_id)

    def request_job(self,key,scope,payload,expected_version):
        fingerprint=hashlib.sha256(dump({'payload':payload,'version':expected_version}).encode()).hexdigest()
        with self.tx() as db:
            row=db.execute('SELECT * FROM music_requests WHERE id=?',(key,)).fetchone()
            if row:
                if row['scope_key']!=scope or row['fingerprint']!=fingerprint:raise ValueError('idempotency_conflict')
                return dict(row),False
            db.execute('INSERT INTO music_requests VALUES(?,?,?,?,?,?,?,?,?)',(key,scope,fingerprint,int(expected_version),'pending',dump(payload),None,time.time(),time.time()))
            return dict(db.execute('SELECT * FROM music_requests WHERE id=?',(key,)).fetchone()),True

    def music_job(self,key):
        with self.lock:
            r=self.conn.execute('SELECT * FROM music_requests WHERE id=?',(key,)).fetchone()
            return {**dict(r),'result':load(r['result_json'],{})} if r else None

    def finish_music_job(self,key,result):
        with self.tx() as db:
            db.execute('UPDATE music_requests SET state=?,result_json=?,updated_at=? WHERE id=?',(result.get('state','failed'),dump(result),time.time(),key))
