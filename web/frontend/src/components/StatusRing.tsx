import { motion } from "framer-motion";

export type RingState = "running" | "ok" | "error" | "stopped";

interface Props {
  state: RingState;
  size?: number;
}

const STATE_COLOR: Record<RingState, string> = {
  running: "#e0a52f", // amber
  ok: "#7fa66b", // sage
  error: "#c1666b", // rust
  stopped: "#8f887c", // muted
};

/**
 * A small circular badge that pulses outward while a tool/turn is running,
 * then settles into a solid glyph once it resolves. Concentric rings
 * echo the "beaver gnawing a ring into wood" motif from the loading
 * phrases, instead of a generic spinner — the one bit of signature
 * motion this UI leans on, reused everywhere something is "in flight".
 */
export function StatusRing({ state, size = 18 }: Props) {
  const color = STATE_COLOR[state];

  return (
    <span
      className="relative inline-flex shrink-0 items-center justify-center"
      style={{ width: size, height: size }}
    >
      {state === "running" && (
        <>
          <motion.span
            className="absolute inset-0 rounded-full"
            style={{ border: `1px solid ${color}` }}
            initial={{ scale: 0.6, opacity: 0.7 }}
            animate={{ scale: [0.6, 1.8], opacity: [0.7, 0] }}
            transition={{ duration: 1.6, repeat: Infinity, ease: "easeOut" }}
          />
          <motion.span
            className="absolute inset-0 rounded-full"
            style={{ border: `1px solid ${color}` }}
            initial={{ scale: 0.6, opacity: 0.7 }}
            animate={{ scale: [0.6, 1.8], opacity: [0.7, 0] }}
            transition={{ duration: 1.6, repeat: Infinity, ease: "easeOut", delay: 0.8 }}
          />
        </>
      )}
      <span
        className="relative flex items-center justify-center rounded-full"
        style={{
          width: size * 0.6,
          height: size * 0.6,
          background: state === "running" ? "transparent" : color,
          border: `1.5px solid ${color}`,
          boxShadow: state === "running" ? `0 0 6px ${color}99` : `0 0 5px ${color}66`,
        }}
      >
        {state === "ok" && (
          <svg viewBox="0 0 10 10" width="7" height="7">
            <path d="M1.5 5.2 L4 7.6 L8.5 2.2" fill="none" stroke="#12100d" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        )}
        {state === "error" && (
          <svg viewBox="0 0 10 10" width="6" height="6">
            <path d="M2 2 L8 8 M8 2 L2 8" stroke="#12100d" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        )}
        {state === "stopped" && (
          <span className="block h-[5px] w-[5px] rounded-[1px]" style={{ background: "#12100d" }} />
        )}
        {state === "running" && (
          <motion.span
            className="block rounded-full"
            style={{ width: size * 0.22, height: size * 0.22, background: color }}
            animate={{ opacity: [1, 0.4, 1] }}
            transition={{ duration: 1, repeat: Infinity, ease: "easeInOut" }}
          />
        )}
      </span>
    </span>
  );
}
