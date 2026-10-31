export const ASTRBOT_COVE_PROTOCOL = "astrbot-cove/1" as const;

export type ActorRole = "owner" | "bot" | "other" | "unknown" | "system";
export type OperationState =
  | "accepted"
  | "running"
  | "confirmed"
  | "rejected"
  | "cancelled"
  | "unknown";
export type OperationAction =
  | "CREATE"
  | "JOIN"
  | "ENQUEUE_NEXT"
  | "PLAY_NOW"
  | "PAUSE"
  | "RESUME"
  | "NEXT"
  | "LEAVE"
  | "SEND_MESSAGE"
  | "CANCEL_QUEUED_RECOMMENDATION";

export type OperationOrigin = {
  kind: "qq_command" | "room_command" | "autodj" | "room_reply" | "dashboard";
  request_id: string;
};

export type OperationPreconditions = {
  room_id?: string | null;
  room_epoch?: string | null;
  room_revision?: number | null;
  queue_fingerprint?: string | null;
};

export type OperationRequest = {
  operation_id: string;
  lease_id: string;
  fencing_token: number;
  session_id: string;
  origin: OperationOrigin;
  deadline_at?: number;
  control_generation?: number;
  binding_revision?: number;
  session_version?: number;
  preconditions?: OperationPreconditions;
  action: OperationAction;
  args?: Record<string, unknown>;
};

export type BridgeEventRecord = {
  seq: number;
  event_id: string;
  event_type: string;
  room_id: string | null;
  room_epoch: string | null;
  occurred_at: number;
  recorded_at: number;
  source: string;
  source_event_key: string | null;
  actor_role: ActorRole;
  actor_uid: string | null;
  operation_id: string | null;
  payload: Record<string, unknown>;
};

export type SourceEvidence = {
  source: string;
  observed_at: number;
  validated_at: number | null;
  stale: boolean;
};

export type ProtocolSong = {
  id: string;
  name: string;
  artist: string;
  duration_ms: number;
  cover_url?: string;
};

export type ProtocolSnapshot = {
  protocol: typeof ASTRBOT_COVE_PROTOCOL;
  bridge_instance_id: string;
  event_cursor: number;
  captured_at: number;
  room_id: string | null;
  room_epoch: string | null;
  room_revision: number;
  connected: boolean;
  nim_connected: boolean;
  bot_account: { uid: string | null; nickname?: string | null };
  owner_binding: { uid: string | null; present: boolean | null };
  members: Array<{ uid: string; role: "bot" | "owner" | "other" | "unknown"; present: boolean | null }>;
  current_song: ProtocolSong | null;
  playback: {
    status: "PLAY" | "PAUSE" | "UNKNOWN";
    progress_ms: number;
    observed_at: number;
    server_seq: number | null;
  };
  queue: {
    song_ids: string[];
    play_mode: string | null;
    versions: Array<{ user_id: string; version: number }>;
    fingerprint: string | null;
  };
  evidence: {
    room: SourceEvidence;
    members: SourceEvidence;
    queue: SourceEvidence;
    playback: SourceEvidence;
  };
  worker: {
    phase: string;
    account_id: string | null;
    last_poll_at: string | null;
    last_heartbeat_at: string | null;
    last_error: string | null;
  };
};

export type OperationView = {
  operation_id: string;
  state: OperationState;
  action: OperationAction;
  session_id: string;
  origin: OperationOrigin;
  preconditions: OperationPreconditions;
  result: Record<string, unknown> | null;
  steps: Array<{
    name: string;
    state: string;
    evidence: Record<string, unknown> | null;
    updated_at: number;
  }>;
  error: { code: string; message: string; retryable: boolean } | null;
  created_at: number;
  updated_at: number;
};

export type ErrorBody = {
  error: {
    code: string;
    message: string;
    retryable: boolean;
  };
};

export function nowMs(): number {
  return Date.now();
}

export function operationRequestHash(request: OperationRequest): string {
  return JSON.stringify({
    session_id: request.session_id,
    deadline_at: request.deadline_at,
    control_generation: request.control_generation,
    binding_revision: request.binding_revision,
    session_version: request.session_version,
    origin: request.origin,
    preconditions: request.preconditions ?? {},
    action: request.action,
    args: request.args ?? {},
  });
}
