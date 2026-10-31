"""Durable search batches, listening cycles and a once-only introduction outbox."""
from __future__ import annotations
import json
import time
import uuid


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class StoreV13:
    def migrate_v13(self):
        with self.tx() as db:
            first = not db.execute("SELECT 1 FROM sqlite_master WHERE name='music_search_batches'").fetchone()
            db.executescript("""
            CREATE TABLE IF NOT EXISTS music_search_batches(id TEXT PRIMARY KEY,session_id TEXT,scope_key TEXT NOT NULL,revision INTEGER NOT NULL,decision_revision INTEGER NOT NULL,query_json TEXT NOT NULL,songs_json TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS music_search_scope ON music_search_batches(scope_key,expires_at);
            CREATE TABLE IF NOT EXISTS music_cycles(session_id TEXT PRIMARY KEY,qualified INTEGER NOT NULL DEFAULT 0,baseline INTEGER NOT NULL DEFAULT 0,target INTEGER NOT NULL DEFAULT 2,runs INTEGER NOT NULL DEFAULT 0,last_recommend_at REAL NOT NULL DEFAULT 0,user_count INTEGER NOT NULL DEFAULT 0,user_baseline INTEGER NOT NULL DEFAULT 0,user_target INTEGER NOT NULL DEFAULT 2,protect_until REAL NOT NULL DEFAULT 0,protect_song_id TEXT);
            CREATE TABLE IF NOT EXISTS music_playback_instances(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,song_id TEXT NOT NULL,origin TEXT NOT NULL,song_json TEXT NOT NULL,started_at REAL NOT NULL,last_at REAL NOT NULL,last_progress REAL NOT NULL DEFAULT 0,listened REAL NOT NULL DEFAULT 0,qualified INTEGER NOT NULL DEFAULT 0,ended_at REAL);
            CREATE UNIQUE INDEX IF NOT EXISTS music_instance_active ON music_playback_instances(session_id) WHERE ended_at IS NULL;
            CREATE INDEX IF NOT EXISTS music_instances_session ON music_playback_instances(session_id,started_at DESC);
            CREATE TABLE IF NOT EXISTS music_introductions(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,song_id TEXT NOT NULL,instance_id TEXT,recommendation_id TEXT,kind TEXT NOT NULL,state TEXT NOT NULL,text TEXT NOT NULL DEFAULT '',decision_revision INTEGER NOT NULL,not_before REAL NOT NULL,deadline REAL NOT NULL,receipt_json TEXT NOT NULL DEFAULT '{}',created_at REAL NOT NULL,updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS music_intro_due ON music_introductions(state,not_before);
            CREATE TABLE IF NOT EXISTS music_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT,stage TEXT NOT NULL,detail_json TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS music_decisions_session ON music_decisions(session_id,id DESC);
            """)
            if first:
                db.execute('DELETE FROM music_shortlists')
            if 'user_counted' not in {r[1] for r in db.execute('PRAGMA table_info(music_playback_instances)')}:
                db.execute('ALTER TABLE music_playback_instances ADD COLUMN user_counted INTEGER NOT NULL DEFAULT 0')
            if 'played_seconds' not in {r[1] for r in db.execute('PRAGMA table_info(music_cycles)')}:
                db.execute('ALTER TABLE music_cycles ADD COLUMN played_seconds REAL NOT NULL DEFAULT 0')
            # A crash after send started is ambiguous: never replay it blindly.
            db.execute("UPDATE music_introductions SET state='unknown',updated_at=? WHERE state='sending'", (time.time(),))

    def save_search_batch(self, session, songs, query):
        at = time.time(); binding = self.binding(session.get('owner_umo', ''))
        ident = uuid.uuid4().hex
        with self.tx() as db:
            db.execute('DELETE FROM music_search_batches WHERE expires_at<?', (at,))
            db.execute('INSERT INTO music_search_batches VALUES(?,?,?,?,?,?,?,?,?)',
                       (ident, session.get('id'), self.scope_key(session), binding['revision'],
                        int(session.get('decision_revision') or 0), dump(query), dump(songs[:120]), at, at+300))
        self.remember_songs(session, songs[:120])
        return self.search_batch(session, ident)

    def search_batch(self, session, ident):
        binding=self.binding(session.get('owner_umo',''))
        if session.get('owner_umo')!=binding['owner_umo']:
            return None
        with self.lock:
            row = self.conn.execute('SELECT * FROM music_search_batches WHERE id=? AND scope_key=? AND expires_at>?',
                                    (ident, self.scope_key(session), time.time())).fetchone()
        if not row or row['session_id'] != session.get('id'):
            return None
        if row['revision'] != self.binding(session.get('owner_umo', ''))['revision']:
            return None
        if row['decision_revision'] != int(session.get('decision_revision') or 0):
            return None
        return {**dict(row), 'purpose': 'search', 'songs': json.loads(row['songs_json']), 'query': json.loads(row['query_json'])}

    def decision(self, session_id, stage, **detail):
        with self.tx() as db:
            db.execute('INSERT INTO music_decisions(session_id,stage,detail_json,created_at) VALUES(?,?,?,?)',
                       (session_id, stage, dump(detail), time.time()))

    def decision_trace(self, session_id):
        with self.lock:
            rows = self.conn.execute('SELECT * FROM music_decisions WHERE session_id IS ? ORDER BY id DESC LIMIT 24', (session_id,)).fetchall()
        return [{**dict(r), 'detail': json.loads(r['detail_json'])} for r in rows]

    def cycle(self, session_id, create=False):
        if create:
            with self.tx() as db:
                db.execute('INSERT OR IGNORE INTO music_cycles(session_id) VALUES(?)', (session_id,))
        with self.lock:
            row = self.conn.execute('SELECT * FROM music_cycles WHERE session_id=?', (session_id,)).fetchone()
        return dict(row) if row else {'session_id': session_id, 'qualified': 0, 'baseline': 0, 'target': 2,
                                     'runs': 0, 'last_recommend_at': 0, 'user_count': 0, 'user_baseline': 0,
                                     'user_target': 2, 'protect_until': 0, 'protect_song_id': None,'played_seconds':0}

    def observe_listening(self, session, song, playback, origin, at=None, reliable=True):
        at = time.time() if at is None else at; sid = session['id']
        if not reliable or playback.get('status') != 'PLAY' or not song.get('id'):
            with self.tx() as db:
                # Break continuity without manufacturing listening time.
                db.execute('UPDATE music_playback_instances SET last_at=? WHERE session_id=? AND ended_at IS NULL', (at, sid))
            return None
        progress = max(0, float(playback.get('progress_ms') or 0))
        duration = float(song.get('duration_ms') or song.get('durationMs') or 0)/1000
        self.cycle(sid, True)
        with self.tx() as db:
            row = db.execute('SELECT * FROM music_playback_instances WHERE session_id=? AND ended_at IS NULL', (sid,)).fetchone()
            wall = max(0, at-row['last_at']) if row else 0
            looped = bool(row and duration > 0 and row['song_id'] == str(song['id']) and wall <= 35
                          and row['last_progress']/1000 > duration*.9 and progress/1000 < duration*.1
                          and row['last_progress']/1000+wall >= duration-.5)
            if row and (row['song_id'] != str(song['id']) or looped):
                db.execute('UPDATE music_playback_instances SET ended_at=? WHERE id=?', (at, row['id'])); row = None
            if not row:
                ident = uuid.uuid4().hex
                db.execute('INSERT INTO music_playback_instances(id,session_id,song_id,origin,song_json,started_at,last_at,last_progress) VALUES(?,?,?,?,?,?,?,?)',
                           (ident, sid, str(song['id']), origin, dump(song), at, at, progress))
                row = db.execute('SELECT * FROM music_playback_instances WHERE id=?', (ident,)).fetchone(); wall = 0
            listened = row['listened'] + (wall if 0 <= wall <= 35 else 0)
            db.execute('UPDATE music_cycles SET played_seconds=played_seconds+? WHERE session_id=?',(wall if 0<=wall<=35 else 0,sid))
            threshold = min(60, duration*.8) if duration > 0 else 60
            qualified = int(listened >= threshold)
            db.execute('UPDATE music_playback_instances SET last_at=?,last_progress=?,listened=?,qualified=? WHERE id=?',
                       (at, progress, listened, max(row['qualified'], qualified), row['id']))
            if qualified and not row['qualified']:
                db.execute('UPDATE music_cycles SET qualified=qualified+1 WHERE session_id=?', (sid,))
            if row['origin'] != 'bot' and listened >= 15 and not row['user_counted']:
                db.execute('UPDATE music_playback_instances SET user_counted=1 WHERE id=?', (row['id'],))
                db.execute('UPDATE music_cycles SET user_count=user_count+1 WHERE session_id=?', (sid,))
            return dict(db.execute('SELECT * FROM music_playback_instances WHERE id=?', (row['id'],)).fetchone())

    def advance_cycle(self, session_id, at=None):
        at = time.time() if at is None else at
        self.cycle(session_id, True)
        with self.tx() as db:
            db.execute('UPDATE music_cycles SET baseline=qualified,target=CASE WHEN runs%2=0 THEN 2 ELSE 3 END,runs=runs+1,last_recommend_at=? WHERE session_id=?', (at, session_id))

    def protect_queue(self, session_id, song_id=None, seconds=120):
        self.cycle(session_id, True)
        with self.tx() as db:
            db.execute('UPDATE music_cycles SET protect_until=?,protect_song_id=? WHERE session_id=?',
                       (time.time()+seconds, str(song_id) if song_id else None, session_id))

    def queue_introduction(self, session, song_id, text='', *, recommendation_id=None, instance_id=None, kind='recommendation', delay=0):
        ident = 'intro:'+str(recommendation_id or instance_id)
        at = time.time()
        with self.tx() as db:
            db.execute('INSERT OR IGNORE INTO music_introductions(id,session_id,song_id,instance_id,recommendation_id,kind,state,text,decision_revision,not_before,deadline,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, session['id'], str(song_id), instance_id, recommendation_id, kind,
                        'ready' if text else 'pending', text[:80], int(session.get('decision_revision') or 0), at+delay, at+180, at, at))
        return ident

    def introductions(self, session_id, pending=False):
        condition = " AND state IN ('pending','ready','unknown')" if pending else ''
        with self.lock:
            rows = self.conn.execute('SELECT * FROM music_introductions WHERE session_id=?'+condition+' ORDER BY created_at DESC LIMIT 20', (session_id,)).fetchall()
        return [{**dict(r), 'receipt': json.loads(r['receipt_json'])} for r in rows]

    def finish_introduction(self, ident, state, receipt=None, text=None):
        with self.tx() as db:
            db.execute('UPDATE music_introductions SET state=?,receipt_json=?,text=COALESCE(?,text),updated_at=? WHERE id=?',
                       (state, dump(receipt or {}), text[:80] if text else None, time.time(), ident))
