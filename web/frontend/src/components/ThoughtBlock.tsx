import { motion } from "framer-motion";
import type { ThoughtEntry } from "../types";

interface Props {
  entry: ThoughtEntry;
}

/**
 * The model's short reasoning line, extracted client-side from the raw
 * `{"thought": "...", "tool": "...", "args": {...}}` blob it streamed
 * (see useBeaverSocket's retract_pending handling) — never the JSON
 * itself. Sits between the user's message and the ToolBlock it led to,
 * styled as a quiet aside rather than another chat bubble.
 */
export function ThoughtBlock({ entry }: Props) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.18, ease: "easeOut" }}
      dir="auto"
      style={{ unicodeBidi: "plaintext" }}
      className="self-start max-w-[85%] flex items-start gap-2 rounded-md border-l-[3px] border-l-goldDim/40 bg-panel/50 px-3.5 py-1.5 text-[12.5px] leading-snug text-muted"
    >
      <span className="shrink-0 opacity-70">💭</span>
      <span className="italic">{entry.text}</span>
    </motion.div>
  );
}
