from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Mapping

TZ = ZoneInfo("Asia/Shanghai")


def get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    try:
        value = config.get(key, default)
    except Exception:
        value = default
    return default if value is None else value


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "开", "启用"}:
        return True
    if text in {"0", "false", "no", "off", "关", "停用"}:
        return False
    return default


def as_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def split_terms(value: Any, limit: int = 30) -> list[str]:
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    else:
        text = str(value or "").replace("；", ",").replace(";", ",").replace("、", ",")
        items = [item.strip() for item in text.split(",")]
    return list(dict.fromkeys(item for item in items if item))[:limit]


def now() -> datetime:
    return datetime.now(TZ)


def day_key(at: datetime | None = None) -> str:
    return (at or now()).astimezone(TZ).date().isoformat()


@dataclass(slots=True)
class Settings:
    token_daily_limit: int = 1_000_000
    persona_music_terms: list[str] = field(default_factory=list)
    pause_end_seconds: int = 300
    owner_absence_seconds: int = 60
    disconnect_timeout_seconds: int = 120
    meeting_timeout_seconds: int = 180
    trusted_private_origins: list[str] = field(default_factory=list)
    invitation_enabled: bool = False
    invitation_threshold: float = .75
    exploration_rate: float = .15
    same_category_limit: int = 3
    diagnostic_progress_seconds: int = 0
    enabled: bool = False
    owner_umo: str = ""
    owner_netease_uid: str = ""
    cove_base_url: str = "http://127.0.0.1:8787"
    cove_token_file: str = ""
    db_path: str = ""
    provider_id: str = ""
    fallback_provider_id: str = ""
    provider_retry_count: int = 1
    autodj_enabled: bool = False
    room_chat_enabled: bool = True
    recommendation_interval_seconds: int = 600
    first_recommendation_delay_seconds: int = 60
    dedup_minutes: int = 120
    session_max_minutes: int = 0
    pending_recommendation_limit: int = 1
    model_daily_budget: int = 200
    room_chat_daily_budget: int = 80
    autodj_daily_budget: int = 40
    proactive_daily_budget: int = 40
    reserve_daily_budget: int = 40
    autodj_hourly_limit: int = 6
    model_timeout_seconds: int = 30
    agent_timeout_seconds: int = 60
    agent_max_steps: int = 7
    agent_context_bytes: int = 32_000
    qq_history_turns: int = 6
    room_history_messages: int = 10
    chat_batch_wait_seconds: int = 2
    chat_batch_max_wait_seconds: int = 5
    room_chat_backlog_ttl_seconds: int = 300
    proactive_cooldown_seconds: int = 600
    proactive_window_delay_seconds: int = 15
    reference_playlist_ids: list[str] = field(default_factory=list)
    music_preferences: list[str] = field(default_factory=list)
    excluded_terms: list[str] = field(default_factory=list)
    reduced_motion: bool = False
    default_panel_page: str = "home"
    retention_days: int = 30
    backup_keep: int = 7
    player_stream_enabled: bool = True

    @classmethod
    def from_config(cls, config: Any) -> "Settings":
        return cls(
            token_daily_limit=as_int(get(config, 'token_daily_limit', 1_000_000),1_000_000,1000,100_000_000),
            persona_music_terms=split_terms(get(config, 'persona_music_terms', []),10),
            pause_end_seconds=as_int(get(config, "pause_end_seconds", 300),300,30,3600),
            owner_absence_seconds=as_int(get(config, "owner_absence_seconds", 60),60,15,600),
            disconnect_timeout_seconds=as_int(get(config, "disconnect_timeout_seconds", 120),120,30,600),
            meeting_timeout_seconds=as_int(get(config, "meeting_timeout_seconds", 180),180,30,600),
            trusted_private_origins=split_terms(get(config,"trusted_private_origins",[])),
            invitation_enabled=as_bool(get(config,"invitation_enabled",False),False),
            invitation_threshold=max(0.,min(1.,float(get(config,"invitation_threshold",.75)))),
            exploration_rate=max(0.,min(.4,float(get(config,"exploration_rate",.15)))),
            same_category_limit=as_int(get(config,"same_category_limit",3),3,1,10),
            diagnostic_progress_seconds=as_int(get(config,"diagnostic_progress_seconds",0),0,0,60),
            enabled=as_bool(get(config, "enabled", False)),
            owner_umo=str(get(config, "owner_umo", "")).strip(),
            owner_netease_uid=str(get(config, "owner_netease_uid", "")).strip(),
            cove_base_url=str(get(config, "cove_base_url", "http://127.0.0.1:8787")).rstrip("/"),
            cove_token_file=str(get(config, "cove_token_file", "")).strip(),
            db_path=str(get(config, "db_path", "")).strip(),
            provider_id=str(get(config, "provider_id", "")).strip(),
            fallback_provider_id=str(get(config, "fallback_provider_id", "")).strip(),
            provider_retry_count=as_int(get(config, "provider_retry_count", 1), 1, 0, 3),
            autodj_enabled=as_bool(get(config, "autodj_enabled", False), False),
            room_chat_enabled=as_bool(get(config, "room_chat_enabled", True), True),
            recommendation_interval_seconds=as_int(get(config, "recommendation_interval_seconds", 600), 600, 300, 3600),
            first_recommendation_delay_seconds=as_int(get(config, "first_recommendation_delay_seconds", 60), 60, 10, 600),
            dedup_minutes=as_int(get(config, "dedup_minutes", 120), 120, 30, 720),
            session_max_minutes=as_int(get(config, "session_max_minutes", 0), 0, 0, 720),
            pending_recommendation_limit=1,
            model_daily_budget=as_int(get(config, "model_daily_budget", 200), 200, 0, 2000),
            room_chat_daily_budget=as_int(get(config, "room_chat_daily_budget", 80), 80, 0, 1000),
            autodj_daily_budget=as_int(get(config, "autodj_daily_budget", 40), 40, 0, 500),
            proactive_daily_budget=as_int(get(config, "proactive_daily_budget", 40), 40, 0, 500),
            reserve_daily_budget=as_int(get(config, "reserve_daily_budget", 40), 40, 0, 500),
            autodj_hourly_limit=as_int(get(config, "autodj_hourly_limit", 6), 6, 0, 50),
            model_timeout_seconds=as_int(get(config, "model_timeout_seconds", 30), 30, 5, 120),
            agent_timeout_seconds=as_int(get(config, "agent_timeout_seconds", 60), 60, 20, 180),
            agent_max_steps=as_int(get(config, "agent_max_steps", 7), 7, 2, 12),
            agent_context_bytes=as_int(get(config, "agent_context_bytes", 32_000), 32_000, 12_000, 64_000),
            qq_history_turns=as_int(get(config, "qq_history_turns", 6), 6, 0, 20),
            room_history_messages=as_int(get(config, "room_history_messages", 10), 10, 0, 30),
            chat_batch_wait_seconds=as_int(get(config, "chat_batch_wait_seconds", 2), 2, 1, 5),
            chat_batch_max_wait_seconds=as_int(get(config, "chat_batch_max_wait_seconds", 5), 5, 2, 10),
            room_chat_backlog_ttl_seconds=as_int(get(config, "room_chat_backlog_ttl_seconds", 300), 300, 30, 3600),
            proactive_cooldown_seconds=as_int(get(config, "proactive_cooldown_seconds", 600), 600, 120, 3600),
            proactive_window_delay_seconds=as_int(get(config, "proactive_window_delay_seconds", 15), 15, 5, 60),
            reference_playlist_ids=[x for x in split_terms(get(config, "reference_playlist_ids", ""), 3) if x.isdigit()],
            music_preferences=split_terms(get(config, "music_preferences", "")),
            excluded_terms=split_terms(get(config, "excluded_terms", "")),
            reduced_motion=as_bool(get(config, "reduced_motion", False)),
            default_panel_page=str(get(config, "default_panel_page", "home")).strip() or "home",
            retention_days=as_int(get(config, "retention_days", 30), 30, 1, 365),
            backup_keep=as_int(get(config, "backup_keep", 7), 7, 1, 60),
            player_stream_enabled=as_bool(get(config, "player_stream_enabled", True), True),
        )

    def public(self) -> dict[str, Any]:
        return {
            'token_daily_limit': self.token_daily_limit,
            'persona_music_terms': self.persona_music_terms,
            "pause_end_seconds":self.pause_end_seconds,
            "owner_absence_seconds":self.owner_absence_seconds,
            "invitation_enabled":self.invitation_enabled,
            "exploration_rate":self.exploration_rate,
            "same_category_limit":self.same_category_limit,
            "enabled": self.enabled,
            "cove_base_url": self.cove_base_url,
            "provider_id": self.provider_id,
            "fallback_provider_id": self.fallback_provider_id,
            "provider_retry_count": self.provider_retry_count,
            "autodj_enabled": self.autodj_enabled,
            "room_chat_enabled": self.room_chat_enabled,
            "recommendation_interval_seconds": self.recommendation_interval_seconds,
            "first_recommendation_delay_seconds": self.first_recommendation_delay_seconds,
            "dedup_minutes": self.dedup_minutes,
            "session_max_minutes": self.session_max_minutes,
            "model_daily_budget": self.model_daily_budget,
            "room_chat_daily_budget": self.room_chat_daily_budget,
            "autodj_daily_budget": self.autodj_daily_budget,
            "proactive_daily_budget": self.proactive_daily_budget,
            "reserve_daily_budget": self.reserve_daily_budget,
            "agent_timeout_seconds": self.agent_timeout_seconds,
            "agent_max_steps": self.agent_max_steps,
            "agent_context_bytes": self.agent_context_bytes,
            "qq_history_turns": self.qq_history_turns,
            "room_history_messages": self.room_history_messages,
            "chat_batch_wait_seconds": self.chat_batch_wait_seconds,
            "chat_batch_max_wait_seconds": self.chat_batch_max_wait_seconds,
            "room_chat_backlog_ttl_seconds": self.room_chat_backlog_ttl_seconds,
            "proactive_cooldown_seconds": self.proactive_cooldown_seconds,
            "proactive_window_delay_seconds": self.proactive_window_delay_seconds,
            "autodj_hourly_limit": self.autodj_hourly_limit,
            "music_preferences": self.music_preferences,
            "excluded_terms": self.excluded_terms,
            "reference_playlist_ids": self.reference_playlist_ids,
            "reduced_motion": self.reduced_motion,
            "default_panel_page": self.default_panel_page,
            "retention_days": self.retention_days,
            "backup_keep": self.backup_keep,
            "player_stream_enabled": self.player_stream_enabled,
            "owner_configured": bool(self.owner_umo),
            "netease_uid_configured": bool(self.owner_netease_uid),
        }

