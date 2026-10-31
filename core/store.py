from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import day_key, now

SCHEMA_VERSION = 9
PENDING_RECOMMENDATION_SQL = "('selecting','enqueueing','queued','queued_intro_sent','queued_intro_unknown','unknown')"
MUSIC_CONTROL_ACTIONS = {
    "JOIN",
    "CREATE",
    "PLAY_NOW",
    "ENQUEUE_NEXT",
    "NEXT",
    "PAUSE",
    "RESUME",
    "CANCEL_QUEUED_RECOMMENDATION",
    "LEAVE",
}


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def _loads(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _ts(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return float(value)
    except (TypeError, ValueError):
        return time.time()


def _first_song_id(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key in ("song_id", "songId", "id"):
            candidate = value.get(key)
            if candidate is not None and str(candidate).strip():
                return str(candidate).strip()
        for key in ("enqueue", "playback", "result"):
            nested = _first_song_id(value.get(key))
            if nested:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _first_song_id(item)
            if nested:
                return nested
    return None


from .upgrade_store import UpgradeStore


class CompanionStore(UpgradeStore):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.migration_backup: Path | None = None
        current_version = self._schema_version()
        if current_version and current_version < SCHEMA_VERSION:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.migration_backup = self.backup(
                self.path.parent / "backups" / f"pre-migration-v{current_version}-{stamp}.sqlite3"
            )
        self._migrate()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def _schema_version(self) -> int:
        try:
            row = self.conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            return int(row["value"]) if row else 0
        except (sqlite3.Error, TypeError, ValueError):
            return 0

    @contextmanager
    def tx(self):
        with self.lock:
            nested=self.conn.in_transaction
            name='sp_'+uuid.uuid4().hex
            if nested:self.conn.execute('SAVEPOINT '+name)
            else:self.conn.execute('BEGIN')
            try:
                yield self.conn
                if nested:self.conn.execute('RELEASE SAVEPOINT '+name)
                else:self.conn.commit()
            except BaseException:
                if nested:
                    self.conn.execute('ROLLBACK TO SAVEPOINT '+name)
                    self.conn.execute('RELEASE SAVEPOINT '+name)
                else:self.conn.rollback()
                raise

    def _migrate(self) -> None:
        with self.tx() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS listen_sessions (
                    id TEXT PRIMARY KEY,
                    owner_umo TEXT NOT NULL,
                    persona_id TEXT,
                    room_id TEXT,
                    room_epoch TEXT,
                    phase TEXT NOT NULL,
                    autodj_enabled INTEGER NOT NULL DEFAULT 1,
                    room_chat_enabled INTEGER NOT NULL DEFAULT 1,
                    playback_state TEXT NOT NULL DEFAULT 'UNKNOWN',
                    session_version INTEGER NOT NULL DEFAULT 1,
                    started_at REAL NOT NULL,
                    expires_at REAL,
                    ended_at REAL,
                    next_recommend_at REAL,
                    next_eligibility_check_at REAL,
                    blocked_reason TEXT,
                    blocked_until REAL,
                    decision_revision INTEGER NOT NULL DEFAULT 0,
                    end_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS listen_sessions_phase ON listen_sessions(phase, started_at DESC);
                CREATE TABLE IF NOT EXISTS owner_bindings (
                    owner_umo TEXT PRIMARY KEY,
                    qq_uid TEXT NOT NULL,
                    netease_uid TEXT,
                    persona_id TEXT,
                    confirmed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    kind TEXT NOT NULL,
                    slot_key TEXT NOT NULL,
                    run_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until REAL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(kind, slot_key)
                );
                CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, run_at);
                CREATE TABLE IF NOT EXISTS event_inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    source TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at REAL NOT NULL,
                    processed_at REAL,
                    UNIQUE(source, event_id)
                );
                CREATE INDEX IF NOT EXISTS event_inbox_pending ON event_inbox(status, id);
                CREATE TABLE IF NOT EXISTS event_cursors (
                    source TEXT PRIMARY KEY,
                    cursor INTEGER NOT NULL,
                    gap_from INTEGER,
                    gap_to INTEGER,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recommendations (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    slot_key TEXT NOT NULL,
                    song_id TEXT NOT NULL,
                    song_name TEXT,
                    artist TEXT,
                    intro TEXT,
                    state TEXT NOT NULL,
                    source TEXT,
                    operation_id TEXT,
                    evidence_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(session_id, slot_key)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS recommendations_one_pending
                    ON recommendations(session_id) WHERE state IN ('selecting','enqueueing','queued','unknown');
                CREATE INDEX IF NOT EXISTS recommendations_recent ON recommendations(session_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS listening_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    song_id TEXT NOT NULL,
                    song_name TEXT,
                    artist TEXT,
                    state TEXT NOT NULL,
                    source TEXT,
                    occurred_at REAL NOT NULL,
                    evidence_json TEXT
                );
                CREATE INDEX IF NOT EXISTS listening_history_song_time ON listening_history(song_id, occurred_at DESC);
                CREATE TABLE IF NOT EXISTS room_messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    provider_message_id TEXT,
                    sender_uid TEXT,
                    sender_role TEXT,
                    text TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    sent_at REAL,
                    UNIQUE(session_id, provider_message_id)
                );
                CREATE TABLE IF NOT EXISTS reply_batches (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    first_at REAL NOT NULL,
                    last_at REAL NOT NULL,
                    state TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    output_text TEXT,
                    message_id TEXT,
                    error TEXT,
                    UNIQUE(session_id, first_at)
                );
                CREATE TABLE IF NOT EXISTS model_calls (
                    id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    hour INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    finished_at REAL,
                    latency_ms INTEGER,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS model_calls_day ON model_calls(day, kind, status);
                CREATE INDEX IF NOT EXISTS model_calls_hour ON model_calls(hour, kind, status);
                CREATE TABLE IF NOT EXISTS recommendation_runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('auto','manual')),
                    day TEXT NOT NULL,
                    hour INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    reason TEXT,
                    job_id TEXT,
                    created_at REAL NOT NULL,
                    finished_at REAL,
                    locked_until REAL
                );
                CREATE INDEX IF NOT EXISTS recommendation_runs_hour ON recommendation_runs(day,hour,mode,state);
                CREATE INDEX IF NOT EXISTS recommendation_runs_day ON recommendation_runs(day,mode,state);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    action TEXT NOT NULL,
                    origin_kind TEXT NOT NULL,
                    request_id TEXT,
                    state TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS imports (
                    source_path TEXT NOT NULL,
                    file_digest TEXT NOT NULL,
                    line_no INTEGER NOT NULL,
                    record_key TEXT NOT NULL,
                    imported_at REAL NOT NULL,
                    PRIMARY KEY(source_path, file_digest, line_no)
                );
                CREATE TABLE IF NOT EXISTS agent_turns (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    kind TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    song_id TEXT,
                    persona_id TEXT,
                    provider_id TEXT,
                    state TEXT NOT NULL,
                    context_bytes INTEGER NOT NULL DEFAULT 0,
                    input_json TEXT NOT NULL,
                    output_text TEXT,
                    tool_calls_json TEXT NOT NULL DEFAULT '[]',
                    operation_ids_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    started_at REAL NOT NULL,
                    finished_at REAL
                );
                CREATE INDEX IF NOT EXISTS agent_turns_kind_time ON agent_turns(kind, started_at DESC);
                CREATE TABLE IF NOT EXISTS search_cache (
                    turn_id TEXT NOT NULL,
                    session_id TEXT,
                    song_id TEXT NOT NULL,
                    song_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY(turn_id, song_id)
                );
                CREATE INDEX IF NOT EXISTS search_cache_expiry ON search_cache(expires_at);
                CREATE TABLE IF NOT EXISTS candidate_batches (
                    id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    session_id TEXT,
                    song_ids_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_overrides (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    event_type TEXT NOT NULL,
                    attribution TEXT NOT NULL,
                    room_id TEXT,
                    old_song_id TEXT,
                    new_song_id TEXT,
                    old_fingerprint TEXT,
                    new_fingerprint TEXT,
                    evidence_json TEXT NOT NULL,
                    action_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS external_overrides_time ON external_overrides(created_at DESC);
                CREATE TABLE IF NOT EXISTS room_summaries (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    song_id TEXT,
                    song_name TEXT,
                    summary TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS room_summaries_day ON room_summaries(created_at DESC);
                CREATE TABLE IF NOT EXISTS room_memory_drafts (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    song_id TEXT NOT NULL,
                    song_name TEXT,
                    artist TEXT,
                    user_text TEXT NOT NULL DEFAULT '',
                    assistant_text TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(session_id, song_id)
                );
                CREATE INDEX IF NOT EXISTS room_memory_drafts_session ON room_memory_drafts(session_id, updated_at DESC);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(room_summaries)").fetchall()}
            if "source_key" not in columns:
                db.execute("ALTER TABLE room_summaries ADD COLUMN source_key TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS room_summaries_source_key ON room_summaries(source_key) WHERE source_key IS NOT NULL")
            session_columns = {row["name"] for row in db.execute("PRAGMA table_info(listen_sessions)").fetchall()}
            for name, declaration in (
                ("next_eligibility_check_at", "REAL"),
                ("blocked_reason", "TEXT"),
                ("blocked_until", "REAL"),
                ("decision_revision", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in session_columns:
                    db.execute(f"ALTER TABLE listen_sessions ADD COLUMN {name} {declaration}")
            db.execute(
                "UPDATE listen_sessions SET next_eligibility_check_at=next_recommend_at "
                "WHERE next_eligibility_check_at IS NULL AND next_recommend_at IS NOT NULL"
            )
            db.execute("UPDATE recommendation_runs SET state='abandoned',finished_at=?,reason='schema_upgrade' WHERE state='running'", (time.time(),))
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS recommendation_runs_one_active "
                "ON recommendation_runs(session_id) WHERE state='running'"
            )
            db.execute(
                "DELETE FROM external_overrides WHERE event_type='playback_changed' AND old_song_id IS NOT NULL AND new_song_id IS NOT NULL AND old_song_id=new_song_id"
            )
            db.execute(
                "UPDATE external_overrides SET old_song_id=NULL,new_song_id=NULL WHERE event_type='queue_changed'"
            )
            db.execute("INSERT INTO schema_meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))
        self._migrate_v2()
        self.migrate_v13()
        self.audit("system", "migration", {"version": SCHEMA_VERSION})

    def audit(self, actor: str, kind: str, detail: Mapping[str, Any]) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO audit_log(actor,kind,detail_json,created_at) VALUES(?,?,?,?)", (actor, kind, _json(dict(detail)), time.time()))

    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return _loads(row["value"], default) if row else default

    def set(self, key: str, value: Any) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, _json(value), time.time()))

    def active_session(self) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM listen_sessions WHERE phase NOT IN ('ended') ORDER BY started_at DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def session(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM listen_sessions WHERE id=?", (session_id,)).fetchone()
            return dict(row) if row else None

    def create_session(self, owner_umo: str, room_id: str, room_epoch: str | None, autodj: bool, room_chat: bool, max_minutes: int = 0, persona_id: str | None = None) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        started = time.time()
        expires = started + max_minutes * 60 if max_minutes else None
        next_at = started
        with self.tx() as db:
            db.execute("UPDATE listen_sessions SET phase='ended',ended_at=?,end_reason='replaced' WHERE phase NOT IN ('ended')", (started,))
            db.execute(
                "INSERT INTO listen_sessions(id,owner_umo,persona_id,room_id,room_epoch,phase,autodj_enabled,room_chat_enabled,playback_state,session_version,started_at,expires_at,next_recommend_at) VALUES(?,?,?,?,?,'joining',?,?,'UNKNOWN',1,?,?,?)",
                (session_id, owner_umo, persona_id, room_id, room_epoch, int(autodj), int(room_chat), started, expires, next_at),
            )
        self.bind_session(session_id,owner_umo)
        return self.session(session_id) or {}

    def update_session(self, session_id: str, **values: Any) -> None:
        allowed = {"persona_id", "room_id", "room_epoch", "phase", "autodj_enabled", "room_chat_enabled", "playback_state", "expires_at", "ended_at", "next_recommend_at", "next_eligibility_check_at", "blocked_reason", "blocked_until", "decision_revision", "end_reason", "pause_since", "absent_since", "disconnected_since", "leaving_since", "remote_result", "last_evidence_at", "control_generation", "bot_music_account_id", "owner_binding_id", "binding_revision"}
        with self.lock:
            row = self.conn.execute("SELECT * FROM listen_sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                return
            fields = [(key, value) for key, value in values.items() if key in allowed and row[key] != value]
            if not fields:
                return
        with self.tx() as db:
            bump=any(key not in {"pause_since","absent_since","disconnected_since","last_evidence_at","next_eligibility_check_at","next_recommend_at","blocked_until"} for key,_ in fields)
            db.execute(f"UPDATE listen_sessions SET {', '.join(key + '=?' for key, _ in fields)}, session_version=session_version+? WHERE id=?", (*[value for _, value in fields], int(bump),session_id))
        if "phase" in values and values["phase"] == "ended":
            with self.tx() as db:
                db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE session_id=? AND status IN ('pending','running')", (time.time(), session_id))

    def sync_active_persona(self, persona_id: str | None) -> int:
        value = str(persona_id or "").strip()
        if not value:
            return 0
        with self.tx() as db:
            cursor = db.execute(
                "UPDATE listen_sessions SET persona_id=?,session_version=session_version+1 WHERE phase!='ended' AND COALESCE(persona_id,'')!=?",
                (value, value),
            )
            return int(cursor.rowcount or 0)

    def session_version(self, session_id: str) -> int:
        with self.lock:
            row = self.conn.execute("SELECT session_version FROM listen_sessions WHERE id=?", (session_id,)).fetchone()
            return int(row["session_version"]) if row else 0

    def set_recommendation_due(self, session_id: str, due_at: float) -> None:
        self.update_session(
            session_id,
            next_recommend_at=float(due_at),
            next_eligibility_check_at=float(due_at),
            blocked_reason=None,
            blocked_until=None,
        )

    def defer_recommendation(self, session_id: str, reason: str, until: float, *, shorten_only: bool = False) -> None:
        with self.lock:
            row = self.conn.execute(
                "SELECT next_eligibility_check_at,blocked_until FROM listen_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if not row:
                return
            current_check = float(row["next_eligibility_check_at"] or 0)
            current_until = float(row["blocked_until"] or 0)
        if shorten_only:
            check_at = min(current_check, until) if current_check > time.time() else until
            until_at = min(current_until, until) if current_until > time.time() else until
        else:
            check_at = until_at = until
        self.update_session(
            session_id,
            next_eligibility_check_at=check_at,
            blocked_reason=str(reason)[:120],
            blocked_until=until_at,
        )

    def clear_recommendation_block(self, session_id: str) -> None:
        self.update_session(session_id, blocked_reason=None, blocked_until=None)

    def bump_decision_revision(self, session_id: str) -> int:
        with self.tx() as db:
            db.execute("UPDATE listen_sessions SET decision_revision=decision_revision+1,session_version=session_version+1 WHERE id=?", (session_id,))
            row = db.execute("SELECT decision_revision FROM listen_sessions WHERE id=?", (session_id,)).fetchone()
            return int(row["decision_revision"]) if row else 0

    def has_pending_job(self, kind: str, session_id: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM jobs WHERE kind=? AND session_id=? AND status IN ('pending','running') LIMIT 1",
                (kind, session_id),
            ).fetchone()
            return row is not None

    def enqueue_job(self, kind: str, slot_key: str, run_at: float, session_id: str | None = None, payload: Mapping[str, Any] | None = None) -> str | None:
        job_id = str(uuid.uuid4())
        with self.tx() as db:
            try:
                db.execute("INSERT INTO jobs(id,session_id,kind,slot_key,run_at,status,payload_json,created_at,updated_at) VALUES(?,?,?,?,?, 'pending',?,?,?)", (job_id, session_id, kind, slot_key, run_at, _json(dict(payload or {})), time.time(), time.time()))
                return job_id
            except sqlite3.IntegrityError:
                return None

    def claim_due_job(self, kinds: Iterable[str] | None = None) -> dict[str, Any] | None:
        now_ts = time.time()
        clauses = ["status IN ('pending','running')", "run_at<=?", "(locked_until IS NULL OR locked_until<?)"]
        params: list[Any] = [now_ts, now_ts]
        values = list(kinds or [])
        if values:
            clauses.append("kind IN (" + ",".join("?" for _ in values) + ")")
            params.extend(values)
        with self.tx() as db:
            row = db.execute("UPDATE jobs SET status='running',attempts=attempts+1,locked_until=?,updated_at=? WHERE id=(SELECT id FROM jobs WHERE " + " AND ".join(clauses) + " ORDER BY run_at ASC LIMIT 1) RETURNING *", [now_ts + 120, now_ts, *params]).fetchone()
            if not row:
                return None
            result = dict(row)
            result["payload"] = _loads(row["payload_json"], {})
            return result

    def finish_job(self, job_id: str, result: Mapping[str, Any] | None = None, error: str | None = None) -> None:
        with self.tx() as db:
            db.execute("UPDATE jobs SET status=?,result_json=?,error=?,updated_at=?,locked_until=NULL WHERE id=?", ("failed" if error else "done", _json(dict(result or {})), error, time.time(), job_id))

    def retry_job(self, job_id: str, error: str, delay: float = 30) -> None:
        with self.tx() as db:
            db.execute("UPDATE jobs SET status='pending',run_at=?,error=?,updated_at=?,locked_until=NULL WHERE id=?", (time.time() + delay, error, time.time(), job_id))

    def jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(500, limit)),)).fetchall()
            return [{**dict(row), "payload": _loads(row["payload_json"], {}), "result": _loads(row["result_json"], {})} for row in rows]

    def ingest_event(self, source: str, event: Mapping[str, Any], session_id: str | None) -> bool:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            return False
        occurred = float(event.get("occurred_at") or 0) / 1000.0 if float(event.get("occurred_at") or 0) > 10_000_000_000 else float(event.get("occurred_at") or time.time())
        try:
            with self.tx() as db:
                db.execute("INSERT INTO event_inbox(session_id,source,event_id,event_type,occurred_at,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (session_id, source, event_id, str(event.get("event_type") or "unknown"), occurred, _json(dict(event)), time.time()))
            return True
        except sqlite3.IntegrityError:
            return False

    def pending_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM event_inbox WHERE status='pending' ORDER BY id ASC LIMIT ?", (max(1, min(200, limit)),)).fetchall()
            return [{**dict(row), "payload": _loads(row["payload_json"], {})} for row in rows]

    def mark_event(self, event_row_id: int, status: str, error: str | None = None) -> None:
        with self.tx() as db:
            db.execute("UPDATE event_inbox SET status=?,attempts=attempts+1,error=?,processed_at=? WHERE id=?", (status, error, time.time(), event_row_id))

    def cursor(self, source: str) -> dict[str, Any]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM event_cursors WHERE source=?", (source,)).fetchone()
            return dict(row) if row else {"source": source, "cursor": 0, "gap_from": None, "gap_to": None}

    def set_cursor(self, source: str, cursor: int, gap_from: int | None = None, gap_to: int | None = None) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO event_cursors(source,cursor,gap_from,gap_to,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor,gap_from=excluded.gap_from,gap_to=excluded.gap_to,updated_at=excluded.updated_at", (source, cursor, gap_from, gap_to, time.time()))

    def create_recommendation(self, session_id: str, slot_key: str, song: Mapping[str, Any], intro: str, source: str) -> dict[str, Any]:
        rec_id = str(uuid.uuid4())
        with self.tx() as db:
            cursor = db.execute(
                "INSERT INTO recommendations(id,session_id,slot_key,song_id,song_name,artist,intro,state,source,created_at,updated_at) "
                "SELECT ?,?,?,?,?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM recommendations WHERE session_id=? AND state IN " + PENDING_RECOMMENDATION_SQL + ")",
                (rec_id, session_id, slot_key, str(song.get("id")), str(song.get("name") or ""), str(song.get("artist") or ""), intro, "selecting", source, time.time(), time.time(), session_id),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("pending recommendation already exists")
        return self.recommendation(rec_id) or {}

    def recommendation(self, rec_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
            return dict(row) if row else None

    def set_recommendation(self, rec_id: str, state: str, operation_id: str | None = None, evidence: Mapping[str, Any] | None = None) -> None:
        with self.tx() as db:
            row = db.execute("SELECT evidence_json FROM recommendations WHERE id=?", (rec_id,)).fetchone()
            previous = _loads(row["evidence_json"], {}) if row else {}
            combined = {**previous, **dict(evidence or {})}
            db.execute("UPDATE recommendations SET state=?,operation_id=COALESCE(?,operation_id),evidence_json=?,updated_at=? WHERE id=?", (state, operation_id, _json(combined), time.time(), rec_id))

    def pending_recommendation(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM recommendations WHERE session_id=? AND state IN " + PENDING_RECOMMENDATION_SQL + " ORDER BY created_at DESC LIMIT 1", (session_id,)).fetchone()
            return dict(row) if row else None

    def recent_song_ids(self, minutes: int = 120) -> set[str]:
        cutoff=time.time()-minutes*60
        binding=self.binding(getattr(self,'owner_umo',''))
        with self.lock:
            rows=self.conn.execute("SELECT h.song_id FROM listening_history h JOIN listen_sessions s ON s.id=h.session_id WHERE h.occurred_at>=? AND h.state='played' AND s.owner_binding_id=? AND s.bot_music_account_id=? AND s.binding_revision=? UNION SELECT r.song_id FROM recommendations r JOIN listen_sessions s ON s.id=r.session_id WHERE r.created_at>=? AND r.state IN ('queued','queued_intro_sent','queued_intro_unknown','played','unknown') AND s.owner_binding_id=? AND s.bot_music_account_id=? AND s.binding_revision=?",(cutoff,binding['id'],binding['account_id'],binding['revision'],cutoff,binding['id'],binding['account_id'],binding['revision'])).fetchall()
            return {str(r['song_id']) for r in rows}

    def history(self, session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            if session_id:
                rows = self.conn.execute("SELECT * FROM listening_history WHERE session_id=? ORDER BY occurred_at DESC LIMIT ?", (session_id, max(1, min(200, limit)))).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM listening_history ORDER BY occurred_at DESC LIMIT ?", (max(1, min(200, limit)),)).fetchall()
            return [{**dict(row), "evidence": _loads(row["evidence_json"], {})} for row in rows]

    def record_history(self, session_id: str | None, song_id: str, song_name: str, artist: str, state: str, source: str, evidence: Mapping[str, Any] | None = None) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO listening_history(session_id,song_id,song_name,artist,state,source,occurred_at,evidence_json) VALUES(?,?,?,?,?,?,?,?)", (session_id, song_id, song_name, artist, state, source, time.time(), _json(dict(evidence or {}))))

    def record_playback_observation(self, session_id: str, song_id: str, song_name: str, artist: str, evidence: Mapping[str, Any] | None = None) -> bool:
        """Record song transitions idempotently while allowing A -> B -> A repeats."""
        with self.tx() as db:
            previous = db.execute(
                "SELECT song_id FROM listening_history WHERE session_id=? AND source='playback_observation' "
                "ORDER BY occurred_at DESC,id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if previous and str(previous["song_id"]) == str(song_id):
                return False
            db.execute(
                "INSERT INTO listening_history(session_id,song_id,song_name,artist,state,source,occurred_at,evidence_json) "
                "VALUES(?,?,?,?, 'played','playback_observation',?,?)",
                (session_id, song_id, song_name, artist, time.time(), _json(dict(evidence or {}))),
            )
            return True

    def reply_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM reply_batches WHERE id=?", (batch_id,)).fetchone()
            return {**dict(row), "input": _loads(row["input_json"], [])} if row else None

    def create_reply_batch(self, session_id: str, batch_id: str, first_at: float, messages: list[Mapping[str, Any]]) -> None:
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO reply_batches(id,session_id,first_at,last_at,state,input_json) VALUES(?,?,?,?, 'pending',?)", (batch_id, session_id, first_at, time.time(), _json(messages)))

    def set_reply_batch(self, batch_id: str, state: str, output_text: str | None = None, message_id: str | None = None, error: str | None = None) -> None:
        with self.tx() as db:
            db.execute("UPDATE reply_batches SET state=?,output_text=COALESCE(?,output_text),message_id=COALESCE(?,message_id),error=? WHERE id=?", (state, output_text, message_id, error, batch_id))

    def models_used_today(self, kind: str | None = None) -> int:
        with self.lock:
            if kind:
                row = self.conn.execute("SELECT COUNT(*) AS c FROM model_calls WHERE day=? AND kind=?", (day_key(), kind)).fetchone()
            else:
                row = self.conn.execute("SELECT COUNT(*) AS c FROM model_calls WHERE day=?", (day_key(),)).fetchone()
            return int(row["c"])

    def models_used_hour(self, kind: str = "autodj") -> int:
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) AS c FROM model_calls WHERE day=? AND hour=? AND kind=?", (day_key(), now().hour, kind)).fetchone()
            return int(row["c"])

    def reserve_recommendation_run(
        self,
        session_id: str,
        mode: str,
        *,
        hourly_limit: int | None = None,
        daily_limit: int | None = None,
        job_id: str | None = None,
    ) -> tuple[str | None, str | None]:
        current = now()
        day = day_key(current)
        hour = current.hour
        now_ts = time.time()
        with self.tx() as db:
            db.execute(
                "UPDATE recommendation_runs SET state='abandoned',finished_at=?,reason='stale_worker' "
                "WHERE session_id=? AND state='running' AND locked_until<?",
                (now_ts, session_id, now_ts),
            )
            if mode == "auto":
                hourly = int(db.execute(
                    "SELECT COUNT(*) AS c FROM recommendation_runs WHERE day=? AND hour=? AND mode='auto' "
                    "AND state NOT IN ('not_started','abandoned')",
                    (day, hour),
                ).fetchone()["c"])
                if hourly_limit is not None and hourly >= hourly_limit:
                    return None, "hourly_limit"
                daily = int(db.execute(
                    "SELECT COUNT(*) AS c FROM recommendation_runs WHERE day=? AND mode='auto' "
                    "AND state NOT IN ('not_started','abandoned')",
                    (day,),
                ).fetchone()["c"])
                if daily_limit is not None and daily >= daily_limit:
                    return None, "daily_limit"
            run_id = str(uuid.uuid4())
            try:
                db.execute(
                    "INSERT INTO recommendation_runs(id,session_id,mode,day,hour,state,job_id,created_at,locked_until) "
                    "VALUES(?,?,?,?,?,'running',?,?,?)",
                    (run_id, session_id, mode, day, hour, job_id, now_ts, now_ts + 120),
                )
            except sqlite3.IntegrityError:
                return None, "run_in_progress"
            return run_id, None

    def finish_recommendation_run(self, run_id: str, state: str, reason: str | None = None) -> None:
        with self.tx() as db:
            db.execute(
                "UPDATE recommendation_runs SET state=?,reason=?,finished_at=?,locked_until=NULL WHERE id=? AND state='running'",
                (state, str(reason)[:200] if reason else None, time.time(), run_id),
            )

    def recommendation_usage(self) -> dict[str, int]:
        current = now()
        day = day_key(current)
        with self.lock:
            daily = int(self.conn.execute(
                "SELECT COUNT(*) AS c FROM recommendation_runs WHERE day=? AND mode='auto' AND state NOT IN ('not_started','abandoned')",
                (day,),
            ).fetchone()["c"])
            hourly = int(self.conn.execute(
                "SELECT COUNT(*) AS c FROM recommendation_runs WHERE day=? AND hour=? AND mode='auto' AND state NOT IN ('not_started','abandoned')",
                (day, current.hour),
            ).fetchone()["c"])
            return {"auto_today": daily, "auto_this_hour": hourly}

    def reserve_model_call(self, kind: str, total_limit: int | None, kind_limit: int | None, hourly_limit: int | None = None) -> tuple[str | None, str | None]:
        with self.tx() as db:
            day = day_key()
            hour = now().hour
            total = int(db.execute("SELECT COUNT(*) AS c FROM model_calls WHERE day=?", (day,)).fetchone()["c"])
            typed = int(db.execute("SELECT COUNT(*) AS c FROM model_calls WHERE day=? AND kind=?", (day, kind)).fetchone()["c"])
            hourly = int(db.execute("SELECT COUNT(*) AS c FROM model_calls WHERE day=? AND hour=? AND kind=?", (day, hour, kind)).fetchone()["c"])
            if total_limit is not None and total >= total_limit:
                return None, "daily_budget_exhausted"
            if kind_limit is not None and typed >= kind_limit:
                return None, "kind_budget_exhausted"
            if hourly_limit is not None and hourly >= hourly_limit:
                return None, "hourly_limit"
            call_id = str(uuid.uuid4())
            db.execute("INSERT INTO model_calls(id,day,hour,kind,status,created_at) VALUES(?,?,?,?, 'reserved',?)", (call_id, day, hour, kind, time.time()))
            return call_id, None

    def finish_model_call(self, call_id: str, status: str, latency_ms: int | None = None, error: str | None = None) -> None:
        with self.tx() as db:
            db.execute("UPDATE model_calls SET status=?,finished_at=?,latency_ms=?,error=? WHERE id=?", (status, time.time(), latency_ms, error, call_id))

    def record_operation(
        self,
        operation_id: str,
        session_id: str | None,
        action: str,
        origin_kind: str,
        request_id: str,
        state: str,
        *,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        result_json = _json(dict(result)) if result is not None else None
        with self.tx() as db:
            db.execute(
                "INSERT INTO operations(operation_id,session_id,action,origin_kind,request_id,state,result_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET "
                "state=excluded.state,result_json=COALESCE(excluded.result_json,result_json),updated_at=excluded.updated_at WHERE operations.state<>'abandoned'",
                (operation_id, session_id, action, origin_kind, request_id, state, result_json, time.time(), time.time()),
            )

    def has_unresolved_operations(self) -> bool:
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) AS c FROM operations WHERE state IN ('accepted','running','unknown')").fetchone()
            return bool(row and int(row["c"]) > 0)

    def has_unresolved_music_operations(self, session_id: str) -> bool:
        actions = sorted(MUSIC_CONTROL_ACTIONS)
        placeholders = ",".join("?" for _ in actions)
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM operations WHERE session_id=? AND state IN ('accepted','running','unknown') "
                f"AND action IN ({placeholders})",
                (session_id, *actions),
            ).fetchone()
            return bool(row and int(row["c"]) > 0)

    def operations(self, limit: int = 50, *, session_id: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            if session_id:
                rows = self.conn.execute(
                    "SELECT * FROM operations WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                    (session_id, max(1, min(200, limit))),
                ).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM operations ORDER BY created_at DESC LIMIT ?", (max(1, min(200, limit)),)).fetchall()
            return [{**dict(row), "result": _loads(row["result_json"], {})} for row in rows]

    def song_identity(self, song_id: str | None) -> tuple[str, str]:
        value = str(song_id or "").strip()
        if not value:
            return "", ""
        with self.lock:
            row = self.conn.execute(
                "SELECT song_name,artist FROM listening_history WHERE song_id=? ORDER BY occurred_at DESC LIMIT 1",
                (value,),
            ).fetchone()
            if not row:
                row = self.conn.execute(
                    "SELECT song_json FROM search_cache WHERE song_id=? ORDER BY created_at DESC LIMIT 1",
                    (value,),
                ).fetchone()
                if row:
                    song = _loads(row["song_json"], {})
                    return str(song.get("name") or ""), str(song.get("artist") or "")
            if not row:
                row = self.conn.execute(
                    "SELECT song_name,artist FROM recommendations WHERE song_id=? ORDER BY created_at DESC LIMIT 1",
                    (value,),
                ).fetchone()
            return (str(row["song_name"] or ""), str(row["artist"] or "")) if row else ("", "")

    def recent_operations_context(self, *, session_id: str | None, limit: int = 3) -> list[dict[str, Any]]:
        candidate_rows = self.operations(max(limit * 8, 12), session_id=session_id)
        operations = [item for item in candidate_rows if str(item.get("action") or "") in MUSIC_CONTROL_ACTIONS][:limit]
        if not operations:
            return []
        op_ids = {str(item.get("operation_id") or "") for item in operations}
        tool_context: dict[str, dict[str, Any]] = {}
        for turn in reversed(self.agent_turns(100)):
            songs_by_id: dict[str, dict[str, Any]] = {}
            for tool in turn.get("tool_calls") or []:
                if str(tool.get("name") or "") != "cove_music_search":
                    continue
                for song in ((tool.get("result") or {}).get("songs") or []):
                    if isinstance(song, Mapping) and song.get("id") is not None:
                        songs_by_id[str(song["id"])] = dict(song)
            for tool in turn.get("tool_calls") or []:
                result = tool.get("result") or {}
                operation_id = str(result.get("operation_id") or "")
                if operation_id not in op_ids:
                    continue
                args = tool.get("args") or {}
                song_id = str(args.get("song_id") or _first_song_id(result) or "")
                song = songs_by_id.get(song_id) or {}
                evidence = result.get("snapshot_evidence") or {}
                playback = ((result.get("result") or {}).get("playback") or {})
                tool_context[operation_id] = {
                    "tool": str(tool.get("name") or ""),
                    "target_song_id": song_id,
                    "target_song_name": str(song.get("name") or ""),
                    "artist": str(song.get("artist") or ""),
                    "snapshot_verified": result.get("snapshot_verified"),
                    "play_status": evidence.get("play_status") or playback.get("playStatus"),
                }
        result_items: list[dict[str, Any]] = []
        for operation in operations:
            operation_id = str(operation.get("operation_id") or "")
            enriched = tool_context.get(operation_id) or {}
            target_song_id = str(enriched.get("target_song_id") or _first_song_id(operation.get("result")) or "")
            target_song_name = str(enriched.get("target_song_name") or "")
            artist = str(enriched.get("artist") or "")
            operation_result = operation.get("result") or {}
            snapshot_evidence = operation_result.get("snapshot_evidence") or {}
            playback_result = ((operation_result.get("result") or {}).get("playback") or {})
            snapshot_verified = enriched.get("snapshot_verified")
            if snapshot_verified is None:
                snapshot_verified = operation_result.get("snapshot_verified")
            play_status = enriched.get("play_status") or snapshot_evidence.get("play_status") or playback_result.get("playStatus")
            if target_song_id and not target_song_name:
                target_song_name, fallback_artist = self.song_identity(target_song_id)
                artist = artist or fallback_artist
            result_items.append({
                "operation_id": operation_id,
                "action": str(operation.get("action") or "UNKNOWN"),
                "state": str(operation.get("state") or "unknown"),
                "origin_kind": str(operation.get("origin_kind") or "unknown"),
                "target_song_id": target_song_id or None,
                "target_song_name": target_song_name or None,
                "artist": artist or None,
                "snapshot_verified": snapshot_verified,
                "play_status": play_status or None,
                "created_at": float(operation.get("created_at") or 0),
                "updated_at": float(operation.get("updated_at") or 0),
            })
        return result_items

    def record_room_message(
        self,
        *,
        session_id: str | None,
        provider_message_id: str | None,
        sender_uid: str | None,
        sender_role: str,
        text: str,
        direction: str,
        state: str,
    ) -> str:
        message_id = str(uuid.uuid4())
        with self.tx() as db:
            try:
                db.execute(
                    "INSERT INTO room_messages(id,session_id,provider_message_id,sender_uid,sender_role,text,direction,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (message_id, session_id, provider_message_id, sender_uid, sender_role, text, direction, state, time.time()),
                )
            except sqlite3.IntegrityError:
                row = db.execute("SELECT id FROM room_messages WHERE session_id=? AND provider_message_id=?", (session_id, provider_message_id)).fetchone()
                return str(row["id"]) if row else message_id
        return message_id

    def room_messages(self, limit: int = 50, *, session_id: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            if session_id:
                rows = self.conn.execute(
                    "SELECT * FROM room_messages WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                    (session_id, max(1, min(200, limit))),
                ).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM room_messages ORDER BY created_at DESC LIMIT ?", (max(1, min(200, limit)),)).fetchall()
            return [dict(row) for row in rows]

    def reply_batches(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM reply_batches ORDER BY first_at DESC LIMIT ?", (max(1, min(200, limit)),)).fetchall()
            return [{**dict(row), "input": _loads(row["input_json"], [])} for row in rows]

    def usage_summary(self) -> dict[str, Any]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT kind,status,COUNT(*) AS count,AVG(latency_ms) AS avg_latency_ms FROM model_calls WHERE day=? GROUP BY kind,status",
                (day_key(),),
            ).fetchall()
            return {
                "day": day_key(),
                "total": self.models_used_today(),
                "autodj": self.models_used_today("autodj"),
                "room_reply": self.models_used_today("room_reply"),
                "proactive": self.models_used_today("proactive"),
                "by_kind": [dict(row) for row in rows],
            }

    def start_agent_turn(
        self,
        *,
        kind: str,
        trigger: str,
        session_id: str | None,
        input_payload: Mapping[str, Any],
        song_id: str | None = None,
        persona_id: str | None = None,
        provider_id: str | None = None,
        context_bytes: int = 0,
    ) -> dict[str, Any]:
        turn_id = str(uuid.uuid4())
        with self.tx() as db:
            db.execute(
                "INSERT INTO agent_turns(id,session_id,kind,trigger,song_id,persona_id,provider_id,state,context_bytes,input_json,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (turn_id, session_id, kind, trigger, song_id, persona_id, provider_id, "running", context_bytes, _json(dict(input_payload)), time.time()),
            )
        return self.agent_turn(turn_id) or {}

    def finish_agent_turn(
        self,
        turn_id: str,
        *,
        state: str,
        output_text: str | None = None,
        tool_calls: list[Mapping[str, Any]] | None = None,
        operation_ids: list[str] | None = None,
        error: str | None = None,
        input_payload: Mapping[str, Any] | None = None,
        provider_id: str | None = None,
    ) -> None:
        with self.tx() as db:
            db.execute(
                "UPDATE agent_turns SET state=?,output_text=COALESCE(?,output_text),tool_calls_json=?,operation_ids_json=?,error=?,input_json=COALESCE(?,input_json),provider_id=COALESCE(?,provider_id),finished_at=? WHERE id=?",
                (
                    state,
                    output_text,
                    _json(list(tool_calls or [])),
                    _json(list(operation_ids or [])),
                    error,
                    _json(dict(input_payload)) if input_payload is not None else None,
                    provider_id,
                    time.time(),
                    turn_id,
                ),
            )

    def agent_turn(self, turn_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM agent_turns WHERE id=?", (turn_id,)).fetchone()
            if not row:
                return None
            return {
                **dict(row),
                "input": _loads(row["input_json"], {}),
                "tool_calls": _loads(row["tool_calls_json"], []),
                "operation_ids": _loads(row["operation_ids_json"], []),
            }

    def agent_turns(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM agent_turns ORDER BY started_at DESC LIMIT ?", (max(1, min(200, limit)),)).fetchall()
            return [{
                **dict(row),
                "input": _loads(row["input_json"], {}),
                "tool_calls": _loads(row["tool_calls_json"], []),
                "operation_ids": _loads(row["operation_ids_json"], []),
            } for row in rows]

    def cache_search_results(
        self,
        *,
        turn_id: str,
        session_id: str | None,
        songs: list[Mapping[str, Any]],
        ttl_seconds: int = 600,
    ) -> list[str]:
        now_ts = time.time()
        expires = now_ts + max(30, ttl_seconds)
        song_ids: list[str] = []
        with self.tx() as db:
            db.execute("DELETE FROM search_cache WHERE expires_at<?", (now_ts,))
            for item in songs:
                song_id = str(item.get("id") or item.get("song_id") or "").strip()
                if not song_id.isdigit():
                    continue
                song_ids.append(song_id)
                db.execute(
                    "INSERT INTO search_cache(turn_id,session_id,song_id,song_json,created_at,expires_at) VALUES(?,?,?,?,?,?) ON CONFLICT(turn_id,song_id) DO UPDATE SET song_json=excluded.song_json,expires_at=excluded.expires_at",
                    (turn_id, session_id, song_id, _json(dict(item)), now_ts, expires),
                )
        return list(dict.fromkeys(song_ids))

    def cached_song(self, turn_id: str, song_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT song_json,expires_at FROM search_cache WHERE turn_id=? AND song_id=?",
                (turn_id, str(song_id)),
            ).fetchone()
            if not row or float(row["expires_at"]) < time.time():
                return None
            return _loads(row["song_json"], {})

    def create_candidate_batch(self, *, turn_id: str, session_id: str | None, song_ids: list[str], ttl_seconds: int = 600) -> str:
        batch_id = str(uuid.uuid4())
        now_ts = time.time()
        with self.tx() as db:
            db.execute(
                "INSERT INTO candidate_batches(id,turn_id,session_id,song_ids_json,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                (batch_id, turn_id, session_id, _json(list(dict.fromkeys(song_ids))), now_ts, now_ts + ttl_seconds),
            )
        return batch_id

    def candidate_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM candidate_batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                return None
            return {**dict(row), "song_ids": _loads(row["song_ids_json"], [])}

    def record_external_override(
        self,
        *,
        session_id: str | None,
        event_type: str,
        attribution: str,
        room_id: str | None,
        old_song_id: str | None,
        new_song_id: str | None,
        old_fingerprint: str | None,
        new_fingerprint: str | None,
        evidence: Mapping[str, Any],
        action: Mapping[str, Any],
    ) -> str:
        override_id = str(uuid.uuid4())
        with self.tx() as db:
            db.execute(
                "INSERT INTO external_overrides(id,session_id,event_type,attribution,room_id,old_song_id,new_song_id,old_fingerprint,new_fingerprint,evidence_json,action_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (override_id, session_id, event_type, attribution, room_id, old_song_id, new_song_id, old_fingerprint, new_fingerprint, _json(dict(evidence)), _json(dict(action)), time.time()),
            )
        return override_id

    def record_room_summary(
        self,
        *,
        session_id: str | None,
        song_id: str | None,
        song_name: str | None,
        summary: str,
        source_key: str | None = None,
    ) -> str | None:
        summary_id = str(uuid.uuid4())
        with self.tx() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO room_summaries(id,session_id,song_id,song_name,summary,source_key,created_at) VALUES(?,?,?,?,?,?,?)",
                (summary_id, session_id, song_id, song_name, summary, source_key, time.time()),
            )
            return summary_id if cursor.rowcount else None

    def append_room_memory_draft(
        self,
        *,
        session_id: str,
        song_id: str,
        song_name: str,
        artist: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        now_ts = time.time()
        with self.tx() as db:
            row = db.execute(
                "SELECT * FROM room_memory_drafts WHERE session_id=? AND song_id=?",
                (session_id, song_id),
            ).fetchone()
            if row:
                users = (str(row["user_text"] or "") + "\n" + user_text).strip()[-900:]
                replies = (str(row["assistant_text"] or "") + "\n" + assistant_text).strip()[-900:]
                db.execute(
                    "UPDATE room_memory_drafts SET song_name=?,artist=?,user_text=?,assistant_text=?,updated_at=? WHERE id=?",
                    (song_name, artist, users, replies, now_ts, row["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO room_memory_drafts(id,session_id,song_id,song_name,artist,user_text,assistant_text,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), session_id, song_id, song_name, artist, user_text[-900:], assistant_text[-900:], now_ts, now_ts),
                )

    def room_memory_draft(self, session_id: str, song_id: str | None = None) -> dict[str, Any] | None:
        with self.lock:
            if song_id:
                row = self.conn.execute(
                    "SELECT * FROM room_memory_drafts WHERE session_id=? AND song_id=?",
                    (session_id, song_id),
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT * FROM room_memory_drafts WHERE session_id=? ORDER BY updated_at DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
            return dict(row) if row else None

    def delete_room_memory_draft(self, draft_id: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM room_memory_drafts WHERE id=?", (draft_id,))

    def room_summaries(self, limit: int = 20, *, session_id: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            if session_id:
                rows = self.conn.execute("SELECT * FROM room_summaries WHERE session_id=? ORDER BY created_at DESC LIMIT ?", (session_id, max(1, min(200, limit)))).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM room_summaries ORDER BY created_at DESC LIMIT ?", (max(1, min(200, limit)),)).fetchall()
            return [dict(row) for row in rows]

    def external_overrides(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM external_overrides ORDER BY created_at DESC LIMIT ?", (max(1, min(200, limit)),)).fetchall()
            return [{
                **dict(row),
                "evidence": _loads(row["evidence_json"], {}),
                "action": _loads(row["action_json"], {}),
            } for row in rows]

    def pending_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM jobs WHERE status IN ('pending','running') ORDER BY run_at ASC LIMIT ?", (max(1, min(200, limit)),)).fetchall()
            return [{**dict(row), "payload": _loads(row["payload_json"], {})} for row in rows]

    def quick_check(self) -> str:
        with self.lock:
            row = self.conn.execute("PRAGMA quick_check").fetchone()
            return str(row[0]) if row else "unknown"

    def health(self) -> dict[str, Any]:
        with self.lock:
            counts = {}
            for table in ("listen_sessions", "jobs", "event_inbox", "recommendations", "recommendation_runs", "room_messages", "model_calls", "operations", "agent_turns", "search_cache", "candidate_batches", "external_overrides", "room_summaries", "room_memory_drafts"):
                counts[table] = int(self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
            db_bytes = self.path.stat().st_size if self.path.exists() else 0
            return {"schema_version": SCHEMA_VERSION, "db_bytes": db_bytes, "counts": counts, "today_model_calls": self.models_used_today(), "hour_autodj_calls": self.models_used_hour("autodj"), "recommendations": self.recommendation_usage()}

    def purge_old_data(self, retention_days: int = 30) -> dict[str, int]:
        days = max(1, min(365, int(retention_days)))
        cutoff = time.time() - days * 86400
        deleted: dict[str, int] = {}
        targets = {
            "event_inbox": ("created_at", "status!='pending'"),
            "room_messages": ("created_at", "1=1"),
            "agent_turns": ("started_at", "state!='running'"),
            "operations": ("created_at", "1=1"),
            "external_overrides": ("created_at", "1=1"),
            "room_summaries": ("created_at", "1=1"),
            "listening_history": ("occurred_at", "1=1"),
            "reply_batches": ("first_at", "1=1"),
            "model_calls": ("created_at", "1=1"),
            "audit_log": ("created_at", "1=1"),
            "jobs": ("created_at", "status IN ('done','failed','cancelled')"),
            "music_decisions": ("created_at", "1=1"),
            "music_introductions": ("updated_at", "state NOT IN ('pending','ready','sending','unknown')"),
            "music_playback_instances": ("ended_at", "ended_at IS NOT NULL"),
            "music_search_batches": ("expires_at", "1=1"),
        }
        with self.tx() as db:
            for table, (column, condition) in targets.items():
                cursor = db.execute(f"DELETE FROM {table} WHERE {column}<? AND {condition}", (cutoff,))
                deleted[table] = int(cursor.rowcount or 0)
            now_ts = time.time()
            db.execute("DELETE FROM search_cache WHERE expires_at<?", (now_ts,))
            db.execute("DELETE FROM candidate_batches WHERE expires_at<?", (now_ts,))
            db.execute("DELETE FROM settings WHERE key LIKE 'control:%' AND updated_at<?", (now_ts - 86400,))
            db.execute("DELETE FROM settings WHERE key LIKE 'proactive_song:%' AND updated_at<?", (now_ts - 86400,))
        return deleted

    def optimize_if_needed(self, minimum_free_ratio: float = 0.25) -> bool:
        with self.lock:
            page_count = int(self.conn.execute("PRAGMA page_count").fetchone()[0] or 0)
            free_pages = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)
            if page_count <= 0 or free_pages / page_count < minimum_free_ratio:
                return False
            self.conn.execute("VACUUM")
            return True

    def prune_backups(self, directory: str | Path, keep: int = 7) -> list[Path]:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        backups = sorted(root.glob("*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)
        removed: list[Path] = []
        for path in backups[max(1, keep):]:
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                continue
        return removed

    def daily_backup(self, directory: str | Path, *, keep: int = 7) -> Path:
        root = Path(directory)
        stamp = datetime.now().strftime("%Y%m%d")
        destination = root / f"daily-{stamp}.sqlite3"
        if not destination.exists():
            self.backup(destination)
        self.prune_backups(root, keep=keep)
        return destination

    def backup(self, destination: str | Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            target = sqlite3.connect(str(destination))
            try:
                self.conn.backup(target)
            finally:
                target.close()
        return destination

    def import_legacy_ledger(self, path: str | Path) -> dict[str, Any]:
        source = Path(path)
        if not source.is_file():
            return {"imported": 0, "skipped": 0, "error": "not_found"}
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        imported = 0
        skipped = 0
        for line_no, raw in enumerate(content.decode("utf-8", errors="replace").splitlines(), 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except ValueError:
                skipped += 1
                continue
            raw_song_id = str(value.get("song_id") or value.get("songId") or value.get("id") or "").strip()
            name = str(value.get("name") or value.get("song_name") or "").strip()
            artist = str(value.get("artist") or "").strip()
            if raw_song_id.isdigit():
                song_id = raw_song_id
                record_key = "netease:" + raw_song_id
            elif name:
                normalized = re.sub(r"\s+", " ", (name + "\0" + artist).strip().lower())
                song_id = "fallback:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                record_key = song_id
            else:
                skipped += 1
                continue
            timestamp = value.get("ts") or value.get("time") or value.get("occurred_at") or time.time()
            if isinstance(timestamp, str):
                try:
                    timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    timestamp = time.time()
            try:
                timestamp = float(timestamp)
            except (TypeError, ValueError):
                timestamp = time.time()
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            key = record_key
            with self.tx() as db:
                try:
                    db.execute("INSERT INTO imports(source_path,file_digest,line_no,record_key,imported_at) VALUES(?,?,?,?,?)", (str(source), digest, line_no, key, time.time()))
                except sqlite3.IntegrityError:
                    skipped += 1
                    continue
                db.execute("INSERT INTO listening_history(session_id,song_id,song_name,artist,state,source,occurred_at,evidence_json) VALUES(NULL,?,?,?, 'legacy', 'ledger_import', ?, ?)", (song_id, name, artist, timestamp, _json({"source_path": str(source), "line": line_no, "record_key": record_key, "confirmed": False})))
            imported += 1
        return {"imported": imported, "skipped": skipped, "digest": digest}
