import { motion } from "framer-motion";
import type { StatusUpdate } from "../types";

interface Props {
  status: StatusUpdate | null;
}

function formatUptime(sec: number): string {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

/** [ANIM-1] Whole badge flashes a brief gold highlight whenever its value
 * actually changes — keying the motion.div on `value` remounts it on every
 * change, which is exactly the trigger for a fresh initial→animate flash.
 * status_update only fires on real changes (connect, settings applied,
 * /model, /persona — never on a timer), so this correctly pulses only the
 * field(s) that actually changed, not all four on every tick. */
function Stat({ label, value }: { label: string; value: string }) {
  return (
    <motion.div
      key={value}
      initial={{ backgroundColor: "rgba(217,164,65,0.38)" }}
      animate={{ backgroundColor: "rgba(18,16,13,0.4)" }}
      transition={{ duration: 0.7, ease: "easeOut" }}
      className="flex items-baseline gap-1.5 rounded-full border border-goldDim/25 px-2.5 py-0.5"
    >
      <span className="text-[0.68em] uppercase tracking-wide text-goldDim">{label}</span>
      <span className="text-[0.82em] text-cream">{value}</span>
    </motion.div>
  );
}

export function StatusBanner({ status }: Props) {
  return (
    <div className="hidden items-center gap-1.5 sm:flex">
      <Stat label="model" value={status?.model ?? "—"} />
      <Stat label="persona" value={status?.persona ?? "—"} />
      <Stat label="tokens" value={status ? `${status.token_count}/${status.context_limit}` : "—"} />
      <Stat label="up" value={status ? formatUptime(status.uptime_seconds) : "—"} />
    </div>
  );
}