import { useState } from "react";
import type { FormEvent } from "react";
import { AnimatePresence, motion } from "framer-motion";

interface Props {
  turnInProgress: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}

export function Composer({ turnInProgress, onSend, onStop }: Props) {
  const [draft, setDraft] = useState("");
  const [focused, setFocused] = useState(false);

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!draft.trim()) return;
    onSend(draft);
    setDraft("");
  }

  return (
    <form
      onSubmit={submit}
      className={[
        "mx-auto flex max-w-3xl overflow-hidden rounded-2xl border bg-panel shadow-lg transition-shadow",
        focused ? "border-gold/70 shadow-gold/10" : "border-goldDim/25",
      ].join(" ")}
    >
      <input
        dir="auto"
        autoComplete="off"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        placeholder="اكتب رسالة… / type a message…"
        style={{ unicodeBidi: "plaintext" }}
        className="flex-1 bg-transparent px-4 py-3.5 text-[14.5px] text-cream outline-none placeholder:text-muted"
      />
      {/* [ANIM-1] Crossfade + tap/hover feedback instead of an instant swap */}
      <AnimatePresence mode="wait" initial={false}>
        {turnInProgress ? (
          <motion.button
            key="stop"
            type="button"
            onClick={onStop}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            whileTap={{ scale: 0.94 }}
            whileHover={{ scale: 1.03 }}
            className="border-l border-goldDim/20 px-5 font-bold text-rust transition-colors hover:bg-rust/15"
          >
            Stop
          </motion.button>
        ) : (
          <motion.button
            key="send"
            type="submit"
            disabled={!draft.trim()}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            whileTap={draft.trim() ? { scale: 0.94 } : undefined}
            whileHover={draft.trim() ? { scale: 1.03 } : undefined}
            className="bg-gold px-5 font-bold text-ink transition-colors hover:bg-goldDim disabled:opacity-40"
          >
            Send
          </motion.button>
        )}
      </AnimatePresence>
    </form>
  );
}