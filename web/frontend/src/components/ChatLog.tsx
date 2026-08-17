import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import type { LogEntry, ReasoningGroupEntry } from "../types";
import { MessageBubble } from "./MessageBubble";
import { ThoughtBlock } from "./ThoughtBlock";
import { ToolBlock } from "./ToolBlock";
import { ReasoningTrace } from "./ReasoningTrace";
import { ErrorBlock } from "./ErrorBlock";
import { CommandResultBlock } from "./CommandResultBlock";

interface Props {
  log: LogEntry[];
  sendCommand: (cmdText: string) => void;
}

type RenderItem = LogEntry | ReasoningGroupEntry;

/** Collapses consecutive thought/tool entries (a turn's reasoning between
 *  two chat messages) into one ReasoningGroupEntry so long tool-call chains
 *  don't read as an endless vertical wall. Single, standalone steps are
 *  left ungrouped — nothing to collapse there. */
function groupReasoningSteps(log: LogEntry[]): RenderItem[] {
  const out: RenderItem[] = [];
  let run: (LogEntry & { kind: "thought" | "tool" })[] = [];

  const flush = () => {
    if (run.length === 0) return;
    if (run.length === 1) {
      out.push(run[0]);
    } else {
      out.push({ id: `group-${run[0].id}`, kind: "reasoning_group", steps: run });
    }
    run = [];
  };

  for (const entry of log) {
    if (entry.kind === "thought" || entry.kind === "tool") {
      run.push(entry);
    } else {
      flush();
      out.push(entry);
    }
  }
  flush();
  return out;
}

// How close to the bottom (px) counts as "still following along" — inside
// this, new content auto-scrolls; outside it, the user has intentionally
// scrolled up to read something, so we leave them alone instead of
// yanking them back down on every streamed token.
const STICK_THRESHOLD = 80;

export function ChatLog({ log, sendCommand }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const [showJump, setShowJump] = useState(false);
  const grouped = useMemo(() => groupReasoningSteps(log), [log]);

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD;
    stickToBottomRef.current = atBottom;
    setShowJump(!atBottom);
  }

  useEffect(() => {
    if (stickToBottomRef.current) {
      // Instant, not smooth — this effect re-fires on every streamed token
      // (each token produces a new `log` array reference), so an animated
      // scroll here restarts mid-flight dozens of times a second and looks
      // jittery instead of smooth. "smooth" is reserved for the explicit
      // jump-to-latest click below, where it's a single deliberate jump.
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "auto" });
    } else {
      setShowJump(true);
    }
  }, [log]);

  function jumpToBottom() {
    stickToBottomRef.current = true;
    setShowJump(false);
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }

  return (
    <div className="relative flex-1 overflow-hidden">
      <div ref={scrollRef} onScroll={handleScroll} className="h-full overflow-y-auto scroll-thin px-4 py-6">
        <div className="mx-auto flex max-w-3xl flex-col gap-3">
          <AnimatePresence initial={false}>
            {grouped.map((entry) => {
              switch (entry.kind) {
                case "chat":
                  return <MessageBubble key={entry.id} entry={entry} />;
                case "thought":
                  return <ThoughtBlock key={entry.id} entry={entry} />;
                case "tool":
                  return <ToolBlock key={entry.id} entry={entry} />;
                case "reasoning_group":
                  return <ReasoningTrace key={entry.id} entry={entry} defaultExpanded />;
                case "error":
                  return <ErrorBlock key={entry.id} entry={entry} />;
                case "command":
                  return <CommandResultBlock key={entry.id} entry={entry} sendCommand={sendCommand} />;
                default:
                  return null;
              }
            })}
          </AnimatePresence>
        </div>
      </div>

      <AnimatePresence>
        {showJump && (
          <motion.button
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            transition={{ duration: 0.15 }}
            onClick={jumpToBottom}
            className="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-full border border-goldDim/50 bg-panel/95 px-4 py-1.5 text-xs font-medium text-cream shadow-lg backdrop-blur hover:border-gold"
          >
            ↓ jump to latest
          </motion.button>
        )}
      </AnimatePresence>
    </div>
  );
}