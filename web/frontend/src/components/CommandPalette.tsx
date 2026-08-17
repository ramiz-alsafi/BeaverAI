import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";

interface Props {
  open: boolean;
  onClose: () => void;
  sendCommand: (cmdText: string) => void;
  onOpenSidebar: () => void;
}

interface Cmd {
  cmd: string;
  desc: string;
  /** For commands needing a value typed after them (e.g. "/model llama3") —
   *  picking one fills the composer's next... actually just fills nothing
   *  here, palette runs argument-less commands directly and lets the user
   *  type argument-bearing ones themselves via the input below. */
  needsArg?: boolean;
  action?: () => void;
}

function buildCommands(sendCommand: (c: string) => void, onOpenSidebar: () => void): Cmd[] {
  return [
    { cmd: "/help", desc: "show all commands" },
    { cmd: "/personas", desc: "list personas" },
    { cmd: "/models", desc: "list Ollama models" },
    { cmd: "/plugins", desc: "list active plugins" },
    { cmd: "/agents", desc: "list A2A sub-agents" },
    { cmd: "/mcp", desc: "show MCP servers" },
    { cmd: "/mcp reload", desc: "reconnect MCP servers" },
    { cmd: "/tools", desc: "list active resolved tool set" },
    { cmd: "/memory", desc: "list recent long-term memories" },
    { cmd: "/sessions", desc: "list saved sessions" },
    { cmd: "/hud", desc: "show status" },
    { cmd: "/dir", desc: "show workspace" },
    { cmd: "/scope", desc: "show target scope" },
    { cmd: "/reset", desc: "reset counters" },
    { cmd: "sidebar", desc: "open the sidebar panel", needsArg: false, action: onOpenSidebar },
  ].map((c) => (c.action ? c : { ...c, action: () => sendCommand(c.cmd) }));
}

export function CommandPalette({ open, onClose, sendCommand, onOpenSidebar }: Props) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const commands = useMemo(() => buildCommands(sendCommand, onOpenSidebar), [sendCommand, onOpenSidebar]);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter((c) => c.cmd.toLowerCase().includes(q) || c.desc.toLowerCase().includes(q));
  }, [query, commands]);

  useEffect(() => {
    if (open) {
      setQuery("");
      setSelected(0);
      // Focus after mount/animation frame so the closing keystroke of the
      // Cmd+K shortcut doesn't land in the input.
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  useEffect(() => setSelected(0), [query]);

  function run(c: Cmd) {
    c.action?.();
    onClose();
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      onClose();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelected((s) => Math.min(s + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((s) => Math.max(s - 1, 0));
    } else if (e.key === "Enter" && filtered[selected]) {
      e.preventDefault();
      run(filtered[selected]);
    }
  }

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
          className="fixed inset-0 z-[60] flex items-start justify-center bg-ink/70 pt-[12vh] backdrop-blur-sm"
        >
          <motion.div
            initial={{ opacity: 0, y: -12, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -12, scale: 0.98 }}
            transition={{ duration: 0.15, ease: "easeOut" }}
            onClick={(e) => e.stopPropagation()}
            className="w-full max-w-md overflow-hidden rounded-lg border border-goldDim/40 bg-panel shadow-2xl"
          >
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Type a command…"
              className="w-full border-b border-goldDim/20 bg-transparent px-4 py-3 text-[14px] text-cream outline-none placeholder:text-muted"
            />
            <div className="max-h-80 overflow-y-auto scroll-thin py-1">
              {filtered.length === 0 ? (
                <div className="px-4 py-6 text-center text-[12px] text-muted">no matching commands</div>
              ) : (
                filtered.map((c, i) => (
                  <button
                    key={c.cmd}
                    type="button"
                    onClick={() => run(c)}
                    onMouseEnter={() => setSelected(i)}
                    className={`flex w-full items-center justify-between gap-3 px-4 py-2 text-left text-[13px] ${
                      i === selected ? "bg-goldDim/20 text-cream" : "text-muted"
                    }`}
                  >
                    <span className="font-mono text-gold">{c.cmd}</span>
                    <span className="truncate text-[11.5px]">{c.desc}</span>
                  </button>
                ))
              )}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
