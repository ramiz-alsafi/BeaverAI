import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import type { ReactNode } from "react";
import type { ActiveToolsResult, MemoriesResult, SessionsResult } from "../types";

type Tab = "sessions" | "tools" | "memory";

interface Props {
  open: boolean;
  onClose: () => void;
  currentThreadId?: string;
  sendCommand: (cmdText: string) => void;
  activeTools: ActiveToolsResult | null;
  memories: MemoriesResult | null;
  sidebarSessions: SessionsResult | null;
  listActiveTools: () => void;
  listMemories: (limit?: number, category?: string) => void;
  listSidebarSessions: () => void;
}

const TABS: { id: Tab; label: string }[] = [
  { id: "sessions", label: "sessions" },
  { id: "tools", label: "tools" },
  { id: "memory", label: "memory" },
];

function Empty({ children }: { children: ReactNode }) {
  return <div className="px-3 py-6 text-center text-[12px] text-muted">{children}</div>;
}

export function Sidebar({
  open,
  onClose,
  currentThreadId,
  sendCommand,
  activeTools,
  memories,
  sidebarSessions,
  listActiveTools,
  listMemories,
  listSidebarSessions,
}: Props) {
  const [tab, setTab] = useState<Tab>("sessions");
  // [SIDEBAR-1] Memory tab filter — "all" vs "reflections". Reflections are
  // just memories stored under category="lesson" by
  // skills/memory_skill.py's reflect_and_store_lesson — no new backend
  // concept, this reuses the list_memories(category=...) param that
  // already existed for the drawer.
  const [memoryFilter, setMemoryFilter] = useState<"all" | "lesson">("all");

  // Fetch on open and whenever the tab changes — cheap read-only calls,
  // and this is the only way the panel ever gets fresh data since it's
  // deliberately not on a polling timer (same "no noise" reasoning as
  // the status banner).
  useEffect(() => {
    if (!open) return;
    if (tab === "sessions") listSidebarSessions();
    else if (tab === "tools") listActiveTools();
    else if (tab === "memory") listMemories(undefined, memoryFilter === "lesson" ? "lesson" : undefined);
  }, [open, tab, memoryFilter, listSidebarSessions, listActiveTools, listMemories]);

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
            className="fixed inset-0 z-40 bg-ink/60 backdrop-blur-[1px] sm:hidden"
          />
          <motion.aside
            initial={{ x: -280, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: -280, opacity: 0 }}
            transition={{ duration: 0.2, ease: "easeOut" }}
            className="fixed left-0 top-0 z-50 flex h-full w-72 flex-col border-r border-goldDim/25 bg-panel/95 backdrop-blur-sm"
          >
            <div className="flex items-center justify-between border-b border-goldDim/20 px-3 py-2.5">
              <span className="text-[0.85em] font-bold uppercase tracking-wide text-gold">beaver</span>
              <button
                type="button"
                onClick={onClose}
                className="rounded px-1.5 text-muted hover:text-cream"
                title="Close sidebar"
              >
                ✕
              </button>
            </div>

            <div className="flex border-b border-goldDim/20">
              {TABS.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => setTab(t.id)}
                  className={`flex-1 px-2 py-2 text-[11.5px] uppercase tracking-wide transition-colors ${
                    tab === t.id ? "border-b-2 border-gold text-gold" : "text-muted hover:text-cream"
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </div>

            <div className="flex-1 overflow-y-auto scroll-thin">
              {tab === "sessions" && (
                <div className="flex flex-col">
                  {!sidebarSessions ? (
                    <Empty>loading…</Empty>
                  ) : sidebarSessions.kind === "error" ? (
                    <Empty>{sidebarSessions.message}</Empty>
                  ) : !sidebarSessions.rows || sidebarSessions.rows.length === 0 ? (
                    <Empty>no saved sessions</Empty>
                  ) : (
                    sidebarSessions.rows.map((row) => (
                      <div
                        key={row.thread_id}
                        className={`flex items-center justify-between gap-2 border-b border-goldDim/10 px-3 py-2 text-[12px] ${
                          row.thread_id === currentThreadId ? "bg-sage/10 text-sage" : "text-cream"
                        }`}
                      >
                        <div className="min-w-0">
                          <div className="truncate">{row.thread_id}</div>
                          <div className="text-[10.5px] text-muted">
                            {row.type} · {row.checkpoints} ckpt
                          </div>
                        </div>
                        {row.thread_id !== currentThreadId && (
                          <div className="flex shrink-0 gap-1">
                            <button
                              type="button"
                              onClick={() => sendCommand(`/continue ${row.thread_id}`)}
                              className="rounded border border-goldDim px-1.5 py-0.5 text-[10.5px] hover:border-gold"
                            >
                              resume
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                // [FIX-SIDEBAR-RACE] sendCommand() just posts
                                // the /delete frame over the WebSocket — the
                                // server processes it async and replies with
                                // its own command_result later. Calling
                                // listSidebarSessions() in the very same
                                // tick re-fetches before the delete has
                                // actually happened server-side, so the
                                // just-deleted session often still showed up
                                // in the refreshed list. There's no request
                                // id to correlate a specific command_result
                                // back to this click (command_result is
                                // generic, shared by every slash command),
                                // so a short delay — not a real fix, but a
                                // proportionate one — gives the round trip
                                // time to land for the common case without
                                // a bigger protocol change.
                                sendCommand(`/delete ${row.thread_id}`);
                                window.setTimeout(listSidebarSessions, 300);
                              }}
                              className="rounded border border-goldDim px-1.5 py-0.5 text-[10.5px] hover:border-rust hover:text-rust"
                            >
                              del
                            </button>
                          </div>
                        )}
                      </div>
                    ))
                  )}
                </div>
              )}

              {tab === "tools" && (
                <div className="flex flex-col">
                  {!activeTools ? (
                    <Empty>loading…</Empty>
                  ) : activeTools.kind === "error" ? (
                    <Empty>{activeTools.message}</Empty>
                  ) : !activeTools.rows || activeTools.rows.length === 0 ? (
                    <Empty>no tools resolved for this persona</Empty>
                  ) : (
                    <>
                      {activeTools.footer && (
                        <div className="border-b border-goldDim/10 px-3 py-1.5 text-[10.5px] text-muted">
                          {activeTools.footer}
                        </div>
                      )}
                      {activeTools.rows.map((row, i) => (
                        <div key={i} className="border-b border-goldDim/10 px-3 py-2 text-[12px]">
                          <div className="flex items-center gap-1.5">
                            <span className="font-medium text-cream">{row[0]}</span>
                            <span className="rounded-full border border-goldDim/40 px-1.5 py-0 text-[9.5px] text-muted">
                              {row[1]}
                            </span>
                          </div>
                          {row[2] && <div className="mt-0.5 text-[10.5px] text-muted">{row[2]}</div>}
                        </div>
                      ))}
                    </>
                  )}
                </div>
              )}

              {tab === "memory" && (
                <div className="flex flex-col">
                  <div className="flex gap-1 border-b border-goldDim/10 px-3 py-1.5">
                    {(["all", "lesson"] as const).map((f) => (
                      <button
                        key={f}
                        type="button"
                        onClick={() => setMemoryFilter(f)}
                        className={`rounded-full border px-2 py-0.5 text-[10.5px] transition-colors ${
                          memoryFilter === f
                            ? "border-gold text-gold"
                            : "border-goldDim/25 text-muted hover:text-cream"
                        }`}
                      >
                        {f === "all" ? "all" : "reflections"}
                      </button>
                    ))}
                  </div>
                  {!memories ? (
                    <Empty>loading…</Empty>
                  ) : memories.kind === "error" ? (
                    <Empty>{memories.message}</Empty>
                  ) : memories.kind === "info" ? (
                    <Empty>{memories.lines?.[0] ?? "no memories stored yet"}</Empty>
                  ) : !memories.rows || memories.rows.length === 0 ? (
                    <Empty>no memories stored yet</Empty>
                  ) : (
                    memories.rows.map((row, i) => (
                      <div key={i} className="border-b border-goldDim/10 px-3 py-2 text-[12px]">
                        <div className="mb-1 flex items-center justify-between gap-2">
                          <span className="rounded-full border border-goldDim/40 px-1.5 py-0 text-[9.5px] text-muted">
                            {row[0]}
                          </span>
                          <button
                            type="button"
                            onClick={() => {
                              // [FIX-SIDEBAR-RACE] Same race as the session
                              // delete button above — see its comment.
                              sendCommand(`/forget ${row[2]}`);
                              window.setTimeout(() => listMemories(), 300);
                            }}
                            className="shrink-0 text-[10.5px] text-muted hover:text-rust"
                            title="Forget this memory"
                          >
                            forget
                          </button>
                        </div>
                        <div className="text-cream">{row[1]}</div>
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}
