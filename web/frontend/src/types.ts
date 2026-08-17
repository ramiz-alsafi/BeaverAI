// Mirrors the WS protocol documented at the top of web/server.py.
// Keep this in sync by hand — single source of truth on the frontend side
// for what the backend can send/receive.

export type Role = "user" | "beaver";

// ── Log entries (one flat ordered stream, same as old #log div's children) ─

interface BaseEntry {
  id: string;
}

export interface ChatEntry extends BaseEntry {
  kind: "chat";
  role: Role;
  text: string;
  /** true while still streaming/spinning — spinner shows until first token */
  pending: boolean;
  /** chosen once when the bubble starts spinning; shown until first token arrives */
  spinnerLabel?: string;
  /** appended after the fact if the turn was cancelled mid-stream */
  stopped?: boolean;
  /** snapshot of the active persona/model when this beaver message was
   *  generated — for the small tag under the bubble. Undefined for user
   *  messages and for anything generated before status_update ever fired. */
  persona?: string;
  model?: string;
}

export interface ThoughtEntry extends BaseEntry {
  kind: "thought";
  /** the model's short "thought" field, already extracted from its raw
   *  JSON tool-call — plain text, never the surrounding JSON blob. */
  text: string;
}

export interface ToolEntry extends BaseEntry {
  kind: "tool";
  name: string;
  args: string;
  spinnerLabel?: string;
  /** undefined while the tool is still running */
  ok?: boolean;
  output?: string;
  stopped?: boolean;
}

/** A run of consecutive thought/tool entries between two chat messages,
 *  built client-side in ChatLog for the collapsible reasoning trace — not
 *  part of the wire protocol, never dispatched into `log` directly. */
export interface ReasoningGroupEntry extends BaseEntry {
  kind: "reasoning_group";
  steps: (ThoughtEntry | ToolEntry)[];
}

export interface ErrorEntry extends BaseEntry {
  kind: "error";
  message: string;
}

export interface CommandEntry extends BaseEntry {
  kind: "command";
  result: CommandResult;
}

export type LogEntry = ChatEntry | ThoughtEntry | ToolEntry | ErrorEntry | CommandEntry;

// ── Slash-command results (web/commands.py) ─────────────────────────────

export interface CommandResultTable {
  kind: "table";
  title?: string;
  columns?: string[];
  rows?: string[][];
  footer?: string;
}

export interface SessionRow {
  thread_id: string;
  type: string;
  checkpoints: number;
  current?: boolean;
}

export interface CommandResultSessions {
  kind: "sessions";
  rows?: SessionRow[];
  footer?: string;
}

export interface CommandResultHud {
  kind: "hud";
  model?: string;
  persona?: string;
  temperature?: number;
  msg_count?: number;
  token_count?: number;
  context_limit?: number;
  tool_count?: number;
  plugin_count?: number;
  agent_count?: number;
  thread_id?: string;
}

export interface CommandResultInfo {
  kind: "info";
  title?: string;
  lines?: string[];
}

export interface CommandResultError {
  kind: "error";
  message: string;
}

export type CommandResult =
  | CommandResultTable
  | CommandResultSessions
  | CommandResultHud
  | CommandResultInfo
  | CommandResultError;

export interface ReplayMessage {
  role: Role | string;
  content: string;
}

// ── Settings panel — [CFG-1] ─────────────────────────────────────────────

export interface LiveConfig {
  model?: string;
  temperature?: number;
  persona?: string;
  max_loops?: number;
  max_output_chars?: number;
  scope?: string[];
}

export interface RestartConfig {
  num_ctx?: number;
  num_gpu?: number;
  num_thread?: number;
  num_batch?: number;
  keep_alive?: string;
  low_vram?: boolean;
  base_url?: string;
  chroma_path?: string;
  embedding_model?: string;
}

export interface ConfigState {
  live: LiveConfig;
  restart: RestartConfig;
}

export interface ConfigUpdatedResult {
  applied: Record<string, unknown>;
  queued_for_restart: Record<string, unknown>;
  errors: Record<string, string>;
}

// ── Plugins / tool manuals — [SKILLS-1] ─────────────────────────────────

export interface PluginInfo {
  name: string;
  persona: string | string[];
  enabled?: boolean;
  tools?: string[];
  error?: string;
}

// ── Live status banner — [BANNER-1] ─────────────────────────────────────

export interface StatusUpdate {
  model: string;
  persona: string;
  temperature: number;
  context_limit: number;
  token_count: number;
  msg_count: number;
  thread_id: string;
  plugin_count: number;
  agent_count: number;
  uptime_seconds: number;
  max_loops: number;
  scope: string[] | string;
}

// ── Sidebar data feeds — [SIDEBAR-1] ─────────────────────────────────────
// Reuse the exact same result shapes as slash commands (web/commands.py
// builds these with the same functions /tools, /memory, /sessions use) —
// just delivered outside the chat-log pipeline so opening/refreshing a
// sidebar tab never drops a card into the conversation.

export type ActiveToolsResult = CommandResultTable | CommandResultError;
export type MemoriesResult = CommandResultTable | CommandResultInfo | CommandResultError;
export type SessionsResult = CommandResultSessions | CommandResultError;

// ── Activity panel log tailing — [LOGS-1] ─────────────────────────────────
// agent.log / beaver_traces.log / uvicorn.log — see web/server.py's
// LOG_FILES map. tail_log is a one-shot read of the backlog; watch_log
// starts a live follower that pushes individual log_line frames.

export type LogWhich = "agent" | "traces" | "uvicorn";

export interface LogTailResult {
  kind: "ok" | "info" | "error";
  lines?: string[];
  message?: string;
  size?: number;
}

// ── Client -> Server ─────────────────────────────────────────────────────

export type ClientFrame =
  | { type: "user_message"; content: string }
  | { type: "stop" }
  | { type: "get_config" }
  | { type: "update_config"; changes: Record<string, unknown> }
  | { type: "list_personas" }
  | { type: "add_persona"; name: string; content?: string }
  | { type: "list_plugins" }
  | { type: "toggle_plugin"; name: string; enabled: boolean }
  | { type: "list_tool_manuals" }
  | { type: "add_tool_manual"; name: string; content: string }
  | { type: "list_active_tools" }
  | { type: "list_memories"; limit?: number; category?: string }
  | { type: "list_sessions" }
  | { type: "tail_log"; which: LogWhich; lines?: number }
  | { type: "watch_log"; which: LogWhich }
  | { type: "unwatch_log"; which: LogWhich };

// ── Server -> Client ─────────────────────────────────────────────────────

export type ServerFrame =
  | { type: "gen_start" }
  | { type: "token"; text: string }
  | { type: "retract_pending" }
  | { type: "gen_end" }
  | { type: "tool_start"; name: string; args: string }
  | { type: "tool_end"; name: string; output: string; ok: boolean }
  | { type: "tokens_used"; count: number; limit: number }
  | { type: "stopped" }
  | { type: "done" }
  | { type: "error"; message: string }
  | ({
      type: "command_result";
      switch_to_thread?: string;
      replay_messages?: ReplayMessage[];
    } & CommandResult)
  | ({ type: "config_state" } & ConfigState)
  | ({ type: "config_updated" } & ConfigUpdatedResult)
  | ({ type: "status_update" } & StatusUpdate)
  | { type: "personas_list"; personas: string[] }
  | { type: "persona_added"; ok: boolean; name?: string; error?: string }
  | { type: "plugins_list"; plugins: PluginInfo[] }
  | { type: "plugin_toggled"; ok: boolean; name?: string; enabled?: boolean; error?: string }
  | { type: "tool_manuals_list"; manuals: string[] }
  | { type: "tool_manual_added"; ok: boolean; name?: string; error?: string }
  | ({ type: "active_tools_list" } & ActiveToolsResult)
  | ({ type: "memories_list" } & MemoriesResult)
  | ({ type: "sessions_list" } & SessionsResult)
  | ({ type: "log_tail"; which: LogWhich } & LogTailResult)
  | { type: "log_line"; which: LogWhich; line: string };