import { motion } from "framer-motion";
import type { ToolEntry } from "../types";
import { Spinner } from "./Spinner";

interface Props {
  entry: ToolEntry;
}

export function ToolBlock({ entry }: Props) {
  const running = entry.ok === undefined;
  const borderClass = running ? "border-l-amber" : entry.ok ? "border-l-sage" : "border-l-rust";

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{
        opacity: 1,
        y: 0,
        // [ANIM-1] Soft breathing glow while actively running — the spinner
        // alone is easy to miss in peripheral vision during a longer call;
        // a pulse on the whole block is a much stronger "still working" cue.
        boxShadow: running
          ? ["0 0 0px rgba(224,165,47,0)", "0 0 14px rgba(224,165,47,0.28)", "0 0 0px rgba(224,165,47,0)"]
          : "0 0 0px rgba(224,165,47,0)",
      }}
      transition={
        running
          ? { boxShadow: { duration: 1.8, repeat: Infinity, ease: "easeInOut" }, default: { duration: 0.18, ease: "easeOut" } }
          : { duration: 0.18, ease: "easeOut" }
      }
      dir="auto"
      style={{ unicodeBidi: "plaintext" }}
      className={`self-start max-w-[85%] rounded-md border-l-[3px] ${borderClass} bg-panel px-3.5 py-2 text-[13px] text-muted whitespace-pre-wrap break-words`}
    >
      {running ? (
        <Spinner label={entry.spinnerLabel ?? entry.name} />
      ) : (
        <>
          <span>{entry.ok ? "✓" : "✗"} </span>
          <span className="font-bold text-cream">{entry.name}</span>
          {entry.output && (
            <div className="mt-0.5">{entry.output.slice(0, 600)}</div>
          )}
        </>
      )}
      {entry.stopped && <span className="text-muted">  [stopped]</span>}
    </motion.div>
  );
}