import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import type { LogEntry } from "../types";
import { StatusRing, type RingState } from "./StatusRing";
import { PrettyJsonNode, tryParseJson } from "../lib/prettyJson";

interface Props {
  log: LogEntry[];
  agentLog: string[];
  tracesLog: string[];
  uvicornLog: string[];
  open: boolean;
  onClose: () => void;
  // [LOGS-1] tail_log/watch_log/unwatch_log — see useBeaverSocket. The
  // panel drives its own fetch/follow lifecycle the same way Sidebar
  // drives listSessions/listActiveTools/listMemories: fetch on open,
  // re-fetch on tab change, nothing on a dumb polling timer.
  tailLog: (which: "agent" | "traces" | "uvicorn") => void;
  watchLog: (which: "agent" | "traces" | "uvicorn") => void;
  unwatchLog: (which: "agent" | "traces" | "uvicorn") => void;
}

type Tab = "events" | "agent" | "traces" | "uvicorn";

const TABS: { id: Tab; label: string }[] = [
  { id: "events", label: "events" },
  { id: "agent", label: "agent.log" },
  { id: "traces", label: "traces" },
  { id: "uvicorn", label: "uvicorn" },
];

type ActivityEntry = Extract<LogEntry, { kind: "tool" | "error" }>;

function isActivityEntry(e: LogEntry): e is ActivityEntry {
  return e.kind === "tool" || e.kind === "error";
}

function stateFor(entry: Extract<LogEntry, { kind: "tool" }>): RingState {
  if (entry.stopped) return "stopped";
  if (entry.ok === undefined) return "running";
  return entry.ok ? "ok" : "error";
}

export function ActivityPanel({
  log,
  agentLog,
  tracesLog,
  uvicornLog,
  open,
  onClose,
  tailLog,
  watchLog,
  unwatchLog,
}: Props) {
  const [tab, setTab] = useState<Tab>("events");
  const scrollRef = useRef<HTMLDivElement>(null);
  const entries = useMemo(() => log.filter(isActivityEntry), [log]);

  const rawLogFor = (t: Tab) => (t === "agent" ? agentLog : t === "traces" ? tracesLog : uvicornLog);
  const activeLength = tab === "events" ? entries.length : rawLogFor(tab).length;

  useEffect(() => {
    if (!open) return;
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "auto" });
  }, [open, tab, activeLength]);

  // [LOGS-1] For a raw-log tab: pull the backlog once, then start
  // following. Only one tab's worth of following runs at a time (matches
  // "no noise" — no reason to stream three files while looking at one),
  // and it's torn down on tab switch / panel close, not just on unmount.
  useEffect(() => {
    if (!open || tab === "events") return;
    const which = tab as "agent" | "traces" | "uvicorn";
    tailLog(which);
    watchLog(which);
    return () => unwatchLog(which);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, tab]);

  return (
    <AnimatePresence>
      {open && (
        <motion.aside
          initial={{ width: 0, opacity: 0 }}
          animate={{ width: 380, opacity: 1 }}
          exit={{ width: 0, opacity: 0 }}
          transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
          className="relative z-40 flex h-full shrink-0 flex-col overflow-hidden border-l border-goldDim/20 bg-ink/95 backdrop-blur-sm"
        >
          <div className="flex w-[380px] shrink-0 flex-col border-b border-goldDim/20 bg-panel/60">
            <div className="flex items-center justify-between px-3 pt-2.5">
              <div className="flex items-center gap-2">
                <span className="relative flex h-1.5 w-1.5">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-gold/60" />
                  <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-gold" />
                </span>
                <span className="text-[11.5px] font-bold uppercase tracking-wide text-gold">activity</span>
              </div>
              <button
                type="button"
                onClick={onClose}
                className="rounded px-1.5 py-0.5 text-[13px] text-muted transition-colors hover:text-cream"
                aria-label="Close activity panel"
              >
                ✕
              </button>
            </div>

            {/* Tab strip — a sliding gold underline follows the active tab */}
            <div className="relative mt-2 flex px-3">
              {TABS.map((t) => {
                const count = t.id === "events" ? entries.length : rawLogFor(t.id).length;
                const active = tab === t.id;
                return (
                  <button
                    key={t.id}
                    type="button"
                    onClick={() => setTab(t.id)}
                    className={`relative flex items-center gap-1.5 px-2.5 pb-2 text-[11.5px] transition-colors ${
                      active ? "text-cream" : "text-muted hover:text-cream/80"
                    }`}
                  >
                    <span className="font-mono">{t.label}</span>
                    {count > 0 && (
                      <span className="rounded-full border border-goldDim/25 px-1 text-[10px] text-muted">
                        {count}
                      </span>
                    )}
                    {active && (
                      <motion.span
                        layoutId="activity-tab-underline"
                        className="absolute inset-x-2 bottom-0 h-[2px] rounded-full bg-gold shadow-[0_0_6px_rgba(217,164,65,0.6)]"
                        transition={{ type: "spring", stiffness: 500, damping: 40 }}
                      />
                    )}
                  </button>
                );
              })}
            </div>
          </div>

          <div ref={scrollRef} className="w-[380px] flex-1 overflow-y-auto scroll-thin px-3 py-3">
            {tab === "events" && <EventsTab entries={entries} />}
            {tab === "agent" && <RawLogTab lines={agentLog} emptyHint="agent.log — nothing logged yet." />}
            {tab === "traces" && <RawLogTab lines={tracesLog} emptyHint="beaver_traces.log — nothing traced yet." />}
            {tab === "uvicorn" && <RawLogTab lines={uvicornLog} emptyHint="uvicorn.log — nothing logged yet." />}
          </div>
        </motion.aside>
      )}
    </AnimatePresence>
  );
}

// ── Events tab — the parsed/pretty view of tool calls and errors ───────────

function EventsTab({ entries }: { entries: ActivityEntry[] }) {
  if (entries.length === 0) {
    return (
      <p className="mt-6 text-center text-[12px] text-muted">
        Tool calls and errors will show up here as parsed events.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-2.5">
      {entries.map((entry, i) => (
        <EventRow key={entry.id} entry={entry} index={i + 1} />
      ))}
    </div>
  );
}

function EventRow({ entry, index }: { entry: ActivityEntry; index: number }) {
  if (entry.kind === "error") {
    return (
      <motion.div
        layout
        initial={{ opacity: 0, x: 10 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ type: "spring", stiffness: 380, damping: 32 }}
        className="rounded border border-rust/40 bg-rust/5 px-2.5 py-2"
      >
        <div className="mb-1 flex items-center gap-1.5 text-[10.5px] text-rust">
          <span className="text-muted">#{index}</span>
          <span className="font-bold uppercase tracking-wide">error</span>
        </div>
        <div className="whitespace-pre-wrap break-words font-mono text-[11.5px] text-rust/90">
          {entry.message}
        </div>
      </motion.div>
    );
  }

  const args = tryParseJson(entry.args ?? "");
  const output = entry.ok !== undefined ? tryParseJson(entry.output ?? "") : null;

  return (
    <motion.div
      layout
      initial={{ opacity: 0, x: 10 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ type: "spring", stiffness: 380, damping: 32 }}
      className="rounded border border-goldDim/15 bg-panel/70 px-2.5 py-2"
    >
      <div className="mb-1.5 flex items-center gap-1.5">
        <span className="text-[10.5px] text-muted">#{index}</span>
        <StatusRing state={stateFor(entry)} size={13} />
        <span className="font-mono text-[11.5px] font-semibold text-cream">{entry.name}</span>
      </div>

      {entry.args && (
        <pre className="mb-1 overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-snug text-muted">
          {args.ok ? <PrettyJsonNode value={args.value} /> : args.raw}
        </pre>
      )}

      {entry.output !== undefined && entry.output !== "" && (
        <pre className="overflow-x-auto whitespace-pre-wrap break-words border-t border-goldDim/10 pt-1 font-mono text-[11px] leading-snug text-muted/90">
          {output?.ok ? <PrettyJsonNode value={output.value} /> : entry.output}
        </pre>
      )}
    </motion.div>
  );
}

// ── Raw log tabs — agent.log / beaver_traces.log, tailed live ──────────────

// Loose classification just for a left accent color + tint — these files
// aren't structured JSON, so this is pattern matching on the conventions
// both writers already use (logging levels; local_tracer.py's [TAG] style).
function classifyLine(line: string): { color: string; accent: string } {
  if (/\bERROR\b|\[TOOL ERROR\]|\[LLM ERROR\]|\[CHAIN ERROR\]/.test(line)) {
    return { color: "text-rust/90", accent: "border-l-rust" };
  }
  if (/\bWARNING\b/.test(line)) {
    return { color: "text-amber/90", accent: "border-l-amber" };
  }
  if (/\[TOOL START\]|\[TOOL EXECUTION\]/.test(line)) {
    return { color: "text-gold/90", accent: "border-l-gold" };
  }
  if (/\[TOOL END\]/.test(line)) {
    return { color: "text-sage/90", accent: "border-l-sage" };
  }
  if (/\[LLM START\]|\[LLM END\]|\[RUN START\]/.test(line)) {
    return { color: "text-cream/80", accent: "border-l-goldDim" };
  }
  return { color: "text-muted", accent: "border-l-goldDim/20" };
}

function RawLogTab({ lines, emptyHint }: { lines: string[]; emptyHint: string }) {
  if (lines.length === 0) {
    return <p className="mt-6 text-center text-[12px] text-muted">{emptyHint}</p>;
  }
  // Only animate the newest handful in — replaying a spring transition for
  // an 800-line backlog on every render is wasted motion, not "alive".
  const RECENT = 12;
  const cutoff = Math.max(0, lines.length - RECENT);

  return (
    <div className="flex flex-col gap-0.5 font-mono text-[11px] leading-snug">
      <AnimatePresence initial={false}>
        {lines.map((line, i) => {
          const { color, accent } = classifyLine(line);
          const isRecent = i >= cutoff;
          const row = (
            <div className={`whitespace-pre-wrap break-words border-l-2 ${accent} py-0.5 pl-2 ${color}`}>
              {line}
            </div>
          );
          return isRecent ? (
            <motion.div
              key={`${i}-${line.slice(0, 24)}`}
              initial={{ opacity: 0, x: -6 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.18 }}
            >
              {row}
            </motion.div>
          ) : (
            <div key={`${i}-${line.slice(0, 24)}`}>{row}</div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}