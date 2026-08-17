import { AnimatePresence, motion } from "framer-motion";
import { useState } from "react";
import type { ReasoningGroupEntry } from "../types";
import { ThoughtBlock } from "./ThoughtBlock";
import { ToolBlock } from "./ToolBlock";

interface Props {
  entry: ReasoningGroupEntry;
  /** Groups belonging to the turn currently in flight start expanded and
   *  stay that way while it runs — collapsing mid-run would hide exactly
   *  the "is it stuck" signal a user watching a long turn wants. */
  defaultExpanded: boolean;
}

export function ReasoningTrace({ entry, defaultExpanded }: Props) {
  const [expanded, setExpanded] = useState(defaultExpanded);

  const toolSteps = entry.steps.filter((s) => s.kind === "tool");
  const running = toolSteps.some((s) => s.kind === "tool" && s.ok === undefined);
  const failed = toolSteps.some((s) => s.kind === "tool" && s.ok === false);
  const dotClass = running ? "bg-amber animate-pulse" : failed ? "bg-rust" : "bg-sage";

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: "easeOut" }}
      className="self-start max-w-[85%] rounded-md border border-goldDim/25 bg-panel/40"
    >
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="flex w-full items-center gap-2 px-3.5 py-1.5 text-left text-[12px] text-muted hover:text-cream"
      >
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotClass}`} />
        <span>
          reasoning · {entry.steps.length} step{entry.steps.length === 1 ? "" : "s"}
          {toolSteps.length > 0 && ` · ${toolSteps.length} tool call${toolSteps.length === 1 ? "" : "s"}`}
        </span>
        <span className="ml-auto shrink-0 text-[10px]">{expanded ? "▲ collapse" : "▼ expand"}</span>
      </button>

      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.16, ease: "easeOut" }}
            className="overflow-hidden"
          >
            <div className="flex flex-col gap-2 border-t border-goldDim/20 px-3 py-2.5">
              {entry.steps.map((step) =>
                step.kind === "thought" ? (
                  <ThoughtBlock key={step.id} entry={step} />
                ) : (
                  <ToolBlock key={step.id} entry={step} />
                )
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
