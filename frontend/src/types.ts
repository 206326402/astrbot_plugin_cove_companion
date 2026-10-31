export type Song = {id:string;name:string;artist:string;album?:{id?:string;name?:string};aliases?:string[];translations?:string[];duration_ms?:number;durationMs?:number;cover_url?:string;coverUrl?:string;source?:string};
export type Snapshot = {
  room_id?:string|null; room_id_hint?:string; room_epoch?:string|null; room_revision?:number; connected?:boolean; nim_connected?:boolean;
  current_song?:Song|null; playback?:{status?:string;progress_ms?:number;observed_at?:number;server_seq?:number|null};
  queue?:{song_ids?:string[];play_mode?:string|null;versions?:{user_id:string;version:number}[];fingerprint?:string|null};
  evidence?:Record<string,{source?:string;observed_at?:number;validated_at?:number|null;stale?:boolean}>;
  worker?:{phase?:string;account_id?:string|null;last_poll_at?:string|null;last_error?:string|null};
};
export type Session = {
  id:string;phase:string;room_id?:string|null;room_epoch?:string|null;autodj_enabled?:number;room_chat_enabled?:number;
  playback_state?:string;session_version?:number;expires_at?:number|null;next_recommend_at?:number|null;
  next_eligibility_check_at?:number|null;blocked_reason?:string|null;blocked_until?:number|null;decision_revision?:number;
};
export type Bootstrap = {
  enabled:boolean;session:Session|null;snapshot:Snapshot;cove:Record<string,unknown>|null;cove_error?:string|null;lease?:Record<string,unknown>|null;
  settings:Record<string,unknown>;health:Record<string,unknown>;version:string;protocol:string;
};
export type HistoryItem = {id?:number;session_id?:string;song_id:string;song_name?:string;artist?:string;state:string;source?:string;occurred_at:number;evidence?:Record<string,unknown>};
export type Operation = {operation_id:string;session_id?:string|null;action:string;origin_kind?:string;request_id?:string;state:string;result?:Record<string,unknown>;error?:string;created_at:number;updated_at:number;detail?:Record<string,unknown>};
export type Health = {plugin:{version:string;enabled:boolean;store:Record<string,unknown>;usage:Record<string,unknown>};cove:Record<string,unknown>|null;cove_error?:string|null;snapshot:Snapshot};
export type HistoryPayload = {items:HistoryItem[];messages:Record<string,unknown>[];reply_batches:Record<string,unknown>[]};
export type AgentTurn = {id:string;kind:string;trigger:string;state:string;persona_id?:string|null;provider_id?:string|null;context_bytes?:number;error?:string|null;started_at:number;finished_at?:number|null;input?:{context?:{qq_turns?:number;room_messages?:number;recent_operations?:number}};tool_calls?:Array<{name?:string;state?:string}>};
export type OperationPayload = {items:Operation[];jobs:Record<string,unknown>[];agent_turns?:AgentTurn[];external_overrides?:Record<string,unknown>[]};
export type LyricsLine = {timeMs:number;text:string};
export type LyricsPayload = {songId:string;lrc:{raw?:string;lines?:LyricsLine[]}|null;translation?:string|null;romanization?:string|null};

export type QueueItem = {id:string;name:string;artist:string;duration_ms?:number;cover_url?:string;position:number;is_current:boolean;is_next:boolean};
export type QueueState = {items:QueueItem[];song_ids:string[];play_mode?:string|null;fingerprint?:string|null;offset?:number;limit?:number;total?:number;current_position?:number|null};
export type PlayerState = {
  server_time:number;protocol:string;session:Session|null;snapshot:Snapshot;song:Song|null;
  playback:{status?:string;progress_ms?:number;observed_at?:number;server_seq?:number|null};
  queue:QueueState;
  lyric:{previous?:string;current?:string;next?:string;timeline?:LyricsLine[]}|null;
  pending_recommendation:Record<string,unknown>|null;recent_operations:Record<string,unknown>[];
  recent_agent_turns:AgentTurn[];recent_summaries:Record<string,unknown>[];
  health:{enabled:boolean;store:Record<string,unknown>;usage:Record<string,unknown>;cove_error?:string|null};
};
export type OnboardingCheck = {id:string;label:string;state:'pass'|'warn'|'fail';message:string};
export type OnboardingPayload = {ready:boolean;checks:OnboardingCheck[];generated_at:number};
