import { motion } from "framer-motion";
import type { ReactNode } from "react";
import type { ChatEntry } from "../types";
import { CodeBlock } from "./CodeBlock";
import { Spinner } from "./Spinner";

interface Props {
  entry: ChatEntry;
}

const FENCE_RE = /```(\w*)\n([\s\S]*?)```/g;

/** Splits message text on ```lang fenced code blocks, rendering each as a
 *  highlighted CodeBlock and everything else as plain wrapped text — a
 *  trailing unclosed fence (mid-stream) is left as literal text rather
 *  than guessed-closed. */
function renderMessageContent(text: string): ReactNode {
  const parts: ReactNode[] = [];
  let last = 0;
  let i = 0;
  FENCE_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = FENCE_RE.exec(text)) !== null) {
    if (m.index > last) parts.push(<span key={i++}>{text.slice(last, m.index)}</span>);
    parts.push(<CodeBlock key={i++} code={m[2].replace(/\n$/, "")} lang={m[1] || undefined} />);
    last = FENCE_RE.lastIndex;
  }
  if (last < text.length) parts.push(<span key={i++}>{text.slice(last)}</span>);
  return parts.length ? parts : text;
}

/**
 * `dir="auto"` + `unicode-bidi: plaintext` (not `rtl`) is the important
 * bit here — it lets each bubble pick its own direction from its own
 * text, so an Arabic reply and an English one sit naturally in the same
 * thread instead of the whole page flipping. Carried over deliberately
 * from the static build's Arabic-input fix.
 */
export function MessageBubble({ entry }: Props) {
  const isUser = entry.role === "user";
  // [SPIN-1] Spin until the first real token arrives — spinnerLabel is
  // cleared the instant token streaming starts (see useBeaverSocket).
  const showSpinner = entry.pending && entry.spinnerLabel !== undefined && entry.text === "";

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: "easeOut" }}
      dir="auto"
      style={{ unicodeBidi: "plaintext" }}
      className={[
        "max-w-[82%] rounded-2xl px-4 py-2.5 text-[14.5px] leading-relaxed whitespace-pre-wrap break-words shadow-sm",
        isUser
          ? "self-end bg-gradient-to-br from-goldDim/25 to-goldDim/15 border border-goldDim/50 text-cream"
          : "self-start bg-panel border border-goldDim/20 text-cream",
      ].join(" ")}
    >
      {!isUser && (
        <span className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-goldDim/80">
          <span>beaver</span>
          {(entry.persona || entry.model) && (
            <span className="rounded-full border border-goldDim/40 px-1.5 py-0.5 text-[9.5px] font-normal normal-case tracking-normal text-muted">
              {[entry.persona, entry.model].filter(Boolean).join(" · ")}
            </span>
          )}
        </span>
      )}
      {showSpinner ? (
        <Spinner label={entry.spinnerLabel!} />
      ) : entry.text.includes("```") ? (
        renderMessageContent(entry.text)
      ) : (
        entry.text
      )}
      {entry.pending && !showSpinner && (
        <span className="ml-0.5 inline-block w-[2px] h-[1em] align-middle bg-gold animate-pulse" />
      )}
      {entry.stopped && <span className="text-muted">  [stopped]</span>}
    </motion.div>
  );
}