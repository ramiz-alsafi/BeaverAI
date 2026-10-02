import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import type {
  ActiveToolsResult,
  ChatEntry,
  ClientFrame,
  CommandResult,
  ConfigState,
  ConfigUpdatedResult,
  LogEntry,
  LogWhich,
  MemoriesResult,
  PluginInfo,
  ServerFrame,
  SessionsResult,
  StatusUpdate,
} from "../types";
import { randomPhrase } from "../spinner";

function uid(): string {
  return Math.random().toString(36).slice(2, 10);
}

// [FIX-21-JS] Mirrors agent/graph.py's _lenient_json_loads fix: strict
// JSON.parse() alone silently failed on two shapes small/quantised local
// models commonly emit — single-quoted strings ({'thought': '...'}, which
// the marker list right below already anticipated but never actually
// parsed) and Python-literal True/False/None instead of true/false/null.
// Lower stakes here than the Python-side fix (this only gates whether the
// "thought" preview shows while streaming — the server already parses and
// executes the actual tool call correctly regardless of this function's
// result), but worth matching for consistency.
//
// Deliberately does NOT use eval()/Function() the way Python's
// ast.literal_eval fallback does — this runs in the browser on raw model-
// generated text, and evaluating untrusted text as code would be a real
// XSS/RCE-in-browser risk if the model's output were ever influenced by a
// prompt injection. Single-quote handling below is pure string
// manipulation (swap unescaped `'` for `"` when not already inside a
// double-quoted string) — safe, imperfect on pathological input, and
// falls through to returning null exactly like before if it still doesn't
// parse.
function lenientJsonParse(chunk: string): unknown {
  try {
    return JSON.parse(chunk);
  } catch {
    // fall through
  }

  const normalized = chunk
    .replace(/\bTrue\b/g, "true")
    .replace(/\bFalse\b/g, "false")
    .replace(/\bNone\b/g, "null");
  try {
    return JSON.parse(normalized);
  } catch {
    // fall through
  }

  let swapped = "";
  let inSingle = false;
  let inDouble = false;
  for (let i = 0; i < normalized.length; i++) {
    const c = normalized[i];
    const prev = normalized[i - 1];
    if (c === '"' && prev !== "\\" && !inSingle) {
      inDouble = !inDouble;
      swapped += c;
    } else if (c === "'" && prev !== "\\" && !inDouble) {
      inSingle = !inSingle;
      swapped += '"';
    } else {
      swapped += c;
    }
  }
  try {
    return JSON.parse(swapped);
  } catch {
    return null;
  }
}

// ── Thought extraction ──────────────────────────────────────────────────
// Mirrors agent/graph.py's _extract_json_tool_call: find a
// {"thought": "...", "tool": "...", "args": {...}} object anywhere in the
// raw streamed text (tolerant of ```json fences / leading prose), brace-
// matched so nested braces inside args don't confuse it. Returns just the
// thought string, or null if there isn't one worth showing.
function extractThought(raw: string): string | null {
  if (!raw) return null;
  const cleaned = raw.replace(/```json/g, "").replace(/```/g, "");

  let start = -1;
  // [FIX-21-JS] Added the single-quote variants — this list never
  // included them at all, so a single-quoted tool-call object's start
  // position was never even found, let alone parsed (see
  // lenientJsonParse above for the parsing half of this fix).
  for (const marker of [
    '{"thought"', '{ "thought"', "{'thought'",
    '{"tool"', '{ "tool"', "{'tool'",
  ]) {
    const i = cleaned.indexOf(marker);
    if (i !== -1 && (start === -1 || i < start)) start = i;
  }
  if (start === -1) return null;

  let depth = 0;
  // [FIX-21-JS] Track WHICH quote char opened the current string (or
  // null), not just a bool — the old bool-only version only toggled on
  // '"', so a single-quoted string value containing a literal '{' or '}'
  // could throw off brace depth counting for the single-quoted shape the
  // marker list above now recognizes.
  let inStr: '"' | "'" | null = null;
  let esc = false;
  for (let i = start; i < cleaned.length; i++) {
    const c = cleaned[i];
    if (esc) {
      esc = false;
    } else if (c === "\\" && inStr) {
      esc = true;
    } else if (inStr) {
      if (c === inStr) inStr = null;
    } else if (c === '"' || c === "'") {
      inStr = c;
    } else if (c === "{") {
      depth++;
    } else if (c === "}") {
      depth--;
      if (depth === 0) {
        const chunk = cleaned.slice(start, i + 1);
        const obj = lenientJsonParse(chunk) as { thought?: unknown } | null;
        const thought = obj && typeof obj.thought === "string" ? obj.thought.trim() : "";
        return thought || null;
      }
    }
  }
  return null;
}

// ── Log reducer ──────────────────────────────────────────────────────────
// One flat ordered array mirrors the old app's single #log div where every
// bubble/tool-block/error-block/cmd-block was just appended as a DOM child
// in arrival order.

type Patch<T> = Partial<T> | ((prev: T) => Partial<T>);

type LogAction =
  | { type: "add"; entry: LogEntry }
  | { type: "remove"; id: string }
  | { type: "update"; id: string; patch: Patch<LogEntry> }
  | { type: "appendText"; id: string; text: string }
  | { type: "reset"; entries: LogEntry[] };

function logReducer(state: LogEntry[], action: LogAction): LogEntry[] {
  switch (action.type) {
    case "add":
      return [...state, action.entry];
    case "remove":
      return state.filter((e) => e.id !== action.id);
    case "update":
      return state.map((e) => {
        if (e.id !== action.id) return e;
        const patch = typeof action.patch === "function" ? action.patch(e) : action.patch;
        return { ...e, ...patch } as LogEntry;
      });
    case "appendText":
      return state.map((e) =>
        e.id === action.id && e.kind === "chat" ? { ...e, text: e.text + action.text } : e
      );
    case "reset":
      return action.entries;
    default:
      return state;
  }
}

function defaultWsUrl(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  // Same-origin in both dev (Vite's own /ws proxy, see vite.config.ts) and
  // prod (server.py serves the built frontend from the same FastAPI app) —
  // no hardcoded host/port needed either way.
  //
  // [AUTH-1] No token here — it never travels in the URL (browser history,
  // access logs, Referer headers). It's sent as the WebSocket's first
  // application message instead; see connect()'s onopen handler below.
  return `${proto}://${location.host}/ws`;
}

const TOKEN_STORAGE_KEY = "beaver_web_token";

// [AUTH-1] Dev-only auto-connect. `import.meta.env.BEAVER_WEB_TOKEN` is
// only populated when running `npm run dev` (see vite.config.ts's envDir
// / envPrefix) — Vite statically inlines `import.meta.env.DEV` at build
// time and dead-code-eliminates whichever branch doesn't match, so this
// whole block (token included) is stripped out of `npm run build` output
// entirely. It will NOT end up in a production bundle even if
// BEAVER_WEB_TOKEN is set when you build.
//
// This exists purely so solo local dev (`python web/server.py` +
// `npm run dev`, same machine, same .env) doesn't require re-typing the
// token every time sessionStorage is cleared. Do NOT rely on this for
// anything beyond that: if you ever run the Vite dev server somewhere
// reachable by anyone else, this hands them the token for free.
function devEnvToken(): string {
  if (import.meta.env.DEV) {
    const fromEnv = (import.meta.env as Record<string, string | undefined>).BEAVER_WEB_TOKEN;
    if (fromEnv) return fromEnv;
  }
  return "";
}

function readStoredToken(): string {
  try {
    const stored = sessionStorage.getItem(TOKEN_STORAGE_KEY);
    if (stored) return stored;
  } catch {
    // private-browsing / storage-disabled — fall through to env fallback
  }
  return devEnvToken();
}

export interface AckResult {
  ok: boolean;
  name?: string;
  enabled?: boolean;
  error?: string;
}

export function useBeaverSocket(url: string = defaultWsUrl()) {
  const [connected, setConnected] = useState(false);
  const [authError, setAuthError] = useState(false);
  // [AUTH-1] Token lives in sessionStorage (cleared when the tab closes),
  // never in the URL. tokenRef mirrors it for connect()'s onopen handler,
  // which needs the latest value without connect() itself being recreated
  // on every keystroke.
  const [token, setTokenState] = useState<string>(() => readStoredToken());
  const tokenRef = useRef(token);
  useEffect(() => {
    tokenRef.current = token;
  }, [token]);
  const [log, dispatch] = useReducer(logReducer, [] as LogEntry[]);
  const [turnInProgress, setTurnInProgress] = useState(false);
  const [tokenUsage, setTokenUsage] = useState<{ count: number; limit: number }>({ count: 0, limit: 0 });
  const [status, setStatus] = useState<StatusUpdate | null>(null);
  const statusRef = useRef<StatusUpdate | null>(null);
  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  // [LOOP-1] Live "how deep into this turn are we" counters — approximated
  // client-side from events the server already sends (one gen_start per
  // agent-graph iteration, one tool_start per tool call) rather than a new
  // wire field, since the graph's real iteration count isn't otherwise
  // exposed. Reset at the start of every turn in send() below.
  const [turnIteration, setTurnIteration] = useState(0);
  const [turnToolCount, setTurnToolCount] = useState(0);

  // [SIDEBAR-1]
  const [activeTools, setActiveTools] = useState<ActiveToolsResult | null>(null);
  const [memories, setMemories] = useState<MemoriesResult | null>(null);
  const [sidebarSessions, setSidebarSessions] = useState<SessionsResult | null>(null);

  // [LOGS-1] Activity panel raw log tabs — agent.log / beaver_traces.log /
  // uvicorn.log. tail_log seeds each array with the backlog; watch_log's
  // log_line frames append to it. Capped client-side so a long-running tab
  // watching a chatty file doesn't grow this unboundedly — the file itself
  // is the source of truth, this is just what's rendered.
  const [agentLog, setAgentLog] = useState<string[]>([]);
  const [tracesLog, setTracesLog] = useState<string[]>([]);
  const [uvicornLog, setUvicornLog] = useState<string[]>([]);
  const MAX_LOG_LINES = 500;
  const logSetterFor = useCallback(
    (which: LogWhich) => (which === "agent" ? setAgentLog : which === "traces" ? setTracesLog : setUvicornLog),
    []
  );

  // [CFG-1] settings drawer state
  const [config, setConfig] = useState<ConfigState | null>(null);
  const [configUpdateResult, setConfigUpdateResult] = useState<ConfigUpdatedResult | null>(null);

  // [PERSONA-1]
  const [personas, setPersonas] = useState<string[]>([]);
  const [personaAddResult, setPersonaAddResult] = useState<AckResult | null>(null);

  // [SKILLS-1]
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [pluginToggleResult, setPluginToggleResult] = useState<AckResult | null>(null);
  const [toolManuals, setToolManuals] = useState<string[]>([]);
  const [manualAddResult, setManualAddResult] = useState<AckResult | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<number | null>(null);
  // Exponential backoff (2s -> 4s -> 8s ... capped at 30s) instead of a fixed
  // 2s retry forever — a real outage shouldn't mean hammering the backend
  // every 2 seconds indefinitely. Reset to 0 on every successful connect.
  const reconnectAttemptRef = useRef(0);

  // [SPIN-1]/[SPIN-2]/[STOP-1] — mirrors the old app's module-level vars,
  // scoped as refs since they must survive across renders without causing
  // re-renders themselves.
  const pendingIdRef = useRef<string | null>(null);
  const firstTokenReceivedRef = useRef(false);
  // Raw text accumulated for the CURRENT pending bubble, kept alongside the
  // rendered copy so retract_pending can pull the "thought" out of it
  // before the bubble itself is thrown away — see extractThought() above.
  const pendingRawTextRef = useRef("");
  const awaitingFirstGenRef = useRef(false);
  const expectingStaleStopRef = useRef(false);
  const runningToolIdsRef = useRef<Set<string>>(new Set());
  const toolQueueRef = useRef<Record<string, string[]>>({});

  const beginPendingBubble = useCallback(() => {
    const id = uid();
    pendingIdRef.current = id;
    firstTokenReceivedRef.current = false;
    pendingRawTextRef.current = "";
    const entry: ChatEntry = {
      id,
      kind: "chat",
      role: "beaver",
      text: "",
      pending: true,
      spinnerLabel: randomPhrase(),
      persona: statusRef.current?.persona,
      model: statusRef.current?.model,
    };
    dispatch({ type: "add", entry });
  }, []);

  const finalizeRunningToolsAsStopped = useCallback(() => {
    for (const id of runningToolIdsRef.current) {
      dispatch({
        type: "update",
        id,
        patch: (prev) => (prev.kind === "tool" ? { ok: false, stopped: true, output: prev.output ?? "" } : {}),
      });
    }
    runningToolIdsRef.current.clear();
    toolQueueRef.current = {};
  }, []);

  const handleFrame = useCallback(
    (frame: ServerFrame) => {
      switch (frame.type) {
        case "gen_start": {
          setTurnInProgress(true);
          setTurnIteration((n) => n + 1);
          if (awaitingFirstGenRef.current && pendingIdRef.current) {
            // Already showing the optimistic bubble from beginPendingBubble()
            // (SPIN-2) — this just confirms it, don't duplicate.
            awaitingFirstGenRef.current = false;
          } else {
            // A later generation within the same turn (e.g. after a tool
            // call) — no optimistic bubble exists yet for this one.
            beginPendingBubble();
          }
          break;
        }

        case "token": {
          const id = pendingIdRef.current;
          if (!id) break;
          if (!firstTokenReceivedRef.current) {
            firstTokenReceivedRef.current = true;
            dispatch({ type: "update", id, patch: { text: "", spinnerLabel: undefined } });
          }
          pendingRawTextRef.current += frame.text;
          dispatch({ type: "appendText", id, text: frame.text });
          break;
        }

        case "retract_pending": {
          // This generation turned out to be a tool call, not narration —
          // the raw {"thought": ..., "tool": ..., "args": {...}} blob the
          // user briefly saw streaming in should never stay on screen as
          // JSON. But the "thought" inside it is worth keeping: pull it
          // out and swap the bubble for a small standalone thought block
          // instead of just deleting everything. The upcoming tool_start
          // frame renders its own ToolBlock right after, so the two end up
          // stacked: thought, then execution.
          const id = pendingIdRef.current;
          if (id) {
            const thought = extractThought(pendingRawTextRef.current);
            dispatch({ type: "remove", id });
            if (thought) {
              dispatch({ type: "add", entry: { id: uid(), kind: "thought", text: thought } });
            }
          }
          pendingIdRef.current = null;
          pendingRawTextRef.current = "";
          break;
        }

        case "gen_end": {
          const id = pendingIdRef.current;
          if (id) dispatch({ type: "update", id, patch: { pending: false } });
          pendingIdRef.current = null;
          break;
        }

        case "stopped": {
          if (expectingStaleStopRef.current) {
            // [SPIN-2] Belongs to a turn we already locally replaced when
            // the user sent a new message mid-generation — ignore.
            expectingStaleStopRef.current = false;
            break;
          }
          setTurnInProgress(false);
          const id = pendingIdRef.current;
          if (id) {
            if (!firstTokenReceivedRef.current) {
              dispatch({ type: "remove", id }); // never showed real content
            } else {
              dispatch({ type: "update", id, patch: { pending: false, stopped: true } });
            }
            pendingIdRef.current = null;
          }
          finalizeRunningToolsAsStopped();
          break;
        }

        case "tool_start": {
          const id = uid();
          setTurnToolCount((n) => n + 1);
          runningToolIdsRef.current.add(id);
          const queue = toolQueueRef.current[frame.name] ?? (toolQueueRef.current[frame.name] = []);
          queue.push(id);
          dispatch({
            type: "add",
            entry: {
              id,
              kind: "tool",
              name: frame.name,
              args: frame.args,
              spinnerLabel: `${frame.name}${frame.args ? "(" + frame.args + ")" : ""} …`,
            },
          });
          break;
        }

        case "tool_end": {
          const queue = toolQueueRef.current[frame.name];
          const id = queue && queue.length ? queue.shift() : undefined;
          if (id) {
            runningToolIdsRef.current.delete(id);
            dispatch({
              type: "update",
              id,
              patch: { ok: frame.ok, output: frame.output, spinnerLabel: undefined },
            });
          } else {
            // No matching tool_start seen (shouldn't normally happen) —
            // still surface the result rather than silently dropping it.
            dispatch({
              type: "add",
              entry: { id: uid(), kind: "tool", name: frame.name, args: "", ok: frame.ok, output: frame.output },
            });
          }
          break;
        }

        case "tokens_used":
          setTokenUsage({ count: frame.count, limit: frame.limit });
          break;

        case "error":
          dispatch({ type: "add", entry: { id: uid(), kind: "error", message: frame.message } });
          setTurnInProgress(false);
          break;

        case "done":
          setTurnInProgress(false);
          break;

        case "command_result": {
          const { type: _t, switch_to_thread, replay_messages, ...rest } = frame;
          if (switch_to_thread && Array.isArray(replay_messages)) {
            const replayed: LogEntry[] = replay_messages.map((m) => ({
              id: uid(),
              kind: "chat",
              role: m.role === "user" ? "user" : "beaver",
              text: m.content,
              pending: false,
            }));
            dispatch({ type: "reset", entries: replayed });
          }
          dispatch({ type: "add", entry: { id: uid(), kind: "command", result: rest as CommandResult } });
          break;
        }

        case "config_state":
          setConfig({ live: frame.live, restart: frame.restart });
          break;

        case "config_updated":
          setConfigUpdateResult({
            applied: frame.applied,
            queued_for_restart: frame.queued_for_restart,
            errors: frame.errors,
          });
          break;

        case "status_update":
          setStatus({
            model: frame.model,
            persona: frame.persona,
            temperature: frame.temperature,
            context_limit: frame.context_limit,
            token_count: frame.token_count,
            msg_count: frame.msg_count,
            thread_id: frame.thread_id,
            plugin_count: frame.plugin_count,
            agent_count: frame.agent_count,
            uptime_seconds: frame.uptime_seconds,
            max_loops: frame.max_loops,
            scope: frame.scope,
          });
          break;

        case "active_tools_list": {
          const { type: _t, ...rest } = frame;
          setActiveTools(rest as ActiveToolsResult);
          break;
        }

        case "memories_list": {
          const { type: _t, ...rest } = frame;
          setMemories(rest as MemoriesResult);
          break;
        }

        case "sessions_list": {
          const { type: _t, ...rest } = frame;
          setSidebarSessions(rest as SessionsResult);
          break;
        }

        // [LOGS-1] Backlog on open/tab-switch — replaces whatever was there.
        case "log_tail": {
          const lines = frame.kind === "ok" ? frame.lines ?? [] : [];
          logSetterFor(frame.which)(lines);
          break;
        }

        // [LOGS-1] One new line from a live watch_log follower — append
        // and cap so this can't grow forever on a long session.
        case "log_line": {
          logSetterFor(frame.which)((prev) => {
            const next = [...prev, frame.line];
            return next.length > MAX_LOG_LINES ? next.slice(next.length - MAX_LOG_LINES) : next;
          });
          break;
        }

        case "personas_list":
          setPersonas(frame.personas);
          break;

        case "persona_added":
          setPersonaAddResult({ ok: frame.ok, name: frame.name, error: frame.error });
          break;

        case "plugins_list":
          setPlugins(frame.plugins);
          break;

        case "plugin_toggled":
          setPluginToggleResult({ ok: frame.ok, name: frame.name, enabled: frame.enabled, error: frame.error });
          break;

        case "tool_manuals_list":
          setToolManuals(frame.manuals);
          break;

        case "tool_manual_added":
          setManualAddResult({ ok: frame.ok, name: frame.name, error: frame.error });
          break;

        default:
          break;
      }
    },
    [beginPendingBubble, finalizeRunningToolsAsStopped, logSetterFor]
  );

  const connect = useCallback(() => {
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      // [AUTH-1] Always sent first, whether or not a token is actually
      // required server-side — server.py accepts an empty/absent token
      // when BEAVER_WEB_TOKEN isn't configured, so this one line covers
      // both the local no-auth default and the token-protected case.
      ws.send(JSON.stringify({ type: "auth", token: tokenRef.current }));
      setConnected(true);
      reconnectAttemptRef.current = 0; // reset backoff on a real successful connect
    };

    ws.onclose = (event) => {
      setConnected(false);
      wsRef.current = null;
      // [AUTH-1] server.py closes with 4401 when BEAVER_WEB_TOKEN is set and
      // the connection didn't present a valid one. Retrying won't help — the
      // token isn't going to become valid on its own — so stop hammering
      // the server and surface it instead of showing "reconnecting…" forever.
      if (event.code === 4401) {
        setAuthError(true);
        return;
      }
      const attempt = reconnectAttemptRef.current;
      const delay = Math.min(2000 * 2 ** attempt, 30000); // 2s, 4s, 8s, 16s, capped at 30s
      reconnectAttemptRef.current = attempt + 1;
      reconnectTimer.current = window.setTimeout(connect, delay);
    };

    ws.onerror = () => ws.close();

    ws.onmessage = (event) => {
      let frame: ServerFrame;
      try {
        frame = JSON.parse(event.data) as ServerFrame;
      } catch {
        // A malformed frame shouldn't silently vanish — surface it instead
        // of throwing inside the handler with nothing visible to the user.
        dispatch({
          type: "add",
          entry: { id: uid(), kind: "error", message: "Received a malformed message from the server." },
        });
        return;
      }
      handleFrame(frame);
    };
  }, [url, handleFrame]);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimer.current) window.clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const isOpen = useCallback(() => wsRef.current?.readyState === WebSocket.OPEN, []);

  // [AUTH-1] Called from the "enter token" prompt shown when authError is
  // true. At that point wsRef.current is already null — server.py's 4401
  // close handler above doesn't schedule a reconnect — so there's nothing
  // to tear down first, just store the new token and try again with it.
  const setToken = useCallback(
    (value: string) => {
      try {
        sessionStorage.setItem(TOKEN_STORAGE_KEY, value);
      } catch {
        // private-browsing / storage-disabled — token still works for this
        // connection via tokenRef, just won't survive a reload.
      }
      setTokenState(value);
      setAuthError(false);
      reconnectAttemptRef.current = 0;
      connect();
    },
    [connect]
  );

  const sendFrame = useCallback(
    (frame: ClientFrame) => {
      if (!isOpen()) return;
      wsRef.current!.send(JSON.stringify(frame));
    },
    [isOpen]
  );

  /** Composer submit — types a message OR a slash command, shows a "user"
   * bubble either way, and (only for non-slash input) drives the SPIN-2
   * optimistic-pending / STOP-1 implicit-stop dance. */
  const send = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || !isOpen()) return;

      dispatch({ type: "add", entry: { id: uid(), kind: "chat", role: "user", text: trimmed, pending: false } });

      if (!trimmed.startsWith("/")) {
        if (turnInProgress) {
          // [STOP-1] A new prompt sent mid-turn implicitly stops the old
          // one. Clean up its bubble/tool blocks locally now rather than
          // waiting for the server's own "stopped" event — that event is
          // still coming for the OLD turn and must be ignored (STOP-1/
          // SPIN-2), since by the time it arrives pendingIdRef will point
          // at the NEW turn's bubble.
          const pid = pendingIdRef.current;
          if (pid) {
            if (!firstTokenReceivedRef.current) {
              dispatch({ type: "remove", id: pid });
            } else {
              dispatch({ type: "update", id: pid, patch: { pending: false, stopped: true } });
            }
          }
          finalizeRunningToolsAsStopped();
          expectingStaleStopRef.current = true;
        }
        setTurnInProgress(true);
        setTurnIteration(0);
        setTurnToolCount(0);
        awaitingFirstGenRef.current = true;
        beginPendingBubble();
      }

      sendFrame({ type: "user_message", content: trimmed });
    },
    [isOpen, turnInProgress, beginPendingBubble, finalizeRunningToolsAsStopped, sendFrame]
  );

  /** Programmatic command dispatch (e.g. the "resume"/"delete" buttons on
   * a /sessions result) — same wire message as `send`, but doesn't add a
   * user bubble to the log, matching the old app's `sendCommand`. */
  const sendCommand = useCallback(
    (cmdText: string) => {
      sendFrame({ type: "user_message", content: cmdText });
    },
    [sendFrame]
  );

  const stop = useCallback(() => sendFrame({ type: "stop" }), [sendFrame]);

  const getConfig = useCallback(() => sendFrame({ type: "get_config" }), [sendFrame]);
  const updateConfig = useCallback(
    (changes: Record<string, unknown>) => sendFrame({ type: "update_config", changes }),
    [sendFrame]
  );
  const listPersonas = useCallback(() => sendFrame({ type: "list_personas" }), [sendFrame]);
  const addPersona = useCallback(
    (name: string, content = "") => sendFrame({ type: "add_persona", name, content }),
    [sendFrame]
  );
  const listPlugins = useCallback(() => sendFrame({ type: "list_plugins" }), [sendFrame]);
  const togglePlugin = useCallback(
    (name: string, enabled: boolean) => sendFrame({ type: "toggle_plugin", name, enabled }),
    [sendFrame]
  );
  const listToolManuals = useCallback(() => sendFrame({ type: "list_tool_manuals" }), [sendFrame]);
  const addToolManual = useCallback(
    (name: string, content: string) => sendFrame({ type: "add_tool_manual", name, content }),
    [sendFrame]
  );

  // [SIDEBAR-1]
  const listActiveTools = useCallback(() => sendFrame({ type: "list_active_tools" }), [sendFrame]);
  const listMemories = useCallback(
    (limit?: number, category?: string) => sendFrame({ type: "list_memories", limit, category }),
    [sendFrame]
  );
  const listSidebarSessions = useCallback(() => sendFrame({ type: "list_sessions" }), [sendFrame]);

  // [LOGS-1]
  const tailLog = useCallback((which: LogWhich) => sendFrame({ type: "tail_log", which }), [sendFrame]);
  const watchLog = useCallback((which: LogWhich) => sendFrame({ type: "watch_log", which }), [sendFrame]);
  const unwatchLog = useCallback((which: LogWhich) => sendFrame({ type: "unwatch_log", which }), [sendFrame]);

  return {
    connected,
    authError,
    token,
    setToken,
    log,
    turnInProgress,
    tokenUsage,
    status,
    send,
    sendCommand,
    stop,
    // settings drawer
    config,
    configUpdateResult,
    getConfig,
    updateConfig,
    // personas
    personas,
    personaAddResult,
    listPersonas,
    addPersona,
    // plugins
    plugins,
    pluginToggleResult,
    listPlugins,
    togglePlugin,
    // tool manuals
    toolManuals,
    manualAddResult,
    listToolManuals,
    addToolManual,
    // [LOOP-1] live per-turn counters
    turnIteration,
    turnToolCount,
    // [SIDEBAR-1]
    activeTools,
    memories,
    sidebarSessions,
    listActiveTools,
    listMemories,
    listSidebarSessions,
    // [LOGS-1]
    agentLog,
    tracesLog,
    uvicornLog,
    tailLog,
    watchLog,
    unwatchLog,
  };
}
